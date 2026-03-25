# ai_utils.py — 共用工具函數，不含路由
import os, json, time, base64, requests, math, re, threading

__all__ = [
    # 底線開頭（import * 預設不匯出）
    '_get_key', '_refresh_keys',
    '_get_messages', '_save_messages',
    '_load_world', '_save_world',
    '_custom_models_path',
    '_ENGINE_OK', '_ENGINE_ERR',
    # engine_manager 函數（_ENGINE_OK=True 時才有值）
    'stream_ai', 'get_status', 'set_engine', 'get_engine',
    'get_models_by_engine', 'ping_engines',
    # 一般名稱
    'load_roles', 'save_roles',
    'load_rag_index', 'save_rag_index', 'chunk_text', 'rag_search',
    'ai_rate_limited', 'web_search',
    'all_models', 'load_custom_models', 'save_custom_models',
    'stream_groq', 'call_groq_once', 'build_user_message',
    'get_ai_messages',
    'RAG_DIR', 'AVAILABLE_MODELS',
    'GROQ_API_KEY', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY',
    'BRAVE_API_KEY', 'OLLAMA_URL', 'COLAB_API_URL',
    'GROQ_API_URL', 'GROQ_MODEL_TEXT', 'GROQ_MODEL_VISION',
    'MAX_FILE_BYTES', 'ALLOWED_IMAGE',
    'DATA_DIR', 'RAG_INDEX', 'ROLES_FILE',
    'DEFAULT_ROLES', 'tokenize', 'tfidf_search',
]




# ── 對話記憶（SQLite，重啟不清空）──
def _get_messages(sid, system_prompt):
    """從 SQLite 讀取對話歷史，重組成 messages 格式"""
    try:
        from database import get_chat_history
        rows = get_chat_history(sid, limit=20)
        rows = list(reversed(rows))  # 舊到新
        msgs = [{"role":"system","content":system_prompt}]
        for row in rows:
            msgs.append({"role": row["role"], "content": row["content"]})
        return msgs
    except Exception:
        return [{"role":"system","content":system_prompt}]

def _save_messages(sid, msgs):
    """存最新 assistant 訊息，並自動更新 session 標題"""
    try:
        from database import save_chat_message, create_session, update_session_title
        last = msgs[-1] if msgs else None
        if last and last["role"] == "assistant":
            save_chat_message(sid, "assistant", last["content"])
            # 用第一條 user 訊息當標題（最多20字）
            user_msgs = [m for m in msgs if m["role"]=="user"]
            if user_msgs:
                title = user_msgs[0]["content"][:20].strip()
                if len(user_msgs[0]["content"]) > 20:
                    title += "…"
                update_session_title(sid, title)
    except Exception as e:
        import traceback; traceback.print_exc()

def _load_world():
    try:
        from world_manager import load_world
        return load_world()
    except Exception:
        return {}

def _save_world(w):
    try:
        from world_manager import save_world
        save_world(w)
    except Exception as e:
        import traceback; traceback.print_exc()
from collections import Counter
from flask import request, Response, render_template, session, jsonify
_ENGINE_OK = False
_ENGINE_ERR = ""
try:
    import engine_manager as _em
    stream_ai             = getattr(_em, "stream_ai")
    get_status            = getattr(_em, "get_status")
    set_engine            = getattr(_em, "set_engine")
    get_engine            = getattr(_em, "get_engine")
    get_models_by_engine  = getattr(_em, "get_models_by_engine")
    ping_engines          = getattr(_em, "ping_engines")
    _ENGINE_OK = True
except Exception as _e:
    import traceback as _tb; _tb.print_exc()
    _ENGINE_ERR = f"{type(_e).__name__}: {_e}"
    _ENGINE_OK  = False

# ====== 設定（從 config.py 讀取）======
def _get_key(key, default=""):
    """優先從 DB settings 讀取，fallback 到 config.py"""
    try:
        from database import get_setting
        val = get_setting(key)
        if val: return val
    except Exception as e:
        import traceback; traceback.print_exc()
    try:
        import config as _cfg
        return getattr(_cfg, key, default) or default
    except Exception:
        return default

