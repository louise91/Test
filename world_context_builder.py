"""
world_context_builder.py — 世界狀態注入策略
─────────────────────────────────────────────
策略 A：單人對話  → 完整注入（full）
策略 B：多代理討論 → 摘要注入（summary）
策略 C：自主代理  → 感知式（perception）
"""
import json, os, time, requests
from pathlib import Path

BASE       = Path(__file__).parent
WORLD_PATH = BASE / "data" / "world_state.json"
CHARS_PATH = BASE / "data" / "characters.json"

# ══════════════════════════════════════════════════════════════
# 資料讀取
# ══════════════════════════════════════════════════════════════
def _load_world() -> dict:
    try:
        return json.loads(WORLD_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _load_chars() -> list:
    try:
        return json.loads(CHARS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

def _get_char(char_id: str) -> dict:
    for c in _load_chars():
        if c["id"] == char_id:
            return c
    return {}

def _get_api_key():
    try:
        import config
        return getattr(config, "GROQ_API_KEY", os.environ.get("GROQ_API_KEY",""))
    except ImportError:
        return os.environ.get("GROQ_API_KEY","")

# ══════════════════════════════════════════════════════════════
# 策略 A：完整注入（單人對話用）
# ══════════════════════════════════════════════════════════════
def build_full_context(char_id: str) -> str:
    """
    把世界狀態完整組成 system prompt 前綴
    適合：單一角色的沉浸式對話
    長度：約 400~600 字
    """
    world  = _load_world()
    char   = _get_char(char_id)
    meta   = world.get("world_meta", {})
    states = world.get("character_states", {})
    scenes = world.get("scenes", {})
    st     = states.get(char_id, {})

    # ── 場景資訊 ──
    loc      = st.get("location", "辦公室")
    scene_id = _loc_to_scene_id(loc)
    scene    = scenes.get(scene_id, {})

    # ── 天氣 ──
    weather = meta.get("weather", {})
    w_emoji = weather.get("emoji", "🌡")
    w_desc  = (f"{w_emoji} {weather.get('condition','')} "
               f"{weather.get('temperature_c','')}°C，"
               f"濕度 {weather.get('humidity_pct','')}%，"
               f"氣壓 {weather.get('pressure_hpa','')}hPa，"
               f"風 {weather.get('wind_speed_ms','')}m/s {weather.get('wind_direction','')}")

    # ── 組合 ──
    lines = [
        "═══ 【世界狀態】═══",
        f"時間：{meta.get('date','')} {meta.get('time','')} {meta.get('day_of_week','')}，{meta.get('season','')}季",
        f"地點：{meta.get('location',{}).get('building','')} {meta.get('location',{}).get('floor','')}",
        f"天氣：{w_desc}",
        f"世界氛圍：{meta.get('atmosphere','')}，{meta.get('phase','')}",
        "",
        "═══ 【你的當前狀態】═══",
        f"位置：{loc}",
    ]

    # 場景細節
    if scene:
        scene_details = []
        if scene.get("lighting"):      scene_details.append(f"燈光：{scene['lighting']}")
        if scene.get("temperature_c") is not None:
                                       scene_details.append(f"溫度：{scene['temperature_c']}°C")
        if scene.get("noise_level"):   scene_details.append(f"噪音：{scene['noise_level']}")
        if scene.get("smell"):         scene_details.append(f"氣味：{scene['smell']}")
        if scene_details:
            lines.append("場景感知：" + "，".join(scene_details))
        if scene.get("notes"):
            lines.append(f"場景備注：{scene['notes']}")
        if scene.get("items"):
            lines.append(f"場景物品：{'、'.join(scene['items'][:4])}")

    # 角色狀態
    if st:
        lines += [
            "",
            f"姿勢：{st.get('posture','站姿')}",
            f"情緒：{st.get('emotion','')}（強度 {int(st.get('emotion_intensity',0.5)*100)}%）" +
            (f"，次要情緒：{st['secondary_emotion']}" if st.get("secondary_emotion") else ""),
            f"精力：{int(st.get('energy_level',0.5)*100)}%　"
            f"壓力：{int(st.get('stress_level',0.5)*100)}%　"
            f"飢餓：{int(st.get('hunger',0.3)*100)}%",
        ]

        outfit = st.get("outfit", {})
        if outfit:
            outfit_str = f"{outfit.get('style','')}：{outfit.get('top','')}，{outfit.get('bottom','')}（{outfit.get('condition','')}）"
            if outfit.get("note"):
                outfit_str += f"，{outfit['note']}"
            lines.append(f"衣著：{outfit_str}")

        if st.get("holding"):
            lines.append(f"手持：{st.get('holding')}")

        if st.get("current_task"):
            progress = int(st.get("task_progress", 0) * 100)
            lines.append(f"任務：{st['current_task']}（進度 {progress}%）")

        if st.get("short_term_memory"):
            lines.append(f"近期記憶：{'；'.join(st['short_term_memory'][:3])}")

        if st.get("private_notes"):
            lines.append(f"內心想法：{st['private_notes']}")

    # 其他在場角色
    others = _get_others_in_scene(char_id, states, scene_id)
    if others:
        lines += ["", f"同場角色：{others}"]

    lines.append("═══════════════")
    lines.append("請完全投入角色，根據以上世界狀態自然地回應，不要說明或跳出角色。")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 策略 B：摘要注入（多代理討論用）
# ══════════════════════════════════════════════════════════════
def build_summary_context(char_id: str = None) -> str:
    """
    只帶最關鍵的世界狀態，控制在 100 字以內
    適合：多代理討論（節省 context）
    """
    world   = _load_world()
    meta    = world.get("world_meta", {})
    weather = meta.get("weather", {})

    base = (
        f"【現在】{meta.get('date','')} {meta.get('time','')} {meta.get('day_of_week','')}，"
        f"{weather.get('emoji','')} {weather.get('condition','')} {weather.get('temperature_c','')}°C。"
        f"公司氛圍：{meta.get('atmosphere','')}，{meta.get('phase','')}。"
    )

    if char_id:
        states = world.get("character_states", {})
        st     = states.get(char_id, {})
        if st:
            base += (
                f"\n你目前：{st.get('location','')}，"
                f"{st.get('emotion','')}（{int(st.get('emotion_intensity',0.5)*100)}%），"
                f"精力 {int(st.get('energy_level',0.5)*100)}%。"
                f"手持：{st.get('holding','無')}。"
            )

    return base


# ══════════════════════════════════════════════════════════════
# 策略 C：感知式（自主代理用）
# ══════════════════════════════════════════════════════════════
def build_perception(char_id: str) -> dict:
    """
    讓 AI 自己決定「此刻感知到什麼」
    回傳：{
      "raw_context": str,         # 完整世界狀態（給 AI 讀）
      "perception": str,          # AI 過濾後的感知結果
      "relevant_facts": list,     # 列出感知到的事實
      "action_hints": list,       # 建議的行動方向
    }
    """
    world  = _load_world()
    char   = _get_char(char_id)
    states = world.get("character_states", {})
    st     = states.get(char_id, {})
    meta   = world.get("world_meta", {})
    scenes = world.get("scenes", {})

    # 先建完整 raw context
    raw = build_full_context(char_id)

    # 加入他人狀態（感知周圍）
    all_chars_info = []
    for cid, cst in states.items():
        if cid == char_id:
            continue
        c = _get_char(cid)
        if not c:
            continue
        # 只有在同一場景或鄰近的才能感知到
        same_scene = _loc_to_scene_id(cst.get("location","")) == _loc_to_scene_id(st.get("location",""))
        if same_scene:
            all_chars_info.append(
                f"{c.get('name',cid)}（{c.get('role','')}）：{cst.get('emotion','')}，{cst.get('current_task','無任務')}"
            )

    api_key = _get_api_key()
    if not api_key:
        # 沒有 API → 直接用規則式感知
        return _rule_based_perception(char_id, world, char, st, meta, scenes, all_chars_info)

    # 用 AI 過濾感知
    perception_prompt = f"""你是 {char.get('name','角色')}（{char.get('role','')}）。

以下是完整的世界狀態資訊：
{raw}

同場其他人：{', '.join(all_chars_info) if all_chars_info else '無'}

請以第一人稱，用 JSON 格式描述你此刻的感知（只回 JSON，不要其他文字）：
{{
  "perception": "用一段話描述你此刻的感知（50字內）",
  "relevant_facts": ["你注意到的重要事實1", "事實2", "事實3"],
  "action_hints": ["根據現狀，你可能想做的事1", "可能2"],
  "mood_trigger": "是什麼讓你有這種情緒"
}}"""

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": perception_prompt}],
                "max_tokens": 400, "temperature": 0.6,
            },
            timeout=20,
        )
        if r.ok:
            raw_resp = r.json()["choices"][0]["message"]["content"].strip()
            raw_resp = raw_resp.replace("```json","").replace("```","").strip()
            parsed = json.loads(raw_resp)
            return {
                "raw_context":    raw,
                "perception":     parsed.get("perception",""),
                "relevant_facts": parsed.get("relevant_facts",[]),
                "action_hints":   parsed.get("action_hints",[]),
                "mood_trigger":   parsed.get("mood_trigger",""),
                "others_nearby":  all_chars_info,
                "strategy":       "C_ai_perception",
            }
    except Exception:
        pass

    return _rule_based_perception(char_id, world, char, st, meta, scenes, all_chars_info)


