"""
kb_manager.py — 知識庫管理
分類：SOP、角色設定、會議記錄、筆記、其他
支援：全文搜尋 + TF-IDF 語意搜尋 + 標籤
"""
import sqlite3
import json
import os
import math
import re
from datetime import datetime
from collections import Counter

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "chatroom.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_kb():
    conn = get_conn()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS kb_docs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        category    TEXT    NOT NULL DEFAULT 'note',
        tags        TEXT    DEFAULT '[]',
        source      TEXT,
        is_active   INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_category ON kb_docs(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_active   ON kb_docs(is_active)")
    conn.commit()
    conn.close()

# ── 分類定義 ──
CATEGORIES = {
    "sop":      {"label": "SOP / 流程",   "icon": "📋"},
    "character":{"label": "角色 / 世界觀", "icon": "🎭"},
    "meeting":  {"label": "會議 / 決策",   "icon": "🗣"},
    "note":     {"label": "筆記 / 文章",   "icon": "📝"},
    "other":    {"label": "其他",          "icon": "📁"},
}

# ══════════════════════════════════════
# CRUD
# ══════════════════════════════════════
def add_doc(title, content, category="note", tags=None, source=None):
    conn = get_conn()
    c = conn.execute(
        "INSERT INTO kb_docs (title,content,category,tags,source) VALUES (?,?,?,?,?)",
        (title, content, category, json.dumps(tags or []), source)
    )
    doc_id = c.lastrowid
    conn.commit()
    conn.close()
    return doc_id

def update_doc(doc_id, **kwargs):
    if "tags" in kwargs:
        kwargs["tags"] = json.dumps(kwargs["tags"])
    kwargs["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [doc_id]
    conn = get_conn()
    conn.execute(f"UPDATE kb_docs SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()

def delete_doc(doc_id):
    conn = get_conn()
    conn.execute("DELETE FROM kb_docs WHERE id=?", (doc_id,))
    conn.commit()
    conn.close()

def get_doc(doc_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM kb_docs WHERE id=?", (doc_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    return d

def list_docs(category=None, active_only=True, limit=200):
    conn = get_conn()
    q = "SELECT id,title,category,tags,source,is_active,created_at,updated_at, substr(content,1,120) as preview FROM kb_docs WHERE 1=1"
    params = []
    if active_only:
        q += " AND is_active=1"
    if category:
        q += " AND category=?"; params.append(category)
    q += " ORDER BY category, updated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["category_info"] = CATEGORIES.get(d["category"], CATEGORIES["other"])
        result.append(d)
    return result

def get_kb_stats():
    conn = get_conn()
    rows = conn.execute("""
        SELECT category, COUNT(*) as cnt
        FROM kb_docs WHERE is_active=1
        GROUP BY category
    """).fetchall()
    conn.close()
    stats = {cat: 0 for cat in CATEGORIES}
    for r in rows:
        stats[r["category"]] = r["cnt"]
    stats["total"] = sum(stats.values())
    return stats

# ══════════════════════════════════════
# 全文搜尋
# ══════════════════════════════════════
def fulltext_search(query, category=None, limit=10):
    conn = get_conn()
    q = """
        SELECT id, title, category, tags, created_at,
               substr(content,1,200) as preview,
               (instr(lower(title), lower(?)) > 0) as title_match
        FROM kb_docs WHERE is_active=1
        AND (lower(title) LIKE lower(?) OR lower(content) LIKE lower(?))
    """
    params = [query, f"%{query}%", f"%{query}%"]
    if category:
        q += " AND category=?"; params.append(category)
    q += " ORDER BY title_match DESC, updated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["category_info"] = CATEGORIES.get(d["category"], CATEGORIES["other"])
        result.append(d)
    return result

# ══════════════════════════════════════
# TF-IDF 語意搜尋（供 AI 使用）
# ══════════════════════════════════════
def _tokenize(text):
    """中英文分詞（簡易版）"""
    text = text.lower()
    # 英文單詞
    words = re.findall(r'[a-z0-9]+', text)
    # 中文 2-gram
    cjk = re.findall(r'[\u4e00-\u9fff]', text)
    bigrams = [''.join(cjk[i:i+2]) for i in range(len(cjk)-1)]
    return words + cjk + bigrams

def _tfidf_score(query_tokens, doc_tokens):
    if not doc_tokens:
        return 0.0
    doc_freq = Counter(doc_tokens)
    total = len(doc_tokens)
    score = 0.0
    for tok in query_tokens:
        tf = doc_freq.get(tok, 0) / total
        score += tf
    return score

def semantic_search(query, category=None, top_k=5, active_only=True):
    """給 AI 使用的語意搜尋，回傳最相關的段落"""
    conn = get_conn()
    q = "SELECT id, title, content, category FROM kb_docs WHERE is_active=1"
    params = []
    if category:
        q += " AND category=?"; params.append(category)
    rows = conn.execute(q, params).fetchall()
    conn.close()

    if not rows:
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    scored = []
    for row in rows:
        doc_tokens = _tokenize(row["title"] * 3 + " " + row["content"])
        score = _tfidf_score(query_tokens, doc_tokens)
        if score > 0:
            # 切成段落
            chunks = _chunk_text(row["content"], 400)
            best_chunk = ""
            best_score = 0
            for chunk in chunks:
                chunk_score = _tfidf_score(query_tokens, _tokenize(chunk))
                if chunk_score > best_score:
                    best_score = chunk_score
                    best_chunk = chunk
            scored.append({
                "id": row["id"],
                "title": row["title"],
                "category": row["category"],
                "chunk": best_chunk or row["content"][:400],
                "score": score,
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]

def _chunk_text(text, size=400):
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    chunks, cur = [], ""
    for para in paragraphs:
        if len(cur) + len(para) < size:
            cur += para + "\n"
        else:
            if cur:
                chunks.append(cur.strip())
            cur = para + "\n"
    if cur:
        chunks.append(cur.strip())
    return chunks or [text[:size]]

def build_kb_context(query, category=None, top_k=5):
    """組合給 AI 的知識庫背景"""
    results = semantic_search(query, category=category, top_k=top_k)
    if not results:
        return ""
    lines = ["【知識庫相關資料】"]
    for r in results:
        cat_info = CATEGORIES.get(r["category"], CATEGORIES["other"])
        lines.append(f"\n▍{cat_info['icon']} {r['title']}（{cat_info['label']}）")
        lines.append(r["chunk"])
    return "\n".join(lines)

# ── 快速匯入 ──
def import_from_text(text, title, category="note", tags=None):
    """直接把純文字存入知識庫"""
    return add_doc(title, text, category, tags)

def import_from_world_state(world_state_path):
    """把現有 world_state.json 匯入知識庫"""
    try:
        with open(world_state_path, 'r', encoding='utf-8') as f:
            ws = json.load(f)
    except Exception:
        return 0

    count = 0
    # 世界元資料
    meta = ws.get("meta", {})
    if meta:
        content = f"世界名稱：{meta.get('world_name','')}\n"
        content += f"當前日期：{meta.get('current_date','')}\n"
        content += f"氛圍：{meta.get('atmosphere','')}\n"
        content += f"背景：{meta.get('background','')}"
        add_doc("世界觀設定", content, "character", ["世界觀", "設定"])
        count += 1

    # 事件歷史
    for evt in ws.get("event_history", [])[:20]:
        content = f"時間：{evt.get('timestamp','')}\n"
        content += f"類型：{evt.get('type','')}\n"
        content += f"描述：{evt.get('description','')}\n"
        if evt.get('outcome'):
            content += f"結果：{evt.get('outcome','')}"
        add_doc(evt.get("title", "事件記錄"), content, "meeting", ["事件", "歷史"])
        count += 1

    return count

# 初始化
init_kb()