GROQ_API_KEY       = ""  # 動態讀取，見 _get_key()
OPENROUTER_API_KEY = ""
ANTHROPIC_API_KEY  = ""
BRAVE_API_KEY      = ""
OLLAMA_URL         = "http://localhost:11434"
COLAB_API_URL      = ""

def _refresh_keys():
    """重新從 DB/config 讀取所有 key"""
    global GROQ_API_KEY, OPENROUTER_API_KEY, ANTHROPIC_API_KEY
    global BRAVE_API_KEY, OLLAMA_URL, COLAB_API_URL
    GROQ_API_KEY       = _get_key("GROQ_API_KEY")
    OPENROUTER_API_KEY = _get_key("OPENROUTER_API_KEY")
    ANTHROPIC_API_KEY  = _get_key("ANTHROPIC_API_KEY")
    BRAVE_API_KEY      = _get_key("BRAVE_API_KEY")
    OLLAMA_URL         = _get_key("OLLAMA_URL", "http://localhost:11434")
    COLAB_API_URL      = _get_key("COLAB_API_URL")

_refresh_keys()  # 啟動時載入一次
GROQ_API_URL      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL_TEXT   = "llama-3.3-70b-versatile"
GROQ_MODEL_VISION = "meta-llama/llama-4-scout-17b-16e-instruct"

# 預設模型清單（內建，不可刪）
AVAILABLE_MODELS = [
    {"id": "llama-3.3-70b-versatile",              "name": "Llama 3.3 70B",       "tag": "預設",   "vision": False, "builtin": True},
    {"id": "llama-3.1-8b-instant",                 "name": "Llama 3.1 8B",        "tag": "快速",   "vision": False, "builtin": True},
    {"id": "gemma2-9b-it",                         "name": "Gemma 2 9B",          "tag": "輕量",   "vision": False, "builtin": True},
    {"id": "mixtral-8x7b-32768",                   "name": "Mixtral 8x7B",        "tag": "長文",   "vision": False, "builtin": True},
    {"id": "meta-llama/llama-4-scout-17b-16e-instruct", "name": "Llama 4 Scout", "tag": "視覺",   "vision": True,  "builtin": True},
]

# ── 自訂模型讀寫 ──
def _custom_models_path():
    base = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(base, "data"), exist_ok=True)
    return os.path.join(base, "data", "custom_models.json")

def load_custom_models() -> list:
    try:
        with open(_custom_models_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_custom_models(models: list):
    with open(_custom_models_path(), "w", encoding="utf-8") as f:
        json.dump(models, f, ensure_ascii=False, indent=2)

def all_models() -> list:
    """內建 + 自訂，合併回傳"""
    custom = load_custom_models()
    existing_ids = {m["id"] for m in AVAILABLE_MODELS}
    extra = [m for m in custom if m["id"] not in existing_ids]
    return AVAILABLE_MODELS + extra
MAX_FILE_BYTES    = 10 * 1024 * 1024
ALLOWED_IMAGE     = {"image/jpeg","image/png","image/gif","image/webp"}

BASE      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE, "data")
RAG_DIR   = os.path.join(DATA_DIR, "rag_docs")
RAG_INDEX = os.path.join(DATA_DIR, "rag_index.json")
ROLES_FILE= os.path.join(DATA_DIR, "ai_roles.json")

os.makedirs(RAG_DIR, exist_ok=True)

DEFAULT_ROLES = [
    {"id":"default",    "name":"通用助手",  "prompt":"你是一個直接、有用的 AI 助手，用繁體中文回覆，不廢話。"},
    {"id":"translator", "name":"翻譯專家",  "prompt":"你是專業翻譯，只輸出翻譯結果，不解釋。"},
    {"id":"coder",      "name":"程式專家",  "prompt":"你是資深程式師，擅長 Python/Flask，回答要有程式碼範例並指出潛在 bug。"},
    {"id":"critic",     "name":"批判分析師","prompt":"你擅長找出論點的漏洞和盲點，用繁體中文給出批判性分析，不留情面。"},
]

