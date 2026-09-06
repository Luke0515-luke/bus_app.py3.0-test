// ══════════════════════════════════════════════════════════
// 台南公車 AI 助理 — 前端邏輯
// ══════════════════════════════════════════════════════════

const ROUTE_COLOR_MAP = [
  ['黃', '#F1C40F'], ['棕', '#8B4513'], ['綠', '#27AE60'], ['橘', '#E67E22'],
  ['藍', '#2980B9'], ['紅', '#E74C3C'], ['H', '#9B59B6'],
  ['0', '#1ABC9C'],
  ['101', '#673AB7'], ['102', '#673AB7'], ['103', '#673AB7'], ['107', '#673AB7'],
  ['111', '#00BCD4'], ['168', '#00BCD4'],
  ['10', '#FF5722'], ['11', '#FF5722'], ['14', '#FF5722'], ['15', '#FF5722'],
  ['18', '#FF9800'], ['19', '#FF9800'], ['20', '#FF9800'], ['21', '#FF9800'],
  ['31', '#795548'], ['32', '#795548'], ['33', '#795548'],
  ['62', '#607D8B'], ['70', '#3F51B5'], ['77', '#009688'], ['98', '#F44336'],
  ['901', '#8BC34A'], ['902', '#8BC34A'], ['904', '#8BC34A'], ['905', '#8BC34A'],
  ['6', '#E91E63'], ['7', '#E91E63'], ['9', '#E91E63'],
  ['東山', '#FF6F00'], ['梅嶺', '#AD1457'], ['菱波', '#00838F'], ['雙層', '#BF360C'],
];
function getRouteColor(name) {
  for (const [prefix, color] of ROUTE_COLOR_MAP) {
    if (name.startsWith(prefix)) return color;
  }
  return '#7F8C8D';
}

const state = {
  fontLarge: false,
  currentPage: 'query',
  selectedFilters: new Set(),
  routeChoice: '',
  dirToggle: '去程',
  destNames: { 去程: '去程', 回程: '回程' },
  favorites: [],
  recent: [],
  mapInited: false,
  mapFilterRoutes: [],
  mapActiveRoutes: new Set(),
  mapShowAll: false,
  mapAllRoutes: [],
  mapBusData: [],
  mapShapeData: [],
  mapStopData: [],
  savedRoutes: [],
  reminders: [],
  reminderAlertedIds: new Set(),
  reminderPollTimer: null,
  routeStatusPollTimer: null,
  mapPollTimer: null,
  busListOpen: true,
  leafletMap: null,
  busLayer: null,
  shapeLayer: null,
  stopLayer: null,
  userLocationLayer: null,
  ttsText: '',
};

async function api(url, opts) {
  const res = await fetch(url, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* noop */ }
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { data });
  return data;
}
function el(id) { return document.getElementById(id); }
function esc(s) {
  return (s || '').toString().replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── 初始化 ────────────────────────────────────────────────
function applyResponsiveLayout() {
  // 有些瀏覽器環境（例如「要求電腦版網站」、特殊 WebView）算出來的版面寬度
  // 可能跟純 CSS media query 判斷的不一致，這裡額外用 JS 實際量測的寬度
  // 補一層保險，強制套用手機版排版，避免側欄／地圖清單直接整片顯示佔滿版面。
  document.body.classList.toggle('js-mobile', window.innerWidth <= 1024);
}
applyResponsiveLayout();
window.addEventListener('resize', applyResponsiveLayout);

// ── 日夜主題：依台南目前時間自動切換背景（白天用白、晚上用黑）──────────
function applyTimeTheme() {
  const hourStr = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Taipei', hour: 'numeric', hour12: false
  }).format(new Date());
  const hour = parseInt(hourStr, 10) % 24; // 部分瀏覽器午夜會回傳 "24"，取餘數修正成 0
  const isNight = hour >= 18 || hour < 6;
  document.body.classList.toggle('theme-night', isNight);
  document.body.classList.toggle('theme-day', !isNight);
}
applyTimeTheme();
setInterval(applyTimeTheme, 5 * 60 * 1000); // 每 5 分鐘重新檢查一次，日夜切換時不用整頁重新整理

document.addEventListener('DOMContentLoaded', () => {
  applyResponsiveLayout();
  document.body.classList.remove('sidebar-open'); // 確保每次載入頁面側欄都是收合狀態
  bindStaticEvents();
  loadFilterRoutes(null);
  loadFavorites();
  loadRecent();
  loadChatSessions();
  loadChatCurrent();
  loadAdvancedStops();
  loadAuthStatus();
  loadReminders();
  loadHomeWeather();
});

async function loadHomeWeather() {
  try {
    const data = await api('/api/weather');
    if (data.weather) {
      el('home-weather-text').textContent = data.weather;
      el('home-weather-chip').classList.remove('hidden');
    }
  } catch (e) { /* 首頁天氣讀不到就不顯示，不影響其他功能 */ }
}

function bindStaticEvents() {
  el('btn-font-toggle').addEventListener('click', toggleFont);
  el('btn-page-toggle').addEventListener('click', togglePage);
  el('btn-map-home').addEventListener('click', () => switchPage('query'));
  el('btn-back-home').addEventListener('click', showHome);

  el('yellow-bus-select').addEventListener('change', () => {
    const route = el('yellow-bus-select').value;
    if (route) selectRouteByName(route);
  });
  el('btn-show-timetable').addEventListener('click', () => {
    const box = el('timetable-box');
    if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
    if (state.routeChoice) loadTimetable(state.routeChoice);
  });

  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const f = btn.dataset.f;
      // 多選：點一下切換這個篩選條件的選取狀態，其他已選的條件維持不變
      if (state.selectedFilters.has(f)) {
        state.selectedFilters.delete(f);
        btn.classList.remove('active');
      } else {
        state.selectedFilters.add(f);
        btn.classList.add('active');
      }
      loadFilterRoutes([...state.selectedFilters]);
    });
  });
  el('btn-clear-filter').addEventListener('click', () => {
    state.selectedFilters.clear();
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    loadFilterRoutes([]);
  });

  el('route-select').addEventListener('change', onRouteSelect);
  el('btn-fav-toggle').addEventListener('click', toggleFavoriteCurrent);
  el('start-select').addEventListener('change', () => loadRouteStatus());
  el('end-select').addEventListener('change', () => loadRouteStatus());
  el('btn-dir0').addEventListener('click', () => setDirection('去程'));
  el('btn-dir1').addEventListener('click', () => setDirection('回程'));
  el('btn-refresh-status').addEventListener('click', () => {
    loadRouteStatus();
    startRouteStatusAutoRefresh();
  });

  el('btn-gps').addEventListener('click', gpsLocate);
  el('btn-search-nearby').addEventListener('click', searchNearby);

  el('btn-chat-history-toggle').addEventListener('click', () => {
    el('chat-history-panel').classList.toggle('hidden');
  });
  el('btn-new-chat').addEventListener('click', async () => {
    await api('/api/chat/sessions/new', { method: 'POST' });
    await loadChatSessions();
    await loadChatCurrent();
  });

  el('btn-adv-search').addEventListener('click', advancedSearch);

  el('btn-update-cache').addEventListener('click', updateCache);

  el('btn-login').addEventListener('click', () => submitAuth('/api/auth/login'));
  el('btn-register').addEventListener('click', () => submitAuth('/api/auth/register'));
  el('btn-logout').addEventListener('click', logoutAccount);
  el('auth-password').addEventListener('keydown', e => { if (e.key === 'Enter') submitAuth('/api/auth/login'); });

  el('btn-map-locate').addEventListener('click', locateMeOnMap);

  el('btn-add-reminder').addEventListener('click', addReminder);

  el('btn-chat-send').addEventListener('click', sendChat);
  el('chat-input').addEventListener('keydown', e => { if (e.key === 'Enter') sendChat(); });

  el('btn-tts-speak').addEventListener('click', () => {
    if (!state.ttsText) return;
    const u = new SpeechSynthesisUtterance(state.ttsText);
    u.lang = 'zh-TW'; u.rate = 0.9;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  });
  el('btn-tts-stop').addEventListener('click', () => window.speechSynthesis.cancel());

  el('btn-map-refresh').addEventListener('click', () => loadMapData(true));
  el('map-route-input').addEventListener('keydown', e => { if (e.key === 'Enter') loadMapData(true); });
  el('adv-stop-search').addEventListener('input', e => renderAdvStopOptions(e.target.value));
  el('btn-toggle-map-panel').addEventListener('click', () => {
    const panel = el('map-panel');
    const btn = el('btn-toggle-map-panel');
    const expanded = panel.classList.toggle('expanded');
    btn.textContent = expanded ? '✕ 收合設定' : '☰ 更多設定';
  });
  el('btn-toggle-bus-list').addEventListener('click', () => {
    state.busListOpen = !state.busListOpen;
    renderMapBusList();
  });
  el('map-search-box').addEventListener('input', e => renderMapPanel(e.target.value));
  el('btn-save-route-coords').addEventListener('click', saveRouteCoords);

  el('btn-mobile-menu').addEventListener('click', () => {
    document.body.classList.toggle('sidebar-open');
  });
  el('sidebar-backdrop').addEventListener('click', () => {
    document.body.classList.remove('sidebar-open');
  });

  document.querySelectorAll('.home-tile').forEach(tile => {
    tile.addEventListener('click', () => handleHomeTile(tile.dataset.action));
  });
}

