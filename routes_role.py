# routes_role.py — 角色 / RAG / 模型 / 劇本角色 / 世界狀態 / 代理 / 工作流程
import os, json, time, base64, requests, math, re, threading
from flask import request, Response, render_template, session, jsonify
from ai_utils import *

def register_role_routes(app):
    @app.route("/ai/reset", methods=["POST"])
    def ai_reset():
        sid = session.get("sid") or request.remote_addr or "default"
        try:
            from database import delete_chat_session
            delete_chat_session(sid)
        except Exception:
            pass
        return jsonify({"status":"ok"})

    # ══════════════════════════════
    # 角色 API
    # ══════════════════════════════
    @app.route("/ai/roles", methods=["GET"])
    def ai_roles_get():
        return jsonify(load_roles())

    @app.route("/ai/roles", methods=["POST"])
    def ai_roles_save():
        data   = request.get_json() or {}
        name   = (data.get("name") or "").strip()
        prompt = (data.get("prompt") or "").strip()
        if not name or not prompt:
            return jsonify({"error":"name 和 prompt 必填"}), 400
        roles  = load_roles()
        new_id = f"role_{int(time.time())}"
        roles.append({"id":new_id,"name":name,"prompt":prompt})
        save_roles(roles)
        return jsonify({"status":"ok","id":new_id})

    @app.route("/ai/roles/<role_id>", methods=["DELETE"])
    def ai_roles_delete(role_id):
        if role_id in ("default","translator","coder","critic"):
            return jsonify({"error":"預設角色不能刪除"}), 400
        save_roles([r for r in load_roles() if r["id"] != role_id])
        return jsonify({"status":"ok"})

    # ══════════════════════════════
    # RAG 文件 API
    # ══════════════════════════════
    @app.route("/ai/rag/docs", methods=["GET"])
    def rag_docs_list():
        return jsonify(load_rag_index())

    @app.route("/ai/rag/upload", methods=["POST"])
    def rag_upload():
        file_obj = request.files.get("file")
        if not file_obj or not file_obj.filename:
            return jsonify({"error":"沒有檔案"}), 400
        fname = file_obj.filename.lower()
        if not fname.endswith((".txt",".md",".csv",".json",".py",".js",".html",".css")):
            return jsonify({"error":"不支援的格式"}), 415
        raw = file_obj.read(5 * 1024 * 1024)  # 最大 5MB
        text = raw.decode("utf-8", errors="replace")
        
        doc_id   = f"doc_{int(time.time())}"
        filename = f"{doc_id}.txt"
        path     = os.path.join(RAG_DIR, filename)
        with open(path,"w",encoding="utf-8") as f:
            f.write(text)
        
        index = load_rag_index()
        index.append({
            "id":       doc_id,
            "name":     file_obj.filename,
            "filename": filename,
            "size":     len(text),
            "chunks":   len(chunk_text(text)),
            "uploaded": time.strftime("%Y-%m-%d %H:%M")
        })
        save_rag_index(index)
        return jsonify({"status":"ok","id":doc_id,"name":file_obj.filename})

    @app.route("/ai/rag/docs/<doc_id>", methods=["DELETE"])
    def rag_doc_delete(doc_id):
        index = load_rag_index()
        doc   = next((d for d in index if d["id"]==doc_id), None)
        if doc:
            try:
                os.remove(os.path.join(RAG_DIR, doc["filename"]))
            except Exception:
                pass
        save_rag_index([d for d in index if d["id"] != doc_id])
        return jsonify({"status":"ok"})

    # ── 真多代理（並行，獨立記憶）──
    @app.route("/ai/true_multi", methods=["POST"])
    def ai_true_multi():
        from true_multi_agent import run_discussion, clear_all_memories
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        if ai_rate_limited(ip, limit=3):
            return Response("請求太頻繁", status=429)
        key = _get_key("GROQ_API_KEY")
        if not key:
            return Response("[錯誤] 未設定 GROQ_API_KEY，請到設定頁面填入", status=500)

        data             = request.get_json() or {}
        text             = (data.get("message") or "").strip()
        role_ids         = data.get("role_ids", [])
        rag_doc_ids      = data.get("rag_doc_ids", [])
        do_search        = data.get("search", False)
        do_reset         = data.get("reset_memory", False)
        moderator_prompt = (data.get("moderator_prompt") or
            "你是客觀的主持人，擅長整合不同觀點，給出平衡而有深度的總結。用繁體中文回覆。")

        if not text:
            return Response("[錯誤] 訊息不能為空", status=400)
        if len(role_ids) < 2:
            return Response("[錯誤] 至少選 2 個角色", status=400)

        from world_manager import load_characters, build_character_context
        import threading as _threading

        # ── 策略 C：並行感知，各角色各自建立世界感知 ──
        all_chars = {c["id"]: c for c in load_characters()}
        all_roles = {r["id"]: r for r in load_roles()}

        # 把劇本角色加進 all_roles（未加的才加）
        for c in all_chars.values():
            if c["id"] not in all_roles:
                all_roles[c["id"]] = c

        agents_raw = [all_roles[rid] for rid in role_ids if rid in all_roles]
        if len(agents_raw) < 2:
            return Response("[錯誤] 找不到指定角色", status=400)

        if do_reset:
            clear_all_memories(role_ids)

        # 並行執行每個角色的感知（策略 C），壓縮等待時間
        perception_map = {}  # char_id → perception_result
        _perception_errors = {}

        def _perceive(char_id):
            try:
                from world_context_builder import build_perception, perception_to_system_prompt
                result = build_perception(char_id)
                perception_map[char_id] = {
                    "system_prompt": perception_to_system_prompt(char_id, result),
                    "perception":    result.get("perception",""),
                    "facts":         result.get("relevant_facts",[]),
                    "strategy":      result.get("strategy",""),
                }
            except Exception as e:
                _perception_errors[char_id] = str(e)
                # fallback：舊的 context
                try:
                    ctx = build_character_context(char_id)
                    sp  = all_roles.get(char_id,{}).get("system_prompt","") or ""
                    perception_map[char_id] = {
                        "system_prompt": sp + ("\n\n" + ctx if ctx else ""),
                        "perception":    "",
                        "facts":         [],
                        "strategy":      "fallback",
                    }
                except Exception:
                    perception_map[char_id] = {
                        "system_prompt": all_roles.get(char_id,{}).get("system_prompt",""),
                        "perception": "", "facts": [], "strategy": "error",
                    }

        # 只對劇本角色做感知；AI 助手角色維持原 prompt
        threads = []
        for agent in agents_raw:
            aid = agent["id"]
            if aid in all_chars:   # 劇本角色
                t = _threading.Thread(target=_perceive, args=(aid,), daemon=True)
                t.start()
                threads.append(t)
            else:                  # AI 助手角色：直接用 system_prompt
                perception_map[aid] = {
                    "system_prompt": agent.get("prompt", agent.get("system_prompt","")),
                    "perception": "", "facts": [], "strategy": "assistant",
                }

        # 等所有感知完成（最多 12 秒）
        for t in threads:
            t.join(timeout=12)

        # 組裝 agents（帶感知後的 prompt）
        agents = []
        for agent in agents_raw:
            aid  = agent["id"]
            perc = perception_map.get(aid, {})
            agents.append({
                "id":     aid,
                "name":   agent.get("name", aid),
                "prompt": perc.get("system_prompt", agent.get("prompt", agent.get("system_prompt",""))),
                # 把感知摘要加進 agent 物件，讓前端可以顯示
                "_perception": perc.get("perception",""),
                "_facts":      perc.get("facts",[]),
                "_strategy":   perc.get("strategy",""),
            })

        # RAG / 搜尋 context（共用，不重複感知）
        context_parts = []
        if rag_doc_ids:
            rc = rag_search(text, rag_doc_ids)
            if rc: context_parts.append(rc)
        if do_search:
            sr = web_search(text)
            if sr: context_parts.append(f"網路搜尋結果：\n{sr}")
        context = "\n\n".join(context_parts)

        result_queue = run_discussion(
            agents, text, context,
            moderator_prompt=moderator_prompt,
            num_rounds=2
        )

        def generate():
            while True:
                try:
                    event = result_queue.get(timeout=120)
                except Exception:
                    yield "data: {\"type\":\"error\",\"text\":\"超時\"}\n\n".encode("utf-8")
                    break
                payload = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode("utf-8")
                if event["type"] == "all_done":
                    break

        return Response(generate(), mimetype="text/event-stream",
                        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    # 清除代理記憶
    @app.route("/ai/true_multi/reset", methods=["POST"])
    def ai_true_multi_reset():
        from true_multi_agent import clear_all_memories
        data = request.get_json() or {}
        role_ids = data.get("role_ids", [])
        clear_all_memories(role_ids)
        return jsonify({"status": "ok"})

    @app.route("/ai/rate_status", methods=["GET"])
    def ai_rate_status():
        from true_multi_agent import get_rate_status
        return jsonify(get_rate_status())

    @app.route("/ai/models", methods=["GET"])
    def ai_models():
        return jsonify(all_models())

    @app.route("/ai/models", methods=["POST"])
    def ai_models_add():
        data = request.get_json() or {}
        mid  = (data.get("id") or "").strip()
        name = (data.get("name") or "").strip()
        tag  = (data.get("tag") or "自訂").strip()
        if not mid or not name:
            return jsonify({"error": "id 和 name 必填"}), 400
        custom = load_custom_models()
        # 檢查是否已存在（內建或自訂）
        all_ids = {m["id"] for m in all_models()}
        if mid in all_ids:
            return jsonify({"error": "此模型 ID 已存在"}), 409
        new_model = {
            "id":      mid,
            "name":    name,
            "tag":     tag,
            "vision":  tag == "視覺",
            "builtin": False
        }
        custom.append(new_model)
        save_custom_models(custom)
        return jsonify({"status": "ok", "model": new_model})

    @app.route("/ai/models/<path:model_id>", methods=["DELETE"])
    def ai_models_delete(model_id):
        custom = load_custom_models()
        # 不能刪內建模型
        if any(m["id"] == model_id and m.get("builtin") for m in AVAILABLE_MODELS):
            return jsonify({"error": "不能刪除內建模型"}), 403
        new_custom = [m for m in custom if m["id"] != model_id]
        if len(new_custom) == len(custom):
            return jsonify({"error": "找不到此模型"}), 404
        save_custom_models(new_custom)
        return jsonify({"status": "ok"})

    # ══════════════════════════════════════
    # 角色（characters.json）API
    # ══════════════════════════════════════
    @app.route("/ai/characters", methods=["GET"])
    def characters_list():
        from world_manager import load_characters
        return jsonify(load_characters())

    @app.route("/ai/characters", methods=["POST"])
    def characters_add():
        from world_manager import load_characters, save_characters
        data = request.get_json() or {}
        name   = (data.get("name") or "").strip()
        role   = (data.get("role") or "").strip()
        prompt = (data.get("system_prompt") or "").strip()
        if not name or not prompt:
            return jsonify({"error": "name 和 system_prompt 必填"}), 400
        chars  = load_characters()
        new_id = f"char_{int(time.time())}"
        chars.append({
            "id":            new_id,
            "name":          name,
            "role":          role,
            "personality":   data.get("personality", ""),
            "system_prompt": prompt,
            "tags":          data.get("tags", []),
            "avatar":        name[0]
        })
        save_characters(chars)
        return jsonify({"status": "ok", "id": new_id})

    @app.route("/ai/characters/<char_id>", methods=["PUT"])
    def characters_update(char_id):
        from world_manager import load_characters, save_characters
        data  = request.get_json() or {}
        chars = load_characters()
        for c in chars:
            if c["id"] == char_id:
                for k in ("name","role","personality","system_prompt","tags"):
                    if k in data: c[k] = data[k]
                if c.get("name"): c["avatar"] = c["name"][0]
                break
        save_characters(chars)
        return jsonify({"status": "ok"})

    @app.route("/ai/characters/<char_id>", methods=["DELETE"])
    def characters_delete(char_id):
        from world_manager import load_characters, save_characters
        save_characters([c for c in load_characters() if c["id"] != char_id])
        return jsonify({"status": "ok"})

    # ══════════════════════════════════════
    # 世界狀態 API
    # ══════════════════════════════════════
    @app.route("/ai/world", methods=["GET"])
    def world_get():
        from world_manager import load_world
        return jsonify(load_world())

    @app.route("/ai/world/meta", methods=["PUT"])
    def world_meta_update():
        from world_manager import load_world, save_world
        world = load_world()
        world["world_meta"].update(request.get_json() or {})
        save_world(world)
        return jsonify({"status": "ok"})

    @app.route("/ai/world/state/<char_id>", methods=["PUT"])
    def world_state_update(char_id):
        from world_manager import update_character_state
        update_character_state(char_id, request.get_json() or {})
        return jsonify({"status": "ok"})

    @app.route("/ai/world/event", methods=["POST"])
    def world_event_add():
        from world_manager import add_event
        add_event(request.get_json() or {})
        return jsonify({"status": "ok"})

    # ══════════════════════════════════════
    # 取得角色背景（供前端注入 prompt）
    # ══════════════════════════════════════
    @app.route("/ai/characters/<char_id>/context", methods=["GET"])
    def character_context(char_id):
        from world_manager import build_character_context
        return jsonify({"context": build_character_context(char_id)})

    # ══════════════════════════════════════
    # 自主代理 API
    # ══════════════════════════════════════

    @app.route("/ai/agent/run/<char_id>", methods=["POST"])
    def agent_run(char_id):
        """手動觸發單一代理行動迴圈"""
        from agent_engine import run_agent_cycle
        data    = request.get_json() or {}
        trigger = data.get("trigger", "手動")
        result  = run_agent_cycle(char_id, trigger=trigger)
        return jsonify(result)

    @app.route("/ai/agent/meeting", methods=["POST"])
    def agent_meeting():
        """手動觸發會議"""
        from agent_engine import run_meeting
        data     = request.get_json() or {}
        char_ids = data.get("char_ids", [])
        topic    = (data.get("topic") or "").strip()
        if not topic:
            return jsonify({"error": "需要會議主題"}), 400
        if len(char_ids) < 2:
            return jsonify({"error": "需要至少 2 個角色"}), 400
        result = run_meeting(char_ids, topic, trigger="手動")
        return jsonify(result)

    @app.route("/ai/agent/daily", methods=["POST"])
    def agent_daily():
        """手動觸發所有代理每日循環（非同步，立即回傳）"""
        from agent_engine import run_daily_cycle
        import threading
        threading.Thread(
            target=run_daily_cycle,
            kwargs={"trigger": "手動每日"},
            daemon=True
        ).start()
        return jsonify({"status": "ok", "msg": "每日循環已啟動，請查看日誌"})

    @app.route("/ai/agent/log", methods=["GET"])
    def agent_log():
        """取得代理行動日誌"""
        from agent_engine import get_log
        limit = int(request.args.get("limit", 50))
        return jsonify(get_log(limit))

    # ══════════════════════════════════════
    # 工作流程 API
    # ══════════════════════════════════════
    @app.route("/ai/workflows", methods=["GET"])
    def workflows_list():
        from workflow_engine import load_workflows
        return jsonify(load_workflows())

    @app.route("/ai/workflows/<wf_id>/run", methods=["POST"])
    def workflow_run(wf_id):
        """非同步觸發工作流程，立即回傳 run_id"""
        from workflow_engine import run_workflow
        data    = request.get_json() or {}
        context = data.get("context", "")
        run_id  = f"{wf_id}_{int(time.time())}"

        def run_bg():
            run_workflow(wf_id, trigger_context=context, trigger="介面手動")

        threading.Thread(target=run_bg, daemon=True).start()
        return jsonify({"status": "started", "run_id": run_id, "wf_id": wf_id})

    @app.route("/ai/workflows/event", methods=["POST"])
    def workflow_event():
        """事件觸發"""
        from workflow_engine import trigger_by_event
        data       = request.get_json() or {}
        event_type = (data.get("event_type") or "").strip()
        context    = data.get("context", "")
        if not event_type:
            return jsonify({"error": "需要 event_type"}), 400

        def run_bg():
            trigger_by_event(event_type, context)

        threading.Thread(target=run_bg, daemon=True).start()
        return jsonify({"status": "triggered", "event_type": event_type})

    @app.route("/ai/workflows/status", methods=["GET"])
    def workflow_status():
        from workflow_engine import get_running_status
        return jsonify(get_running_status())

    # ══════════════════════════════════════════════════════════════
    # 知識庫路由 /ai/kb/*
    # ══════════════════════════════════════════════════════════════