# ═══════════════════════════════════════
# 角色管理
# ═══════════════════════════════════════
def load_roles():
    try:
        with open(ROLES_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_ROLES

def save_roles(roles):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ROLES_FILE,"w",encoding="utf-8") as f:
        json.dump(roles, f, ensure_ascii=False, indent=2)

# ═══════════════════════════════════════
# RAG：文件永久儲存 + TF-IDF 向量搜尋
# ═══════════════════════════════════════
def load_rag_index():
    try:
        with open(RAG_INDEX,"r",encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_rag_index(index):
    with open(RAG_INDEX,"w",encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

def chunk_text(text, size=400, overlap=80):
    """把長文切成有重疊的小塊"""
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i+size])
        i += size - overlap
    return chunks

def tokenize(text):
    """簡單斷詞（支援中英文）"""
    return re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', text.lower())

def tfidf_search(query, chunks, top_k=4):
    """純 Python TF-IDF，找最相關的 chunks"""
    if not chunks:
        return []
    
    query_tokens = Counter(tokenize(query))
    
    # 計算每個 chunk 的 TF-IDF 相似度
    scores = []
    all_tokens = [Counter(tokenize(c)) for c in chunks]
    
    # IDF：計算每個詞出現在幾個 chunk
    doc_freq = Counter()
    for token_count in all_tokens:
        for token in token_count:
            doc_freq[token] += 1
    
    N = len(chunks)
    
    for i, (chunk, token_count) in enumerate(zip(chunks, all_tokens)):
        score = 0.0
        total = sum(token_count.values()) or 1
        for token, qcount in query_tokens.items():
            tf  = token_count.get(token, 0) / total
            idf = math.log((N + 1) / (doc_freq.get(token, 0) + 1)) + 1
            score += tf * idf * qcount
        scores.append((score, i, chunk))
    
    scores.sort(reverse=True)
    return [chunk for score, idx, chunk in scores[:top_k] if score > 0]

def rag_search(query, doc_ids=None):
    """從指定文件（或所有文件）搜尋相關段落"""
    index = load_rag_index()
    if doc_ids:
        docs = [d for d in index if d["id"] in doc_ids]
    else:
        docs = index
    
    all_chunks = []
    for doc in docs:
        path = os.path.join(RAG_DIR, doc["filename"])
        try:
            with open(path,"r",encoding="utf-8") as f:
                text = f.read()
            chunks = chunk_text(text)
            all_chunks.extend([(c, doc["name"]) for c in chunks])
        except Exception:
            continue
    
    if not all_chunks:
        return ""
    
    chunks_only = [c for c,_ in all_chunks]
    top = tfidf_search(query, chunks_only)
    
    if not top:
        return ""
    
    return "以下是從文件搜尋到的相關內容：\n\n" + "\n\n---\n".join(top)

# ═══════════════════════════════════════
# Rate Limit
# ═══════════════════════════════════════
_ai_rate: dict = {}
def ai_rate_limited(ip, seconds=60, limit=30):
    now = time.time()
    _ai_rate[ip] = [t for t in _ai_rate.get(ip,[]) if now-t < seconds]
    if len(_ai_rate[ip]) >= limit:
        return True
    _ai_rate[ip].append(now)
    return False