// ── 主頁圖示／子頁面導覽 ─────────────────────────────────────
function openSidebar() {
  document.body.classList.add('sidebar-open');
}

function showHome() {
  el('mobile-home-grid').classList.remove('hidden');
  el('subpage-container').classList.add('hidden');
  document.querySelectorAll('.subpage').forEach(s => s.classList.add('hidden'));
  el('yellow-bus-picker').classList.add('hidden');
  stopRouteStatusAutoRefresh();
}

// 公車即時定位／到站動態現在改成後台每分鐘統一向 TDX 抓一次、存進檔案，
// 所有使用者的查詢都直接讀這份檔案（見後端 fetch_bus_data／
// fetch_bus_realtime_positions），所以前端可以放心地每 15 秒自動重新整理一次
// 畫面，不會因此增加對 TDX 的查詢量——15 秒只是「更常去讀後台已經準備好的
// 那份檔案」，不是「更常去問 TDX」。
const AUTO_REFRESH_SECONDS = 15;

function startRouteStatusAutoRefresh() {
  stopRouteStatusAutoRefresh();
  let secondsLeft = AUTO_REFRESH_SECONDS;
  const countdownEl = el('route-refresh-countdown');
  if (countdownEl) countdownEl.textContent = `⏱️ ${secondsLeft}s 後自動更新`;
  state.routeStatusPollTimer = setInterval(() => {
    secondsLeft -= 1;
    if (secondsLeft <= 0) {
      if (state.routeChoice) loadRouteStatus();
      secondsLeft = AUTO_REFRESH_SECONDS;
    }
    if (countdownEl) countdownEl.textContent = `⏱️ ${secondsLeft}s 後自動更新`;
  }, 1000);
}
function stopRouteStatusAutoRefresh() {
  if (state.routeStatusPollTimer) {
    clearInterval(state.routeStatusPollTimer);
    state.routeStatusPollTimer = null;
  }
  const countdownEl = el('route-refresh-countdown');
  if (countdownEl) countdownEl.textContent = '';
}
function startMapAutoRefresh() {
  stopMapAutoRefresh();
  let secondsLeft = AUTO_REFRESH_SECONDS;
  const countdownEl = el('map-refresh-countdown');
  if (countdownEl) countdownEl.textContent = `⏱️ ${secondsLeft}s 後自動更新`;
  state.mapPollTimer = setInterval(() => {
    secondsLeft -= 1;
    if (secondsLeft <= 0) {
      loadMapData();
      secondsLeft = AUTO_REFRESH_SECONDS;
    }
    if (countdownEl) countdownEl.textContent = `⏱️ ${secondsLeft}s 後自動更新`;
  }, 1000);
}
function stopMapAutoRefresh() {
  if (state.mapPollTimer) {
    clearInterval(state.mapPollTimer);
    state.mapPollTimer = null;
  }
  const countdownEl = el('map-refresh-countdown');
  if (countdownEl) countdownEl.textContent = '';
}

function showSubpage(id, anchorId) {
  el('mobile-home-grid').classList.add('hidden');
  el('subpage-container').classList.remove('hidden');
  document.querySelectorAll('.subpage').forEach(s => s.classList.toggle('hidden', s.id !== id));
  window.scrollTo({ top: 0, behavior: 'auto' });
  if (anchorId) {
    requestAnimationFrame(() => {
      const target = el(anchorId);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
}

async function handleHomeTile(action) {
  switch (action) {
    case 'map':
      switchPage('map');
      break;
    case 'filter':
      el('yellow-bus-picker').classList.add('hidden');
      showSubpage('subpage-route', 'filter-anchor');
      startRouteStatusAutoRefresh();
      break;
    case 'nearby':
      el('yellow-bus-picker').classList.add('hidden');
      showSubpage('subpage-nearby', 'nearby-anchor');
      stopRouteStatusAutoRefresh();
      break;
    case 'chat':
      showSubpage('subpage-chat', 'chat-anchor');
      stopRouteStatusAutoRefresh();
      break;
    case 'yellow-bus':
      showSubpage('subpage-route', 'yellow-bus-anchor');
      el('yellow-bus-picker').classList.remove('hidden');
      loadYellowBusRoutes();
      stopRouteStatusAutoRefresh();
      break;
    case 'favorites':
      openSidebar();
      el('favorites-section').classList.remove('hidden');
      break;
    case 'advsearch':
      openSidebar();
      el('details-advsearch').setAttribute('open', '');
      requestAnimationFrame(() => el('details-advsearch').scrollIntoView({ behavior: 'smooth', block: 'start' }));
      break;
    case 'more':
      openSidebar();
      break;
  }
}

// ── 小黃公車 ─────────────────────────────────────────────
let _yellowBusLoaded = false;
async function loadYellowBusRoutes() {
  if (_yellowBusLoaded) return;
  const sel = el('yellow-bus-select');
  const statusBox = el('yellow-bus-status');
  statusBox.textContent = '路線清單載入中...';
  try {
    const data = await api('/api/yellow_bus_routes');
    sel.innerHTML = '<option value="">請選擇路線...</option>' +
      data.routes.map(r => `<option value="${esc(r.route_name)}">${esc(r.route_name)}</option>`).join('');
    statusBox.textContent = `共 ${data.total} 條小黃公車路線（資料來自 TDX，依營運業者自動判斷）`;
    _yellowBusLoaded = true;
  } catch (e) {
    statusBox.textContent = `❌ 路線清單載入失敗：${e.message}`;
  }
}

// ── 固定時刻表 ────────────────────────────────────────────
async function loadTimetable(route) {
  const box = el('timetable-box');
  box.classList.remove('hidden');
  box.innerHTML = '<div class="caption">時刻表載入中...</div>';
  try {
    const data = await api(`/api/timetable?route=${encodeURIComponent(route)}`);
    if (!data.has_data) {
      box.innerHTML = `<div class="warning-box">${esc(data.message || '查無這條路線的固定時刻表')}</div>`;
      return;
    }
    box.innerHTML = data.directions.map(d => `
      <div class="timetable-direction">
        <div class="timetable-dir-title">${d.direction === 0 ? '➡️ 去程' : '⬅️ 回程'}${d.destination ? '　往 ' + esc(d.destination) : ''}</div>
        ${d.groups.map(g => `
          <div class="timetable-group">
            <div class="timetable-days">${esc(g.days)}</div>
            <div class="timetable-times">${g.times.map(t => `<span class="timetable-time">${esc(t)}</span>`).join('')}</div>
          </div>`).join('')}
      </div>`).join('') || '<div class="info-box">這條路線目前沒有公告固定時刻表</div>';
  } catch (e) {
    box.innerHTML = `<div class="error-box">❌ 時刻表載入失敗：${esc(e.message)}</div>`;
  }
}

// ── 字體 / 頁面切換 ─────────────────────────────────────────
function toggleFont() {
  state.fontLarge = !state.fontLarge;
  document.body.classList.toggle('font-large', state.fontLarge);
  el('btn-font-toggle').textContent = state.fontLarge ? '🔡 縮小字體' : '🔠 放大字體（視障輔助）';
}

function togglePage() {
  switchPage(state.currentPage === 'map' ? 'query' : 'map');
}
function switchPage(page) {
  state.currentPage = page;
  el('page-query').classList.toggle('hidden', page !== 'query');
  el('page-map').classList.toggle('hidden', page !== 'map');
  el('btn-page-toggle').textContent = page === 'map' ? '🚌 回到查詢頁面' : '🗺️ 公車即時地圖';
  document.body.classList.remove('sidebar-open');
  if (page === 'map') {
    initMapPageIfNeeded();
  } else {
    stopMapAutoRefresh();
  }
}

// ── 路線篩選 / 選擇 ───────────────────────────────────────
async function loadFilterRoutes(filterVals) {
  const list = Array.isArray(filterVals) ? filterVals : (filterVals ? [filterVals] : []);
  const url = list.length ? `/api/filter_routes?filter=${encodeURIComponent(list.join(','))}` : '/api/filter_routes';
  const data = await api(url);
  const sel = el('route-select');
  const prev = sel.value;
  sel.innerHTML = '<option value="">請選擇或輸入路線...</option>' +
    data.routes.map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join('');
  if (data.routes.includes(prev)) sel.value = prev;
  el('filter-status').textContent = list.length
    ? `篩選：【${list.join('、')}】（共 ${data.routes.length} 條）`
    : '顯示：全部路線';

  // 直接把篩選結果列成一排可點的路線標籤，不用打開下拉選單也看得出來真的有篩選、篩到哪些路線
  const chipsBox = el('filter-route-chips');
  if (!list.length) {
    chipsBox.innerHTML = '';
  } else if (!data.routes.length) {
    chipsBox.innerHTML = '<div class="warning-box">這個篩選條件下沒有符合的路線</div>';
  } else {
    chipsBox.innerHTML = data.routes.map(r =>
      `<button type="button" class="route-chip" data-route="${esc(r)}" style="background:${getRouteColor(r)}">${esc(r)}</button>`
    ).join('');
    chipsBox.querySelectorAll('.route-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        sel.value = chip.dataset.route;
        onRouteSelect();
      });
    });
  }
}

