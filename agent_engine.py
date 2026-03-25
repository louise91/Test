# ================== 自主代理引擎 ==================
# 代理行動迴圈：思考 → 搜集 → 開會 → 更新狀態
# 可手動觸發，也可排程自動跑

import os, json, time, threading, requests
from world_manager import (
    load_characters, load_world, save_world,
    build_character_context, add_event, update_character_state
)

GROQ_API_KEY = None
BRAVE_API_KEY = None

try:
    from config import GROQ_API_KEY, BRAVE_API_KEY
except ImportError:
    GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")
    BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"

# ── 執行日誌（記憶體，重啟清空）──
_action_log: list[dict] = []
_log_lock = threading.Lock()

def log_action(char_name: str, action: str, detail: str = ""):
    with _log_lock:
        entry = {
            "time":      time.strftime("%H:%M:%S"),
            "date":      time.strftime("%Y-%m-%d"),
            "char_name": char_name,
            "action":    action,
            "detail":    detail[:300]
        }
        _action_log.append(entry)
        if len(_action_log) > 200:
            _action_log.pop(0)

def get_log(limit: int = 50) -> list:
    with _log_lock:
        return list(reversed(_action_log[-limit:]))

# ── LLM 呼叫 ──
def call_llm(messages: list, max_tokens: int = 600) -> str:
    wait = 5
    for attempt in range(3):
        try:
            r = requests.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json"
                },
                json={
                    "model":       GROQ_MODEL,
                    "messages":    messages,
                    "max_tokens":  max_tokens,
                    "temperature": 0.8,
                },
                timeout=60
            )
            if r.status_code == 429:
                time.sleep(int(r.headers.get("retry-after", wait)))
                wait *= 2; continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt < 2:
                time.sleep(wait); wait *= 2; continue
            return f"[LLM 錯誤: {e}]"
    return "[超過重試次數]"

# ── 網路搜尋 ──
def agent_search(query: str) -> str:
    try:
        if BRAVE_API_KEY:
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
                params={"q": query, "count": 5}, timeout=10
            )
            r.raise_for_status()
            results = r.json().get("web", {}).get("results", [])
            return "\n".join([f"• {i['title']}: {i.get('description','')}" for i in results[:5]])
        else:
            r = requests.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10
            )
            data = r.json()
            lines = []
            if data.get("Abstract"):
                lines.append(data["Abstract"])
            for t in data.get("RelatedTopics", [])[:4]:
                if isinstance(t, dict) and t.get("Text"):
                    lines.append(f"• {t['Text']}")
            return "\n".join(lines) if lines else "無搜尋結果"
    except Exception as e:
        return f"搜尋失敗: {e}"

