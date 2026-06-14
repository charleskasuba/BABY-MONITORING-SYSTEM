#!/usr/bin/env python3
import os
import sys
import time
import json
import random
import threading
import platform
import logging
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = "baby-monitor-secret-key"
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize Supabase (with graceful fallback)
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
supabase = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client, Client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("Supabase client connected: %s", SUPABASE_URL[:30] + "...")
    except Exception as e:
        log.warning("Supabase init failed (app will run without DB): %s", e)
else:
    log.warning("SUPABASE_URL/KEY not set — running without database")

# ---------------------------------------------------------------------------
# Platform detection: Raspberry Pi vs others (Windows/macOS/Linux desktop)
# ---------------------------------------------------------------------------
IS_RASPBERRY_PI = False
try:
    with open("/proc/device-tree/model", "r") as f:
        if "Raspberry Pi" in f.read():
            IS_RASPBERRY_PI = True
except Exception:
    pass

if IS_RASPBERRY_PI:
    import RPi.GPIO as GPIO
    import board
    import adafruit_dht
    from picamera2 import Picamera2
    import cv2
    import numpy as np

    # GPIO Setup
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    PIR_PIN = 17
    SOUND_PIN = 27
    WETNESS_PIN = 22
    BUZZER_PIN = 18

    GPIO.setup(PIR_PIN, GPIO.IN)
    GPIO.setup(SOUND_PIN, GPIO.IN)
    GPIO.setup(WETNESS_PIN, GPIO.IN)
    GPIO.setup(BUZZER_PIN, GPIO.OUT)

    # Initialize DHT22
    dht_device = adafruit_dht.DHT22(board.D4)

    # Initialize Camera
    picam2 = Picamera2()
    camera_config = picam2.create_video_configuration(main={"size": (640, 480)})
    picam2.configure(camera_config)
    picam2.start()

    log.info("[HARDWARE] Running on Raspberry Pi with real sensors")
else:
    log.info("[SIMULATION] Running in simulation mode (no Raspberry Pi detected)")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
current_data = {
    "temperature": 0,
    "humidity": 0,
    "motion": False,
    "sound": 0,
    "wetness": False,
    "last_update": None,
}

# Track when real Pi data was last received (so simulation doesn't overwrite it)
_last_real_data = 0
}


def simulate_sensor():
    """Generate plausible simulated sensor data for development/testing."""
    temp = round(random.uniform(21.0, 26.0), 1)
    hum = round(random.uniform(42.0, 62.0), 1)
    motion = random.choice([True, False])
    sound = random.choice([0, 1])
    wetness = random.choice([False, False, False, True])  # 1 in 4 chance
    return temp, hum, motion, sound, wetness


def read_real_sensors():
    """Read real hardware sensors on Raspberry Pi."""
    temp = dht_device.temperature
    hum = dht_device.humidity
    motion = GPIO.input(PIR_PIN) == 1
    sound = GPIO.input(SOUND_PIN)
    wetness = GPIO.input(WETNESS_PIN) == 1
    return (
        round(temp if temp else 0, 1),
        round(hum if hum else 0, 1),
        motion,
        sound,
        wetness,
    )


def check_abnormal(temp, hum):
    temp_min = float(os.getenv("TEMP_MIN", 20))
    temp_max = float(os.getenv("TEMP_MAX", 25))
    hum_min = float(os.getenv("HUMIDITY_MIN", 40))
    hum_max = float(os.getenv("HUMIDITY_MAX", 60))

    if temp and (temp < temp_min or temp > temp_max):
        return True
    if hum and (hum < hum_min or hum > hum_max):
        return True
    return False