async function onRouteSelect() {
  const route = el('route-select').value;
  state.routeChoice = route;
  el('stop-select-body').classList.add('hidden');
  el('reminder-add-box').classList.add('hidden');
  el('status-box').classList.add('hidden');
  el('weather-box').classList.add('hidden');
  el('status-empty').classList.remove('hidden');
  el('timetable-box').classList.add('hidden');
  el('timetable-box').innerHTML = '';

  if (!route) {
    el('btn-fav-toggle').classList.add('hidden');
    el('btn-show-timetable').classList.add('hidden');
    el('route-hint').textContent = '請選擇路線';
    el('route-hint').classList.remove('hidden');
    return;
  }
  el('route-hint').classList.add('hidden');
  el('btn-fav-toggle').classList.remove('hidden');
  el('btn-show-timetable').classList.remove('hidden');
  el('btn-show-timetable').textContent = '📅 查看固定時刻表';
  refreshFavToggleLabel();

  const data = await api(`/api/route_stops?route=${encodeURIComponent(route)}&direction=${encodeURIComponent(state.dirToggle)}`);
  loadRecent();
  if (!data.stops || data.stops.length === 0) {
    el('route-hint').textContent = `⚠️ 無法載入【${route}】站點。`;
    el('route-hint').className = 'warning-box';
    el('route-hint').classList.remove('hidden');
    return;
  }
  const startSel = el('start-select');
  const endSel = el('end-select');
  startSel.innerHTML = data.stops.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  endSel.innerHTML = data.stops.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  startSel.selectedIndex = 0;
  endSel.selectedIndex = data.stops.length - 1;
  el('stop-select-body').classList.remove('hidden');
  el('reminder-add-box').classList.remove('hidden');

  await loadRouteStatus();
  startRouteStatusAutoRefresh();
}

function refreshFavToggleLabel() {
  const isFav = state.favorites.includes(state.routeChoice);
  el('btn-fav-toggle').textContent = isFav ? '⭐ 已加入最愛' : '☆ 加入最愛';
}
async function toggleFavoriteCurrent() {
  if (!state.routeChoice) return;
  const data = await api('/api/favorites/toggle', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ route: state.routeChoice })
  });
  state.favorites = data.favorites;
  refreshFavToggleLabel();
  renderFavorites();
}

function setDirection(dir) {
  state.dirToggle = dir;
  el('btn-dir0').classList.toggle('active', dir === '去程');
  el('btn-dir1').classList.toggle('active', dir === '回程');
  reloadStopSelectorsForDirection();
}

async function reloadStopSelectorsForDirection() {
  // 去程／回程的完整站序清單常常不一樣（單行道、繞道路段），切換方向時要重新載入
  // 等候站／目的地下拉選單，不然選單裡還是舊方向的站名，跟即時動態兜不起來，
  // 容易讓「等候站」高亮跟到站時間對不上，甚至查出一堆看起來不合理的空白結果。
  if (!state.routeChoice) return;
  const prevStart = el('start-select').value;
  const prevEnd = el('end-select').value;
  const data = await api(`/api/route_stops?route=${encodeURIComponent(state.routeChoice)}&direction=${encodeURIComponent(state.dirToggle)}`);
  if (data.stops && data.stops.length) {
    const startSel = el('start-select');
    const endSel = el('end-select');
    startSel.innerHTML = data.stops.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
    endSel.innerHTML = data.stops.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
    startSel.value = data.stops.includes(prevStart) ? prevStart : data.stops[0];
    endSel.value = data.stops.includes(prevEnd) ? prevEnd : data.stops[data.stops.length - 1];
  }
  loadRouteStatus();
}

async function loadRouteStatus() {
  if (!state.routeChoice) return;
  const startSt = el('start-select').value;
  const endSt = el('end-select').value;
  let data;
  try {
    const params = new URLSearchParams({
      route: state.routeChoice, direction: state.dirToggle,
      start_st: startSt || '', end_st: endSt || ''
    });
    data = await api(`/api/route_status?${params.toString()}`);
  } catch (e) {
    el('status-box').classList.add('hidden');
    el('status-empty').textContent = '無法取得即時動態。';
    el('status-empty').className = 'error-box';
    el('status-empty').classList.remove('hidden');
    return;
  }

  el('status-empty').classList.add('hidden');
  el('weather-box').textContent = `🌡️ 台南目前天氣：${data.weather}`;
  el('weather-box').classList.remove('hidden');
  el('realtime-stale-hint').classList.toggle('hidden', data.data_fresh !== false);

  state.destNames = { 去程: data.dest0, 回程: data.dest1 };
  const busCountText = typeof data.active_bus_count === 'number'
    ? `（GPS 定位目前共 ${data.active_bus_count} 台營運中）` : '';
  el('status-title').textContent = `🚌 ${state.routeChoice} 全線即時動態${busCountText}`;
  el('btn-dir0').textContent = `➡️ 往 ${data.dest0}`;
  el('btn-dir1').textContent = `⬅️ 往 ${data.dest1}`;
  el('btn-dir0').classList.toggle('active', state.dirToggle === '去程');
  el('btn-dir1').classList.toggle('active', state.dirToggle === '回程');

  const ubBox = el('ubike-suggestion');
  if (data.ubike_suggestion) {
    ubBox.textContent = data.ubike_suggestion;
    ubBox.classList.remove('hidden');
  } else {
    ubBox.classList.add('hidden');
  }

  const container = el('timeline-container');
  container.innerHTML = data.stops.map(s => {
    let busHtml = '';
    if (s.has_bus) {
      const wc = s.is_low
        ? '<span class="wheelchair-tag">♿ 無障礙</span>'
        : '<span class="no-wheelchair-tag">🚌 一般車</span>';
      const ev = s.is_ev ? '<span class="ev-tag">⚡ 電動</span>' : '';
      busHtml = `<span class="bus-tag">🚌 ${esc(s.plate)} (${esc(s.car_size)})</span>${wc}${ev}`;
    }
    // 繞道／支線公車：這一班實際開往的目的地跟路線平常公告的方向不一樣時特別標示出來
    const branchHtml = s.branch ? `<span class="branch-tag">🔀 往 ${esc(s.branch)}</span>` : '';
    const ubikeHtml = (s.ubikes || []).map(u =>
      `<span class="ubike-tag">🚲 可借:${u.available} 可還:${u.empty}</span>`).join('');
    return `
<div class="timeline-item ${s.is_waiting_stop ? 'waiting-stop' : ''}">
  <div class="timeline-circle"></div>
  <div class="station-box">
    <div class="station-info">
      <div class="station-info-top">
        <span class="station-name">${esc(s.name)}</span>
        ${busHtml}${branchHtml}
      </div>
      ${ubikeHtml ? `<div class="station-info-ubike">${ubikeHtml}</div>` : ''}
    </div>
    <span class="time-badge ${s.badge_class}">${esc(s.eta_text)}</span>
  </div>
</div>`;
  }).join('');

  state.ttsText = data.tts_text || '';
  el('status-box').classList.remove('hidden');
}

