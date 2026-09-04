# 台南公車 AI 助理 — Flask 版

這是原本 `bus_web.py`（Streamlit）的 **Flask 版本**，功能一對一轉移，沒有刪減或變更任何邏輯。

## 安裝與執行

```bash
cd tainan_bus_flask
pip install -r requirements.txt
cp .env.example .env   # 填入 CLIENT_ID / CLIENT_SECRET / GROQ_API_KEY
python app.py
```

瀏覽器開啟 http://localhost:5000

## 架構

```
app.py                 Flask 後端，所有 API 路由 + TDX/天氣/UBike/Groq 邏輯
templates/index.html   單頁應用程式外殼（查詢頁 + 地圖頁）
static/css/style.css   樣式（時間軸、標籤、地圖側欄樣式與原版相同）
static/js/app.js       前端邏輯，透過 fetch() 呼叫下方 API
tainan_stops_cache.json 系統維護「更新站點快取」產生的檔案（執行後才會出現）
```

## 功能對照表

| 原 Streamlit 功能 | Flask 對應 |
|---|---|
| 側邊欄字體放大/縮小 | `#btn-font-toggle`（純前端 CSS class 切換） |
| 查詢頁 / 地圖頁切換 | `#btn-page-toggle`，前端顯示/隱藏對應區塊 |
| AI 對話記錄（多分頁、新增/切換/刪除） | `/api/chat/sessions*`，伺服器端以 `SESSION_STORE`（依瀏覽器 cookie）保存 |
| 最愛路線 / 最近查詢 | `/api/favorites`, `/api/recent` |
| 路線顏色/數字快速篩選 | `/api/filter_routes` |
| 站點選單、即時到站時間軸、無障礙/電動車/UBike標籤 | `/api/route_stops`, `/api/route_status` |
| UBike 比公車快的建議（OSRM 路徑，含直線估算備援） | `check_ubike_suggestion()`（原封不動搬移） |
| GPS 定位 + 附近站牌搜尋 | 前端 `navigator.geolocation` + `/api/nearby_stops` |
| 語音朗讀 (TTS) / 停止朗讀 | 前端 Web Speech API，文字由 `/api/route_status` 回傳的 `tts_text` |
| 進階查詢（站到站，直達 + 一次轉乘） | `/api/advanced_search/stops`, `/api/advanced_search` |
| 客運（跨縣市）業者/路線/到站查詢 | `/api/intercity/operators`, `/api/intercity/routes`, `/api/intercity/detail` |
| 系統維護：重建全台南站點快取 | `/api/update_cache`（POST，寫入 `tainan_stops_cache.json`） |
| 公車即時地圖（Leaflet，路線篩選、路線清單面板、圖層開關） | `/api/map_data` + `static/js/app.js` 中的 Leaflet 邏輯（與原本注入的 HTML/JS 相同） |
| AI 助理（Groq，llama-3.3-70b-versatile，帶天氣/公車狀態 context） | `/api/chat` |

## 與原本 Streamlit 版本的架構差異（必要的，因為框架不同）

- Streamlit 的 `st.session_state` → 改用 Flask session cookie（僅存一組匿名 `uid`）搭配伺服器端記憶體字典 `SESSION_STORE`，效果相同：重新整理頁面資料還在，重啟伺服器才會清空。
- `st.cache_data(ttl=...)` → 改用同樣邏輯的 `@cached(ttl_seconds)` 裝飾器（TTL 完全對應：站點 3600s、到站時間 30s、天氣 600s、UBike 60s、全站牌 300s、路線軌跡 3600s、客運路線 3600s、客運到站 60s）。
- Streamlit 的自動 rerun → 改為前端 `fetch()` 呼叫對應 API 後局部更新 DOM。
- 原本用 `st.components.v1.html()` 注入的 Leaflet 地圖 HTML/JS，原封不動搬到 `static/js/app.js` 內的 `initMapPageIfNeeded()` / `drawMapBuses()` / `drawMapShapes()` / `renderMapPanel()`，資料改為透過 `/api/map_data` 取得而非 Python 字串注入。

## 注意事項

- 本機測試環境沒有對外網路，因此本次未實際啟動伺服器連線 TDX/Groq/OSRM 驗證，請在有網路的環境安裝依賴後執行 `python app.py` 測試。
- `app.secret_key` 預設用 `os.urandom(24)`，代表**每次重啟伺服器都會清空所有人的最愛/最近查詢/對話記錄**（與原本 `bus.py` 骨架的行為一致）。如果需要跨重啟保留，請在 `.env` 設定固定的 `FLASK_SECRET_KEY`，並考慮把 `SESSION_STORE` 換成檔案或資料庫儲存。
