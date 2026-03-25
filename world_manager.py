# ================== 世界狀態管理模組 ==================
# 讀寫 characters.json 和 world_state.json
# 選角色時自動組合背景 context 注入 system prompt

import os, json, time

BASE           = os.path.dirname(os.path.abspath(__file__))
DATA_DIR       = os.path.join(BASE, "data")
CHARS_FILE     = os.path.join(DATA_DIR, "characters.json")
WORLD_FILE     = os.path.join(DATA_DIR, "world_state.json")

os.makedirs(DATA_DIR, exist_ok=True)

# ══════════════════════════════════════
# 角色讀寫
# ══════════════════════════════════════
def load_characters() -> list:
    try:
        with open(CHARS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_characters(chars: list):
    with open(CHARS_FILE, "w", encoding="utf-8") as f:
        json.dump(chars, f, ensure_ascii=False, indent=2)

def get_character(char_id: str) -> dict | None:
    return next((c for c in load_characters() if c["id"] == char_id), None)

# ══════════════════════════════════════
# 世界狀態讀寫
# ══════════════════════════════════════
def load_world() -> dict:
    try:
        with open(WORLD_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "world_meta": {"name": "未命名世界", "date": "", "phase": "", "overall_mood": ""},
            "character_states": {},
            "event_history": []
        }

def save_world(world: dict):
    with open(WORLD_FILE, "w", encoding="utf-8") as f:
        json.dump(world, f, ensure_ascii=False, indent=2)

def update_character_state(char_id: str, updates: dict):
    """更新單一角色的狀態"""
    world = load_world()
    if char_id not in world["character_states"]:
        world["character_states"][char_id] = {}
    world["character_states"][char_id].update(updates)
    save_world(world)

def add_event(event: dict):
    """新增一筆事件歷史"""
    world = load_world()
    event.setdefault("id", f"evt_{int(time.time())}")
    event.setdefault("date", time.strftime("%Y-%m-%d"))
    world["event_history"].append(event)
    # 只保留最近 50 筆
    if len(world["event_history"]) > 50:
        world["event_history"] = world["event_history"][-50:]
    save_world(world)

# ══════════════════════════════════════
# 核心：組合 system prompt 背景
# ══════════════════════════════════════
def build_character_context(char_id: str) -> str:
    """
    給定角色 ID，回傳注入 system prompt 的背景字串
    包含：世界狀態、角色當前狀態、相關近期事件
    """
    char  = get_character(char_id)
    world = load_world()
    if not char:
        return ""

    lines = []

    # 世界背景
    meta = world.get("world_meta", {})
    if meta.get("name"):
        lines.append(f"【世界背景】{meta['name']} | {meta.get('date','')} | {meta.get('phase','')} | 整體氛圍：{meta.get('overall_mood','')}")

    # 角色當前狀態
    state = world.get("character_states", {}).get(char_id, {})
    if state:
        lines.append(f"\n【你現在的狀態】")
        if state.get("emotion"):    lines.append(f"情緒：{state['emotion']}")
        if state.get("location"):   lines.append(f"位置：{state['location']}")
        if state.get("current_task"):lines.append(f"當前任務：{state['current_task']}")
        if state.get("private_notes"):lines.append(f"內心想法：{state['private_notes']}")

    # 與其他角色的關係
    relationships = state.get("relationships", {})
    if relationships:
        lines.append(f"\n【你與其他人的關係】")
        chars = {c["id"]: c["name"] for c in load_characters()}
        for other_id, rel in relationships.items():
            other_name = chars.get(other_id, other_id)
            lines.append(f"{other_name}：{rel}")

    # 近期相關事件（最多3筆）
    events = world.get("event_history", [])
    related = [e for e in events if char_id in e.get("involved", [])][-3:]
    if related:
        lines.append(f"\n【近期相關事件】")
        for e in related:
            status = f"[{e.get('status','')}]" if e.get("status") else ""
            lines.append(f"{e.get('date','')} {e.get('title','')} {status}")
            if e.get("description"):
                lines.append(f"  {e['description']}")

    lines.append("\n請根據以上背景，以角色身份回應，保持個性一致。")
    return "\n".join(lines)

def build_multi_context(char_ids: list) -> str:
    """
    多代理討論時：組合所有角色都能看到的共同世界狀態
    （不包含各角色的私人內心想法）
    """
    world = load_world()
    chars = {c["id"]: c for c in load_characters()}
    lines = []

    meta = world.get("world_meta", {})
    if meta.get("name"):
        lines.append(f"【世界背景】{meta['name']} | {meta.get('date','')} | {meta.get('phase','')} | 氛圍：{meta.get('overall_mood','')}")

    # 各角色公開狀態（不含私人想法）
    lines.append("\n【各角色當前狀態】")
    for cid in char_ids:
        char  = chars.get(cid)
        state = world.get("character_states", {}).get(cid, {})
        if char and state:
            lines.append(
                f"{char['name']}（{char.get('role','')}）："
                f"情緒={state.get('emotion','?')} | "
                f"位置={state.get('location','?')} | "
                f"任務={state.get('current_task','?')}"
            )

    # 近期事件（所有人可見）
    events = world.get("event_history", [])
    relevant = [
        e for e in events
        if any(cid in e.get("involved", []) for cid in char_ids)
    ][-5:]
    if relevant:
        lines.append("\n【近期相關事件】")
        for e in relevant:
            lines.append(f"{e.get('date','')} {e.get('title','')} [{e.get('status','')}]")

    return "\n".join(lines)