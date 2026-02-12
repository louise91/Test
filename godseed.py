# godseed.py
import os
import json
import time
import logging
import threading
import requests

from datetime import datetime
from flask import (
    Blueprint, request, session,
    render_template, Response,
    jsonify, g
)

# =========================================================
# 基本設定
# =========================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("Godseed")

godseed_bp = Blueprint("godseed", __name__)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

VISITS_FILE = os.path.join(DATA_DIR, "visits.json")

# =========================================================
# 訪問紀錄
# =========================================================

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def log_visit():
    visits = load_json(VISITS_FILE, [])
    visits.append({
        "time": datetime.now().isoformat(),
        "ip": request.remote_addr or "未知",
        "path": request.path
    })
    save_json(VISITS_FILE, visits)

# =========================================================
# 限流
# =========================================================

RATE_LIMIT = 8
user_requests = {}

def reset_rate_limit():
    while True:
        time.sleep(3600)
        user_requests.clear()
        log.info("【限流重置】")

def check_rate_limit(ip):
    hour_key = f"{ip}_{int(time.time()//3600)}"
    if hour_key not in user_requests:
        user_requests[hour_key] = 0
    if user_requests[hour_key] >= RATE_LIMIT:
        return False
    user_requests[hour_key] += 1
    return True

# =========================================================
# Engine
# =========================================================

class GodseedEngine:
    def __init__(self):
        self.model = "llama-3.3-70b-versatile"
        self.url = "https://api.groq.com/openai/v1/chat/completions"
        self.api_key = os.environ.get("GROQ_API_KEY")

    def generate(self, seed):

        messages = [
            {"role": "system", "content": f"崩壞種子：{seed}"},
            {"role": "user", "content": "開始"}
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": 2048
        }

        r = requests.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            stream=True,
            timeout=120
        )

        for line in r.iter_lines():
            if line:
                yield line.decode("utf-8") + "\n\n"

godseed_engine = GodseedEngine()

# =========================================================
# Routes
# =========================================================

@godseed_bp.route("/godseed")
def godseed_page():
    return render_template("godseed.html")

@godseed_bp.route("/godseed/start", methods=["POST"])
def start():
    data = request.get_json(silent=True) or {}
    seed = data.get("seed", "無")
    ip = request.remote_addr

    if not check_rate_limit(ip):
        return jsonify({"error": "本小時已達上限"}), 429

    session["seed"] = seed
    session["allowed"] = True
    return {"status": "ok"}

@godseed_bp.route("/godseed/stream")
def stream():

    if not session.get("allowed"):
        return Response(
            "data: 未授權\n\n",
            mimetype="text/event-stream"
        )

    seed = session.get("seed", "無")

    def generate():
        for chunk in godseed_engine.generate(seed):
            yield chunk

    return Response(generate(), mimetype="text/event-stream")

@godseed_bp.route("/test_visit")
def test_visit():
    return "ok"

# =========================================================
# 初始化函數（關鍵）
# =========================================================

def init_godseed(app):

    app.register_blueprint(godseed_bp)

    @app.before_request
    def before():
        g.start_time = time.time()
        log_visit()

    @app.after_request
    def after(response):
        if hasattr(g, "start_time"):
            duration = time.time() - g.start_time
            log.debug(f"耗時: {duration:.3f}s")
        return response

    threading.Thread(
        target=reset_rate_limit,
        daemon=True
    ).start()

    log.info("Godseed 插件初始化完成")