def _rule_based_perception(char_id, world, char, st, meta, scenes, others_nearby) -> dict:
    """無 AI 時的規則式感知"""
    loc     = st.get("location","辦公室")
    scene   = scenes.get(_loc_to_scene_id(loc), {})
    weather = meta.get("weather", {})
    emotion = st.get("emotion","")
    energy  = st.get("energy_level", 0.5)
    hunger  = st.get("hunger", 0.3)

    facts = []
    if energy < 0.3:    facts.append("精力非常低，感到疲憊")
    if hunger > 0.7:    facts.append("很餓，需要進食")
    if weather.get("condition_code") in ("rain","storm"): facts.append("外面在下雨")
    if scene.get("noise_db",0) > 65: facts.append(f"環境噪音大（{scene.get('noise_db')}dB）")
    if st.get("task_progress",0) < 0.2: facts.append("任務進度落後")

    hints = []
    if hunger > 0.7: hints.append("去休息區吃東西")
    if energy < 0.3: hints.append("需要休息或喝咖啡")
    if st.get("current_task"): hints.append(f"繼續處理：{st.get('current_task')}")

    perception = (
        f"現在是{meta.get('time','')}，我在{loc}，感覺{emotion}。"
        f"精力{int(energy*100)}%。"
        + (f"周圍有：{', '.join(others_nearby[:2])}。" if others_nearby else "")
    )

    return {
        "raw_context":    build_full_context(char_id),
        "perception":     perception,
        "relevant_facts": facts,
        "action_hints":   hints,
        "mood_trigger":   st.get("private_notes",""),
        "others_nearby":  others_nearby,
        "strategy":       "C_rule_based",
    }