// ── GPS 附近站牌 ─────────────────────────────────────────
function gpsLocate() {
  if (!navigator.geolocation) { alert('瀏覽器不支援定位'); return; }
  navigator.geolocation.getCurrentPosition(pos => {
    el('lat-disp').value = pos.coords.latitude.toFixed(6);
    el('lon-disp').value = pos.coords.longitude.toFixed(6);
    el('gps-lat-in').value = pos.coords.latitude.toFixed(6);
    el('gps-lon-in').value = pos.coords.longitude.toFixed(6);
  }, () => alert('請允許瀏覽器定位權限'));
}

async function searchNearby() {
  const lat = parseFloat(el('gps-lat-in').value);
  const lon = parseFloat(el('gps-lon-in').value);
  const box = el('nearby-result');
  if (isNaN(lat) || isNaN(lon)) {
    box.innerHTML = '<div class="error-box">請輸入有效數字</div>';
    return;
  }
  box.innerHTML = '<div class="caption">搜尋中...</div>';
  try {
    const data = await api(`/api/nearby_stops?lat=${lat}&lon=${lon}`);
    if (!data.nearby || data.nearby.length === 0) {
      box.innerHTML = '<div class="warning-box">附近 500m 內無公車站牌</div>';
      return;
    }
    box.innerHTML = `<div class="caption">找到 ${data.nearby.length} 個站牌（500m內）：</div>` +
      data.nearby.map(n => `<div class="stop-item">🚏 <b>${esc(n.name)}</b>（${Math.round(n.dist * 1000)}m）</div>`).join('');
  } catch (e) {
    box.innerHTML = '<div class="error-box">無法載入站牌資料</div>';
  }
}

// ── 最愛 / 最近查詢 ──────────────────────────────────────
async function loadFavorites() {
  const data = await api('/api/favorites');
  state.favorites = data.favorites;
  renderFavorites();
}
function renderFavorites() {
  const sec = el('favorites-section');
  const list = el('favorites-list');
  if (!state.favorites.length) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  list.innerHTML = state.favorites.map(fav => `
    <div class="fav-row">
      <button class="btn fav-main" data-r="${esc(fav)}">🚌 ${esc(fav)}</button>
      <button class="btn fav-remove" data-r="${esc(fav)}">✕</button>
    </div>`).join('');
  list.querySelectorAll('.fav-main').forEach(b => b.addEventListener('click', () => selectRouteByName(b.dataset.r)));
  list.querySelectorAll('.fav-remove').forEach(b => b.addEventListener('click', async () => {
    const data = await api('/api/favorites/toggle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ route: b.dataset.r })
    });
    state.favorites = data.favorites;
    renderFavorites();
    if (state.routeChoice === b.dataset.r) refreshFavToggleLabel();
  }));
  refreshFavToggleLabel();
}

async function loadRecent() {
  const data = await api('/api/recent');
  state.recent = data.recent;
  renderRecent();
}
function renderRecent() {
  const sec = el('recent-section');
  const list = el('recent-list');
  if (!state.recent.length) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  list.innerHTML = state.recent.map(r => `<button class="btn" data-r="${esc(r)}">🔁 ${esc(r)}</button>`).join('');
  list.querySelectorAll('button').forEach(b => b.addEventListener('click', () => selectRouteByName(b.dataset.r)));
}

async function selectRouteByName(route) {
  document.body.classList.remove('sidebar-open');
  state.selectedFilters.clear();
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  await loadFilterRoutes(null);
  const sel = el('route-select');
  if (![...sel.options].some(o => o.value === route)) {
    sel.insertAdjacentHTML('beforeend', `<option value="${esc(route)}">${esc(route)}</option>`);
  }
  sel.value = route;
  await onRouteSelect();
}

