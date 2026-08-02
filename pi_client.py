#!/usr/bin/env python3
import os
import sys
import threading
import time
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

# Sensor pins
WATER_PIN = 17
MOTION_PIN = 27
SOUND_PIN = 22
DHT_PIN = board.D4

# Recording
RECORD_DURATION = 10
VIDEO_FOLDER = "recordings"
INTERVAL = 3

# Live stream frame upload interval (seconds) — how often a JPEG is sent to cloud
FRAME_UPLOAD_INTERVAL = 2.0

# Wetness sensor: True if sensor outputs HIGH (1) when wet, else False
WETNESS_ACTIVE_HIGH = False

# Thread lock so recording and streaming never grab the camera at the same time
is_recording = False
camera_busy = threading.Lock()

# ---------------------------------------------------------------------------
# Camera setup: Native Picamera2 API
# ---------------------------------------------------------------------------
CAMERA = None
try:
    from picamera2 import Picamera2
    CAMERA = Picamera2()
    cam_config = CAMERA.create_video_configuration(main={"size": (640, 480)})
    CAMERA.configure(cam_config)
    CAMERA.start()
    print("[CAMERA] Picamera2 initialized successfully.")
except Exception as e:
    print(f"[CAMERA] No camera detected or init error: {e}")

# ---------------------------------------------------------------------------
# Sensor setup
# ---------------------------------------------------------------------------
water_sensor = DigitalInputDevice(WATER_PIN)
motion_sensor = DigitalInputDevice(MOTION_PIN)
sound_sensor = DigitalInputDevice(SOUND_PIN)
dht_sensor = adafruit_dht.DHT22(DHT_PIN)

os.makedirs(VIDEO_FOLDER, exist_ok=True)

print("=" * 65)
print("  Smart Baby Monitor — Threaded Pi Client")
print(f"  Sending to: {SERVER_URL}")
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
# Video recording (background thread, on motion/sound)
# ---------------------------------------------------------------------------
def _video_recorder_worker(reason):
    global is_recording
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(VIDEO_FOLDER, f"ALERT_{reason}_{timestamp}.h264")
    print(f"\n  [RECORDING] {RECORD_DURATION}s clip — {reason}")

    with camera_busy:
        try:
            CAMERA.start_recording(filename)
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
# Live frame upload (background thread) — streams a JPEG to the cloud
# ---------------------------------------------------------------------------
def frame_upload_loop():
    import io
    while True:
        sleep(FRAME_UPLOAD_INTERVAL)
        if CAMERA is None:
            continue
        # Don't capture while a recording is in progress
        if is_recording:
            continue
        if not camera_busy.acquire(blocking=False):
            continue
        try:
            # picamera2's built-in encoder writes JPEG without OpenCV/PIL
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
    # Start the live frame upload thread
    if CAMERA is not None:
        upload_thread = threading.Thread(target=frame_upload_loop, daemon=True)
        upload_thread.start()
        print("[STREAM] Live video upload active")

    while True:
        # Read sensors
        wet = water_sensor.value
        water_active = (wet == 1) if WETNESS_ACTIVE_HIGH else (wet == 0)
        motion_active = motion_sensor.value == 1
        sound_active = sound_sensor.value == 1

        try:
            temp_c = dht_sensor.temperature
            humidity = dht_sensor.humidity
        except RuntimeError:
            temp_c = None
            humidity = None

        w = "WET" if water_active else "dry"
        m = "MOTION" if motion_active else "quiet"
        s = "NOISE" if sound_active else "quiet"
        t = f"{temp_c:.1f}C/{humidity:.1f}%" if temp_c is not None else "--/--"
        print(f"  Water:{w:<6} Motion:{m:<8} Sound:{s:<8} | {t}")

        # Video recording triggers (non-blocking)
        if motion_active:
            trigger_async_recording("MOTION")
        elif sound_active:
            trigger_async_recording("SOUND")

        # POST sensor data to cloud
        post_data(temp_c, humidity, motion_active, sound_active, water_active)

        sleep(INTERVAL)

except KeyboardInterrupt:
    print("\n[!] Shutdown.")
    dht_sensor.exit()
    if CAMERA:
        CAMERA.stop()
