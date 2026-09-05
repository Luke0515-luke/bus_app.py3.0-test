import os
import json
import math
import time
import uuid
import concurrent.futures
from datetime import datetime, timedelta

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


def _fetch_and_save_stop_data(route_name):
    """即時向 TDX 查詢某路線的 StopOfRoute 原始資料，驗證路線名稱後才存檔。"""
    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/StopOfRoute/City/Tainan/{route_name}?%24format=JSON"
    res = tdx_get(url, timeout=10, retries=1)
    if res is None:
        return None
    try:
        data = res.json()
    except Exception:
        return None
    data = _filter_route_entries(data, route_name)
    if data:
        _save_route_json(_route_stop_file_path(route_name), data)
    return data


def _fetch_and_save_shape_data(route_name):
    """即時向 TDX 查詢某路線的 Shape 原始資料，驗證路線名稱後才存檔。"""
    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/Shape/City/Tainan/{route_name}?%24format=JSON"
    res = tdx_get(url, timeout=10, retries=1)
    if res is None:
        return None
    try:
        data = res.json()
    except Exception:
        return None
    data = _filter_route_entries(data, route_name)
    if data:
        _save_route_json(_route_shape_file_path(route_name), data)
    return data


def _fetch_and_save_timetable_data(route_name):
    """即時向 TDX 查詢某路線的固定時刻表（Bus/Schedule）原始資料，驗證路線名稱後才存檔。
    跟站牌／軌跡走同一套「查一次、之後都吃檔案」的邏輯，避免每次打開時刻表都要
    重新等 TDX 回應（TDX 這支端點常常比較慢，手機在訊號不穩時容易直接 fetch 失敗）。"""
    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/Schedule/City/Tainan/{route_name}?%24format=JSON"
    res = tdx_get(url, timeout=15, retries=1)
    if res is None:
        return None
    try:
        data = res.json()
    except Exception:
        return None
    data = _filter_route_entries(data, route_name)
    if data:
        _save_route_json(_route_timetable_file_path(route_name), data)
    return data


