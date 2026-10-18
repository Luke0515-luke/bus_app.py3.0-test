import os
import json
import math
import time
import uuid
import concurrent.futures
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

try:
    from groq import Groq
except ImportError:
    Groq = None

from pull_backup import pull_backup
from push_backup import git_push_backup
from realtime_sync import pull_realtime_backup, push_realtime_backup
import asyncio
import threading
import shutil
import fcntl
import calendar
import gc

load_dotenv()

# 即時公車資料（定位／到站預估）快照的存放位置。跟路線 Shape/StopOfRoute 用的
# /opt/render/project/data 是完全獨立的資料夾與獨立的 git 狀態（獨立分支），
# 兩邊互不影響。
REALTIME_DATA_DIR = "/opt/render/project/realtime"

def create_app():
    import traceback
    traceback.print_stack()   # 印出是哪一行呼叫了 create_app
    pull_backup()
    pull_realtime_backup(REALTIME_DATA_DIR)
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
    return app
app = create_app()

# ── 環境變數 / 認證資訊 ───────────────────────────────────
app_id = os.environ.get("CLIENT_ID")
app_key = os.environ.get("CLIENT_SECRET")
groq_api_key = os.environ.get("GROQ_API_KEY")

client = None
if Groq and groq_api_key:
    try:
        client = Groq(api_key=groq_api_key)
    except Exception:
        client = None
        print("找不到 GROQ_API_KEY，AI 功能將受限。")
else:
    print("找不到 GROQ_API_KEY，AI 功能將受限。")

AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TAINAN_LAT, TAINAN_LON = 22.9997, 120.2270
UBIKE_MIN_PER_KM = 10  # 使用者指定：騎 UBike 的時間概估用「1 公里約 10 分鐘」計算
# 路線原始資料（站牌 StopOfRoute ＋ 軌跡 Shape）的正式儲存位置。
# 這個資料夾在 /opt/render/project/data 底下，會被排程每 10 分鐘備份到 GitHub 的
# backup 分支，即使 Render 重啟、清空硬碟，資料也不會不見——不再使用會消失在
# Render 硬碟根目錄、不會被備份的暫存快取檔。
ROUTE_DATA_SAVE_DIR = "/opt/render/project/data/route"
_route_file_lock = threading.Lock()

# 使用者帳號資料（登入系統）。
# 正式儲存位置改成 Supabase 的 users 資料表（id、created_at、username、password_hash），
# 不再依賴本機檔案＋排程備份到 GitHub 那一套——帳號密碼雜湊值本來就不該進 git 歷史紀錄，
# 就算是雜湊過的也一樣，用真正的資料庫存放才是正確作法。
# USERS_FILE／_load_users／_save_users 保留下來，純粹是給「還沒設定 Supabase 環境變數時」
# 的本機備援用（例如本機開發環境），以及把舊帳號一次性搬去 Supabase 時當作資料來源。
USERS_FILE = "/opt/render/project/data/users.json"
_users_lock = threading.Lock()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
# 很多人會把 Supabase 的網址複製成連 /rest/v1 都帶進來（例如從 Connect 對話框、
# 或某些教學文章複製的是完整 REST 端點，不是單純的 Project URL）。我們自己組網址
# 時會再補一次 /rest/v1/users，如果環境變數本身已經帶了 /rest/v1，就會兜成
# .../rest/v1/rest/v1/users 這種重複路徑，PostgREST 會回傳 PGRST125
# 「Invalid path specified in request URL」——這裡自動把常見的尾綴修掉，
# 不管貼的是哪一種網址格式都能正常運作。
for _suffix in ("/rest/v1", "/rest"):
    if SUPABASE_URL.endswith(_suffix):
        SUPABASE_URL = SUPABASE_URL[: -len(_suffix)]
        break
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if SUPABASE_URL:
    print(f"ℹ️ Supabase 設定：實際會呼叫的 REST 端點是 {SUPABASE_URL}/rest/v1/users"
          "（如果這個網址看起來不對，檢查一下 Render 上 SUPABASE_URL 這個環境變數）。")


def _supabase_enabled():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def _supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _load_users():
    """舊版本機備援：讀取 /opt/render/project/data/users.json。
    只有在 Supabase 沒有設定好環境變數時才會用到（見 get_user_record／create_user_record）。"""
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_users(users):
    with _users_lock:
        try:
            os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 寫入使用者資料失敗：{e}")


def _supabase_get_user(username):
    """向 Supabase 的 users 資料表用 REST API（PostgREST）查詢帳號。
    回傳 (使用者資料或 None, 錯誤說明或 None)——刻意把詳細錯誤原因也一起回傳，
    而不是只印在 log 裡，這樣呼叫端（尤其是手動搬遷用的 admin 端點）才能直接把
    Supabase 真正回應的錯誤內容（HTTP 狀態碼、錯誤訊息）秀出來，不用另外去翻 log。
    直接用 requests 打 REST 端點，不額外依賴 supabase 這個 pip 套件，
    跟專案裡其他呼叫外部 API（TDX、Groq）的寫法一致，部署時也不用多裝東西。"""
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/users",
            headers=_supabase_headers(),
            params={"username": f"eq.{username}", "select": "*", "limit": 1},
            timeout=8)
        if res.status_code == 200:
            rows = res.json()
            return (rows[0] if rows else None), None
        detail = f"HTTP {res.status_code}：{res.text[:300]}"
        print(f"⚠️ Supabase 查詢使用者失敗：{detail}")
        return None, detail
    except Exception as e:
        detail = str(e)
        print(f"⚠️ Supabase 查詢使用者失敗：{detail}")
        return None, detail


def _supabase_create_user(username, password_hash):
    """新增一筆帳號到 Supabase 的 users 資料表。id、created_at 讓資料庫自己產生
    （identity 欄位＋預設值 now()），這裡只需要送 username／password_hash。
    回傳 (是否成功, 錯誤說明或 None)，理由同上——把 Supabase 實際回應的錯誤內容
    往上帶，方便直接從 API 回應判斷問題（常見的像是金鑰錯誤、資料表權限、
    username 違反 UNIQUE 限制等等，錯誤訊息裡通常會講清楚）。"""
    try:
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/users",
            headers=_supabase_headers(),
            json={"username": username, "password_hash": password_hash},
            timeout=8)
        if res.status_code in (200, 201):
            return True, None
        detail = f"HTTP {res.status_code}：{res.text[:300]}"
        print(f"⚠️ Supabase 新增使用者失敗：{detail}")
        return False, detail
    except Exception as e:
        detail = str(e)
        print(f"⚠️ Supabase 新增使用者失敗：{detail}")
        return False, detail


def get_user_record(username):
    """查詢帳號資料：Supabase 有設定好就查 Supabase，沒有設定的話（例如本機開發環境）
    退回讀本機 users.json，帳號功能還是能正常運作，不會因為少了 Supabase 金鑰就整個掛掉。"""
    if _supabase_enabled():
        row, _err = _supabase_get_user(username)
        return row
    return _load_users().get(username)


