(function () {
'use strict';
// 每一列：[中文原文（數字／時間／路線代號／「」內的名稱以 # 代替）, English, 日本語]
// 譯文裡用 {1} {2}… 代表依序出現的 # 內容；沒有寫 {n} 時 # 依序原樣放回。
const ROWS = [
// ── 側邊欄／標題 ──
['🚌 台南公車 AI 助理','🚌 Tainan Bus AI Assistant','🚌 台南バス AIアシスタント'],
['開啟選單','Open menu','メニューを開く'],
['🚌 台南公車助理','🚌 Tainan Bus Assistant','🚌 台南バスアシスタント'],
['🔠 放大字體（視障輔助）','🔠 Larger text (accessibility)','🔠 文字を拡大（視覚サポート）'],
['🔡 縮小字體','🔡 Smaller text','🔡 文字を縮小'],
['🗺️ 公車即時地圖','🗺️ Live Bus Map','🗺️ バスリアルタイムマップ'],
['🚌 回到查詢頁面','🚌 Back to search page','🚌 検索ページへ戻る'],
['💬 AI 對話記錄','💬 AI Chat History','💬 AIチャット履歴'],
['📋 對話記錄','📋 Chat History','📋 チャット履歴'],
['➕ 新對話','➕ New chat','➕ 新しいチャット'],
['⭐ 最愛路線','⭐ Favorite routes','⭐ お気に入り路線'],
['🕐 最近查詢','🕐 Recent searches','🕐 最近の検索'],
['🔔 到站鈴聲提醒','🔔 Arrival alert sound','🔔 到着アラーム'],
['啟用鈴聲提醒（需要瀏覽器分頁保持開啟）','Enable alert sound (keep this browser tab open)','アラームを有効にする（ブラウザのタブを開いたままにしてください）'],
['🔍 進階查詢（站到站）','🔍 Advanced search (stop to stop)','🔍 詳細検索（停留所〜停留所）'],
['輸入起站與終點，找出直達或轉乘一次的路線','Enter a start and a destination to find direct routes or routes with one transfer','出発地と目的地を入力して、直通または1回乗り換えの路線を探します'],
['🔎 快速篩選站名（可不填，直接在下面欄位打字或選單找也可以）','🔎 Quick stop filter (optional — you can also type or pick in the fields below)','🔎 停留所をすばやく絞り込み（空欄でも可。下の欄に入力・選択もできます）'],
['輸入關鍵字篩選下面的站名選單...','Type a keyword to filter the stop list below...','キーワードで下の停留所リストを絞り込み...'],
['出發站','Start stop','出発停留所'],
['請選擇或輸入站名...','Select or type a stop name...','停留所名を選択または入力...'],
['目的站','Destination stop','目的停留所'],
['🔎 查詢路線','🔎 Find routes','🔎 路線を検索'],
['👤 帳號登入','👤 Sign in','👤 アカウントログイン'],
['登入後，最愛路線、最近查詢、AI 對話記錄都會綁定在帳號上，換裝置、換瀏覽器登入一樣看得到。','After signing in, your favorites, recent searches and AI chat history are tied to your account, so you can see them on any device or browser.','ログインすると、お気に入り路線・最近の検索・AIチャット履歴がアカウントに紐づき、端末やブラウザを変えても確認できます。'],
['帳號','Username','アカウント'],
['輸入帳號...','Enter username...','アカウントを入力...'],
['密碼','Password','パスワード'],
['輸入密碼...','Enter password...','パスワードを入力...'],
['🔑 登入','🔑 Sign in','🔑 ログイン'],
['📝 註冊新帳號','📝 Create account','📝 新規登録'],
['已登入：','Signed in:','ログイン中：'],
['已登入','Signed in','ログイン中'],
['🚪 登出','🚪 Sign out','🚪 ログアウト'],
['⚙️ 系統維護','⚙️ System maintenance','⚙️ システム保守'],
['🔄 更新全台南站點快取','🔄 Refresh Tainan stop cache','🔄 台南の停留所キャッシュを更新'],
['請輸入帳號與密碼','Please enter username and password','アカウントとパスワードを入力してください'],
['帳號至少需要 # 個字','Username must be at least {1} characters','アカウントは{1}文字以上で入力してください'],
['密碼至少需要 # 碼','Password must be at least {1} characters','パスワードは{1}文字以上で入力してください'],
['這個帳號已經被註冊了，請直接登入或換一個帳號','This username is already taken. Please sign in or choose another.','このアカウントは既に登録されています。ログインするか別のアカウントを使ってください'],
['帳號建立失敗，請稍後再試','Could not create the account. Please try again later.','アカウントを作成できませんでした。後でもう一度お試しください'],
['帳號或密碼錯誤','Wrong username or password','アカウントまたはパスワードが違います'],
['處理中...','Processing...','処理中...'],
// ── 首頁 ──
['🚌 台南公車即時時刻查詢','🚌 Tainan Bus Real-time Arrivals','🚌 台南バス リアルタイム時刻案内'],
['📋 今日行程總覽','📋 Today at a glance','📋 本日のまとめ'],
['台南市・今日天氣','Tainan · Today’s weather','台南市・今日の天気'],
['即時到站','Live arrivals','リアルタイム到着'],
['朗讀','Read aloud','読み上げ'],
['🔔 提醒已響起','🔔 Alert triggered','🔔 アラームが鳴りました'],
['動態公車','Live buses','バス位置'],
['路線篩選','Route filter','路線で絞り込み'],
['附近站牌','Nearby stops','近くの停留所'],
['AI 對話','AI chat','AIチャット'],
['我的最愛','Favorites','お気に入り'],
['小黃公車','Yellow minibus','小黄バス'],
['站到站查詢','Stop to stop','停留所間検索'],
['更多設定','More settings','その他の設定'],
['← 返回主頁','← Back to home','← ホームへ戻る'],
['🚕 小黃公車查詢','🚕 Yellow minibus search','🚕 小黄バス検索'],
['小黃公車是用計程車營運的固定路線公車，查詢方式跟一般公車完全一樣。','Yellow minibuses are fixed-route buses run with taxis. You search for them exactly like regular buses.','小黄バスはタクシーで運行される定路線バスです。検索方法は通常のバスと同じです。'],
['選擇小黃公車路線','Select a yellow minibus route','小黄バスの路線を選択'],
['請選擇路線...','Select a route...','路線を選択...'],
['共 # 條小黃公車路線（資料來自 TDX，依營運業者自動判斷）','{1} yellow minibus routes (data from TDX, detected by operator)','小黄バス路線 全{1}本（TDXのデータ、運行事業者で自動判定）'],
['路線清單載入中...','Loading route list...','路線リストを読み込み中...'],
['❌ 路線清單載入失敗','❌ Failed to load route list','❌ 路線リストの読み込みに失敗'],
['載入路線清單中...','Loading route list...','路線リストを読み込み中...'],
['路線清單載入失敗','Failed to load route list','路線リストの読み込みに失敗'],
// ── 路線篩選 ──
['🔍 路線篩選','🔍 Route filter','🔍 路線フィルター'],
['點選顏色或數字篩選：','Tap a color or number to filter:','色または数字をタップして絞り込み：'],
['綠','Green','緑'],['橘','Orange','橙'],['棕','Brown','茶'],['藍','Blue','青'],['紅','Red','赤'],['黃','Yellow','黄'],
['市區','City','市区'],['高鐵','HSR','高鐵'],['高鐵快捷','HSR Express','高鐵シャトル'],['觀光','Tourist','観光'],
['東山','Dongshan','東山'],['梅嶺','Meiling','梅嶺'],['菱波','Lingbo','菱波'],['雙層','Double-decker','2階建て'],['雙層巴士','Double-decker bus','2階建てバス'],
['❌ 清除篩選','❌ Clear filter','❌ 絞り込み解除'],
['顯示：全部路線','Showing: all routes','表示：全路線'],
['篩選：【#】（共 # 條）','Filter: [{1}] ({2} routes)','絞り込み：【{1}】（全{2}本）'],
['這個篩選條件下沒有符合的路線','No routes match this filter','この条件に合う路線はありません'],
['選擇路線','Select route','路線を選択'],
['請選擇或輸入路線...','Select or type a route...','路線を選択または入力...'],
['☆ 加入最愛','☆ Add to favorites','☆ お気に入りに追加'],
['⭐ 已加入最愛','⭐ In favorites','⭐ お気に入り登録済み'],
['等候站','Waiting stop','乗車停留所'],
['目的地','Destination','目的地'],
['🔔 到站前幾分鐘提醒（會有鈴聲）','🔔 Alert minutes before arrival (with sound)','🔔 到着の何分前に通知（音あり）'],
['🔔 加入到站提醒','🔔 Add arrival alert','🔔 到着アラームを追加'],
['請選擇路線','Please select a route','路線を選択してください'],
['📅 查看固定時刻表','📅 View fixed timetable','📅 固定時刻表を見る'],
['時刻表載入中...','Loading timetable...','時刻表を読み込み中...'],
['這條路線目前沒有公告固定時刻表','This route has no published fixed timetable','この路線には現在、固定時刻表の公開がありません'],
['❌ 時刻表載入失敗','❌ Failed to load timetable','❌ 時刻表の読み込みに失敗'],
['查無這條路線的固定時刻表','No fixed timetable found for this route','この路線の固定時刻表が見つかりません'],
['TDX 目前查不到這條路線的固定時刻表（可能是依班距發車的幹線，沒有公告時刻表）','TDX has no fixed timetable for this route right now (it may be a trunk line that runs at intervals without a published timetable).','TDXにこの路線の固定時刻表がありません（運行間隔で走る幹線で、時刻表が公開されていない可能性があります）。'],
['一、二、三、四、五','Mon–Fri','月〜金'],['六、日','Sat–Sun','土・日'],['六','Sat','土'],['日','Sun','日'],['每日','Daily','毎日'],
['⚠️ 尚未更新資料，目前顯示的可能是稍早的結果','⚠️ Data not updated yet — what you see may be earlier results','⚠️ データ未更新です。表示は少し前の結果の可能性があります'],
['➡️ 往','➡️ To','➡️ 行先：'],['⬅️ 往','⬅️ To','⬅️ 行先：'],
['🔄 重新整理','🔄 Refresh','🔄 更新'],
['🔀 本路線目前有','🔀 This route currently has','🔀 この路線には現在'],
['班支線／繞道車輛','branch / detour bus(es)','台の支線・迂回車両があります'],
['🔊 朗讀公車資訊','🔊 Read bus info aloud','🔊 バス情報を読み上げ'],
['⏹ 停止','⏹ Stop','⏹ 停止'],
['👈 請從左側選擇路線開始查詢','👈 Choose a route on the left to start','👈 左側から路線を選んで検索を始めてください'],
['去程','Outbound','往路'],['回程','Return','復路'],
['（去程）','(Outbound)','（往路）'],['（回程）','(Return)','（復路）'],
['➡️ 去程','➡️ Outbound','➡️ 往路'],['⬅️ 回程','⬅️ Return','⬅️ 復路'],
['✕ 收合設定','✕ Hide settings','✕ 設定を閉じる'],
['☰ 更多設定','☰ More settings','☰ その他の設定'],
['⏱️ #s 後自動更新','⏱️ Auto-refresh in {1}s','⏱️ {1}秒後に自動更新'],
// ── AI ──
['🤖 AI 助理','🤖 AI Assistant','🤖 AIアシスタント'],
['目前對話：','Current chat:','現在のチャット：'],
['有什麼我可以幫忙的嗎？','How can I help you?','何かお手伝いできますか？'],
['送出','Send','送信'],
['思考中...','Thinking...','考え中...'],
['AI 錯誤','AI error','AIエラー'],
['AI 模組未啟用，請檢查 GROQ_API_KEY。','The AI module is not enabled. Please check GROQ_API_KEY.','AIモジュールが有効になっていません。GROQ_API_KEYを確認してください。'],
['缺少訊息內容','Message is empty','メッセージがありません'],
['尚未查詢','Not searched yet','未検索'],
['尚未查詢路線','No route searched yet','路線は未検索です'],
// ── 附近站牌 ──
['📍 附近公車站','📍 Nearby bus stops','📍 近くのバス停'],
['📡 自動取得座標','📡 Get my coordinates','📡 座標を自動取得'],
['緯度：','Latitude:','緯度：'],['點上方取得','Tap above to get','上のボタンで取得'],['經度：','Longitude:','経度：'],
['緯度','Latitude','緯度'],['例：22.9997','e.g. 22.9997','例：22.9997'],['經度','Longitude','経度'],['例：120.2270','e.g. 120.2270','例：120.2270'],
['🔍 搜尋附近站牌','🔍 Search nearby stops','🔍 近くの停留所を検索'],
['瀏覽器不支援定位','Geolocation not supported','位置情報に未対応です'],
['瀏覽器不支援定位功能','This browser does not support geolocation','このブラウザは位置情報に対応していません'],
['請允許瀏覽器定位權限','Please allow location permission in your browser','ブラウザの位置情報の許可を有効にしてください'],
['請輸入有效數字','Please enter valid numbers','有効な数値を入力してください'],
['搜尋中...','Searching...','検索中...'],
['附近 #m 內無公車站牌','No bus stops within {1} m','{1}m以内にバス停はありません'],
['找到 # 個站牌（#m內）：','Found {1} stops (within {2} m):','{1}か所の停留所が見つかりました（{2}m以内）：'],
['無法載入站牌資料','Could not load stop data','停留所データを読み込めません'],
['# 公尺','{1} m','{1}m'],['# 公里','{1} km','{1}km'],['# 公里（直線估算）','{1} km (straight-line estimate)','{1}km（直線距離の概算）'],
// ── 地圖 ──
['🚌 台南公車即時地圖','🚌 Tainan Live Bus Map','🚌 台南バス リアルタイムマップ'],
['篩選路線（留空或輸入「#」＝顯示全部路線），例：#, #','Filter routes (leave blank or type “{1}” = show all routes), e.g. {2}, {3}','路線を絞り込み（空欄または「{1}」で全路線を表示）例：{2}, {3}'],
['全部','All','全て'],['全部路線','All routes','全路線'],
['💡 可輸入多條路線（用逗號分隔），或直接點下面路線清單裡的項目累加選取','💡 Enter several routes separated by commas, or tap items in the list below to add them','💡 複数の路線をカンマ区切りで入力するか、下のリストの項目をタップして追加できます'],
['🔄 更新','🔄 Update','🔄 更新'],
['🏠 回查詢頁','🏠 Back to search','🏠 検索ページへ'],
['📍 定位我的位置','📍 Locate me','📍 現在地を表示'],
['💾 抓取並儲存路線原始資料（Shape＋StopOfRoute）','💾 Fetch & save raw route data (Shape + StopOfRoute)','💾 路線の元データを取得・保存（Shape＋StopOfRoute）'],
['輸入路線名稱，例：#','Enter a route name, e.g. {1}','路線名を入力（例：{1}）'],
['💾 抓取並儲存到檔案','💾 Fetch & save to file','💾 取得してファイルに保存'],
['載入中...','Loading...','読み込み中...'],
['💡 放大地圖至街道等級即可看到各站站名；任何縮放層級皆可點擊圓點查看站名','💡 Zoom in to street level to see stop names; at any zoom level you can tap a dot to see its name','💡 地図を道路レベルまで拡大すると停留所名が表示されます。どの縮尺でも点をタップすると名前を確認できます'],
['搜尋路線...','Search routes...','路線を検索...'],
['請點選路線，或按下方「#」載入公車動態與軌跡','Pick a route, or tap “{1}” below to load bus positions and route lines','路線を選ぶか、下の「{1}」を押してバスの位置と路線を読み込みます'],
['載入失敗','Failed to load','読み込みに失敗しました'],
['地圖尚未載入完成，請稍後再試','The map is not ready yet. Please try again shortly.','地図がまだ読み込まれていません。しばらくしてからお試しください'],
['定位中...','Locating...','位置を取得中...'],
['📍 我的位置','📍 My location','📍 現在地'],
['已定位到你目前的位置','Moved to your current location','現在地に移動しました'],
['無法取得定位，請確認已允許瀏覽器的定位權限','Could not get your location. Please make sure location permission is allowed.','位置を取得できません。ブラウザの位置情報の許可を確認してください'],
['⚠️ 請輸入路線名稱','⚠️ Please enter a route name','⚠️ 路線名を入力してください'],
['抓取「#」的 Shape 與 StopOfRoute 資料中...','Fetching Shape and StopOfRoute data for “{1}”...','「{1}」のShapeとStopOfRouteデータを取得中...'],
['✅ 已儲存','✅ Saved','✅ 保存しました'],['路線軌跡：','Route line: ','路線の軌跡：'],['站牌清單：','Stop list: ','停留所リスト：'],['❌ 抓取失敗','❌ Fetch failed','❌ 取得に失敗'],
['點一下可直接改用這個名稱重新抓取：','Tap to retry with this name:','タップするとこの名前で再取得します：'],
['TDX 上找不到名稱包含「#」的路線，可能是這個字首本身就不對','No route on TDX contains “{1}”. The prefix itself may be wrong.','TDXに「{1}」を含む路線が見つかりません。入力した文字が違うかもしれません'],
['TDX 上名稱包含「#」的路線（點一下可直接改用這個名稱查詢）：','Routes on TDX containing “{1}” (tap one to search with that name):','TDX上で「{1}」を含む路線（タップするとその名前で検索）：'],
['查詢失敗','Search failed','検索に失敗しました'],
['顯示 # 條路線・# 台公車','Showing {1} routes · {2} buses','{1}路線・{2}台を表示'],
['🔼 收合公車定位清單（共 # 台）','🔼 Hide bus position list ({1})','🔼 バス位置リストを閉じる（{1}台）'],
['🚌 查看路線上每台公車的最新定位（共 # 台）','🚌 View the latest position of every bus on the route ({1})','🚌 路線上の各バスの最新位置を見る（{1}台）'],
['目前查無這個篩選條件下的公車即時定位（該路線可能暫時沒有營運中的車輛，也可能是路線名稱跟 TDX 登記的不完全一樣）','No live bus positions for this filter (the route may have no buses running right now, or its name may differ from the one registered on TDX).','この条件に該当するバスの位置情報はありません（運行中の車両がないか、路線名がTDXの登録名と異なる可能性があります）。'],
['🔍 查詢 TDX 正確路線名稱','🔍 Look up the exact TDX route name','🔍 TDXの正確な路線名を検索'],
['🚌 目前共 # 台公車最新定位（點項目可在地圖上定位）：','🚌 Latest positions of {1} buses (tap an item to find it on the map):','🚌 現在{1}台のバスの最新位置（項目をタップすると地図上に表示）：'],
['車牌','Plate','ナンバー'],['方向','Direction','方向'],['速度','Speed','速度'],
['已儲存路線原始資料（Shape＋StopOfRoute）','Saved raw route data (Shape + StopOfRoute)','保存済みの路線元データ（Shape＋StopOfRoute）'],
['已儲存路線原始資料（Shape＋StopOfRoute）－目前不在篩選範圍內，點一下即可加入','Saved raw route data (Shape + StopOfRoute) – not in the current filter; tap to add','保存済みの路線元データ（Shape＋StopOfRoute）－現在の絞り込み外です。タップで追加'],
['# 條路線','{1} routes','{1}路線'],
// ── 即時狀態 ──
['無法取得即時動態。','Could not get real-time information.','リアルタイム情報を取得できません。'],
['無法取得即時動態','Could not get real-time information','リアルタイム情報を取得できません'],
['缺少路線','Route missing','路線が指定されていません'],
['缺少路線名稱','Route name missing','路線名が指定されていません'],
['⚠️ 無法載入【#】站點。','⚠️ Could not load stops for [{1}].','⚠️ 【{1}】の停留所を読み込めませんでした。'],
['🌙 末班車已收車','🌙 Last bus has departed','🌙 終バスは終了しました'],
['🌙 末班車已收車，明日 @ 恢復發車','🌙 Service has ended. Buses resume tomorrow at {1}.','🌙 本日の運行は終了しました。明日 {1} に再開します。'],
['🌡️ 台南目前天氣','🌡️ Tainan weather now','🌡️ 台南の現在の天気'],
['氣溫 #°C','{1}°C','気温 {1}°C'],['風速 # km/h','wind {1} km/h','風速 {1}km/h'],
['大致晴朗🌤️','Mostly clear 🌤️','おおむね晴れ🌤️'],['晴天☀️','Sunny ☀️','晴れ☀️'],['有霧🌫️','Foggy 🌫️','霧🌫️'],['部分多雲⛅','Partly cloudy ⛅','一部曇り⛅'],['陰天☁️','Overcast ☁️','曇り☁️'],
['中雨🌧️','Moderate rain 🌧️','雨🌧️'],['大雨🌧️','Heavy rain 🌧️','大雨🌧️'],['小雨🌧️','Light rain 🌧️','小雨🌧️'],['毛毛雨🌦️','Drizzle 🌦️','霧雨🌦️'],['陣雨🌦️','Showers 🌦️','にわか雨🌦️'],['雷雨⛈️','Thunderstorm ⛈️','雷雨⛈️'],
['未知天氣','Unknown weather','天気不明'],['無法取得天氣','Weather unavailable','天気を取得できません'],['晴朗舒適','Clear and comfortable','晴れて快適'],
['（目前共 # 台營運中）','({1} buses in service)','（現在 {1} 台が運行中）'],
['🚌 # 全線即時動態','🚌 {1} — live status of the whole route','🚌 {1} 全線リアルタイム状況'],
['♿ 無障礙','♿ Accessible','♿ バリアフリー'],['🚌 一般車','🚌 Standard bus','🚌 一般車両'],['⚡ 電動','⚡ Electric','⚡ 電気バス'],
['🚲 可借:# 可還:#','🚲 Rent: {1}  Return: {2}','🚲 貸出可:{1}  返却可:{2}'],
['－ 🚌 # 🔀 往','– 🚌 {1} 🔀 to','－ 🚌 {1} 🔀 行先'],
['無障礙低底盤。','Low-floor accessible bus.','ノンステップバスです。'],['電動公車。','Electric bus.','電気バスです。'],
['即將進站','Arriving soon','まもなく到着'],['進站中','Arriving','到着中'],
['# 分鐘','{1} min','{1}分'],
['（時刻表估計）','(timetable estimate)','（時刻表による推定）'],
['即將進站（時刻表估計）','Arriving soon (timetable estimate)','まもなく到着（時刻表による推定）'],
['# 分鐘（時刻表估計）','{1} min (timetable estimate)','{1}分（時刻表による推定）'],
['約 # 分鐘（時刻表估計）','≈ {1} min (timetable estimate)','約{1}分（時刻表による推定）'],
['尚未發車','Not departed yet','未発車'],['交管不停靠','Skipped (traffic control)','交通規制で通過'],['末班車已過','Last bus has passed','終バスは通過済み'],['今日停駛','No service today','本日運休'],['營運中（無預估時間）','In service (no estimate)','運行中（到着予測なし）'],['無車牌','No plate','ナンバーなし'],
['大巴','Large bus','大型バス'],['中巴','Medium bus','中型バス'],['小巴','Small bus','小型バス'],
['資料剛更新','Data just updated','データを今更新しました'],['資料 # 分鐘前','Data {1} min ago','データ {1}分前'],
['📡 到站資料','📡 Arrival data fetched','📡 到着データ取得：'],['抓取','',''],
['剛更新','just updated','たった今更新'],['# 分鐘前','{1} min ago','{1}分前'],['# 小時 # 分鐘前','{1} h {2} min ago','{1}時間{2}分前'],['# 小時前','{1} h ago','{1}時間前'],
['（尖峰約每 # 分、平常約每 # 分更新；倒數是依時間推算，下次更新會校正）','(Updated about every {1} min at rush hour and {2} min otherwise; the countdown is estimated and corrected at the next update.)','（ラッシュ時は約{1}分、通常は約{2}分ごとに更新。カウントダウンは推定で、次回の更新で補正されます）'],
['⚠️ 尚未更新資料','⚠️ Data not updated yet','⚠️ データ未更新'],
['🚲 UBike 更快！實際騎車約 # 分鐘（#），比等公車+搭車（約 # 分鐘）更省時。','🚲 UBike is faster! Cycling takes about {1} min ({2}), vs. about {3} min waiting for and riding the bus.','🚲 UBikeの方が速い！自転車で約{1}分（{2}）、バスの待ち＋乗車は約{3}分です。'],
['- 起點 UBike：#（可借 # 輛）','- Start UBike: {1} ({2} bikes available)','- 出発地のUBike：{1}（貸出可 {2}台）'],
['- 終點 UBike：#（可還 # 格）','- End UBike: {1} ({2} docks free)','- 目的地のUBike：{1}（返却可 {2}枠）'],
// ── 提醒／鈴聲／快取 ──
['🔔 已設定：#（往#）到站前 # 分鐘提醒','🔔 Alert set: {1} (to {2}), {3} min before arrival','🔔 設定済み：{1}（{2}方面）到着{3}分前に通知'],
['（往#）－ #','(to {1}) – {2}','（{1}方面）－ {2}'],
['到站前 # 分鐘提醒','Alert {1} min before arrival','到着{1}分前に通知'],
['查詢中...','Searching...','検索中...'],
['🔔 #（往#）即將抵達「#」','🔔 {1} (to {2}) is about to reach “{3}”','🔔 {1}（{2}方面）がまもなく「{3}」に到着'],
['公車到站提醒','Bus arrival alert','バス到着アラート'],
['請先選擇路線與站牌，再加入到站提醒','Please choose a route and a stop before adding an alert','アラームを追加する前に、路線と停留所を選んでください'],
['最多只能設定 # 個到站提醒，請先刪除幾個舊的','You can set up to {1} alerts. Please delete some old ones first.','アラームは最大{1}件までです。古いものを削除してください'],
['離線化中，請稍候（可能需要幾分鐘）...','Building offline cache, please wait (may take a few minutes)...','オフライン化中です。しばらくお待ちください（数分かかる場合があります）...'],
['🎉 快取建立成功！共 # 條路線','🎉 Cache built! {1} routes','🎉 キャッシュを作成しました！全{1}路線'],
['建立失敗','Build failed','作成に失敗しました'],
['24 小時內已經更新過了（為了節省 TDX 額度），請明天再試','Already refreshed within the last 24 hours (to save TDX quota). Please try again tomorrow.','24時間以内に更新済みです（TDXの利用枠を節約するため）。明日もう一度お試しください'],
['目前正在更新中，請稍後再看結果','An update is in progress. Please check back shortly.','更新中です。しばらくしてから結果をご確認ください'],
// ── 站到站查詢／轉乘 ──
['請選擇出發站和目的站','Please select a start and a destination','出発停留所と目的停留所を選択してください'],
['出發站和目的站不能相同','Start and destination must be different','出発と目的地は別の停留所にしてください'],
['暫時無法取得站點資料，請稍後再試','Stop data is temporarily unavailable. Please try again later.','停留所データを一時的に取得できません。後でもう一度お試しください'],
['站序資料還沒準備好，請稍後再試','Stop sequence data is not ready yet. Please try again shortly.','停留所の順序データがまだ準備できていません。しばらくしてからお試しください'],
['轉乘時間估算失敗，請稍後再試','Could not estimate trip times. Please try again later.','所要時間を見積もれませんでした。後でもう一度お試しください'],
['不到 # 分','Under {1} min','{1}分未満'],['# 分','{1} min','{1}分'],['# 小時 # 分','{1} h {2} min','{1}時間{2}分'],['# 小時','{1} h','{1}時間'],
['無資料','No data','データなし'],
['約 # 分','≈ {1} min','約{1}分'],['約 # 小時 # 分','≈ {1} h {2} min','約{1}時間{2}分'],['約 # 小時','≈ {1} h','約{1}時間'],
['@ 抵達','Arrive {1}','{1} 着'],
['至少 # 分','At least {1} min','少なくとも{1}分'],['至少 # 小時 # 分','At least {1} h {2} min','少なくとも{1}時間{2}分'],
['不含第二班等車','excl. wait for the 2nd bus','2本目の待ち時間を除く'],
['無法估算','Cannot estimate','見積もり不可'],
['下一班即將進站','Next bus arriving soon','次のバスはまもなく到着'],
['下一班約 # 分鐘','Next bus in about {1} min','次のバスは約{1}分後'],
['下一班','Next bus','次のバス'],
['在「#」等車','Wait at “{1}”','「{1}」で待つ'],
['搭 # 往','Take {1} to','{1} に乗車・行先'],
['坐 # 站','Ride {1} stops','{1}停留所乗車'],
['抵達「#」','Arrive at “{1}”','「{1}」に到着'],
['直達・# 站','Direct · {1} stops','直通・{1}停留所'],
['# 站','{1} stops','{1}停留所'],
['步行約 # 公尺到「#」','Walk about {1} m to “{2}”','「{2}」まで約{1}m歩く'],
['在「#」下車後步行','Get off at “{1}” and walk','「{1}」で降りて徒歩'],
['在「#」下車轉乘','Get off at “{1}” and transfer','「{1}」で降りて乗り換え'],
['同站轉乘','Same-stop transfer','同じ停留所で乗り換え'],
['坐 # 站・到「#」','Ride {1} stops to “{2}”','{1}停留所乗車・「{2}」まで'],
['等 # 往','Wait for {1} to','{1} 待ち・行先'],
['第二班車','2nd bus','2本目のバス'],
['搭 #','Take {1}','{1} に乗車'],
['轉乘 # 次・# 站','Transfer ×{1} · {2} stops','乗換{1}回・{2}停留所'],
['🔄 重新計算時間','🔄 Recalculate times','🔄 時間を再計算'],
['⏱ 運算轉乘時間','⏱ Calculate trip times','⏱ 所要時間を計算'],
['按下按鈕，估算每個方案的等車、搭乘、步行與抵達時間','Tap to estimate waiting, riding, walking and arrival times for each option','ボタンを押すと、各プランの待ち時間・乗車時間・徒歩・到着時刻を概算します'],
['現在 @・搭乘時間以每站約 # 分估算；等車依「#」到站、「#」或歷史「#」（平均等班距的一半）','Now {1} · Riding time is estimated at about {2} min per stop; waiting uses “{3}” arrivals, “{4}” or historical “{5}” (average wait = half the headway)','現在 {1}・乗車時間は1停留所あたり約{2}分で概算。待ち時間は「{3}」到着、「{4}」、または過去の「{5}」（平均待ち＝運行間隔の半分）を使用'],
['即時','Live','リアルタイム'],['時刻表','Timetable','時刻表'],['班距','Headway','運行間隔'],
['✅ 直達（共 # 個方案）','✅ Direct ({1} options)','✅ 直通（{1}プラン）'],
['無直達路線','No direct route','直通路線なし'],
['🔄 轉乘一次方案（共 # 個，依站數排序）','🔄 One-transfer options ({1}, sorted by number of stops)','🔄 1回乗り換えプラン（{1}件、停留所数順）'],
['🔄 轉乘一次方案（共 # 個，依總時間排序）','🔄 One-transfer options ({1}, sorted by total time)','🔄 1回乗り換えプラン（{1}件、所要時間順）'],
['※ 尚未運算時間，目前只列站數；「#」指的是第一段車。','※ Times not calculated yet; only stop counts are shown. “{1}” refers to the first bus.','※ 所要時間は未計算で、停留所数のみ表示しています。「{1}」は最初のバスを指します。'],
['找不到直達或一次轉乘方案，請考慮其他方式','No direct or one-transfer option found. Please consider other ways.','直通・1回乗り換えのプランが見つかりません。他の方法をご検討ください。'],
['✅ 直達路線（共 # 條）','✅ Direct routes ({1})','✅ 直通路線（{1}本）'],
['🔄 轉乘一次方案（共 # 個）','🔄 One-transfer options ({1})','🔄 1回乗り換えプラン（{1}件）'],
['搭','Take','乗車'],['→ 在','→ at','→ '],['轉','transfer to','乗換:'],
['找不到一次轉乘方案，請考慮其他方式','No one-transfer option found. Please consider other ways.','1回乗り換えのプランが見つかりません。他の方法をご検討ください。'],
['運算中...','Calculating...','計算中...'],
['約 #','About {1}','約 {1}'],
['約 # 分鐘','≈ {1} min','約{1}分'],
// ── 分享／班距 ──
['🔗 分享這個站牌／路線（連結＋QR code）','🔗 Share this stop / route (link + QR code)','🔗 この停留所・路線を共有（リンク＋QRコード）'],
['QR code 產生中...','Generating QR code...','QRコードを生成中...'],
['📋 複製連結','📋 Copy link','📋 リンクをコピー'],['✕ 關閉','✕ Close','✕ 閉じる'],['✅ 已複製','✅ Copied','✅ コピーしました'],['📤 分享','📤 Share','📤 共有'],
['台南公車','Tainan Bus','台南バス'],
['🔗 #','🔗 {1}','🔗 {1}'],
['🔗 分享這一站','🔗 Share this stop','🔗 この停留所を共有'],['分享這一站','Share this stop','この停留所を共有'],
['用手機掃描 QR code 或點連結，會直接打開這條路線。','Scan the QR code with your phone or tap the link to open this route directly.','スマホでQRコードを読み取るかリンクをタップすると、この路線が直接開きます。'],
['用手機掃描 QR code 或點連結，會直接打開這條路線並停在這一站。','Scan the QR code with your phone or tap the link to open this route right at this stop.','スマホでQRコードを読み取るかリンクをタップすると、この停留所でこの路線が直接開きます。'],
['QR code 載入失敗（網路問題），請直接複製連結。','Could not load the QR code (network issue). Please copy the link instead.','QRコードを読み込めませんでした（ネットワークの問題）。リンクをコピーしてください。'],
['📊 歷史班距','📊 Typical headway','📊 過去の運行間隔'],
['📊 歷史班距（上班日・# 點）','📊 Typical headway (weekdays · {1}:00)','📊 過去の運行間隔（平日・{1}時）'],
['📊 歷史班距（假日・# 點）','📊 Typical headway (weekends/holidays · {1}:00)','📊 過去の運行間隔（休日・{1}時）'],
['還沒有累積到統計資料（系統每次更新到站資料時會自動累積，約一兩天後會有數字）。','No statistics yet (collected automatically with every arrival update; numbers appear after a day or two).','統計データはまだありません（到着情報の更新ごとに自動で蓄積され、1〜2日ほどで表示されます）。'],
['約','About','約'],['分鐘一班','min between buses','分間隔'],
['（樣本 #）','(samples: {1})','（サンプル数 {1}）'],
['這個時段樣本還不夠','Not enough samples for this hour yet','この時間帯はサンプルがまだ不足しています'],
['# 點','{1}:00','{1}時'],
['各時段班距（上班日）','Headway by hour (weekdays)','時間帯別の運行間隔（平日）'],
['各時段班距（假日）','Headway by hour (weekends/holidays)','時間帯別の運行間隔（休日）'],
['以「#」為參考站，取每個時段最近的實際班距中位數。','Based on “{1}” as the reference stop; median of recent real headways for each hour.','「{1}」を基準停留所とし、時間帯ごとの最近の実測運行間隔の中央値を表示しています。'],
// ── 地圖時鐘 ──
['🕒 現在','🕒 Now','🕒 現在'],
['📍 目前沒有公車定位資料','📍 No bus position data right now','📍 バスの位置データがありません'],
['📍 定位資料 @ 抓取：','📍 Position data fetched at {1}: ','📍 位置データ取得 {1}：'],
['・目前位置為依路線推算','· current positions are estimated along the route','・現在位置は路線上の推定です'],
['定位時間不明','Position time unknown','位置データの時刻不明'],
['圖上公車位置每 # 分鐘依路線推進一站（推算，非即時定位）','Bus icons advance one stop every {1} min along the route (estimated, not live GPS)','地図上のバスは{1}分ごとに路線に沿って1停留所ずつ進みます（推定で、リアルタイムGPSではありません）'],
['剛更新','just updated','たった今更新'],
];

// ══════════════════════════════════════════════════════════
// 多語系引擎（中文／English／日本語）
// 做法：程式照舊產生中文畫面，引擎在「畫面文字節點」上做即時翻譯。
//  - 原文都存在節點上（__orig），隨時可切回中文或換另一種語言。
//  - 邏輯用的字串（'去程'、路線名稱、API 參數）完全不動，只改顯示。
//  - 數字／時間／路線代號／「」內的名稱會先抽成 #，再比對上面的字典。
//  - 站名、路線名稱：英文版會用 TDX 本來就附的英文名稱（/api/name_map）。
// ══════════════════════════════════════════════════════════
const LANGS = {
  zh: { label: '中文',    locale: 'zh-TW', speech: 'zh-TW', html: 'zh-Hant' },
  en: { label: 'English', locale: 'en-GB', speech: 'en-US', html: 'en' },
  ja: { label: '日本語',   locale: 'ja-JP', speech: 'ja-JP', html: 'ja' },
};
const IDX = { en: 1, ja: 2 };
const CJK = /[\u3400-\u9fff]/;
const DICT = new Map(ROWS.map(r => [r[0], r]));
let lang = 'zh';
let nameMap = { all: {} };

const ROUTE_TOKEN = '(?:黃|棕|綠|橘|藍|紅)(?:\\d+(?:-\\d+)*|幹線|線)';
const CAP_RE = new RegExp(
  '(\\d{1,2}:\\d{2}(?::\\d{2})?)|「([^」]*)」|【([^】]*)】|（往([^）]*)）|(' + ROUTE_TOKEN + ')|(\\d+(?:\\.\\d+)?)', 'g');

function norm(s) {
  const caps = [];
  const key = s.replace(CAP_RE, (m, time, q1, q2, dest, route, num) => {
    if (time !== undefined) { caps.push({ t: 'v', v: time }); return '@'; }
    if (q1 !== undefined)   { caps.push({ t: 'n', v: q1 });   return '「#」'; }
    if (q2 !== undefined)   { caps.push({ t: 'n', v: q2 });   return '【#】'; }
    if (dest !== undefined) { caps.push({ t: 'n', v: dest }); return '（往#）'; }
    if (route !== undefined){ caps.push({ t: 'n', v: route });return '#'; }
    caps.push({ t: 'v', v: num }); return '#';
  });
  return { key, caps };
}

const COLOR_EN = { 黃: 'Yellow', 棕: 'Brown', 綠: 'Green', 橘: 'Orange', 藍: 'Blue', 紅: 'Red' };
function routeTokenEn(v) {
  const m = v.match(/^(黃|棕|綠|橘|藍|紅)(.*)$/);
  if (!m) return v;
  let rest = m[2];
  if (rest === '幹線') rest = ' Trunk'; else if (rest === '線') rest = ' Line'; else if (rest) rest = ' ' + rest;
  return COLOR_EN[m[1]] + rest;
}

function trName(v, depth) {
  if (lang === 'zh' || !v) return v;
  if (lang === 'en') {
    if (nameMap.all[v]) return nameMap.all[v];
    if (new RegExp('^' + ROUTE_TOKEN + '$').test(v)) return routeTokenEn(v);
  }
  if (CJK.test(v)) return trCore(v, (depth || 0) + 1);
  return v;
}

function fill(tpl, caps, depth) {
  if (tpl.indexOf('{') >= 0) {
    return tpl.replace(/\{(\d+)\}/g, (m, n) => {
      const c = caps[+n - 1];
      return c ? (c.t === 'n' ? trName(c.v, depth) : c.v) : '';
    });
  }
  let i = 0;
  return tpl.replace(/#/g, () => { const c = caps[i++]; return c ? (c.t === 'n' ? trName(c.v, depth) : c.v) : ''; });
}

const WEEK_EN = { 一: 'Mon', 二: 'Tue', 三: 'Wed', 四: 'Thu', 五: 'Fri', 六: 'Sat', 日: 'Sun' };
const WEEK_JA = { 一: '月', 二: '火', 三: '水', 四: '木', 五: '金', 六: '土', 日: '日' };
const WEEK_ORDER = '一二三四五六日';
const RULES = [
  { re: /^([一二三四五六日](?:、[一二三四五六日])+)$/, fn: m => {
    const ds = m[1].split('、');
    const consecutive = ds.length >= 3 && ds.every((d, i) => i === 0 || WEEK_ORDER.indexOf(d) === WEEK_ORDER.indexOf(ds[i - 1]) + 1);
    const W = lang === 'en' ? WEEK_EN : WEEK_JA;
    if (consecutive) return W[ds[0]] + '–' + W[ds[ds.length - 1]].replace(/^/, '');
    return ds.map(d => W[d]).join(lang === 'en' ? ', ' : '・');
  } },
  // UBike 建議（後端組好的三行文字；站名不在「」裡，所以用規則處理）
  { re: /^🚲 UBike 更快！實際騎車約 ([\d.]+) 分鐘（(.+)），比等公車\+搭車（約 ([\d.]+) 分鐘）更省時。$/, fn: m => {
    const d = trName(m[2], 1);
    return lang === 'en'
      ? `🚲 UBike is faster! Cycling takes about ${m[1]} min (${d}), vs. about ${m[3]} min waiting for and riding the bus.`
      : `🚲 UBikeの方が速い！自転車で約${m[1]}分（${d}）、バスの待ち＋乗車は約${m[3]}分です。`;
  } },
  { re: /^- 起點 UBike：(.+)（可借 (\d+) 輛）$/, fn: m => lang === 'en'
      ? `- Start UBike: ${trName(m[1], 1)} (${m[2]} bikes available)` : `- 出発地のUBike：${trName(m[1], 1)}（貸出可 ${m[2]}台）` },
  { re: /^- 終點 UBike：(.+)（可還 (\d+) 格）$/, fn: m => lang === 'en'
      ? `- End UBike: ${trName(m[1], 1)} (${m[2]} docks free)` : `- 目的地のUBike：${trName(m[1], 1)}（返却可 ${m[2]}枠）` },
  { re: /^(➡️ 去程|⬅️ 回程)　往 (.+)$/, fn: m => {
    const d = lang === 'en' ? (m[1].startsWith('➡️') ? '➡️ Outbound' : '⬅️ Return') : (m[1].startsWith('➡️') ? '➡️ 往路' : '⬅️ 復路');
    return d + ' → ' + trName(m[2].trim(), 1);
  } },
];
const PREFIX = [
  ['➡️ 往 ', '➡️ To ', '➡️ 行先：'], ['⬅️ 往 ', '⬅️ To ', '⬅️ 行先：'],
  ['🔀 往 ', '🔀 to ', '🔀 行先：'], ['前往 ', 'To ', '行先：'],
];
const SEP_RE = /(\n|[，、；：｜]|。(?=\s*\S)|・)/;
const SEP_OUT = {
  en: { '，': ', ', '、': ', ', '；': '; ', '：': ': ', '｜': ' | ', '。': '. ', '・': ' · ', '\n': '\n' },
  ja: { '，': '、', '、': '、', '；': '；', '：': '：', '｜': ' | ', '。': '。', '・': '・', '\n': '\n' },
};
let glossaryCache = null;
function glossary() {
  if (glossaryCache) return glossaryCache;
  glossaryCache = ROWS.filter(r => r[0].length >= 2 && CJK.test(r[0]) && !/[#@{]/.test(r[0]) && r[1] !== '')
    .sort((a, b) => b[0].length - a[0].length);
  return glossaryCache;
}

function trCore(core, depth) {
  depth = depth || 0;
  const idx = IDX[lang];
  if (!idx) return core;
  if (lang === 'en') {
    if (nameMap.all[core]) return nameMap.all[core];
    if (new RegExp('^' + ROUTE_TOKEN + '$').test(core)) return routeTokenEn(core);
  }
  const n = norm(core);
  const row = DICT.get(n.key);
  if (row && row[idx] !== undefined) return fill(row[idx], n.caps, depth);
  if (core.indexOf('\n') >= 0) {            // 多行文字：一行一行翻
    return core.split('\n').map(l => (l.trim() && CJK.test(l)) ? trCore(l.trim(), depth + 1) : l).join('\n');
  }
  for (const r of RULES) { const m = core.match(r.re); if (m) return r.fn(m); }
  if (depth > 3) return core;
  for (const p of PREFIX) {
    if (core.startsWith(p[0])) return p[idx] + trName(core.slice(p[0].length).trim(), depth + 1);
  }
  const parts = core.split(SEP_RE);
  if (parts.length > 1) {
    let changed = false;
    const out = parts.map((p, i) => {
      if (i % 2 === 1) return SEP_OUT[lang][p] !== undefined ? SEP_OUT[lang][p] : p;
      if (!p.trim() || !CJK.test(p)) return p;
      const lead = p.match(/^\s*/)[0], trail = p.match(/\s*$/)[0];
      const t = trCore(p.trim(), depth + 1);
      if (t !== p.trim()) changed = true;
      return lead + t + trail;
    }).join('');
    if (changed) return out;
  }
  let out = core, hit = false;
  for (const r of glossary()) {
    if (out.indexOf(r[0]) >= 0) { out = out.split(r[0]).join(r[idx]); hit = true; }
  }
  return hit ? out : core;
}

function tr(text) {
  if (lang === 'zh' || typeof text !== 'string' || !CJK.test(text)) return text;
  const lead = text.match(/^\s*/)[0], trail = text.match(/\s*$/)[0];
  const core = text.trim();
  if (!core) return text;
  return lead + trCore(core, 0) + trail;
}

if (typeof document === 'undefined') { module.exports = { tr, setLang: l => { lang = l; }, setNames: n => { nameMap.all = n; }, ROWS, norm, DICT }; return; }

// ── 畫面：把文字節點／屬性即時翻譯，並可隨時還原 ──────────────────────
const ATTRS = ['placeholder', 'title', 'aria-label', 'alt'];
const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'NOSCRIPT', 'CODE']);

function skipNode(node) {
  const el = node.nodeType === 1 ? node : node.parentElement;
  if (!el) return true;
  if (SKIP_TAGS.has(el.tagName)) return true;
  if (el.closest('[data-no-i18n]')) return true;
  // AI 對話內容（使用者問題／AI 回覆）是別人說的話，不翻；只翻「思考中...」這類系統提示
  const msg = el.closest('.chat-msg');
  if (msg && msg.id !== 'chat-thinking') return true;
  return false;
}

function fixText(node) {
  if (skipNode(node)) return;
  let orig = node.__orig;
  if (orig === undefined || node.data !== node.__out) { orig = node.data; node.__orig = orig; node.__out = undefined; }
  if (!CJK.test(orig)) { node.__orig = undefined; return; }
  const out = tr(orig);
  node.__out = out;
  if (node.data !== out) node.data = out;
}

function fixAttrs(el) {
  if (skipNode(el)) return;
  for (const a of ATTRS) {
    if (!el.hasAttribute || !el.hasAttribute(a)) continue;
    const store = el.__attr || (el.__attr = {});
    const cur = el.getAttribute(a);
    const rec = store[a];
    let orig;
    if (rec && rec.out === cur) orig = rec.orig; else orig = cur;
    if (!CJK.test(orig)) { delete store[a]; continue; }
    const out = tr(orig);
    store[a] = { orig, out };
    if (cur !== out) el.setAttribute(a, out);
  }
}

function scan(root) {
  if (!root) return;
  if (root.nodeType === 3) { fixText(root); return; }
  if (root.nodeType !== 1 && root.nodeType !== 9) return;
  const start = root.nodeType === 9 ? root.body : root;
  if (start.nodeType === 1) fixAttrs(start);
  const walker = document.createTreeWalker(start, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  let n;
  while ((n = walker.nextNode())) {
    if (n.nodeType === 3) fixText(n); else fixAttrs(n);
  }
}

let observer = null;
function startObserver() {
  if (observer) return;
  observer = new MutationObserver(muts => {
    if (lang === 'zh') return;
    for (const m of muts) {
      if (m.type === 'childList') m.addedNodes.forEach(scan);
      else if (m.type === 'characterData') fixText(m.target);
      else if (m.type === 'attributes') fixAttrs(m.target);
    }
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: ATTRS });
}

function restoreAll() {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  let n;
  while ((n = walker.nextNode())) {
    if (n.nodeType === 3) {
      if (n.__orig !== undefined && n.data === n.__out) { n.data = n.__orig; }
      n.__orig = undefined; n.__out = undefined;
    } else if (n.__attr) {
      for (const a of Object.keys(n.__attr)) {
        const rec = n.__attr[a];
        if (n.getAttribute(a) === rec.out) n.setAttribute(a, rec.orig);
      }
      n.__attr = undefined;
    }
  }
}

let nameMapPromise = null;
function loadNameMap() {
  if (nameMapPromise) return nameMapPromise;
  nameMapPromise = fetch('/api/name_map')
    .then(r => r.json())
    .then(d => { nameMap.all = Object.assign({}, d.routes || {}, d.stops || {}); })
    .catch(() => { nameMapPromise = null; });
  return nameMapPromise;
}

function applyLang() {
  const L = LANGS[lang];
  document.documentElement.lang = L.html;
  document.querySelectorAll('.lang-switch [data-lang]').forEach(b => b.classList.toggle('active', b.dataset.lang === lang));
  if (lang === 'zh') {
    restoreAll();
    if (document.title.__orig) document.title = document.title.__orig;
  } else {
    scan(document.body);
  }
  const t = document.querySelector('title');
  if (t) {
    if (t.__orig === undefined) t.__orig = t.textContent;
    t.textContent = lang === 'zh' ? t.__orig : tr(t.__orig);
  }
  document.dispatchEvent(new CustomEvent('i18n:change', { detail: { lang } }));
}

async function setLang(next) {
  if (!LANGS[next]) next = 'zh';
  lang = next;
  try { localStorage.setItem('lang', next); } catch (e) { /* noop */ }
  if (lang === 'en') await loadNameMap();   // 英文站名：TDX 本來就附的 En 欄位
  applyLang();
}

function detectLang() {
  try { const s = localStorage.getItem('lang'); if (LANGS[s]) return s; } catch (e) { /* noop */ }
  const nav = (navigator.language || 'zh-TW').toLowerCase();
  if (nav.startsWith('ja')) return 'ja';
  if (nav.startsWith('en')) return 'en';
  return 'zh';
}

window.I18N = {
  lang: () => lang,
  set: setLang,
  t: tr,                                   // 翻譯任意一段中文字串（給朗讀、通知這類不在畫面上的文字用）
  locale: () => LANGS[lang].locale,
  speech: () => LANGS[lang].speech,
  langs: LANGS,
};

function initI18n() {
  document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('.lang-switch [data-lang]');
    if (b) setLang(b.dataset.lang);
  });
  startObserver();
  setLang(detectLang());
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initI18n);
else initI18n();

})();
