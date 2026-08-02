#!/usr/bin/env python3
import os
import sys
import time
import threading
import logging
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response
from flask_socketio import SocketIO
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

# Initialize Supabase (graceful fallback)
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
supabase = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("Supabase client connected: %s", SUPABASE_URL[:30] + "...")
    except Exception as e:
        log.warning("Supabase init failed (app will run without DB): %s", e)
else:
    log.warning("SUPABASE_URL/KEY not set — running without database")

# ---------------------------------------------------------------------------
# Global state — data is ONLY populated by POSTs from the Raspberry Pi
# ---------------------------------------------------------------------------
current_data = {
    "temperature": 0,
    "humidity": 0,
    "motion": False,
    "sound": 0,
    "wetness": False,
    "last_update": None,
}

# Latest JPEG frame uploaded by the Pi (served on /video_feed)
latest_frame = None
_frame_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Alert helpers
# ---------------------------------------------------------------------------
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
            alerts.append({"alert_type": "temperature", "severity": "critical",
                           "message": f"⚠️ Temperature too low: {temp}°C"})
        elif temp > temp_max:
            alerts.append({"alert_type": "temperature", "severity": "critical",
                           "message": f"⚠️ Temperature too high: {temp}°C"})

    if hum:
        if hum < hum_min:
            alerts.append({"alert_type": "humidity", "severity": "warning",
                           "message": f"💧 Humidity too low: {hum}%"})
        elif hum > hum_max:
            alerts.append({"alert_type": "humidity", "severity": "warning",
                           "message": f"💧 Humidity too high: {hum}%"})

    if wetness:
        alerts.append({"alert_type": "wetness", "severity": "warning",
                       "message": "🚼 Diaper wetness detected!"})

    if supabase is None:
        return

    for alert in alerts:
        try:
            supabase.table("alerts").insert(alert).execute()
            socketio.emit("new_alert", alert)
            log.info("Alert: %s", alert["message"])
        except Exception as e:
            log.error("Alert error: %s", e)


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
    defaults = {"temp_min": 20, "temp_max": 25,
                "humidity_min": 40, "humidity_max": 60, "alert_email": ""}
    if supabase is None:
        if request.method == "GET":
            return jsonify(defaults)
        return jsonify({"success": True})

    if request.method == "GET":
        try:
            response = supabase.table("settings").select("*").eq("id", 1).execute()
            if response.data:
                return jsonify(response.data[0])
            return jsonify(defaults)
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
# Raspberry Pi ingestion endpoint — THE ONLY data source
# ---------------------------------------------------------------------------
@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    data = request.get_json(silent=True) or {}
    if "temperature" not in data or "humidity" not in data:
        return jsonify({"error": "Missing required fields: temperature, humidity"}), 400

    temp = data.get("temperature")
    hum = data.get("humidity")
    motion = data.get("motion_detected", False)
    sound = data.get("sound_level", 0)
    wetness = data.get("wetness_detected", False)

    current_data["temperature"] = temp
    current_data["humidity"] = hum
    current_data["motion"] = motion
    current_data["sound"] = sound
    current_data["wetness"] = wetness
    current_data["last_update"] = datetime.now().isoformat()

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

    check_alerts(temp, hum, wetness)
    socketio.emit("sensor_update", current_data)

    return jsonify({"status": "ok", "abnormal": check_abnormal(temp, hum)})


# ---------------------------------------------------------------------------
# Video frame upload from the Pi + MJPEG streaming
# ---------------------------------------------------------------------------
@app.route("/api/upload_frame", methods=["POST"])
def api_upload_frame():
    """Receive a JPEG frame from the Pi camera."""
    global latest_frame
    data = request.get_data()
    if not data or len(data) < 100:
        return jsonify({"error": "Empty or invalid frame"}), 400
    with _frame_lock:
        latest_frame = data
    return jsonify({"status": "ok"})


@app.route("/video_feed")
def video_feed():
    def generate():
        last_served = None
        while True:
            with _frame_lock:
                frame = latest_frame
            if frame is not None and frame != last_served:
                last_served = frame
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
            time.sleep(0.2)
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ---------------------------------------------------------------------------
# Chatbot / assistant endpoint
# ---------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    msg = (data.get("message") or "").lower().strip()
    d = current_data

    temp = d.get("temperature") or "--"
    hum = d.get("humidity") or "--"
    tmin = os.getenv("TEMP_MIN", "20")
    tmax = os.getenv("TEMP_MAX", "25")
    hmin = os.getenv("HUMIDITY_MIN", "40")
    hmax = os.getenv("HUMIDITY_MAX", "60")
    motion_str = "moving" if d.get("motion") else "quiet/sleeping"
    diaper_str = "wet — needs changing" if d.get("wetness") else "dry"

    if any(w in msg for w in ["temp", "hot", "cold", "warm"]):
        reply = f"The current temperature is {temp}°C. Safe range is {tmin}–{tmax}°C."
        if temp != "--":
            if temp < float(tmin):
                reply += " It's **below** the minimum — consider warming the room."
            elif temp > float(tmax):
                reply += " It's **above** the maximum — consider cooling the room."
            else:
                reply += " This is within the normal range."
    elif any(w in msg for w in ["humid", "moist"]):
        reply = f"The current humidity is {hum}%. Safe range is {hmin}–{hmax}%."
        if hum != "--":
            if hum < float(hmin):
                reply += " It's **below** the minimum — consider using a humidifier."
            elif hum > float(hmax):
                reply += " It's **above** the maximum — consider using a dehumidifier."
            else:
                reply += " This is within the normal range."
    elif any(w in msg for w in ["motion", "move", "moving", "activity", "active"]):
        reply = f"Baby is currently **{motion_str}**."
        reply += " Recent motion was detected." if d.get("motion") else " No recent motion — baby may be sleeping."
    elif any(w in msg for w in ["diaper", "wet", "wee", "nappy", "change"]):
        reply = f"Diaper is **{diaper_str}**."
        reply += " It's time for a change!" if d.get("wetness") else " All good, no change needed."
    elif any(w in msg for w in ["hi", "hello", "hey", "help"]):
        reply = "Hello! I'm your Baby Monitor assistant. Ask about **temperature**, **humidity**, **motion**, or **diaper**."
    elif any(w in msg for w in ["status", "summary", "all", "overview"]):
        flags = []
        if d.get("motion"):
            flags.append("motion detected")
        if d.get("wetness"):
            flags.append("wet diaper")
        if check_abnormal(temp if temp != "--" else None, hum if hum != "--" else None):
            flags.append("⚠️ abnormal readings")
        reply = f"**Temperature:** {temp}°C  |  **Humidity:** {hum}%  |  **Motion:** {motion_str}  |  **Diaper:** {diaper_str}"
        if flags:
            reply += f"\n\nNotable: {' · '.join(flags)}"
    else:
        reply = ("I can answer about: **temperature**, **humidity**, **motion**, **diaper**, "
                 "or say **status** for a full summary. Type **help** for options.")

    return jsonify({"reply": reply})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("RENDER") is None
    log.info("Baby Monitor Server starting on port %s...", port)
    socketio.run(app, host="0.0.0.0", port=port, debug=debug,
                 allow_unsafe_werkzeug=True)
