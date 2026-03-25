"""
world_engine.py — 世界引擎
功能：
  1. 時間推進（+1小時 / +1天）
  2. 天氣自動更新（wttr.in，免費不需 Key）
  3. 時間推進後 AI 自動更新角色狀態
  4. 場景狀態更新（天氣影響室外場景）
"""
import os, json, time, datetime, requests
from pathlib import Path

# ── 路徑 ──
BASE = Path(__file__).parent
WORLD_PATH = BASE / "data" / "world_state.json"
CHARS_PATH = BASE / "data" / "characters.json"

def _load() -> dict:
    try:
        return json.loads(WORLD_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save(data: dict):
    WORLD_PATH.parent.mkdir(exist_ok=True)
    WORLD_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _get_api_key(name):
    try:
        import config
        return getattr(config, name, os.environ.get(name, ""))
    except ImportError:
        return os.environ.get(name, "")

# ══════════════════════════════════════════════════════════════
# 天氣系統
# ══════════════════════════════════════════════════════════════

# condition_code → 代碼分類
WEATHER_CODES = {
    "clear":         {"zh": "晴天",   "code": "clear",   "emoji": "☀️",  "particle": "none"},
    "sunny":         {"zh": "晴天",   "code": "clear",   "emoji": "☀️",  "particle": "none"},
    "partly_cloudy": {"zh": "多雲時晴","code": "pcloudy", "emoji": "⛅",  "particle": "none"},
    "cloudy":        {"zh": "多雲",   "code": "cloudy",  "emoji": "☁️",  "particle": "none"},
    "overcast":      {"zh": "陰天",   "code": "cloudy",  "emoji": "🌥",  "particle": "none"},
    "mist":          {"zh": "薄霧",   "code": "mist",    "emoji": "🌫",  "particle": "mist"},
    "fog":           {"zh": "大霧",   "code": "fog",     "emoji": "🌁",  "particle": "mist"},
    "drizzle":       {"zh": "毛毛雨", "code": "rain",    "emoji": "🌦",  "particle": "rain_light"},
    "rain":          {"zh": "下雨",   "code": "rain",    "emoji": "🌧",  "particle": "rain"},
    "heavy_rain":    {"zh": "大雨",   "code": "rain",    "emoji": "⛈",  "particle": "rain_heavy"},
    "thunderstorm":  {"zh": "雷雨",   "code": "storm",   "emoji": "⛈",  "particle": "rain_heavy"},
    "snow":          {"zh": "下雪",   "code": "snow",    "emoji": "❄️",  "particle": "snow"},
    "sleet":         {"zh": "雨夾雪", "code": "snow",    "emoji": "🌨",  "particle": "snow"},
    "hail":          {"zh": "冰雹",   "code": "hail",    "emoji": "🌨",  "particle": "rain_heavy"},
    "windy":         {"zh": "強風",   "code": "wind",    "emoji": "💨",  "particle": "none"},
    "blizzard":      {"zh": "暴雪",   "code": "snow",    "emoji": "🌨",  "particle": "snow"},
}

def _parse_wttr_condition(desc: str) -> dict:
    """解析 wttr.in 天氣描述 → 標準格式"""
    desc_lower = desc.lower()
    for key, info in WEATHER_CODES.items():
        if key in desc_lower:
            return info
    if "rain" in desc_lower or "shower" in desc_lower:
        return WEATHER_CODES["rain"]
    if "cloud" in desc_lower or "overcast" in desc_lower:
        return WEATHER_CODES["cloudy"]
    if "sun" in desc_lower or "clear" in desc_lower:
        return WEATHER_CODES["clear"]
    if "snow" in desc_lower:
        return WEATHER_CODES["snow"]
    return {"zh": desc, "code": "unknown", "emoji": "🌡", "particle": "none"}

def fetch_weather(city: str = "Taipei") -> dict:
    """
    從 wttr.in 取得即時天氣（免費，不需 API Key）
    回傳標準天氣 dict
    """
    try:
        url = f"https://wttr.in/{city}?format=j1"
        r = requests.get(url, timeout=10)
        if not r.ok:
            return _weather_fallback()
        data = r.json()
        cur  = data["current_condition"][0]
        desc = cur.get("weatherDesc", [{}])[0].get("value", "Unknown")
        parsed = _parse_wttr_condition(desc)

        temp_c    = int(cur.get("temp_C", 20))
        feels_c   = int(cur.get("FeelsLikeC", temp_c))
        humidity  = int(cur.get("humidity", 60))
        pressure  = int(cur.get("pressure", 1013))
        wind_kmh  = int(cur.get("windspeedKmph", 10))
        wind_ms   = round(wind_kmh / 3.6, 1)
        wind_dir  = cur.get("winddir16Point", "N")
        uv        = int(cur.get("uvIndex", 3))
        vis_km    = int(cur.get("visibility", 10))

        # 風向中文化
        wind_dir_zh = {
            "N":"北","NNE":"北北東","NE":"東北","ENE":"東北東",
            "E":"東","ESE":"東南東","SE":"東南","SSE":"南南東",
            "S":"南","SSW":"南南西","SW":"西南","WSW":"西南西",
            "W":"西","WNW":"西北西","NW":"西北","NNW":"北北西",
        }.get(wind_dir, wind_dir)

        # 天氣感受描述
        feel_desc = _weather_feel_desc(temp_c, parsed["code"], humidity, wind_ms)

        return {
            "condition":      parsed["zh"],
            "condition_code": parsed["code"],
            "emoji":          parsed["emoji"],
            "particle":       parsed["particle"],
            "temperature_c":  temp_c,
            "feels_like_c":   feels_c,
            "humidity_pct":   humidity,
            "pressure_hpa":   pressure,
            "wind_speed_ms":  wind_ms,
            "wind_direction": wind_dir_zh,
            "uv_index":       uv,
            "visibility_km":  vis_km,
            "description":    feel_desc,
            "last_updated":   datetime.datetime.now().isoformat(timespec="minutes"),
            "source":         "wttr.in",
        }
    except Exception as e:
        return _weather_fallback()

def _weather_fallback() -> dict:
    return {
        "condition": "多雲", "condition_code": "cloudy",
        "emoji": "☁️", "particle": "none",
        "temperature_c": 20, "feels_like_c": 19,
        "humidity_pct": 65, "pressure_hpa": 1013,
        "wind_speed_ms": 2.0, "wind_direction": "北",
        "uv_index": 3, "visibility_km": 10,
        "description": "天氣資料暫時無法取得",
        "last_updated": datetime.datetime.now().isoformat(timespec="minutes"),
        "source": "fallback",
    }

def _weather_feel_desc(temp_c, code, humidity, wind_ms) -> str:
    """根據數據生成天氣感受描述"""
    parts = []
    if code == "clear":
        parts.append("晴空萬里" if temp_c > 25 else "天氣晴朗")
    elif code == "rain":
        parts.append("雨天，記得帶傘")
    elif code == "snow":
        parts.append("降雪中，路面可能濕滑")
    elif code == "cloudy":
        parts.append("天色陰沉" if humidity > 80 else "多雲")

    if temp_c <= 5:    parts.append("非常寒冷")
    elif temp_c <= 15: parts.append("涼意十足")
    elif temp_c <= 25: parts.append("溫度適中")
    elif temp_c <= 32: parts.append("有些悶熱")
    else:              parts.append("酷熱難耐")

    if wind_ms > 10: parts.append("強風")
    if humidity > 85: parts.append("體感潮濕")

    return "，".join(parts)

# ══════════════════════════════════════════════════════════════
# 時間系統
# ══════════════════════════════════════════════════════════════

WEEKDAYS = ["週一","週二","週三","週四","週五","週六","週日"]
SEASONS  = {(3,4,5):"春",(6,7,8):"夏",(9,10,11):"秋",(12,1,2):"冬"}

def _get_season(month: int) -> str:
    for months, name in SEASONS.items():
        if month in months:
            return name
    return "春"

def _parse_datetime(world: dict):
    meta = world.get("world_meta", {})
    date_str = meta.get("date", datetime.date.today().isoformat())
    time_str = meta.get("time", "09:00")
    dt = datetime.datetime.fromisoformat(f"{date_str}T{time_str}")
    return dt

def _apply_datetime(world: dict, dt: datetime.datetime):
    meta = world.setdefault("world_meta", {})
    meta["date"]        = dt.date().isoformat()
    meta["time"]        = dt.strftime("%H:%M")
    meta["day_of_week"] = WEEKDAYS[dt.weekday()]
    meta["season"]      = _get_season(dt.month)
    return world

def tick_hours(n: int = 1) -> dict:
    """推進 n 小時，回傳新的 world_state"""
    world = _load()
    dt    = _parse_datetime(world)
    dt   += datetime.timedelta(hours=n)
    world  = _apply_datetime(world, dt)

    # 更新 tick 計數
    meta = world.setdefault("world_meta", {})
    meta["tick"] = meta.get("tick", 0) + n

    # 時間推進後更新戶外場景溫度
    _sync_outdoor_scene(world)
    _save(world)
    return world

def tick_days(n: int = 1) -> dict:
    """推進 n 天，同時更新天氣"""
    world = _load()
    dt    = _parse_datetime(world)
    dt   += datetime.timedelta(days=n)
    world  = _apply_datetime(world, dt)

    meta = world.setdefault("world_meta", {})
    meta["tick"] = meta.get("tick", 0) + n * 24

    # 推進一天時自動更新天氣
    city = meta.get("location", {}).get("city", "Taipei")
    new_weather = fetch_weather(city)
    meta["weather"] = new_weather

    # 更新戶外場景
    _sync_outdoor_scene(world)
    _save(world)
    return world

def update_weather_only() -> dict:
    """只更新天氣，不動時間"""
    world = _load()
    meta  = world.setdefault("world_meta", {})
    city  = meta.get("location", {}).get("city", "Taipei")
    meta["weather"] = fetch_weather(city)
    _sync_outdoor_scene(world)
    _save(world)
    return world

def _sync_outdoor_scene(world: dict):
    """把天氣資料同步到戶外場景的描述"""
    weather = world.get("world_meta", {}).get("weather", {})
    if not weather:
        return
    scenes = world.setdefault("scenes", {})
    outdoor = scenes.setdefault("outdoor", {})
    temp    = weather.get("temperature_c", 20)
    cond    = weather.get("condition", "多雲")
    emoji   = weather.get("emoji", "🌡")
    desc    = weather.get("description", "")
    outdoor["temperature_c"] = temp
    outdoor["humidity_pct"]  = weather.get("humidity_pct", 60)
    outdoor["notes"]         = f"{emoji} {cond}，{temp}°C。{desc}"
    outdoor["smell"]         = _outdoor_smell(weather.get("condition_code",""))

def _outdoor_smell(code: str) -> str:
    smells = {
        "rain":  "雨後潮濕的氣息，柏油路濕透的味道",
        "snow":  "冷冽清新的空氣",
        "clear": "清新空氣，微帶花草香",
        "mist":  "潮濕的霧氣，能見度低",
        "fog":   "濃霧，潮濕陰冷",
    }
    return smells.get(code, "都市空氣")

# ══════════════════════════════════════════════════════════════
# AI 自動更新角色狀態（時間推進後呼叫）
# ══════════════════════════════════════════════════════════════

def ai_update_chars_after_tick(hours: int = 1) -> dict:
    """
    時間推進後，用 AI 更新每個角色的狀態
    回傳更新後的 world_state
    """
    world = _load()
    meta  = world.get("world_meta", {})
    chars_raw = _load_chars()

    api_key = _get_api_key("GROQ_API_KEY")
    if not api_key:
        # 沒有 API Key → 只做基本自動更新
        return _basic_auto_update(world, hours)

    try:
        import requests as req
        weather = meta.get("weather", {})
        time_str = meta.get("time", "?")
        date_str = meta.get("date", "?")

        for char in chars_raw:
            cid   = char["id"]
            state = world.get("character_states", {}).get(cid, {})

            prompt = f"""世界時間推進了 {hours} 小時。
現在是 {date_str} {time_str}，{meta.get('day_of_week','')}。
天氣：{weather.get('condition','')} {weather.get('temperature_c','')}°C，{weather.get('description','')}

角色：{char['name']}（{char.get('role','')}）
個性：{char.get('personality','')}
目前情緒：{state.get('emotion','')}（強度 {state.get('emotion_intensity',0.5)}）
目前位置：{state.get('location','')}
目前任務：{state.get('current_task','')}（進度 {state.get('task_progress',0)}）
衣物狀態：{json.dumps(state.get('outfit',{}), ensure_ascii=False)}
精力：{state.get('energy_level',0.5)}
壓力：{state.get('stress_level',0.5)}

時間推進 {hours} 小時後，請用 JSON 回覆這個角色的狀態變化（只回 JSON，無其他文字）：
{{
  "emotion": "...",
  "emotion_intensity": 0.0~1.0,
  "secondary_emotion": "...",
  "energy_level": 0.0~1.0,
  "stress_level": 0.0~1.0,
  "hunger": 0.0~1.0,
  "current_task": "...",
  "task_progress": 0.0~1.0,
  "holding": "...",
  "posture": "坐姿/站姿/走路/休息",
  "outfit_note": "衣物是否有變化（無變化就空字串）",
  "short_term_memory": ["...","..."],
  "private_notes": "..."
}}"""

            r = req.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"model": "llama-3.1-8b-instant",
                      "messages": [{"role":"user","content":prompt}],
                      "max_tokens": 512, "temperature": 0.7},
                timeout=30,
            )
            if not r.ok:
                continue

            raw = r.json()["choices"][0]["message"]["content"].strip()
            # 清理 markdown fence
            raw = raw.replace("```json","").replace("```","").strip()
            updates = json.loads(raw)

            # 套用更新
            if cid not in world["character_states"]:
                world["character_states"][cid] = {}
            st = world["character_states"][cid]
            for k in ["emotion","emotion_intensity","secondary_emotion",
                      "energy_level","stress_level","hunger",
                      "current_task","task_progress","holding",
                      "posture","short_term_memory","private_notes"]:
                if k in updates:
                    st[k] = updates[k]
            if updates.get("outfit_note"):
                st.setdefault("outfit", {})["note"] = updates["outfit_note"]

    except Exception as e:
        pass  # AI 更新失敗就保留原狀態

    _save(world)
    return world

