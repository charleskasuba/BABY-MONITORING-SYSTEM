#!/usr/bin/env python3
import os
import sys
import subprocess
import shutil
import threading
from time import sleep, time as time_now
from datetime import datetime

import board
import adafruit_dht
from gpiozero import DigitalInputDevice
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SERVER_URL = "https://baby-monitoring-system.onrender.com"

# "hls" = continuous true video (needs ffmpeg + libcamera-vid), "mjpeg" = photo snapshots
STREAM_MODE = os.getenv("STREAM_MODE", "hls").lower()

# Sensor pins (BCM GPIO)
WATER_PIN = 17
MOTION_PIN = 27
SOUND_PIN = 22
DHT_PIN = board.D4

# Recording
RECORD_DURATION = 10
VIDEO_FOLDER = "recordings"

# How often sensor data is sent to the web dashboard (seconds)
INTERVAL = 10

# Live stream photo interval (seconds) — only used in MJPEG fallback mode
FRAME_UPLOAD_INTERVAL = 2.0

# Minimum seconds between alert video clips (stops a stuck sensor hogging the camera)
RECORD_COOLDOWN = 30

# Wetness sensor logic: True if sensor outputs HIGH (1) when wet, False if LOW (0)
WETNESS_ACTIVE_HIGH = False

# HLS live video settings
HLS_DIR = "hls_stream"
HLS_WIDTH = 640
HLS_HEIGHT = 480
HLS_FPS = 15
HLS_BITRATE = 500000
HLS_SEGMENT_SECS = 4
HLS_LIST_SIZE = 4

# Thread lock so recording and streaming never grab the camera at the same time
is_recording = False
camera_busy = threading.Lock()

# Edge-trigger bookkeeping so a stuck/noisy sensor can't trigger endless recordings
last_record_ts = 0
prev_motion = False
prev_sound = False

os.makedirs(VIDEO_FOLDER, exist_ok=True)


# ---------------------------------------------------------------------------
# Camera setup
#   HLS mode  : libcamera-vid owns the camera (true video streaming)
#   MJPEG mode: picamera2 takes photo snapshots for the live feed
# ---------------------------------------------------------------------------
CAMERA = None


def init_picamera():
    global CAMERA
    try:
        from picamera2 import Picamera2
        CAMERA = Picamera2()
        cam_config = CAMERA.create_video_configuration(main={"size": (HLS_WIDTH, HLS_HEIGHT)})
        CAMERA.configure(cam_config)
        CAMERA.start()
        print("[CAMERA] Picamera2 initialized successfully.")
    except Exception as e:
        print(f"[CAMERA] No camera detected or init error: {e}")


if STREAM_MODE == "mjpeg":
    init_picamera()
