# ================== 工作流程引擎 ==================
# 讀 workflows.json，執行串行/並行/會議步驟
# 支援：手動觸發、事件觸發、排程觸發

import os, json, time, threading
from agent_engine import run_agent_cycle, run_meeting, log_action, get_log
from world_manager import load_characters, load_world, add_event

BASE           = os.path.dirname(os.path.abspath(__file__))
DATA_DIR       = os.path.join(BASE, "data")
WORKFLOWS_FILE = os.path.join(DATA_DIR, "workflows.json")

os.makedirs(DATA_DIR, exist_ok=True)

# ── 執行狀態追蹤 ──
_running_workflows: dict[str, dict] = {}  # workflow_id → 狀態
_wf_lock = threading.Lock()

# ══════════════════════════════════════
# 讀取工作流程
# ══════════════════════════════════════
def load_workflows() -> list:
    try:
        with open(WORKFLOWS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("workflows", [])
    except Exception:
        return []

def get_workflow(wf_id: str) -> dict | None:
    return next((w for w in load_workflows() if w["id"] == wf_id), None)

# ══════════════════════════════════════
# 解析 agent_ids
# ══════════════════════════════════════
def resolve_agent_ids(agent_ids) -> list[str]:
    """
    "all" → 所有劇本角色 ID
    [...] → 直接用
    """
    if agent_ids == "all":
        return [c["id"] for c in load_characters()]
    return list(agent_ids)

# ══════════════════════════════════════
# 執行單一步驟
# ══════════════════════════════════════
def execute_step(step: dict, context: str, wf_name: str, step_idx: int) -> dict:
    """
    執行單一步驟，回傳結果 dict
    context = 上一步的輸出（傳給下一步）
    """
    step_type = step.get("type", "agent")
    result    = {"type": step_type, "step": step_idx, "output": ""}

    # ── 單一代理 ──
    if step_type == "agent":
        agent_id = step.get("agent_id", "")
        task     = step.get("task", "")
        if context:
            task = f"{task}\n\n上一步的輸出供參考：\n{context}"
        log_action(wf_name, f"步驟{step_idx} 代理行動", f"代理：{agent_id}")
        r = run_agent_cycle(agent_id, trigger=f"工作流程：{wf_name} 步驟{step_idx}")
        result["output"]   = r.get("action", "")
        result["agent_id"] = agent_id
        result["emotion"]  = r.get("emotion", "")

    # ── 所有代理各自行動 ──
    elif step_type == "all_agents":
        task  = step.get("task", "")
        chars = load_characters()
        outputs = []
        log_action(wf_name, f"步驟{step_idx} 全員行動", task)
        for char in chars:
            r = run_agent_cycle(char["id"], trigger=f"工作流程：{wf_name}")
            outputs.append(f"【{char['name']}】{r.get('action','')}")
            time.sleep(2)  # 避免 rate limit
        result["output"] = "\n".join(outputs)

    # ── 並行（多代理同時行動）──
    elif step_type == "parallel":
        agents_config = step.get("agents", [])
        log_action(wf_name, f"步驟{step_idx} 並行行動", f"{len(agents_config)} 個代理")
        outputs   = {}
        threads   = []
        lock      = threading.Lock()

        def run_one(cfg):
            r = run_agent_cycle(cfg["agent_id"], trigger=f"工作流程並行：{wf_name}")
            with lock:
                outputs[cfg["agent_id"]] = r.get("action", "")

        for cfg in agents_config:
            t = threading.Thread(target=run_one, args=(cfg,), daemon=True)
            threads.append(t)
        for t in threads: t.start()
        for t in threads: t.join(timeout=90)

        result["output"] = "\n".join([f"【{k}】{v}" for k, v in outputs.items()])

    # ── 會議 ──
    elif step_type == "meeting":
        agent_ids = resolve_agent_ids(step.get("agent_ids", []))
        topic     = step.get("topic", "")
        if context:
            topic = f"{topic}（背景：{context[:200]}）"
        log_action(wf_name, f"步驟{step_idx} 開會", topic)
        r = run_meeting(agent_ids, topic, trigger=f"工作流程：{wf_name}")
        result["output"]  = r.get("summary", "")
        result["results"] = r.get("results", {})

    else:
        result["output"] = f"[未知步驟類型: {step_type}]"

    return result

# ══════════════════════════════════════
# 執行完整工作流程
# ══════════════════════════════════════
def run_workflow(wf_id: str, trigger_context: str = "", trigger: str = "手動") -> dict:
    """
    執行一個工作流程的全部步驟
    串行：上一步輸出 → 下一步的 context
    """
    wf = get_workflow(wf_id)
    if not wf:
        return {"error": f"找不到工作流程：{wf_id}"}

    wf_name = wf["name"]
    steps   = wf.get("steps", [])
    run_id  = f"{wf_id}_{int(time.time())}"

    # 記錄執行狀態
    with _wf_lock:
        _running_workflows[run_id] = {
            "wf_id":     wf_id,
            "name":      wf_name,
            "trigger":   trigger,
            "started":   time.strftime("%Y-%m-%d %H:%M:%S"),
            "status":    "執行中",
            "current_step": 0,
            "total_steps":  len(steps),
            "step_results": []
        }

    log_action(wf_name, "工作流程開始", f"觸發：{trigger} | 步驟數：{len(steps)}")
    add_event({
        "type":        "工作流程",
        "title":       f"工作流程啟動：{wf_name}",
        "description": f"觸發：{trigger}",
        "involved":    [],
        "status":      "執行中"
    })

    context = trigger_context
    step_results = []

    try:
        for i, step in enumerate(steps):
            with _wf_lock:
                _running_workflows[run_id]["current_step"] = i + 1

            log_action(wf_name, f"執行步驟 {i+1}/{len(steps)}", step.get("type",""))
            result = execute_step(step, context, wf_name, i + 1)
            step_results.append(result)

            # 上一步輸出作為下一步 context
            context = result.get("output", "")
            time.sleep(1)  # 步驟間略作停頓

        # 完成
        final_output = step_results[-1].get("output", "") if step_results else ""
        with _wf_lock:
            _running_workflows[run_id]["status"]       = "完成"
            _running_workflows[run_id]["step_results"] = step_results
            _running_workflows[run_id]["final_output"] = final_output

        log_action(wf_name, "工作流程完成", final_output[:200])
        add_event({
            "type":        "工作流程",
            "title":       f"工作流程完成：{wf_name}",
            "description": final_output[:500],
            "involved":    [],
            "status":      "完成"
        })

        return {
            "run_id":       run_id,
            "wf_id":        wf_id,
            "name":         wf_name,
            "status":       "完成",
            "steps_count":  len(steps),
            "final_output": final_output,
            "step_results": step_results
        }

    except Exception as e:
        with _wf_lock:
            _running_workflows[run_id]["status"] = f"錯誤：{e}"
        log_action(wf_name, "工作流程錯誤", str(e))
        return {"error": str(e), "run_id": run_id}

# ══════════════════════════════════════
# 事件觸發
# ══════════════════════════════════════
def trigger_by_event(event_type: str, context: str = ""):
    """
    根據事件類型自動執行對應的工作流程
    可以在 agent_engine 或 app.py 裡呼叫
    """
    workflows = load_workflows()
    matched   = [w for w in workflows if w.get("trigger") == "event"
                 and w.get("event_type") == event_type]
    results   = []
    for wf in matched:
        log_action("系統", "事件觸發", f"{event_type} → {wf['name']}")
        r = run_workflow(wf["id"], trigger_context=context, trigger=f"事件:{event_type}")
        results.append(r)
    return results

# ══════════════════════════════════════
# 取得執行狀態
# ══════════════════════════════════════
def get_running_status() -> list:
    with _wf_lock:
        return [
            {
                "run_id":        k,
                "name":          v["name"],
                "status":        v["status"],
                "current_step":  v["current_step"],
                "total_steps":   v["total_steps"],
                "started":       v["started"],
            }
            for k, v in _running_workflows.items()
        ]

# ══════════════════════════════════════
# 排程觸發（由 PythonAnywhere Task 呼叫）
# ══════════════════════════════════════
if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "schedule":
        # 找出所有 schedule 觸發的工作流程
        now_time = time.strftime("%H:%M")
        workflows = load_workflows()
        for wf in workflows:
            if wf.get("trigger") == "schedule":
                wf_time = wf.get("time", "")
                if wf_time == now_time:
                    print(f"排程觸發：{wf['name']}")
                    r = run_workflow(wf["id"], trigger="排程")
                    print(f"完成：{r.get('final_output','')[:100]}")

    elif cmd == "run" and len(sys.argv) > 2:
        wf_id   = sys.argv[2]
        context = sys.argv[3] if len(sys.argv) > 3 else ""
        print(f"手動執行工作流程：{wf_id}")
        r = run_workflow(wf_id, trigger_context=context, trigger="手動排程")
        print(f"結果：{r.get('final_output','')[:200]}")

    elif cmd == "event" and len(sys.argv) > 2:
        event_type = sys.argv[2]
        context    = sys.argv[3] if len(sys.argv) > 3 else ""
        print(f"事件觸發：{event_type}")
        results = trigger_by_event(event_type, context)
        for r in results:
            print(f"  {r.get('name','')}: {r.get('final_output','')[:100]}")