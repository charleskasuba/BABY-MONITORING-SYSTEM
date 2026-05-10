#!/usr/bin/env python3
import os
import sys
import time
import json
import random
import threading
import platform
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = "baby-monitor-secret-key"
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize Supabase
supabase: Client = create_client(
    os.getenv("SUPABASE_URL", ""),
    os.getenv("SUPABASE_KEY", ""),
)

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

    print("[HARDWARE] Running on Raspberry Pi with real sensors")
else:
    print("[SIMULATION] Running in simulation mode (no Raspberry Pi detected)")

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

    for alert in alerts:
        try:
            supabase.table("alerts").insert(alert).execute()
            socketio.emit("new_alert", alert)
            print(f"Alert: {alert['message']}")
        except Exception as e:
            print(f"Alert error: {e}")


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
            supabase.table("sensor_readings").insert(reading).execute()

            check_alerts(temperature, humidity, wetness)
            socketio.emit("sensor_update", current_data)

            if wetness:
                trigger_buzzer()

            time.sleep(5)
        except Exception as e:
            print(f"Sensor error: {e}")
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
                print(f"Stream error: {e}")
                time.sleep(1)

else:
    def generate_frames():
        """Generate a placeholder video frame for non-Pi environments."""
        # Create a simple blue gradient placeholder image
        import numpy as np
        import cv2

        while True:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(frame, "Camera Not Connected", (100, 220),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.putText(frame, "(Simulation Mode)", (150, 270),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
            # Draw a border
            cv2.rectangle(frame, (0, 0), (639, 479), (100, 100, 255), 3)
            ret, buffer = cv2.imencode(".jpg", frame)
            frame_bytes = buffer.tobytes()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
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
    try:
        supabase.table("alerts").update({"is_read": True}).neq("is_read", True).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    print(f"Baby Monitor Server Starting [{mode}]...")
    print("Dashboard: http://localhost:5000")

    # Get local IP for network access
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        print(f"Network:   http://{local_ip}:5000")
    except Exception:
        pass

    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