else:
    if not (shutil.which("ffmpeg") and shutil.which("libcamera-vid")):
        print("[HLS] ffmpeg or libcamera-vid not found — falling back to MJPEG photo mode")
        print("[HLS] Install ffmpeg with: sudo apt install ffmpeg")
        STREAM_MODE = "mjpeg"
        init_picamera()
    else:
        os.makedirs(HLS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Sensor setup
# ---------------------------------------------------------------------------
water_sensor = DigitalInputDevice(WATER_PIN)

# Software bounce filtering on Motion sensor to reduce false motion triggers
motion_sensor = DigitalInputDevice(MOTION_PIN, pull_up=False, bounce_time=0.2)

sound_sensor = DigitalInputDevice(SOUND_PIN)

# Initialize DHT with use_pulseio=False to avoid C-level pulse overflow crashes
dht_sensor = adafruit_dht.DHT22(DHT_PIN, use_pulseio=False)

print("=" * 65)
print("  Baby Cradle Monitoring System — Threaded Pi Client")
print(f"  Sending to: {SERVER_URL}  |  Video mode: {STREAM_MODE.upper()}")
print("=" * 65)


def post_data(temp, hum, motion, sound, wetness):
    """POST sensor readings to the cloud server."""
    payload = {
        "temperature": round(temp, 1) if temp is not None else 0,
        "humidity": round(hum, 1) if hum is not None else 0,
        "motion_detected": motion,
        "sound_level": 1 if sound else 0,
        "wetness_detected": wetness,
    }
    try:
        r = requests.post(f"{SERVER_URL}/api/ingest", json=payload, timeout=10)
        if r.status_code != 200:
            print(f"  [POST ERROR] HTTP {r.status_code}: {r.text[:100]}")
        return r.ok
    except requests.RequestException as e:
        print(f"  [NETWORK ERROR] {e}")
        return False


# ---------------------------------------------------------------------------
# HLS live video (true video throughout)
# ---------------------------------------------------------------------------
def start_hls_pipeline():
    """Pipe libcamera-vid H.264 into ffmpeg which produces HLS segments."""
    os.makedirs(HLS_DIR, exist_ok=True)
    capture = [
        "libcamera-vid", "-t", "0", "--inline",
        "--width", str(HLS_WIDTH), "--height", str(HLS_HEIGHT),
        "--framerate", str(HLS_FPS), "--codec", "h264",
        "--profile", "baseline", "--level", "4",
        "--bitrate", str(HLS_BITRATE), "--output", "-",
    ]
    mux = [
        "ffmpeg", "-loglevel", "warning",
        "-f", "h264", "-i", "pipe:0", "-an", "-c", "copy",
        "-f", "hls", "-hls_time", str(HLS_SEGMENT_SECS),
        "-hls_list_size", str(HLS_LIST_SIZE),
        "-hls_flags", "delete_segments",
        os.path.join(HLS_DIR, "index.m3u8"),
    ]
    p1 = subprocess.Popen(capture, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen(mux, stdin=p1.stdout, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    if p1.stdout:
        p1.stdout.close()
    return p1, p2


def hls_loop():
    """Supervise the HLS pipeline — restart it if it ever dies."""
    while True:
        try:
            p1, p2 = start_hls_pipeline()
            p2.wait()
            p1.wait()
        except Exception as e:
            print(f"[HLS] pipeline error: {e}")
        sleep(2)


def hls_uploader_loop():
    """Upload new/changed HLS segments + playlist to the cloud server."""
    sent = {}
    while True:
        try:
            if not os.path.isdir(HLS_DIR):
                sleep(1)
                continue
            for fname in sorted(os.listdir(HLS_DIR)):
                if not (fname.endswith(".m3u8") or fname.endswith(".ts")):
                    continue
                path = os.path.join(HLS_DIR, fname)
                mtime = os.path.getmtime(path)
                if sent.get(path) == mtime:
                    continue
                with open(path, "rb") as f:
                    content = f.read()
                r = requests.post(
                    f"{SERVER_URL}/api/hls_upload",
                    params={"filename": fname},
                    data=content, timeout=8,
                )
                if r.ok:
                    sent[path] = mtime
        except Exception:
            pass
        sleep(1)


# ---------------------------------------------------------------------------
# Video recording (background thread, on motion/sound) — MJPEG mode only
# ---------------------------------------------------------------------------
def _video_recorder_worker(reason):
    global is_recording
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(VIDEO_FOLDER, f"ALERT_{reason}_{timestamp}.h264")
    print(f"\n  [RECORDING] {RECORD_DURATION}s clip — {reason}")

    with camera_busy:
        try:
            # Fixed Picamera2 recording workflow with explicit encoder and FileOutput
            encoder = CAMERA.create_encoder()
            output = __import__("picamera2.outputs", fromlist=["FileOutput"]).FileOutput(filename)
            CAMERA.start_recording(encoder, output)
            sleep(RECORD_DURATION)
            CAMERA.stop_recording()
            print(f"  [SAVED] {filename}\n")
        except Exception as e:
            print(f"  [CAM RECORD ERROR] {e}")
        finally:
            is_recording = False


def trigger_async_recording(reason):
    global is_recording
    if CAMERA is None or is_recording:
        return
    is_recording = True
    video_thread = threading.Thread(target=_video_recorder_worker, args=(reason,))
    video_thread.daemon = True
    video_thread.start()


# ---------------------------------------------------------------------------
# Live frame upload (background thread) — MJPEG fallback mode
# ---------------------------------------------------------------------------
def frame_upload_loop():
    import io
    while True:
        sleep(FRAME_UPLOAD_INTERVAL)
        if CAMERA is None:
            continue
        if is_recording:
            continue
        if not camera_busy.acquire(blocking=False):
            continue
        try:
            buf = io.BytesIO()
            CAMERA.capture_file(buf, format="jpeg")
            jpeg = buf.getvalue()
            try:
                requests.post(f"{SERVER_URL}/api/upload_frame",
                              data=jpeg,
                              headers={"Content-Type": "image/jpeg"},
                              timeout=5)
            except requests.RequestException:
                pass
        except Exception:
            pass
        finally:
            camera_busy.release()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
try:
    # Start live video streaming
    if STREAM_MODE == "hls":
        threading.Thread(target=hls_loop, daemon=True).start()
        threading.Thread(target=hls_uploader_loop, daemon=True).start()
        print("[STREAM] HLS live video active")
    elif CAMERA is not None:
        threading.Thread(target=frame_upload_loop, daemon=True).start()
        print("[STREAM] MJPEG live video upload active")

    while True:
        # Read digital sensors
        wet = water_sensor.value
        water_active = (wet == 1) if WETNESS_ACTIVE_HIGH else (wet == 0)
        motion_active = motion_sensor.value == 1
        sound_active = sound_sensor.value == 1

        # Edge-triggered alert recording (MJPEG mode only; CAMERA is None in HLS)
        motion_trigger = motion_active and not prev_motion
        sound_trigger = sound_active and not prev_sound
        prev_motion = motion_active
        prev_sound = sound_active
        now = time_now()
        if (motion_trigger or sound_trigger) and now - last_record_ts >= RECORD_COOLDOWN:
            last_record_ts = now
            trigger_async_recording("MOTION" if motion_trigger else "SOUND")

        # Read temperature and humidity safely
        try:
            temp_c = dht_sensor.temperature
            humidity = dht_sensor.humidity
        except (RuntimeError, Exception):
            temp_c = None
            humidity = None

        w = "WET" if water_active else "dry"
        m = "MOTION" if motion_active else "quiet"
        s = "NOISE" if sound_active else "quiet"
        t = f"{temp_c:.1f}C/{humidity:.1f}%" if temp_c is not None else "--/--"
        print(f"  Water:{w:<6} Motion:{m:<8} Sound:{s:<8} | {t}")

        # Ingest sensor readings to web server
        post_data(temp_c, humidity, motion_active, sound_active, water_active)

        sleep(INTERVAL)

except KeyboardInterrupt:
    print("\n[!] Shutdown requested. Cleaning up...")
    try:
        dht_sensor.exit()
    except Exception:
        pass
    if CAMERA:
        CAMERA.stop()
    sys.exit(0)
