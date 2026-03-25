"""
database.py — SQLite 資料庫管理
涵蓋：聊天記錄、任務、事件歷史、自訂資料表
"""
import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "chatroom.db")

def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    """建立所有資料表（首次執行）"""
    conn = get_conn()
    c = conn.cursor()

    # ── 聊天記錄 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS chat_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT    NOT NULL DEFAULT 'default',
        role        TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        character_id TEXT,
        model_id    TEXT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id)")
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS chat_sessions (
        session_id  TEXT    PRIMARY KEY,
        title       TEXT    NOT NULL DEFAULT '新對話',
        char_id     TEXT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )""")

    # ── 任務 / 待辦清單 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT    NOT NULL,
        description TEXT,
        status      TEXT    NOT NULL DEFAULT 'todo',
        priority    TEXT    NOT NULL DEFAULT 'medium',
        assigned_to TEXT,
        due_date    TEXT,
        tags        TEXT    DEFAULT '[]',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )""")

    # ── 事件 / 決策歷史 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type   TEXT    NOT NULL DEFAULT 'general',
        title        TEXT    NOT NULL,
        description  TEXT,
        participants TEXT    DEFAULT '[]',
        outcome      TEXT,
        importance   TEXT    NOT NULL DEFAULT 'normal',
        created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")

    # ── 自訂資料表定義 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS custom_tables (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL UNIQUE,
        display_name TEXT   NOT NULL,
        fields      TEXT    NOT NULL DEFAULT '[]',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )""")

    # ── 自訂資料記錄 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS custom_records (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name  TEXT    NOT NULL,
        data        TEXT    NOT NULL DEFAULT '{}',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_records_table ON custom_records(table_name)")

    conn.commit()
    conn.close()

# ══════════════════════════════════════
# 聊天記錄
# ══════════════════════════════════════
def save_chat_message(session_id, role, content, character_id=None, model_id=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO chat_history (session_id,role,content,character_id,model_id) VALUES (?,?,?,?,?)",
        (session_id, role, content, character_id, model_id)
    )
    conn.commit()
    conn.close()

def get_chat_history(session_id="default", limit=100, offset=0):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_history WHERE session_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (session_id, limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]

def get_setting(key, default=""):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO settings (key, value, updated_at)
        VALUES (?, ?, datetime('now','localtime'))
    """, (key, value))
    conn.commit()
    conn.close()

def get_all_settings():
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def create_session(session_id, title="新對話", char_id=None):
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO chat_sessions (session_id, title, char_id, created_at, updated_at)
        VALUES (?, ?, ?, datetime('now','localtime'), datetime('now','localtime'))
    """, (session_id, title, char_id))
    conn.commit()
    conn.close()

def update_session_title(session_id, title):
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO chat_sessions (session_id, title, updated_at)
        VALUES (?, ?, datetime('now','localtime'))
    """, (session_id, title))
    conn.commit()
    conn.close()

