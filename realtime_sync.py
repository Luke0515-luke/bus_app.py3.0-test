"""
即時公車資料（定位／到站預估）快照的 GitHub 同步 + GPS watchdog。
"""

import subprocess
import os
import json
import time
import threading
import fcntl
import requests


REALTIME_BRANCH = "realtime-data"

_REPO_URL_TMPL = (
    "https://{token}@github.com/"
    "Luke0515-luke/bus_app.py3.0backup.git"
)


# =========================================================
# Git 指令工具
# =========================================================

def _run(args, cwd=None, check=True):
    try:
        subprocess.run(args, cwd=cwd, check=check)
        return True
    except subprocess.CalledProcessError as e:
        print(
            f"❌ 指令失敗: {' '.join(args)}\n"
            f"原因: {e}",
            flush=True
        )
        return False


def _remote_url():
    token = os.environ.get("GITHUB_TOKEN")

    if not token:
        print(
            "⚠️ 沒有設定 GITHUB_TOKEN，"
            "即時資料無法同步到 GitHub。",
            flush=True
        )

    return _REPO_URL_TMPL.format(token=token or "")


# =========================================================
# 啟動時拉回上一份即時資料
# =========================================================

def pull_realtime_backup(local_dir):
    remote_url = _remote_url()

    os.makedirs(local_dir, exist_ok=True)

    git_dir = os.path.join(local_dir, ".git")

    if not os.path.exists(git_dir):

        print(
            f"📂 {local_dir} 尚未初始化，"
            f"嘗試 clone {REALTIME_BRANCH} 分支...",
            flush=True
        )

        cloned = _run(
            [
                "git",
                "clone",
                "--branch",
                REALTIME_BRANCH,
                "--single-branch",
                remote_url,
                local_dir
            ],
            check=False
        )

        if cloned:
            print(
                f"✅ 已從 {REALTIME_BRANCH} "
                "分支拉回上次的即時資料。",
                flush=True
            )
            return

        print(
            "ℹ️ realtime-data 分支可能還不存在，"
            "建立新的本機 Git repository。",
            flush=True
        )

        _run(
            ["git", "init", local_dir],
            check=False
        )

        _run(
            [
                "git",
                "-C",
                local_dir,
                "config",
                "user.name",
                "luke"
            ],
            check=False
        )

        _run(
            [
                "git",
                "-C",
                local_dir,
                "config",
                "user.email",
                "0515luke@gmail.com"
            ],
            check=False
        )

        _run(
            [
                "git",
                "-C",
                local_dir,
                "checkout",
                "-b",
                REALTIME_BRANCH
            ],
            check=False
        )

        _run(
            [
                "git",
                "-C",
                local_dir,
                "remote",
                "add",
                "origin",
                remote_url
            ],
            check=False
        )

        return

    _run(
        [
            "git",
            "-C",
            local_dir,
            "config",
            "user.name",
            "luke"
        ],
        check=False
    )

    _run(
        [
            "git",
            "-C",
            local_dir,
            "config",
            "user.email",
            "0515luke@gmail.com"
        ],
        check=False
    )

    _run(
        [
            "git",
            "-C",
            local_dir,
            "remote",
            "remove",
            "origin"
        ],
        check=False
    )

    _run(
        [
            "git",
            "-C",
            local_dir,
            "remote",
            "add",
            "origin",
            remote_url
        ],
        check=False
    )

    if _run(
        [
            "git",
            "-C",
            local_dir,
            "pull",
            "origin",
            REALTIME_BRANCH
        ],
        check=False
    ):
        print(
            f"✅ 已從 {REALTIME_BRANCH} "
            "拉取最新即時資料。",
            flush=True
        )


# =========================================================
# 推送即時資料到 GitHub
# =========================================================

