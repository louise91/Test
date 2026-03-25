# ================== 真多代理模組 v3 ==================
# 新增：Rate limit 偵測 → 自動切換排隊模式

import threading, time, json, requests, queue, os

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY")
GROQ_API_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL     = "llama-3.3-70b-versatile"
MAX_RETRIES    = 4
BASE_WAIT      = 5
MAX_TOKENS     = 800
SUMMARY_TOKENS = 1200

# ── Rate limit 監控 ──
# 記錄最近 60 秒內遇到 429 的次數
_rate_hit_times: list[float] = []
_rate_lock = threading.Lock()
QUEUE_MODE_THRESHOLD = 2   # 60秒內遇到 N 次 429 就切換排隊模式
QUEUE_MODE_COOLDOWN  = 120 # 排隊模式持續幾秒後才嘗試回並行

def record_rate_hit():
    """記錄一次 429"""
    with _rate_lock:
        now = time.time()
        _rate_hit_times.append(now)
        # 只保留最近 60 秒的記錄
        _rate_hit_times[:] = [t for t in _rate_hit_times if now - t < 60]

def should_use_queue_mode() -> bool:
    """判斷是否應該用排隊模式"""
    with _rate_lock:
        now = time.time()
        recent = [t for t in _rate_hit_times if now - t < 60]
        return len(recent) >= QUEUE_MODE_THRESHOLD

def get_rate_status() -> dict:
    """給前端的狀態資訊"""
    with _rate_lock:
        now = time.time()
        recent = [t for t in _rate_hit_times if now - t < 60]
        in_queue = len(recent) >= QUEUE_MODE_THRESHOLD
        return {
            "mode": "queue" if in_queue else "parallel",
            "hits_last_60s": len(recent),
            "threshold": QUEUE_MODE_THRESHOLD
        }

# ── 全域排隊鎖（排隊模式用）──
_api_queue_lock = threading.Semaphore(1)  # 同時只允許 1 個 API 呼叫

# ── 代理記憶 ──
_agent_memories: dict[str, list] = {}
_memory_lock = threading.Lock()

def get_agent_memory(agent_id: str, system_prompt: str) -> list:
    with _memory_lock:
        if agent_id not in _agent_memories:
            _agent_memories[agent_id] = [{"role": "system", "content": system_prompt}]
        else:
            _agent_memories[agent_id][0] = {"role": "system", "content": system_prompt}
        return list(_agent_memories[agent_id])

def append_agent_memory(agent_id: str, role: str, content: str):
    with _memory_lock:
        if agent_id in _agent_memories:
            _agent_memories[agent_id].append({"role": role, "content": content})
            if len(_agent_memories[agent_id]) > 21:
                _agent_memories[agent_id][1:3] = []

def clear_agent_memory(agent_id: str):
    with _memory_lock:
        _agent_memories.pop(agent_id, None)

def clear_all_memories(agent_ids: list):
    for aid in agent_ids:
        clear_agent_memory(aid)

# ── Groq 呼叫（帶重試 + 排隊模式感知）──
def call_groq_with_retry(
    messages:   list,
    max_tokens: int = MAX_TOKENS,
    use_queue:  bool = False,
) -> tuple:
    """
    use_queue=True：用全域 Semaphore 確保只有一個請求同時跑
    回傳 (content, error)
    """
    wait = BASE_WAIT

    def do_call():
        nonlocal wait
        for attempt in range(MAX_RETRIES):
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
                        "temperature": 0.7,
                    },
                    timeout=60
                )
                if r.status_code == 429:
                    record_rate_hit()  # 記錄這次 429
                    retry_after = int(r.headers.get("retry-after", wait))
                    time.sleep(retry_after)
                    wait *= 2
                    continue
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"], None
            except requests.exceptions.HTTPError as e:
                return "", f"HTTP {e.response.status_code}"
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait); wait *= 2; continue
                return "", str(e)
        return "", "超過重試次數"

    if use_queue:
        # 排隊模式：等候取得鎖才呼叫
        with _api_queue_lock:
            return do_call()
    else:
        return do_call()

# ── 單輪工作執行緒 ──
def round_worker(
    agent_id:     str,
    agent_name:   str,
    system_prompt: str,
    user_message: str,
    context:      str,
    prior_round:  list,
    round_num:    int,
    result_queue: queue.Queue,
    use_queue:    bool = False,
):
    result_queue.put({
        "type": "agent_start", "agent_id": agent_id,
        "name": agent_name, "round": round_num
    })

    sp = system_prompt
    if context:
        sp += f"\n\n{context}"

    messages = get_agent_memory(agent_id, sp)

    if round_num == 1:
        messages.append({"role": "user", "content": user_message})
    else:
        others = "\n\n".join([
            f"【{name}】的看法：\n{content}"
            for name, content in prior_round
            if name != agent_name
        ])
        prompt = (
            f"原始問題：{user_message}\n\n"
            f"其他代理的看法：\n{others}\n\n"
            f"請從你的專業角度：同意哪些？反駁哪些？有什麼補充？"
        )
        messages.append({"role": "user", "content": prompt})

    content, error = call_groq_with_retry(messages, use_queue=use_queue)

    if error:
        result_queue.put({
            "type": "agent_error", "agent_id": agent_id,
            "name": agent_name, "round": round_num, "text": f"[錯誤: {error}]"
        })
        result_queue.put({
            "type": "agent_done", "agent_id": agent_id, "round": round_num, "text": ""
        })
        return

    append_agent_memory(agent_id, "user",      messages[-1]["content"])
    append_agent_memory(agent_id, "assistant", content)

    result_queue.put({
        "type": "agent_result", "agent_id": agent_id,
        "name": agent_name, "round": round_num, "text": content
    })
    result_queue.put({
        "type": "agent_done", "agent_id": agent_id, "round": round_num, "text": content
    })