def check_alerts(temp, hum, wetness):
    temp_min = float(os.getenv("TEMP_MIN", 20))
    temp_max = float(os.getenv("TEMP_MAX", 25))
    hum_min = float(os.getenv("HUMIDITY_MIN", 40))
    hum_max = float(os.getenv("HUMIDITY_MAX", 60))

    alerts = []

    if temp:
        if temp < temp_min:
            alerts.append(
                {
                    "alert_type": "temperature",
                    "severity": "critical",
                    "message": f"⚠️ Temperature too low: {temp}°C",
                }
            )
        elif temp > temp_max:
            alerts.append(
                {
                    "alert_type": "temperature",
                    "severity": "critical",
                    "message": f"⚠️ Temperature too high: {temp}°C",
                }
            )

    if hum:
        if hum < hum_min:
            alerts.append(
                {
                    "alert_type": "humidity",
                    "severity": "warning",
                    "message": f"💧 Humidity too low: {hum}%",
                }
            )
        elif hum > hum_max:
            alerts.append(
                {
                    "alert_type": "humidity",
                    "severity": "warning",
                    "message": f"💧 Humidity too high: {hum}%",
                }
            )

    if wetness:
        alerts.append(
            {
                "alert_type": "wetness",
                "severity": "warning",
                "message": "🚼 Diaper wetness detected!",
            }
        )

    if supabase is None:
        return

    for alert in alerts:
        try:
            supabase.table("alerts").insert(alert).execute()
            socketio.emit("new_alert", alert)
            log.info("Alert: %s", alert["message"])
        except Exception as e:
            log.error("Alert error: %s", e)


def trigger_buzzer():
    if IS_RASPBERRY_PI:
        GPIO.output(BUZZER_PIN, True)
        time.sleep(0.5)
        GPIO.output(BUZZER_PIN, False)


def sensor_loop():
    """Main sensor reading loop – runs in a background thread."""
    while True:
        try:
            if IS_RASPBERRY_PI:
                temperature, humidity, motion, sound, wetness = read_real_sensors()
            else:
                # On cloud: skip simulation if real Pi data arrived within last 10s
                if time.time() - _last_real_data < 10:
                    time.sleep(2)
                    continue
                temperature, humidity, motion, sound, wetness = simulate_sensor()

            current_data["temperature"] = temperature
            current_data["humidity"] = humidity
            current_data["motion"] = motion
            current_data["sound"] = sound
            current_data["wetness"] = wetness
            current_data["last_update"] = datetime.now().isoformat()

            reading = {
                "temperature": temperature,
                "humidity": humidity,
                "motion_detected": motion,
                "sound_level": 1 if sound else 0,
                "wetness_detected": wetness,
                "is_abnormal": check_abnormal(temperature, humidity),
            }
            if supabase is not None:
                supabase.table("sensor_readings").insert(reading).execute()

            check_alerts(temperature, humidity, wetness)
            socketio.emit("sensor_update", current_data)

            if wetness:
                trigger_buzzer()

            time.sleep(5)
        except Exception as e:
            log.error("Sensor error: %s", e)
            time.sleep(2)


# ---------------------------------------------------------------------------
# Video streaming – real on Pi, simulated placeholder on other platforms
# ---------------------------------------------------------------------------
if IS_RASPBERRY_PI:
    import cv2

    def generate_frames():
        while True:
            try:
                frame = picam2.capture_array()
                ret, buffer = cv2.imencode(".jpg", frame)
                frame_bytes = buffer.tobytes()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                )
                time.sleep(0.1)
            except Exception as e:
                log.error("Stream error: %s", e)
                time.sleep(1)

else:
    # Minimal 1x1 JPEG for environments without camera/OpenCV
    PLACEHOLDER_JPEG = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c"
        b"\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c"
        b"\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xdb\x00C\x01\t\t\t\x0c"
        b"\x0b\x0c\x18\r\r\x182!\x1c!2222222222222222222222222222222222222222"
        b"2222222222222\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\"\x00\x02\x11"
        b"\x01\x03\x11\x01\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01"
        b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t"
        b"\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04"
        b"\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q"
        b"\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18"
        b"\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86"
        b"\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5"
        b"\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4"
        b"\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2"
        b"\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9"
        b"\xfa\xff\xc4\x00\x1f\x01\x00\x03\x01\x01\x01\x01\x01\x01\x01\x01\x01"
        b"\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff"
        b"\xc4\x00\xb5\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04\x04\x00"
        b"\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x06\x12AQ\x07aq\x13\"2\x81\x08"
        b"\x14B\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\n\x16$4\xe1%\xf1\x17\x18\x19"
        b"\x1a&'()*56789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x82\x83\x84\x85\x86\x87"
        b"\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6"
        b"\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5"
        b"\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3\xe4"
        b"\xe5\xe6\xe7\xe8\xe9\xea\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda"
        b"\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xf9\xfe\x8a(\xa0\x0f\xff"
        b"\xd9"
    )

    def generate_frames():
        while True:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + PLACEHOLDER_JPEG + b"\r\n"
            )
            time.sleep(1)

# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/live")
def live():
    return render_template("live.html")


@app.route("/history")
def history():
    return render_template("history.html")


@app.route("/settings")
def settings():
    return render_template("settings.html")


@app.route("/api/current_data")
def api_current_data():
    return jsonify(current_data)


@app.route("/api/history")
def api_history():
    if supabase is None:
        return jsonify([])
    limit = request.args.get("limit", 100, type=int)
    try:
        response = (
            supabase.table("sensor_readings")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return jsonify(response.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/alerts")
def api_alerts():
    if supabase is None:
        return jsonify([])
    limit = request.args.get("limit", 50, type=int)
    try:
        response = (
            supabase.table("alerts")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return jsonify(response.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if supabase is None:
        return jsonify({"temp_min": 20, "temp_max": 25, "humidity_min": 40, "humidity_max": 60})
    if request.method == "GET":
        try:
            response = supabase.table("settings").select("*").eq("id", 1).execute()
            if response.data:
                return jsonify(response.data[0])
            return jsonify({})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif request.method == "POST":
        try:
            data = request.json
            if supabase is not None:
                supabase.table("settings").update(data).eq("id", 1).execute()

            if "temp_min" in data:
                os.environ["TEMP_MIN"] = str(data["temp_min"])
            if "temp_max" in data:
                os.environ["TEMP_MAX"] = str(data["temp_max"])
            if "humidity_min" in data:
                os.environ["HUMIDITY_MIN"] = str(data["humidity_min"])
            if "humidity_max" in data:
                os.environ["HUMIDITY_MAX"] = str(data["humidity_max"])

            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/video_feed")
def video_feed():
    return Response(
        generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/api/clear_alerts", methods=["POST"])
def clear_alerts():
    if supabase is None:
        return jsonify({"success": True})
    try:
        supabase.table("alerts").update({"is_read": True}).neq("is_read", True).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Raspberry Pi ingestion endpoint
# ---------------------------------------------------------------------------
@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Receive sensor readings from the Raspberry Pi."""
    data = request.get_json(silent=True) or {}
    required = ["temperature", "humidity"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing required fields: temperature, humidity"}), 400

    temp = data.get("temperature")
    hum = data.get("humidity")
    motion = data.get("motion_detected", False)
    sound = data.get("sound_level", 0)
    wetness = data.get("wetness_detected", False)

    # Mark that real data arrived (stops simulation loop from overwriting)
    global _last_real_data
    _last_real_data = time.time()

    # Update global state (used by /api/current_data, WebSocket, etc.)
    current_data["temperature"] = temp
    current_data["humidity"] = hum
    current_data["motion"] = motion
    current_data["sound"] = sound
    current_data["wetness"] = wetness
    current_data["last_update"] = datetime.now().isoformat()

    # Persist to Supabase
    if supabase is not None:
        reading = {
            "temperature": temp,
            "humidity": hum,
            "motion_detected": motion,
            "sound_level": sound,
            "wetness_detected": wetness,
            "is_abnormal": check_abnormal(temp, hum),
        }
        try:
            supabase.table("sensor_readings").insert(reading).execute()
        except Exception as e:
            log.error("DB insert error: %s", e)

    # Check alert thresholds
    check_alerts(temp, hum, wetness)

    # Broadcast live update
    socketio.emit("sensor_update", current_data)

    return jsonify({"status": "ok", "abnormal": check_abnormal(temp, hum)})


# ---------------------------------------------------------------------------
# Start background sensor thread
# ---------------------------------------------------------------------------
sensor_thread = threading.Thread(target=sensor_loop, daemon=True)
sensor_thread.start()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mode = "SIMULATION" if not IS_RASPBERRY_PI else "PRODUCTION"
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("RENDER") is None
    log.info("Baby Monitor Server Starting [%s] on port %s...", mode, port)

    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        log.info("Network:   http://%s:%s", local_ip, port)
    except Exception:
        pass

    socketio.run(app, host="0.0.0.0", port=port, debug=debug,
                 allow_unsafe_werkzeug=True)