def create_user_record(username, password_hash):
    """新增帳號：邏輯跟 get_user_record 對稱，一樣是 Supabase 優先、本機檔案當備援。"""
    if _supabase_enabled():
        ok, _err = _supabase_create_user(username, password_hash)
        return ok
    users = _load_users()
    users[username] = {
        "password_hash": password_hash,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_users(users)
    return True


def _migrate_local_users_to_supabase():
    """把舊版存在本機 users.json 裡、Supabase 上線前就已經註冊的帳號，一次性搬到
    Supabase 的 users 資料表。只在有設定 Supabase 環境變數時才會執行；每個帳號都會
    先用 username 查一次是否已經存在，存在就跳過（不會覆蓋 Supabase 上已有的密碼），
    確保重複執行（例如伺服器重啟、多個 worker process 各自啟動一次）也不會出錯或
    搬出重複帳號。搬完之後，新註冊的帳號一律直接寫進 Supabase，不會再用到本機檔案。

    每個分支都會印出訊息（不管有沒有東西可搬、成功或失敗），刻意不要「靜默跳過」——
    之前『本機沒有帳號資料』的情況完全不會印任何東西，導致沒辦法從 Render 的 log
    分辨到底是「本來就沒有舊帳號」還是「搬遷根本沒執行到」，除錯很困難。
    失敗的話會把 Supabase 實際回應的錯誤內容一起收集進 errors，直接回傳給呼叫端
    （/api/admin/migrate_users），不用另外去翻 log 才找得到真正的失敗原因。"""
    if not _supabase_enabled():
        print("ℹ️ 舊帳號搬遷：Supabase 尚未設定（SUPABASE_URL / SUPABASE_SERVICE_KEY），略過。")
        return {"ran": False, "reason": "supabase_not_configured"}
    local_users = _load_users()
    if not local_users:
        print(f"ℹ️ 舊帳號搬遷：本機找不到帳號資料（{USERS_FILE} 不存在或是空的），沒有東西可以搬。"
              "如果你確定之前有註冊過帳號，可能是 Render 的磁碟在這次部署時被清空、"
              "或是備份還原（pull_backup）還沒跑完就先執行到這裡了。")
        return {"ran": True, "local_count": 0, "migrated": 0, "skipped": 0, "failed": 0, "errors": []}
    print(f"ℹ️ 舊帳號搬遷：本機找到 {len(local_users)} 個帳號，開始搬到 Supabase...")
    migrated, skipped, failed = 0, 0, 0
    errors = []
    for username, info in local_users.items():
        pw_hash = (info or {}).get("password_hash")
        if not pw_hash:
            failed += 1
            errors.append({"username": username, "reason": "本機資料裡缺少 password_hash"})
            continue
        existing, get_err = _supabase_get_user(username)
        if existing:
            skipped += 1
            continue
        if get_err:
            failed += 1
            errors.append({"username": username, "reason": f"查詢是否已存在時失敗：{get_err}"})
            continue
        ok, create_err = _supabase_create_user(username, pw_hash)
        if ok:
            migrated += 1
        else:
            failed += 1
            errors.append({"username": username, "reason": create_err or "未知錯誤"})
    print(f"✅ 舊帳號搬遷到 Supabase 完成：新增 {migrated} 個、已存在略過 {skipped} 個、失敗 {failed} 個。")
    for e in errors[:5]:
        print(f"   - {e['username']}：{e['reason']}")
    return {"ran": True, "local_count": len(local_users), "migrated": migrated,
            "skipped": skipped, "failed": failed, "errors": errors[:10]}


# 應用程式啟動時就跑一次舊帳號搬遷（見上面函式說明）。包一層 try/except，
# 就算 Supabase 一時連不上，也不能讓整個網站因此啟動失敗。
try:
    _migrate_local_users_to_supabase()
except Exception as e:
    print(f"⚠️ 搬遷舊帳號到 Supabase 時發生錯誤（不影響網站正常啟動）：{e}")

if not _supabase_enabled():
    print("ℹ️ 尚未設定 SUPABASE_URL / SUPABASE_SERVICE_KEY，帳號登入功能暫時使用本機檔案儲存"
          "（/opt/render/project/data/users.json），正式環境建議設定好 Supabase 環境變數。")


def _route_stop_file_path(route_name):
    return os.path.join(ROUTE_DATA_SAVE_DIR, f"{route_name}_route_stop.json")


def _route_shape_file_path(route_name):
    return os.path.join(ROUTE_DATA_SAVE_DIR, f"{route_name}_route_shape.json")


def _route_timetable_file_path(route_name):
    return os.path.join(ROUTE_DATA_SAVE_DIR, f"{route_name}_route_timetable.json")


def _save_route_json(path, data):
    """把某路線的 TDX 原始 JSON 寫進 /opt/render/project/data/route，
    會跟著現有的排程一起被備份到 GitHub，不會因為 Render 重啟而消失。"""
    with _route_file_lock:
        try:
            os.makedirs(ROUTE_DATA_SAVE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 寫入路線資料失敗（{path}）：{e}")


def _entry_route_name(entry):
    """從 TDX 回傳的單筆資料（StopOfRoute 或 Shape 的其中一筆）取出真正的路線名稱。"""
    try:
        return (entry.get("RouteName") or {}).get("Zh_tw", "")
    except Exception:
        return ""


def _filter_route_entries(data, route_name):
    """只保留『真的屬於 route_name 這條路線』的資料。
    TDX 有些端點對路線名稱是用「包含比對」而不是完全比對，例如查詢路線「0」時，
    可能會把名稱裡有「0」的其他路線（10、70右、0左…）的站牌／軌跡資料也一併回傳，
    如果不過濾就直接存檔，就會出現『存到其他路線』的錯誤資料。
    這裡強制比對 RouteName 是否與查詢的 route_name 完全相同，不符的一律捨棄。"""
    if not isinstance(data, list):
        return data
    filtered = [d for d in data if isinstance(d, dict) and _entry_route_name(d) == route_name]
    return filtered


# ══════════════════════════════════════════════════════════
# TDX 每月用量控管（免費額度 3000 次／月，這裡把上限壓在 2800 次以下）
# ══════════════════════════════════════════════════════════
# 所有對 TDX「資料 API」的請求都必須經過 tdx_get()，tdx_get() 每發一次請求就在這裡
# 記一筆（失敗、逾時也算，因為 TDX 是用請求次數計費）。計數存在
# /opt/render/project/data/tdx_usage.json（會跟著原本的備份機制一起備份），
# 每個月自動歸零，多個 worker process 之間用 flock 共用同一個計數。
DATA_DIR = "/opt/render/project/data"
TDX_MONTHLY_CAP = 2800          # 絕對上限：靜態資料（每月 1 號更新）最多可用到這裡
TDX_DYNAMIC_CAP = 2780          # 即時資料（到站／定位／UBike）只用到這裡，留 20 次給靜態資料補抓
TDX_MANUAL_DAILY_LIMIT = 10     # 網頁上手動按鈕（單一路線重抓等）每天最多能打幾次 TDX
TDX_USAGE_FILE = os.path.join(DATA_DIR, "tdx_usage.json")
TDX_USAGE_LOCK_FILE = os.path.join(DATA_DIR, ".tdx_usage.lock")
_usage_thread_lock = threading.Lock()


def _usage_blank(month):
    return {"month": month, "count": 0, "by_kind": {}, "by_source": {}, "by_day": {}, "manual_day": "", "manual_count": 0}


def _tdx_usage_txn(fn):
    """在 flock 保護下「讀取 → 修改 → 寫回」用量計數檔，fn(usage) 的回傳值會原樣回傳。"""
    with _usage_thread_lock:
        lock_fp = None
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            lock_fp = open(TDX_USAGE_LOCK_FILE, "w")
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        except Exception:
            lock_fp = None
        try:
            month = _taipei_now().strftime("%Y-%m")
            try:
                with open(TDX_USAGE_FILE, "r", encoding="utf-8") as f:
                    usage = json.load(f)
                if not isinstance(usage, dict) or usage.get("month") != month:
                    usage = _usage_blank(month)
            except (FileNotFoundError, json.JSONDecodeError):
                usage = _usage_blank(month)
            result = fn(usage)
            tmp = TDX_USAGE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(usage, f, ensure_ascii=False)
            os.replace(tmp, TDX_USAGE_FILE)
            return result
        finally:
            if lock_fp is not None:
                try:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                lock_fp.close()


def tdx_budget_consume(kind, source=None):
    """發 TDX 請求「之前」呼叫：還有額度就扣一次並回傳 True，沒額度回傳 False（呼叫端就不能發請求）。
    kind：'static'（每月靜態資料）、'dynamic'（到站／定位／UBike 排程）、'manual'（網頁手動按鈕）。"""
    def txn(u):
        cap = TDX_MONTHLY_CAP if kind == "static" else TDX_DYNAMIC_CAP
        if u["count"] >= cap:
            return False
        if kind == "manual":
            today = _taipei_now().strftime("%Y-%m-%d")
            if u.get("manual_day") != today:
                u["manual_day"], u["manual_count"] = today, 0
            if u["manual_count"] >= TDX_MANUAL_DAILY_LIMIT:
                return False
            u["manual_count"] += 1
        u["count"] += 1
        u["by_kind"][kind] = u["by_kind"].get(kind, 0) + 1
        # 儀表板用：依來源（eta／ubike／positions／static／manual）與依日期再各記一份
        src = source or kind
        bs = u.setdefault("by_source", {})
        bs[src] = bs.get(src, 0) + 1
        bd = u.setdefault("by_day", {})
        day_key = _taipei_now().strftime("%d")
        bd[day_key] = bd.get(day_key, 0) + 1
        return True
    try:
        return _tdx_usage_txn(txn)
    except Exception as e:
        # 計數檔讀寫壞掉時寧可讓服務繼續（只印警告），不要因此整個停擺
        print(f"⚠️ TDX 用量計數檔讀寫失敗（本次請求照常放行）：{e}", flush=True)
        return True


def tdx_usage_snapshot():
    """回傳目前這個月的用量（唯讀），給排程節流、管理端點用。"""
    try:
        return _tdx_usage_txn(lambda u: json.loads(json.dumps(u)))
    except Exception:
        return _usage_blank(_taipei_now().strftime("%Y-%m"))


def _pacing_allows_optional_poll(now=None, margin=150):
    """非尖峰（離峰／週末）的輪詢才需要過這一關：把整個月的額度按日期比例攤開，
    如果目前已用量超過「這個時間點該有的進度 ＋ 150 次緩衝」，就先跳過這一輪，
    等進度追回來再抓。尖峰時段不受影響。這是保證整個月不會爆量的第二道保險
    （第一道是 tdx_budget_consume 的硬上限）。"""
    now = now or _taipei_now()
    used = tdx_usage_snapshot().get("count", 0)
    dim = calendar.monthrange(now.year, now.month)[1]
    frac = ((now.day - 1) + (now.hour * 60 + now.minute) / 1440.0) / dim
    return used <= TDX_MONTHLY_CAP * frac + margin


# ══════════════════════════════════════════════════════════
# 靜態資料：每月 1 號用「全台南一次查詢」抓齊，存成檔案，平常只讀檔
# ══════════════════════════════════════════════════════════
# 公車路線軌跡（Shape）、站牌順序與座標（StopOfRoute）、固定時刻表（Schedule）、
# 全部站牌（Stop，附近站牌用）、路線清單（Route）、UBike 站點（Bike/Station）。
# 以前是「每條路線各查一次」（150 幾條 × 3 種 ＝ 好幾百次），現在改成每種資料
# 各查一次整個台南（共 6 次），再依路線名稱拆成原本的 {路線}_route_stop.json 等檔案，
# 讀取端（load_route_*_data）完全不用改，只是不再於缺檔時即時打 TDX。
STATIC_META_FILE = os.path.join(ROUTE_DATA_SAVE_DIR, "_static_meta.json")
ALL_BUS_STOPS_FILE = os.path.join(ROUTE_DATA_SAVE_DIR, "_all_bus_stops.json")
ALL_ROUTE_META_FILE = os.path.join(ROUTE_DATA_SAVE_DIR, "_all_route_meta.json")
UBIKE_STATIONS_FILE = os.path.join(ROUTE_DATA_SAVE_DIR, "_ubike_stations.json")
UBIKE_AVAIL_FILE = os.path.join(DATA_DIR, "ubike_avail.json")
# False：站牌／軌跡只存「ALL_ROUTE_NAMES＋已存過＋小黃公車」的路線（跟舊版地圖清單一致）；
# True：TDX 回傳的每一條路線都存（地圖路線清單會多出系統分類表沒列的路線）。
SAVE_ALL_ROUTES = False
STATIC_MAX_ATTEMPTS_PER_DAY = 3     # 靜態資料某天最多嘗試補抓幾輪，避免失敗時一直燒額度
STATIC_FORCE_COOLDOWN_SECONDS = 24 * 3600  # 手動「更新全台南站點快取」最短間隔
_static_refresh_lock = threading.Lock()
_json_file_cache = {}

_STATIC_DATASETS = {
    # 名稱: (TDX 路徑, timeout)
    "stop": ("Bus/StopOfRoute/City/Tainan", 90),
    "shape": ("Bus/Shape/City/Tainan", 90),
    "schedule": ("Bus/Schedule/City/Tainan", 90),
    "bus_stops": ("Bus/Stop/City/Tainan", 60),
    "route_meta": ("Bus/Route/City/Tainan", 60),
    "ubike_stations": ("Bike/Station/City/Tainan", 30),
}


def _atomic_write_json(path, obj, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def _read_json_cached(path, default):
    """讀 JSON 檔；檔案沒變（修改時間相同）就直接用記憶體裡的，不重複解析大檔。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return default
    hit = _json_file_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    _json_file_cache[path] = (mtime, data)
    return data


def _tdx_get_json(path, timeout, kind):
    url = f"https://tdx.transportdata.tw/api/basic/v2/{path}?%24format=JSON"
    res = tdx_get(url, timeout=timeout, retries=0, kind=kind)
    if res is None:
        return None
    try:
        data = res.json()
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _group_entries_by_route(data):
    grouped = {}
    for entry in data or []:
        if isinstance(entry, dict):
            name = _entry_route_name(entry)
            if name:
                grouped.setdefault(name, []).append(entry)
    return grouped


def _yellow_bus_route_names(route_meta):
    names = set()
    for r in route_meta or []:
        name = (r.get("RouteName") or {}).get("Zh_tw", "")
        ops = [(op.get("OperatorName") or {}).get("Zh_tw", "") for op in (r.get("Operators") or [])]
        if name and any(any(kw in on for kw in _YELLOW_BUS_OPERATOR_KEYWORDS) for on in ops):
            names.add(name)
    return names


def _static_dataset_present(name):
    """檔案還在不在（Render 清碟、備份沒還原時，meta 說做完了但檔案其實沒了，要能偵測到）。"""
    try:
        files = os.listdir(ROUTE_DATA_SAVE_DIR)
    except FileNotFoundError:
        return False
    if name == "stop":
        return any(f.endswith("_route_stop.json") for f in files)
    if name == "shape":
        return any(f.endswith("_route_shape.json") for f in files)
    if name == "schedule":
        return any(f.endswith("_route_timetable.json") for f in files)
    single = {"bus_stops": ALL_BUS_STOPS_FILE, "route_meta": ALL_ROUTE_META_FILE,
              "ubike_stations": UBIKE_STATIONS_FILE}
    return os.path.exists(single[name])


def refresh_static_data(force=False):
    """每月 1 號（或這個月第一次啟動、檔案不見時）把靜態資料一次抓齊，共 6 次 TDX 請求。
    - 平常（force=False）：本月已完成的資料集會自動略過，只補抓沒做完的；
      同一天最多嘗試 STATIC_MAX_ATTEMPTS_PER_DAY 輪，失敗也不會無限重試。
    - force=True（網頁上的「更新全台南站點快取」按鈕）：全部重抓，但 24 小時內只能按一次。
    回傳 dict，status 為 success / partial / skipped / cooldown / busy。"""
    if not _static_refresh_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        now = _taipei_now()
        month, today = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
        meta = _read_json_cached(STATIC_META_FILE, {}) or {}
        if meta.get("month") != month:
            meta = {"month": month, "done": [], "attempts": {}, "last_forced": meta.get("last_forced", 0)}
        done = set(meta.get("done", [])) if not force else set()
        done = {d for d in done if _static_dataset_present(d)}

        if force:
            if time.time() - float(meta.get("last_forced") or 0) < STATIC_FORCE_COOLDOWN_SECONDS:
                return {"status": "cooldown"}
            meta["last_forced"] = time.time()
        todo = [d for d in _STATIC_DATASETS if d not in done]
        if not todo:
            return {"status": "skipped", "reason": "本月已完成"}
        attempts = meta.setdefault("attempts", {})
        if not force:
            if attempts.get(today, 0) >= STATIC_MAX_ATTEMPTS_PER_DAY:
                return {"status": "skipped", "reason": "今天補抓次數已達上限"}
        attempts[today] = attempts.get(today, 0) + 1
        meta["attempts"] = {today: attempts[today]}   # 只留今天，檔案不要越長越大
        meta["done"] = sorted(done)
        _atomic_write_json(STATIC_META_FILE, meta)     # 先記錄「嘗試過了」，程式中途掛掉也不會狂重試

        print(f"🗓️ 靜態資料更新開始（{month}），要抓：{'、'.join(todo)}", flush=True)
        fetched = {}
        for name in todo:
            path, timeout = _STATIC_DATASETS[name]
            data = _tdx_get_json(path, timeout, "static")
            if data:
                fetched[name] = data
            else:
                print(f"⚠️ 靜態資料「{name}」這一輪沒抓到（之後最多再補抓 {STATIC_MAX_ATTEMPTS_PER_DAY} 輪／天）", flush=True)

        # 路線清單最先處理：小黃公車名單要靠它判斷
        if "route_meta" in fetched:
            _atomic_write_json(ALL_ROUTE_META_FILE, fetched["route_meta"])
        if "bus_stops" in fetched:
            _atomic_write_json(ALL_BUS_STOPS_FILE, fetched["bus_stops"])
        if "ubike_stations" in fetched:
            _atomic_write_json(UBIKE_STATIONS_FILE, fetched["ubike_stations"])

        route_meta = fetched.get("route_meta") or _read_json_cached(ALL_ROUTE_META_FILE, [])
        if SAVE_ALL_ROUTES:
            wanted = None
        else:
            wanted = set(get_all_known_routes()) | _yellow_bus_route_names(route_meta)

        routes_saved = 0
        for name, path_fn in (("stop", _route_stop_file_path), ("shape", _route_shape_file_path),
                              ("schedule", _route_timetable_file_path)):
            if name not in fetched:
                continue
            grouped = _group_entries_by_route(fetched[name])
            count = 0
            for route, entries in grouped.items():
                # 時刻表每條路線都存（小黃公車等需要）；站牌／軌跡依 wanted 篩選
                if name != "schedule" and wanted is not None and route not in wanted:
                    continue
                _save_route_json(path_fn(route), entries)
                count += 1
            if name == "stop":
                routes_saved = count
            print(f"   ✔ {name}：寫入 {count} 條路線的檔案", flush=True)

        done |= set(fetched.keys())
        meta["done"] = sorted(done)
        _atomic_write_json(STATIC_META_FILE, meta)

        _cache_store.clear()
        _stop_route_index_cache["data"] = None
        _stop_route_index_cache["time"] = 0
        ok = len(done) == len(_STATIC_DATASETS)
        print(f"{'✅' if ok else '⚠️'} 靜態資料更新結束：完成 {len(done)}/{len(_STATIC_DATASETS)} 項", flush=True)
        return {"status": "success" if ok else "partial", "routes_saved": routes_saved,
                "done": sorted(done), "missing": [d for d in _STATIC_DATASETS if d not in done]}
    finally:
        _static_refresh_lock.release()


def _fetch_and_save_stop_data(route_name):
    """（手動單一路線重抓用）向 TDX 查某路線的 StopOfRoute，驗證路線名稱後存檔。
    只給 /api/save_route_data 用，算『手動』額度（每天上限 TDX_MANUAL_DAILY_LIMIT 次）。"""
    data = _tdx_get_json(f"Bus/StopOfRoute/City/Tainan/{route_name}", 15, "manual")
    data = _filter_route_entries(data, route_name) if data else None
    if data:
        _save_route_json(_route_stop_file_path(route_name), data)
    return data


def _fetch_and_save_shape_data(route_name):
    """（手動單一路線重抓用）向 TDX 查某路線的 Shape，驗證路線名稱後存檔。"""
    data = _tdx_get_json(f"Bus/Shape/City/Tainan/{route_name}", 15, "manual")
    data = _filter_route_entries(data, route_name) if data else None
    if data:
        _save_route_json(_route_shape_file_path(route_name), data)
    return data


def _load_route_file(path, route_name):
    """只讀存檔，絕不即時打 TDX。檔案不存在、損毀、或內容不屬於這條路線都回傳 []。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    valid = _filter_route_entries(data, route_name)
    return valid or []


def load_route_stop_data(route_name):
    """讀取已存檔的 StopOfRoute（每月 1 號更新），不會打 TDX。"""
    return _load_route_file(_route_stop_file_path(route_name), route_name)


def load_route_shape_data(route_name):
    """讀取已存檔的 Shape（每月 1 號更新），不會打 TDX。"""
    return _load_route_file(_route_shape_file_path(route_name), route_name)


def load_route_timetable_data(route_name):
    """讀取已存檔的固定時刻表（每月 1 號更新），不會打 TDX。"""
    return _load_route_file(_route_timetable_file_path(route_name), route_name)


def _cleanup_known_bad_route_files():
    """開機時清掉已知的壞檔：路線名稱字面上是「0」的存檔（0_route_stop.json / 0_route_shape.json /
    0_route_timetable.json）。系統裡從來沒有一條路線正式名稱就叫「0」（實際上是「0左」「0右」），
    這個檔案如果存在，幾乎可以確定是之前查詢時把其他路線的資料混進來、存錯的殘留檔案。"""
    for bad_name in ("0",):
        for path in (_route_stop_file_path(bad_name), _route_shape_file_path(bad_name),
                     _route_timetable_file_path(bad_name)):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    print(f"🗑️ 已刪除錯誤殘留檔案：{path}")
            except Exception as e:
                print(f"⚠️ 刪除殘留檔案失敗（{path}）：{e}")


def _invalidate_route_cache(route_name):
    """清掉某條路線在記憶體暫存（in-memory cache）裡的舊資料，
    讓下一次查詢立即讀到剛存好的檔案。"""
    for fn in ("fetch_route_stops", "fetch_route_shape", "fetch_route_stop_positions", "fetch_route_schedule"):
        _cache_store.pop(f"{fn}:({route_name!r},):{{}}", None)
    _stop_route_index_cache["data"] = None
    _stop_route_index_cache["time"] = 0


_cleanup_known_bad_route_files()

# ── 常數與對照表（與原始 Streamlit 版本完全一致） ─────────
ROUTE_CATEGORIES = {
    "黃線": ["黃幹線", "黃1", "黃2", "黃3", "黃4", "黃5", "黃6", "黃6-1", "黃7", "黃9", "黃10", "黃11", "黃11-1", "黃12", "黃13", "黃14", "黃14-1", "黃15", "黃16", "黃20", "黃22", "黃23", "黃24", "黃25"],
    "棕線": ["棕幹線", "棕1", "棕2", "棕3", "棕3-1", "棕4", "棕5", "棕6", "棕20", "棕10", "棕11"],
    "綠線": ["綠幹線", "綠1", "綠2", "綠2-1", "綠3", "綠4", "綠5", "綠6", "綠7", "綠10", "綠11", "綠12", "綠12-1", "綠12-2", "綠13", "綠14", "綠15", "綠16", "綠17", "綠20", "綠20-1", "綠21", "綠22", "綠23", "綠24", "綠25", "綠26", "綠27", "綠28", "綠29", "綠30", "綠30-1", "綠31", "綠32"],
    "橘線": ["橘幹線", "橘1", "橘2", "橘3", "橘4", "橘4-1", "橘5", "橘6", "橘9", "橘9-1", "橘10", "橘10-1", "橘11", "橘11-1", "橘12", "橘13", "橘14", "橘20"],
    "藍線": ["藍幹線", "藍1", "藍2", "藍3", "藍4", "藍10", "藍11", "藍13", "藍14", "藍15", "藍20", "藍21", "藍22", "藍23", "藍24", "藍25", "藍26", "藍27", "藍28", "藍29", "藍30"],
    "紅線": ["紅幹線", "紅1", "紅2", "紅3", "紅4", "紅10", "紅11", "紅12", "紅13", "紅14"],
    "市區": ["0左", "0右", "6", "7", "9", "10", "11", "14", "15", "18", "19", "20", "21", "31", "32", "33 關子嶺線", "62", "70左", "70右", "77", "98", "101", "102", "103", "107", "111", "168 虎埤老街線", "901", "902", "904", "905"],
    "高鐵快捷": ["H31"],
    "觀光": ["東山咖啡線", "梅嶺線", "菱波官田線", "雙層巴士"]
}

ROUTE_COLOR_MAP = {
    "黃": "#F1C40F", "棕": "#8B4513", "綠": "#27AE60", "橘": "#E67E22",
    "藍": "#2980B9", "紅": "#E74C3C", "H": "#9B59B6",
    "0": "#1ABC9C",
    "6": "#E91E63", "7": "#E91E63", "9": "#E91E63",
    "10": "#FF5722", "11": "#FF5722", "14": "#FF5722", "15": "#FF5722",
    "18": "#FF9800", "19": "#FF9800", "20": "#FF9800", "21": "#FF9800",
    "31": "#795548", "32": "#795548", "33 關子嶺線": "#795548",
    "62": "#607D8B", "70": "#3F51B5", "77": "#009688", "98": "#F44336",
    "101": "#673AB7", "102": "#673AB7", "103": "#673AB7", "107": "#673AB7",
    "111": "#00BCD4", "168 虎埤老街線": "#00BCD4",
    "901": "#8BC34A", "902": "#8BC34A", "904": "#8BC34A", "905": "#8BC34A",
    "東山": "#FF6F00", "梅嶺": "#AD1457", "菱波": "#00838F", "雙層": "#BF360C",
}

async def backup():
        try:
            # 備份來源與儲存位置
            source_folder = '/opt/render/project/data'
            backup_folder = '/opt/render/project/backups'

            os.makedirs(backup_folder, exist_ok=True)

            # 備份檔案路徑
            now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_filename = f"backup_{now}.zip"
            backup_path = os.path.join(backup_folder, backup_filename)

            # 壓縮成 zip
            #shutil.make_archive(backup_path.replace(".zip", ""), 'zip', source_folder)
            await asyncio.to_thread(
            shutil.make_archive, 
            backup_path.replace(".zip", ""),       # 對應原本的 backup_path.replace(".zip", "")
            'zip',           # 壓縮格式
            source_folder    # 來源資料夾
            )

            print(f"✅ 自動備份完成：{backup_path}")

            # 保留最新10個備份
            backups = sorted(
                [f for f in os.listdir(backup_folder) if f.endswith('.zip')],
                key=lambda f: os.path.getmtime(os.path.join(backup_folder, f)),
                reverse=True  # 最新的在前
            )
            for old_backup in backups[10:]:
                old_path = os.path.join(backup_folder, old_backup)
                os.remove(old_path)
                print(f"🗑️ 已刪除過舊備份：{old_backup}")

            git_push_backup(source_folder)
        except Exception as e:
            print(f"❌ 備份失敗: {e}")

async def backup_loop():
    """每 3 小時執行備份的非同步迴圈"""
    while True:
        await backup()
        await asyncio.sleep(10 * 60)

def run_scheduler():
    """在獨立執行緒中跑 asyncio 事件迴圈"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(backup_loop())

# 啟動排程執行緒
scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()


# 所有設定過的路線（去重、保留順序），用來確保地圖「未篩選」時每條路線都會被繪製，
# 不會因為該路線目前沒有營運中的公車而被漏掉。
ALL_ROUTE_NAMES = []
_seen_route_names = set()
for _rl in ROUTE_CATEGORIES.values():
    for _r in _rl:
        if _r not in _seen_route_names:
            _seen_route_names.add(_r)
            ALL_ROUTE_NAMES.append(_r)

def get_saved_route_names():
    """掃描 /opt/render/project/data/route，回傳『站牌（StopOfRoute）與軌跡（Shape）
    兩份資料都真的存在』的路線名稱（不管是不是在系統設定的 ROUTE_CATEGORIES 裡）。
    兩者缺一都不算——只有其中一份存在，代表地圖顯示時另一份還是得即時向 TDX 查，
    不能顯示 💾 讓人誤以為這條路線已經完整存好、只需要查公車定位就好。"""
    has_stop, has_shape = set(), set()
    try:
        for fn in os.listdir(ROUTE_DATA_SAVE_DIR):
            if fn.endswith("_route_stop.json"):
                has_stop.add(fn[: -len("_route_stop.json")])
            elif fn.endswith("_route_shape.json"):
                has_shape.add(fn[: -len("_route_shape.json")])
    except FileNotFoundError:
        pass
    return has_stop & has_shape


def get_all_known_routes():
    """系統設定的全部路線（ALL_ROUTE_NAMES）＋ 實際上已經存檔的路線，去重合併。
    確保就算某條路線不在預設分類表裡，只要曾經存過資料，一樣會被畫在地圖上、
    出現在『已儲存路線』清單裡。"""
    combined = list(ALL_ROUTE_NAMES)
    seen = set(combined)
    for r in sorted(get_saved_route_names()):
        if r and r not in seen:
            seen.add(r)
            combined.append(r)
    return combined


# ── in-memory 快取（等同於 st.cache_data）────────────────
_cache_store = {}


def cached(ttl_seconds):
    def decorator(func):
        def wrapper(*args, **kwargs):
            key = f"{func.__name__}:{args}:{kwargs}"
            hit = _cache_store.get(key)
            if hit and time.time() - hit["time"] < ttl_seconds:
                return hit["data"]
            result = func(*args, **kwargs)
            _cache_store[key] = {"time": time.time(), "data": result}
            return result
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator


# ── in-memory 使用者 session store（等同於 st.session_state）──
SESSION_STORE = {}


def _default_state():
    return {
        "recent_routes": [],
        "favorite_routes": [],
        "reminders": [],
        "chat_sessions": {},
        "current_session_id": None,
        "current_weather": "尚未查詢",
        "bus_status": "尚未查詢路線",
    }


def get_uid():
    # 已登入的話用「使用者帳號」當作 key，這樣最愛路線／最近查詢／對話記錄
    # 才會綁定在帳號上，換裝置、換瀏覽器登入同一個帳號都看得到；
    # 沒登入則照舊用瀏覽器 session 產生的匿名 uid。
    if session.get("username"):
        uid = f"user:{session['username']}"
    else:
        if "uid" not in session:
            session["uid"] = str(uuid.uuid4())
        uid = session["uid"]
    if uid not in SESSION_STORE:
        SESSION_STORE[uid] = _default_state()
    return uid


def get_state():
    return SESSION_STORE[get_uid()]


def _login_user(username):
    """把目前瀏覽器的匿名資料（如果有的話）搬到帳號底下，再切換 session 成已登入狀態，
    這樣登入前查過的最愛／最近路線不會直接消失不見。"""
    old_uid = session.get("uid")
    session["username"] = username
    session.pop("uid", None)
    new_uid = f"user:{username}"
    if new_uid not in SESSION_STORE:
        SESSION_STORE[new_uid] = _default_state()
    if old_uid and old_uid in SESSION_STORE:
        old_state = SESSION_STORE[old_uid]
        new_state = SESSION_STORE[new_uid]
        if not new_state.get("favorite_routes") and old_state.get("favorite_routes"):
            new_state["favorite_routes"] = old_state["favorite_routes"]
        if not new_state.get("recent_routes") and old_state.get("recent_routes"):
            new_state["recent_routes"] = old_state["recent_routes"]
        if not new_state.get("reminders") and old_state.get("reminders"):
            new_state["reminders"] = old_state["reminders"]


# ── 基礎工具 ──────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def get_route_color(route_name):
    for prefix in sorted(ROUTE_COLOR_MAP.keys(), key=len, reverse=True):
        if route_name.startswith(prefix):
            return ROUTE_COLOR_MAP[prefix]
    return "#7F8C8D"


def get_osrm_bike_distance(start_lat, start_lon, end_lat, end_lon):
    """量測騎 UBike 的實際路網距離（公里），優先用 OSRM 算出的道路路徑距離
    （比直線距離準，會考慮繞路、單行道、河道等障礙），查不到才退回用直線距離概估。
    騎乘所需時間改用使用者指定的「1 公里約 10 分鐘」固定換算，不採用 OSRM 自己估的
    騎乘時間（那個估法通常偏樂觀，沒有考慮紅綠燈、牽車、找車柱等現實中的耗時）。"""
    try:
        url = (f"http://router.project-osrm.org/route/v1/bike/"
               f"{start_lon},{start_lat};{end_lon},{end_lat}?overview=false")
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            route = res.json()["routes"][0]
            dist_m = route["distance"]
            dist_km = dist_m / 1000
            dist_text = f"{dist_km:.1f} 公里" if dist_m >= 1000 else f"{round(dist_m)} 公尺"
            return dist_km, dist_text
    except Exception:
        pass
    return None, None


def parse_wkt_linestring(geo):
    points = []
    try:
        coords_str = geo.replace("LINESTRING (", "").replace("LINESTRING(", "").replace(")", "")
        for pair in coords_str.split(","):
            parts = pair.strip().split()
            if len(parts) >= 2:
                points.append([float(parts[1]), float(parts[0])])
    except Exception:
        pass
    return points


def add_recent_route(state, route):
    lst = state["recent_routes"]
    if route in lst:
        lst.remove(route)
    lst.insert(0, route)
    state["recent_routes"] = lst[:5]


def new_chat_session(state):
    sid = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    state["chat_sessions"][sid] = {
        "title": f"對話 {datetime.now().strftime('%m/%d %H:%M')}",
        "history": []
    }
    state["current_session_id"] = sid
    return sid


# ── TDX 認證 ──────────────────────────────────────────────
# ── TDX 認證 ──────────────────────────────────────────────
@cached(3600)
def get_tdx_token():
    try:
        res = requests.post(AUTH_URL, data={
            'content-type': 'application/x-www-form-urlencoded',
            'grant_type': 'client_credentials',
            'client_id': app_id,
            'client_secret': app_key
        }, timeout=10)
        return res.json().get("access_token", "")
    except Exception:
        return ""


_TOKEN_CACHE_KEY = "get_tdx_token:():{}"


def tdx_headers():
    return {'authorization': f'Bearer {get_tdx_token()}', 'Accept-Encoding': 'gzip'}


def tdx_get(url, timeout=8, retries=0, kind="dynamic", source=None):
    """整個程式『唯一』對 TDX 資料 API 發請求的出入口。每發一次請求（不管成功失敗）
    都先過 tdx_budget_consume() 扣一次月額度，額度用完就直接回傳 None、不發請求。
    retries 預設改成 0：重試也是一次計費的請求，3000 次額度很緊，失敗就等下一輪排程再抓。
    kind：'static'（每月靜態資料）／'dynamic'（排程即時資料）／'manual'（手動按鈕）。"""
    token = get_tdx_token()
    if not token:
        _cache_store.pop(_TOKEN_CACHE_KEY, None)   # 拿 token 失敗不要被快取住一個小時
        return None
    headers = {'authorization': f'Bearer {token}', 'Accept-Encoding': 'gzip'}
    for attempt in range(retries + 1):
        if not tdx_budget_consume(kind, source):
            print(f"⛔ TDX 本月額度已達上限（或手動額度用完），略過請求：{url.split('?')[0]}", flush=True)
            return None
        try:
            res = requests.get(url, headers=headers, timeout=timeout)
            if res.status_code == 200:
                return res
            if res.status_code == 401:
                _cache_store.pop(_TOKEN_CACHE_KEY, None)   # token 失效，下次重新取
                return None
        except Exception:
            pass
        if attempt < retries:
            time.sleep(0.4)
    return None


# ── TDX / 第三方資料存取（皆對應原本 st.cache_data 函數）───
@cached(3600)
def fetch_route_stops(route_name):
    data = load_route_stop_data(route_name)
    try:
        if data:
            return [s['StopName']['Zh_tw'] for s in data[0]['Stops']]
    except Exception:
        pass
    return []


def fetch_route_stops_by_direction(route_name, direction):
    """回傳某路線『指定方向』（去程=0／回程=1）的完整站序清單。
    跟 fetch_route_stops 不同：那支函式不管方向一律回傳 data[0]（TDX 通常把去程放在第一筆，
    但不保證每條路線都這樣）。如果查的是回程，卻拿去程的站序去跟『只過濾回程』的即時動態
    （EstimatedTimeOfArrival）逐站比對站名，兩邊站序、站名很多都對不上，會讓回程幾乎每一站
    都比對失敗、被誤判成『尚未發車』——這是『查詢常常顯示尚未發車，但實際上有車』的主因之一，
    所以查即時動態時一定要用這支，確保站序清單跟過濾出來的方向一致。"""
    data = load_route_stop_data(route_name)
    try:
        if data:
            for entry in data:
                if entry.get("Direction", 0) == direction:
                    return [s['StopName']['Zh_tw'] for s in entry.get('Stops', [])]
            # 少數路線的存檔資料裡找不到完全符合的方向，退回用第一筆，
            # 至少還能顯示站名，總比整條路線直接顯示「無法載入站點」好
            return [s['StopName']['Zh_tw'] for s in data[0]['Stops']]
    except Exception:
        pass
    return []


def fetch_bus_data_all():
    """一次性向 TDX 拿『全台南所有路線』的到站預估時間（含每站的車牌），只給後台排程呼叫。
    整個台南只算 1 次請求。失敗回傳 None（等下一輪排程，不即時重試、也不退回逐條路線查詢——
    那一輪就要一百多次請求，3000 次的月額度撐不住）。"""
    url = "https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/City/Tainan?%24format=JSON"
    res = tdx_get(url, timeout=30, retries=0, kind="dynamic", source="eta")
    if res is not None:
        try:
            data = res.json()
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return None


def fetch_bus_data(route_name):
    """回傳某路線的到站預估時間：只讀後台排程抓好的共用快照，絕不因為使用者查詢而打 TDX。
    快照還沒有任何資料（剛部署、排程還沒跑第一輪）時回傳 None，上層會顯示『無法取得即時動態』。"""
    _ensure_realtime_cache_fresh()
    with _realtime_lock:
        by_route = _realtime_cache.get("eta_by_route") or {}
        fetched = _realtime_cache.get("eta_fetched_at")
        data = by_route.get(route_name)
    if data is not None:
        return data
    if fetched or by_route:
        # 快照有資料，但這條路線沒有任何一筆（可能真的沒有車在跑），回傳空清單
        return []
    return None


@cached(600)
def fetch_weather():
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={TAINAN_LAT}&longitude={TAINAN_LON}"
               f"&current=temperature_2m,weathercode,windspeed_10m&timezone=Asia%2FTaipei")
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            cur = res.json().get("current", {})
            temp = cur.get("temperature_2m", "?")
            wind = cur.get("windspeed_10m", "?")
            wmap = {0: "晴天☀️", 1: "大致晴朗🌤️", 2: "部分多雲⛅", 3: "陰天☁️", 45: "有霧🌫️",
                     51: "毛毛雨🌦️", 61: "小雨🌧️", 63: "中雨🌧️", 65: "大雨🌧️", 80: "陣雨🌦️", 95: "雷雨⛈️"}
            desc = wmap.get(cur.get("weathercode", -1), "未知天氣")
            return f"{desc}，氣溫 {temp}°C，風速 {wind} km/h"
    except Exception:
        pass
    return "無法取得天氣"


_ubike_memo = {"key": None, "stations": [], "avail": {}}


def fetch_ubike_all():
    """回傳 (UBike 站點清單, {StationUID: 可借可還數量})。
    站點清單來自每月 1 號存好的檔案；可借／可還數量來自『每週尖峰時段每小時抓一次』
    存下來的快照（見 _ubike_poll_once），使用者查詢時只讀檔，不打 TDX。"""
    try:
        key = (os.path.getmtime(UBIKE_STATIONS_FILE) if os.path.exists(UBIKE_STATIONS_FILE) else 0,
               os.path.getmtime(UBIKE_AVAIL_FILE) if os.path.exists(UBIKE_AVAIL_FILE) else 0)
    except OSError:
        key = (0, 0)
    if _ubike_memo["key"] == key:
        return _ubike_memo["stations"], _ubike_memo["avail"]
    stations = _read_json_cached(UBIKE_STATIONS_FILE, [])
    snap = _read_json_cached(UBIKE_AVAIL_FILE, {}) or {}
    avail_map = {}
    for av in snap.get("items", []):
        uid = av.get("StationUID")
        if uid:
            avail_map[uid] = av
    _ubike_memo.update({"key": key, "stations": stations, "avail": avail_map})
    return stations, avail_map


def get_ubike_fetched_at():
    """UBike 可借可還數量快照是什麼時候抓的（epoch 秒），沒有資料回傳 None。"""
    snap = _read_json_cached(UBIKE_AVAIL_FILE, {}) or {}
    return snap.get("fetched_at")


def fetch_all_bus_stops():
    """全台南站牌（附近站牌查詢用），來自每月 1 號存好的檔案，不打 TDX。"""
    return _read_json_cached(ALL_BUS_STOPS_FILE, [])


def fetch_all_route_meta():
    """TDX 登記的全台南路線清單（路線名稱查詢、小黃公車清單用），來自每月 1 號存好的檔案，不打 TDX。"""
    return _read_json_cached(ALL_ROUTE_META_FILE, [])


@cached(3600)
def fetch_route_shape(route_name):
    return load_route_shape_data(route_name)


def _parse_stop_positions_from_stop_of_route(data):
    """把 StopOfRoute 回傳（合併去/回程、依站名去重）整理成 [{name, lat, lon}, ...]"""
    result = []
    seen = set()
    for dir_data in data:
        for s in dir_data.get("Stops", []):
            name = s.get("StopName", {}).get("Zh_tw", "")
            pos = s.get("StopPosition", {})
            lat, lon = pos.get("PositionLat"), pos.get("PositionLon")
            if name and lat and lon and name not in seen:
                seen.add(name)
                result.append({"name": name, "lat": lat, "lon": lon})
    return result


@cached(3600)
def fetch_route_stop_positions(route_name):
    """回傳某路線所有站牌的座標，供地圖畫小圓點用。
    優先讀取 /opt/render/project/data/route 底下已存檔的 StopOfRoute 資料
    （會自動被排程備份到 GitHub），沒有的話即時查 TDX 並自動存檔。"""
    data = load_route_stop_data(route_name)
    try:
        return _parse_stop_positions_from_stop_of_route(data)
    except Exception:
        return []


@cached(3600)
def fetch_route_stop_sequences(route_name):
    """回傳某路線『依方向、依站序』排好的站牌座標，格式 [{"dir": 0, "stops": [[lat, lon], ...]}, ...]。
    來自每月存好的 StopOfRoute 檔案，不打 TDX。地圖上公車的『每 5 分鐘走一站』模擬移動會用到。"""
    out = []
    for entry in load_route_stop_data(route_name):
        stops = sorted(entry.get("Stops", []), key=lambda s: s.get("StopSequence", 0))
        pts = []
        for s in stops:
            pos = s.get("StopPosition") or {}
            lat, lon = pos.get("PositionLat"), pos.get("PositionLon")
            if lat and lon:
                pts.append([round(lat, 5), round(lon, 5)])
        if pts:
            out.append({"dir": entry.get("Direction", 0), "stops": pts})
    return out

def fetch_shapes_and_stops_parallel(routes):
    """平行抓取多條路線的軌跡與站牌座標，避免地圖「顯示全部路線」時要序列等待上百次 API。"""
    shape_map, stop_map = {}, {}

    def worker(r):
        return r, fetch_route_shape(r), fetch_route_stop_positions(r)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker, r) for r in routes]
        for fut in concurrent.futures.as_completed(futures):
            try:
                r, shapes, stops = fut.result()
                shape_map[r] = shapes
                stop_map[r] = stops
            except Exception:
                pass
    return shape_map, stop_map


# ── 即時資料（到站預估／公車定位／UBike）：後台排程＋共用快照 ─────────────────
# 設計重點（為了把 TDX 用量壓在每月 2800 次以下）：
#   ① 到站預估（含車牌車型）：後台排程依時段抓『全台南一次』＝ 1 次請求。
#        尖峰（週一～五 07:00-08:00、17:00-18:00）每 6 分鐘；
#        平常（週一～五 05:50-07:00、08:00-17:00、18:00-23:00）每 15 分鐘；
#        週末 05:50-23:00 每 20 分鐘；23:00-05:50 完全不抓。
#        兩次抓取之間，API 與前端會用「現在時間 － 抓取時間」自動往下修正倒數。
#   ② UBike 可借可還：只在週一～五尖峰時段，每小時抓 1 次（每個尖峰時段 1 次）。
#   ③ 公車定位（地圖）：沒有人開地圖就完全不抓；有人開、而快照超過 1 小時才抓 1 次，
#        兩次之間由前端自己讓公車沿路線每 5 分鐘走一站。
REALTIME_POSITIONS_FILE = os.path.join(REALTIME_DATA_DIR, "positions.json")
REALTIME_ETA_FILE = os.path.join(REALTIME_DATA_DIR, "eta.json")
REALTIME_META_FILE = os.path.join(REALTIME_DATA_DIR, "meta.json")
REALTIME_LOCK_FILE = os.path.join(REALTIME_DATA_DIR, ".poll.lock")
REALTIME_POS_LOCK_FILE = os.path.join(REALTIME_DATA_DIR, ".positions.lock")
REALTIME_UBIKE_LOCK_FILE = os.path.join(REALTIME_DATA_DIR, ".ubike.lock")
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def _taipei_now():
    # 不管伺服器本身時區設定是什麼（Render 預設常常是 UTC），一律用台北時間判斷，
    # 避免離峰／尖峰／收班時段判斷錯位 8 小時。
    return datetime.now(TAIPEI_TZ)


# ── 智慧輪詢時刻表 ────────────────────────────────────────
SERVICE_START = (5, 50)   # 05:50 首班車（之前的查詢時間點）
SERVICE_END = (23, 0)     # 23:00 末班車之後停止取資料
WEEKDAY_PEAK_WINDOWS = [((7, 0), (8, 0)), ((17, 0), (18, 0))]   # 週一～五尖峰
PEAK_INTERVAL_SECONDS = 6 * 60        # 尖峰每 6 分鐘（沒有額度餘裕時的基本頻率）
PEAK_ACTIVE_INTERVAL_SECONDS = 4 * 60 # 有人使用、且額度進度正常時，尖峰改成每 4 分鐘（想要 3 分鐘就改這個數字）
USER_ACTIVE_WINDOW_SECONDS = 8 * 60   # 最近 8 分鐘內有人操作網站，才算「有人在用」
OFFPEAK_INTERVAL_SECONDS = 15 * 60    # 平常時段每 15 分鐘
WEEKEND_INTERVAL_SECONDS = 20 * 60    # 週末（你沒指定，預設 20 分鐘，想改直接改這個數字）
UBIKE_REFRESH_SECONDS = 3600          # 尖峰時段 UBike 每小時一次
POSITIONS_REFRESH_SECONDS = 3600      # 有人用地圖時，公車定位每小時抓一次
POSITIONS_RETRY_COOLDOWN = 300        # 定位抓取失敗後，至少隔 5 分鐘才再試（避免每次點地圖都燒一次額度）
POSITIONS_GPS_FALLBACK_MAX_AGE = 10 * 60   # 「查詢頁」只有在定位快照 10 分鐘內才拿來補站上有車
SERVICE_CLOSED_MESSAGE = "🌙 末班車已收車，明日 05:50 恢復發車"


def _minutes_of(t):
    return t.hour * 60 + t.minute


def _in_window(now, start, end):
    m = _minutes_of(now)
    return start[0] * 60 + start[1] <= m < end[0] * 60 + end[1]


def is_service_hours(now=None):
    now = now or _taipei_now()
    return _in_window(now, SERVICE_START, SERVICE_END)


# ── 國定假日判斷（項目 2）────────────────────────────────
# 連假、國定假日當天不會有上下班的尖峰，所以「週一～五」不能只看星期幾：
#   - 國定假日（平日放假）→ 當週末處理（20 分鐘一次、沒有尖峰、UBike 不抓）
#   - 補班日（週六上班）→ 當平日處理（有尖峰）
# 資料來源（不耗 TDX 額度）：開放資料 TaiwanCalendar（政府行事曆整理成 JSON）。
#   ① 每月最多更新一次（見 refresh_holiday_calendar），存成 data/holidays_年份.json（會跟著備份）
#   ② 程式裡內建 2026、2027 的假日當後備（網路抓不到時用）
#   ③ 臨時變動（颱風假、政府公告新增的假日）可以直接在下面兩個集合加日期，格式 "2026-10-24"
MANUAL_EXTRA_HOLIDAYS = set()   # 額外放假的日子（優先於一切）
MANUAL_EXTRA_WORKDAYS = set()   # 額外上班的日子（補班，優先度次於上面）
HOLIDAY_SEEDS = {
    2026: {"holidays": ["20260101", "20260216", "20260217", "20260218", "20260219", "20260220", "20260227", "20260403", "20260406", "20260501", "20260619", "20260925", "20260928", "20261009", "20261026", "20261225"], "workdays": []},
    2027: {"holidays": ["20270101", "20270204", "20270205", "20270208", "20270209", "20270210", "20270301", "20270405", "20270406", "20270430", "20270609", "20270915", "20270928", "20271011", "20271025", "20271224", "20271231"], "workdays": []},
}
HOLIDAY_API_URLS = [
    "https://raw.githubusercontent.com/ruyut/TaiwanCalendar/master/data/{year}.json",
    "https://cdn.jsdelivr.net/gh/ruyut/TaiwanCalendar/data/{year}.json",
]
HOLIDAY_REFRESH_DAYS = 30
HOLIDAY_RETRY_SECONDS = 6 * 3600
_holiday_last_attempt = {}


def _holiday_file(year):
    return os.path.join(DATA_DIR, f"holidays_{year}.json")


def is_workday(now=None):
    """這一天是不是「上班日」（會有通勤尖峰的日子）。判斷順序：
    手動額外假日／補班 → 抓下來的整年行事曆 → 內建後備 → 最後才退回「週一～五」。"""
    d = (now or _taipei_now()).date()
    iso, ymd = d.isoformat(), d.strftime("%Y%m%d")
    if iso in MANUAL_EXTRA_HOLIDAYS:
        return False
    if iso in MANUAL_EXTRA_WORKDAYS:
        return True
    cal = _read_json_cached(_holiday_file(d.year), None)
    if isinstance(cal, dict) and ymd in cal:
        return not bool(cal[ymd])
    seed = HOLIDAY_SEEDS.get(d.year)
    if seed:
        if ymd in seed["holidays"]:
            return False
        if ymd in seed["workdays"]:
            return True
    return d.weekday() < 5


def refresh_holiday_calendar():
    """每個月最多更新一次今年（10 月之後連明年）的行事曆檔案。失敗不影響服務，
    會繼續用上一份檔案或程式內建的後備資料，並且至少隔 6 小時才再試。"""
    now = _taipei_now()
    years = [now.year] + ([now.year + 1] if now.month >= 10 else [])
    for y in years:
        path = _holiday_file(y)
        try:
            age_days = (time.time() - os.path.getmtime(path)) / 86400
        except OSError:
            age_days = None
        if age_days is not None and age_days < HOLIDAY_REFRESH_DAYS:
            continue
        if time.time() - _holiday_last_attempt.get(y, 0) < HOLIDAY_RETRY_SECONDS:
            continue
        _holiday_last_attempt[y] = time.time()
        for url in HOLIDAY_API_URLS:
            try:
                res = requests.get(url.format(year=y), timeout=15)
                rows = res.json() if res.status_code == 200 else None
                if isinstance(rows, list) and len(rows) >= 360:
                    cal = {r["date"]: bool(r["isHoliday"]) for r in rows if "date" in r and "isHoliday" in r}
                    if len(cal) >= 360:
                        _atomic_write_json(path, cal)
                        print(f"📅 {y} 年假日行事曆已更新", flush=True)
                        break
            except Exception as e:
                print(f"⚠️ 假日行事曆 {y} 更新失敗（{url.split('/')[2]}）：{e}", flush=True)


def is_weekday(now=None):
    """相容舊名稱：現在代表「上班日」（會考慮國定假日與補班），不再只看星期幾。"""
    return is_workday(now)


def is_peak_time(now=None):
    """週一～五的尖峰時段（07:00-08:00、17:00-18:00）。"""
    now = now or _taipei_now()
    return is_weekday(now) and any(_in_window(now, s, e) for s, e in WEEKDAY_PEAK_WINDOWS)


def get_smart_poll_interval(now=None):
    """回傳現在這一刻『到站預估』該用的輪詢間隔秒數；23:00-05:50 回傳 None，
    代表完全不要向 TDX 發任何請求。"""
    now = now or _taipei_now()
    if not is_service_hours(now):
        return None
    if not is_weekday(now):
        return WEEKEND_INTERVAL_SECONDS
    if is_peak_time(now):
        return PEAK_INTERVAL_SECONDS
    return OFFPEAK_INTERVAL_SECONDS


def _seconds_until_service_resumes(now=None):
    now = now or _taipei_now()
    resume = now.replace(hour=SERVICE_START[0], minute=SERVICE_START[1], second=0, microsecond=0)
    if resume <= now:
        resume += timedelta(days=1)
    return max(30, (resume - now).total_seconds())


REALTIME_STALE_SECONDS = 40 * 60   # 超過這個秒數沒成功更新到站預估，視為「尚未更新資料」（最長間隔 20 分鐘的兩倍）
REALTIME_PUSH_MIN_INTERVAL = 5 * 60  # 推送到 GitHub 的節流

_realtime_lock = threading.Lock()
_positions_fetch_lock = threading.Lock()
_realtime_cache = {
    "positions_by_route": {},
    "eta_by_route": {},
    "updated_at": None,            # 最近一次「成功」更新到站預估的時間字串
    "eta_fetched_at": None,        # 最近一次成功抓到到站預估的時間（epoch 秒）
    "positions_fetched_at": None,  # 最近一次成功抓到公車定位的時間（epoch 秒）
    "last_attempt": None,
    "last_attempt_ts": None,       # 最近一次「嘗試」抓到站預估的時間（epoch 秒，失敗也算，用來控制重試間隔）
    "last_attempt_ok": None,
    "service_closed": False,
}
_realtime_file_mtime = None
_last_realtime_push_at = 0
_positions_last_attempt = 0
_ubike_last_attempt = 0


def get_realtime_status():
    """回傳到站預估快照的新鮮度，給 API 附加在回應裡。"""
    with _realtime_lock:
        updated_at = _realtime_cache.get("updated_at")
        service_closed = bool(_realtime_cache.get("service_closed"))
        eta_fetched_at = _realtime_cache.get("eta_fetched_at")
        positions_fetched_at = _realtime_cache.get("positions_fetched_at")
    base = {"updated_at": updated_at, "is_fresh": False, "age_seconds": None,
            "service_closed": service_closed, "eta_fetched_at": eta_fetched_at,
            "positions_fetched_at": positions_fetched_at}
    if eta_fetched_at:
        age = time.time() - eta_fetched_at
        base.update({"is_fresh": age <= REALTIME_STALE_SECONDS, "age_seconds": int(age)})
    return base


def _load_realtime_cache_from_disk():
    """把存檔的快照讀進記憶體（開機、或別的 worker 更新了檔案時）。"""
    positions_by_route, eta_by_route, meta = {}, {}, {}
    for path, name in ((REALTIME_POSITIONS_FILE, "p"), (REALTIME_ETA_FILE, "e"), (REALTIME_META_FILE, "m")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if name == "p":
                positions_by_route = obj
            elif name == "e":
                eta_by_route = obj
            else:
                meta = obj
        except Exception:
            pass
    with _realtime_lock:
        _realtime_cache["positions_by_route"] = positions_by_route
        _realtime_cache["eta_by_route"] = eta_by_route
        _realtime_cache["updated_at"] = meta.get("updated_at")
        _realtime_cache["eta_fetched_at"] = meta.get("eta_fetched_at")
        _realtime_cache["positions_fetched_at"] = meta.get("positions_fetched_at")
        _realtime_cache["last_attempt"] = meta.get("last_attempt")
        _realtime_cache["last_attempt_ts"] = meta.get("last_attempt_ts")
        _realtime_cache["last_attempt_ok"] = meta.get("last_attempt_ok")


def _ensure_realtime_cache_fresh():
    """每次讀快照前，先看 meta.json 的修改時間：別的 worker 更新過就重新讀檔，沒變就什麼都不做。"""
    global _realtime_file_mtime
    try:
        mtime = os.path.getmtime(REALTIME_META_FILE)
    except OSError:
        return
    if _realtime_file_mtime is None or mtime > _realtime_file_mtime:
        _load_realtime_cache_from_disk()
        _realtime_file_mtime = mtime


def _acquire_flock(path):
    """跨 worker 的非阻塞鎖：搶到回傳檔案物件，沒搶到（別人正在抓）回傳 None。"""
    fp = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fp = open(path, "w")
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fp
    except BlockingIOError:
        if fp:
            fp.close()
        return None
    except Exception:
        return fp   # 檔案系統有問題時照常執行（單 worker 情況不影響）


def _release_flock(fp):
    if fp is None:
        return
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    fp.close()


def _write_realtime_snapshot(write_eta=False, write_positions=False):
    """把記憶體裡最新的快照寫成檔案（原子替換，讀的人不會讀到寫一半的檔）。meta.json 最後寫。"""
    global _realtime_file_mtime
    try:
        with _realtime_lock:
            positions = dict(_realtime_cache["positions_by_route"])
            eta = dict(_realtime_cache["eta_by_route"])
            meta = {k: _realtime_cache.get(k) for k in (
                "updated_at", "eta_fetched_at", "positions_fetched_at",
                "last_attempt", "last_attempt_ts", "last_attempt_ok")}
        if write_positions:
            _atomic_write_json(REALTIME_POSITIONS_FILE, positions)
        if write_eta:
            _atomic_write_json(REALTIME_ETA_FILE, eta)
        _atomic_write_json(REALTIME_META_FILE, meta)
        try:
            _realtime_file_mtime = os.path.getmtime(REALTIME_META_FILE)
        except OSError:
            pass
    except Exception as e:
        print(f"⚠️ 寫入即時資料快照失敗：{e}", flush=True)


# ── ① 到站預估 ────────────────────────────────────────────
def _eta_poll_due(interval):
    """距離上一次『嘗試』是否已經超過這個時段該有的間隔（成功失敗都算嘗試）。
    以共用檔案裡的時間為準，多個 worker 不會各抓各的。"""
    _ensure_realtime_cache_fresh()
    with _realtime_lock:
        last = max(_realtime_cache.get("eta_fetched_at") or 0, _realtime_cache.get("last_attempt_ts") or 0)
    return time.time() - last >= interval - 5


def _eta_poll_once(interval, push=True):
    """向 TDX 抓『全台南到站預估』1 次（含每站車牌／車型），存進快照。"""
    lock_fp = _acquire_flock(REALTIME_LOCK_FILE)
    if lock_fp is None:
        return False
    try:
        if not _eta_poll_due(interval):   # 搶到鎖之後再確認一次，避免別的 worker 剛抓完
            return False
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        eta_all = fetch_bus_data_all()
        ok = eta_all is not None
        with _realtime_lock:
            if ok:
                eta_by_route = {}
                for item in eta_all:
                    r = (item.get("RouteName") or {}).get("Zh_tw", "")
                    if r:
                        eta_by_route.setdefault(r, []).append(item)
                # 成功就整批換新（舊的已經收班或過站的車就該消失）
                _realtime_cache["eta_by_route"] = eta_by_route
                _realtime_cache["eta_fetched_at"] = time.time()
                _realtime_cache["updated_at"] = now_str
            _realtime_cache["last_attempt"] = now_str
            _realtime_cache["last_attempt_ts"] = time.time()
            _realtime_cache["last_attempt_ok"] = ok
            count = sum(len(v) for v in (_realtime_cache["eta_by_route"] or {}).values())
        _write_realtime_snapshot(write_eta=ok)
        if ok:
            try:
                headway_record(eta_by_route, _realtime_cache.get("eta_fetched_at") or time.time())
            except Exception as e:
                print(f"⚠️ 班距統計記錄失敗（不影響服務）：{e}", flush=True)

        global _last_realtime_push_at
        if push and time.time() - _last_realtime_push_at >= REALTIME_PUSH_MIN_INTERVAL:
            try:
                push_realtime_backup(REALTIME_DATA_DIR)
                _last_realtime_push_at = time.time()
            except Exception as e:
                print(f"❌ 即時資料推送到 GitHub 失敗：{e}", flush=True)

        used = tdx_usage_snapshot().get("count", 0)
        print(f"{'✅' if ok else '⚠️'} 到站預估排程：{count} 筆，時間 {now_str}，"
              f"本月 TDX 已用 {used}/{TDX_MONTHLY_CAP}", flush=True)
        return ok
    finally:
        _release_flock(lock_fp)


# ── ② UBike（只在週一～五尖峰時段，每小時 1 次）──────────────
def _ubike_poll_due(now):
    if not is_peak_time(now):
        return False
    if time.time() - _ubike_last_attempt < 600:   # 失敗後 10 分鐘內不重試
        return False
    fetched = get_ubike_fetched_at() or 0
    return time.time() - fetched >= UBIKE_REFRESH_SECONDS - 5


def _ubike_poll_once():
    global _ubike_last_attempt
    lock_fp = _acquire_flock(REALTIME_UBIKE_LOCK_FILE)
    if lock_fp is None:
        return
    try:
        fetched = get_ubike_fetched_at() or 0
        if time.time() - fetched < UBIKE_REFRESH_SECONDS - 5:
            return
        _ubike_last_attempt = time.time()
        url = "https://tdx.transportdata.tw/api/basic/v2/Bike/Availability/City/Tainan?%24format=JSON"
        res = tdx_get(url, timeout=20, retries=0, kind="dynamic", source="ubike")
        if res is None:
            return
        try:
            items = res.json()
        except Exception:
            return
        if isinstance(items, list) and items:
            _atomic_write_json(UBIKE_AVAIL_FILE, {"fetched_at": time.time(), "items": items})
            print(f"🚲 UBike 可借可還更新：{len(items)} 站", flush=True)
    finally:
        _release_flock(lock_fp)


# ── ③ 公車定位（只有地圖有人用才抓）────────────────────────
def _fetch_bus_realtime_positions_from_tdx():
    """向 TDX 查『全台南』即時公車定位 1 次。失敗回傳 None。"""
    url = "https://tdx.transportdata.tw/api/basic/v2/Bus/RealTimeByFrequency/City/Tainan?%24format=JSON"
    res = tdx_get(url, timeout=30, retries=0, kind="dynamic", source="positions")
    if res is not None:
        try:
            data = res.json()
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return None


def ensure_positions_fresh_for_map():
    """地圖 API 呼叫：有人在用地圖，而且定位快照已經超過 1 小時，才向 TDX 抓 1 次；
    收班時段（23:00-05:50）完全不抓。沒有人開地圖就永遠不會被呼叫＝不耗額度。
    回傳 {'service_closed': bool, 'fetched_at': epoch 或 None}。"""
    global _positions_last_attempt
    _ensure_realtime_cache_fresh()
    if not is_service_hours():
        return {"service_closed": True, "fetched_at": None}

    def current():
        with _realtime_lock:
            return _realtime_cache.get("positions_fetched_at")

    fetched = current()
    if fetched and time.time() - fetched < POSITIONS_REFRESH_SECONDS:
        return {"service_closed": False, "fetched_at": fetched}
    if time.time() - _positions_last_attempt < POSITIONS_RETRY_COOLDOWN:
        return {"service_closed": False, "fetch_skipped": True, "fetched_at": fetched}
    # 用量進度檢查比到站預估「更嚴格」（緩衝 0 次，到站預估是 150 次）：
    # 額度吃緊時先犧牲地圖定位更新，保住到站預估，整個月才不會在月底提早用完。
    if not _pacing_allows_optional_poll(margin=0):
        return {"service_closed": False, "fetch_skipped": True, "fetched_at": fetched}

    with _positions_fetch_lock:
        _ensure_realtime_cache_fresh()
        fetched = current()
        if fetched and time.time() - fetched < POSITIONS_REFRESH_SECONDS:
            return {"service_closed": False, "fetched_at": fetched}
        lock_fp = _acquire_flock(REALTIME_POS_LOCK_FILE)
        if lock_fp is None:      # 別的 worker 正在抓，這裡直接用現有快照
            return {"service_closed": False, "fetched_at": fetched}
        try:
            _positions_last_attempt = time.time()
            positions_all = _fetch_bus_realtime_positions_from_tdx()
            if positions_all is not None:
                by_route = {}
                for b in positions_all:
                    r = (b.get("RouteName") or {}).get("Zh_tw", "")
                    if r:
                        by_route.setdefault(r, []).append(b)
                with _realtime_lock:
                    _realtime_cache["positions_by_route"] = by_route
                    _realtime_cache["positions_fetched_at"] = time.time()
                _write_realtime_snapshot(write_positions=True)
                print(f"🗺️ 地圖有人使用，公車定位更新：{len(positions_all)} 台", flush=True)
        finally:
            _release_flock(lock_fp)
    return {"service_closed": False, "fetched_at": current()}


def get_positions_snapshot(route_name=None, max_age=None):
    """讀『已經存在的』公車定位快照（不會打 TDX）。max_age（秒）：快照比這個舊就當作沒有。"""
    _ensure_realtime_cache_fresh()
    with _realtime_lock:
        by_route = _realtime_cache.get("positions_by_route") or {}
        fetched = _realtime_cache.get("positions_fetched_at")
        if max_age is not None and (not fetched or time.time() - fetched > max_age):
            return []
        if route_name:
            return list(by_route.get(route_name, []))
        return [b for lst in by_route.values() for b in lst]


# ── 依使用者活動調整輪詢（項目 1）──────────────────────────
# 沒人開網站就完全不向 TDX 抓到站預估／UBike（離峰、週末、尖峰都一樣）；
# 有人開網站才恢復，而且「剛有人來」的那一刻會立刻補抓一次，不用等下一輪。
# 判斷「有人在用」：瀏覽器對 /api/ 發出的請求（前端開著頁面時每分鐘都會有）。
# 排除：Render 的健康檢查（Go-http-client）、沒有 User-Agent 的請求、HEAD 請求、診斷網址。
_last_user_activity = 0.0
_activity_event = threading.Event()


def is_user_active():
    return time.time() - _last_user_activity < USER_ACTIVE_WINDOW_SECONDS


@app.before_request
def _track_user_activity():
    global _last_user_activity
    path = request.path
    if not path.startswith('/api/') or path.startswith('/api/debug_status'):
        return
    ua = request.headers.get('User-Agent', '')
    if request.method == 'HEAD' or not ua or 'Go-http-client' in ua or 'Render' in ua:
        return
    was_idle = not is_user_active()
    _last_user_activity = time.time()
    if was_idle:
        _activity_event.set()   # 叫醒背景排程，立刻檢查是否要補抓


def ensure_eta_fresh_for_request():
    """使用者查詢到站時，如果快照已經很舊（例如沒人使用期間輪詢暫停了），就立刻補抓一次，
    讓第一個進來的人也看得到新資料。仍然遵守：收班時段不抓、額度進度檢查、每個時段間隔只抓一次。"""
    now = _taipei_now()
    interval = get_smart_poll_interval(now)
    if interval is None:
        return
    _ensure_realtime_cache_fresh()
    with _realtime_lock:
        last = max(_realtime_cache.get("eta_fetched_at") or 0, _realtime_cache.get("last_attempt_ts") or 0)
    if time.time() - last < interval * 2:
        return
    if is_peak_time(now) or _pacing_allows_optional_poll(now):
        _eta_poll_once(interval, push=False)


# ── 背景排程主迴圈 ────────────────────────────────────────
def _realtime_poll_loop():
    """每 20 秒（或有人剛來時立刻）醒來看一次「現在該不該抓」，不是睡整段間隔，所以從平常時段
    切進尖峰時，會在 07:00／17:00 準時換頻率，不會晚一大段。
      - 沒人在用網站：不抓到站預估、不抓 UBike（省額度）。
      - 有人在用：到站預估依 get_smart_poll_interval() 的間隔；尖峰時段額度進度正常的話
        改成 PEAK_ACTIVE_INTERVAL_SECONDS（4 分鐘）；平常／週末／假日還要過用量進度檢查。
      - UBike：週一～五（上班日）尖峰時段，有人在用才每小時抓 1 次。
      - 靜態資料、假日行事曆：每小時檢查一次（真的要抓才抓：每月 1 號或當月首次啟動）。
      - 23:00-05:50：不打任何即時資料請求。"""
    last_static_check = 0
    while True:
        sleep_s = 20
        try:
            now = _taipei_now()
            if time.time() - last_static_check >= 3600:
                last_static_check = time.time()
                threading.Thread(target=refresh_static_data, daemon=True).start()
                threading.Thread(target=refresh_holiday_calendar, daemon=True).start()
            interval = get_smart_poll_interval(now)
            if interval is None:
                with _realtime_lock:
                    _realtime_cache["service_closed"] = True
                sleep_s = min(_seconds_until_service_resumes(now), 1800)
            else:
                with _realtime_lock:
                    _realtime_cache["service_closed"] = False
                if is_user_active():
                    pacing_ok = _pacing_allows_optional_poll(now)
                    if is_peak_time(now) and pacing_ok:
                        interval = PEAK_ACTIVE_INTERVAL_SECONDS
                    if _eta_poll_due(interval) and (is_peak_time(now) or pacing_ok):
                        _eta_poll_once(interval)
                    if _ubike_poll_due(now):
                        _ubike_poll_once()
        except Exception as e:
            print(f"❌ 即時資料排程發生例外：{e}", flush=True)
        _activity_event.wait(sleep_s)
        _activity_event.clear()


_load_realtime_cache_from_disk()


def find_nearby_stops(all_stops, lat, lon, radius_km=0.5):
    nearby, seen = [], set()
    for stop in all_stops:
        pos = stop.get("StopPosition", {})
        s_lat, s_lon = pos.get("PositionLat"), pos.get("PositionLon")
        name = stop.get("StopName", {}).get("Zh_tw", "")
        if s_lat and s_lon and name and name not in seen:
            dist = haversine(lat, lon, s_lat, s_lon)
            if dist <= radius_km:
                seen.add(name)
                nearby.append({"name": name, "dist": dist})
    nearby.sort(key=lambda x: x["dist"])
    return nearby[:15]


def get_ubike_near(s_lat, s_lon, stations, avail_map, radius_km=0.3):
    result = []
    for ub in stations:
        pos = ub.get("StationPosition", {})
        u_lat, u_lon = pos.get("PositionLat"), pos.get("PositionLon")
        if u_lat and u_lon and haversine(s_lat, s_lon, u_lat, u_lon) <= radius_km:
            uid = ub.get("StationUID", "")
            av = avail_map.get(uid, {})
            result.append({
                "name": ub.get("StationName", {}).get("Zh_tw", ""),
                "available": av.get("AvailableRentBikes", 0),
                "empty": av.get("AvailableReturnBikes", 0),
                "lat": u_lat, "lon": u_lon
            })
    return result


# ── 進階路線查詢：直達 + 一次轉乘 ───────────────────────────
def _fetch_all_route_stops_parallel(routes):
    """平行為多條路線取得站名清單。內部走 fetch_route_stops，
    每條路線都會優先讀 data/route 裡的存檔，沒有才即時查並自動存檔。"""
    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_route_stops, r): r for r in routes}
        for fut in concurrent.futures.as_completed(futures):
            r = futures[fut]
            try:
                result[r] = fut.result()
            except Exception:
                result[r] = []
    return result


_stop_route_index_cache = {"data": None, "time": 0}


def build_stop_route_index():
    """建立「站名 → 可搭乘路線」索引，供進階查詢（站到站）使用。
    這裡刻意跟即時地圖用同一份路線清單（get_all_known_routes，系統設定的路線 ∪
    地圖頁「已儲存路線」），不是只看系統設定的固定清單，才不會漏掉只在地圖那邊
    存過站牌資料的路線（例如小黃公車、自訂儲存的路線）。
    不依賴手動按「系統維護」：自動走遍全部路線，data/route 裡有存檔的直接用，
    沒有的即時查詢並自動存檔（存到 /opt/render/project/data/route，會被排程備份）。

    這裡刻意不用一般的 @cached(3600)，改用下面這個自訂的小快取，是因為兩種極端都要避免：
    1) 完全不快取／每次都要求全部路線都成功才快取 → 只要固定有少數幾條路線持續查不到
       （例如剛好那幾條路線名稱在 TDX 對不起來），就永遠達不到「幾乎全部成功」的門檻，
       導致「進階查詢」每次都要重新對 150 幾條路線發送查詢，在請求逾時內做不完，
       反而讓整個站名清單直接查不到、車站全部不見。
    2) 完整或不完整都用同一個長 TTL（例如 1 小時）→ 一旦剛好在系統剛啟動、還有很多路線
       沒存過檔的情況下查詢，那次不完整的結果會卡住一整個小時都不會自動補齊。
    所以無論這次結果完不完整都「先快取住」，讓查詢至少能馬上有結果可用；
    只是完整的話快取久一點（1 小時），不完整的話快取短一點（2 分鐘）盡快自動重新嘗試補齊。"""
    now = time.time()
    cached_entry = _stop_route_index_cache["data"]
    ttl = _stop_route_index_cache.get("ttl", 3600)
    if cached_entry is not None and now - _stop_route_index_cache["time"] < ttl:
        return cached_entry

    all_routes = get_all_known_routes()
    stops_map = _fetch_all_route_stops_parallel(all_routes)
    index = {}
    for route_name, stops in stops_map.items():
        for stop in stops:
            if stop not in index:
                index[stop] = []
            if route_name not in index[stop]:
                index[stop].append(route_name)

    missing = [r for r, s in stops_map.items() if not s]
    complete = bool(all_routes) and len(missing) <= max(3, len(all_routes) * 0.05)
    _stop_route_index_cache["data"] = index
    _stop_route_index_cache["time"] = now
    _stop_route_index_cache["ttl"] = 3600 if complete else 120
    if not complete and missing:
        print(f"⚠️ 進階查詢站名索引尚未完整（{len(missing)} 條路線暫時查不到站牌），"
              f"先用目前查到的結果服務，2 分鐘後會自動重新嘗試補齊。")
    return index


def find_direct_routes(stop_index, start_stop, end_stop):
    start_routes = set(stop_index.get(start_stop, []))
    end_routes = set(stop_index.get(end_stop, []))
    return sorted(start_routes & end_routes)


def find_transfer_routes(stop_index, start_stop, end_stop, max_results=10):
    start_routes = stop_index.get(start_stop, [])
    end_routes = stop_index.get(end_stop, [])

    start_route_stops = {}
    for r in start_routes:
        for stop, routes in stop_index.items():
            if r in routes:
                start_route_stops.setdefault(r, set()).add(stop)

    end_route_stops = {}
    for r in end_routes:
        for stop, routes in stop_index.items():
            if r in routes:
                end_route_stops.setdefault(r, set()).add(stop)

    results = []
    for rA, stopsA in start_route_stops.items():
        for rB, stopsB in end_route_stops.items():
            if rA == rB:
                continue
            transfer_stops = stopsA & stopsB
            if transfer_stops:
                for ts in sorted(transfer_stops)[:3]:
                    results.append({"routeA": rA, "transfer": ts, "routeB": rB})
                    if len(results) >= max_results:
                        return results
    return results


# ── 轉乘路線規劃（項目 3）────────────────────────────────
# 完全用本地已存檔的「站序」計算，不打 TDX：
#   直達：在同一條路線「同一個方向」上，出發站排在目的站前面。
#   轉乘一次：搭 A 路線在 X 站下車，換同站（或步行 PLAN_WALK_METERS 公尺內的站）的 B 路線，
#             而且 B 路線的方向要能到目的站。已經有直達的路線不會被當成轉乘的一段。
# 排序：站數少的在前，轉乘一次視同多搭 PLAN_TRANSFER_PENALTY 站，步行也會加一點分數。
# 只提供「哪一路、在哪下車」；沒有時刻表，所以不會算出實際等車與轉乘等待時間。
# 「下一班幾分鐘」只在有現成的到站快照時順便附上（讀快照，不額外查 TDX）。
PLAN_WALK_METERS = 150
PLAN_TRANSFER_PENALTY = 8
_plan_index_cache = {"data": None, "time": 0}


@cached(3600)
def fetch_route_stop_names(route_name):
    """某路線依方向、依站序的站牌：[{"dir": 0, "stops": [(站名, 緯度, 經度), ...]}, ...]（讀存檔）。"""
    out = []
    for entry in load_route_stop_data(route_name):
        stops = sorted(entry.get("Stops", []), key=lambda s: s.get("StopSequence", 0))
        seq = []
        for s in stops:
            name = (s.get("StopName") or {}).get("Zh_tw", "")
            pos = s.get("StopPosition") or {}
            if name:
                seq.append((name, pos.get("PositionLat"), pos.get("PositionLon")))
        if seq:
            out.append({"dir": entry.get("Direction", 0), "stops": seq})
    return out


def build_plan_index():
    """建立規劃用索引：每條「路線＋方向」的站序，以及「站名 → 在哪些路線的第幾站」。"""
    now = time.time()
    c = _plan_index_cache
    if c["data"] is not None and now - c["time"] < (3600 if c["data"]["seqs"] else 120):
        return c["data"]
    seqs, coords, by_name = {}, {}, {}
    for route in get_all_known_routes():
        for k, e in enumerate(fetch_route_stop_names(route)):
            rd = (route, e["dir"], k)
            seqs[rd] = [st[0] for st in e["stops"]]
            for i, (name, la, lo) in enumerate(e["stops"]):
                by_name.setdefault(name, []).append((rd, i))
                if la and lo and name not in coords:
                    coords[name] = (la, lo)
    grid = {}
    for name, (la, lo) in coords.items():
        grid.setdefault((round(la / 0.002), round(lo / 0.002)), []).append(name)
    c["data"] = {"seqs": seqs, "by_name": by_name, "coords": coords, "grid": grid, "walk_memo": {}}
    c["time"] = now
    return c["data"]


def _transfer_candidates(idx, name):
    """可以在這站轉乘的站：同名站，加上步行 PLAN_WALK_METERS 公尺內的其他站名。回傳 [(站名, 步行公尺)]。"""
    memo = idx["walk_memo"]
    if name in memo:
        return memo[name]
    out = [(name, 0)]
    c = idx["coords"].get(name)
    if c:
        gx, gy = round(c[0] / 0.002), round(c[1] / 0.002)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for n2 in idx["grid"].get((gx + dx, gy + dy), []):
                    if n2 == name:
                        continue
                    la2, lo2 = idx["coords"][n2]
                    d = haversine(c[0], c[1], la2, lo2) * 1000
                    if d <= PLAN_WALK_METERS:
                        out.append((n2, int(d)))
    memo[name] = out
    return out


def _next_bus_minutes(route, direction, stop_name):
    """從到站快照讀出這條路線在這站「下一班」還要幾分鐘（已依快照年齡修正）；沒資料回傳 None。"""
    items = fetch_bus_data(route)
    if not items:
        return None
    rt = get_realtime_status()
    elapsed = max(0.0, time.time() - rt["eta_fetched_at"]) if rt["eta_fetched_at"] else 0.0
    if elapsed > REALTIME_STALE_SECONDS:
        return None
    best = None
    for it in items:
        if it.get("Direction") != direction:
            continue
        if (it.get("StopName") or {}).get("Zh_tw") != stop_name:
            continue
        eta = it.get("EstimateTime")
        if eta is None:
            continue
        eta = max(0, eta - elapsed)
        if best is None or eta < best:
            best = eta
    return None if best is None else int(best // 60)


def plan_trip(start, end, max_results=6):
    """規劃 start → end：回傳直達與轉乘一次的方案（本地計算，不打 TDX）。"""
    idx = build_plan_index()
    if not idx["seqs"]:
        return {"index_ok": False, "direct_plans": [], "transfer_plans": []}
    seqs, by_name = idx["seqs"], idx["by_name"]
    starts = by_name.get(start, [])
    end_map = {}
    for rd, j in by_name.get(end, []):
        end_map.setdefault(rd, []).append(j)

    # 直達
    best_direct, direct_board_idx = {}, {}
    for rd, i in starts:
        for j in end_map.get(rd, []):
            if j > i and (rd not in best_direct or j - i < best_direct[rd]):
                best_direct[rd] = j - i
                direct_board_idx[rd] = i
    direct_plans, seen_direct = [], set()
    for rd, n in sorted(best_direct.items(), key=lambda kv: (kv[1], kv[0][0])):
        key = (rd[0], rd[1])
        if key in seen_direct:
            continue
        seen_direct.add(key)
        direct_plans.append({"id": f"d|{rd[0]}|{rd[1]}", "route": rd[0], "dir": rd[1],
                             "dest": seqs[rd][-1], "stops": n, "board_idx": direct_board_idx[rd]})

    # 轉乘一次
    best_t = {}
    for rd1, i in starts:
        if rd1 in best_direct:
            continue                      # 這條自己就能直達，不當轉乘的一段
        seq1 = seqs[rd1]
        for k in range(i + 1, len(seq1)):
            xname = seq1[k]
            for tname, walk in _transfer_candidates(idx, xname):
                for rd2, x in by_name.get(tname, []):
                    if rd2[0] == rd1[0] or rd2 in best_direct:
                        continue
                    for j in end_map.get(rd2, []):
                        if j <= x:
                            continue
                        total = (k - i) + (j - x)
                        score = total + PLAN_TRANSFER_PENALTY + walk / 50.0
                        key = (rd1[0], rd1[1], rd2[0], rd2[1])
                        cur = best_t.get(key)
                        if cur is None or score < cur["score"]:
                            best_t[key] = {
                                "id": f"t|{rd1[0]}|{rd1[1]}|{rd2[0]}|{rd2[1]}",
                                "boardIdxA": i, "boardIdxB": x,
                                "score": score, "routeA": rd1[0], "dirA": rd1[1], "destA": seq1[-1],
                                "stopsA": k - i, "transfer": xname, "walk_m": walk,
                                "board_b": tname, "routeB": rd2[0], "dirB": rd2[1],
                                "destB": seqs[rd2][-1], "stopsB": j - x, "total_stops": total,
                            }
    transfer_plans = sorted(best_t.values(), key=lambda p: p["score"])[:max_results]
    for p in transfer_plans:
        p.pop("score", None)
        p["next_min"] = _next_bus_minutes(p["routeA"], p["dirA"], start)
    for p in direct_plans[:10]:
        p["next_min"] = _next_bus_minutes(p["route"], p["dir"], start)
    return {"index_ok": True, "direct_plans": direct_plans[:12], "transfer_plans": transfer_plans}


# ── 轉乘時間估算（「運算轉乘時間」按鈕）──────────────────────
# 把規劃結果換算成「大約要花幾分鐘、幾點到」。全程用本地資料，不打 TDX：
#   等第一班車：優先用到站快照的即時預估（已依快照年齡修正），沒有就用今天的固定時刻表推算
#   搭乘：每站約 PLAN_MIN_PER_STOP 分鐘（跟查詢頁推算沒有資料的站時用的基準一致）
#   步行轉乘：以每秒 1.2 公尺估算
#   等第二班車：用今天的固定時刻表，找「抵達轉乘站＋步行之後」最早到站的那一班；
#              沒有時刻表才用即時預估（而且那班必須在你抵達之後才有意義）
# 這些都是估算值，不含紅綠燈、塞車與實際到站誤差，畫面上也會這樣標示。
PLAN_MIN_PER_STOP = 2
PLAN_WALK_MPS = 1.2


def _next_bus_seconds(route, direction, stop_name):
    """快照裡這條路線在這站「下一班」還要幾秒（已依快照年齡修正）；沒資料回傳 None。"""
    items = fetch_bus_data(route)
    if not items:
        return None
    rt = get_realtime_status()
    elapsed = max(0.0, time.time() - rt["eta_fetched_at"]) if rt["eta_fetched_at"] else 0.0
    if elapsed > REALTIME_STALE_SECONDS:
        return None
    best = None
    for it in items:
        if it.get("Direction") != direction:
            continue
        if (it.get("StopName") or {}).get("Zh_tw") != stop_name:
            continue
        eta = it.get("EstimateTime")
        if eta is None:
            continue
        eta = max(0, eta - elapsed)
        if best is None or eta < best:
            best = eta
    return best


def _schedule_arrival_offsets(route, direction, stop_idx):
    """用「今天」的固定時刻表推算：這條路線在第 stop_idx 站，之後每班車還要幾秒到站（由近到遠排序）。
    發車時間 ＋ 每站 PLAN_MIN_PER_STOP 分鐘。服務日依台北時間判斷，國定假日（平日放假）當週日、
    補班的週六當週一。沒有時刻表資料回傳空 list。"""
    raw = fetch_route_schedule(route)
    if not raw:
        return []
    now = _taipei_now().replace(tzinfo=None)
    wd = now.weekday()
    if wd < 5 and not is_workday(_taipei_now()):
        wd = 6
    elif wd >= 5 and is_workday(_taipei_now()):
        wd = 0
    key = _WEEKDAY_KEYS[wd]
    offsets = []
    for entry in raw:
        if entry.get("Direction", 0) != direction:
            continue
        for t in entry.get("Timetables", []):
            svc = t.get("ServiceDay") or {}
            if svc and not svc.get(key, False):
                continue
            dep_str = _timetable_departure_time(t)
            if not dep_str:
                continue
            try:
                hh, mm = dep_str.split(":")[:2]
                hh, mm = int(hh), int(mm)
                dep = now.replace(hour=hh % 24, minute=mm, second=0, microsecond=0) + timedelta(days=hh // 24)
            except Exception:
                continue
            off = (dep - now).total_seconds() + stop_idx * PLAN_MIN_PER_STOP * 60
            if off >= -60:
                offsets.append(max(0.0, off))
    return sorted(offsets)


def _fmt_min(sec):
    return None if sec is None else int(round(sec / 60.0))


def estimate_plan_times(start, plans):
    """替 plan_trip 的結果算出每個方案的預估時間。回傳 {方案 id: 明細}。"""
    now_tw = _taipei_now()
    out = {}

    def arrive_text(sec):
        return (now_tw + timedelta(seconds=sec)).strftime("%H:%M")

    for p in plans.get("direct_plans", []):
        wait, src = _next_bus_seconds(p["route"], p["dir"], start), "即時"
        if wait is None:
            offs = _schedule_arrival_offsets(p["route"], p["dir"], p.get("board_idx", 0))
            wait, src = (offs[0] if offs else None), "時刻表"
        if wait is None:
            hw = headway_lookup(p["route"], p["dir"])
            if hw:
                wait, src = hw["median"] * 30.0, "班距"
        ride = p["stops"] * PLAN_MIN_PER_STOP * 60
        total = None if wait is None else wait + ride
        out[p["id"]] = {
            "wait1_min": _fmt_min(wait), "wait1_src": src if wait is not None else None,
            "ride1_min": _fmt_min(ride), "total_min": _fmt_min(total),
            "arrive": arrive_text(total) if total is not None else None,
        }

    for p in plans.get("transfer_plans", []):
        wait1, src1 = _next_bus_seconds(p["routeA"], p["dirA"], start), "即時"
        if wait1 is None:
            offs = _schedule_arrival_offsets(p["routeA"], p["dirA"], p.get("boardIdxA", 0))
            wait1, src1 = (offs[0] if offs else None), "時刻表"
        if wait1 is None:
            hw = headway_lookup(p["routeA"], p["dirA"])
            if hw:
                wait1, src1 = hw["median"] * 30.0, "班距"
        ride1 = p["stopsA"] * PLAN_MIN_PER_STOP * 60
        walk = (p.get("walk_m", 0) or 0) / PLAN_WALK_MPS
        ride2 = p["stopsB"] * PLAN_MIN_PER_STOP * 60
        rec = {"wait1_min": _fmt_min(wait1), "wait1_src": src1 if wait1 is not None else None,
               "ride1_min": _fmt_min(ride1), "walk_min": _fmt_min(walk) if walk else 0,
               "ride2_min": _fmt_min(ride2), "wait2_min": None, "wait2_src": None,
               "total_min": None, "arrive": None, "partial_min": None}
        if wait1 is None:
            out[p["id"]] = rec          # 第一班車都估不出來，後面不用算
            continue
        ready_b = wait1 + ride1 + walk   # 抵達 B 路線上車站的時間（距離現在幾秒）
        wait2, src2 = None, None
        offs = _schedule_arrival_offsets(p["routeB"], p["dirB"], p.get("boardIdxB", 0))
        later = [o for o in offs if o >= ready_b]
        if later:
            wait2, src2 = later[0] - ready_b, "時刻表"
        else:
            live = _next_bus_seconds(p["routeB"], p["dirB"], p["board_b"])
            if live is not None and live >= ready_b:
                wait2, src2 = live - ready_b, "即時"
        if wait2 is None:
            hw = headway_lookup(p["routeB"], p["dirB"])
            if hw:
                wait2, src2 = hw["median"] * 30.0, "班距"
        rec["partial_min"] = _fmt_min(ready_b + ride2)       # 不含第二班車等待時間的最短時間
        if wait2 is not None:
            total = ready_b + wait2 + ride2
            rec.update({"wait2_min": _fmt_min(wait2), "wait2_src": src2,
                        "total_min": _fmt_min(total), "arrive": arrive_text(total)})
        out[p["id"]] = rec
    return out


# ── UBike 騎車建議 ────────────────────────────────────────
def check_ubike_suggestion(start_st, end_st, stop_coord_map, ub_stations, ub_avail, bus_wait_sec, bus_travel_sec):
    if start_st not in stop_coord_map or end_st not in stop_coord_map:
        return None
    s_lat, s_lon = stop_coord_map[start_st]
    e_lat, e_lon = stop_coord_map[end_st]

    start_ub = [u for u in get_ubike_near(s_lat, s_lon, ub_stations, ub_avail, 0.4) if u["available"] > 0]
    end_ub = [u for u in get_ubike_near(e_lat, e_lon, ub_stations, ub_avail, 0.4) if u["empty"] > 0]

    if not start_ub or not end_ub:
        return None

    dist_km, dist_text = get_osrm_bike_distance(s_lat, s_lon, e_lat, e_lon)
    if dist_km is None:
        dist_km = haversine(s_lat, s_lon, e_lat, e_lon)
        dist_text = f"{dist_km:.1f} 公里（直線估算）"
    bike_min = dist_km * UBIKE_MIN_PER_KM

    bus_total_min = (bus_wait_sec + bus_travel_sec) / 60

    if bike_min < bus_total_min * 0.85:
        best_start = start_ub[0]
        best_end = end_ub[0]
        return (
            f"🚲 UBike 更快！實際騎車約 {bike_min:.0f} 分鐘（{dist_text}），"
            f"比等公車+搭車（約 {bus_total_min:.0f} 分鐘）更省時。\n"
            f"- 起點 UBike：{best_start['name']}（可借 {best_start['available']} 輛）\n"
            f"- 終點 UBike：{best_end['name']}（可還 {best_end['empty']} 格）"
        )
    return None


def eta_status_text(eta, status):
    """對應時間軸上的狀態文字與 badge 樣式。
    原則：只要 TDX 有給實際預估時間（eta 不是 None），就一定要顯示出來，
    不能因為 StopStatus 剛好不是 0（例如 TDX 資料本身標記怪怪的）就被蓋成「尚未發車」。
    只有真的完全沒有 eta 資料時，才退回用 StopStatus 判斷文字。"""
    if eta is not None:
        if eta <= 120:
            return "即將進站", "ts-orange"
        return f"{eta // 60} 分鐘", "ts-green"
    if status == 1:
        return "尚未發車", "ts-gray"
    elif status == 2:
        return "交管不停靠", "ts-gray"
    elif status == 3:
        return "末班車已過", "ts-red"
    elif status == 4:
        return "今日停駛", "ts-red"
    elif status == 0:
        # TDX 標記這站是正常營運狀態，只是暫時沒有可用的預估到站時間（不代表沒發車）
        return "營運中（無預估時間）", "ts-gray"
    return "尚未發車", "ts-gray"


_WEEKDAY_KEYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _timetable_departure_time(t):
    """從一筆 Timetables（一個班次）取得這班車實際的發車時間。
    TDX 的 Bus/Schedule 資料結構裡，Timetables 物件本身並沒有 DepartureTime 欄位；
    真正的發車時間是藏在 StopTimes 裡「站序（StopSequence）為 1」那一站的 DepartureTime
    （官方文件：取 Timetables 裡每個 Trip 的 StopTimes，找 StopSequence=1 的離站時間，
    即為該班車的發車時間）。之前直接取 t.get('DepartureTime') 一定拿到空字串，
    導致時刻表畫面上每個時間格都是空的。"""
    stop_times = t.get("StopTimes") or []
    if stop_times:
        first = min(stop_times, key=lambda s: s.get("StopSequence", 999))
        dt = first.get("DepartureTime") or first.get("ArrivalTime") or ""
        if dt:
            return dt
    # 保險：萬一哪天 TDX 格式又改了、真的有頂層 DepartureTime，還是讀得到
    return t.get("DepartureTime", "")


def _schedule_departure_times(route, direction):
    """回傳某路線／方向，「今天」（依星期幾比對服務日曆）所有固定班次的發車時間
    （today 的 datetime 物件）。沒有固定時刻表資料就回傳空 list。
    只在即時動態／GPS 都查不到資料時，拿來當備援估算用，不是每次都查。"""
    raw = fetch_route_schedule(route)
    if not raw:
        return []
    now = datetime.now()
    weekday_key = _WEEKDAY_KEYS[now.weekday()]
    times = []
    for entry in raw:
        if entry.get("Direction", 0) != direction:
            continue
        for t in entry.get("Timetables", []):
            svc = t.get("ServiceDay") or {}
            # 有 ServiceDay 資訊的話要符合今天星期幾；完全沒標示服務日曆的視為每天都有發車
            if svc and not svc.get(weekday_key, False):
                continue
            dep_str = _timetable_departure_time(t)
            if not dep_str:
                continue
            try:
                parts = dep_str.split(":")
                dep_dt = now.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)
            except Exception:
                continue
            times.append(dep_dt)
    return times


def estimate_eta_from_schedule(dep_times, stop_index):
    """即時動態／GPS 都沒有這站的資料時，退回用固定時刻表概估到站時間：
    「發車時間 ＋ 每站約 2 分鐘（跟轉乘建議用的估算基準一致）」。
    僅供參考用的備援估算，不是真正的即時動態，所以呼叫端要另外標示清楚。"""
    if not dep_times:
        return None
    now = datetime.now()
    offset = timedelta(seconds=stop_index * 120)
    best = None
    for dep_dt in dep_times:
        diff = (dep_dt + offset - now).total_seconds()
        if diff < -60:
            # 這班車照時刻表推算應該早就過站了，不列入（可能是今天已經跑完的班次）
            continue
        diff = max(diff, 0)
        if best is None or diff < best:
            best = diff
    return best


# ══════════════════════════════════════════════════════════
# 頁面路由
# ══════════════════════════════════════════════════════════
@app.route('/')
def index():
    get_uid()
    return render_template('index.html',
                            route_categories=ROUTE_CATEGORIES)


# ══════════════════════════════════════════════════════════
# API：帳號登入
# ══════════════════════════════════════════════════════════
@app.route('/api/auth/status')
def api_auth_status():
    return jsonify({"username": session.get("username")})


@app.route('/api/auth/register', methods=['POST'])
def api_auth_register():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({"error": "請輸入帳號與密碼"}), 400
    if len(username) < 2:
        return jsonify({"error": "帳號至少需要 2 個字"}), 400
    if len(password) < 4:
        return jsonify({"error": "密碼至少需要 4 碼"}), 400
    if get_user_record(username):
        return jsonify({"error": "這個帳號已經被註冊了，請直接登入或換一個帳號"}), 400
    if not create_user_record(username, generate_password_hash(password)):
        return jsonify({"error": "帳號建立失敗，請稍後再試"}), 500
    _login_user(username)
    return jsonify({"username": username})


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    u = get_user_record(username)
    if not u or not check_password_hash(u.get("password_hash", ""), password):
        return jsonify({"error": "帳號或密碼錯誤"}), 401
    _login_user(username)
    return jsonify({"username": username})


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    session.pop("username", None)
    session.pop("uid", None)
    return jsonify({"ok": True})


@app.route('/api/admin/migrate_users', methods=['POST'])
def api_admin_migrate_users():
    """手動重新觸發一次「本機 users.json → Supabase」的舊帳號搬遷。
    平常用不到，只有在懷疑伺服器啟動當下搬遷沒有成功執行（例如剛設定好 Supabase
    環境變數但還沒重新部署、或本機檔案剛好還原得比較慢）時，拿來手動補跑一次，
    不用整個服務重新部署。回傳的內容會直接告訴你本機到底找到幾個帳號、搬了幾個。
    需要帶對 ADMIN_TOKEN（環境變數）才能執行，避免被任何人隨便觸發。"""
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        return jsonify({"error": "伺服器尚未設定 ADMIN_TOKEN 環境變數，無法使用這支端點"}), 400
    provided = (request.get_json(silent=True) or {}).get("token", "") or request.args.get("token", "")
    if provided != admin_token:
        return jsonify({"error": "未授權"}), 403
    result = _migrate_local_users_to_supabase()
    return jsonify(result)


# ══════════════════════════════════════════════════════════
# API：基礎資料
# ══════════════════════════════════════════════════════════
@app.route('/api/weather')
def api_weather():
    w = fetch_weather()
    get_state()["current_weather"] = w
    return jsonify({"weather": w})


@app.route('/api/route_categories')
def api_route_categories():
    return jsonify({"categories": ROUTE_CATEGORIES, "colors": ROUTE_COLOR_MAP})


@app.route('/api/filter_routes')
def api_filter_routes():
    """依照顏色／數字／分類篩選路線。支援一次選取多個篩選條件（用逗號分隔），
    只要符合任一個條件的路線都會列出（OR 邏輯），不是原本只能單選一種顏色/數字。"""
    raw_param = request.args.get('filter', '').strip()
    cf_list = [c.strip() for c in raw_param.replace('，', ',').split(',') if c.strip()]

    all_routes = []
    for rl in ROUTE_CATEGORIES.values():
        all_routes.extend(rl)
    seen_s = set()
    all_routes = [x for x in all_routes if not (x in seen_s or seen_s.add(x))]

    def match_one(cf):
        if cf == "市區":
            return ROUTE_CATEGORIES["市區"]
        if cf == "高鐵":
            return ROUTE_CATEGORIES["高鐵快捷"]
        if cf == "觀光":
            return ROUTE_CATEGORIES["觀光"]
        raw = [r for r in all_routes if cf in r]
        if cf.isdigit():
            def nsort(rs):
                nums = ''.join(c for c in rs if c.isdigit())
                return (0 if rs.startswith(cf) else 1, int(nums) if nums else 999, rs)
            return sorted(raw, key=nsort)
        return raw

    if not cf_list:
        filtered = all_routes
    else:
        # 多個篩選條件取聯集（符合任一個就列出），並保留 all_routes 原本的排序
        matched_set = set()
        for cf in cf_list:
            matched_set.update(match_one(cf))
        filtered = [r for r in all_routes if r in matched_set]
        # 「市區」「高鐵」「觀光」這幾類本身不在 all_routes 排序裡的路線，另外補上
        extra = [r for cf in cf_list for r in match_one(cf) if r not in filtered]
        seen_extra = set(filtered)
        for r in extra:
            if r not in seen_extra:
                filtered.append(r)
                seen_extra.add(r)

    return jsonify({"routes": filtered})


@app.route('/api/route_stops')
def api_route_stops():
    route = request.args.get('route', '')
    direction_label = request.args.get('direction', '')
    if not route:
        return jsonify({"stops": []})
    if direction_label in ('去程', '回程'):
        stops = fetch_route_stops_by_direction(route, 0 if direction_label == '去程' else 1)
    else:
        stops = fetch_route_stops(route)
    state = get_state()
    add_recent_route(state, route)
    return jsonify({"stops": stops})


@app.route('/api/route_status')
def api_route_status():
    route = request.args.get('route', '')
    direction = request.args.get('direction', '去程')
    start_st = request.args.get('start_st') or None
    end_st = request.args.get('end_st') or None
    if not route:
        return jsonify({"error": "缺少路線"}), 400

    state = get_state()

    # 深夜收班時段：不管快取裡還留著什麼舊資料，一律不顯示任何到站預估
    # （避免使用者看到收班前殘留的「5 分鐘」誤以為現在還有車），直接回覆
    # 「末班車已收車」，也不用再多花一次天氣查詢或站牌整理的運算。
    if not is_service_hours():
        return jsonify({
            "dest0": "", "dest1": "", "stops": [], "empty": True,
            "service_closed": True, "closed_message": SERVICE_CLOSED_MESSAGE,
        })

    weather_info = fetch_weather()
    state["current_weather"] = weather_info

    ensure_eta_fresh_for_request()   # 沒人使用期間輪詢是暫停的，第一個進來的人立刻補抓一次
    bus_list = fetch_bus_data(route)
    if bus_list is None:
        return jsonify({"error": "無法取得即時動態"}), 502

    # 到站預估是後台排程「抓取當下」的數字（尖峰 6 分鐘、平常 15 分鐘、週末 20 分鐘才更新一次），
    # 這裡依「現在 － 抓取時間」把每一站的剩餘秒數往下修正，使用者每次查詢（前端每分鐘會重新查）
    # 看到的都是『現在』的預估時間，而不是抓取當下那個已經過時的數字。
    _rt = get_realtime_status()
    eta_elapsed = max(0.0, time.time() - _rt["eta_fetched_at"]) if _rt["eta_fetched_at"] else 0.0
    if eta_elapsed > REALTIME_STALE_SECONDS:
        bus_list = []   # 快照太舊（例如隔夜、排程連續失敗），不拿來推算，避免所有站都變成「即將進站」

    dir0 = sorted([x for x in bus_list if x.get("Direction") == 0], key=lambda x: x.get('StopSequence', 0))
    dir1 = sorted([x for x in bus_list if x.get("Direction") == 1], key=lambda x: x.get('StopSequence', 0))
    dest_0 = dir0[-1].get("StopName", {}).get("Zh_tw", "去程") if dir0 else "去程"
    dest_1 = dir1[-1].get("StopName", {}).get("Zh_tw", "回程") if dir1 else "回程"

    active_list = dir0 if direction == "去程" else dir1
    target_dir = 0 if direction == "去程" else 1

    # 站點座標：改用跟站牌／地圖同一套「查一次、長期存檔」的資料來源，不要每次查即時動態
    # 都額外發一支獨立、沒有快取也沒有重試的即時 TDX 請求。原本那支請求只要剛好逾時或
    # TDX 忙線失敗，stop_coord_map 就會整個變空字典，GPS 定位備援（判斷『站上有車』）
    # 就會整組悄悄失效，這也是『明明有車在跑卻常常顯示尚未發車』的另一個主因。
    stop_coord_map = {}
    for sp in fetch_route_stop_positions(route):
        stop_coord_map[sp["name"]] = (sp["lat"], sp["lon"])

    ub_stations, ub_avail = fetch_ubike_all()
    # UBike 車數只在週一～五尖峰時段每小時更新一次，其餘時間是舊資料，一定要讓使用者知道是幾點的
    _ub_ts = get_ubike_fetched_at()
    ubike_hm = datetime.fromtimestamp(_ub_ts, TAIPEI_TZ).strftime("%m/%d %H:%M") if _ub_ts else ""
    realtime_map = {item.get("StopName", {}).get("Zh_tw", ""): item for item in active_list}
    all_stops_raw = fetch_route_stops_by_direction(route, target_dir)
    full_stop_list = all_stops_raw or [item.get("StopName", {}).get("Zh_tw", "") for item in active_list]

    if not full_stop_list:
        return jsonify({"dest0": dest_0, "dest1": dest_1, "stops": [], "empty": True})

    # TDX 的「逐站預估到站時間」（EstimatedTimeOfArrival）資料常常有缺口，
    # 尤其班次少、或車輛剛好在兩站中間時，容易讓明明有車在跑的站被判定成「尚未發車」。
    # 這裡另外抓「即時公車 GPS 定位」（RealTimeByFrequency，跟地圖用的是同一支端點），
    # 用最近站的方式回頭比對，確保只要 TDX 查得到這班車，時間軸上就不會漏掉它。
    # 公車定位快照只有「有人開地圖」時才會每小時更新，所以這裡只採信 10 分鐘內的快照，
    # 太舊的定位拿來判斷「站上有車」會誤報，寧可不用。
    gps_buses = get_positions_snapshot(route, max_age=POSITIONS_GPS_FALLBACK_MAX_AGE)
    gps_dir_buses = [b for b in gps_buses if b.get("Direction", 0) == target_dir]
    # 營運中的車輛數改用到站預估資料裡出現的「不重複車牌」來算（不依賴很少更新的 GPS 定位）
    active_bus_count = len({
        it.get("PlateNumb") for it in active_list
        if it.get("PlateNumb") and it.get("PlateNumb") not in ("🧱", "無車牌", "-1")
    })
    gps_near_stop = {}
    for b in gps_dir_buses:
        pos = b.get("BusPosition", {})
        b_lat, b_lon = pos.get("PositionLat"), pos.get("PositionLon")
        if not b_lat or not b_lon or not stop_coord_map:
            continue
        best_name, best_dist = None, None
        for name, (s_lat, s_lon) in stop_coord_map.items():
            d = haversine(b_lat, b_lon, s_lat, s_lon)
            if best_dist is None or d < best_dist:
                best_dist, best_name = d, name
        if best_name and best_dist is not None and best_dist <= 0.5:  # 500 公尺內才採信
            gps_near_stop.setdefault(best_name, []).append(b)

    # UBike 建議
    ubike_suggestion = None
    if start_st and end_st and start_st != end_st:
        start_item = realtime_map.get(start_st, {})
        _raw_wait = start_item.get("EstimateTime")
        bus_wait = max(0, _raw_wait - eta_elapsed) if _raw_wait is not None else 0
        if start_st in full_stop_list and end_st in full_stop_list:
            idx_s = full_stop_list.index(start_st)
            idx_e = full_stop_list.index(end_st)
            bus_travel = abs(idx_e - idx_s) * 120
        else:
            bus_travel = 600
        ubike_suggestion = check_ubike_suggestion(
            start_st, end_st, stop_coord_map, ub_stations, ub_avail, bus_wait, bus_travel)
        if ubike_suggestion and ubike_hm:
            ubike_suggestion += f"\n（UBike 車數為 {ubike_hm} 的資料，僅供參考）"

    tts_lines = [f"路線 {route}，往 {dest_0 if direction == '去程' else dest_1}方向。"]
    stops_out = []
    seen_plates = set()  # 用來讓同一輛實體公車只在最接近的站顯示一次（依目前位置單一顯示）
    last_anchor = None  # (stop_index, eta_seconds)：最近一個「有真實資料佐證」的定錨點，
                         # 用來推算後面沒有 TDX 資料的站，見下面迴圈內的說明。
    main_dest = dest_0 if direction == "去程" else dest_1
    # 即時動態／GPS 都查不到資料時的最後備援：用固定時刻表概估到站時間（例如「尚未發車」
    # 但其實時刻表上等一下就有一班車），這裡只查一次，下面逐站比對時重複使用。
    schedule_dep_times = _schedule_departure_times(route, target_dir)

    for idx, s_name in enumerate(full_stop_list):
        item = realtime_map.get(s_name, {})
        eta = item.get("EstimateTime")
        if eta is not None:
            eta = max(0, eta - eta_elapsed)   # 修正成「現在」的剩餘秒數
        status = item.get("StopStatus", 1)
        plate = item.get("PlateNumb", "")
        v_type = item.get("VehicleType")
        is_ev = item.get("IsElectric", False) or (v_type == 5)

        # 無障礙判斷（大巴預設有，小巴/中巴看 IsLowFloor）
        if v_type == 1:
            is_low, car_size = True, "大巴"
        elif v_type == 2:
            is_low, car_size = item.get("IsLowFloor", False), "中巴"
        elif v_type == 3:
            is_low, car_size = item.get("IsLowFloor", False), "小巴"
        else:
            is_low, car_size = item.get("IsLowFloor", False), "大巴"

        time_text, badge_class = eta_status_text(eta, status)
        eta_seconds_out = eta  # 給前端做「即時倒數」用的數字秒數，跟著 time_text 一起維護

        # ETA 端點沒有這站的資料，但 GPS 定位確認附近真的有車在跑 → 用 GPS 資料補正，
        # 不要讓使用者看到「尚未發車」卻其實漏掉了一班查得到的車
        gps_here = gps_near_stop.get(s_name)
        if eta is None and status in (0, 1) and gps_here:
            gps_bus = gps_here[0]
            if not plate:
                plate = gps_bus.get("PlateNumb", "")
            time_text, badge_class = "進站中", "ts-red"
            eta_seconds_out = 0

        # 即時動態、GPS 都完全查不到這一站的資料時，分兩種情況處理：
        # ① 這條路線這個方向「後面已經有確認在跑的車」（前面某一站有真的 TDX eta，
        #    或 GPS 定位確認進站中）→ 用那個真實定錨點往下推算：每站約 2 分鐘，
        #    樣式（顏色、文字格式）直接沿用 eta_status_text()，跟真的 TDX 資料
        #    長得一模一樣，不特別標示成估計——因為這是根據已經確認在跑的車推算，
        #    可信度遠比單純查時刻表高，沒必要讓使用者覺得「這站資料比較不可靠」。
        # ② 整條路線這個方向到目前為止都還沒有任何真的在跑的車可以當基準
        #    （沒有定錨點）→ 才退回用固定時刻表概估（僅供參考，這種情況才需要
        #    清楚標示「時刻表估計」，因為這只是查時刻表猜的，不是根據真的車在推算）。
        # 兩種都只在 status 是 0（營運中，只是沒給預估時間）或 1（尚未發車）才適用，
        # status 2/3/4（交管不停靠／末班車已過／今日停駛）代表這站明確不會有車，
        # 不能因為想補資料就蓋掉這個明確的狀態。
        est_from_schedule = None
        is_calculated_estimate = False
        if eta is not None:
            last_anchor = (idx, eta)
        elif gps_here:
            last_anchor = (idx, 0)
        elif status in (0, 1) and last_anchor is not None:
            anchor_idx, anchor_eta = last_anchor
            calc_eta = anchor_eta + (idx - anchor_idx) * 120
            time_text, badge_class = eta_status_text(calc_eta, status)
            eta_seconds_out = calc_eta
            is_calculated_estimate = True
        elif status == 1:
            est_from_schedule = estimate_eta_from_schedule(schedule_dep_times, idx)
            if est_from_schedule is not None:
                mins = int(est_from_schedule // 60)
                time_text = "即將進站（時刻表估計）" if mins <= 0 else f"約 {mins} 分鐘（時刻表估計）"
                badge_class = "ts-blue"
                eta_seconds_out = est_from_schedule

        # 支線／繞道：這一班車實際開往的目的地跟這條路線平常公告的方向不一樣時，
        # 特別標示出來，不要讓人誤以為所有車都開到同一個終點站。
        sub_route = (item.get("SubRouteName") or {}).get("Zh_tw", "") if item else ""
        dest_stop = (item.get("DestinationStopNameZh") or "") if item else ""
        if not dest_stop and not sub_route and gps_here:
            gb = gps_here[0]
            dest_stop = gb.get("DestinationStopNameZh", "") or ""
            sub_route = (gb.get("SubRouteName") or {}).get("Zh_tw", "")
        branch_label = ""
        if dest_stop and dest_stop != main_dest:
            branch_label = dest_stop
        elif sub_route and sub_route != route:
            branch_label = sub_route

        ubikes_near = []
        if s_name in stop_coord_map:
            s_lat, s_lon = stop_coord_map[s_name]
            for ub in get_ubike_near(s_lat, s_lon, ub_stations, ub_avail):
                ubikes_near.append(ub)

        raw_has_bus = bool(plate) and plate not in ("🧱", "無車牌")
        # 同一車牌只在最先出現（也就是離該車目前位置最近）的站顯示公車標籤，
        # 避免同一輛車因為連續多站都被列為「下一班」而重複顯示。
        show_bus_tag = False
        if raw_has_bus:
            if plate not in seen_plates:
                seen_plates.add(plate)
                show_bus_tag = True

        # 車型／無障礙／經過路線（支線、繞道）這些都是「這一輛車」的資訊，只要
        # 車輛標籤本身只在最近站顯示一次，這些附屬資訊也應該只跟著出現那一次，
        # 不然同一班繞道公車經過的每一站都會重複顯示同樣的「🔀 往 X」，變得很雜。
        stops_out.append({
            "name": s_name,
            "eta_text": time_text,
            "eta_seconds": eta_seconds_out,
            "badge_class": badge_class,
            "plate": plate if show_bus_tag else "",
            "car_size": car_size,
            "is_low": bool(is_low),
            "is_ev": bool(is_ev),
            "has_bus": show_bus_tag,
            "ubikes": ubikes_near,
            "is_waiting_stop": bool(start_st and s_name == start_st),
            "branch": branch_label if show_bus_tag else "",
            "is_schedule_estimate": est_from_schedule is not None,
            "is_calculated_estimate": is_calculated_estimate,
        })

        if start_st and s_name == start_st:
            tts_lines.append(f"等候站 {s_name}，{time_text}。{'無障礙低底盤。' if is_low else ''}{'電動公車。' if is_ev else ''}")

    # 給 AI 助理當「這條路線目前狀況」的依據：整條路線每一站的動態都給，而不是只給
    # 使用者選的那一個等候站，這樣使用者在對話裡問到「XX站」時 AI 才有實際資料可以回答，
    # 不用亂猜、亂編。同時附上資料擷取時間，AI 才知道這份資料可能已經過一段時間了，
    # 該提醒使用者「僅供參考、請以查詢頁面上最新結果為準」，而不是講得像絕對正確。
    ai_stop_summary = [
        {"站": s["name"], "動態": s["eta_text"], "車牌": s["plate"] or "無",
         **({"往": s["branch"]} if s["branch"] else {})}
        for s in stops_out
    ][:30]  # 站數太多的路線只取前 30 站，避免塞爆 AI 的對話上下文
    bus_status = (
        f"路線：{route}（往{direction}方向，往{main_dest}）。"
        f"資料擷取時間：{datetime.now().strftime('%H:%M:%S')}。"
        f"逐站動態：{json.dumps(ai_stop_summary, ensure_ascii=False)}"
    )
    state["bus_status"] = bus_status

    realtime_status = get_realtime_status()
    # 支線／繞道摘要：把整條路線目前查得到的支線車輛集中列一份清單，
    # 給前端「展開箭頭」用，不用使用者自己一站一站找哪裡有標示支線。
    branch_summary = [
        {"stop": s["name"], "plate": s["plate"], "to": s["branch"]}
        for s in stops_out if s["branch"]
    ]
    return jsonify({
        "dest0": dest_0, "dest1": dest_1,
        "weather": weather_info,
        "stops": stops_out,
        "branches": branch_summary,
        "ubike_suggestion": ubike_suggestion,
        "tts_text": "".join(tts_lines),
        "active_bus_count": active_bus_count,
        "ubike_updated_hm": ubike_hm,
        "empty": False,
        "service_closed": False,
        "data_fresh": realtime_status["is_fresh"],
        "data_updated_at": realtime_status["updated_at"],
        "eta_fetched_at": time.time(),   # eta_seconds 已經修正成「現在」的數字，所以基準時間就是現在
        "data_age_seconds": realtime_status["age_seconds"],
    })


@app.route('/api/nearby_stops')
def api_nearby_stops():
    try:
        lat = float(request.args.get('lat'))
        lon = float(request.args.get('lon'))
    except (TypeError, ValueError):
        return jsonify({"error": "請輸入有效數字"}), 400
    all_stops_data = fetch_all_bus_stops()
    if not all_stops_data:
        return jsonify({"error": "無法載入站牌資料"}), 502
    nearby = find_nearby_stops(all_stops_data, lat, lon)
    return jsonify({"nearby": nearby})


# ══════════════════════════════════════════════════════════
# API：最愛 / 最近查詢
# ══════════════════════════════════════════════════════════
@app.route('/api/favorites', methods=['GET'])
def api_favorites_get():
    return jsonify({"favorites": get_state()["favorite_routes"]})


@app.route('/api/favorites/toggle', methods=['POST'])
def api_favorites_toggle():
    route = (request.json or {}).get('route')
    state = get_state()
    fav = state["favorite_routes"]
    if route in fav:
        fav.remove(route)
        is_fav = False
    else:
        fav.append(route)
        is_fav = True
    return jsonify({"favorites": fav, "is_favorite": is_fav})


@app.route('/api/recent', methods=['GET'])
def api_recent_get():
    return jsonify({"recent": get_state()["recent_routes"]})


# ══════════════════════════════════════════════════════════
# API：到站鈴聲提醒
# ══════════════════════════════════════════════════════════
@app.route('/api/reminders', methods=['GET'])
def api_reminders_get():
    return jsonify({"reminders": get_state()["reminders"]})


@app.route('/api/reminders/add', methods=['POST'])
def api_reminders_add():
    data = request.get_json(silent=True) or {}
    route = (data.get('route') or '').strip()
    direction = (data.get('direction') or '去程').strip()
    stop = (data.get('stop') or '').strip()
    try:
        alert_minutes = int(data.get('alert_minutes', 5))
    except (TypeError, ValueError):
        alert_minutes = 5
    alert_minutes = max(1, min(alert_minutes, 60))
    if not route or not stop:
        return jsonify({"error": "請先選擇路線與站牌，再加入到站提醒"}), 400

    state = get_state()
    reminders = state["reminders"]
    # 同一條路線＋方向＋站牌只留一筆，重複加入的話當作只是要改提醒時間，不要一直疊出重複項目
    for r in reminders:
        if r["route"] == route and r["direction"] == direction and r["stop"] == stop:
            r["alert_minutes"] = alert_minutes
            return jsonify({"reminders": reminders})
    if len(reminders) >= 20:
        return jsonify({"error": "最多只能設定 20 個到站提醒，請先刪除幾個舊的"}), 400
    reminders.append({
        "id": str(uuid.uuid4())[:8],
        "route": route, "direction": direction, "stop": stop,
        "alert_minutes": alert_minutes,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    return jsonify({"reminders": reminders})


@app.route('/api/reminders/update', methods=['POST'])
def api_reminders_update():
    data = request.get_json(silent=True) or {}
    rid = data.get('id')
    state = get_state()
    reminders = state["reminders"]
    for r in reminders:
        if r["id"] == rid:
            if "alert_minutes" in data:
                try:
                    r["alert_minutes"] = max(1, min(int(data["alert_minutes"]), 60))
                except (TypeError, ValueError):
                    pass
            break
    return jsonify({"reminders": reminders})


@app.route('/api/reminders/delete', methods=['POST'])
def api_reminders_delete():
    rid = (request.get_json(silent=True) or {}).get('id')
    state = get_state()
    state["reminders"] = [r for r in state["reminders"] if r["id"] != rid]
    return jsonify({"reminders": state["reminders"]})


# ══════════════════════════════════════════════════════════
# API：進階查詢（站到站）
# ══════════════════════════════════════════════════════════
@app.route('/api/advanced_search/stops')
def api_advanced_stops():
    stop_index = build_stop_route_index()
    return jsonify({"stops": sorted(stop_index.keys())})


@app.route('/api/advanced_search')
def api_advanced_search():
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    if not start or not end:
        return jsonify({"error": "請選擇出發站和目的站"}), 400
    if start == end:
        return jsonify({"error": "出發站和目的站不能相同"}), 400
    stop_index = build_stop_route_index()
    if not stop_index:
        return jsonify({"error": "暫時無法取得站點資料，請稍後再試"}), 400
    # 優先用「依方向、依站序」的規劃（直達要方向對、轉乘要能到得了，附站數與下一班），
    # 站序資料還沒準備好時才退回舊的「兩站都有經過的路線」粗略比對。
    try:
        plans = plan_trip(start, end)
    except Exception as e:
        print(f"⚠️ 路線規劃發生例外，退回舊版比對：{e}", flush=True)
        plans = None
    if plans and plans.get("index_ok"):
        directs, seen = [], set()
        for p in plans["direct_plans"]:
            if p["route"] not in seen:
                seen.add(p["route"])
                directs.append(p["route"])
        transfers = [{"routeA": p["routeA"], "transfer": p["transfer"], "routeB": p["routeB"]}
                     for p in plans["transfer_plans"]]
        return jsonify({"directs": directs, "transfers": transfers,
                        "direct_plans": plans["direct_plans"],
                        "transfer_plans": plans["transfer_plans"]})
    directs = find_direct_routes(stop_index, start, end)
    transfers = find_transfer_routes(stop_index, start, end)
    return jsonify({"directs": directs, "transfers": transfers})


@app.route('/api/plan_times')
def api_plan_times():
    """「運算轉乘時間」按鈕：回傳目前每個直達／轉乘方案的預估時間明細（本地估算，不打 TDX）。
    用 id 對應 /api/advanced_search 回傳的 direct_plans／transfer_plans。"""
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    if not start or not end or start == end:
        return jsonify({"error": "請選擇出發站和目的站"}), 400
    ensure_eta_fresh_for_request()   # 沒人使用期間輪詢暫停了，第一次查詢時先補一份新的到站快照
    try:
        plans = plan_trip(start, end)
        if not plans.get("index_ok"):
            return jsonify({"error": "站序資料還沒準備好，請稍後再試"}), 400
        times = estimate_plan_times(start, plans)
    except Exception as e:
        print(f"⚠️ 轉乘時間估算失敗：{e}", flush=True)
        return jsonify({"error": "轉乘時間估算失敗，請稍後再試"}), 500
    return jsonify({"times": times, "min_per_stop": PLAN_MIN_PER_STOP,
                    "now": _taipei_now().strftime("%H:%M")})


# ══════════════════════════════════════════════════════════
# API：地圖頁面
# ══════════════════════════════════════════════════════════
def _parse_route_filter():
    r_filter = request.args.get('routes', '')
    filter_list = [r.strip() for r in r_filter.replace("，", ",").split(',') if r.strip()] if r_filter else []
    # 若使用者輸入「全部」（或包含在清單中），視同未篩選，顯示全部路線的公車
    if any(x in ("全部", "全部路線") for x in filter_list):
        filter_list = []
    return filter_list


def _collect_bus_features(filter_list, service_closed):
    """把定位快照整理成地圖用的公車清單。收班時段（23:00-05:50）不回傳任何公車。"""
    if service_closed:
        return [], set()
    if filter_list:
        all_buses = []
        for r in filter_list:
            all_buses.extend(get_positions_snapshot(r))
    else:
        all_buses = get_positions_snapshot()

    bus_features = []
    live_route_set = set()
    for bus in all_buses:
        pos = bus.get("BusPosition", {})
        lat, lon = pos.get("PositionLat"), pos.get("PositionLon")
        route = bus.get("RouteName", {}).get("Zh_tw", "")
        if not lat or not lon or not route:
            continue
        live_route_set.add(route)
        # 有些車是繞道／支線行駛，實際終點跟路線平常公告的不一樣，這裡一併帶出來
        sub_route = (bus.get("SubRouteName") or {}).get("Zh_tw", "")
        dest_stop = bus.get("DestinationStopNameZh", "") or ""
        branch = dest_stop or (sub_route if sub_route and sub_route != route else "")
        bus_features.append({
            "lat": lat, "lon": lon, "route": route,
            "plate": bus.get("PlateNumb", ""),
            "dir": "去程" if bus.get("Direction", 0) == 0 else "回程",
            "speed": bus.get("Speed", "?"),
            "color": get_route_color(route),
            "branch": branch,
        })
    return bus_features, live_route_set


def _positions_age_seconds(fetched_at):
    """定位快照『現在』有多舊（秒）。前端用這個數字算公車沿路線走到哪一站了，
    刻意回傳「年齡」而不是時間戳，才不會被使用者手機時鐘不準影響。"""
    return None if not fetched_at else max(0, int(time.time() - fetched_at))


@app.route('/api/map_data')
def api_map_data():
    filter_list = _parse_route_filter()

    # 有人開地圖才會走到這裡：定位快照超過 1 小時才向 TDX 抓 1 次，否則直接讀檔
    pos_status = ensure_positions_fresh_for_map()
    service_closed = pos_status["service_closed"]
    bus_features, live_route_set = _collect_bus_features(filter_list, service_closed)

    # 無論該路線目前有沒有營運中的公車，都要能顯示其路線軌跡與站牌，
    # 因此路線清單一律使用「使用者指定的篩選清單」或「系統設定的全部路線＋已存檔路線」。
    routes_to_draw = filter_list if filter_list else get_all_known_routes()

    shape_map, stop_map = fetch_shapes_and_stops_parallel(routes_to_draw)

    shape_features = []
    stop_features = []
    stop_seq_features = []
    for r in routes_to_draw:
        color = get_route_color(r)
        for sh in shape_map.get(r, []):
            pts = parse_wkt_linestring(sh.get("Geometry", ""))
            if pts:
                shape_features.append({"route": r, "color": color, "points": pts,
                                       "dir": sh.get("Direction", 0)})
        for sp in stop_map.get(r, []):
            stop_features.append({
                "route": r, "name": sp["name"],
                "lat": sp["lat"], "lon": sp["lon"], "color": color
            })
        # 站牌順序（依方向）：前端讓公車沿路線「每 5 分鐘走一站」時要用
        for seq in fetch_route_stop_sequences(r):
            stop_seq_features.append({"route": r, "dir": seq["dir"], "stops": seq["stops"]})

    realtime_status = get_realtime_status()
    return jsonify({
        "buses": bus_features,
        "shapes": shape_features,
        "stops": stop_features,
        "stop_seq": stop_seq_features,
        "routes": routes_to_draw,
        "saved_routes": sorted(get_saved_route_names()),
        "live_routes": sorted(live_route_set),
        "now": datetime.now().strftime("%H:%M:%S"),
        "service_closed": service_closed,
        "closed_message": SERVICE_CLOSED_MESSAGE if service_closed else "",
        "positions_age_seconds": _positions_age_seconds(pos_status["fetched_at"]),
        "data_fresh": realtime_status["is_fresh"],
        "data_updated_at": realtime_status["updated_at"],
        "data_age_seconds": realtime_status["age_seconds"],
    })


@app.route('/api/map_buses')
def api_map_buses():
    """輕量版：只回傳公車定位（不含軌跡／站牌），給前端『每小時更新一次定位』用。
    跟 /api/map_data 一樣，只有定位快照超過 1 小時才會真的向 TDX 抓。"""
    filter_list = _parse_route_filter()
    pos_status = ensure_positions_fresh_for_map()
    service_closed = pos_status["service_closed"]
    bus_features, live_route_set = _collect_bus_features(filter_list, service_closed)
    return jsonify({
        "buses": bus_features,
        "live_routes": sorted(live_route_set),
        "now": datetime.now().strftime("%H:%M:%S"),
        "service_closed": service_closed,
        "closed_message": SERVICE_CLOSED_MESSAGE if service_closed else "",
        "positions_age_seconds": _positions_age_seconds(pos_status["fetched_at"]),
    })


@app.route('/api/map_route_list')
def api_map_route_list():
    """輕量版：只回傳『全部已知路線』的名稱清單＋已存檔清單，完全不去查即時公車動態、
    軌跡、站牌。地圖頁面一打開只需要這份輕量資料就能先畫出路線選單，不用像以前那樣
    一進地圖頁就把全台南所有路線（含公車位置、軌跡、站牌）整個抓一遍——那樣很吃 TDX
    的查詢額度、也會拖慢開頁速度。改成只有使用者真的按下「全部路線」或勾選特定路線時，
    才去抓那些路線實際的地圖資料（見 /api/map_data）。"""
    all_routes = get_all_known_routes()
    return jsonify({
        "routes": all_routes,
        "saved_routes": sorted(get_saved_route_names()),
    })


@app.route('/api/saved_routes')
def api_saved_routes():
    """列出目前 /opt/render/project/data/route 底下實際已經存檔（Shape 或 StopOfRoute）
    的路線清單。用於地圖頁面顯示『已儲存路線』按鈕清單——只列出真的有資料的路線，
    而不是系統設定裡的全部路線清單。"""
    return jsonify({"routes": sorted(get_saved_route_names())})


_YELLOW_BUS_OPERATOR_KEYWORDS = ("大車隊", "皇冠交通", "衛星")  # 台一大車隊／中華衛星台南車隊（皇冠交通）


@app.route('/api/yellow_bus_routes')
def api_yellow_bus_routes():
    """列出台南『小黃公車』的路線清單。
    小黃公車本質上是用計程車營運一般公車路線，用的仍然是同一套 TDX 公車 API
    （StopOfRoute／Shape／即時動態），差別只在營運業者是台一大車隊／中華衛星台南車隊
    （皇冠交通），所以這裡直接用 TDX 路線清單的『營運業者』欄位反查，
    不用另外接一套 API，路線異動時也不用手動維護清單。"""
    routes = fetch_all_route_meta()
    result = []
    seen = set()
    for r in routes:
        name = (r.get("RouteName") or {}).get("Zh_tw", "")
        ops = r.get("Operators") or []
        op_names = [(op.get("OperatorName") or {}).get("Zh_tw", "") for op in ops]
        if not name or name in seen:
            continue
        if any(any(kw in on for kw in _YELLOW_BUS_OPERATOR_KEYWORDS) for on in op_names):
            seen.add(name)
            result.append({"route_name": name, "operators": [o for o in op_names if o]})
    result.sort(key=lambda x: x["route_name"])
    return jsonify({"routes": result, "total": len(result)})


def fetch_route_schedule(route_name):
    """取得某路線的固定時刻表（TDX Bus/Schedule）。
    小黃公車、支線公車大多是固定班次時刻，跟幹線那種『依班距發車』不一樣，
    所以另外用這支端點取時刻表，而不是即時動態。
    跟站牌／軌跡資料走同一套「查一次、長期存檔」的邏輯（load_route_timetable_data
    本身就會優先讀取已存檔的資料），這裡刻意不再疊加額外的短 TTL 記憶體快取——
    之前疊了一層 10 分鐘的快取，會連『TDX 暫時查詢頻率過高、這次剛好查空了』的
    空結果都一起快取住，導致明明時刻表存檔還在，畫面卻有 10 分鐘看起來像沒有
    時刻表資料。拿掉這層之後行為才會跟站牌資料一致：查得到就直接長期沿用存檔，
    查不到就下一次請求時重試，不會被自己快取卡住。"""
    return load_route_timetable_data(route_name)


@app.route('/api/timetable')
def api_timetable():
    """回傳某路線的固定時刻表，依方向（去程/回程）＋平日/假日整理成班次時間清單。"""
    route = request.args.get('route', '').strip()
    if not route:
        return jsonify({"error": "缺少路線名稱"}), 400
    raw = fetch_route_schedule(route)
    if not raw:
        return jsonify({"route": route, "directions": [], "has_data": False,
                         "message": "TDX 目前查不到這條路線的固定時刻表（可能是依班距發車的幹線，沒有公告時刻表）"})

    directions = []
    for entry in raw:
        try:
            direction = entry.get("Direction", 0)
            dest = (entry.get("DestinationStopNameZh") or
                    (entry.get("SubRouteName") or {}).get("Zh_tw") or "")
            timetables = entry.get("Timetables", [])
            # 依服務日曆（平日/假日/全週…）分組，每組列出所有發車時間
            groups = {}
            for t in timetables:
                svc = (t.get("ServiceDay") or {})
                label_bits = []
                for k, zh in (("Monday", "一"), ("Tuesday", "二"), ("Wednesday", "三"),
                              ("Thursday", "四"), ("Friday", "五"), ("Saturday", "六"), ("Sunday", "日")):
                    if svc.get(k):
                        label_bits.append(zh)
                label = "、".join(label_bits) if label_bits else "每日"
                dep_str = _timetable_departure_time(t)
                if not dep_str:
                    continue
                groups.setdefault(label, []).append(dep_str)
            for label, times in groups.items():
                times.sort()
            directions.append({
                "direction": direction,
                "destination": dest,
                "groups": [{"days": k, "times": v} for k, v in groups.items()],
            })
        except Exception:
            continue

    return jsonify({"route": route, "directions": directions, "has_data": bool(directions)})


@app.route('/api/route_lookup')
def api_route_lookup():
    """反查 TDX 上『真正登記』的路線名稱是什麼。
    當某條路線用系統設定的名稱查不到即時定位、也查不到 Shape/StopOfRoute 時，
    很可能是這個名稱跟 TDX 實際登記的不完全一樣（例如改名、整併過），
    這裡直接去 TDX 的路線清單裡做包含比對，讓使用者確認正確名稱，而不是用猜的。"""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({"matches": []})
    routes = fetch_all_route_meta()
    matches = []
    seen = set()
    for r in routes:
        name = (r.get("RouteName") or {}).get("Zh_tw", "")
        if name and q in name and name not in seen:
            seen.add(name)
            ops = r.get("Operators") or []
            op_names = [(op.get("OperatorName") or {}).get("Zh_tw", "") for op in ops]
            matches.append({
                "route_name": name,
                "route_uid": r.get("RouteUID", ""),
                "operators": [o for o in op_names if o],
            })
    return jsonify({"query": q, "matches": matches, "total_routes_checked": len(routes)})


@app.route('/api/save_route_data', methods=['POST'])
def api_save_route_data():
    """從 TDX 即時抓取指定路線的「路線軌跡（Shape）」與「站牌清單（StopOfRoute）」，
    強制重新查詢並把 TDX 回傳的原始 JSON 內容原封不動存成兩份檔案：
      /opt/render/project/data/route/{路線名稱}_route_shape.json
      /opt/render/project/data/route/{路線名稱}_route_stop.json
    （一般情況下不需要手動按這顆按鈕——每條路線第一次被查詢或在地圖上顯示時，
    就會自動存檔；這顆按鈕只是用來強制刷新單一路線的最新資料。）
    """
    route = (request.json or {}).get('route', '').strip()
    if not route:
        return jsonify({"error": "請輸入路線名稱"}), 400

    shape_data = _fetch_and_save_shape_data(route)
    stop_data = _fetch_and_save_stop_data(route)

    if not shape_data and not stop_data:
        # 查不到資料時，順便反查 TDX 上名稱相近的路線，幫忙抓出可能是「名稱對不起來」的狀況
        keyword = route[0] if route else route
        suggestions = []
        try:
            for r in fetch_all_route_meta():
                name = (r.get("RouteName") or {}).get("Zh_tw", "")
                if name and keyword in name and name != route and name not in suggestions:
                    suggestions.append(name)
        except Exception:
            pass
        msg = (f"無法從 TDX 取得路線「{route}」的軌跡或站牌資料，請確認路線名稱是否正確"
               "（也可能是今天的手動查詢額度 10 次已用完，TDX 月額度很緊，明天再試）")
        if suggestions:
            msg += f"。TDX 上名稱相近的路線有：{'、'.join(suggestions[:8])}"
        return jsonify({"error": msg, "suggestions": suggestions[:8]}), 404

    _invalidate_route_cache(route)

    shape_segments = len(shape_data) if isinstance(shape_data, list) else 0
    stop_count = 0
    if isinstance(stop_data, list):
        for dir_data in stop_data:
            stop_count += len(dir_data.get("Stops", []))

    return jsonify({
        "status": "success",
        "route": route,
        "shape_file": _route_shape_file_path(route),
        "stop_file": _route_stop_file_path(route),
        "shape_segments": shape_segments,
        "stop_count": stop_count,
        "shape_ok": bool(shape_data),
        "stop_ok": bool(stop_data),
    })



# ══════════════════════════════════════════════════════════
# 歷史班距統計：把每次抓到的到站預估「順便」整理成班距資料（完全不多打 TDX）
# ══════════════════════════════════════════════════════════
# 做法：每條路線、每個方向挑一個「參考站」（站序中間那一站），每次到站快照取該站
# 『下一班』的預測到站時間；同一班車的預測會隨快照更新、合併成一筆，等它的預測時間
# 過去了就定案，跟上一班定案時間相減就是一個「班距」。再依「上班日／假日」與「小時」
# 分桶，每桶只留最近 12 筆，取中位數（漏抓一班造成的雙倍班距不會把平均拉高）。
# 限制：快照間隔離峰是 15 分鐘，班距短於這個值的路線會因為漏抓而偏長，樣本數也會
# 偏少，所以只有樣本夠（>=3）才會被拿來用。
HEADWAY_FILE = os.path.join(DATA_DIR, "headway_stats.json")
HEADWAY_MIN_GAP_MIN = 2
HEADWAY_MAX_GAP_MIN = 90
HEADWAY_KEEP_PER_BUCKET = 12
HEADWAY_MIN_SAMPLES = 3
_headway_write_lock = threading.Lock()


def _headway_daytype(dt):
    return "wd" if is_workday(dt) else "we"


def headway_record(eta_by_route, fetch_ts):
    """每次到站快照更新後呼叫一次（只在拿到鎖的那個 worker 呼叫）。"""
    tz = _taipei_now().tzinfo
    with _headway_write_lock:
        try:
            with open(HEADWAY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        routes = data.setdefault("routes", {})
        changed = False
        for rname, items in (eta_by_route or {}).items():
            by_dir = {}
            for it in items:
                d = it.get("Direction")
                if d in (0, 1):
                    by_dir.setdefault(d, []).append(it)
            for d, its in by_dir.items():
                key = f"{rname}|{d}"
                rec = routes.get(key)
                if rec is None:
                    seqd = sorted((x for x in its if x.get("StopSequence") is not None),
                                  key=lambda x: x["StopSequence"])
                    if not seqd:
                        continue
                    ref = (seqd[len(seqd) // 2].get("StopName") or {}).get("Zh_tw")
                    if not ref:
                        continue
                    rec = routes[key] = {"stop": ref, "pending": [], "last_final": None,
                                         "bins": {"wd": {}, "we": {}}}
                cand = None
                for x in its:
                    if (x.get("StopName") or {}).get("Zh_tw") == rec["stop"] and x.get("EstimateTime") is not None:
                        if cand is None or x["EstimateTime"] < cand["EstimateTime"]:
                            cand = x
                if cand is not None and cand["EstimateTime"] <= 2400:
                    arr = int(fetch_ts + cand["EstimateTime"])
                    plate = cand.get("PlateNumb") or ""
                    if plate in ("-1", "無車牌"):
                        plate = ""
                    for ev in rec["pending"]:
                        if (plate and ev[1] == plate and abs(ev[0] - arr) <= 900) or abs(ev[0] - arr) <= 120:
                            ev[0], ev[1] = arr, (plate or ev[1])
                            break
                    else:
                        rec["pending"].append([arr, plate])
                    changed = True
                # 預測時間已經過去的就定案
                rec["pending"].sort()
                keep = []
                for ev in rec["pending"]:
                    if ev[0] < fetch_ts - 60:
                        prev = rec.get("last_final")
                        if prev is not None:
                            gap = (ev[0] - prev) / 60.0
                            if HEADWAY_MIN_GAP_MIN <= gap <= HEADWAY_MAX_GAP_MIN:
                                dt = datetime.fromtimestamp(ev[0], tz)
                                bucket = rec["bins"][_headway_daytype(dt)].setdefault(str(dt.hour), [])
                                bucket.append(round(gap, 1))
                                del bucket[:-HEADWAY_KEEP_PER_BUCKET]
                        rec["last_final"] = ev[0]
                        changed = True
                    else:
                        keep.append(ev)
                rec["pending"] = keep[-6:]
        if changed:
            data["updated_at"] = int(fetch_ts)
            _atomic_write_json(HEADWAY_FILE, data)


def _median(vals):
    v = sorted(vals)
    n = len(v)
    return None if not n else (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0)


def headway_lookup(route, direction, now=None):
    """這條路線現在這個時段（上班日／假日 × 小時）的歷史班距中位數（分鐘）；樣本不夠回傳 None。"""
    now = now or _taipei_now()
    data = _read_json_cached(HEADWAY_FILE, {})
    rec = (data.get("routes") or {}).get(f"{route}|{direction}")
    if not rec:
        return None
    bins = rec["bins"].get(_headway_daytype(now), {})
    gaps = list(bins.get(str(now.hour), []))
    if len(gaps) < HEADWAY_MIN_SAMPLES:        # 這個小時樣本不夠，併入前後一小時一起看
        gaps += bins.get(str((now.hour - 1) % 24), []) + bins.get(str((now.hour + 1) % 24), [])
    if len(gaps) < HEADWAY_MIN_SAMPLES:
        return None
    return {"median": _median(gaps), "n": len(gaps)}


@app.route('/api/headway')
def api_headway():
    """某路線的歷史班距統計（依方向、上班日／假日、小時）。"""
    route = request.args.get('route', '').strip()
    if not route:
        return jsonify({"error": "缺少路線名稱"}), 400
    now = _taipei_now()
    data = _read_json_cached(HEADWAY_FILE, {})
    out = []
    for d in (0, 1):
        rec = (data.get("routes") or {}).get(f"{route}|{d}")
        if not rec:
            continue
        table = {}
        for dt_key in ("wd", "we"):
            rows = []
            for h in range(24):
                g = rec["bins"].get(dt_key, {}).get(str(h), [])
                if g:
                    rows.append({"hour": h, "median": round(_median(g), 1), "n": len(g)})
            table[dt_key] = rows
        out.append({"direction": d, "ref_stop": rec["stop"], "table": table,
                    "now": headway_lookup(route, d, now)})
    return jsonify({"route": route, "directions": out,
                    "daytype_now": _headway_daytype(now), "hour_now": now.hour,
                    "min_samples": HEADWAY_MIN_SAMPLES})



# ══════════════════════════════════════════════════════════
# 用量儀表板：/admin/usage（頁面）＋ /api/usage（資料）
# 有設定 ADMIN_TOKEN 環境變數時，需要帶 ?token=ADMIN_TOKEN 才能看
# ══════════════════════════════════════════════════════════
USAGE_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TDX 用量儀表板</title>
<style>
:root{--bg:#04110d;--panel:#0a1f19;--line:#164036;--text:#eef1f8;--muted:#92a0c4;--ok:#2dd4bf;--warn:#f5a524;--bad:#ff6b6b}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Noto Sans TC',-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:16px;max-width:960px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px}.sub{color:var(--muted);font-size:12px;margin-bottom:14px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.card h2{font-size:13px;color:var(--muted);font-weight:500;margin-bottom:6px}
.big{font-size:30px;font-weight:700}.unit{font-size:13px;color:var(--muted);margin-left:4px}
.bar{height:14px;background:#0e1a16;border-radius:999px;overflow:hidden;position:relative;margin:8px 0 4px;border:1px solid var(--line)}
.bar>i{display:block;height:100%;background:var(--ok);transition:width .4s}
.bar>b{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--warn)}
.row{display:grid;grid-template-columns:110px 1fr 90px;gap:8px;align-items:center;padding:5px 0;font-size:13px}
.row .bar{margin:0;height:10px}.row span:last-child{text-align:right;color:var(--muted)}
.status{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700}
.s-ok{background:rgba(45,212,191,.16);color:var(--ok)}.s-warn{background:rgba(245,165,36,.16);color:var(--warn)}.s-bad{background:rgba(255,107,107,.16);color:var(--bad)}
.days{display:flex;align-items:flex-end;gap:3px;height:110px;margin-top:8px}
.days div{flex:1;background:var(--ok);border-radius:3px 3px 0 0;min-height:1px;position:relative;opacity:.85}
.days div.today{background:var(--warn);opacity:1}.days div.fut{background:#13302a}
.axis{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:3px}
.small{font-size:12px;color:var(--muted);line-height:1.6}
#login{display:none;max-width:360px;margin:60px auto}
input{width:100%;padding:10px;border-radius:8px;border:1px solid var(--line);background:#0e1a16;color:var(--text);margin:8px 0}
button{padding:10px 14px;border-radius:8px;border:0;background:var(--ok);color:#03211b;font-weight:700;cursor:pointer;width:100%}
@media(max-width:480px){.big{font-size:26px}.row{grid-template-columns:90px 1fr 70px}}
</style></head><body>
<div id="login" class="card"><h2>需要管理密碼</h2><input id="tok" type="password" placeholder="ADMIN_TOKEN"><button onclick="saveTok()">進入</button><div id="loginErr" class="small" style="color:var(--bad)"></div></div>
<div id="main" style="display:none">
<h1>📊 TDX 用量儀表板</h1><div class="sub" id="sub"></div>
<div class="grid">
 <div class="card"><h2>本月已用</h2><div><span class="big" id="used">-</span><span class="unit" id="cap"></span></div>
  <div class="bar"><i id="usedBar"></i><b id="paceMark" title="按日期比例該有的進度"></b></div>
  <div class="small" id="pace"></div></div>
 <div class="card"><h2>預估月底用量</h2><div><span class="big" id="proj">-</span><span class="unit">次</span></div>
  <div style="margin-top:8px"><span class="status" id="status">-</span></div><div class="small" id="projNote" style="margin-top:6px"></div></div>
 <div class="card"><h2>目前排程</h2><div class="small" id="sched"></div></div>
</div>
<div class="card" style="margin-bottom:12px"><h2>用量來源占比</h2><div id="sources"></div></div>
<div class="card"><h2>每日用量</h2><div class="days" id="days"></div><div class="axis"><span>1 日</span><span id="axMid"></span><span id="axEnd"></span></div></div>
<p class="small" style="margin-top:12px">橘色刻度＝依日期比例該有的進度；程式有兩道保險：即時資料用到 2780 次就停、離峰輪詢會在用量超前時自動跳過。每 30 秒自動更新。</p>
</div>
<script>
const LABELS={eta:'🚌 到站預估',ubike:'🚲 UBike 可借可還',positions:'📍 公車定位',static:'🗂️ 靜態資料（每月）',manual:'🛠️ 手動／測試',dynamic:'⚙️ 其他即時'};
const COLORS={eta:'#2dd4bf',ubike:'#60a5fa',positions:'#c084fc',static:'#f5a524',manual:'#ff6b6b',dynamic:'#92a0c4'};
let TOKEN=new URLSearchParams(location.search).get('token')||sessionStorage.getItem('adm')||'';
function saveTok(){TOKEN=document.getElementById('tok').value.trim();sessionStorage.setItem('adm',TOKEN);load();}
async function load(){
  try{
    const r=await fetch('/api/usage'+(TOKEN?'?token='+encodeURIComponent(TOKEN):''));
    if(r.status===401){document.getElementById('login').style.display='block';document.getElementById('main').style.display='none';document.getElementById('loginErr').textContent=TOKEN?'密碼不正確':'';return;}
    const d=await r.json();render(d);
  }catch(e){document.getElementById('sub').textContent='讀取失敗：'+e.message;}
}
function render(d){
  document.getElementById('login').style.display='none';document.getElementById('main').style.display='block';
  document.getElementById('sub').textContent=`台北時間 ${d.now}・${d.month} 月（第 ${d.day}/${d.days_in_month} 天）`;
  document.getElementById('used').textContent=d.used;document.getElementById('cap').textContent='/ '+d.cap+' 次';
  const pct=Math.min(100,d.used/d.cap*100);const bar=document.getElementById('usedBar');
  bar.style.width=pct+'%';bar.style.background=d.used>=d.cap*0.97?'#ff6b6b':(d.used>d.expected_by_now+150?'#f5a524':'#2dd4bf');
  document.getElementById('paceMark').style.left=Math.min(100,d.expected_by_now/d.cap*100)+'%';
  const diff=d.used-Math.round(d.expected_by_now);
  document.getElementById('pace').textContent=`依日期進度約該用 ${Math.round(d.expected_by_now)} 次（${diff>=0?'超前':'落後'} ${Math.abs(diff)} 次）・剩餘 ${Math.max(0,d.cap-d.used)} 次`;
  document.getElementById('proj').textContent=d.projected==null?'—':d.projected;
  const st=document.getElementById('status');
  const lvl=d.level;st.className='status '+(lvl==='ok'?'s-ok':lvl==='warn'?'s-warn':'s-bad');
  st.textContent=lvl==='ok'?'✅ 不會超標':lvl==='warn'?'⚠️ 接近上限':'⛔ 有超標風險';
  document.getElementById('projNote').textContent=d.projected==null?'月初資料還不夠，先不預估':`依目前速度推算，月底約 ${d.projected} 次（上限 ${d.cap}）`;
  const iv=d.poll_interval_minutes;
  document.getElementById('sched').innerHTML=[
    '服務時段：'+(d.service_hours?'營運中':'收班（不抓資料）'),
    '到站預估間隔：'+(iv?iv+' 分鐘':'—'),
    '今天是否上班日：'+(d.workday_today?'是':'否'),
    '有人使用中：'+(d.user_active?'是':'否（離峰輪詢暫停）'),
    '到站資料年齡：'+(d.eta_age_seconds==null?'—':Math.round(d.eta_age_seconds/60)+' 分鐘')
  ].join('<br>');
  const total=Object.values(d.by_source).reduce((a,b)=>a+b,0)||1;
  const rows=Object.entries(d.by_source).sort((a,b)=>b[1]-a[1]).map(([k,v])=>
    `<div class="row"><span>${LABELS[k]||k}</span><div class="bar"><i style="width:${v/total*100}%;background:${COLORS[k]||'#92a0c4'}"></i></div><span>${v}（${Math.round(v/total*100)}%）</span></div>`).join('');
  document.getElementById('sources').innerHTML=rows||'<div class="small">這個月還沒有資料</div>';
  const max=Math.max(1,...d.daily.map(x=>x.count||0));
  document.getElementById('days').innerHTML=d.daily.map(x=>
    `<div class="${x.day===d.day?'today':(x.day>d.day?'fut':'')}" style="height:${x.day>d.day?3:Math.max(2,(x.count||0)/max*100)}%" title="${x.day} 日：${x.count} 次"></div>`).join('');
  document.getElementById('axMid').textContent=Math.ceil(d.days_in_month/2)+' 日';document.getElementById('axEnd').textContent=d.days_in_month+' 日';
}
load();setInterval(load,30000);
</script></body></html>
"""


def _admin_ok():
    tok = os.environ.get("ADMIN_TOKEN", "")
    if not tok:
        return True
    given = request.args.get("token", "") or request.headers.get("X-Admin-Token", "")
    return given == tok


@app.route('/admin/usage')
def admin_usage_page():
    return app.response_class(USAGE_DASHBOARD_HTML, mimetype='text/html')


@app.route('/api/usage')
def api_usage():
    if not _admin_ok():
        return jsonify({"error": "需要管理密碼"}), 401
    now = _taipei_now()
    u = tdx_usage_snapshot()
    used = int(u.get("count", 0))
    dim = calendar.monthrange(now.year, now.month)[1]
    frac = ((now.day - 1) + (now.hour * 60 + now.minute) / 1440.0) / dim
    expected = TDX_MONTHLY_CAP * frac
    projected = int(round(used / frac)) if frac >= 0.03 else None
    if used >= TDX_MONTHLY_CAP * 0.97 or (projected is not None and projected > TDX_MONTHLY_CAP):
        level = "bad"
    elif projected is not None and projected > TDX_MONTHLY_CAP * 0.92:
        level = "warn"
    else:
        level = "ok"
    by_day = u.get("by_day", {}) or {}
    daily = [{"day": d, "count": int(by_day.get(f"{d:02d}", 0))} for d in range(1, dim + 1)]
    interval = get_smart_poll_interval(now)
    rt = get_realtime_status()
    return jsonify({
        "now": now.strftime("%Y-%m-%d %H:%M"), "month": u.get("month"), "day": now.day, "days_in_month": dim,
        "used": used, "cap": TDX_MONTHLY_CAP, "expected_by_now": round(expected, 1),
        "projected": projected, "level": level,
        "by_source": u.get("by_source", {}) or {}, "by_kind": u.get("by_kind", {}) or {},
        "daily": daily,
        "service_hours": is_service_hours(now),
        "poll_interval_minutes": None if interval is None else interval // 60,
        "workday_today": is_workday(now), "user_active": is_user_active(),
        "eta_age_seconds": rt.get("age_seconds"),
    })


# ══════════════════════════════════════════════════════════
# API：系統維護（重建站點快取）
# ══════════════════════════════════════════════════════════
@app.route('/api/debug_status')
def api_debug_status():
    """診斷用（唯讀）：看目前排程、快照、TDX 用量狀態。
    加上 ?test=1 會『真的』向 TDX 發 1 次到站預估請求，並原樣回報 HTTP 狀態碼與回應片段，
    用來判斷到底是 TDX 沒回資料、金鑰有問題、還是別的原因（會扣 1 次手動額度）。
    如果有設定 ADMIN_TOKEN 環境變數，test=1 需要帶 &token=你的ADMIN_TOKEN。"""
    now = _taipei_now()
    _ensure_realtime_cache_fresh()
    with _realtime_lock:
        eta_routes = len(_realtime_cache.get("eta_by_route") or {})
        eta_items = sum(len(v) for v in (_realtime_cache.get("eta_by_route") or {}).values())
        cache_meta = {k: _realtime_cache.get(k) for k in (
            "updated_at", "eta_fetched_at", "positions_fetched_at",
            "last_attempt", "last_attempt_ts", "last_attempt_ok")}
    usage = tdx_usage_snapshot()
    interval = get_smart_poll_interval(now)
    info = {
        "version": "tdx-budget-v1",
        "taipei_now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "service_hours": is_service_hours(now),
        "poll_interval_minutes": None if interval is None else interval // 60,
        "tdx_credentials_set": bool(app_id and app_key),
        "tdx_token_ok": bool(get_tdx_token()),
        "usage": {"count": usage.get("count"), "cap": TDX_MONTHLY_CAP, "by_kind": usage.get("by_kind")},
        "eta_snapshot": {"routes": eta_routes, "items": eta_items,
                         "age_seconds": None if not cache_meta["eta_fetched_at"]
                         else int(time.time() - cache_meta["eta_fetched_at"]),
                         **cache_meta},
        "memory_mb": None if _process_rss_mb() is None else round(_process_rss_mb(), 1),
        "memory_limit_mb": MEMORY_LIMIT_MB,
        "user_active": is_user_active(),
        "last_activity_seconds_ago": None if not _last_user_activity else int(time.time() - _last_user_activity),
        "workday_today": is_workday(now),
        "poll_thread_alive": bool(globals().get("realtime_thread") and realtime_thread.is_alive()),
        "static_meta": _read_json_cached(STATIC_META_FILE, {}),
        "saved_route_count": len(get_saved_route_names()),
    }
    if request.args.get("test") == "1":
        admin_token = os.environ.get("ADMIN_TOKEN", "")
        if admin_token and request.args.get("token", "") != admin_token:
            info["test"] = {"error": "需要帶 &token=ADMIN_TOKEN"}
            return jsonify(info)
        if not tdx_budget_consume("manual"):
            info["test"] = {"error": "額度或今日手動測試次數已用完"}
            return jsonify(info)
        try:
            res = requests.get(
                "https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/City/Tainan?%24format=JSON&%24top=3",
                headers=tdx_headers(), timeout=20)
            body = res.text[:400]
            try:
                parsed = res.json()
                count = len(parsed) if isinstance(parsed, list) else None
            except Exception:
                count = None
            info["test"] = {"http_status": res.status_code, "items_returned": count, "body_preview": body}
        except Exception as e:
            info["test"] = {"error": str(e)}
    return jsonify(info)


@app.route('/api/update_cache', methods=['POST'])
def api_update_cache():
    """手動重新抓取全部靜態資料（路線軌跡、站牌、時刻表、站牌清單、路線清單、UBike 站點）。
    平常不需要按——系統每月 1 號會自動抓好；這顆按鈕共 6 次 TDX 請求，24 小時內只能用一次。"""
    result = refresh_static_data(force=True)
    status = result.get("status")
    if status == "cooldown":
        return jsonify({"error": "24 小時內已經更新過了（為了節省 TDX 額度），請明天再試"}), 429
    if status == "busy":
        return jsonify({"error": "目前正在更新中，請稍後再看結果"}), 409
    if status not in ("success", "partial"):
        return jsonify({"error": f"更新失敗：{result.get('reason', '未知原因')}"}), 502
    return jsonify({"status": "success", "count": result.get("routes_saved", 0),
                    "partial": status == "partial", "missing": result.get("missing", [])})


# ══════════════════════════════════════════════════════════
# API：AI 助理（對話記錄多分頁）
# ══════════════════════════════════════════════════════════
@app.route('/api/chat/sessions', methods=['GET'])
def api_chat_sessions():
    state = get_state()
    if state["current_session_id"] is None or state["current_session_id"] not in state["chat_sessions"]:
        new_chat_session(state)
    sessions = [{"sid": sid, "title": s["title"], "is_current": sid == state["current_session_id"]}
                for sid, s in sorted(state["chat_sessions"].items(), reverse=True)]
    return jsonify({"sessions": sessions, "current_session_id": state["current_session_id"]})


@app.route('/api/chat/sessions/current')
def api_chat_current():
    state = get_state()
    if state["current_session_id"] is None or state["current_session_id"] not in state["chat_sessions"]:
        new_chat_session(state)
    sid = state["current_session_id"]
    sess = state["chat_sessions"][sid]
    return jsonify({"sid": sid, "title": sess["title"], "history": sess["history"]})


@app.route('/api/chat/sessions/new', methods=['POST'])
def api_chat_new():
    state = get_state()
    sid = new_chat_session(state)
    return jsonify({"sid": sid, "title": state["chat_sessions"][sid]["title"]})


@app.route('/api/chat/sessions/switch', methods=['POST'])
def api_chat_switch():
    sid = (request.json or {}).get('sid')
    state = get_state()
    if sid in state["chat_sessions"]:
        state["current_session_id"] = sid
        return jsonify({"status": "ok"})
    return jsonify({"error": "找不到該對話"}), 404


@app.route('/api/chat/sessions/delete', methods=['POST'])
def api_chat_delete():
    sid = (request.json or {}).get('sid')
    state = get_state()
    if sid in state["chat_sessions"]:
        del state["chat_sessions"][sid]
        if state["current_session_id"] == sid:
            state["current_session_id"] = None
    return jsonify({"status": "ok"})


@app.route('/api/chat', methods=['POST'])
def api_chat():
    if not client:
        return jsonify({"reply": "AI 模組未啟用，請檢查 GROQ_API_KEY。"})

    data = request.json or {}
    user_q = data.get('query', '')
    if not user_q:
        return jsonify({"error": "缺少訊息內容"}), 400

    state = get_state()
    if state["current_session_id"] is None or state["current_session_id"] not in state["chat_sessions"]:
        new_chat_session(state)
    sid = state["current_session_id"]
    sess = state["chat_sessions"][sid]

    system_msg = (
        "你是台南公車查詢網站內建的 AI 助理，個性專業、友善、有耐心，擁有完整的對話記憶。\n"
        "請務必遵守以下規則，確保回答精準、不誤導使用者：\n"
        "1. 只有在下面【公車狀態】欄位裡實際列出的路線、站名、動態，才可以當作事實引用；"
        "沒有列出的路線／站牌／到站時間，一律不要憑印象猜測或編造具體數字，"
        "要老實說『目前沒有這條路線/這一站的即時資料』，並建議使用者到查詢頁面或地圖頁面直接查詢。\n"
        "2. 【公車狀態】是使用者『最近一次』查詢當下的快照，附有擷取時間；"
        "如果現在時間跟擷取時間差距較大，或使用者問的是『現在』『剛剛』這種即時性問題，"
        "要提醒對方這筆資料可能已經過時，建議重新整理查詢頁面取得最新結果，不要講得像絕對即時。\n"
        "3. 回答公車動態時盡量具體（幾分鐘、有沒有無障礙車、車牌等），但只用資料裡真的有的內容，"
        "不足的部分就明說不確定，不要為了讓回答看起來完整而補上沒有根據的細節。\n"
        "4. 用流暢自然的繁體中文回答，符合台南在地用語習慣，回答盡量精簡扼要，不要有無意義的贅字。"
        f"\n\n【目前天氣】{state['current_weather']}"
        f"\n【公車狀態】{state['bus_status']}"
    )
    msgs = [{"role": "system", "content": system_msg}]
    for h in sess["history"][-20:]:
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": user_q})

    # Groq 三不五時會下架／改名模型（例如 2026/06 就把 llama-3.3-70b-versatile 整個下架），
    # 一旦主要模型剛好被下架，直接整個 AI 功能掛掉太可惜，這裡準備幾個備援模型依序嘗試。
    # temperature 刻意調低（0.2）：這是公車動態查詢助理，使用者要的是準確、可核對的資訊，
    # 不是有創意的自由發揮，溫度太高容易在時間、站名這類具體事實上「講得煞有其事但其實編的」。
    candidate_models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"]
    resp = None
    last_err = None
    for m in candidate_models:
        try:
            resp = client.chat.completions.create(
                messages=msgs, model=m, max_tokens=1024, temperature=0.2, top_p=0.9)
            break
        except Exception as e:
            last_err = e
            continue

    try:
        if resp is None:
            raise last_err or RuntimeError("AI 模型目前都無法使用")
        ai_text = resp.choices[0].message.content

        if len(sess["history"]) == 0:
            sess["title"] = user_q[:20] + ("..." if len(user_q) > 20 else "")

        sess["history"].append({"role": "user", "content": user_q})
        sess["history"].append({"role": "assistant", "content": ai_text})

        return jsonify({"reply": ai_text, "title": sess["title"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════
# 記憶體看守：Render 記憶體快滿時，自動清掉可以重建的暫存
# ══════════════════════════════════════════════════════════
# 每 30 秒量一次這個處理程序實際佔用的記憶體，超過上限的 80% 就清掉「可以重新讀檔重建」的暫存
# （路線軌跡／站牌的記憶體快取、JSON 檔案快取、站名索引、UBike 快照），再強制回收記憶體。
# 只清暫存，不會刪除任何存檔、快照檔、帳號或使用者的最愛／對話資料。
# Render 免費方案記憶體約 512MB；要換成別的大小，在 Render 的 Environment 設定 MEMORY_LIMIT_MB。
MEMORY_LIMIT_MB = float(os.environ.get("MEMORY_LIMIT_MB", "512"))
MEMORY_CLEAN_RATIO = 0.80
MEMORY_CHECK_SECONDS = 30


def _process_rss_mb():
    """目前這個處理程序實際使用的記憶體（MB）。讀不到回傳 None。"""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return None


def _free_memory_caches():
    """清掉可重建的暫存（保留 TDX token 快取，避免白白重新登入）。"""
    for key in list(_cache_store.keys()):
        if key != _TOKEN_CACHE_KEY:
            _cache_store.pop(key, None)
    _json_file_cache.clear()
    _stop_route_index_cache["data"] = None
    _stop_route_index_cache["time"] = 0
    _ubike_memo["key"] = None
    _ubike_memo["stations"] = []
    _ubike_memo["avail"] = {}
    gc.collect()


def _memory_guard_loop():
    while True:
        try:
            rss = _process_rss_mb()
            if rss is not None and rss > MEMORY_LIMIT_MB * MEMORY_CLEAN_RATIO:
                _free_memory_caches()
                after = _process_rss_mb()
                print(f"🧹 記憶體 {rss:.0f}MB 超過上限 {MEMORY_LIMIT_MB * MEMORY_CLEAN_RATIO:.0f}MB，"
                      f"已自動清除暫存，清除後約 {after:.0f}MB" if after is not None else
                      f"🧹 記憶體 {rss:.0f}MB 偏高，已自動清除暫存", flush=True)
        except Exception as e:
            print(f"⚠️ 記憶體看守發生例外：{e}", flush=True)
        time.sleep(MEMORY_CHECK_SECONDS)


# 背景排程放在檔案最後才啟動：確保上面所有函式、變數都已經定義好，不會有啟動瞬間的競態問題。
realtime_thread = threading.Thread(target=_realtime_poll_loop, daemon=True)
realtime_thread.start()
threading.Thread(target=_memory_guard_loop, daemon=True).start()


if __name__ == '__main__':
    # 只有明確設定 FLASK_DEBUG=1 才會開 debug 模式（會洩漏詳細錯誤堆疊、也會拖慢速度），
    # 避免哪天不小心直接用 `python app.py` 跑在正式環境時，忘記帶入 debug=True。
    # 正式環境還是強烈建議用 gunicorn（見 start.sh）啟動，而不是靠這個內建開發伺服器；
    # 這裡開 threaded=True 純粹是讓「不小心」用這個內建伺服器時，至少還能同時處理
    # 一個以上的請求，不會退化成完全一個一個排隊。
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode, threaded=True, port=int(os.environ.get("PORT", 5000)))
