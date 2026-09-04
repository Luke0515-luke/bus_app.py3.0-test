"""
即時公車資料（定位／到站預估）快照的 GitHub 同步。

跟現有 push_backup.py / pull_backup.py 完全分開、互不影響：
- push_backup.py / pull_backup.py：負責把路線 Shape/StopOfRoute（存在
  /opt/render/project/data）備份到 bus_app.py3.0backup 這個 repo 的 master 分支，
  每 10 分鐘一次，這裡完全沒有動它們。
- 這個檔案：負責把「每分鐘」抓一次的公車即時定位／到站預估快照，推到同一個
  repo 底下另一個獨立的分支（REALTIME_BRANCH），存放在另一個獨立的本機資料夾，
  兩邊各自維護自己的 git 狀態，不會互相干擾。
"""
import subprocess
import os

REALTIME_BRANCH = "realtime-data"
_REPO_URL_TMPL = "https://{token}@github.com/Luke0515-luke/bus_app.py3.0backup.git"


def _run(args, cwd=None, check=True):
    try:
        subprocess.run(args, cwd=cwd, check=check)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ 指令失敗: {' '.join(args)}\n原因: {e}", flush=True)
        return False


def _remote_url():
    token = os.environ.get("GITHUB_TOKEN")
    return _REPO_URL_TMPL.format(token=token)


def pull_realtime_backup(local_dir):
    """開機時盡量把 realtime-data 分支上次留下的快照拉回來，讓伺服器一啟動
    就有『上次成功抓到的資料』可以先用，不用整整等到第一次排程（最多 1 分鐘）
    跑完才有東西可以顯示。如果分支還不存在（例如系統第一次上線，從沒推送過），
    這裡會失敗，屬於正常情況，之後第一次排程成功時會自動建立這個分支。"""
    remote_url = _remote_url()
    os.makedirs(local_dir, exist_ok=True)
    git_dir = os.path.join(local_dir, ".git")

    if not os.path.exists(git_dir):
        print(f"📂 {local_dir} 尚未初始化，嘗試 clone {REALTIME_BRANCH} 分支...", flush=True)
        cloned = _run(
            ["git", "clone", "--branch", REALTIME_BRANCH, "--single-branch", remote_url, local_dir],
            check=False,
        )
        if cloned:
            print(f"✅ 已從 {REALTIME_BRANCH} 分支拉回上次的即時資料快照。", flush=True)
            return
        print(f"ℹ️ {REALTIME_BRANCH} 分支可能還不存在（第一次執行屬正常現象），"
              "改成本機初始化，等第一次排程成功時自動建立該分支。", flush=True)
        _run(["git", "init", local_dir], check=False)
        _run(["git", "-C", local_dir, "config", "user.name", "luke"], check=False)
        _run(["git", "-C", local_dir, "config", "user.email", "0515luke@gmail.com"], check=False)
        _run(["git", "-C", local_dir, "checkout", "-b", REALTIME_BRANCH], check=False)
        _run(["git", "-C", local_dir, "remote", "add", "origin", remote_url], check=False)
        return

    _run(["git", "-C", local_dir, "config", "user.name", "luke"], check=False)
    _run(["git", "-C", local_dir, "config", "user.email", "0515luke@gmail.com"], check=False)
    _run(["git", "-C", local_dir, "remote", "remove", "origin"], check=False)
    _run(["git", "-C", local_dir, "remote", "add", "origin", remote_url], check=False)
    if _run(["git", "-C", local_dir, "pull", "origin", REALTIME_BRANCH], check=False):
        print(f"✅ 已從 {REALTIME_BRANCH} 分支拉取最新的即時資料快照。", flush=True)


def push_realtime_backup(local_dir):
    """把這一輪抓到的即時公車快照（定位＋到站預估）整批推到 bus_app.py3.0backup
    這個 repo 的 realtime-data 分支，強制覆蓋（-f），確保遠端永遠只留『最新這一份』，
    不會累積歷史快照檔案，也完全不會動到 master 分支上的路線資料備份。
    刻意全程用 cwd=local_dir 而不是 os.chdir()：os.chdir() 會改變『整個程式』
    的工作目錄，這支函式是被獨立的排程執行緒每 1 分鐘呼叫一次，跟現有
    backup()（每 10 分鐘、也會呼叫 os.chdir）是兩條各自獨立的執行緒，
    如果兩邊都用 os.chdir，剛好同時觸發時彼此的工作目錄會互相干擾；
    用 cwd 參數就不會有這個問題。"""
    remote_url = _remote_url()
    try:
        os.makedirs(local_dir, exist_ok=True)
        if not os.path.exists(os.path.join(local_dir, ".git")):
            _run(["git", "init"], cwd=local_dir, check=True)

        _run(["git", "config", "user.name", "luke"], cwd=local_dir, check=True)
        _run(["git", "config", "user.email", "0515luke@gmail.com"], cwd=local_dir, check=True)
        _run(["git", "remote", "remove", "origin"], cwd=local_dir, check=False)
        _run(["git", "remote", "add", "origin", remote_url], cwd=local_dir, check=True)
        _run(["git", "checkout", "-B", REALTIME_BRANCH], cwd=local_dir, check=False)

        status_output = subprocess.check_output(["git", "status", "--porcelain"], cwd=local_dir, text=True)
        if not status_output.strip():
            print("✅ 即時資料無變動，無需推送。", flush=True)
            return

        _run(["git", "add", "--all"], cwd=local_dir, check=False)
        _run(["git", "commit", "-m", "Realtime snapshot update"], cwd=local_dir, check=False)
        pushed = _run(["git", "push", "-f", "origin", f"HEAD:{REALTIME_BRANCH}"], cwd=local_dir, check=False)
        if pushed:
            print(f"✅ 即時資料已同步到 GitHub（分支：{REALTIME_BRANCH}）", flush=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ 即時資料 Git 推送失敗：{e}", flush=True)
    except Exception as e:
        print(f"❌ 即時資料推送發生錯誤：{e}", flush=True)