// ── 進階查詢（站到站） ────────────────────────────────────
let advStopsAll = [];
async function loadAdvancedStops() {
  const data = await api('/api/advanced_search/stops');
  advStopsAll = data.stops;
  renderAdvStopOptions('');
}
function renderAdvStopOptions(keyword) {
  // adv-start / adv-end 現在是「打字自動篩選」的輸入框（搭配 datalist），
  // 使用者直接在欄位裡打字就會即時篩選建議清單；上面這個 adv-stop-search
  // 篩選框則是原本就有的功能，保留下來，可以同時預先縮小兩個欄位的建議清單。
  const kw = keyword.trim();
  const matched = kw ? advStopsAll.filter(s => s.includes(kw)) : advStopsAll;
  const opts = matched.map(s => `<option value="${esc(s)}"></option>`).join('');
  ['adv-start-list', 'adv-end-list'].forEach(id => {
    const dl = el(id);
    if (dl) dl.innerHTML = opts;
  });
}
async function advancedSearch() {
  const start = el('adv-start').value.trim();
  const end = el('adv-end').value.trim();
  const box = el('adv-search-result');
  box.innerHTML = '';
  if (!start || !end) { box.innerHTML = '<div class="error-box">請選擇出發站和目的站</div>'; return; }
  if (start === end) { box.innerHTML = '<div class="error-box">出發站和目的站不能相同</div>'; return; }
  box.innerHTML = '<div class="caption">搜尋中...</div>';
  try {
    const data = await api(`/api/advanced_search?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
    let html = '';
    if (data.directs && data.directs.length) {
      html += `<div class="success-box">✅ 直達路線（共 ${data.directs.length} 條）</div>`;
      html += data.directs.map(r => `<div class="route-item"><button class="btn adv-go" data-r="${esc(r)}">🚌 ${esc(r)}</button></div>`).join('');
    } else {
      html += '<div class="info-box">無直達路線</div>';
    }
    if (data.transfers && data.transfers.length) {
      html += `<div class="warning-box">🔄 轉乘一次方案（共 ${data.transfers.length} 個）</div>`;
      html += data.transfers.map(t => `<div class="route-item">搭 <b>${esc(t.routeA)}</b> → 在 <b>${esc(t.transfer)}</b> 轉 <b>${esc(t.routeB)}</b></div>`).join('');
    } else {
      html += '<div class="error-box">找不到一次轉乘方案，請考慮其他方式</div>';
    }
    box.innerHTML = html;
    box.querySelectorAll('.adv-go').forEach(b => b.addEventListener('click', () => selectRouteByName(b.dataset.r)));
  } catch (e) {
    box.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}

// ── 帳號登入 ─────────────────────────────────────────────
async function loadAuthStatus() {
  try {
    const data = await api('/api/auth/status');
    renderAuthState(data.username || null);
  } catch (e) {
    renderAuthState(null);
  }
}
function renderAuthState(username) {
  const loggedOut = el('account-logged-out');
  const loggedIn = el('account-logged-in');
  if (username) {
    loggedOut.classList.add('hidden');
    loggedIn.classList.remove('hidden');
    el('account-username').textContent = username;
  } else {
    loggedOut.classList.remove('hidden');
    loggedIn.classList.add('hidden');
  }
}
async function submitAuth(url) {
  const username = el('auth-username').value.trim();
  const password = el('auth-password').value;
  const statusBox = el('auth-status');
  if (!username || !password) {
    statusBox.innerHTML = '<div class="error-box">請輸入帳號與密碼</div>';
    return;
  }
  statusBox.innerHTML = '<div class="caption">處理中...</div>';
  try {
    const data = await api(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    statusBox.innerHTML = '';
    el('auth-password').value = '';
    renderAuthState(data.username);
    // 登入／註冊後身分變了，最愛路線、最近查詢、AI 對話記錄都要重新載入成這個帳號的資料
    await Promise.all([loadFavorites(), loadRecent(), loadChatSessions(), loadChatCurrent(), loadReminders()]);
  } catch (e) {
    statusBox.innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}
async function logoutAccount() {
  await api('/api/auth/logout', { method: 'POST' });
  renderAuthState(null);
  await Promise.all([loadFavorites(), loadRecent(), loadChatSessions(), loadChatCurrent(), loadReminders()]);
}

// ── 到站鈴聲提醒 ───────────────────────────────────────────
// 播提示音用同一顆 AudioContext 就好，不要每次響鈴都 new 一個：
// 瀏覽器的自動播放限制大多只卡在「第一次要有使用者手動操作過」，
// 所以在第一次點擊頁面時就先建立好、resume 起來，之後背景計時器
// 觸發提醒時才能順利發出聲音，不會被靜音擋掉。
let sharedAudioCtx = null;
function unlockReminderAudio() {
  if (!sharedAudioCtx) {
    try { sharedAudioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { return; }
  }
  if (sharedAudioCtx.state === 'suspended') sharedAudioCtx.resume();
}
document.addEventListener('click', unlockReminderAudio, { once: true });

function playReminderBell() {
  try {
    if (!sharedAudioCtx) sharedAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (sharedAudioCtx.state === 'suspended') sharedAudioCtx.resume();
    const ctx = sharedAudioCtx;
    const now = ctx.currentTime;
    // 「叮－咚」兩個音，連響三次，聽起來像到站鈴聲，不需要額外的音效檔案
    [0, 0.9, 1.8].forEach(offset => {
      [[880, offset], [660, offset + 0.28]].forEach(([freq, t]) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0, now + t);
        gain.gain.linearRampToValueAtTime(0.35, now + t + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, now + t + 0.25);
        osc.connect(gain).connect(ctx.destination);
        osc.start(now + t);
        osc.stop(now + t + 0.3);
      });
    });
  } catch (e) { /* 瀏覽器不支援音效就算了，畫面上的橫幅通知還是會照常顯示 */ }
}

let reminderBannerTimeout = null;
function showReminderBanner(msg) {
  const b = el('reminder-banner');
  if (!b) return;
  b.textContent = msg;
  b.classList.remove('hidden');
  clearTimeout(reminderBannerTimeout);
  reminderBannerTimeout = setTimeout(() => b.classList.add('hidden'), 10000);
}

// 把「5 分鐘」「約 44 分鐘（時刻表估計）」這類到站文字轉成數字分鐘；
// 「即將進站」「進站中」視為 0 分鐘；「尚未發車」等完全沒有時間資訊的狀態回傳 null。
function parseEtaMinutes(text) {
  if (!text) return null;
  if (text.includes('即將進站') || text.includes('進站中')) return 0;
  const m = text.match(/(\d+)\s*分鐘/);
  if (m) return parseInt(m[1], 10);
  return null;
}

async function addReminder() {
  const route = state.routeChoice;
  const direction = state.dirToggle;
  const stop = el('start-select').value;
  const minutes = el('reminder-minutes').value;
  if (!route || !stop) return;
  unlockReminderAudio();
  if (window.Notification && Notification.permission === 'default') {
    Notification.requestPermission();
  }
  try {
    const data = await api('/api/reminders/add', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ route, direction, stop, alert_minutes: minutes })
    });
    if (data.error) {
      showReminderBanner(`⚠️ ${data.error}`);
      return;
    }
    state.reminders = data.reminders;
    renderReminders();
    ensureReminderPolling();
    showReminderBanner(`🔔 已設定：${route}（往${direction}）到站前 ${minutes} 分鐘提醒`);
  } catch (e) { /* 忽略單次失敗，使用者可以再按一次 */ }
}

function ensureReminderPolling() {
  if (state.reminders.length && !state.reminderPollTimer) {
    state.reminderPollTimer = setInterval(checkReminders, 20000);
    checkReminders();
  } else if (!state.reminders.length && state.reminderPollTimer) {
    clearInterval(state.reminderPollTimer);
    state.reminderPollTimer = null;
  }
}

async function loadReminders() {
  try {
    const data = await api('/api/reminders');
    state.reminders = data.reminders || [];
    renderReminders();
    ensureReminderPolling();
  } catch (e) { /* 忽略，稍後其他操作觸發時還會再拉一次 */ }
}

function renderReminders() {
  const section = el('reminders-section');
  const list = el('reminders-list');
  if (!state.reminders.length) {
    section.classList.add('hidden');
    list.innerHTML = '';
  } else {
    section.classList.remove('hidden');
    list.innerHTML = state.reminders.map(r => `
      <div class="stop-item reminder-item">
        <div>
          <b>${esc(r.route)}</b>（往${esc(r.direction)}）－ ${esc(r.stop)}<br>
          <span class="caption">到站前 ${r.alert_minutes} 分鐘提醒</span>
        </div>
        <button class="btn reminder-delete-btn" data-id="${esc(r.id)}">🗑️</button>
      </div>`).join('');
    list.querySelectorAll('.reminder-delete-btn').forEach(b => {
      b.addEventListener('click', async () => {
        const id = b.dataset.id;
        try {
          await api('/api/reminders/delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
          });
        } catch (e) { /* 忽略 */ }
        state.reminderAlertedIds.delete(id);
        state.reminders = state.reminders.filter(r => r.id !== id);
        renderReminders();
        ensureReminderPolling();
      });
    });
  }

  const homeChip = el('home-reminder-chip');
  if (!state.reminders.length) {
    homeChip.classList.add('hidden');
  } else if (state.reminders.length === 1) {
    const r = state.reminders[0];
    el('home-reminder-text').textContent = `${r.route}（往${r.direction}）－ ${r.stop}`;
    homeChip.classList.remove('hidden');
  } else {
    el('home-reminder-text').textContent = `目前有 ${state.reminders.length} 個提醒進行中`;
    homeChip.classList.remove('hidden');
  }
}

async function checkReminders() {
  if (!state.reminders.length) return;
  if (!el('reminder-sound-enabled').checked) return;
  // 同一條路線＋方向如果被好幾個提醒共用，合併成一次查詢就好，不用每個提醒各查一次
  const groups = {};
  state.reminders.forEach(r => {
    const key = `${r.route}|${r.direction}`;
    (groups[key] = groups[key] || []).push(r);
  });
  for (const key of Object.keys(groups)) {
    const [route, direction] = key.split('|');
    try {
      const params = new URLSearchParams({ route, direction });
      const data = await api(`/api/route_status?${params.toString()}`);
      if (!data.stops) continue;
      groups[key].forEach(r => {
        const stopInfo = data.stops.find(s => s.name === r.stop);
        if (!stopInfo) return;
        const mins = parseEtaMinutes(stopInfo.eta_text);
        if (mins !== null && mins <= r.alert_minutes) {
          if (!state.reminderAlertedIds.has(r.id)) {
            state.reminderAlertedIds.add(r.id);
            playReminderBell();
            const msg = `🔔 ${r.route}（往${r.direction}）即將抵達「${r.stop}」：${stopInfo.eta_text}`;
            showReminderBanner(msg);
            if (window.Notification && Notification.permission === 'granted') {
              new Notification('公車到站提醒', { body: msg });
            }
          }
        } else {
          // 車還沒到門檻範圍內（或這班已經過站、換下一班了），解除已提醒標記，
          // 下一班車再進入提醒範圍時才能重新響鈴，不會被卡住只提醒一次。
          state.reminderAlertedIds.delete(r.id);
        }
      });
    } catch (e) { /* 這次查詢失敗就跳過，20 秒後下個週期會重試 */ }
  }
}

// ── 系統維護 ─────────────────────────────────────────────
async function updateCache() {
  const box = el('cache-status');
  const btn = el('btn-update-cache');
  btn.disabled = true;
  box.innerHTML = '<div class="caption">離線化中，請稍候（可能需要幾分鐘）...</div>';
  try {
    const data = await api('/api/update_cache', { method: 'POST' });
    box.innerHTML = `<div class="success-box">🎉 快取建立成功！共 ${data.count} 條路線</div>`;
  } catch (e) {
    box.innerHTML = `<div class="error-box">建立失敗：${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

// ── AI 助理 ──────────────────────────────────────────────
async function loadChatSessions() {
  const data = await api('/api/chat/sessions');
  renderChatSessions(data.sessions);
}
function renderChatSessions(sessions) {
  el('chat-session-list').innerHTML = sessions.map(s => `
    <div class="sess-item">
      <button class="btn sess-select ${s.is_current ? 'active' : ''}" data-sid="${esc(s.sid)}">${s.is_current ? '▶ ' : ''}${esc(s.title)}</button>
      <button class="btn sess-del" data-sid="${esc(s.sid)}">🗑</button>
    </div>`).join('');
  document.querySelectorAll('.sess-select').forEach(b => b.addEventListener('click', async () => {
    await api('/api/chat/sessions/switch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sid: b.dataset.sid })
    });
    el('chat-history-panel').classList.add('hidden');
    await loadChatSessions();
    await loadChatCurrent();
  }));
  document.querySelectorAll('.sess-del').forEach(b => b.addEventListener('click', async () => {
    await api('/api/chat/sessions/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sid: b.dataset.sid })
    });
    await loadChatSessions();
    await loadChatCurrent();
  }));
}
async function loadChatCurrent() {
  const data = await api('/api/chat/sessions/current');
  el('chat-current-title').textContent = data.title;
  renderChatMessages(data.history);
}
function renderChatMessages(history) {
  const box = el('chat-messages');
  box.innerHTML = history.map(m => `<div class="chat-msg ${m.role}">${esc(m.content)}</div>`).join('');
  box.scrollTop = box.scrollHeight;
}
async function sendChat() {
  const input = el('chat-input');
  const q = input.value.trim();
  if (!q) return;
  input.value = '';
  const box = el('chat-messages');
  box.insertAdjacentHTML('beforeend', `<div class="chat-msg user">${esc(q)}</div>`);
  box.scrollTop = box.scrollHeight;
  box.insertAdjacentHTML('beforeend', `<div class="chat-msg assistant" id="chat-thinking">思考中...</div>`);
  box.scrollTop = box.scrollHeight;
  try {
    const data = await api('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: q })
    });
    const thinking = el('chat-thinking');
    if (thinking) thinking.remove();
    if (data.error) {
      box.insertAdjacentHTML('beforeend', `<div class="chat-msg assistant">AI 錯誤：${esc(data.error)}</div>`);
    } else {
      box.insertAdjacentHTML('beforeend', `<div class="chat-msg assistant">${esc(data.reply)}</div>`);
      if (data.title) el('chat-current-title').textContent = data.title;
      loadChatSessions();
    }
    box.scrollTop = box.scrollHeight;
  } catch (e) {
    const thinking = el('chat-thinking');
    if (thinking) thinking.remove();
    box.insertAdjacentHTML('beforeend', `<div class="chat-msg assistant">AI 錯誤：${esc(e.message)}</div>`);
  }
}

// ── 地圖頁面 ─────────────────────────────────────────────
function initMapPageIfNeeded() {
  if (state.mapInited) {
    // 重新切回地圖頁：只重新整理『目前已經選取的路線』（或什麼都沒選就維持空白），
    // 不會又整批重新抓一次全部路線的資料。
    if (state.mapShowAll || el('map-route-input').value.trim()) loadMapData(true);
    return;
  }
  state.mapInited = true;
  state.leafletMap = L.map('leaflet-map', { zoomControl: true, preferCanvas: true }).setView([22.9997, 120.2270], 13);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors', subdomains: 'abc', maxZoom: 19
  }).addTo(state.leafletMap);
  state.shapeLayer = L.layerGroup().addTo(state.leafletMap);
  state.stopLayer = L.layerGroup().addTo(state.leafletMap);
  state.busLayer = L.layerGroup().addTo(state.leafletMap);
  state.userLocationLayer = L.layerGroup().addTo(state.leafletMap);
  state.leafletMap.on('zoomend', updateStopLabelVisibility);
  // 一開地圖頁只先載入「路線名稱清單」（很輕量，不會查即時公車／軌跡／站牌），
  // 讓路線選單馬上看得到；實際的公車動態／軌跡／站牌，改成只有使用者按下
  // 「全部路線」或勾選特定路線時才去抓，不會一打開地圖頁就把全台南路線整個抓一遍。
  loadMapRouteList();
}