# ═══════════════════════════════════════
# 網路搜尋
# ═══════════════════════════════════════
def web_search(query, max_results=5):
    try:
        if BRAVE_API_KEY:
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept":"application/json","X-Subscription-Token":BRAVE_API_KEY},
                params={"q":query,"count":max_results}, timeout=10
            )
            r.raise_for_status()
            results = r.json().get("web",{}).get("results",[])
            lines = [f"• {i.get('title','')}\n  {i.get('description','')}\n  {i.get('url','')}"
                     for i in results[:max_results]]
            return "\n\n".join(lines) if lines else "無搜尋結果"
        else:
            r = requests.get(
                "https://api.duckduckgo.com/",
                params={"q":query,"format":"json","no_html":1,"skip_disambig":1},
                headers={"User-Agent":"Mozilla/5.0"}, timeout=10
            )
            data = r.json()
            lines = []
            if data.get("Abstract"):
                lines.append(f"摘要：{data['Abstract']}\n{data.get('AbstractURL','')}")
            for topic in data.get("RelatedTopics",[])[:max_results]:
                if isinstance(topic,dict) and topic.get("Text"):
                    lines.append(f"• {topic['Text']}\n  {topic.get('FirstURL','')}")
            return "\n\n".join(lines) if lines else "無即時結果。"
    except Exception as e:
        return f"搜尋失敗：{e}"

# ═══════════════════════════════════════
# Groq 單次呼叫（非串流，給多代理用）
# ═══════════════════════════════════════
def call_groq_once(messages, use_vision=False, model_id=None):
    if model_id:
        model = model_id
    else:
        model = GROQ_MODEL_VISION if use_vision else GROQ_MODEL_TEXT
    r = requests.post(
        GROQ_API_URL,
        headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
        json={"model":model,"messages":messages,"max_tokens":1024,"temperature":0.7},
        timeout=60
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# ═══════════════════════════════════════
# Groq 串流（單代理用）
# ═══════════════════════════════════════
def stream_groq(messages, use_vision=False, model_id=None):
    if model_id:
        model = model_id
    else:
        model = GROQ_MODEL_VISION if use_vision else GROQ_MODEL_TEXT
    r = requests.post(
        GROQ_API_URL,
        headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
        json={"model":model,"messages":messages,"stream":True,"max_tokens":2048},
        stream=True, timeout=90
    )
    if not r.ok:
        try:
            err = r.json()
            msg = err.get("error", {}).get("message", r.text)
        except Exception:
            msg = r.text
        if r.status_code == 404:
            raise ValueError(f"❌ 模型不存在：{model}\n請換一個模型，或確認 Model ID 是否正確。")
        elif r.status_code == 400:
            raise ValueError(f"❌ 模型不支援此請求：{model}\n原因：{msg[:200]}")
        elif r.status_code == 429:
            raise ValueError(f"⏳ 超過速率限制，請稍後再試。\n{msg[:100]}")
        else:
            raise ValueError(f"❌ API 錯誤 {r.status_code}：{msg[:200]}")
    buffer = ""
    for line in r.iter_lines(decode_unicode=False):
        if not line:
            continue
        try:
            line_str = line.decode("utf-8")
        except Exception:
            continue
        if line_str.startswith("data: "):
            data_str = line_str[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                content = chunk["choices"][0].get("delta",{}).get("content","")
                if content:
                    buffer += content
                    yield content, buffer
            except Exception:
                continue

# ═══════════════════════════════════════
# 建構訊息
# ═══════════════════════════════════════
def build_user_message(text, file_data=None):
    if file_data is None:
        return {"role":"user","content":text}
    if file_data["type"] == "image":
        return {
            "role":"user",
            "content":[
                {"type":"text","text":text or "請描述這張圖片"},
                {"type":"image_url","image_url":{"url":f"data:{file_data['mime']};base64,{file_data['b64']}"}}
            ]
        }
    else:
        try:
            content = base64.b64decode(file_data["b64"]).decode("utf-8",errors="replace")[:6000]
        except Exception:
            content = "[無法讀取]"
        return {"role":"user","content":f"{text}\n\n---附件：{file_data['name']}---\n{content}"}

def get_ai_messages(sid, system_prompt):
    """從 SQLite 讀取歷史訊息，sid 由 route 傳入"""
    return _get_messages(sid or "default", system_prompt)
# ═══════════════════════════════════════
# 路由
# ═══════════════════════════════════════