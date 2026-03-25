# ai_routes.py — 主路由（聊天/設定/Session/World/Video）
import os, json, time, base64, requests, math, re, threading
from flask import request, Response, render_template, session, jsonify

# 共用工具
from ai_utils import *

def register_ai_routes(app):

    @app.route("/ai")
    def ai_index():
        return render_template("ai_chat.html", roles=load_roles())


    @app.route("/ai/debug/keys")
    def debug_keys():
        try:
            from database import get_setting
            or_key = (get_setting("OPENROUTER_API_KEY") or "")
            groq_key = (get_setting("GROQ_API_KEY") or "")
            return jsonify({
                "openrouter_len": len(or_key),
                "openrouter_ok": len(or_key) > 10,
                "openrouter_prefix": or_key[:8] if or_key else "EMPTY",
                "groq_len": len(groq_key),
                "groq_ok": len(groq_key) > 10,
                "engine_ok": bool(_ENGINE_OK),
            })
        except Exception as e:
            import traceback; traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── 單代理串流 ──
    @app.route("/ai/chat", methods=["POST"])
    def ai_stream():
      try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        if ai_rate_limited(ip):
            return Response("請求太頻繁，請稍後再試。", status=429)
        key = _get_key("GROQ_API_KEY")
        if not key:
            return Response("[錯誤] 未設定 GROQ_API_KEY，請到設定頁面填入", status=500)

        text          = (request.form.get("message") or "").strip()
        do_search     = request.form.get("search") == "1"
        custom_prompt = (request.form.get("system_prompt") or "").strip()
        raw_ids = request.form.get("rag_doc_ids")
        try:
            rag_doc_ids = json.loads(raw_ids) if raw_ids else []
            if not isinstance(rag_doc_ids, list): rag_doc_ids = []
        except Exception:
            rag_doc_ids = []
        model_id      = (request.form.get("model_id") or "").strip() or None
        file_obj      = request.files.get("file")
        file_data     = None
        use_vision    = False
        # 視覺模型自動偵測
        if model_id:
            m = next((m for m in AVAILABLE_MODELS if m["id"]==model_id), None)
            if m and m.get("vision"):
                use_vision = True

        # RAG 向量搜尋
        rag_context = ""
        if rag_doc_ids and text:
            rag_context = rag_search(text, rag_doc_ids)

        # 判斷是否為劇本角色（system_prompt 含 char_id 標記）
        char_id_for_world = (request.form.get("char_id") or "").strip() or None

        if custom_prompt:
            system_prompt = custom_prompt
        else:
            system_prompt = "你是一個直接、有用的 AI 助手，用繁體中文回覆，不廢話。"

        # 策略 A：劇本角色 → 注入完整世界狀態
        if char_id_for_world:
            try:
                from world_context_builder import build_full_context
                world_ctx = build_full_context(char_id_for_world)
                if world_ctx:
                    system_prompt = world_ctx + "\n\n" + system_prompt
            except Exception:
                pass

        if rag_context:
            system_prompt += f"\n\n{rag_context}"

        # 檔案處理
        if file_obj and file_obj.filename:
            mime  = file_obj.mimetype or ""
            raw   = file_obj.read(MAX_FILE_BYTES)
            if len(raw) >= MAX_FILE_BYTES:
                return Response("檔案超過 10MB", status=413)
            b64   = base64.b64encode(raw).decode("utf-8")
            fname = file_obj.filename.lower()
            if mime in ALLOWED_IMAGE or fname.endswith((".jpg",".jpeg",".png",".gif",".webp")):
                file_data  = {"type":"image","mime":mime,"b64":b64,"name":file_obj.filename}
                use_vision = True
            elif fname.endswith((".txt",".csv",".json",".md",".py",".js",".html",".css")):
                file_data = {"type":"text","mime":mime,"b64":b64,"name":file_obj.filename}
            else:
                return Response("不支援的檔案類型", status=415)

        if not text and not file_data:
            return Response("[錯誤] 訊息不能為空", status=400)

        # sid 必須在 get_ai_messages 之前設定
        sid = request.form.get("session_id") or session.get("sid") or request.remote_addr or "default"
        session["sid"] = sid
        messages = get_ai_messages(sid, system_prompt)
        if do_search and text:
            messages.append({"role":"system","content":f"網路搜尋「{text}」結果：\n\n{web_search(text)}\n\n請整合以上資訊回答。"})
        messages.append(build_user_message(text, file_data))
        try:
            from database import save_chat_message as _scm, create_session as _cs
            _cs(sid, "新對話")  # 確保 session 存在
            _scm(sid, "user", text)
        except Exception:
            pass

        # 自動判斷：如果 model_id 是 OpenRouter 格式（含 / 或 :free），強制走 engine_manager
        _is_openrouter_model = bool(model_id and ("/" in model_id or ":free" in model_id or ":nitro" in model_id))
        _actual_engine = (get_engine() if _ENGINE_OK else None) or "groq"
        _actual_model  = model_id or "llama-3.3-70b-versatile"
        if _is_openrouter_model and _ENGINE_OK:
            _actual_engine  = "openrouter"
            _use_engine_mgr = True
        else:
            _use_engine_mgr = _ENGINE_OK and _actual_engine != "groq"

        import sys
        print(f"[chat] engine={_actual_engine!r} model={_actual_model!r} engine_mgr={_use_engine_mgr} or_model={_is_openrouter_model}", file=sys.stderr)

        def generate():
            final = ""
            actual_engine = _actual_engine
            try:
                if _use_engine_mgr:
                    stream_fn = lambda: stream_ai(messages, model=model_id)
                else:
                    stream_fn = lambda: stream_groq(messages, use_vision, model_id=model_id)
                for item in stream_fn():
                    content = item[0]
                    buf     = item[1]
                    final = buf
                    yield content.encode("utf-8")
                if final:
                    messages.append({"role":"assistant","content":final})
                    _save_messages(sid, messages)  # in-memory，無 context 問題
            except requests.exceptions.HTTPError as e:
                yield f"\n[HTTP 錯誤 {e.response.status_code}]".encode("utf-8")
            except Exception as e:
                import traceback; traceback.print_exc()
                yield f"\n[錯誤: {str(e)[:300]}]".encode("utf-8")

        return Response(generate(), mimetype="text/plain; charset=utf-8")
      except Exception as _e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(_e)}), 500

    # ── 假多代理（串行，SSE 格式）──
    @app.route("/ai/multi", methods=["POST"])
    def ai_multi():
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        if ai_rate_limited(ip, limit=5):
            return Response("請求太頻繁", status=429)
        key = _get_key("GROQ_API_KEY")
        if not key:
            return Response("[錯誤] 未設定 GROQ_API_KEY，請到設定頁面填入", status=500)

        data        = request.get_json() or {}
        text        = (data.get("message") or "").strip()
        role_ids    = data.get("role_ids", [])
        rag_doc_ids = data.get("rag_doc_ids", [])
        do_search   = data.get("search", False)

        if not text:
            return Response("[錯誤] 訊息不能為空", status=400)
        if len(role_ids) < 2:
            return Response("[錯誤] 多代理至少需要選 2 個角色", status=400)

        all_roles = {r["id"]: r for r in load_roles()}
        agents    = [all_roles[rid] for rid in role_ids if rid in all_roles]
        if len(agents) < 2:
            return Response("[錯誤] 找不到指定角色", status=400)

        # RAG 搜尋（全代理共用）
        rag_context = ""
        if rag_doc_ids:
            rag_context = rag_search(text, rag_doc_ids)

        # 網路搜尋（全代理共用）
        search_context = ""
        if do_search:
            search_context = web_search(text)

        def generate():
            conversation = []  # 記錄各代理的回答
            
            for i, agent in enumerate(agents):
                yield f"AGENT_START:{agent['name']}\n".encode("utf-8")
                
                # 組建這個代理的訊息
                sp = agent["prompt"]
                if rag_context:
                    sp += f"\n\n{rag_context}"
                if search_context:
                    sp += f"\n\n網路搜尋結果：\n{search_context}"
                
                msgs = [{"role":"system","content":sp}]
                
                # 加入前面代理的回答作為脈絡
                if conversation:
                    prior = "\n\n".join([f"【{name}】說：\n{reply}" for name, reply in conversation])
                    msgs.append({
                        "role":"user",
                        "content": f"問題：{text}\n\n前面的代理已經這樣回答：\n{prior}\n\n請根據以上內容，用你的專業角度補充、修正或提出不同觀點。"
                    })
                else:
                    msgs.append({"role":"user","content":text})
                
                # 呼叫 API
                try:
                    reply = call_groq_once(msgs)
                    conversation.append((agent["name"], reply))
                    yield reply.encode("utf-8")
                except Exception as e:
                    yield f"[錯誤: {e}]".encode("utf-8")
                
                yield b"\nAGENT_END\n"
            
            yield b"MULTI_DONE"

        return Response(generate(), mimetype="text/plain; charset=utf-8")

    # ── 對話重置 ──
    # ── 對話 Session 管理 ──
    # ── 設定管理 ──
    @app.route("/ai/settings", methods=["GET"])
    def get_settings():
        from database import get_all_settings
        s = get_all_settings()
        # 隱藏 key 值（只回傳有無設定）
        safe = {}
        for k,v in s.items():
            if "KEY" in k or "TOKEN" in k:
                safe[k] = "***" if v else ""
            else:
                safe[k] = v
        return jsonify(safe)

    @app.route("/ai/settings", methods=["POST"])
    def save_settings():
        from database import set_setting
        import importlib
        data = request.get_json() or {}
        for k, v in data.items():
            if k in ["GROQ_API_KEY","OPENROUTER_API_KEY","ANTHROPIC_API_KEY",
                     "BRAVE_API_KEY","OLLAMA_URL","COLAB_API_URL"]:
                if v and v != "***":  # 不儲存遮罩值
                    set_setting(k, v.strip())
        _refresh_keys()  # 立刻生效，不需重啟
        return jsonify({"status":"ok"})

    @app.route("/ai/sessions", methods=["GET"])
    def get_sessions():
        from database import list_chat_sessions
        sessions = list_chat_sessions()
        return jsonify(sessions)

    @app.route("/ai/sessions/<sid>", methods=["GET","POST"])
    def get_session_history(sid):
        if request.method == "POST":
            from database import create_session
            data = request.get_json() or {}
            create_session(sid, data.get("title","新對話"))
            return jsonify({"status":"ok"})
        from database import get_chat_history
        rows = get_chat_history(sid, limit=50)
        return jsonify(list(reversed(rows)))

    @app.route("/ai/sessions/<sid>", methods=["DELETE"])
    def delete_session(sid):
        from database import delete_chat_session
        delete_chat_session(sid)
        return jsonify({"status":"ok"})

    @app.route("/ai/sessions/<sid>/title", methods=["PUT"])
    def update_title(sid):
        data = request.get_json() or {}
        from database import update_session_title
        update_session_title(sid, data.get("title","新對話"))
        return jsonify({"status":"ok"})


    # ── 角色/RAG/模型/代理/工作流程 路由（見 routes_role.py）──
    from routes_role import register_role_routes
    register_role_routes(app)

    # ── 知識庫/資料庫 路由（見 routes_kb.py）──
    from routes_kb import register_kb_routes
    register_kb_routes(app)

    @app.route("/ai/world/view")
    def world_view():
        return render_template("world_view.html")

    @app.route("/ai/world/rpg")
    def world_view_rpg():
        return render_template("world_view_rpg.html")

    # ══════════════════════════════════════════════════════════════
    # 引擎管理 API
    # ══════════════════════════════════════════════════════════════

    @app.route("/ai/engines", methods=["GET"])
    def engines_status():
        """回傳所有引擎狀態"""
        if not _ENGINE_OK:
            return jsonify({"error": "engine_manager 載入失敗", "detail": _ENGINE_ERR}), 500
        ping_engines()   # 背景更新
        return jsonify(get_status())

    @app.route("/ai/engines/models", methods=["GET"])
    def engines_models():
        """回傳各引擎模型清單"""
        if not _ENGINE_OK:
            return jsonify({})
        return jsonify(get_models_by_engine())

    @app.route("/ai/engines/select", methods=["POST"])
    def engines_select():
        """
        手動切換引擎
        body: {"engine": "groq"} 或 {"engine": null} (自動)
        """
        if not _ENGINE_OK:
            return jsonify({"error": "engine_manager 載入失敗", "detail": _ENGINE_ERR}), 500
        data = request.get_json(force=True) or {}
        engine_id = data.get("engine")  # None = 自動
        try:
            set_engine(engine_id)
            return jsonify({"ok": True, "engine": engine_id or "auto"})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/ai/engines/current", methods=["GET"])
    def engines_current():
        """回傳目前選用的引擎"""
        if not _ENGINE_OK:
            return jsonify({"engine": "groq", "mode": "direct"})
        eid = get_engine()
        return jsonify({"engine": eid or "auto", "mode": "auto" if not eid else "manual"})
    # ══════════════════════════════════════════════════════════════
    # 世界引擎 API（時間推進 / 天氣 / 場景）
    # ══════════════════════════════════════════════════════════════

    @app.route("/ai/world/tick", methods=["POST"])
    def world_tick():
        """推進時間"""
        try:
            from world_engine import tick_hours, tick_days, ai_update_chars_after_tick
        except ImportError:
            return jsonify({"error": "world_engine 未安裝"}), 500
        data  = request.get_json(force=True) or {}
        mode  = data.get("mode", "hour")   # hour / day
        n     = int(data.get("n", 1))
        ai_up = data.get("ai_update", False)
        if mode == "day":
            world = tick_days(n)
        else:
            world = tick_hours(n)
        if ai_up:
            world = ai_update_chars_after_tick(n if mode=="hour" else n*24)
        meta = world.get("world_meta", {})
        return jsonify({
            "ok": True,
            "date": meta.get("date"),
            "time": meta.get("time"),
            "day_of_week": meta.get("day_of_week"),
            "weather": meta.get("weather", {}),
        })

    @app.route("/ai/world/weather", methods=["GET","POST"])
    def world_weather():
        """取得或強制更新天氣"""
        try:
            from world_engine import update_weather_only, fetch_weather
        except ImportError:
            return jsonify({"error": "world_engine 未安裝"}), 500
        if request.method == "POST":
            world = update_weather_only()
            return jsonify(world.get("world_meta", {}).get("weather", {}))
        else:
            ws = _load_world()
            return jsonify(ws.get("world_meta", {}).get("weather", {}))

    @app.route("/ai/world/scenes", methods=["GET"])
    def world_scenes():
        """取得所有場景資訊"""
        try:
            from world_engine import get_all_scenes
            return jsonify(get_all_scenes())
        except ImportError:
            ws = _load_world()
            return jsonify(ws.get("scenes", {}))

    @app.route("/ai/world/scenes/<scene_id>", methods=["GET","PUT"])
    def world_scene_detail(scene_id):
        """取得或更新單一場景"""
        try:
            from world_engine import get_scene, update_scene
        except ImportError:
            return jsonify({"error": "world_engine 未安裝"}), 500
        if request.method == "PUT":
            updates = request.get_json(force=True) or {}
            return jsonify(update_scene(scene_id, updates))
        return jsonify(get_scene(scene_id))

    @app.route("/ai/world/context/<char_id>", methods=["GET"])
    def world_context(char_id):
        """取得角色的完整世界 context（給 AI 注入用）"""
        try:
            from world_engine import build_world_context
            return jsonify({"context": build_world_context(char_id)})
        except ImportError:
            return jsonify({"context": ""})


    # ══════════════════════════════════════════════════════════════
    # RPG 地圖 ↔ 聊天室 即時 Feed
    # ══════════════════════════════════════════════════════════════
    _chat_feed = []   # [{char_id, preview, ts, emotion, location}, ...]
    _MAX_FEED  = 20

    @app.route("/ai/world/chat_feed", methods=["POST"])
    def world_chat_feed_post():
        """聊天室呼叫：把最新一句話存入 feed"""
        data = request.get_json(force=True) or {}
        entry = {
            "char_id":  data.get("charId",""),
            "preview":  (data.get("speech","") or "")[:30],
            "emotion":  data.get("emotion",""),
            "location": data.get("location",""),
            "task":     data.get("task",""),
            "ts":       __import__("time").time(),
        }
        _chat_feed.append(entry)
        if len(_chat_feed) > _MAX_FEED:
            _chat_feed.pop(0)

        # 同步更新 world_state.json 的情緒/任務
        char_id = entry["char_id"]
        if char_id:
            try:
                ws = _load_world()
                states = ws.get("character_states", {})
                if char_id not in states:
                    states[char_id] = {}
                if entry["emotion"]:
                    states[char_id]["emotion"] = entry["emotion"]
                if entry["task"]:
                    states[char_id]["current_task"] = entry["task"]
                ws["character_states"] = states
                _save_world(ws)
            except Exception:
                pass

        return jsonify({"ok": True})

    @app.route("/ai/world/feed", methods=["GET"])
    def world_feed_get():
        """RPG 地圖輪詢：取得最新對話片段"""
        since = float(request.args.get("since", 0))
        new_msgs = [m for m in _chat_feed if m["ts"] > since]
        return jsonify({
            "messages": new_msgs,
            "ts": __import__("time").time(),
        })


    # ══════════════════════════════════════════════════════════════
    # 影片分析路由
    # ══════════════════════════════════════════════════════════════

    @app.route("/ai/video/analyze", methods=["POST"])
    def video_analyze():
        """
        接收影片檔案，進行：
          1. ffmpeg 抽幀
          2. Whisper 轉字幕
          3. 串流送給 Llama 4 Scout 分析
        """
        import threading, queue as q_module

        f = request.files.get("video")
        if not f:
            return jsonify({"error": "沒有收到影片檔案"}), 400

        user_question = request.form.get("question","請詳細分析這段影片的內容、畫面和語音")
        language      = request.form.get("language") or None  # e.g. "zh", "en"
        n_frames      = int(request.form.get("n_frames", 8))

        # 儲存上傳的影片到暫存
        import tempfile, os
        from pathlib import Path
        ext  = Path(f.filename or "video.mp4").suffix.lower() or ".mp4"
        tmp  = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        f.save(tmp.name)
        tmp.close()
        video_path = tmp.name

        def generate():
            from video_processor import process_video, build_video_prompt, check_ffmpeg, MAX_VIDEO_MB

            try:
                # 步驟 1：前置檢查
                yield "data: " + json.dumps({"type":"progress","step":1,"text":"🎬 收到影片，開始處理…"}) + "\n\n"

                if not check_ffmpeg():
                    yield "data: " + json.dumps({"type":"error","text":"❌ 伺服器未安裝 ffmpeg，無法處理影片\n請聯絡管理員安裝 ffmpeg"}) + "\n\n"
                    return

                size_mb = os.path.getsize(video_path) / 1024 / 1024
                if size_mb > MAX_VIDEO_MB:
                    yield "data: " + json.dumps({"type":"error","text":f"❌ 影片過大（{size_mb:.1f}MB），上限 {MAX_VIDEO_MB}MB"}) + "\n\n"
                    return

                # 步驟 2：處理影片
                yield "data: " + json.dumps({"type":"progress","step":2,"text":f"🖼 正在抽取畫面幀（最多 {n_frames} 張）…"}) + "\n\n"
                processed = process_video(video_path, n_frames=n_frames, language=language)

                if processed.get("error"):
                    yield "data: " + json.dumps({"type":"error","text":"❌ " + processed["error"]}) + "\n\n"
                    return

                frames = processed.get("frames", [])
                transcript_fmt = processed.get("transcript_fmt","")
                info = processed.get("info",{})

                # 步驟 3：回報進度
                duration = info.get("duration",0)
                mm, ss = divmod(int(duration), 60)
                progress_text = f"✅ 已取得 {len(frames)} 幀畫面"
                if processed.get("has_audio"):
                    txt_len = len(transcript_fmt)
                    progress_text += f"，字幕 {txt_len} 字"
                else:
                    progress_text += "，無音訊"
                progress_text += f"（影片長度 {mm:02d}:{ss:02d}）"
                yield "data: " + json.dumps({"type":"progress","step":3,"text":progress_text}) + "\n\n"

                if not frames:
                    yield "data: " + json.dumps({"type":"error","text":"❌ 無法提取畫面，請確認影片格式"}) + "\n\n"
                    return

                # 步驟 4：組合 prompt 送 AI
                yield "data: " + json.dumps({"type":"progress","step":4,"text":"🤖 正在送給 AI 分析…"}) + "\n\n"

                prompt_text = build_video_prompt(processed, user_question)

                # 建立多模態訊息（文字 + 圖片）
                content_parts = [{"type":"text","text":prompt_text}]
                for frame in frames:
                    content_parts.append({
                        "type":"image_url",
                        "image_url":{"url":f"data:image/jpeg;base64,{frame['b64']}","detail":"low"}
                    })

                groq_key = GROQ_API_KEY
                headers  = {"Authorization":f"Bearer {groq_key}","Content-Type":"application/json"}
                payload  = {
                    "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                    "messages": [{"role":"user","content":content_parts}],
                    "max_tokens": 2048,
                    "stream": True,
                    "temperature": 0.5,
                }

                # 串流輸出 AI 分析
                yield "data: " + json.dumps({"type":"start"}) + "\n\n"

                with requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers, json=payload, stream=True, timeout=120
                ) as r:
                    if r.status_code != 200:
                        err = r.json().get("error",{}).get("message",r.text)
                        yield "data: " + json.dumps({"type":"error","text":f"❌ AI 分析失敗：{err}"}) + "\n\n"
                        return
                    for line in r.iter_lines():
                        if not line:
                            continue
                        line = line.decode("utf-8","ignore")
                        if line.startswith("data: "):
                            line = line[6:]
                        if line == "[DONE]":
                            break
                        try:
                            chunk = json.loads(line)
                            delta = chunk["choices"][0]["delta"]
                            token = delta.get("content","")
                            if token:
                                yield "data: " + json.dumps({"type":"token","text":token}) + "\n\n"
                        except Exception:
                            pass

                yield "data: " + json.dumps({"type":"done"}) + "\n\n"

            except Exception as e:
                yield "data: " + json.dumps({"type":"error","text":f"❌ 處理失敗：{str(e)}"}) + "\n\n"
            finally:
                try:
                    os.unlink(video_path)
                except Exception:
                    pass

        return Response(generate(), mimetype="text/event-stream",
                        headers={"X-Accel-Buffering":"no","Cache-Control":"no-cache"})

    @app.route("/ai/video/check", methods=["GET"])
    def video_check():
        """檢查 ffmpeg 是否可用"""
        from video_processor import check_ffmpeg
        ok = check_ffmpeg()
        return jsonify({"ffmpeg": ok, "max_mb": 50,
                        "supported": [".mp4",".mov",".avi",".mkv",".webm",".flv",".wmv",".m4v"]})