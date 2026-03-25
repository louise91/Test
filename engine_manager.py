"""
engine_manager.py — 統一 AI 引擎管理層
支援：Groq / OpenRouter / Ollama / Anthropic / Colab
自動 fallback：Ollama → Colab → OpenRouter → Groq → 排隊
"""
import os, json, time, requests, threading
from typing import Generator, Optional

# ══════════════════════════════════════════════════════════════
# Config 讀取
# ══════════════════════════════════════════════════════════════
def _cfg(key, default=""):
    # 優先從 DB 讀（與 ai_utils._get_key 邏輯一致）
    try:
        from database import get_setting
        val = get_setting(key)
        if val: return val
    except Exception:
        pass
    try:
        from config import __dict__ as cfg
        return cfg.get(key, os.environ.get(key, default))
    except Exception:
        return os.environ.get(key, default)

# ══════════════════════════════════════════════════════════════
# 引擎狀態追蹤
# ══════════════════════════════════════════════════════════════
class EngineStatus:
    def __init__(self):
        self._lock  = threading.Lock()
        self._data  = {}   # engine_id → {ok, last_check, error, latency_ms}

    def set(self, engine_id, ok, error="", latency_ms=0):
        with self._lock:
            self._data[engine_id] = {
                "ok": ok, "error": error,
                "latency_ms": latency_ms,
                "last_check": time.time(),
            }

    def get(self, engine_id):
        with self._lock:
            return self._data.get(engine_id, {"ok": None, "error": "", "latency_ms": 0, "last_check": 0})

    def all(self):
        with self._lock:
            return dict(self._data)

_status = EngineStatus()

# ══════════════════════════════════════════════════════════════
# 引擎基底類別
# ══════════════════════════════════════════════════════════════
class BaseEngine:
    engine_id   = "base"
    name        = "Base"
    requires_key = True

    def available(self) -> bool:
        raise NotImplementedError

    def ping(self) -> bool:
        """快速健康檢查，更新 _status"""
        t0 = time.time()
        try:
            ok = self._ping()
            _status.set(self.engine_id, ok, latency_ms=int((time.time()-t0)*1000))
            return ok
        except Exception as e:
            _status.set(self.engine_id, False, error=str(e)[:120])
            return False

    def _ping(self) -> bool:
        return self.available()

    def stream(self, messages, model=None, **kw) -> Generator:
        """
        產生 (token, full_text) 的 generator
        子類別實作 _stream()
        """
        yield from self._stream(messages, model=model, **kw)

    def _stream(self, messages, model=None, **kw) -> Generator:
        raise NotImplementedError

    def list_models(self) -> list:
        return []

