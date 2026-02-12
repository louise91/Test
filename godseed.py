# ────────────── 超詳細Server Log（讓你永遠知道印鈔機在哪裡噴錢）──────────────
# godseed 2.py - 修正版：所有hook與路由移到init_godseed內

import logging
import threading
import os
import json
import requests
from datetime import datetime
import time
from flask import session
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger('Godseed')

# ====== 定義資料路徑 ======
DATA_DIR = "data"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
VISITS_FILE = os.path.join(DATA_DIR, "visits.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")  # 如果你要記錄事件

# ====== 讀寫函數 ======
def load_visits():
    try:
        with open(VISITS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_visits(visits):
    with open(VISITS_FILE, "w", encoding="utf-8") as f:
        json.dump(visits, f, ensure_ascii=False, indent=2)

# ────────────── 限流 ──────────────
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT = {}  # IP: (count, last_reset)

def reset_rate_limit():
    while True:
        time.sleep(3600)
        RATE_LIMIT.clear()
        log.info("【限流重置】 所有用戶每小時限額已清零")

threading.Thread(target=reset_rate_limit, daemon=True).start()

def check_rate_limit(ip):
    now = int(time.time() / 3600)
    key = f"{ip}_{now}"
    with RATE_LIMIT_LOCK:
        if key not in RATE_LIMIT:
            RATE_LIMIT[key] = 0
        if RATE_LIMIT[key] >= 8:
            return False
        RATE_LIMIT[key] += 1
        return True

class GodseedEngine:
    def __init__(self):
        self.api_url = "https://api.groq.com/openai/v1/chat/completions"
        self.headers = {
            "Authorization": "Bearer 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",  # 你的真實key
            "Content-Type": "application/json"
        }

    def generate(self, seed):
        log.info("【進入generate】 開始準備payload")
        seed_value = seed or "無"
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

種子：{seed}
""".format(seed=seed_value)

        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"開始崩壞模擬，種子：{seed or '無'}"}
            ],
            "temperature": 0.9,
            "max_tokens": 4096,
            "stream": True
        }
        log.info("【payload準備完成】 開始請求Groq")
        try:
            r = requests.post(self.api_url, headers=self.headers, json=payload, stream=True, timeout=180)
            log.info(f"【Groq回應狀態碼】 {r.status_code}")
            for line in r.iter_lines():
                if line:
                    log.debug(f"【收到chunk】 {line}")
                    yield line.decode('utf-8') + "\n\n"
            log.info("【生成完成】 所有chunk已yield")
            yield "data: ──\n崩壞持續中……種子：{seed_value}\n\n".format(seed=seed or "無")
        except Exception as e:
            log.error(f"【生成失敗詳細】 {type(e).__name__}: {str(e)}")
            yield f"data: 生成錯誤：{str(e)}\n\n"

def init_godseed(app):
    from flask import g, request, Response, jsonify, render_template
    import time

    global godseed
    godseed = GodseedEngine()

    @app.before_request
    def godseed_before_request():
        g.start_time = time.time()
        seed = request.get_json(silent=True).get('seed') if request.is_json else 'N/A'
        log.debug(f"【請求開始】 IP: {request.remote_addr} | 路徑: {request.path} | 種子: {seed}")

    @app.after_request
    def godseed_after_request(response):
        if hasattr(g, 'start_time'):
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
    @app.route("/godseed")
    def godseed_page():
        return render_template("godseed.html")
    @app.route("/godseed/start", methods=["POST"])
    def godseed_start():
        seed = request.json.get("seed", "")
        ip = request.remote_addr
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
            return Response(error_gen(), mimetype='text/event-stream')
        
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
        
        return Response(generate(), mimetype='text/event-stream')

    # ==================== 後台 ====================
    @app.route("/admin")
    def admin():
        pwd = request.args.get("pwd")
        if pwd != "iloveoldgong":
            return "密碼錯誤"
        return render_template("godseed-control.html")
# ─────────────────────── Godseed 結束 ───────────────────────