def perception_to_system_prompt(char_id: str, perception_result: dict) -> str:
    """
    把感知結果轉成 agent 行動用的 system prompt
    """
    char = _get_char(char_id)
    p    = perception_result

    lines = [
        f"你是 {char.get('name','')}（{char.get('role','')}）。",
        f"{char.get('personality','')}",
        "",
        "【此刻的感知】",
        p.get("perception",""),
        "",
    ]
    if p.get("relevant_facts"):
        lines.append("【你注意到的事】")
        for f in p["relevant_facts"]:
            lines.append(f"  • {f}")
        lines.append("")
    if p.get("others_nearby"):
        lines.append("【周圍的人】")
        for o in p["others_nearby"]:
            lines.append(f"  • {o}")
        lines.append("")
    if p.get("mood_trigger"):
        lines.append(f"【讓你有這種情緒的原因】{p['mood_trigger']}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 通用工具
# ══════════════════════════════════════════════════════════════
_LOC_MAP = {
    "辦公室":"office","辦公區":"office","工作區":"office",
    "會議室":"meeting","會議區":"meeting",
    "休息區":"lounge","茶水間":"lounge","休息室":"lounge",
    "大廳":"lobby","入口":"lobby",
    "機房":"server","伺服器室":"server",
    "執行長辦公室":"ceo","董事長室":"ceo","老闆辦公室":"ceo",
    "戶外":"outdoor","室外":"outdoor",
}

def _loc_to_scene_id(location: str) -> str:
    return _LOC_MAP.get(location, "office")

def _get_others_in_scene(char_id: str, states: dict, scene_id: str) -> str:
    others = []
    for cid, st in states.items():
        if cid == char_id:
            continue
        if _loc_to_scene_id(st.get("location","")) == scene_id:
            c = _get_char(cid)
            name = c.get("name", cid) if c else cid
            others.append(f"{name}（{st.get('emotion','')}）")
    return "、".join(others) if others else ""


# ══════════════════════════════════════════════════════════════
# 對外主入口
# ══════════════════════════════════════════════════════════════
def inject(strategy: str, char_id: str = None) -> str:
    """
    strategy: "full" | "summary" | "perception"
    回傳 system prompt 前綴字串
    """
    if strategy == "full":
        if not char_id:
            return build_summary_context()
        return build_full_context(char_id)
    elif strategy == "summary":
        return build_summary_context(char_id)
    elif strategy == "perception":
        if not char_id:
            return build_summary_context()
        p = build_perception(char_id)
        return perception_to_system_prompt(char_id, p)
    return ""