def push_realtime_backup(local_dir):

    remote_url = _remote_url()

    try:

        os.makedirs(local_dir, exist_ok=True)

        if not os.path.exists(
            os.path.join(local_dir, ".git")
        ):
            _run(
                ["git", "init"],
                cwd=local_dir,
                check=True
            )

        _run(
            [
                "git",
                "config",
                "user.name",
                "luke"
            ],
            cwd=local_dir,
            check=True
        )

        _run(
            [
                "git",
                "config",
                "user.email",
                "0515luke@gmail.com"
            ],
            cwd=local_dir,
            check=True
        )

        _run(
            [
                "git",
                "remote",
                "remove",
                "origin"
            ],
            cwd=local_dir,
            check=False
        )

        _run(
            [
                "git",
                "remote",
                "add",
                "origin",
                remote_url
            ],
            cwd=local_dir,
            check=True
        )

        _run(
            [
                "git",
                "checkout",
                "-B",
                REALTIME_BRANCH
            ],
            cwd=local_dir,
            check=False
        )

        status_output = subprocess.check_output(
            [
                "git",
                "status",
                "--porcelain"
            ],
            cwd=local_dir,
            text=True
        )

        if not status_output.strip():

            print(
                "✅ 即時資料沒有變化，"
                "不用推送。",
                flush=True
            )

            return

        _run(
            [
                "git",
                "add",
                "--all"
            ],
            cwd=local_dir,
            check=False
        )

        _run(
            [
                "git",
                "commit",
                "-m",
                "Realtime snapshot update"
            ],
            cwd=local_dir,
            check=False
        )

        pushed = _run(
            [
                "git",
                "push",
                "-f",
                "origin",
                f"HEAD:{REALTIME_BRANCH}"
            ],
            cwd=local_dir,
            check=False
        )

        if pushed:
            print(
                "✅ 即時資料已同步到 GitHub "
                f"（{REALTIME_BRANCH}）",
                flush=True
            )

    except Exception as e:

        print(
            f"❌ 即時資料 Git 推送失敗：{e}",
            flush=True
        )


# =========================================================
# GPS WATCHDOG
# =========================================================

WATCHDOG_INTERVAL = 15

# 超過 45 秒沒有更新，才啟動補救
WATCHDOG_STALE_SECONDS = 45

WATCHDOG_LOCK_FILE = ".poll.lock"

TDX_AUTH_URL = (
    "https://tdx.transportdata.tw/auth/"
    "realms/TDXConnect/protocol/openid-connect/token"
)

TDX_POSITION_URL = (
    "https://tdx.transportdata.tw/api/basic/v2/"
    "Bus/RealTimeByFrequency/City/Tainan"
    "?$format=JSON"
)


_watchdog_token = ""
_watchdog_token_expire_at = 0


def _get_watchdog_token():

    global _watchdog_token
    global _watchdog_token_expire_at

    # Token 還有效
    if (
        _watchdog_token
        and time.time() < _watchdog_token_expire_at
    ):
        return _watchdog_token

    client_id = os.environ.get(
        "CLIENT_ID",
        ""
    )

    client_secret = os.environ.get(
        "CLIENT_SECRET",
        ""
    )

    if not client_id or not client_secret:

        print(
            "⚠️ GPS watchdog 找不到 "
            "CLIENT_ID / CLIENT_SECRET。",
            flush=True
        )

        return ""

    try:

        response = requests.post(

            TDX_AUTH_URL,

            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret
            },

            headers={
                "Content-Type":
                "application/x-www-form-urlencoded"
            },

            timeout=10
        )

        if response.status_code != 200:

            print(
                "⚠️ TDX Token 取得失敗："
                f"HTTP {response.status_code}",
                flush=True
            )

            return ""

        data = response.json()

        _watchdog_token = data.get(
            "access_token",
            ""
        )

        expires_in = int(
            data.get(
                "expires_in",
                300
            )
        )

        _watchdog_token_expire_at = (
            time.time()
            + max(
                30,
                expires_in - 30
            )
        )

        return _watchdog_token

    except Exception as e:

        print(
            f"⚠️ TDX 認證失敗：{e}",
            flush=True
        )

        return ""