def get_session(session_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM chat_sessions WHERE session_id=?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def list_chat_sessions():
    """列出所有對話（含還沒訊息的新 session）"""
    from datetime import datetime
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            s.session_id,
            COALESCE(s.title, '新對話') as title,
            COUNT(h.id) as msg_count,
            COALESCE(MAX(h.created_at), s.created_at) as last_at,
            s.char_id
        FROM chat_sessions s
        LEFT JOIN chat_history h ON s.session_id = h.session_id
        GROUP BY s.session_id
        ORDER BY last_at DESC, s.created_at DESC
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if not d.get("last_at"):
            d["last_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        result.append(d)
    return result

def delete_chat_session(session_id):
    conn = get_conn()
    conn.execute("DELETE FROM chat_history WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM chat_sessions WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()

# ══════════════════════════════════════
# 任務管理
# ══════════════════════════════════════
def create_task(title, description="", priority="medium", assigned_to=None, due_date=None, tags=None):
    conn = get_conn()
    c = conn.execute(
        "INSERT INTO tasks (title,description,priority,assigned_to,due_date,tags) VALUES (?,?,?,?,?,?)",
        (title, description, priority, assigned_to, due_date, json.dumps(tags or []))
    )
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def get_tasks(status=None, assigned_to=None):
    conn = get_conn()
    q = "SELECT * FROM tasks WHERE 1=1"
    params = []
    if status:
        q += " AND status=?"; params.append(status)
    if assigned_to:
        q += " AND assigned_to=?"; params.append(assigned_to)
    q += " ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        result.append(d)
    return result

def update_task(task_id, **kwargs):
    if not kwargs:
        return
    if "tags" in kwargs:
        kwargs["tags"] = json.dumps(kwargs["tags"])
    kwargs["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [task_id]
    conn = get_conn()
    conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()

def delete_task(task_id):
    conn = get_conn()
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

# ══════════════════════════════════════
# 事件歷史
# ══════════════════════════════════════
def add_event(title, description="", event_type="general", participants=None, outcome="", importance="normal"):
    conn = get_conn()
    c = conn.execute(
        "INSERT INTO events (title,description,event_type,participants,outcome,importance) VALUES (?,?,?,?,?,?)",
        (title, description, event_type, json.dumps(participants or []), outcome, importance)
    )
    eid = c.lastrowid
    conn.commit()
    conn.close()
    return eid

def get_events(event_type=None, limit=50):
    conn = get_conn()
    q = "SELECT * FROM events WHERE 1=1"
    params = []
    if event_type:
        q += " AND event_type=?"; params.append(event_type)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["participants"] = json.loads(d.get("participants") or "[]")
        result.append(d)
    return result

def delete_event(event_id):
    conn = get_conn()
    conn.execute("DELETE FROM events WHERE id=?", (event_id,))
    conn.commit()
    conn.close()

# ══════════════════════════════════════
# 自訂資料表
# ══════════════════════════════════════
def create_custom_table(name, display_name, fields):
    """
    fields 格式：[{"name":"title","type":"text","label":"標題"}, ...]
    type 可以是：text / number / date / select / textarea
    """
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO custom_tables (name,display_name,fields) VALUES (?,?,?)",
            (name, display_name, json.dumps(fields))
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise ValueError(f"資料表 {name} 已存在")
    conn.close()

def list_custom_tables():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM custom_tables ORDER BY created_at").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["fields"] = json.loads(d.get("fields") or "[]")
        # 計算記錄數
        cnt = get_conn().execute(
            "SELECT COUNT(*) FROM custom_records WHERE table_name=?", (d["name"],)
        ).fetchone()[0]
        d["record_count"] = cnt
        result.append(d)
    return result

def delete_custom_table(name):
    conn = get_conn()
    conn.execute("DELETE FROM custom_tables WHERE name=?", (name,))
    conn.execute("DELETE FROM custom_records WHERE table_name=?", (name,))
    conn.commit()
    conn.close()

def add_record(table_name, data):
    conn = get_conn()
    c = conn.execute(
        "INSERT INTO custom_records (table_name,data) VALUES (?,?)",
        (table_name, json.dumps(data, ensure_ascii=False))
    )
    rid = c.lastrowid
    conn.commit()
    conn.close()
    return rid

def get_records(table_name, search=None, limit=100, offset=0):
    conn = get_conn()
    if search:
        rows = conn.execute(
            "SELECT * FROM custom_records WHERE table_name=? AND data LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (table_name, f"%{search}%", limit, offset)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM custom_records WHERE table_name=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (table_name, limit, offset)
        ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d.get("data") or "{}")
        result.append(d)
    return result

def update_record(record_id, data):
    conn = get_conn()
    conn.execute(
        "UPDATE custom_records SET data=?, updated_at=datetime('now','localtime') WHERE id=?",
        (json.dumps(data, ensure_ascii=False), record_id)
    )
    conn.commit()
    conn.close()

def delete_record(record_id):
    conn = get_conn()
    conn.execute("DELETE FROM custom_records WHERE id=?", (record_id,))
    conn.commit()
    conn.close()

def get_db_stats():
    conn = get_conn()
    stats = {
        "chat_messages": conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0],
        "tasks_total":   conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        "tasks_todo":    conn.execute("SELECT COUNT(*) FROM tasks WHERE status='todo'").fetchone()[0],
        "events_total":  conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "custom_tables": conn.execute("SELECT COUNT(*) FROM custom_tables").fetchone()[0],
        "custom_records":conn.execute("SELECT COUNT(*) FROM custom_records").fetchone()[0],
    }
    conn.close()
    return stats

# 初始化
init_db()