// 定位使用者目前的位置，畫一個藍點＋精準度圓圈標示在地圖上，並飛過去該位置。
function locateMeOnMap() {
  const status = el('map-locate-status');
  if (!navigator.geolocation) {
    status.innerHTML = '<div class="error-box">瀏覽器不支援定位功能</div>';
    return;
  }
  if (!state.leafletMap) {
    status.innerHTML = '<div class="error-box">地圖尚未載入完成，請稍後再試</div>';
    return;
  }
  status.innerHTML = '<div class="caption">定位中...</div>';
  navigator.geolocation.getCurrentPosition(pos => {
    const { latitude, longitude, accuracy } = pos.coords;
    state.userLocationLayer.clearLayers();
    L.circle([latitude, longitude], {
      radius: accuracy || 30, color: '#4A90E2', fillColor: '#4A90E2', fillOpacity: 0.12, weight: 1
    }).addTo(state.userLocationLayer);
    L.marker([latitude, longitude], {
      icon: L.divIcon({
        className: '',
        html: '<div style="width:16px;height:16px;background:#4A90E2;border:3px solid #fff;border-radius:50%;box-shadow:0 0 8px #4A90E2;"></div>',
        iconSize: [16, 16], iconAnchor: [8, 8]
      })
    }).bindPopup('📍 我的位置').addTo(state.userLocationLayer);
    state.leafletMap.flyTo([latitude, longitude], 16);
    status.innerHTML = '<div class="success-box">已定位到你目前的位置</div>';
  }, () => {
    status.innerHTML = '<div class="error-box">無法取得定位，請確認已允許瀏覽器的定位權限</div>';
  }, { enableHighAccuracy: true, timeout: 10000 });
}

// 一開地圖頁先只拉「全部已知路線的名稱清單」＋「已儲存路線」，完全不查即時公車／軌跡／
// 站牌（很輕量），讓路線選單馬上看得到；地圖本身保持空白，等使用者按「全部路線」
// 或勾選特定路線才去抓真正的地圖資料。
async function loadMapRouteList(resetSelection = true) {
  const stats = el('map-panel-stats');
  if (resetSelection) stats.textContent = '載入路線清單中...';
  try {
    const data = await api('/api/map_route_list');
    state.mapAllRoutes = data.routes || [];
    state.savedRoutes = data.saved_routes || [];
    if (resetSelection) {
      state.mapBusData = [];
      state.mapShapeData = [];
      state.mapStopData = [];
      state.mapActiveRoutes = new Set();
      state.mapShowAll = false;
      drawMapShapes(); drawMapStops(); drawMapBuses();
      stats.textContent = '請點選路線，或按下方「全部路線」載入公車動態與軌跡';
    }
    renderMapPanel(el('map-search-box').value.trim());
  } catch (e) {
    if (resetSelection) stats.textContent = '路線清單載入失敗';
  }
}

async function saveRouteCoords() {
  const route = el('save-route-input').value.trim();
  const statusBox = el('save-route-status');
  const btn = el('btn-save-route-coords');
  if (!route) {
    statusBox.textContent = '⚠️ 請輸入路線名稱';
    return;
  }
  btn.disabled = true;
  statusBox.textContent = `抓取「${route}」的 Shape 與 StopOfRoute 資料中...`;
  try {
    const data = await api('/api/save_route_data', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ route })
    });
    statusBox.innerHTML =
      `✅ 已儲存<br>路線軌跡：${data.shape_ok ? `${data.shape_segments} 段` : '❌ 抓取失敗'} → ${esc(data.shape_file)}<br>` +
      `站牌清單：${data.stop_ok ? `${data.stop_count} 站` : '❌ 抓取失敗'} → ${esc(data.stop_file)}`;
    // 存檔成功後重新整理一次「路線清單」，讓底下的清單馬上出現這條新路線（💾 標記）；
    // 不直接整批重新抓地圖資料，除非使用者本來就已經選了「全部路線」或其他特定路線。
    if (data.shape_ok || data.stop_ok) {
      await loadMapRouteList(false);
      if (state.mapShowAll || el('map-route-input').value.trim()) await loadMapData(true);
    }
  } catch (e) {
    let html = `❌ ${esc(e.message)}`;
    const suggestions = e.data && e.data.suggestions;
    if (suggestions && suggestions.length) {
      html += '<div class="caption" style="margin-top:6px;">點一下可直接改用這個名稱重新抓取：</div>' +
        suggestions.map(s => `<button class="btn btn-block route-suggest-save" data-name="${esc(s)}">🚌 ${esc(s)}</button>`).join('');
    }
    statusBox.innerHTML = html;
    statusBox.querySelectorAll('.route-suggest-save').forEach(b => {
      b.addEventListener('click', () => {
        el('save-route-input').value = b.dataset.name;
        saveRouteCoords();
      });
    });
  } finally {
    btn.disabled = false;
  }
}