# ── 執行一整輪 ──
def run_round(agents, user_message, context, prior_round, round_num, result_queue, use_queue):
    local_q  = queue.Queue()
    results  = {}

    def worker_wrapper(agent):
        round_worker(
            agent["id"], agent["name"], agent["prompt"],
            user_message, context, prior_round,
            round_num, local_q, use_queue
        )

    if use_queue:
        # 排隊模式：依序執行
        for agent in agents:
            t = threading.Thread(target=worker_wrapper, args=(agent,), daemon=True)
            t.start(); t.join()
    else:
        # 並行模式
        threads = [threading.Thread(target=worker_wrapper, args=(a,), daemon=True) for a in agents]
        for t in threads: t.start()

    done_count = 0
    total      = len(agents)

    while done_count < total:
        try:
            ev = local_q.get(timeout=120)
        except Exception:
            break
        result_queue.put(ev)
        if ev["type"] == "agent_done":
            done_count += 1
        elif ev["type"] == "agent_result":
            results[ev["name"]] = ev["text"]

    return [(name, content) for name, content in results.items()]

# ── 主持人總結 ──
def run_moderator(user_message, all_rounds, moderator_prompt, result_queue, use_queue):
    result_queue.put({"type": "moderator_start"})

    discussion = []
    for round_num, round_results in enumerate(all_rounds, 1):
        label = "第一輪（獨立分析）" if round_num == 1 else f"第{round_num}輪（交叉討論）"
        discussion.append(f"=== {label} ===")
        for name, content in round_results:
            discussion.append(f"【{name}】：\n{content}")

    messages = [
        {"role": "system", "content": moderator_prompt},
        {"role": "user", "content":
            f"原始問題：{user_message}\n\n"
            f"完整討論：\n\n" + "\n\n".join(discussion) +
            "\n\n請整合所有觀點，給出最終結論與建議。"
        }
    ]

    content, error = call_groq_with_retry(
        messages, max_tokens=SUMMARY_TOKENS, use_queue=use_queue
    )

    result_queue.put({
        "type": "moderator_result",
        "text": content if not error else f"[總結失敗: {error}]"
    })
    result_queue.put({"type": "moderator_done"})

# ── 主函數 ──
def run_discussion(
    agents:           list,
    user_message:     str,
    context:          str = "",
    moderator_prompt: str = "你是客觀的主持人，擅長整合不同觀點，給出平衡而有深度的總結。用繁體中文回覆。",
    num_rounds:       int = 2,
) -> queue.Queue:

    result_queue = queue.Queue()

    # 自動判斷模式
    use_queue = should_use_queue_mode()
    mode_label = "queue" if use_queue else "parallel"

    def orchestrate():
        # 送出各角色的感知摘要（若 agent 物件帶有 _perception）
        perceptions = {}
        for ag in agents:
            if ag.get("_perception") or ag.get("_facts"):
                perceptions[ag.get("name", ag["id"])] = {
                    "perception": ag.get("_perception",""),
                    "facts":      ag.get("_facts",[]),
                    "strategy":   ag.get("_strategy",""),
                }
        if perceptions:
            result_queue.put({
                "type":        "perception_ready",
                "perceptions": perceptions,
            })

        # 先通知前端目前模式
        result_queue.put({
            "type":  "mode_info",
            "mode":  mode_label,
            "msg":   "排隊模式（穩定）" if use_queue else "並行模式（快速）"
        })

        all_rounds = []
        prior      = []

        for round_num in range(1, num_rounds + 1):
            # 每輪開始前再檢查一次模式（可能中途切換）
            current_queue = should_use_queue_mode()
            result_queue.put({
                "type": "round_start", "round": round_num, "total": num_rounds,
                "mode": "queue" if current_queue else "parallel"
            })
            round_results = run_round(
                agents, user_message, context, prior,
                round_num, result_queue, current_queue
            )
            all_rounds.append(round_results)
            prior = round_results
            result_queue.put({"type": "round_end", "round": round_num})

        final_queue = should_use_queue_mode()
        run_moderator(user_message, all_rounds, moderator_prompt, result_queue, final_queue)
        result_queue.put({"type": "all_done"})

    threading.Thread(target=orchestrate, daemon=True).start()
    return result_queue

# 舊介面相容
def run_true_multi(agents, user_message, context=""):
    return run_discussion(agents, user_message, context, num_rounds=1)