# ══════════════════════════════════════════════════════════════
# Groq 引擎
# ══════════════════════════════════════════════════════════════
class GroqEngine(BaseEngine):
    engine_id = "groq"
    name      = "Groq"
    API_URL   = "https://api.groq.com/openai/v1"

    DEFAULT_MODELS = [
        {"id":"llama-3.3-70b-versatile",                  "name":"Llama 3.3 70B",   "tag":"預設",  "vision":False},
        {"id":"llama-3.1-8b-instant",                     "name":"Llama 3.1 8B",    "tag":"快速",  "vision":False},
        {"id":"gemma2-9b-it",                              "name":"Gemma 2 9B",      "tag":"輕量",  "vision":False},
        {"id":"mixtral-8x7b-32768",                        "name":"Mixtral 8x7B",    "tag":"長文",  "vision":False},
        {"id":"meta-llama/llama-4-scout-17b-16e-instruct","name":"Llama 4 Scout",   "tag":"視覺",  "vision":True},
        {"id":"meta-llama/llama-4-maverick-17b-128e-instruct","name":"Llama 4 Maverick","tag":"進階","vision":True},
    ]

    def _key(self):
        return (_cfg("GROQ_API_KEY") or "").strip()

    def available(self):
        return bool(self._key())

    def _ping(self):
        r = requests.get(f"{self.API_URL}/models",
                         headers={"Authorization": f"Bearer {self._key()}"}, timeout=5)
        return r.status_code == 200

    def list_models(self):
        return self.DEFAULT_MODELS

    def _stream(self, messages, model=None, vision=False, **kw):
        model = model or ("meta-llama/llama-4-scout-17b-16e-instruct" if vision else "llama-3.3-70b-versatile")
        r = requests.post(
            f"{self.API_URL}/chat/completions",
            headers={"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "stream": True, "max_tokens": 2048},
            stream=True, timeout=90,
        )
        yield from _parse_openai_stream(r, engine=self.engine_id, model=model)

# ══════════════════════════════════════════════════════════════
# OpenRouter 引擎
# ══════════════════════════════════════════════════════════════
class OpenRouterEngine(BaseEngine):
    engine_id = "openrouter"
    name      = "OpenRouter"
    API_URL   = "https://openrouter.ai/api/v1"

    # 精選免費模型（:free 後綴）
    FREE_MODELS = [
        {"id":"meta-llama/llama-3.3-70b-instruct:free",       "name":"Llama 3.3 70B",     "tag":"免費","vision":False},
        {"id":"meta-llama/llama-3.1-8b-instruct:free",        "name":"Llama 3.1 8B",      "tag":"免費","vision":False},
        {"id":"google/gemma-3-27b-it:free",                    "name":"Gemma 3 27B",       "tag":"免費","vision":False},
        {"id":"microsoft/phi-4:free",                          "name":"Phi-4",             "tag":"免費","vision":False},
        {"id":"mistralai/mistral-7b-instruct:free",            "name":"Mistral 7B",        "tag":"免費","vision":False},
        {"id":"qwen/qwen-2.5-72b-instruct:free",               "name":"Qwen 2.5 72B",      "tag":"免費","vision":False},
        {"id":"qwen/qwen2.5-vl-72b-instruct:free",             "name":"Qwen 2.5 VL 72B",   "tag":"免費視覺","vision":True},
        {"id":"deepseek/deepseek-r1:free",                     "name":"DeepSeek R1",       "tag":"推理","vision":False},
        {"id":"deepseek/deepseek-chat-v3-0324:free",           "name":"DeepSeek V3",       "tag":"免費","vision":False},
        {"id":"google/gemini-2.0-flash-exp:free",              "name":"Gemini 2.0 Flash",  "tag":"免費","vision":True},
        {"id":"anthropic/claude-3.5-haiku",                    "name":"Claude 3.5 Haiku",  "tag":"付費","vision":True},
        {"id":"openai/gpt-4o-mini",                            "name":"GPT-4o Mini",       "tag":"付費","vision":True},
        {"id":"cohere/command-r-plus-08-2024",                 "name":"Command R+",        "tag":"付費","vision":False},
    ]

    def _key(self):
        return (_cfg("OPENROUTER_API_KEY") or "").strip()

    def available(self):
        return bool(self._key())

    def _ping(self):
        r = requests.get(f"{self.API_URL}/models",
                         headers={"Authorization": f"Bearer {self._key()}"}, timeout=8)
        return r.status_code == 200

    def list_models(self):
        # 嘗試從 API 取最新免費模型清單，失敗就用內建清單
        try:
            r = requests.get(f"{self.API_URL}/models",
                             headers={"Authorization": f"Bearer {self._key()}"}, timeout=8)
            if r.ok:
                all_models = r.json().get("data", [])
                free = [m for m in all_models if ":free" in m.get("id","")]
                if free:
                    return [{"id":m["id"],"name":m.get("name",m["id"]),"tag":"免費",
                             "vision":"vision" in str(m.get("description","")).lower()} for m in free[:30]]
        except Exception:
            pass
        return self.FREE_MODELS

    def _stream(self, messages, model=None, **kw):
        model = model or "meta-llama/llama-3.3-70b-instruct:free"
        key = self._key()
        import sys
        print(f"[OpenRouter] key_len={len(key)} prefix={key[:8]}... model={model}", file=sys.stderr)
        if not key:
            raise ValueError("❌ OPENROUTER_API_KEY 未設定，請到設定頁面填入")
        r = requests.post(
            f"{self.API_URL}/chat/completions",
            headers={
                "Authorization":  f"Bearer {self._key()}",
                "Content-Type":   "application/json",
                "HTTP-Referer":   "https://pythonanywhere.com",
                "X-Title":        "AI Chatroom",
            },
            json={"model": model, "messages": messages, "stream": True, "max_tokens": 2048},
            stream=True, timeout=90,
        )
        yield from _parse_openai_stream(r, engine=self.engine_id, model=model)

# ══════════════════════════════════════════════════════════════
# Ollama 引擎（本機）
# ══════════════════════════════════════════════════════════════
class OllamaEngine(BaseEngine):
    engine_id    = "ollama"
    name         = "Ollama（本機）"
    requires_key = False

    def _base_url(self):
        return _cfg("OLLAMA_URL", "http://localhost:11434")

    def available(self):
        try:
            r = requests.get(f"{self._base_url()}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def list_models(self):
        try:
            r = requests.get(f"{self._base_url()}/api/tags", timeout=5)
            if r.ok:
                return [{"id": m["name"], "name": m["name"], "tag": "本機", "vision": False}
                        for m in r.json().get("models", [])]
        except Exception:
            pass
        return []

    def _stream(self, messages, model=None, **kw):
        models = self.list_models()
        model  = model or (models[0]["id"] if models else "llama3.2")
        r = requests.post(
            f"{self._base_url()}/api/chat",
            json={"model": model, "messages": messages, "stream": True},
            stream=True, timeout=120,
        )
        if not r.ok:
            raise ValueError(f"❌ Ollama 錯誤 {r.status_code}：{r.text[:200]}")
        full = ""
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                if token:
                    full += token
                    yield token, full
                if chunk.get("done"):
                    break
            except Exception:
                continue

# ══════════════════════════════════════════════════════════════
# Anthropic 引擎
# ══════════════════════════════════════════════════════════════
class AnthropicEngine(BaseEngine):
    engine_id = "anthropic"
    name      = "Anthropic Claude"
    API_URL   = "https://api.anthropic.com/v1/messages"

    MODELS = [
        {"id":"claude-opus-4-5",     "name":"Claude Opus 4.5",    "tag":"最強",  "vision":True},
        {"id":"claude-sonnet-4-5",   "name":"Claude Sonnet 4.5",  "tag":"平衡",  "vision":True},
        {"id":"claude-haiku-4-5",    "name":"Claude Haiku 4.5",   "tag":"快速",  "vision":True},
    ]

    def _key(self):
        return (_cfg("ANTHROPIC_API_KEY") or "").strip()

    def available(self):
        return bool(self._key())

    def list_models(self):
        return self.MODELS

    def _stream(self, messages, model=None, system=None, **kw):
        model = model or "claude-haiku-4-5"
        # 分離 system prompt
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        usr_msgs = [m for m in messages if m.get("role") != "system"]
        system_txt = system or (sys_msgs[0]["content"] if sys_msgs else "")

        payload = {
            "model":      model,
            "max_tokens": 2048,
            "stream":     True,
            "messages":   usr_msgs,
        }
        if system_txt:
            payload["system"] = system_txt

        r = requests.post(
            self.API_URL,
            headers={
                "x-api-key":         self._key(),
                "anthropic-version": "2023-06-01",
                "Content-Type":      "application/json",
            },
            json=payload, stream=True, timeout=90,
        )
        if not r.ok:
            err = r.json().get("error", {}).get("message", r.text)
            raise ValueError(f"❌ Anthropic 錯誤 {r.status_code}：{err[:200]}")

        full = ""
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                ev = json.loads(data_str)
                if ev.get("type") == "content_block_delta":
                    token = ev.get("delta", {}).get("text", "")
                    if token:
                        full += token
                        yield token, full
            except Exception:
                continue

# ══════════════════════════════════════════════════════════════
# Colab 引擎（自架）
# ══════════════════════════════════════════════════════════════
class ColabEngine(BaseEngine):
    engine_id    = "colab"
    name         = "Colab 自架"
    requires_key = False

    def _base_url(self):
        return _cfg("COLAB_API_URL", "")   # e.g. https://xxxx.ngrok-free.app

    def available(self):
        url = self._base_url()
        if not url:
            return False
        try:
            r = requests.get(f"{url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def list_models(self):
        url = self._base_url()
        if not url:
            return []
        try:
            r = requests.get(f"{url}/v1/models", timeout=5)
            if r.ok:
                return [{"id": m["id"], "name": m["id"], "tag": "Colab", "vision": False}
                        for m in r.json().get("data", [])]
        except Exception:
            pass
        return [{"id": "local-model", "name": "Colab 模型", "tag": "自架", "vision": False}]

    def _stream(self, messages, model=None, **kw):
        url   = self._base_url()
        model = model or "local-model"
        r = requests.post(
            f"{url}/v1/chat/completions",
            json={"model": model, "messages": messages, "stream": True, "max_tokens": 2048},
            stream=True, timeout=120,
        )
        yield from _parse_openai_stream(r, engine=self.engine_id, model=model)

# ══════════════════════════════════════════════════════════════
# 共用串流解析（OpenAI 格式）
# ══════════════════════════════════════════════════════════════
def _parse_openai_stream(r, engine="", model=""):
    if not r.ok:
        try:
            err = r.json().get("error", {}).get("message", r.text)
        except Exception:
            err = r.text
        code = r.status_code
        if code == 404:
            raise ValueError(f"❌ 模型不存在：{model}")
        elif code == 429:
            raise ValueError(f"⏳ Rate Limit（{engine}），請稍後再試。")
        elif code == 401:
            raise ValueError(f"❌ API Key 錯誤（{engine}）：{err[:120]}")
        else:
            raise ValueError(f"❌ [{engine}] 錯誤 {code}：{err[:200]}")
    full = ""
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
                token = chunk["choices"][0].get("delta", {}).get("content", "")
                if token:
                    full += token
                    yield token, full
            except Exception:
                continue

# ══════════════════════════════════════════════════════════════
# 引擎管理器
# ══════════════════════════════════════════════════════════════
class EngineManager:
    """
    統一管理所有引擎，提供：
    - 自動 fallback
    - 手動指定引擎
    - 狀態查詢
    - 模型清單彙整
    """

    # fallback 優先順序：隱私最高的在前
    FALLBACK_ORDER = ["ollama", "colab", "openrouter", "groq"]

    def __init__(self):
        self.engines = {
            "groq":        GroqEngine(),
            "openrouter":  OpenRouterEngine(),
            "ollama":      OllamaEngine(),
            "anthropic":   AnthropicEngine(),
            "colab":       ColabEngine(),
        }
        # 使用者手動選擇的引擎（None = 自動）
        self._preferred = None
        # 上次 ping 時間
        self._last_ping = 0

    def set_preferred(self, engine_id: Optional[str]):
        """手動指定引擎，None = 自動 fallback"""
        if engine_id and engine_id not in self.engines:
            raise ValueError(f"未知引擎：{engine_id}")
        self._preferred = engine_id

    def get_preferred(self):
        return self._preferred

    def _pick_engine(self, engine_id=None) -> BaseEngine:
        """選出要用的引擎"""
        eid = engine_id or self._preferred
        if eid:
            eng = self.engines.get(eid)
            if eng and eng.available():
                return eng
            elif eng:
                raise ValueError(f"❌ 引擎 {eng.name} 目前不可用")
            else:
                raise ValueError(f"❌ 未知引擎：{eid}")

        # 自動 fallback
        for eid in self.FALLBACK_ORDER:
            eng = self.engines[eid]
            if eng.available():
                return eng

        raise ValueError("❌ 所有引擎均不可用，請確認 API Key 設定")

    def stream(self, messages, engine_id=None, model=None, **kw) -> Generator:
        """
        主要呼叫入口：自動選引擎 + 串流輸出
        yield (token, full_text, engine_id)
        """
        eng = self._pick_engine(engine_id)
        try:
            for token, full in eng.stream(messages, model=model, **kw):
                yield token, full, eng.engine_id
            _status.set(eng.engine_id, True)
        except ValueError as e:
            msg = str(e)
            # Rate limit → 嘗試下一個引擎
            if "⏳" in msg and not engine_id:
                _status.set(eng.engine_id, False, error="rate_limit")
                next_eng = self._next_engine(eng.engine_id)
                if next_eng:
                    for token, full in next_eng.stream(messages, model=None, **kw):
                        yield token, full, next_eng.engine_id
                    return
            raise

    def _next_engine(self, current_id) -> Optional[BaseEngine]:
        """fallback 到下一個可用引擎"""
        order = self.FALLBACK_ORDER
        try:
            idx = order.index(current_id)
        except ValueError:
            idx = -1
        for eid in order[idx+1:]:
            eng = self.engines.get(eid)
            if eng and eng.available():
                return eng
        return None

    def status(self) -> dict:
        """回傳所有引擎狀態"""
        result = {}
        for eid, eng in self.engines.items():
            st = _status.get(eid)
            result[eid] = {
                "name":       eng.name,
                "available":  eng.available(),
                "ok":         st["ok"],
                "error":      st["error"],
                "latency_ms": st["latency_ms"],
                "last_check": st["last_check"],
                "preferred":  eid == self._preferred,
            }
        return result

    def ping_all(self):
        """背景 ping 所有引擎（在 thread 裡跑）"""
        def _run():
            for eng in self.engines.values():
                try:
                    eng.ping()
                except Exception:
                    pass
        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def all_models(self) -> list:
        """彙整所有引擎的模型清單"""
        result = []
        for eid, eng in self.engines.items():
            if not eng.available():
                continue
            for m in eng.list_models():
                result.append({**m, "engine": eid, "engine_name": eng.name})
        return result

    def models_by_engine(self) -> dict:
        """按引擎分組的模型清單"""
        result = {}
        for eid, eng in self.engines.items():
            if eng.available():
                result[eid] = {
                    "name":   eng.name,
                    "models": eng.list_models(),
                }
        return result


# ── 全域單例 ──
manager = EngineManager()

# ══════════════════════════════════════════════════════════════
# 向外暴露的便利函數（給 ai_routes.py 呼叫）
# ══════════════════════════════════════════════════════════════
def stream_ai(messages, engine_id=None, model=None, **kw):
    """
    主要呼叫入口
    yield (token, full_text, engine_id)
    """
    yield from manager.stream(messages, engine_id=engine_id, model=model, **kw)

def get_status() -> dict:
    return manager.status()

def set_engine(engine_id: Optional[str]):
    manager.set_preferred(engine_id)

def get_engine() -> Optional[str]:
    return manager.get_preferred()

def get_all_models() -> list:
    return manager.all_models()

def get_models_by_engine() -> dict:
    return manager.models_by_engine()

def ping_engines():
    manager.ping_all()