// 查不到某條路線的資料時，用字首（例如「藍幹線」取「藍」）反查 TDX 上名稱相近的路線，
// 讓使用者確認 TDX 真正登記的名稱，而不是憑猜測改設定。
async function lookupRouteName(routeText) {
  const resultBox = el('route-lookup-result');
  if (!resultBox) return;
  const keyword = (routeText || '').trim().charAt(0) || routeText;
  resultBox.innerHTML = '<div class="caption">查詢中...</div>';
  try {
    const data = await api(`/api/route_lookup?q=${encodeURIComponent(keyword)}`);
    if (!data.matches || !data.matches.length) {
      resultBox.innerHTML = `<div class="error-box">TDX 上找不到名稱包含「${esc(keyword)}」的路線，可能是這個字首本身就不對</div>`;
      return;
    }
    resultBox.innerHTML = `<div class="success-box">TDX 上名稱包含「${esc(keyword)}」的路線（點一下可直接改用這個名稱查詢）：</div>` +
      data.matches.map(m => `
        <button class="btn btn-block route-suggest" data-name="${esc(m.route_name)}">
          🚌 ${esc(m.route_name)}${m.operators && m.operators.length ? `（${esc(m.operators.join('、'))}）` : ''}
        </button>`).join('');
    resultBox.querySelectorAll('.route-suggest').forEach(b => {
      b.addEventListener('click', () => {
        el('map-route-input').value = b.dataset.name;
        state.busListOpen = true;
        loadMapData(true);
      });
    });
  } catch (e) {
    resultBox.innerHTML = `<div class="error-box">查詢失敗：${esc(e.message)}</div>`;
  }
}

async function loadMapData(forceRefresh) {
  const inputVal = el('map-route-input').value.trim();
  const stats = el('map-panel-stats');
  // 沒有輸入特定路線、也還沒按過「全部路線」的話，不要打去後端抓「全部路線」的重資料
  // （公車動態＋軌跡＋站牌一次抓全台南所有路線很吃 TDX 額度），維持空白地圖就好，
  // 一定要使用者明確選了東西（打字篩選、勾路線、或按「全部路線」）才真的去抓。
  if (!inputVal && !state.mapShowAll) {
    stats.textContent = '請點選路線，或按下方「全部路線」載入公車動態與軌跡';
    return;
  }
  stats.textContent = '載入中...';
  const params = inputVal ? `?routes=${encodeURIComponent(inputVal)}` : '';
  try {
    const data = await api(`/api/map_data${params}`);
    state.mapBusData = data.buses;
    state.mapShapeData = data.shapes;
    state.mapStopData = data.stops || [];
    state.savedRoutes = data.saved_routes || [];
    state.mapActiveRoutes = new Set(data.routes); // 這次實際抓到、畫出來的路線
    const staleNote = data.data_fresh === false ? '　｜　⚠️ 尚未更新資料' : '';
    el('map-caption').textContent = `資料時間：${data.now}　｜　每次按「🔄 更新」重抓最新位置${staleNote}`;
    drawMapShapes();
    drawMapStops();
    drawMapBuses();
    renderMapPanel(el('map-search-box').value);
    // 只有「畫面上真的有顯示公車定位」才開始跑 15 秒倒數自動更新；
    // 還沒選路線、或選到的路線目前剛好沒有任何公車在跑，就不要一直倒數
    // 卻永遠等不到東西可以更新，那樣只會讓人覺得畫面一直在空轉。
    if (state.mapBusData && state.mapBusData.length > 0) {
      if (!state.mapPollTimer) startMapAutoRefresh();
    } else {
      stopMapAutoRefresh();
    }
  } catch (e) {
    stats.textContent = '載入失敗';
    stopMapAutoRefresh();
  }
}

function makeBusIcon(color) {
  return L.divIcon({
    className: '',
    html: `<div style="width:18px;height:18px;background:${color};border:2.5px solid rgba(255,255,255,0.85);border-radius:50%;box-shadow:0 0 6px ${color};"></div>`,
    iconSize: [18, 18], iconAnchor: [9, 9]
  });
}
function drawMapShapes() {
  state.shapeLayer.clearLayers();
  state.mapShapeData.forEach(sh => {
    if (!state.mapActiveRoutes.has(sh.route)) return;
    const latlngs = sh.points.map(p => [p[0], p[1]]);
    L.polyline(latlngs, { color: sh.color, weight: 3, opacity: 0.75 })
      .bindTooltip(sh.route, { sticky: true }).addTo(state.shapeLayer);
  });
}
function drawMapStops() {
  state.stopLayer.clearLayers();

  // 同一個實體站牌常常被好幾條路線共用，如果每條路線都各自畫一個點，
  // 同一個位置就會疊出好幾個顏色不同、重疊在一起的圓點。這裡先依「站名＋座標」
  // 把同一個站牌合併成一個點，彈出視窗／常駐標籤改成把停靠的路線全部列出來，
  // 而不是每條路線各顯示一次。
  const merged = new Map();
  state.mapStopData.forEach(sp => {
    if (!state.mapActiveRoutes.has(sp.route)) return;
    const key = `${sp.name}|${sp.lat.toFixed(5)}|${sp.lon.toFixed(5)}`;
    if (!merged.has(key)) {
      merged.set(key, { name: sp.name, lat: sp.lat, lon: sp.lon, routes: [] });
    }
    const entry = merged.get(key);
    if (!entry.routes.some(r => r.route === sp.route)) {
      entry.routes.push({ route: sp.route, color: sp.color });
    }
  });

  merged.forEach(sp => {
    const multi = sp.routes.length > 1;
    const dotColor = multi ? '#000000' : sp.routes[0].color;
    const routeTags = sp.routes
      .map(r => `<span class="tag" style="background:${r.color}">${esc(r.route)}</span>`)
      .join(' ');
    L.circleMarker([sp.lat, sp.lon], {
      radius: multi ? 5 : 4, weight: multi ? 2 : 1, color: '#ffffff', opacity: 0.9,
      fillColor: dotColor, fillOpacity: 0.95
    })
      // 常駐標籤：放大到一定程度後才顯示，避免縮小檢視時上千個站名疊在一起看不清楚
      .bindTooltip(multi ? `${sp.name}（${sp.routes.length} 條路線）` : sp.name, {
        permanent: true, direction: 'right', offset: L.point(7, 0),
        className: 'stop-label', opacity: 0.9
      })
      // 不論目前是否放大，點擊/點選圓點都能直接看到站名跟所有停靠路線（手機點按也適用）
      .bindPopup(`<b>🚏 ${esc(sp.name)}</b><br>${routeTags}`)
      .addTo(state.stopLayer);
  });
  updateStopLabelVisibility();
}

function updateStopLabelVisibility() {
  if (!state.leafletMap) return;
  const zoom = state.leafletMap.getZoom();
  state.leafletMap.getContainer().classList.toggle('stops-zoomed-in', zoom >= 15);
}
function countByRoute() {
  const cnt = {};
  state.mapBusData.forEach(b => { if (state.mapActiveRoutes.has(b.route)) cnt[b.route] = (cnt[b.route] || 0) + 1; });
  return cnt;
}
function drawMapBuses() {
  state.busLayer.clearLayers();
  let total = 0;
  state.mapBusData.forEach(b => {
    if (!state.mapActiveRoutes.has(b.route)) return;
    const marker = L.marker([b.lat, b.lon], { icon: makeBusIcon(b.color) });
    marker.bindPopup(`
      <div class="bus-popup">
        <b>🚌 ${esc(b.route)}</b><br>
        <span class="tag" style="background:${b.color}">車牌：${esc(b.plate)}</span>
        <span class="tag" style="background:#555">方向：${esc(b.dir)}</span>
        <span class="tag" style="background:#333">速度：${esc(String(b.speed))} km/h</span>
        ${b.branch ? `<span class="tag" style="background:#f39c12">🔀 往 ${esc(b.branch)}</span>` : ''}
      </div>`, { maxWidth: 220 });
    marker.addTo(state.busLayer);
    total++;
  });
  el('map-panel-stats').textContent = `顯示 ${state.mapActiveRoutes.size} 條路線・${total} 台公車`;
  renderMapBusList();
}

