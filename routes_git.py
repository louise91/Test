# routes_git.py — GitHub 同步管理
# 功能：git init / add / commit / push，從網頁操作，不需要 Bash console
import os, subprocess, time
from flask import request, jsonify

BASE = os.path.dirname(os.path.abspath(__file__))

GITIGNORE_CONTENT = """# 敏感設定（不上傳）
config.py
.env

# 資料庫與資料（不上傳，每台伺服器各自維護）
data/
*.db
*.sqlite

# Python 快取
__pycache__/
*.pyc
*.pyo
*.pyd
.Python

# 上傳的暫存檔
static/img/char_*.jpg
uploads/

# 系統檔
.DS_Store
Thumbs.db
"""

def _run(cmd, cwd=BASE, timeout=30):
    """執行 shell 指令，回傳 (success, output)"""
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True,
            text=True, timeout=timeout
        )
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "指令超時（超過 {}s）".format(timeout)
    except Exception as e:
        return False, str(e)

def _get_git_config():
    """從 DB 讀取 GitHub 設定"""
    try:
        from database import get_setting
        token = get_setting("GITHUB_TOKEN", "")
        repo  = get_setting("GITHUB_REPO", "")   # 格式：username/reponame
        return token.strip(), repo.strip()
    except Exception:
        return "", ""

def _make_remote_url(token, repo):
    """組合帶 token 的 remote URL"""
    return "https://{}@github.com/{}.git".format(token, repo)

def _is_git_repo():
    """檢查是否已 git init"""
    return os.path.isdir(os.path.join(BASE, ".git"))

def _ensure_gitignore():
    """確保 .gitignore 存在"""
    path = os.path.join(BASE, ".gitignore")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(GITIGNORE_CONTENT)
        return True
    return False

def register_git_routes(app):

    @app.route("/ai/git/status", methods=["GET"])
    def git_status():
        """回傳目前 git 狀態"""
        token, repo = _get_git_config()
        is_init = _is_git_repo()

        if not is_init:
            return jsonify({
                "initialized": False,
                "repo": repo,
                "has_token": bool(token),
                "status": "尚未初始化 git 倉庫",
                "log": []
            })

        ok, out = _run(["git", "status", "--short"])
        _, log  = _run(["git", "log", "--oneline", "-5"])

        # 取得目前 remote
        _, remote = _run(["git", "remote", "get-url", "origin"])

        return jsonify({
            "initialized": True,
            "repo": repo,
            "has_token": bool(token),
            "status": out or "（無修改）",
            "log": log.splitlines() if log else [],
            "remote_set": "github.com" in remote
        })

    @app.route("/ai/git/setup", methods=["POST"])
    def git_setup():
        """
        初始化設定：
        1. git init（如果還沒）
        2. 建立 .gitignore
        3. 設定 remote origin
        4. 設定 user.email / user.name
        body: { "token": "...", "repo": "username/reponame" }
        """
        data  = request.get_json() or {}
        token = (data.get("token") or "").strip()
        repo  = (data.get("repo") or "").strip()

        if not token or not repo:
            return jsonify({"ok": False, "msg": "token 和 repo 都必須填"}), 400

        if "/" not in repo:
            return jsonify({"ok": False, "msg": "repo 格式應為 username/reponame"}), 400

        logs = []

        # 儲存到 DB
        try:
            from database import set_setting
            set_setting("GITHUB_TOKEN", token)
            set_setting("GITHUB_REPO", repo)
            logs.append("✅ 已儲存 GitHub 設定到 DB")
        except Exception as e:
            return jsonify({"ok": False, "msg": "DB 儲存失敗：" + str(e)}), 500

        # git init
        if not _is_git_repo():
            ok, out = _run(["git", "init"])
            logs.append(("✅ " if ok else "❌ ") + "git init：" + out)
            if not ok:
                return jsonify({"ok": False, "msg": "\n".join(logs)})
        else:
            logs.append("ℹ️ 已有 .git 倉庫，跳過 init")

        # .gitignore
        created = _ensure_gitignore()
        logs.append("✅ .gitignore " + ("已建立" if created else "已存在"))

        # git config
        _run(["git", "config", "user.email", "bot@knitsite.local"])
        _run(["git", "config", "user.name",  "KnitSite Bot"])
        logs.append("✅ 已設定 git user")

        # 設定 remote（若已存在則更新）
        remote_url = _make_remote_url(token, repo)
        ok, out = _run(["git", "remote", "get-url", "origin"])
        if ok:
            ok2, out2 = _run(["git", "remote", "set-url", "origin", remote_url])
            logs.append(("✅ " if ok2 else "❌ ") + "更新 remote origin：" + out2)
        else:
            ok2, out2 = _run(["git", "remote", "add", "origin", remote_url])
            logs.append(("✅ " if ok2 else "❌ ") + "新增 remote origin：" + out2)

        return jsonify({"ok": True, "msg": "\n".join(logs)})

    @app.route("/ai/git/push", methods=["POST"])
    def git_push():
        """
        執行完整推送：git add -A → git commit → git push
        body: { "message": "commit 訊息（可選）" }
        """
        token, repo = _get_git_config()
        if not token or not repo:
            return jsonify({"ok": False, "msg": "尚未設定 GitHub Token 和 Repo，請先到設定完成初始化"}), 400

        if not _is_git_repo():
            return jsonify({"ok": False, "msg": "尚未初始化 git，請先執行「初始化設定」"}), 400

        data    = request.get_json() or {}
        msg     = (data.get("message") or "").strip()
        if not msg:
            msg = "update " + time.strftime("%Y-%m-%d %H:%M")

        logs = []

        # 確保 remote URL 帶最新 token（token 可能更新過）
        remote_url = _make_remote_url(token, repo)
        _run(["git", "remote", "set-url", "origin", remote_url])

        # git add -A
        ok, out = _run(["git", "add", "-A"])
        logs.append(("✅ " if ok else "❌ ") + "git add：" + (out or "OK"))
        if not ok:
            return jsonify({"ok": False, "msg": "\n".join(logs)})

        # 檢查是否有東西要 commit
        ok2, status = _run(["git", "status", "--short"])
        if not status.strip():
            return jsonify({"ok": True, "msg": "ℹ️ 沒有新的變更，不需要 commit"})

        # git commit
        ok, out = _run(["git", "commit", "-m", msg])
        logs.append(("✅ " if ok else "❌ ") + "git commit：" + (out or "OK"))
        if not ok and "nothing to commit" not in out:
            return jsonify({"ok": False, "msg": "\n".join(logs)})

        # git push
        ok, out = _run(
            ["git", "push", "-u", "origin", "main"],
            timeout=60
        )
        if not ok and "main" in out:
            # 嘗試 master
            ok, out = _run(
                ["git", "push", "-u", "origin", "master"],
                timeout=60
            )
        logs.append(("✅ " if ok else "❌ ") + "git push：" + (out or "OK"))

        return jsonify({"ok": ok, "msg": "\n".join(logs)})

    @app.route("/ai/git/log", methods=["GET"])
    def git_log():
        """取得最近 10 筆 commit 記錄"""
        if not _is_git_repo():
            return jsonify({"log": [], "msg": "尚未初始化"})
        ok, out = _run(["git", "log", "--oneline", "-10"])
        return jsonify({
            "log": out.splitlines() if out else [],
            "ok": ok
        })
