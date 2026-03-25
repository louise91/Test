# routes_kb.py — 知識庫 / 資料庫
import os, json, time, base64, requests, math, re, threading
from flask import request, Response, render_template, session, jsonify
from ai_utils import *

def register_kb_routes(app):
    @app.route("/ai/kb/categories", methods=["GET"])
    def kb_categories():
        from kb_manager import CATEGORIES, get_kb_stats
        stats = get_kb_stats()
        result = []
        for key, info in CATEGORIES.items():
            result.append({"id": key, "label": info["label"], "icon": info["icon"], "count": stats.get(key, 0)})
        result.append({"id": "all", "label": "全部", "icon": "📚", "count": stats.get("total", 0)})
        return jsonify(result)

    @app.route("/ai/kb/docs", methods=["GET"])
    def kb_list():
        from kb_manager import list_docs
        category = request.args.get("category")
        if category == "all":
            category = None
        docs = list_docs(category=category)
        return jsonify(docs)

    @app.route("/ai/kb/docs", methods=["POST"])
    def kb_add():
        from kb_manager import add_doc
        data = request.get_json() or {}
        title    = (data.get("title") or "").strip()
        content  = (data.get("content") or "").strip()
        category = data.get("category", "note")
        tags     = data.get("tags", [])
        source   = data.get("source", "")
        if not title or not content:
            return jsonify({"error": "標題和內容不能空白"}), 400
        doc_id = add_doc(title, content, category, tags, source)
        return jsonify({"id": doc_id, "message": "已新增"})

    @app.route("/ai/kb/docs/<int:doc_id>", methods=["GET"])
    def kb_get(doc_id):
        from kb_manager import get_doc
        doc = get_doc(doc_id)
        if not doc:
            return jsonify({"error": "找不到"}), 404
        return jsonify(doc)

    @app.route("/ai/kb/docs/<int:doc_id>", methods=["PUT"])
    def kb_update(doc_id):
        from kb_manager import update_doc
        data = request.get_json() or {}
        allowed = ["title", "content", "category", "tags", "is_active"]
        kwargs = {k: data[k] for k in allowed if k in data}
        update_doc(doc_id, **kwargs)
        return jsonify({"message": "已更新"})

    @app.route("/ai/kb/docs/<int:doc_id>", methods=["DELETE"])
    def kb_delete(doc_id):
        from kb_manager import delete_doc
        delete_doc(doc_id)
        return jsonify({"message": "已刪除"})

    @app.route("/ai/kb/search", methods=["GET"])
    def kb_search():
        from kb_manager import fulltext_search
        q = request.args.get("q", "").strip()
        category = request.args.get("category")
        if category == "all":
            category = None
        if not q:
            return jsonify([])
        results = fulltext_search(q, category=category)
        return jsonify(results)

    @app.route("/ai/kb/upload", methods=["POST"])
    def kb_upload():
        from kb_manager import add_doc
        f = request.files.get("file")
        category = request.form.get("category", "note")
        tags_str = request.form.get("tags", "[]")
        try:
            tags = json.loads(tags_str)
        except Exception:
            tags = []
        if not f:
            return jsonify({"error": "沒有檔案"}), 400
        fname = f.filename or "文件"
        try:
            content = f.read().decode("utf-8", errors="ignore")
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        title = os.path.splitext(fname)[0]
        doc_id = add_doc(title, content, category, tags, source=fname)
        return jsonify({"id": doc_id, "message": f"{fname} 已匯入知識庫"})

    @app.route("/ai/kb/import_world", methods=["POST"])
    def kb_import_world():
        from kb_manager import import_from_world_state
        ws_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "world_state.json")
        count = import_from_world_state(ws_path)
        return jsonify({"message": f"已從 world_state.json 匯入 {count} 筆資料"})

    # ══════════════════════════════════════════════════════════════
    # 資料庫路由 /ai/db/*
    # ══════════════════════════════════════════════════════════════

    @app.route("/ai/db/stats", methods=["GET"])
    def db_stats():
        from database import get_db_stats
        return jsonify(get_db_stats())

    # ── 聊天記錄 ──
    @app.route("/ai/db/chat", methods=["GET"])
    def db_chat_list():
        from database import get_chat_history, list_chat_sessions
        session_id = request.args.get("session", "default")
        limit = int(request.args.get("limit", 50))
        if session_id == "__sessions__":
            return jsonify(list_chat_sessions())
        return jsonify(get_chat_history(session_id, limit=limit))

    @app.route("/ai/db/chat/<session_id>", methods=["DELETE"])
    def db_chat_delete(session_id):
        from database import delete_chat_session
        delete_chat_session(session_id)
        return jsonify({"message": f"已刪除 {session_id} 的聊天記錄"})

    # ── 任務 ──
    @app.route("/ai/db/tasks", methods=["GET"])
    def db_tasks_list():
        from database import get_tasks
        status = request.args.get("status")
        assigned_to = request.args.get("assigned_to")
        return jsonify(get_tasks(status=status, assigned_to=assigned_to))

    @app.route("/ai/db/tasks", methods=["POST"])
    def db_tasks_create():
        from database import create_task
        data = request.get_json() or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "標題不能空白"}), 400
        tid = create_task(
            title=title,
            description=data.get("description", ""),
            priority=data.get("priority", "medium"),
            assigned_to=data.get("assigned_to"),
            due_date=data.get("due_date"),
            tags=data.get("tags", []),
        )
        return jsonify({"id": tid, "message": "已新增任務"})

    @app.route("/ai/db/tasks/<int:task_id>", methods=["PUT"])
    def db_tasks_update(task_id):
        from database import update_task
        data = request.get_json() or {}
        allowed = ["title", "description", "status", "priority", "assigned_to", "due_date", "tags"]
        kwargs = {k: data[k] for k in allowed if k in data}
        update_task(task_id, **kwargs)
        return jsonify({"message": "已更新"})

    @app.route("/ai/db/tasks/<int:task_id>", methods=["DELETE"])
    def db_tasks_delete(task_id):
        from database import delete_task
        delete_task(task_id)
        return jsonify({"message": "已刪除"})

    # ── 事件歷史 ──
    @app.route("/ai/db/events", methods=["GET"])
    def db_events_list():
        from database import get_events
        event_type = request.args.get("type")
        limit = int(request.args.get("limit", 50))
        return jsonify(get_events(event_type=event_type, limit=limit))

    @app.route("/ai/db/events", methods=["POST"])
    def db_events_create():
        from database import add_event
        data = request.get_json() or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "標題不能空白"}), 400
        eid = add_event(
            title=title,
            description=data.get("description", ""),
            event_type=data.get("event_type", "general"),
            participants=data.get("participants", []),
            outcome=data.get("outcome", ""),
            importance=data.get("importance", "normal"),
        )
        return jsonify({"id": eid, "message": "已記錄事件"})

    @app.route("/ai/db/events/<int:event_id>", methods=["DELETE"])
    def db_events_delete(event_id):
        from database import delete_event
        delete_event(event_id)
        return jsonify({"message": "已刪除"})

    # ── 自訂資料表 ──
    @app.route("/ai/db/tables", methods=["GET"])
    def db_tables_list():
        from database import list_custom_tables
        return jsonify(list_custom_tables())

    @app.route("/ai/db/tables", methods=["POST"])
    def db_tables_create():
        from database import create_custom_table
        data = request.get_json() or {}
        name         = (data.get("name") or "").strip().replace(" ", "_")
        display_name = (data.get("display_name") or name).strip()
        fields       = data.get("fields", [])
        if not name:
            return jsonify({"error": "資料表名稱不能空白"}), 400
        if not fields:
            return jsonify({"error": "至少需要一個欄位"}), 400
        try:
            create_custom_table(name, display_name, fields)
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify({"message": f"資料表 {display_name} 已建立"})

    @app.route("/ai/db/tables/<name>", methods=["DELETE"])
    def db_tables_delete(name):
        from database import delete_custom_table
        delete_custom_table(name)
        return jsonify({"message": "已刪除"})

    @app.route("/ai/db/tables/<name>/records", methods=["GET"])
    def db_records_list(name):
        from database import get_records
        search = request.args.get("q")
        limit  = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        return jsonify(get_records(name, search=search, limit=limit, offset=offset))

    @app.route("/ai/db/tables/<name>/records", methods=["POST"])
    def db_records_create(name):
        from database import add_record
        data = request.get_json() or {}
        rid = add_record(name, data)
        return jsonify({"id": rid, "message": "已新增"})

    @app.route("/ai/db/records/<int:record_id>", methods=["PUT"])
    def db_records_update(record_id):
        from database import update_record
        data = request.get_json() or {}
        update_record(record_id, data)
        return jsonify({"message": "已更新"})

    @app.route("/ai/db/records/<int:record_id>", methods=["DELETE"])
    def db_records_delete(record_id):
        from database import delete_record
        delete_record(record_id)
        return jsonify({"message": "已刪除"})