// 最上面的查詢欄（篩選路線）除了在地圖上畫出公車圖示，
// 同時也可以把該路線（或該次篩選的每一條路線）上「每一台公車」的最新定位，
// 以文字清單的方式列出來，不用逐一點地圖上的圓點才看得到。
// 為了不要一查完就整塊清單直接撐開、把下面「抓取並儲存路線原始資料」的版面往下推，
// 預設是收合的，清單前面會有一顆按鈕，使用者按了才展開／收合。
function renderMapBusList() {
  const btn = el('btn-toggle-bus-list');
  const container = el('map-bus-list');
  if (!btn || !container) return;

  const inputVal = el('map-route-input').value.trim();
  // 「有篩選」不是只看上面那個文字輸入框，直接在下面路線清單點選一條（或少數幾條）
  // 路線一樣算有篩選 —— 之前只認輸入框的話，用清單點路線篩出來的公車定位清單就會
  // 整個消失不見，這是原本「左邊公車班次資訊不見了」的主因。
  const hasSelection = !!inputVal ||
    (state.mapActiveRoutes.size > 0 && state.mapActiveRoutes.size < state.mapAllRoutes.length);
  const buses = state.mapBusData
    .filter(b => state.mapActiveRoutes.has(b.route))
    .sort((a, b) => a.route.localeCompare(b.route) || String(a.plate).localeCompare(String(b.plate)));

  if (!hasSelection) {
    // 顯示「全部路線」時公車數量太多不適合列清單，直接隱藏按鈕與清單
    btn.classList.add('hidden');
    container.classList.add('hidden');
    container.innerHTML = '';
    return;
  }

  btn.classList.remove('hidden');
  btn.textContent = state.busListOpen
    ? `🔼 收合公車定位清單（共 ${buses.length} 台）`
    : `🚌 查看路線上每台公車的最新定位（共 ${buses.length} 台）`;

  if (!state.busListOpen) {
    container.classList.add('hidden');
    return;
  }

  container.classList.remove('hidden');
  if (!buses.length) {
    container.innerHTML = `
      <div class="warning-box">目前查無這個篩選條件下的公車即時定位（該路線可能暫時沒有營運中的車輛，也可能是路線名稱跟 TDX 登記的不完全一樣）</div>
      <button id="btn-route-name-lookup" class="btn btn-block">🔍 查詢 TDX 正確路線名稱</button>
      <div id="route-lookup-result"></div>`;
    el('btn-route-name-lookup').addEventListener('click', () => lookupRouteName(inputVal));
    return;
  }

  container.innerHTML = `<div class="caption">🚌 目前共 ${buses.length} 台公車最新定位（點項目可在地圖上定位）：</div>` +
    buses.map(b => `
      <div class="stop-item bus-list-item" style="cursor:pointer" data-lat="${b.lat}" data-lon="${b.lon}">
        <span class="tag" style="background:${b.color}">${esc(b.route)}</span>
        車牌 <b>${esc(b.plate || '未知')}</b>
        ・${esc(b.dir)}
        ${b.branch ? `・<span class="tag" style="background:#f39c12">🔀 往 ${esc(b.branch)}</span>` : ''}
        ・${esc(String(b.speed))} km/h
        ・📍 ${Number(b.lat).toFixed(5)}, ${Number(b.lon).toFixed(5)}
      </div>`).join('');

  container.querySelectorAll('.bus-list-item').forEach(item => {
    item.addEventListener('click', () => {
      const lat = parseFloat(item.dataset.lat);
      const lon = parseFloat(item.dataset.lon);
      if (state.leafletMap && !isNaN(lat) && !isNaN(lon)) {
        state.leafletMap.flyTo([lat, lon], 17);
      }
    });
  });
}
function renderMapPanel(filterText) {
  const list = el('map-route-list');
  list.innerHTML = '';
  const cnt = countByRoute();

  const allItem = document.createElement('div');
  const allActive = state.mapShowAll;
  allItem.className = 'route-item' + (allActive ? ' active' : '');
  allItem.innerHTML = `<div class="route-dot" style="background:#fff;"></div><span>全部路線</span><span class="route-count">${state.mapAllRoutes.length}</span>`;
  allItem.onclick = () => {
    if (state.mapShowAll) {
      // 再點一次「全部路線」＝取消，清空地圖，不用重新打後端
      state.mapShowAll = false;
      state.mapActiveRoutes = new Set();
      state.mapBusData = []; state.mapShapeData = []; state.mapStopData = [];
      el('map-route-input').value = '';
      drawMapShapes(); drawMapStops(); drawMapBuses();
      renderMapPanel(filterText);
      el('map-panel-stats').textContent = '請點選路線，或按下方「全部路線」載入公車動態與軌跡';
    } else {
      // 使用者明確按下「全部路線」，這時候才真的去後端抓全台南所有路線的資料
      state.mapShowAll = true;
      el('map-route-input').value = '';
      loadMapData(true);
    }
  };
  list.appendChild(allItem);

  state.mapAllRoutes.forEach(route => {
    if (filterText && !route.includes(filterText)) return;
    const color = getRouteColor(route);
    const n = cnt[route] || 0;
    const isSaved = state.savedRoutes.includes(route);
    const item = document.createElement('div');
    item.className = 'route-item' + (state.mapActiveRoutes.has(route) ? ' active' : '');
    item.title = isSaved ? '已儲存路線原始資料（Shape＋StopOfRoute）' : '';
    item.innerHTML = `<div class="route-dot" style="background:${color};"></div><span>${esc(route)}</span><span class="route-count">${n}</span>`;
    item.onclick = () => {
      // 勾選／取消個別路線：只抓使用者實際選取的這幾條路線，不會連帶把其他路線也一起抓，
      // 也不再把「全部路線」的狀態一起打開。
      state.mapShowAll = false;
      if (state.mapActiveRoutes.has(route)) state.mapActiveRoutes.delete(route);
      else state.mapActiveRoutes.add(route);
      if (state.mapActiveRoutes.size === 0) {
        state.mapBusData = []; state.mapShapeData = []; state.mapStopData = [];
        el('map-route-input').value = '';
        drawMapShapes(); drawMapStops(); drawMapBuses();
        renderMapPanel(filterText);
        el('map-panel-stats').textContent = '請點選路線，或按下方「全部路線」載入公車動態與軌跡';
      } else {
        el('map-route-input').value = [...state.mapActiveRoutes].join(', ');
        loadMapData(true);
      }
    };
    list.appendChild(item);
  });

  // 已經存檔過、但目前不在頂端篩選欄範圍內的路線，也一律列在這裡（前面加 💾），
  // 不會因為上面查詢欄篩選成只剩一條路線，就把其他已儲存的路線從清單裡藏起來。
  const extraSaved = state.savedRoutes.filter(r => !state.mapAllRoutes.includes(r));
  extraSaved.forEach(route => {
    if (filterText && !route.includes(filterText)) return;
    const color = getRouteColor(route);
    const item = document.createElement('div');
    item.className = 'route-item';
    item.title = '已儲存路線原始資料（Shape＋StopOfRoute）－目前不在篩選範圍內，點一下即可加入';
    item.innerHTML = `<div class="route-dot" style="background:${color};"></div><span>${esc(route)}</span><span class="route-count">－</span>`;
    item.onclick = () => {
      // 用「加入」而不是「取代」：把這條路線併進目前查詢欄的清單，
      // 這樣才能一次累加選取多條路線一起顯示，不會每點一條就把前面選的路線洗掉。
      state.mapShowAll = false;
      const inp = el('map-route-input');
      const existing = inp.value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
      if (!existing.includes(route)) existing.push(route);
      inp.value = existing.join(', ');
      loadMapData(true);
    };
    list.appendChild(item);
  });
}