# ══════════════════════════════════════
# 代理行動迴圈
# ══════════════════════════════════════
def run_agent_cycle(char_id: str, trigger: str = "手動") -> dict:
    """
    單一代理完整行動迴圈：
    1. 思考（根據當前狀態決定行動）
    2. 搜集資料（如果需要）
    3. 更新自己的狀態
    回傳行動摘要
    """
    char = next((c for c in load_characters() if c["id"] == char_id), None)
    if not char:
        return {"error": f"找不到角色 {char_id}"}

    name  = char["name"]
    world = load_world()
    state = world.get("character_states", {}).get(char_id, {})

    log_action(name, "開始行動", f"觸發：{trigger}")

    # ── 策略 C：感知式世界狀態注入 ──
    perception_result = {}
    perception_str    = ""
    try:
        from world_context_builder import build_perception, perception_to_system_prompt
        perception_result = build_perception(char_id)
        perception_str    = perception_to_system_prompt(char_id, perception_result)
        log_action(name, "感知世界", perception_result.get("perception","")[:100])
    except Exception as e:
        # fallback：用舊的 build_character_context
        from world_manager import build_character_context
        perception_str = build_character_context(char_id)

    # ── 步驟1：思考（帶感知結果）──
    relevant = ""
    if perception_result.get("relevant_facts"):
        relevant = "\n你注意到：" + "；".join(perception_result["relevant_facts"])
    action_hints = ""
    if perception_result.get("action_hints"):
        action_hints = "\n可能的行動方向：" + "；".join(perception_result["action_hints"])

    think_prompt = f"""{perception_str}
{relevant}
{action_hints}

現在請你進行一輪自主思考，用 JSON 格式回答（不要其他文字）：
{{
  "concern": "你目前最在意的事",
  "action_plan": "你打算採取什麼行動",
  "search_needed": true或false,
  "search_query": "搜尋關鍵字（如果需要，否則空字串）",
  "emotion": "當前情緒",
  "thought": "內心想法（考慮到你感知到的世界狀態）"
}}"""

    think_result_raw = call_llm([
        {"role": "system", "content": perception_str},
        {"role": "user",   "content": think_prompt}
    ], max_tokens=400)

    log_action(name, "思考完成", think_result_raw[:200])

    # 解析思考結果
    think_result = {}
    try:
        clean = think_result_raw.strip()
        if "```" in clean:
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        think_result = json.loads(clean.strip())
    except Exception:
        # 解析失敗就用預設值
        think_result = {
            "concern":       "無法解析",
            "action_plan":   think_result_raw[:100],
            "search_needed": False,
            "emotion":       state.get("emotion", "平靜"),
            "thought":       think_result_raw[:200]
        }

    # ── 步驟2：搜集資料（如果需要）──
    search_result = ""
    if think_result.get("search_needed") and think_result.get("search_query"):
        query = think_result["search_query"]
        log_action(name, "搜集資料", f"搜尋：{query}")
        search_result = agent_search(query)
        log_action(name, "搜集完成", search_result[:200])

    # ── 步驟3：根據思考和搜集，生成行動結果 ──
    action_context = f"你的思考：{think_result.get('thought','')}"
    if search_result:
        action_context += f"\n\n你搜集到的資料：\n{search_result}"

    action_prompt = f"""{action_context}

根據以上，請說明你今天的行動結果（100字以內，用第一人稱，自然融入你的感知環境）："""

    action_summary = call_llm([
        {"role": "system", "content": perception_str or char.get("system_prompt","")},
        {"role": "user",   "content": action_prompt}
    ], max_tokens=200)

    log_action(name, "行動完成", action_summary[:200])

    # ── 步驟4：更新狀態 ──
    new_state = {
        "emotion":       think_result.get("emotion", state.get("emotion", "平靜")),
        "current_task":  think_result.get("action_plan", state.get("current_task", "")),
        "private_notes": think_result.get("thought", ""),
        "last_action":   action_summary,
        "last_updated":  time.strftime("%Y-%m-%d %H:%M")
    }
    update_character_state(char_id, new_state)
    log_action(name, "狀態更新", f"情緒：{new_state['emotion']}")

    # 記錄到事件歷史
    add_event({
        "type":        "代理行動",
        "title":       f"{name} 完成行動迴圈",
        "description": action_summary,
        "involved":    [char_id],
        "status":      "完成",
        "trigger":     trigger
    })

    return {
        "char_id":      char_id,
        "name":         name,
        "concern":      think_result.get("concern", ""),
        "emotion":      new_state["emotion"],
        "action":       action_summary,
        "searched":     bool(search_result),
        "search_query": think_result.get("search_query", ""),
    }