def load_route_stop_data(route_name):
    """優先讀取已存檔的 StopOfRoute 資料（會被自動備份到 GitHub）；
    檔案不存在、損毀，或內容其實是其他路線的資料（舊版沒有驗證時可能存錯），
    都會視同快取失效，重新向 TDX 查一次並用驗證過的新資料覆蓋舊檔。"""
    path = _route_stop_file_path(route_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid = _filter_route_entries(data, route_name)
        if valid:
            return valid
        if data:
            print(f"⚠️「{route_name}」的存檔資料與路線名稱不符（疑似存到其他路線），將重新查詢並覆蓋。")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return _fetch_and_save_stop_data(route_name) or []


def load_route_shape_data(route_name):
    """優先讀取已存檔的 Shape 資料（會被自動備份到 GitHub）；
    檔案不存在、損毀，或內容其實是其他路線的資料，都會重新向 TDX 查一次並覆蓋舊檔。"""
    path = _route_shape_file_path(route_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid = _filter_route_entries(data, route_name)
        if valid:
            return valid
        if data:
            print(f"⚠️「{route_name}」的存檔資料與路線名稱不符（疑似存到其他路線），將重新查詢並覆蓋。")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return _fetch_and_save_shape_data(route_name) or []


def load_route_timetable_data(route_name):
    """優先讀取已存檔的固定時刻表資料（會被自動備份到 GitHub）；
    檔案不存在、損毀，或內容其實是其他路線的資料，都會重新向 TDX 查一次並覆蓋舊檔。"""
    path = _route_timetable_file_path(route_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid = _filter_route_entries(data, route_name)
        if valid:
            return valid
        if data:
            print(f"⚠️「{route_name}」的時刻表存檔與路線名稱不符（疑似存到其他路線），將重新查詢並覆蓋。")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return _fetch_and_save_timetable_data(route_name) or []


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


def tdx_headers():
    return {'authorization': f'Bearer {get_tdx_token()}', 'Accept-Encoding': 'gzip'}


def tdx_get(url, timeout=8, retries=1):
    """帶重試的 TDX GET，減少大量平行請求時偶發逾時/限流造成的空白資料。"""
    for attempt in range(retries + 1):
        try:
            res = requests.get(url, headers=tdx_headers(), timeout=timeout)
            if res.status_code == 200:
                return res
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


def _fetch_bus_data_from_tdx(route_name):
    """實際向 TDX 查詢單一路線的到站預估時間（EstimatedTimeOfArrival）。
    只給下面的後台排程呼叫，一般 API 請求改讀 fetch_bus_data() 的共用快照。
    改用 tdx_get()（內建重試一次），單一路線偶發逾時／限流時不會直接放棄，
    這樣『很多站的時間都是從時刻表推的』這個狀況會少很多——那通常就是因為
    這支查詢那一輪剛好失敗，導致這條路線完全沒有即時資料可用，才會整條路線
    都退回時刻表估計。"""
    url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/City/Tainan/{route_name}?%24format=JSON"
    res = tdx_get(url, timeout=8, retries=1)
    if res is not None:
        try:
            return res.json()
        except Exception:
            pass
    return None


def fetch_bus_data_all():
    """一次性向 TDX 拿『全台南所有路線』的到站預估時間，只給後台排程呼叫，
    取代原本每條路線各自查一次 TDX 的做法（跟 fetch_bus_realtime_positions()
    不帶路線名稱時拿『全部公車定位』是同一種寫法）。"""
    url = "https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/City/Tainan?%24format=JSON"
    res = tdx_get(url, timeout=25, retries=0)
    if res is not None:
        try:
            return res.json()
        except Exception:
            pass
    return None


def _fetch_eta_by_route_parallel(routes):
    """備援方案：如果『不帶路線名稱、一次查全部路線』的 fetch_bus_data_all()
    沒有拿到任何資料（有可能 TDX 這個 API 其實不支援這種一次查全部路線的寫法），
    改成逐條路線平行查詢當備援——用執行緒池同時查，不是一條一條依序等，
    確保到站資訊還是查得到，不會因為城市級 API 不支援就整個開天窗。
    max_workers 開比較大（40）：每一條路線的查詢都帶了重試（見
    _fetch_bus_data_from_tdx），單一請求最壞情況可能要等 15 秒以上，
    如果平行數開太小，路線一多（台南有 80~90 條左右），加總下來很容易
    超過後台排程 60 秒一輪的預算，反而讓『這一輪』整個拖過頭。
    回傳 {路線名稱: [到站資訊項目, ...]}，key 直接用查詢時的路線名稱，
    不依賴回傳資料裡有沒有正確的 RouteName 欄位。"""
    result = {}
    failed_routes = []
    if not routes:
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
        future_to_route = {ex.submit(_fetch_bus_data_from_tdx, r): r for r in routes}
        for future in concurrent.futures.as_completed(future_to_route):
            r = future_to_route[future]
            try:
                data = future.result()
            except Exception:
                data = None
            # 特別注意 data 是 None（這次真的查詢失敗）跟 data 是 []（查詢成功，
            # 只是這條路線目前剛好沒有任何到站預估，例如末班車已過）的差別：
            # 只有前者才算「失敗」，需要在外層保留上一輪的舊資料；
            # 後者是真的、最新的狀態，就應該讓它覆蓋過去，不能被當成失敗而略過。
            if data is not None:
                result[r] = data
            else:
                failed_routes.append(r)
    if failed_routes:
        print(f"⚠️ 這一輪到站預估查詢失敗的路線（{len(failed_routes)} 條，"
              f"這幾條這一輪會沿用上一輪的資料，查不到才會退回時刻表估計）："
              f"{'、'.join(failed_routes[:15])}{'…' if len(failed_routes) > 15 else ''}", flush=True)
    return result


def fetch_bus_data(route_name):
    """回傳某路線的到站預估時間。改成讀『後台排程每分鐘統一抓好』的共用快照，
    不再由每一個使用者的請求各自打一次 TDX——這樣所有使用者看到的都是同一份、
    由後台統一更新的資料，也大幅減少對 TDX 的查詢量。
    只有在系統剛啟動、快照完全還是空的（例如第一次部署、還沒抓過任何資料）時，
    才退回直接查一次 TDX，避免使用者看到完全空白的畫面。"""
    _ensure_realtime_cache_fresh()
    with _realtime_lock:
        by_route = _realtime_cache.get("eta_by_route") or {}
        data = by_route.get(route_name)
        has_data = bool(by_route)
    if data is not None:
        return data
    if has_data:
        # 快照有資料，但這條路線剛好沒有任何一筆（可能真的沒有車在跑），
        # 回傳空清單維持跟舊版一樣的語意，而不是回傳 None（那會被上層當成查詢失敗）。
        return []
    return _fetch_bus_data_from_tdx(route_name)


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


@cached(60)
def fetch_ubike_all():
    stations, avail_map = [], {}
    try:
        r1 = requests.get("https://tdx.transportdata.tw/api/basic/v2/Bike/Station/City/Tainan?%24format=JSON",
                           headers=tdx_headers(), timeout=8)
        r2 = requests.get("https://tdx.transportdata.tw/api/basic/v2/Bike/Availability/City/Tainan?%24format=JSON",
                           headers=tdx_headers(), timeout=8)
        if r1.status_code == 200:
            stations = r1.json()
        if r2.status_code == 200:
            for av in r2.json():
                avail_map[av["StationUID"]] = av
    except Exception:
        pass
    return stations, avail_map


@cached(300)
def fetch_all_bus_stops():
    url = "https://tdx.transportdata.tw/api/basic/v2/Bus/Stop/City/Tainan?%24format=JSON"
    try:
        res = requests.get(url, headers=tdx_headers(), timeout=20)
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return []


@cached(3600)
def fetch_all_route_meta():
    """取得 TDX 上台南市『所有』公車路線的正式登記資料（含正確的 RouteName）。
    用來在使用者輸入的路線名稱查不到資料時，反查 TDX 真正登記的名稱是什麼，
    而不是憑猜測去改設定檔。"""
    url = "https://tdx.transportdata.tw/api/basic/v2/Bus/Route/City/Tainan?%24format=JSON"
    try:
        res = requests.get(url, headers=tdx_headers(), timeout=20)
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return []


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


def _fetch_bus_realtime_positions_from_tdx(route_name=None):
    """實際向 TDX 查詢即時公車定位（RealTimeByFrequency）。
    只給下面的後台排程呼叫，一般 API 請求改讀 fetch_bus_realtime_positions() 的共用快照。"""
    if route_name:
        url = f"https://tdx.transportdata.tw/api/basic/v2/Bus/RealTimeByFrequency/City/Tainan/{route_name}?%24format=JSON"
    else:
        url = "https://tdx.transportdata.tw/api/basic/v2/Bus/RealTimeByFrequency/City/Tainan?%24format=JSON"
    res = tdx_get(url, timeout=25, retries=1)
    if res is not None:
        try:
            return res.json()
        except Exception:
            pass
    return []


def fetch_bus_realtime_positions(route_name=None):
    """回傳即時公車 GPS 定位（不帶路線名稱＝全台南所有路線）。改成讀『後台排程
    每分鐘統一抓好』的共用快照，不再由每一個使用者的請求各自打一次 TDX。
    只有在系統剛啟動、快照完全還是空的時候，才退回直接查一次 TDX 當暫時的資料來源。"""
    _ensure_realtime_cache_fresh()
    with _realtime_lock:
        by_route = _realtime_cache.get("positions_by_route") or {}
        has_data = bool(by_route)
        if route_name:
            result = list(by_route.get(route_name, []))
        else:
            result = [b for lst in by_route.values() for b in lst]
    if has_data:
        return result
    return _fetch_bus_realtime_positions_from_tdx(route_name)


# ── 即時公車資料（定位／到站預估）：後台每分鐘統一抓一次的共用快照 ─────────
# 目的：所有使用者都讀同一份、由後台排程統一更新的資料，而不是每個人的每一次
# 請求都各自向 TDX 發送請求；同時把這份快照存檔＋推到 GitHub 的獨立分支，
# 即使 Render 重啟、換機器，也能立刻拿回上一次成功抓到的資料。
REALTIME_POSITIONS_FILE = os.path.join(REALTIME_DATA_DIR, "positions.json")
REALTIME_ETA_FILE = os.path.join(REALTIME_DATA_DIR, "eta.json")
REALTIME_META_FILE = os.path.join(REALTIME_DATA_DIR, "meta.json")
REALTIME_LOCK_FILE = os.path.join(REALTIME_DATA_DIR, ".poll.lock")
REALTIME_POLL_SECONDS = 60      # 每 1 分鐘抓一次
REALTIME_STALE_SECONDS = 90     # 超過這個秒數還沒成功更新過，視為「尚未更新資料」

_realtime_lock = threading.Lock()
_realtime_cache = {
    "positions_by_route": {},
    "eta_by_route": {},
    "updated_at": None,       # 最近一次「成功」更新的時間
    "last_attempt": None,     # 最近一次「嘗試」更新的時間（不管成功與否）
    "last_attempt_ok": None,
}
_realtime_file_mtime = None  # 記憶體裡這份快照，是對應到「檔案」的哪個修改時間


def get_realtime_status():
    """回傳目前這份共用快照的新鮮度，給 API 附加在回應裡，讓前端可以在資料
    太舊（代表後台排程可能連續失敗）時顯示「尚未更新資料」，而不是讓使用者
    誤以為看到的一定是最新狀態。"""
    with _realtime_lock:
        updated_at = _realtime_cache.get("updated_at")
    if not updated_at:
        return {"updated_at": None, "is_fresh": False, "age_seconds": None}
    try:
        updated_dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return {"updated_at": updated_at, "is_fresh": False, "age_seconds": None}
    age = (datetime.now() - updated_dt).total_seconds()
    return {"updated_at": updated_at, "is_fresh": age <= REALTIME_STALE_SECONDS, "age_seconds": int(age)}


def _load_realtime_cache_from_disk():
    """伺服器剛啟動時，把上一次存檔（可能是這台機器自己存的，也可能是剛剛從
    GitHub realtime-data 分支拉回來的）讀進記憶體，讓系統一開機就有資料可用，
    不用整整等到第一次排程（最多 1 分鐘）跑完才有東西可以顯示。"""
    positions_by_route, eta_by_route, meta = {}, {}, {}
    try:
        with open(REALTIME_POSITIONS_FILE, "r", encoding="utf-8") as f:
            positions_by_route = json.load(f)
    except Exception:
        pass
    try:
        with open(REALTIME_ETA_FILE, "r", encoding="utf-8") as f:
            eta_by_route = json.load(f)
    except Exception:
        pass
    try:
        with open(REALTIME_META_FILE, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        pass
    with _realtime_lock:
        _realtime_cache["positions_by_route"] = positions_by_route
        _realtime_cache["eta_by_route"] = eta_by_route
        _realtime_cache["updated_at"] = meta.get("updated_at")
        _realtime_cache["last_attempt"] = meta.get("last_attempt")
        _realtime_cache["last_attempt_ok"] = meta.get("last_attempt_ok")


def _ensure_realtime_cache_fresh():
    """真正讓『使用者查詢一律從檔案抓資料』成立的關鍵：每次查詢前，先看一下
    快照檔案（meta.json）的修改時間有沒有變新——不管是這個處理程序自己的
    後台排程剛更新的，還是（部署成多個 worker process 時）另一個 worker
    process 的排程更新的，只要檔案變新了，就重新讀一次檔案進記憶體。
    平常檔案沒變的話，這裡只做一次很輕量的 os.stat()，不會整個重新讀寫，
    所以正常查詢速度完全不受影響；但只要檔案有變，查詢一定拿到『檔案裡
    最新那份』，不會有某個 worker 記憶體裡卡著舊資料的問題。"""
    global _realtime_file_mtime
    try:
        mtime = os.path.getmtime(REALTIME_META_FILE)
    except OSError:
        return
    if _realtime_file_mtime is None or mtime > _realtime_file_mtime:
        _load_realtime_cache_from_disk()
        _realtime_file_mtime = mtime


def _acquire_realtime_poll_lock():
    """如果之後改成多個 worker process 部署，避免每個 worker 都各自每分鐘
    去打一次 TDX、各自 git push（互相打架、也失去『統一由後台抓一次』的意義）。
    用 flock 非阻塞鎖：同一時間只有搶到鎖的那個 worker 真的去查 TDX、寫檔、
    推送到 GitHub，其他 worker 這一輪直接跳過——反正上面的
    _ensure_realtime_cache_fresh() 會讓它們之後查詢時，照樣讀得到別人剛剛
    寫進（同一顆磁碟上）檔案裡的最新資料。單一 worker 部署時，這裡幾乎不會
    有任何影響，每次都馬上搶得到鎖。"""
    try:
        os.makedirs(REALTIME_DATA_DIR, exist_ok=True)
        fp = open(REALTIME_LOCK_FILE, "w")
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fp
    except BlockingIOError:
        fp.close()
        return None
    except Exception:
        return None


def _write_realtime_snapshot(positions_by_route, eta_by_route, ok, attempted_at):
    """把這一輪抓到的定位／到站資料寫檔：每次都先刪掉舊檔，再整批寫入新的，
    確保資料夾裡永遠只留『最新這一份』快照，而不是累加、保留歷史檔案。"""
    global _realtime_file_mtime
    try:
        os.makedirs(REALTIME_DATA_DIR, exist_ok=True)
        for path in (REALTIME_POSITIONS_FILE, REALTIME_ETA_FILE, REALTIME_META_FILE):
            if os.path.exists(path):
                os.remove(path)
        with open(REALTIME_POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions_by_route, f, ensure_ascii=False)
        with open(REALTIME_ETA_FILE, "w", encoding="utf-8") as f:
            json.dump(eta_by_route, f, ensure_ascii=False)
        with open(REALTIME_META_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "updated_at": _realtime_cache.get("updated_at"),
                "last_attempt": attempted_at,
                "last_attempt_ok": ok,
            }, f, ensure_ascii=False)
        # 這個 process 剛剛自己寫過檔案了，記憶體裡就是最新的，記下這個檔案時間，
        # 避免下一次查詢時 _ensure_realtime_cache_fresh() 又白白重讀一次同樣的內容。
        try:
            _realtime_file_mtime = os.path.getmtime(REALTIME_META_FILE)
        except OSError:
            pass
    except Exception as e:
        print(f"⚠️ 寫入即時資料快照失敗：{e}", flush=True)


def _realtime_poll_once():
    """後台排程主體：每分鐘執行一次，統一向 TDX 抓『全台南公車即時定位』與
    『全台南到站預估時間』各一次（各只發一次請求，不是每條路線各發一次），
    取代原本每個使用者查詢時都各自打一次 TDX 的做法。
    抓到之後：① 依路線名稱分組、更新記憶體共用快取 ② 存檔（先清舊檔再整批換新）
    ③ 推送到 bus_app.py3.0backup 這個 repo 的 realtime-data 分支。
    只要這一輪完全沒抓到任何資料，就不更新 updated_at，讓 get_realtime_status()
    能正確判斷『已經有一段時間沒有成功更新』，回傳給前端顯示「尚未更新資料」。
    最前面先搶跨處理程序的鎖：部署成多個 worker process 時，只有搶到鎖的那個
    worker 真的執行這一輪，其他 worker 直接跳過，避免大家都各自打一次 TDX。"""
    lock_fp = _acquire_realtime_poll_lock()
    if lock_fp is None:
        return  # 已經有別的 worker 正在跑這一輪，這裡不用重複做
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        positions_all = _fetch_bus_realtime_positions_from_tdx(None)

        positions_by_route = {}
        for b in (positions_all or []):
            r = (b.get("RouteName") or {}).get("Zh_tw", "")
            if r:
                positions_by_route.setdefault(r, []).append(b)

        eta_all = fetch_bus_data_all()
        eta_source = "city-wide"
        if eta_all:
            eta_by_route = {}
            for item in eta_all:
                r = (item.get("RouteName") or {}).get("Zh_tw", "")
                if r:
                    eta_by_route.setdefault(r, []).append(item)
        else:
            # 城市級「一次查全部路線」沒有拿到資料，改成逐條路線平行查詢當備援，
            # 確保到站資訊還是查得到，不會讓 fetch_bus_data() 一直讀到空快取，
            # 進而每個使用者的請求又被迫退回去直接查一次 TDX（那才是真正會讓
            # 頁面『跑很久』的原因）。
            eta_source = "per-route fallback"
            eta_by_route = _fetch_eta_by_route_parallel(get_all_known_routes())

        ok = bool(positions_all) or bool(eta_by_route)

        with _realtime_lock:
            if positions_all:
                _realtime_cache["positions_by_route"] = positions_by_route
            if eta_by_route:
                # 用「合併」而不是整批覆蓋：這一輪如果只有部分路線查詢失敗
                # （例如逐條路線平行查詢時，剛好某幾條逾時），失敗的路線保留
                # 上一輪的舊資料，不要因為這一輪剛好沒查到，就讓它整個從快取
                # 消失、被迫顯示成「尚未發車」甚至退回時刻表估計。
                merged_eta = dict(_realtime_cache.get("eta_by_route") or {})
                merged_eta.update(eta_by_route)
                _realtime_cache["eta_by_route"] = merged_eta
            if ok:
                _realtime_cache["updated_at"] = now_str
            _realtime_cache["last_attempt"] = now_str
            _realtime_cache["last_attempt_ok"] = ok
            snapshot_positions = dict(_realtime_cache["positions_by_route"])
            snapshot_eta = dict(_realtime_cache["eta_by_route"])

        _write_realtime_snapshot(snapshot_positions, snapshot_eta, ok, now_str)

        try:
            push_realtime_backup(REALTIME_DATA_DIR)
        except Exception as e:
            print(f"❌ 即時資料推送到 GitHub 失敗：{e}", flush=True)

        print(f"{'✅' if ok else '⚠️'} 即時公車資料排程：定位 {len(positions_all or [])} 筆、"
              f"到站預估 {sum(len(v) for v in eta_by_route.values())} 筆（來源：{eta_source}），"
              f"時間 {now_str}", flush=True)
    finally:
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fp.close()


def _realtime_poll_loop():
    """每 REALTIME_POLL_SECONDS（1 分鐘）跑一次 _realtime_poll_once()，
    跟現有備份路線資料用的 backup_loop／scheduler_thread 是各自獨立的排程執行緒，
    互不影響。"""
    while True:
        try:
            _realtime_poll_once()
        except Exception as e:
            print(f"❌ 即時資料排程發生例外：{e}", flush=True)
        time.sleep(REALTIME_POLL_SECONDS)


_load_realtime_cache_from_disk()
realtime_thread = threading.Thread(target=_realtime_poll_loop, daemon=True)
realtime_thread.start()


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
    weather_info = fetch_weather()
    state["current_weather"] = weather_info

    bus_list = fetch_bus_data(route)
    if bus_list is None:
        return jsonify({"error": "無法取得即時動態"}), 502

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
    realtime_map = {item.get("StopName", {}).get("Zh_tw", ""): item for item in active_list}
    all_stops_raw = fetch_route_stops_by_direction(route, target_dir)
    full_stop_list = all_stops_raw or [item.get("StopName", {}).get("Zh_tw", "") for item in active_list]

    if not full_stop_list:
        return jsonify({"dest0": dest_0, "dest1": dest_1, "stops": [], "empty": True})

    # TDX 的「逐站預估到站時間」（EstimatedTimeOfArrival）資料常常有缺口，
    # 尤其班次少、或車輛剛好在兩站中間時，容易讓明明有車在跑的站被判定成「尚未發車」。
    # 這裡另外抓「即時公車 GPS 定位」（RealTimeByFrequency，跟地圖用的是同一支端點），
    # 用最近站的方式回頭比對，確保只要 TDX 查得到這班車，時間軸上就不會漏掉它。
    gps_buses = fetch_bus_realtime_positions(route)
    gps_dir_buses = [b for b in gps_buses if b.get("Direction", 0) == target_dir]
    active_bus_count = len(gps_dir_buses)
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
        bus_wait = start_item.get("EstimateTime") or 0
        if start_st in full_stop_list and end_st in full_stop_list:
            idx_s = full_stop_list.index(start_st)
            idx_e = full_stop_list.index(end_st)
            bus_travel = abs(idx_e - idx_s) * 120
        else:
            bus_travel = 600
        ubike_suggestion = check_ubike_suggestion(
            start_st, end_st, stop_coord_map, ub_stations, ub_avail, bus_wait, bus_travel)

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

        # ETA 端點沒有這站的資料，但 GPS 定位確認附近真的有車在跑 → 用 GPS 資料補正，
        # 不要讓使用者看到「尚未發車」卻其實漏掉了一班查得到的車
        gps_here = gps_near_stop.get(s_name)
        if eta is None and status in (0, 1) and gps_here:
            gps_bus = gps_here[0]
            if not plate:
                plate = gps_bus.get("PlateNumb", "")
            time_text, badge_class = "進站中", "ts-red"

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
            is_calculated_estimate = True
        elif status == 1:
            est_from_schedule = estimate_eta_from_schedule(schedule_dep_times, idx)
            if est_from_schedule is not None:
                mins = int(est_from_schedule // 60)
                time_text = "即將進站（時刻表估計）" if mins <= 0 else f"約 {mins} 分鐘（時刻表估計）"
                badge_class = "ts-blue"

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
    return jsonify({
        "dest0": dest_0, "dest1": dest_1,
        "weather": weather_info,
        "stops": stops_out,
        "ubike_suggestion": ubike_suggestion,
        "tts_text": "".join(tts_lines),
        "active_bus_count": active_bus_count,
        "empty": False,
        "data_fresh": realtime_status["is_fresh"],
        "data_updated_at": realtime_status["updated_at"],
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
    directs = find_direct_routes(stop_index, start, end)
    transfers = find_transfer_routes(stop_index, start, end)
    return jsonify({"directs": directs, "transfers": transfers})


# ══════════════════════════════════════════════════════════
# API：地圖頁面
# ══════════════════════════════════════════════════════════
@app.route('/api/map_data')
def api_map_data():
    r_filter = request.args.get('routes', '')
    filter_list = [r.strip() for r in r_filter.replace("，", ",").split(',') if r.strip()] if r_filter else []
    # 若使用者輸入「全部」（或包含在清單中），視同未篩選，顯示全部路線的公車
    if any(x in ("全部", "全部路線") for x in filter_list):
        filter_list = []

    if filter_list:
        all_buses = []
        for r in filter_list:
            all_buses.extend(fetch_bus_realtime_positions(r))
    else:
        # 未篩選：一次性拉取全台南所有路線的即時公車位置
        all_buses = fetch_bus_realtime_positions()

    bus_features = []
    live_route_set = set()
    for bus in all_buses:
        pos = bus.get("BusPosition", {})
        lat, lon = pos.get("PositionLat"), pos.get("PositionLon")
        route = bus.get("RouteName", {}).get("Zh_tw", "")
        if not lat or not lon or not route:
            continue
        live_route_set.add(route)
        # 有些車是繞道／支線行駛，實際終點跟路線平常公告的不一樣，這裡一併帶出來，
        # 前端才能在地圖上特別標示「這班車是開去哪裡」。
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

    # 無論該路線目前有沒有營運中的公車，都要能顯示其路線軌跡與站牌，
    # 因此路線清單一律使用「使用者指定的篩選清單」或「系統設定的全部路線＋已存檔路線」，
    # 而不是只看目前有跑的公車有哪些路線。這樣即使某條路線不在預設分類表裡，
    # 只要之前用「抓取並儲存路線原始資料」存過，也會出現在地圖與路線清單中。
    routes_to_draw = filter_list if filter_list else get_all_known_routes()

    shape_map, stop_map = fetch_shapes_and_stops_parallel(routes_to_draw)

    shape_features = []
    stop_features = []
    for r in routes_to_draw:
        color = get_route_color(r)
        for sh in shape_map.get(r, []):
            pts = parse_wkt_linestring(sh.get("Geometry", ""))
            if pts:
                shape_features.append({"route": r, "color": color, "points": pts})
        for sp in stop_map.get(r, []):
            stop_features.append({
                "route": r, "name": sp["name"],
                "lat": sp["lat"], "lon": sp["lon"], "color": color
            })

    realtime_status = get_realtime_status()
    return jsonify({
        "buses": bus_features,
        "shapes": shape_features,
        "stops": stop_features,
        "routes": routes_to_draw,
        "saved_routes": sorted(get_saved_route_names()),
        "live_routes": sorted(live_route_set),
        "now": datetime.now().strftime("%H:%M:%S"),
        "data_fresh": realtime_status["is_fresh"],
        "data_updated_at": realtime_status["updated_at"],
        "data_age_seconds": realtime_status["age_seconds"],
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
        msg = f"無法從 TDX 取得路線「{route}」的軌跡或站牌資料，請確認路線名稱是否正確"
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
# API：系統維護（重建站點快取）
# ══════════════════════════════════════════════════════════
@app.route('/api/update_cache', methods=['POST'])
def api_update_cache():
    """強制重新抓取『全部路線』的站牌資料並存檔到 /opt/render/project/data/route。
    一般情況下不需要手動按這顆按鈕——每條路線第一次被查詢或在地圖上顯示時，
    就會自動存檔；這顆按鈕只是用來一次性強制刷新全部路線的最新資料。"""
    count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_and_save_stop_data, r): r for r in get_all_known_routes()}
        for fut in concurrent.futures.as_completed(futures):
            try:
                if fut.result():
                    count += 1
            except Exception:
                pass
    # 清除相關記憶體快取，讓下一次查詢立即反映新資料
    _cache_store.clear()
    return jsonify({"status": "success", "count": count})


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


if __name__ == '__main__':
    # 只有明確設定 FLASK_DEBUG=1 才會開 debug 模式（會洩漏詳細錯誤堆疊、也會拖慢速度），
    # 避免哪天不小心直接用 `python app.py` 跑在正式環境時，忘記帶入 debug=True。
    # 正式環境還是強烈建議用 gunicorn（見 start.sh）啟動，而不是靠這個內建開發伺服器；
    # 這裡開 threaded=True 純粹是讓「不小心」用這個內建伺服器時，至少還能同時處理
    # 一個以上的請求，不會退化成完全一個一個排隊。
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode, threaded=True, port=int(os.environ.get("PORT", 5000)))