def _basic_auto_update(world: dict, hours: int) -> dict:
    """無 AI 時的基本自動更新（規則式）"""
    states = world.get("character_states", {})
    for cid, st in states.items():
        # 飢餓感隨時間增加
        hunger = st.get("hunger", 0.3)
        st["hunger"] = min(1.0, hunger + hours * 0.1)
        # 精力：工作時間消耗，夜晚恢復
        hour = int(world.get("world_meta", {}).get("time", "09:00").split(":")[0])
        if 8 <= hour <= 18:
            st["energy_level"] = max(0.1, st.get("energy_level", 0.7) - hours * 0.05)
        else:
            st["energy_level"] = min(1.0, st.get("energy_level", 0.5) + hours * 0.1)
    _save(world)
    return world

def _load_chars() -> list:
    try:
        return json.loads(CHARS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

# ══════════════════════════════════════════════════════════════
# 世界狀態轉 AI Context（注入 system prompt 用）
# ══════════════════════════════════════════════════════════════

def build_world_context(char_id: str = None) -> str:
    """
    把世界狀態轉成給 AI 的 context 文字
    char_id: 指定角色時，加入該角色的精細狀態
    """
    world = _load()
    meta  = world.get("world_meta", {})
    weather = meta.get("weather", {})
    scenes  = world.get("scenes", {})
    lines   = []

    # 時間 & 天氣
    lines.append(f"【世界時間】{meta.get('date','')} {meta.get('time','')} {meta.get('day_of_week','')}，{meta.get('season','')}季")
    w_emoji = weather.get("emoji","🌡")
    lines.append(
        f"【天氣】{w_emoji} {weather.get('condition','')} "
        f"{weather.get('temperature_c','')}°C（體感 {weather.get('feels_like_c','')}°C），"
        f"濕度 {weather.get('humidity_pct','')}%，"
        f"氣壓 {weather.get('pressure_hpa','')}hPa，"
        f"風速 {weather.get('wind_speed_ms','')}m/s {weather.get('wind_direction','')}。"
        f"{weather.get('description','')}"
    )
    lines.append(f"【地點】{meta.get('location',{}).get('building','')}，{meta.get('location',{}).get('city','')} {meta.get('location',{}).get('floor','')}")
    lines.append(f"【世界氛圍】{meta.get('atmosphere','')}，正值 {meta.get('phase','')}")
    lines.append("")

    # 角色精細狀態
    if char_id:
        st     = world.get("character_states", {}).get(char_id, {})
        loc    = st.get("location", "辦公室")
        scene  = scenes.get(_loc_to_scene(loc), {})
        outfit = st.get("outfit", {})

        lines.append(f"【你目前的狀態】")
        lines.append(f"  位置：{loc}（{scene.get('lighting','?')}，{scene.get('temperature_c','?')}°C，{scene.get('noise_level','?')}）")
        lines.append(f"  場景：{scene.get('notes','')}")
        lines.append(f"  姿勢：{st.get('posture','站姿')}，朝向 {st.get('facing','south')}")
        lines.append(f"  情緒：{st.get('emotion','')}（強度 {st.get('emotion_intensity',0.5):.0%}）")
        if st.get("secondary_emotion"):
            lines.append(f"  次要情緒：{st.get('secondary_emotion')}")
        lines.append(f"  精力：{int(st.get('energy_level',0.5)*100)}%，壓力：{int(st.get('stress_level',0.5)*100)}%，飢餓：{int(st.get('hunger',0.3)*100)}%")
        if outfit:
            lines.append(f"  衣著：{outfit.get('style','')} — {outfit.get('top','')}，{outfit.get('bottom','')}（{outfit.get('condition','')}）")
            if outfit.get("note"):
                lines.append(f"  衣著備注：{outfit.get('note')}")
        if st.get("holding"):
            lines.append(f"  手持：{st.get('holding')}")
        if st.get("short_term_memory"):
            lines.append(f"  近期記憶：{'、'.join(st['short_term_memory'][:3])}")
        lines.append(f"  當前任務：{st.get('current_task','')}（進度 {int(st.get('task_progress',0)*100)}%）")
        if st.get("private_notes"):
            lines.append(f"  心裡想的：{st.get('private_notes')}")
        lines.append("")

        # 場景裡有什麼
        if scene.get("items"):
            lines.append(f"  場景物品：{'、'.join(scene['items'][:4])}")
        if scene.get("smell"):
            lines.append(f"  氣味：{scene.get('smell')}")

    return "\n".join(lines)

def _loc_to_scene(location: str) -> str:
    LOC_MAP = {
        "辦公室":"office","辦公區":"office","工作區":"office",
        "會議室":"meeting","會議區":"meeting",
        "休息區":"lounge","茶水間":"lounge",
        "大廳":"lobby","入口":"lobby",
        "機房":"server","伺服器室":"server",
        "執行長辦公室":"ceo","董事長室":"ceo",
        "戶外":"outdoor","室外":"outdoor",
    }
    return LOC_MAP.get(location, "office")

# ══════════════════════════════════════════════════════════════
# 場景狀態查詢
# ══════════════════════════════════════════════════════════════

def get_scene(scene_id: str) -> dict:
    return _load().get("scenes", {}).get(scene_id, {})

def get_all_scenes() -> dict:
    return _load().get("scenes", {})

def update_scene(scene_id: str, updates: dict) -> dict:
    world = _load()
    world.setdefault("scenes", {}).setdefault(scene_id, {}).update(updates)
    _save(world)
    return world

# ══════════════════════════════════════════════════════════════
# CLI 入口（給 PythonAnywhere Scheduled Task 用）
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "tick_hour":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        w = tick_hours(n)
        print(f"✅ 時間推進 {n}h → {w['world_meta']['date']} {w['world_meta']['time']}")

    elif cmd == "tick_day":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        w = tick_days(n)
        print(f"✅ 日期推進 {n}天 → {w['world_meta']['date']}")

    elif cmd == "weather":
        city = sys.argv[2] if len(sys.argv) > 2 else "Taipei"
        w = update_weather_only()
        wth = w.get("world_meta", {}).get("weather", {})
        print(f"✅ 天氣更新：{wth.get('emoji','')} {wth.get('condition','')} {wth.get('temperature_c','')}°C")

    elif cmd == "ai_update":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        ai_update_chars_after_tick(n)
        print("✅ AI 角色狀態更新完成")

    elif cmd == "context":
        char_id = sys.argv[2] if len(sys.argv) > 2 else None
        print(build_world_context(char_id))

    else:
        print("用法：python world_engine.py [tick_hour|tick_day|weather|ai_update|context] [參數]")