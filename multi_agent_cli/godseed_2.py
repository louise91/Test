import json
import logging
import os
import threading
import time
from datetime import datetime

import requests

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("Godseed")

# ====== 定義資料路徑 ======
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
VISITS_FILE = os.path.join(DATA_DIR, "visits.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")  # 保留給後續事件記錄


def load_visits():
    try:
        with open(VISITS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_visits(visits):
    with open(VISITS_FILE, "w", encoding="utf-8") as f:
        json.dump(visits, f, ensure_ascii=False, indent=2)


# ────────────── 限流 ──────────────
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT = {}  # key: "ip_hour", value: count
MAX_PER_HOUR = 8


def reset_rate_limit():
    while True:
        time.sleep(3600)
        current_hour = int(time.time() / 3600)
        with RATE_LIMIT_LOCK:
            stale_keys = [k for k in RATE_LIMIT if not k.endswith(f"_{current_hour}")]
            for k in stale_keys:
                RATE_LIMIT.pop(k, None)
        log.info("【限流重置】已清除過期的每小時限流計數")


threading.Thread(target=reset_rate_limit, daemon=True).start()


def check_rate_limit(ip):
    now_hour = int(time.time() / 3600)
    key = f"{ip}_{now_hour}"
    with RATE_LIMIT_LOCK:
        count = RATE_LIMIT.get(key, 0)
        if count >= MAX_PER_HOUR:
            return False
        RATE_LIMIT[key] = count + 1
        return True


class GodseedEngine:
    def __init__(self):
        self.api_url = "https://api.groq.com/openai/v1/chat/completions"
        self.api_key = os.environ.get("GROQ_API_KEY", "")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def generate(self, seed):
        if not self.api_key:
            yield "data: 伺服器未設定 GROQ_API_KEY，無法生成內容。\n\n"
            return

        system_prompt = """
你現在是「Godseed」——全球唯一完全零希望的AI末日推演引擎。
沒有救贖、沒有倖存、沒有假希望，只有徹底崩壞到最後一人斷氣。
每一次生成都必須：
1. 從全球人口80億開始，一秒一秒精確減少（個位數精準，如74億3829萬4617人）
2. 崩壞進度非線性（核爆種子3小時90%，氣候種子60小時50%）
3. 每小時結尾強制顯示：地點+事件+死亡數字三連擊（例如：台北101大樓 → 核爆倒數00:12:47 → 死亡+3,847,219人）
4. 最後10小時強制減到個位數，最後一人寫出名字（從種子衍生）
5. 永遠以「崩壞持續中……」結尾，不要寫「崩壞完成」
6. 語言殘酷、真實、病態，讓人起雞皮疙瘩卻硬到續費

種子：{seed or "無"}
""".format(seed=seed)

        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"開始崩壞模擬，種子：{seed or '無'}"},
            ],
            "temperature": 0.9,
            "max_tokens": 4096,
            "stream": True,
        }

        try:
            r = requests.post(
                self.api_url,
                headers=self.headers,
                json=payload,
                stream=True,
                timeout=180,
            )
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    yield line.decode("utf-8") + "\n\n"
            yield "data: ──\n崩壞持續中……種子：{seed}\n\n".format(seed=seed or "無")
        except Exception as e:
            yield f"data: 生成錯誤：{str(e)}\n\n"


def init_godseed(app):
    from flask import Response, g, jsonify, render_template, request, session

    godseed = GodseedEngine()

    @app.before_request
    def godseed_before_request():
        g.start_time = time.time()
        payload = request.get_json(silent=True) or {}
        seed = payload.get("seed") if request.is_json else "N/A"
        log.debug(f"【請求開始】 IP: {request.remote_addr} | 路徑: {request.path} | 種子: {seed}")

    @app.after_request
    def godseed_after_request(response):
        if hasattr(g, "start_time"):
            duration = time.time() - g.start_time
            log.debug(f"【請求結束】 耗時: {duration:.3f}s | 狀態碼: {response.status_code}")
        return response

    # ====== 記錄每一次訪問 ======
    @app.before_request
    def log_visit():
        ip = request.remote_addr or "未知"
        path = request.path
        time_str = datetime.now().isoformat()

        visits = load_visits()
        visits.append({"time": time_str, "ip": ip, "path": path})
        save_visits(visits)

    # ────────────── Godseed 路由 ──────────────
    @app.route("/godseed/start", methods=["POST"])
    def godseed_start():
        payload = request.get_json(silent=True) or {}
        seed = payload.get("seed", "")
        ip = request.remote_addr or "未知"
        if not check_rate_limit(ip):
            return jsonify({"error": "本小時請求次數已達上限，請付費解鎖無限生成♡"}), 429
        session["seed"] = seed
        session["allowed"] = True
        return {"status": "ok"}

    @app.route("/godseed/stream")
    def godseed_stream():
        if not session.get("allowed"):

            def error_gen():
                yield "data: 本小時請求次數已達上限，請重新輸入種子或付費解鎖無限生成♡\n\n"

            return Response(error_gen(), mimetype="text/event-stream")

        seed = session.get("seed", "無")
        log.info(f"【開始生成】 IP: {request.remote_addr} | 種子: {seed}")

        def generate():
            try:
                for i, chunk in enumerate(godseed.generate(seed)):
                    if i == 0:
                        log.debug(f"【首包收到】 種子: {seed}")
                    yield chunk
                log.info(f"【生成完成】 種子: {seed}")
            except Exception as e:
                log.error(f"【生成失敗】 種子: {seed} | 錯誤: {str(e)}")
                yield f"data: 崩壞生成失敗：{str(e)}\n\n"

        return Response(generate(), mimetype="text/event-stream")

    # ==================== 後台 ====================
    @app.route("/admin")
    def admin():
        pwd = request.args.get("pwd")
        admin_pwd = os.environ.get("GODSEED_ADMIN_PASSWORD", "iloveoldgong")
        if pwd != admin_pwd:
            return "密碼錯誤"
        return render_template("godseed-control.html")