# ══════════════════════════════════════
# 會議：多代理討論 + 存結果
# ══════════════════════════════════════
def run_meeting(char_ids: list, topic: str, trigger: str = "手動") -> dict:
    """
    觸發多代理會議：
    各代理根據各自狀態討論 topic
    結果存進事件歷史
    """
    from true_multi_agent import run_discussion

    chars    = {c["id"]: c for c in load_characters()}
    attendees = [chars[cid] for cid in char_ids if cid in chars]

    if len(attendees) < 2:
        return {"error": "需要至少 2 個角色"}

    names = [c["name"] for c in attendees]
    log_action("會議", "開始", f"參與者：{','.join(names)} | 主題：{topic}")

    # 組成 agents 格式（帶世界狀態）
    agents = []
    for c in attendees:
        ctx = build_character_context(c["id"])
        agents.append({
            "id":     c["id"],
            "name":   c["name"],
            "prompt": c.get("system_prompt", "") + (("\n\n" + ctx) if ctx else "")
        })

    # 執行討論
    result_queue = run_discussion(
        agents, topic,
        moderator_prompt="你是客觀的會議記錄員，整合所有觀點，給出清楚的會議結論與決議。用繁體中文。",
        num_rounds=2
    )

    # 收集結果（blocking，等待完成）
    all_results  = {}
    moderator_summary = ""
    timeout = 120

    start = time.time()
    while time.time() - start < timeout:
        try:
            ev = result_queue.get(timeout=5)
        except Exception:
            break

        if ev["type"] == "agent_result":
            all_results[ev["name"]] = ev["text"]
        elif ev["type"] == "moderator_result":
            moderator_summary = ev["text"]
        elif ev["type"] == "all_done":
            break

    log_action("會議", "結束", moderator_summary[:200])

    # 存進事件歷史
    discussion_text = "\n\n".join([
        f"【{name}】：{content[:300]}"
        for name, content in all_results.items()
    ])

    add_event({
        "type":        "會議",
        "title":       f"會議：{topic}",
        "description": moderator_summary or discussion_text[:500],
        "involved":    char_ids,
        "status":      "完成",
        "trigger":     trigger,
        "full_record": discussion_text
    })

    # 更新各代理的狀態（加入會議記憶）
    for cid in char_ids:
        char_name = chars.get(cid, {}).get("name", cid)
        own_speech = all_results.get(char_name, "")
        update_character_state(cid, {
            "last_meeting": topic,
            "last_meeting_time": time.strftime("%Y-%m-%d %H:%M"),
            "private_notes": f"剛開完會議：{topic}。我說：{own_speech[:100]}"
        })

    return {
        "topic":     topic,
        "attendees": names,
        "summary":   moderator_summary,
        "results":   all_results
    }

# ══════════════════════════════════════
# 排程：所有代理每天自動行動
# ══════════════════════════════════════
def run_daily_cycle(trigger: str = "排程"):
    """
    所有代理依序行動一次
    可以由 PythonAnywhere Scheduled Task 呼叫
    """
    chars = load_characters()
    results = []
    for char in chars:
        try:
            r = run_agent_cycle(char["id"], trigger=trigger)
            results.append(r)
            time.sleep(3)  # 避免 rate limit
        except Exception as e:
            log_action(char.get("name","?"), "錯誤", str(e))

    # 所有人行動完後，觸發一次全體會議
    if len(chars) >= 2:
        all_ids = [c["id"] for c in chars]
        world   = load_world()
        meta    = world.get("world_meta", {})
        topic   = f"{meta.get('date','今日')} 日常同步會議"
        run_meeting(all_ids, topic, trigger=trigger)

    return results

# ══════════════════════════════════════
# 供 PythonAnywhere Scheduled Task 直接執行
# ══════════════════════════════════════
if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daily"

    if cmd == "daily":
        print("開始每日代理循環...")
        results = run_daily_cycle(trigger="每日排程")
        for r in results:
            print(f"  {r.get('name','')}：{r.get('emotion','')} | {r.get('action','')[:80]}")

    elif cmd == "meeting" and len(sys.argv) > 3:
        ids   = sys.argv[2].split(",")
        topic = sys.argv[3]
        print(f"開會：{topic}")
        result = run_meeting(ids, topic, trigger="手動排程")
        print(f"結論：{result.get('summary','')[:200]}")

    elif cmd == "agent" and len(sys.argv) > 2:
        char_id = sys.argv[2]
        print(f"觸發代理 {char_id}...")
        result = run_agent_cycle(char_id, trigger="手動排程")
        print(f"  情緒：{result.get('emotion','')} | 行動：{result.get('action','')[:100]}")