def _fetch_positions():

    token = _get_watchdog_token()

    if not token:
        return None

    try:

        response = requests.get(

            TDX_POSITION_URL,

            headers={
                "Authorization":
                f"Bearer {token}"
            },

            timeout=12
        )

        # Token 過期
        if response.status_code == 401:

            global _watchdog_token_expire_at

            _watchdog_token_expire_at = 0

            token = _get_watchdog_token()

            if not token:
                return None

            response = requests.get(

                TDX_POSITION_URL,

                headers={
                    "Authorization":
                    f"Bearer {token}"
                },

                timeout=12
            )

        if response.status_code != 200:

            print(
                "⚠️ TDX 即時定位查詢失敗："
                f"HTTP {response.status_code}",
                flush=True
            )

            return None

        data = response.json()

        if not isinstance(data, list):
            return []

        return data

    except Exception as e:

        print(
            f"⚠️ TDX 定位查詢錯誤：{e}",
            flush=True
        )

        return None


def _watchdog_once(local_dir):

    positions_file = os.path.join(
        local_dir,
        "positions.json"
    )

    meta_file = os.path.join(
        local_dir,
        "meta.json"
    )

    lock_file = os.path.join(
        local_dir,
        WATCHDOG_LOCK_FILE
    )

    try:

        os.makedirs(
            local_dir,
            exist_ok=True
        )

        # -------------------------------------------------
        # 判斷 positions.json 是否太舊
        # -------------------------------------------------

        try:

            age = (
                time.time()
                - os.path.getmtime(
                    positions_file
                )
            )

        except OSError:

            age = float("inf")

        # 資料還很新，不需要做任何事情
        if age <= WATCHDOG_STALE_SECONDS:
            return

        # -------------------------------------------------
        # 搶鎖
        # -------------------------------------------------

        fp = open(
            lock_file,
            "w"
        )

        try:

            fcntl.flock(
                fp.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB
            )

        except BlockingIOError:

            fp.close()

            return

        try:

            # 搶到鎖後再確認一次
            try:

                age = (
                    time.time()
                    - os.path.getmtime(
                        positions_file
                    )
                )

            except OSError:

                age = float("inf")

            if age <= WATCHDOG_STALE_SECONDS:
                return

            # -------------------------------------------------
            # 向 TDX 抓即時 GPS
            # -------------------------------------------------

            positions = _fetch_positions()

            if positions is None:
                return

            # -------------------------------------------------
            # 按路線分類
            # -------------------------------------------------

            by_route = {}

            for bus in positions:

                route = (
                    bus
                    .get("RouteName", {})
                    .get("Zh_tw", "")
                )

                if not route:
                    continue

                by_route.setdefault(
                    route,
                    []
                ).append(bus)

            # -------------------------------------------------
            # 寫入 JSON
            # -------------------------------------------------

            now = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime()
            )

            temp_positions = (
                positions_file
                + ".tmp"
            )

            temp_meta = (
                meta_file
                + ".tmp"
            )

            with open(
                temp_positions,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    by_route,
                    f,
                    ensure_ascii=False
                )

            with open(
                temp_meta,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    {
                        "updated_at": now,
                        "last_attempt": now,
                        "last_attempt_ok":
                            bool(positions),
                        "position_source":
                            "watchdog"
                    },
                    f,
                    ensure_ascii=False
                )

            # 原子替換
            os.replace(
                temp_positions,
                positions_file
            )

            os.replace(
                temp_meta,
                meta_file
            )

            print(
                "🔧 GPS watchdog 已補抓 TDX："
                f"{len(positions)} 台公車，"
                f"{len(by_route)} 條路線",
                flush=True
            )

        finally:

            try:

                fcntl.flock(
                    fp.fileno(),
                    fcntl.LOCK_UN
                )

            except Exception:
                pass

            fp.close()

    except Exception as e:

        print(
            f"⚠️ GPS watchdog 錯誤：{e}",
            flush=True
        )


# =========================================================
# 啟動 GPS WATCHDOG
# =========================================================

def start_realtime_watchdog(local_dir):

    def loop():

        # 等主程式先初始化
        time.sleep(8)

        while True:

            try:

                _watchdog_once(
                    local_dir
                )

            except Exception as e:

                print(
                    "⚠️ GPS watchdog "
                    f"迴圈錯誤：{e}",
                    flush=True
                )

            time.sleep(
                WATCHDOG_INTERVAL
            )

    thread = threading.Thread(

        target=loop,

        name="realtime-gps-watchdog",

        daemon=True
    )

    thread.start()

    print(
        "✅ GPS realtime watchdog 已啟動",
        flush=True
    )

    return thread
