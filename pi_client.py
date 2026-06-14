#!/usr/bin/env python3
import os
import sys
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

# ---------------------------------------------------------------------------
# Camera setup: try picamera2 first, fall back to OpenCV
# ---------------------------------------------------------------------------
CAMERA = None
try:
    from picamera2 import Picamera2
    CAMERA = Picamera2()
    cam_config = CAMERA.create_video_configuration(main={"size": (640, 480)})
    CAMERA.configure(cam_config)
    CAMERA.start()
    print("[CAMERA] picamera2 initialized")
except Exception:
    import cv2 as _cv2
    os.environ["OPENCV_LOG_LEVEL"] = "ERROR"
    os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"
    try:
        cap = _cv2.VideoCapture(0)
        if cap.isOpened():
            cap.release()
            CAMERA = "cv2"
            print("[CAMERA] OpenCV V4L2 initialized")
        else:
            print("[CAMERA] No camera detected — recording disabled")
    except Exception:
        print("[CAMERA] No camera detected — recording disabled")

# ---------------------------------------------------------------------------
# Sensor setup
# ---------------------------------------------------------------------------
water_sensor = DigitalInputDevice(WATER_PIN)
motion_sensor = DigitalInputDevice(MOTION_PIN)
sound_sensor = DigitalInputDevice(SOUND_PIN)
dht_sensor = adafruit_dht.DHT22(DHT_PIN)

os.makedirs(VIDEO_FOLDER, exist_ok=True)

print("=" * 65)
print("  Smart Baby Monitor — Pi Client")
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
        r = requests.post(f"{SERVER_URL}/api/ingest",
                          json=payload, timeout=10)
        if r.status_code != 200:
            print(f"  [POST ERROR] HTTP {r.status_code}: {r.text[:100]}")
        return r.ok
    except requests.RequestException as e:
        print(f"  [NETWORK ERROR] {e}")
        return False


def record_security_event(reason):
    """Record a 10s video clip using picamera2 or OpenCV."""
    if CAMERA is None:
        print(f"  [CAM SKIP] {reason} — no camera")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(VIDEO_FOLDER, f"ALERT_{reason}_{timestamp}.avi")
    print(f"  [RECORDING] 10s clip — {reason}")

    try:
        if CAMERA == "cv2":
            import cv2
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            fourcc = cv2.VideoWriter_fourcc(*"XVID")
            out = cv2.VideoWriter(filename, fourcc, 20.0, (640, 480))
            start = time_now()
            while int(time_now() - start) < RECORD_DURATION:
                ret, frame = cap.read()
                if not ret:
                    break
                out.write(frame)
                sleep(0.05)
            cap.release()
            out.release()
        else:
            # picamera2
            import cv2
            import numpy as np
            fourcc = cv2.VideoWriter_fourcc(*"XVID")
            out = cv2.VideoWriter(filename, fourcc, 20.0, (640, 480))
            start = time_now()
            while int(time_now() - start) < RECORD_DURATION:
                frame = CAMERA.capture_array()
                out.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                sleep(0.05)
            out.release()
        print(f"  [SAVED] {filename}")
    except Exception:
        print(f"  [CAM CLEAR] Triggered by {reason} (bypassed)")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
try:
    while True:
        # Read sensors
        water_active = water_sensor.value == 0
        motion_active = motion_sensor.value == 1
        sound_active = sound_sensor.value == 1

        try:
            temp_c = dht_sensor.temperature
            humidity = dht_sensor.humidity
        except RuntimeError:
            temp_c = None
            humidity = None

        # Print to console
        w = "WET" if water_active else "dry"
        m = "MOTION" if motion_active else "quiet"
        s = "NOISE" if sound_active else "quiet"
        t = f"{temp_c:.1f}C/{humidity:.1f}%" if temp_c is not None else "--/--"
        print(f"  Water:{w:<6} Motion:{m:<8} Sound:{s:<8} | {t}")

        # POST to cloud
        ok = post_data(temp_c, humidity, motion_active, sound_active, water_active)

        # Video recording on alerts
        if motion_active:
            record_security_event("MOTION")
        elif sound_active:
            record_security_event("SOUND")

        sleep(INTERVAL)

except KeyboardInterrupt:
    print("\n[!] Shutdown.")
    dht_sensor.exit()
