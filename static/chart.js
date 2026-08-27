/* ── Plybit AI — chart.js ─────────────────────────────────────────────────── *
 * Smooth candle: ease-out cubic tween @ 60 fps                               *
 *                                                                             *
 * Each server tick sets a TARGET.  The RAF loop tweens the rendered candle   *
 * from wherever it currently is → that target using easeOutCubic so the      *
 * body/wicks glide to their destination instead of snapping.                 *
 * ─────────────────────────────────────────────────────────────────────────── */

'use strict';

// ── Pair label formatter ────────────────────────────────────────────────────
// Convert asset codes ("EURUSD_otc", "EURUSD", "USDJPY") into the conventional
// XXX/YYY display form. The previous naive `.replace('_otc','').replace('_','/')`
// only swapped underscores for slashes; for "EURUSD_otc" -> "EURUSD" -> "EURUSD"
// there was no underscore left to swap, so users saw "EURUSD" not "EUR/USD".
function _fmtPairLabel(asset) {
  if (!asset) return '';
  let base = String(asset);
  if (base.endsWith('_otc')) base = base.slice(0, -4);
  if (base.length === 6 && /^[A-Z]{6}$/.test(base)) {
    return base.slice(0, 3) + '/' + base.slice(3);
  }
  // Non-6-letter codes (rare): fall back to swapping any remaining underscore.
  return base.replace('_', '/');
}

// ── Chart / WS globals ────────────────────────────────────────────────────
let chart       = null;
let mainSeries  = null;
let predSeries  = null;
let ws          = null;
let reconnTimer = null;

let currentAsset   = 'EURUSD';   // FIX: was 'EURUSD_otc' which is NOT in _VALID_ASSETS
                                  // (EURUSD/GBPUSD/USDJPY/AUDUSD/EURGBP are real, not otc).
                                  // The old default caused the very first /api/subscribe
                                  // POST to fail and the chart to stick on "No data".
let currentPeriod  = 60;

// Restore the last pair/timeframe the user had open — otherwise every
// browser refresh silently dropped back to the EURUSD_otc/1m default.
// try/catch: localStorage can throw (private-browsing quota, etc.) —
// falling back to the hardcoded defaults above is fine either way.
try {
  const _savedAsset  = localStorage.getItem('plybit_asset');
  const _savedPeriod = parseInt(localStorage.getItem('plybit_period'), 10);
  if (_savedAsset) currentAsset = _savedAsset;
  if (_savedPeriod) currentPeriod = _savedPeriod;
} catch (_) {}

function _savePairPrefs() {
  try {
    localStorage.setItem('plybit_asset', currentAsset);
    localStorage.setItem('plybit_period', String(currentPeriod));
  } catch (_) {}
}

// Mobile-only bottom-tab navigation (Home/Signals/Advance/Settings — see
// Default tab is 'home' (the overview dashboard). Restored from
// localStorage if the user previously switched tabs — old saved values
// like 'advance' or 'signals' are migrated to the new layout.
let _activeTab = 'home';
try {
  const _savedTab = localStorage.getItem('plybit_tab');
  if (_savedTab) _activeTab = _savedTab;
  // Migrate old saved tab names: 'advance' → 'chart', 'signals' → 'home'
  if (_activeTab === 'advance') _activeTab = 'chart';
  if (_activeTab === 'signals') _activeTab = 'home';
} catch (_) {}

// Auth/account system fully removed — every visitor gets the full chart
// and the full prediction/microstructure payload. No tier gating.

// ── User preferences — client-side only (localStorage), applied live.
// Rendered as the Preferences card in Settings; consumed by
// _showSignalPopup / _setKLLines / applyPrediction. ─────────────────────
const _PREF_DEFAULTS = { popup: true, klines: true, wwalls: true, ghost: true };
let userPrefs = { ..._PREF_DEFAULTS };
try {
  Object.assign(userPrefs,
    JSON.parse(localStorage.getItem('plybit_prefs') || '{}'));
} catch (_) {}
function _savePrefs() {
  try { localStorage.setItem('plybit_prefs', JSON.stringify(userPrefs)); }
  catch (_) {}
}

// True once the user has actually zoomed/panned the chart (wheel or a
// drag) — once true, incoming EOC/tick updates stop force-scrolling the
// view back to "real time" so a manual zoom survives new candles/signals
// arriving. Reset on a deliberate pair/timeframe switch (resetAndSubscribe),
// where snapping to the latest data again is exactly what's wanted.
let _userTouchedChart = false;
let pairsList      = [];          // [{asset, display, status, payout, locked}] — unified list
let payoutFloor    = 81;          // min payout % a pair needs to be streamable — server-authoritative
let pairSearchTerm = '';          // live filter typed into #pair-search
let lastPrediction = null;
let lastDataAt     = 0;           // Date.now() of the last real candle/tick update

// True from the moment a pair/timeframe switch clears the chart until the
// authoritative candle history for the NEW selection actually arrives.
// Without this, a live "tick" broadcast for the new asset (which the WS
// connection keeps delivering in the background regardless of the pending
// /api/subscribe request) can land BEFORE that request's response — and
// since the chart was just cleared to empty, rendering that one tick alone
// makes it look like most of the history is missing until the real
// snapshot catches up a moment later.
let _awaitingSnapshot = false;

// One id per page load — lets the backend track which pair THIS tab/window
// is interested in (server now runs one independent stream per distinct
// asset/period, not just one shared feed — see the multi-viewer refactor).
// A stream with no interested client ids for a while gets torn down.
const CLIENT_ID = (crypto.randomUUID && crypto.randomUUID()) ||
  ('cid-' + Math.random().toString(36).slice(2) + Date.now());

// ── Live running-candle price line ──────────────────────────────────────────
// One custom price line pinned to the live close, colour escalating
// blue → yellow → red as the candle nears close. Created lazily once
// mainSeries + a real price exist; mainSeries is never recreated on pair
// switch (only its data resets), so this reference stays valid for the app's
// lifetime.
//
// The candle countdown itself does NOT live on this line's title (2026-07-07
// redesign put it there, but on assets with several key-level/wick-wall
// lines close in price, their axis-label stack buried the countdown). It now
// floats in its own #chart-countdown badge, top-left over the chart, clear
// of that stack — see _updateLiveLineTimer below.
let _liveLine = null;

function _ensureLiveLine() {
  if (_liveLine || !mainSeries || !(_rClose > 0)) return;
  try {
    _liveLine = mainSeries.createPriceLine({
      price:            _rClose,
      color:            '#448aff',
      lineWidth:        1,
      lineStyle:        LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title:            '',
    });
  } catch (_) { _liveLine = null; }
}

// Called each second by tickCountdown — updates the floating badge + the
// live line's colour (kept in sync so the dashed line still escalates red
// as a secondary cue, even though its title stays blank).
function _updateLiveLineTimer(left, cls) {
  const color = cls === 'danger' ? '#ff1744'
              : cls === 'warn'   ? '#ffd740'
              :                    '#448aff';
  if (_liveLine) { try { _liveLine.applyOptions({ color }); } catch (_) {} }

  const badge = document.getElementById('chart-countdown');
  if (badge) {
    // Only show once a real live price exists — matches the old title's
    // behaviour of staying blank until _ensureLiveLine had real data.
    badge.textContent = left + 's';
    badge.className   = `chart-countdown ${cls}` + (_liveLine ? '' : ' hidden');
  }
}

// ── Key level price lines ─────────────────────────────────────────────────
let _klLines = [];

function _clearKLLines() {
  if (!mainSeries) return;
  for (const line of _klLines) {
    try { mainSeries.removePriceLine(line); } catch (_) {}
  }
  _klLines = [];
  const ul = document.getElementById('key-levels-list');
  if (ul) ul.innerHTML = '<li class="empty">–</li>';
}

function _addKLLine(price, touches, color, style, labelPrefix, ul) {
  if (!price || touches < 2) return;
  try {
    const line = mainSeries.createPriceLine({
      price,
      color,
      lineWidth: 1,
      lineStyle: style,
      axisLabelVisible: true,
      title: `${labelPrefix}x${touches}`,
    });
    _klLines.push(line);
  } catch (_) {}

  if (ul) {
    const isMajor = touches >= 4;
    const li = document.createElement('li');
    li.className = `kl-item ${isMajor ? 'kl-major' : 'kl-minor'}`;
    li.innerHTML =
      `<span class="kl-price">${price.toPrecision(6)}</span>` +
      `<span class="kl-touch">${labelPrefix}x${touches}</span>`;
    ul.appendChild(li);
  }
}

// keyLevels   : formal swing-pivot levels (analyze_eoc's `key_levels`).
// wickWalls   : { support: [...], resistance: [...] } — looser wick-cluster
//               levels (analyze_eoc's `wick_walls`), drawn dotted so they
//               read as a second, weaker tier next to the dashed pivot levels.
function _setKLLines(keyLevels, wickWalls) {
  _clearKLLines();
  if (!mainSeries) return;
  // User preferences can switch either overlay tier off (Settings card)
  if (!userPrefs.klines) keyLevels = null;
  if (!userPrefs.wwalls) wickWalls = null;
  const hasKL = keyLevels && keyLevels.length;
  const hasWW = wickWalls && ((wickWalls.support || []).length || (wickWalls.resistance || []).length);
  if (!hasKL && !hasWW) return;

  const ul = document.getElementById('key-levels-list');
  if (ul) ul.innerHTML = '';

  for (const [price, touches] of (keyLevels || []).slice(0, 20)) {
    const isMajor = touches >= 4;
    const color = isMajor ? 'rgba(255,160,0,0.65)' : 'rgba(68,138,255,0.5)';
    _addKLLine(price, touches, color, LightweightCharts.LineStyle.Dashed, '', ul);
  }

  for (const [price, touches] of ((wickWalls && wickWalls.support) || []).slice(0, 8)) {
    _addKLLine(price, touches, 'rgba(0,230,118,0.45)', LightweightCharts.LineStyle.Dotted, 'w', ul);
  }
  for (const [price, touches] of ((wickWalls && wickWalls.resistance) || []).slice(0, 8)) {
    _addKLLine(price, touches, 'rgba(255,23,68,0.45)', LightweightCharts.LineStyle.Dotted, 'w', ul);
  }
}

// ── Accuracy history strip ────────────────────────────────────────────────
let _recentResults = [];  // {signal, result} last 15 entries

function _pushResult(signal, result) {
  _recentResults.push({ signal, result });
  if (_recentResults.length > 15) _recentResults.shift();
  _renderAccuracyStrip();
}

function _renderAccuracyStrip() {
  const el = document.getElementById('accuracy-strip');
  if (!el) return;
  if (!_recentResults.length) { el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  el.innerHTML = '';
  for (const { signal, result } of _recentResults) {
    const dot = document.createElement('span');
    dot.className = `acc-dot ${result} ${(signal || '').toLowerCase()}`;
    dot.title     = `${signal}: ${result}`;
    dot.textContent = result === 'correct' ? '✓' : result === 'draw' ? '–' : '✗';
    el.appendChild(dot);
  }
}

// ── Entry timing hint ─────────────────────────────────────────────────────
function updateEntryTiming() {
  const el = document.getElementById('entry-timing');
  if (!el) return;

  if (!lastPrediction || lastPrediction.signal === 'NEUTRAL') {
    el.className = 'entry-timing hidden';
    return;
  }

  const now      = Math.floor(Date.now() / 1000);
  const left     = currentPeriod - (now % currentPeriod);
  const strength = lastPrediction.strength || 'WEAK';

  el.classList.remove('hidden', 'et-go', 'et-warn', 'et-skip');
  el.title = '';

  if (strength === 'WEAK') {
    el.classList.add('et-skip');
    el.textContent = '· SKIP';
  } else if (left <= 5) {
    el.classList.add('et-skip');
    el.textContent = '⏱ TOO LATE';
  } else if (left >= 8 && left <= currentPeriod - 3) {
    el.classList.add('et-go');
    el.textContent = '⚡ ENTER NOW';
  } else {
    el.classList.add('et-warn');
    el.textContent = '◷ WAIT…';
  }
}

// ── Smooth candle — ease-out cubic tween ─────────────────────────────────
//
//  One tick every ~500 ms arrives from the server.
//  We tween the rendered candle from its current position to the new target
//  over TWEEN_MS ms using easeOutCubic — fast start, smooth deceleration.
//  When the next tick arrives mid-tween we restart from wherever we are,
//  giving continuous fluid movement with no jumps.
//
const TWEEN_MS = 480;           // duration: covers ~1 tick interval

// Tween source  (where we were when the last tick arrived)
let _fromClose = 0, _fromHigh = 0, _fromLow = 0;
// Tween target  (where the server says we should be)
let _toClose   = 0, _toHigh   = 0, _toLow   = 0;

// Currently rendered values (updated every frame)
let _rTime  = 0;
let _rOpen  = 0;
let _rClose = 0;
let _rHigh  = 0;
let _rLow   = 0;

let _tweenStart = 0;     // performance.now() when tween began
let _rafActive  = false;

// Ease-out cubic: fast start, decelerates near target
function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

// ── RAF loop ──────────────────────────────────────────────────────────────
function _rafFrame(ts) {
  if (!_rafActive) return;

  // _rOpen <= 0 means no valid tick yet — skip to avoid LightweightCharts "Value is null"
  if (_rTime > 0 && _rOpen > 0 && mainSeries) {
    const elapsed  = ts - _tweenStart;
    const progress = Math.min(elapsed / TWEEN_MS, 1.0);
    const eased    = easeOutCubic(progress);

    // Interpolate all three moving dimensions
    _rClose = _fromClose + (_toClose - _fromClose) * eased;
    _rHigh  = _fromHigh  + (_toHigh  - _fromHigh)  * eased;
    _rLow   = _fromLow   + (_toLow   - _fromLow)   * eased;

    // Clamp: high >= max(close, open), low <= min(close, open)
    const safeHigh  = Math.max(_rHigh,  _rClose, _rOpen);
    const safeLow   = Math.min(_rLow,   _rClose, _rOpen);
    const safeClose = Math.min(safeHigh, Math.max(safeLow, _rClose));

    // Final guard: NaN check before handing off to LightweightCharts render pipeline
    if (!isNaN(safeHigh) && !isNaN(safeLow) && !isNaN(safeClose)) {
      try {
        mainSeries.update({
          time:  _rTime,
          open:  _rOpen,
          high:  safeHigh,
          low:   safeLow,
          close: safeClose,
        });
      } catch (_) {}

      // Keep the live price line (which carries the countdown) glued to the
      // running close so it moves with the candle instead of lagging it.
      _ensureLiveLine();
      if (_liveLine) {
        try { _liveLine.applyOptions({ price: safeClose }); } catch (_) {}
      }
    }
  }

  requestAnimationFrame(_rafFrame);
}

function _startRaf() {
  if (_rafActive) return;
  _rafActive  = true;
  _tweenStart = performance.now();
  requestAnimationFrame(_rafFrame);
}

// ── Target setter — called on every server tick ───────────────────────────
function _setTarget(candle, perfNow) {
  // Reject candles with invalid prices — server may send zeros before first tick
  if (!candle || !candle.open || candle.open <= 0 || !candle.time) return;

  // Reject backward-in-time ticks — mainSeries.update() throws on them and the
  // chart breaks. (Forward = new candle, equal = same candle update.)
  if (_rTime > 0 && candle.time < _rTime) return;

  const isNewCandle = (_rTime !== candle.time);

  if (isNewCandle) {
    // New candle: snap rendered state to the open immediately
    _rTime  = candle.time;
    _rOpen  = candle.open;
    _rClose = candle.open;
    _rHigh  = candle.open;
    _rLow   = candle.open;
  }

  // Restart tween FROM current rendered position → new target
  _fromClose  = _rClose;
  _fromHigh   = _rHigh;
  _fromLow    = _rLow;
  _toClose    = candle.close;
  _toHigh     = candle.high;
  _toLow      = candle.low;
  _tweenStart = perfNow || performance.now();
}

// Snap LERP state to a known candle (on snapshot / pair change)
function _resetRaf(candle) {
  if (candle) {
    _rTime  = candle.time;  _rOpen  = candle.open;
    _rClose = candle.close; _rHigh  = candle.high;  _rLow = candle.low;
    _fromClose = _toClose = candle.close;
    _fromHigh  = _toHigh  = candle.high;
    _fromLow   = _toLow   = candle.low;
  } else {
    _rTime = 0;
  }
  _tweenStart = performance.now();
}

// ── Fatal error overlay (chart library missing / boot crash) ─────────────
function showFatalError(text, sub) {
  const el = document.getElementById('fatal-error');
  if (!el) return;
  const t = document.getElementById('fatal-error-text');
  const s = document.getElementById('fatal-error-sub');
  if (t) t.textContent = text;
  if (s) s.textContent = sub || '';
  el.classList.remove('hidden');
}

function hideFatalError() {
  const el = document.getElementById('fatal-error');
  if (el) el.classList.add('hidden');
}

// ── Init chart ─────────────────────────────────────────────────────────────
function initChart() {
  if (typeof LightweightCharts === 'undefined') {
    throw new Error('LightweightCharts library not loaded');
  }
  const wrap = document.getElementById('chart');

  chart = LightweightCharts.createChart(wrap, {
    layout: {
      background: { color: '#070a0f' },
      textColor:  '#67728c',
    },
    grid: {
      vertLines: { color: '#15151d' },
      horzLines: { color: '#15151d' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: '#334', labelBackgroundColor: '#1a1a24' },
      horzLine: { color: '#334', labelBackgroundColor: '#1a1a24' },
    },
    rightPriceScale: { borderColor: '#2a3345' },
    timeScale: {
      borderColor:    '#2a3345',
      timeVisible:    true,
      secondsVisible: true,
      rightOffset:    8,
    },
    handleScroll: true,
    handleScale:  true,
  });

  // Prediction ghost — drawn first (behind real candles)
  predSeries = chart.addCandlestickSeries({
    upColor:          'rgba(0, 230, 118, 0.18)',
    downColor:        'rgba(255, 23, 68, 0.18)',
    borderUpColor:    'rgba(0, 230, 118, 0.40)',
    borderDownColor:  'rgba(255, 23, 68, 0.40)',
    wickUpColor:      'rgba(0, 230, 118, 0.35)',
    wickDownColor:    'rgba(255, 23, 68, 0.35)',
    priceLineVisible: false,
    lastValueVisible: false,
  });

  // Main series — on top
  mainSeries = chart.addCandlestickSeries({
    upColor:          '#00e676',
    downColor:        '#ff1744',
    borderUpColor:    '#00e676',
    borderDownColor:  '#ff1744',
    wickUpColor:      '#00e676',
    wickDownColor:    '#ff1744',
    // The built-in last-price line + axis label are replaced by _liveLine
    // (a custom price line that ALSO carries the candle countdown as its
    // title) — see _ensureLiveLine / _updateLiveLineTimer. Turning both off
    // here avoids drawing two overlapping lines / two axis labels.
    priceLineVisible: false,
    lastValueVisible: false,
  });

  // Auto-resize
  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: wrap.clientWidth, height: wrap.clientHeight });
  });
  ro.observe(wrap);
  chart.applyOptions({ width: wrap.clientWidth, height: wrap.clientHeight });

  // Mark manual zoom/pan so applySnapshot stops re-centering the view on
  // every new candle/signal — wheel = zoom, pointerdown = the start of a
  // mouse or touch drag-to-pan (unified across input types).
  wrap.addEventListener('wheel', () => { _userTouchedChart = true; }, { passive: true });
  wrap.addEventListener('pointerdown', () => { _userTouchedChart = true; });

  _startRaf();
}

// ── Broker time sync ───────────────────────────────────────────────────────
// The candle countdown MUST align with the Quotex broker's server clock, not
// the user's local clock — otherwise candles would close a few hundred ms
// early/late on machines with skewed NTP. We fetch /api/server-time every
// 30s to learn the offset (broker_time - local_time) and apply it to every
// countdown calculation. The offset is in milliseconds for sub-second
// precision.
let _brokerOffsetMs = 0;   // broker_time - local_time, in ms (positive = broker ahead)

async function _syncBrokerTime() {
  try {
    const r = await fetch('/api/server-time');
    if (!r.ok) return;
    const d = await r.json();
    if (typeof d.offset_ms === 'number') {
      _brokerOffsetMs = d.offset_ms;
      // Log only when the offset changes significantly (>500ms) so the
      // console isn't spammed on every sync.
      if (Math.abs(_brokerOffsetMs) > 500) {
        console.log(`[time] broker offset: ${_brokerOffsetMs}ms ` +
                    `(broker ${d.broker_time} vs local ${d.local_time})`);
      }
    }
  } catch (_) {}
}

// Returns the current broker time in seconds (float, sub-second precision).
function _brokerNowSec() {
  return (Date.now() + _brokerOffsetMs) / 1000;
}

// Initial sync + periodic refresh (every 30s — the offset rarely drifts
// fast, but this catches any gradual NTP skew on either side).
_syncBrokerTime();
setInterval(_syncBrokerTime, 30000);

// ── Countdown ──────────────────────────────────────────────────────────────
// Uses the BROKER's server time (via _brokerNowSec) so the countdown aligns
// exactly with the broker's candle period rollover — no ms drift from local
// NTP skew. Updated every 250ms for sub-second smoothness on the big overlay.
function tickCountdown() {
  const nowSec = _brokerNowSec();
  const left = currentPeriod - (Math.floor(nowSec) % currentPeriod);
  const cls  = left <= 5 ? 'danger' : left <= 15 ? 'warn' : '';

  // (Header countdown element was removed in an earlier refactor — it now
  // lives on the chart's live price line. The lookup is intentionally a
  // no-op so legacy callers don't throw. Left as a guard for safety.)
  const el = document.getElementById('countdown');
  if (el) { el.textContent = left + 's'; el.className = cls; }

  // Big countdown overlay on the chart — large mono digits with pulse
  // animation when <10s remaining. Shows seconds remaining + the broker's
  // current HH:MM:SS so the user can verify the sync.
  const cd = document.getElementById('chart-countdown');
  if (cd) {
    const brokerDate = new Date(nowSec * 1000);
    const hms = brokerDate.toLocaleTimeString('en-GB', {
      hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
      timeZone: 'UTC'
    });
    cd.innerHTML = `<span class="countdown-time">${left}s</span>` +
                   `<span class="countdown-broker">${hms} UTC</span>`;
    cd.className = 'chart-countdown ' + cls;
    cd.classList.toggle('hidden', left <= 0);
  }

  // The running candle's price line shows the countdown on the chart.
  _updateLiveLineTimer(left, cls);

  // Update the per-pair countdown in the sidebar pair list
  _updateSidebarCountdowns();

  updateEntryTiming();
}
setInterval(tickCountdown, 250);   // 4× per second for smooth countdown
tickCountdown();

// ── WebSocket ──────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const q = `cid=${encodeURIComponent(CLIENT_ID)}&asset=${encodeURIComponent(currentAsset)}&period=${currentPeriod}`;
  ws = new WebSocket(`${proto}//${location.host}/ws?${q}`);

  ws.onopen = () => {
    setStatus('connected', '● Live');
    clearTimeout(reconnTimer);
    sendSubscribe();
  };

  ws.onmessage = (e) => {
    const now = performance.now();
    try { handleMsg(JSON.parse(e.data), now); }
    catch (_) {}
  };

  ws.onclose = () => {
    setStatus('disconnected', '● Offline');
    reconnTimer = setTimeout(connect, 3000);
  };

  ws.onerror = () => ws.close();
}

async function sendSubscribe() {
  try {
    const res  = await fetch('/api/subscribe', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ asset: currentAsset, period: currentPeriod, cid: CLIENT_ID }),
    });
    const data = await res.json().catch(() => null);
    if (!data) return;
    // ok:false means the server declined to START a NEW stream (an already
    // -running one is never declined) — surface why via the existing
    // no-data overlay instead of silently doing nothing.
    if (data.ok === false) {
      const msg = data.status === 'at_capacity'
        ? `Server is at capacity (${data.max} pairs live) — try again shortly`
        : data.status === 'locked'
        ? (data.reason || `This pair needs ${payoutFloor}% payout to open`)
        : `Cooling down after connection errors — retry in ~${Math.ceil(data.retry_after || 5)}s`;
      showNoData(true, msg);
      return;
    }
    // Joining an ALREADY-running stream (someone else already has this pair
    // open) skips the initial WS "snapshot" broadcast entirely — the server
    // hands the current candles/prediction back in this response instead so
    // the chart doesn't sit empty until the next candle close. Guard against
    // the pair having changed again while this request was in flight.
    if (data.candles && data.asset === currentAsset && data.period === currentPeriod) {
      applySnapshot(data.candles, data.prediction);
    }
  } catch (_) {}
}

// ── Message handler ────────────────────────────────────────────────────────
function handleMsg(msg, perfNow) {
  // Server now runs one independent stream per distinct (asset, period), so
  // a message for a pair this tab didn't select just means some OTHER
  // viewer's stream ticked — ignore it, no fighting involved.
  if (msg.asset && msg.asset !== currentAsset)    return;
  if (msg.period && msg.period !== currentPeriod) return;

  switch (msg.type) {

    case 'pairs':
      pairsList = msg.pairs || [];
      if (typeof msg.payout_floor === 'number') payoutFloor = msg.payout_floor;
      renderPairSelect();
      break;

    case 'stale':
      showNoData(true);
      break;

    case 'snapshot':
      applySnapshot(msg.candles, msg.prediction);
      break;

    case 'eoc': {
      // FIX: capture the OLD prediction BEFORE applySnapshot replaces it.
      // The accuracy dot is for the candle that just CLOSED — i.e., the
      // PREVIOUS prediction. applySnapshot then loads the NEW prediction
      // (for the candle that's just OPENING). Without this fix, the
      // accuracy strip showed "PUT ✓" when the new prediction was PUT
      // but the graded (old) prediction was actually CALL — wrong label.
      const oldPred = lastPrediction;
      if (msg.candles && msg.candles.length) { lastDataAt = Date.now(); showNoData(false); }
      applySnapshot(msg.candles, msg.prediction);
      if (msg.accuracy) {
        showAccuracy(msg.accuracy);
        if (oldPred && oldPred.signal !== 'NEUTRAL') {
          _pushResult(oldPred.signal, msg.accuracy);
        }
      }
      // Share Signal popup is gated by userPrefs.popup and fires from
      // applySnapshot when a new prediction lands, regardless of active tab.
      loadStats();
      // Refresh the sidebar's "Recent Signals" mini-list after each candle close
      _renderSignalHistoryMini();
      break;
    }

    case 'signal_start':
      // FIX: server emits signal_start on every candle close (per the
      // user's 0-second-signal requirement). Previously the frontend
      // didn't handle this message type at all — it was silently dropped.
      // We capture the broker-locked candle timing so tickCountdown and
      // updateEntryTiming use the actual broker candle boundary instead
      // of a free-running local modulo.
      if (msg.candle_open_time)  _brokerCandleOpen = msg.candle_open_time;
      if (msg.candle_expires_at) _brokerCandleExpires = msg.candle_expires_at;
      // The prediction payload is the same shape as `eoc` — apply it
      // (idempotent if eoc already did, since applyPrediction dedups).
      if (msg.prediction) applyPrediction(msg.prediction);
      break;

    case 'tick':
      // Ignore ticks for chart rendering until the real snapshot for this
      // selection has landed (see _awaitingSnapshot) — otherwise a single
      // early tick paints one bar on an otherwise-empty chart, looking like
      // most of the history is missing until the snapshot catches up.
      if (msg.candle && !_awaitingSnapshot) {
        document.getElementById('chart-loading').classList.add('hidden');
        lastDataAt = Date.now();
        showNoData(false);
        _setTarget(msg.candle, perfNow);
        // Update the topbar live price ticker with the candle's current close
        _updateTicker(msg.candle.close, msg.asset);
      }
      if (msg.prediction) {
        applyPrediction(msg.prediction);
      }
      if (msg.running_conf !== undefined) {
        showRunningConf(msg.running_conf);
      }
      if (msg.micro) {
        renderMicro(msg.micro);
      }
      break;
  }
}

// ── Data handlers ──────────────────────────────────────────────────────────
function applySnapshot(candles, prediction) {
  // Set unconditionally, before any early return below — this is the
  // authoritative reply for the current selection either way (even an
  // empty history is a real answer, not a reason to keep gating ticks).
  _awaitingSnapshot = false;
  if (!mainSeries || !predSeries || !chart) return;   // chart never booted — nothing to draw into

  const loadEl = document.getElementById('chart-loading');

  if (!candles || !candles.length) {
    mainSeries.setData([]);
    predSeries.setData([]);
    _resetRaf(null);
    _clearKLLines();
    const txt = loadEl.querySelector('.loading-text');
    if (txt) txt.textContent = 'Waiting for live data…';
    return;
  }

  loadEl.classList.add('hidden');

  let valid = candles.filter(c => c.open > 0 && c.high > 0 && c.low > 0 && c.close > 0);
  if (!valid.length) return;

  valid.sort((a, b) => a.time - b.time);
  const dedup = [];
  for (const c of valid) {
    if (dedup.length && dedup[dedup.length - 1].time === c.time) {
      dedup[dedup.length - 1] = c;
    } else {
      dedup.push(c);
    }
  }
  valid = dedup;

  mainSeries.setData(valid.map(toBar));
  predSeries.setData([]);
  lastPrediction = null;

  _resetRaf(valid[valid.length - 1]);

  if (prediction) applyPrediction(prediction);

  // Only auto-scroll to the latest candle if the user hasn't manually
  // zoomed/panned since the last pair/timeframe switch — applySnapshot runs
  // on every new candle close, not just the initial load, so unconditionally
  // scrolling here used to yank a deliberately-zoomed view back to real-time
  // the moment the next signal arrived.
  if (!_userTouchedChart) chart.timeScale().scrollToRealTime();
}

function applyPrediction(pred) {
  if (!pred) {
    if (predSeries) predSeries.setData([]);
    lastPrediction = null;
    _clearKLLines();
    return;
  }
  if (!pred.candle || pred.signal === 'NEUTRAL') {
    // NEUTRAL is a real verdict now (dead band / parrot guard produce it on
    // ~half of candles since the 2026-07 bias rework) — show it explicitly
    // instead of leaving the PREVIOUS candle's stale signal on the bar,
    // which read as "the app stopped giving signals". No ghost candle and
    // lastPrediction stays null (NEUTRAL is never graded / entry-timed),
    // but the badge, score, reasons and key levels all update.
    if (predSeries) predSeries.setData([]);
    lastPrediction = null;
    if (pred.key_levels || pred.wick_walls) {
      _setKLLines(pred.key_levels, pred.wick_walls);
    } else {
      _clearKLLines();
    }
    updateSignalUI(pred);
    return;
  }
  lastPrediction = pred;

  if (!userPrefs.ghost) {
    // Ghost candle switched off (Settings preference) — everything else
    // (badge, reasons, levels) still renders below.
    predSeries.setData([]);
    if (pred.key_levels || pred.wick_walls) _setKLLines(pred.key_levels, pred.wick_walls);
    updateSignalUI(pred);
    return;
  }

  const isCall = pred.signal === 'CALL';
  predSeries.applyOptions({
    upColor:         isCall ? 'rgba(0, 230, 118, 0.18)' : 'rgba(255, 23, 68, 0.18)',
    downColor:       isCall ? 'rgba(0, 230, 118, 0.18)' : 'rgba(255, 23, 68, 0.18)',
    borderUpColor:   isCall ? 'rgba(0, 230, 118, 0.40)' : 'rgba(255, 23, 68, 0.40)',
    borderDownColor: isCall ? 'rgba(0, 230, 118, 0.40)' : 'rgba(255, 23, 68, 0.40)',
    wickUpColor:     isCall ? 'rgba(0, 230, 118, 0.35)' : 'rgba(255, 23, 68, 0.35)',
    wickDownColor:   isCall ? 'rgba(0, 230, 118, 0.35)' : 'rgba(255, 23, 68, 0.35)',
  });

  predSeries.setData([toBar(pred.candle)]);

  // Draw key levels from the prediction (computed at EOC time)
  if (pred.key_levels || pred.wick_walls) _setKLLines(pred.key_levels, pred.wick_walls);

  updateSignalUI(pred);
}

function showAccuracy(result) {
  const wrap  = document.getElementById('accuracy-wrap');
  const label = document.getElementById('accuracy-label');
  wrap.classList.remove('hidden', 'correct', 'wrong', 'draw');
  wrap.classList.add(result);
  label.textContent = result === 'correct' ? '✓ Correct'
                     : result === 'draw'    ? '– Draw'
                     : '✗ Wrong';
}

// ── Microstructure panel ───────────────────────────────────────────────────
function renderMicro(m) {
  const wrap    = document.getElementById('micro-wrap');
  const forming = document.getElementById('micro-forming');
  if (!m) { wrap.classList.add('hidden'); forming.classList.remove('hidden'); return; }

  wrap.classList.remove('hidden');
  forming.classList.add('hidden');

  // Buyer/Seller bar
  document.getElementById('micro-buy-pct').textContent  = `B ${m.buy_pct}%`;
  document.getElementById('micro-sell-pct').textContent = `S ${m.sell_pct}%`;
  document.getElementById('micro-bar-fill').style.width = `${m.buy_pct}%`;

  // Pressure tag
  const pEl = document.getElementById('micro-pressure');
  pEl.className = 'micro-tag';
  if (m.pressure === 'BUYER') {
    pEl.classList.add('buyer');
    pEl.textContent = `▲ Buyer Pressure (${m.buy_pct}%)`;
  } else if (m.pressure === 'SELLER') {
    pEl.classList.add('seller');
    pEl.textContent = `▼ Seller Pressure (${m.sell_pct}%)`;
  } else {
    pEl.classList.add('fight');
    pEl.textContent = `↔ Balanced / Fight`;
  }

  // Fight zone
  const fEl = document.getElementById('micro-fight');
  if (m.is_fight) {
    fEl.classList.remove('hidden');
    fEl.className = 'micro-tag fight';
    fEl.textContent = `⚡ Fight Zone (${m.crosses}x crosses)`;
  } else {
    fEl.classList.add('hidden');
  }

  // TRAP alert — extreme one-sided pressure signals liquidity exhaustion
  const trapEl = document.getElementById('micro-trap');
  if (trapEl) {
    const bp = m.buy_pct || 0;
    const isTrap = bp <= 22 || bp >= 78;
    if (isTrap) {
      trapEl.classList.remove('hidden');
      if (bp <= 22) {
        trapEl.className = 'micro-tag trap-bear';
        trapEl.textContent = '⚠ BEAR TRAP — Sellers exhausted';
      } else {
        trapEl.className = 'micro-tag trap-bull';
        trapEl.textContent = '⚠ BULL TRAP — Buyers exhausted';
      }
    } else {
      trapEl.classList.add('hidden');
    }
  }

  // Reaction
  const rEl = document.getElementById('micro-reaction');
  if (m.reaction === 'BUYER') {
    rEl.classList.remove('hidden');
    rEl.className = 'micro-tag reaction-buyer';
    rEl.textContent = '↑ Buyer Reaction (bounced from low)';
  } else if (m.reaction === 'SELLER') {
    rEl.classList.remove('hidden');
    rEl.className = 'micro-tag reaction-seller';
    rEl.textContent = '↓ Seller Reaction (fell from high)';
  } else {
    rEl.classList.add('hidden');
  }

  // Phase arrows
  const arrows = { UP: '↑', DOWN: '↓', FLAT: '–' };
  const cls    = { UP: 'up', DOWN: 'down', FLAT: 'flat' };
  (m.phases || []).forEach((ph, i) => {
    const el = document.getElementById(`micro-phase-${i}`);
    if (!el) return;
    el.textContent = arrows[ph] || '–';
    el.className   = `micro-phase ${cls[ph] || 'flat'}`;
  });

  // Last-tick recovery / exhaustion
  const lrEl = document.getElementById('micro-last-react');
  if (m.last_react === 'RECOVERY') {
    lrEl.classList.remove('hidden');
    lrEl.className   = 'micro-tag recovery';
    lrEl.textContent = '↩ Final Recovery (defense held)';
  } else if (m.last_react === 'EXHAUST') {
    lrEl.classList.remove('hidden');
    lrEl.className   = 'micro-tag exhaust';
    lrEl.textContent = '⚡ Final Exhaustion (capital spent)';
  } else {
    lrEl.classList.add('hidden');
  }

  // Round number proximity
  const rnEl = document.getElementById('micro-round');
  const ri = m.round;
  if (ri && (ri.near_level || ri.hi_level || ri.lo_level)) {
    let txt = '', extra = '';
    if (ri.near_level) {
      extra = ri.near_strength === 'BIG' ? ' rnd-big' : ' rnd-mid';
      txt = `⊙ ${ri.near_strength} ${fmtRnd(ri.near_level)}`;
    } else if (ri.hi_level) {
      extra = ri.hi_strength === 'BIG' ? ' rnd-big' : ' rnd-mid';
      txt = `↑ Hi@${fmtRnd(ri.hi_level)} ${ri.hi_strength}`;
    } else if (ri.lo_level) {
      extra = ri.lo_strength === 'BIG' ? ' rnd-big' : ' rnd-mid';
      txt = `↓ Lo@${fmtRnd(ri.lo_level)} ${ri.lo_strength}`;
    }
    rnEl.classList.remove('hidden');
    rnEl.className   = `micro-tag${extra}`;
    rnEl.textContent = txt;
  } else {
    rnEl.classList.add('hidden');
  }

  // Hold zone
  document.getElementById('micro-hold-price').textContent =
    m.hold_price ? m.hold_price.toString() : '–';
}

function fmtRnd(level) {
  if (level < 10)   return level.toFixed(4);
  if (level < 1000) return level.toFixed(2);
  return level.toFixed(0);
}

function showRunningConf(conf) {
  const el = document.getElementById('running-conf');
  if (!el) return;
  if (!conf || !lastPrediction) {
    el.className = 'running-conf hidden';
    return;
  }
  el.classList.remove('hidden', 'confirming', 'opposing');
  if (conf === 'CONFIRMING') {
    el.classList.add('confirming');
    el.textContent = '▶ Confirming';
  } else {
    el.classList.add('opposing');
    el.textContent = '◀ Opposing';
  }
}

// ── Signal UI ──────────────────────────────────────────────────────────────
// Tracks the last signal+strength shown on the DESKTOP badge specifically,
// so the pop animation (see updateSignalUI) fires only on a genuine change
// — 'tick' WS messages can carry a re-anchored prediction with the SAME
// signal/strength as the candle evolves intra-candle, and popping on every
// one of those would read as constant pulsing rather than "new signal".
let _lastSignalKey = null;

// Broker-locked candle timing — captured from the `signal_start` WS message
// (server.py broadcasts these on every candle close). Used by tickCountdown
// and updateEntryTiming so the countdown is locked to the actual broker
// candle that the signal was issued for, not a free-running local modulo.
// When null, the chart falls back to local-clock modulo arithmetic.
let _brokerCandleOpen    = 0;
let _brokerCandleExpires = 0;

// Update the next time the broker is expected to close the current candle.
// Called from `signal_start` handler with the authoritative broker value.

function updateSignalUI(pred) {
  const bar   = document.getElementById('signal-bar');
  const badge = document.getElementById('signal-badge');
  const score = document.getElementById('signal-score');
  const conf  = document.getElementById('signal-conf');
  const list  = document.getElementById('reasons-list');

  bar.classList.remove('hidden');
  const isNeutral = pred.signal === 'NEUTRAL';
  const strength  = pred.strength || 'WEAK';
  badge.className = `signal-badge ${pred.signal.toLowerCase()}` +
                    (isNeutral ? '' : ` str-${strength.toLowerCase()}`);

  const signalKey = `${pred.signal}:${strength}`;
  if (signalKey !== _lastSignalKey) {
    _lastSignalKey = signalKey;
    // className was just reassigned above (without signal-pop), so adding
    // it now is always a fresh add and the animation reliably (re)plays.
    badge.classList.add('signal-pop');
    setTimeout(() => badge.classList.remove('signal-pop'), 300);
  }
  // Direction and strength are separate child spans (not one text string)
  // so they can be styled independently — a bold direction word plus a
  // small strength chip beside it, instead of one flat run of text.
  // className was reassigned above, which does NOT touch these children.
  const dirEl = badge.querySelector('.signal-dir');
  const strEl = badge.querySelector('.signal-strength');
  if (isNeutral) {
    if (dirEl) dirEl.textContent = '– NO TRADE';
    if (strEl) strEl.textContent = '';
  } else {
    if (dirEl) dirEl.textContent = pred.signal === 'CALL' ? '▲ CALL' : '▼ PUT';
    if (strEl) strEl.textContent = strength;
  }

  const agreeCount = pred.agree || 0;
  score.textContent = isNeutral
    ? `Score ${pred.score > 0 ? '+' : ''}${pred.score || 0}  ·  no clear edge — skip this candle`
    : `Score ${pred.score > 0 ? '+' : ''}${pred.score}  ·  ${agreeCount} theor${agreeCount === 1 ? 'y' : 'ies'} agree`;

  const confPct = isNeutral ? 0 : Math.round((pred.confidence || 0) * 100);
  conf.textContent = isNeutral ? 'Waiting for real evidence' : `Confidence ${confPct}%`;
  conf.title = 'Signal intensity (how strongly theories agree) — not a measured win probability.';

  // Confidence bar
  const bar2 = document.getElementById('signal-conf-bar');
  if (bar2) {
    bar2.style.width = `${confPct}%`;
    bar2.className   = 'conf-bar' +
      (isNeutral ? '' : ` ${pred.signal === 'CALL' ? 'call' : 'put'}`);
  }

  // Entry timing update
  updateEntryTiming();

  // Regime badge (trend + zone context)
  const regimeBadge = document.getElementById('regime-badge');
  if (regimeBadge) {
    const rg = pred.regime;
    if (rg && rg.trend) {
      const trendClass = rg.trend === 'UPTREND' ? 'uptrend'
                       : rg.trend === 'DOWNTREND' ? 'downtrend' : 'sideways';
      const zoneClass  = rg.zone === 'SUPPORT' ? 'zone-sup'
                       : rg.zone === 'RESISTANCE' ? 'zone-res' : 'zone-mid';
      regimeBadge.className = `regime-badge ${trendClass} ${zoneClass}`;
      const icon = rg.trend === 'UPTREND' ? '▲' : rg.trend === 'DOWNTREND' ? '▼' : '↔';
      regimeBadge.textContent = `${icon} ${rg.trend} · ${rg.zone}`;
    } else {
      regimeBadge.classList.add('hidden');
    }
  }

  // RUNCONF badge (Method B strength gate, 2026-07-10, untested)
  const runconfBadge = document.getElementById('runconf-badge');
  if (runconfBadge) {
    if (pred._runconf_tag) {
      runconfBadge.textContent = pred._runconf_tag === 'RUNCONF_UP'
        ? '↑ Strength upgraded' : '↓ Strength demoted';
      runconfBadge.className = 'runconf-badge ' +
        (pred._runconf_tag === 'RUNCONF_UP' ? 'up' : 'down');
      runconfBadge.classList.remove('hidden');
    } else {
      runconfBadge.classList.add('hidden');
    }
  }

  // (micro-zigzag element removed 2026-08-17 — was a leftover from a
  // deleted predictor. No-op here so the rest of the render path runs.)

  // EOC summary line (sidebar, above the reasons list)
  const summaryEl = document.getElementById('eoc-summary');
  if (summaryEl) {
    summaryEl.className = `eoc-summary ${pred.signal.toLowerCase()}`;
    summaryEl.textContent =
      `${pred.signal === 'CALL' ? '▲ CALL' : pred.signal === 'PUT' ? '▼ PUT' : '– NEUTRAL'} ` +
      `${strength}  ·  score ${pred.score > 0 ? '+' : ''}${pred.score}  ·  ${agreeCount} agree`;
  }

  // Reasons list
  list.innerHTML = '';
  const reasons = pred.reasons || [];
  if (!reasons.length) {
    const li = document.createElement('li');
    li.className = 'empty'; li.textContent = 'No signals fired';
    list.appendChild(li);
  } else {
    for (const r of reasons) {
      const li = document.createElement('li');
      li.textContent = r;
      li.className   = r.includes('CALL') ? 'call' : r.includes('PUT') ? 'put' : '';
      list.appendChild(li);
    }
  }

  // Deep-analysis market state card — sidebar panel only (Signals tab
  // mirror removed along with the tab itself).
  renderMarketState(pred.market_state);
}

// ── Deep Analysis card — market-state read from analyze_eoc ─────────────────
// Informational layer (continuation/exhaustion/reversal/trap/range). Every
// client receives market_state now — the old _tier_payload tier system was
// removed when auth was ripped out.
const _MSTATE_LABELS = {
  CONTINUATION: { icon: '➜', name: 'Continuation' },
  EXHAUSTION:   { icon: '⏳', name: 'Exhaustion' },
  REVERSAL:     { icon: '⤾', name: 'Reversal' },
  TRAP:         { icon: '⚠', name: 'Trap' },
  RANGE:        { icon: '↔', name: 'Range' },
  UNCLEAR:      { icon: '·', name: 'Unclear' },
};

function renderMarketState(ms, prefix = 'mstate') {
  const card = document.getElementById(`${prefix}-card`);
  if (!card) return;
  const chip  = document.getElementById(`${prefix}-chip`);
  const bias  = document.getElementById(`${prefix}-bias`);
  const meter = document.getElementById(`${prefix}-meter`);
  const evUl  = document.getElementById(`${prefix}-evidence`);
  const bars  = document.getElementById(`${prefix}-bars`);

  if (!ms || !ms.state) {
    card.classList.add('hidden');
    return;
  }
  card.classList.remove('hidden');

  const lbl = _MSTATE_LABELS[ms.state] || _MSTATE_LABELS.UNCLEAR;
  chip.className  = `mstate-chip ms-${ms.state.toLowerCase()}`;
  chip.textContent = `${lbl.icon} ${lbl.name}`;

  if (ms.bias === 'CALL' || ms.bias === 'PUT') {
    bias.className   = `mstate-bias ${ms.bias.toLowerCase()}`;
    bias.textContent = (ms.bias === 'CALL' ? '▲' : '▼') + ` leans ${ms.bias}`;
  } else {
    bias.className   = 'mstate-bias';
    bias.textContent = 'no lean';
  }

  const conv = Math.max(0, Math.min(100, ms.conviction || 0));
  meter.style.width = `${conv}%`;
  meter.className   = `mstate-meter ms-${ms.state.toLowerCase()}`;
  meter.parentElement.title =
    `Evidence share: ${conv}% of structural points landed on this state — not a win probability.`;

  evUl.innerHTML = '';
  for (const e of (ms.evidence || [])) {
    const li = document.createElement('li');
    li.textContent = e;
    evUl.appendChild(li);
  }

  // Compact per-state point bars — how the evidence split across all 5 reads.
  bars.innerHTML = '';
  const pts = ms.points || {};
  const maxPt = Math.max(1, ...Object.values(pts));
  for (const k of ['TRAP', 'REVERSAL', 'EXHAUSTION', 'CONTINUATION', 'RANGE']) {
    if (!(k in pts)) continue;
    const row = document.createElement('div');
    row.className = 'mstate-bar-row' + (k === ms.state ? ' active' : '');
    const name = document.createElement('span');
    name.className = 'mstate-bar-name';
    name.textContent = (_MSTATE_LABELS[k] || {}).name || k;
    const track = document.createElement('div');
    track.className = 'mstate-bar-track';
    const fill = document.createElement('div');
    fill.className = `mstate-bar-fill ms-${k.toLowerCase()}`;
    fill.style.width = `${Math.round(100 * (pts[k] || 0) / maxPt)}%`;
    track.appendChild(fill);
    const val = document.createElement('span');
    val.className = 'mstate-bar-val';
    val.textContent = pts[k];
    row.appendChild(name); row.appendChild(track); row.appendChild(val);
    bars.appendChild(row);
  }
}

// ── Signal popup helpers — badge/meta text formatting shared between the
// auto-popup and the signal-bar. Kept simple and self-contained. ──────────
function _fmtSignalBadge(pred) {
  const strength = pred.strength || 'WEAK';
  const tag = strength === 'STRONG' ? '★ ' : strength === 'WEAK' ? '· ' : '';
  return {
    cls:  `signal-badge ${pred.signal.toLowerCase()} str-${strength.toLowerCase()}`,
    text: (pred.signal === 'CALL' ? '▲ CALL' : '▼ PUT') + `  ${tag}${strength}`,
  };
}
function _fmtSignalMeta(pred) {
  const agreeCount = pred.agree || 0;
  return `Score ${pred.score > 0 ? '+' : ''}${pred.score}  ·  ${agreeCount} theor${agreeCount === 1 ? 'y' : 'ies'} agree`;
}

let _popupTimer = null;

function _showSignalPopup(pred) {
  if (!pred || pred.signal === 'NEUTRAL') return;
  if (!userPrefs.popup) return;
  const popup = document.getElementById('signal-popup');
  const badge = document.getElementById('signal-popup-badge');
  const meta  = document.getElementById('signal-popup-meta');
  if (!popup || !badge || !meta) return;
  const { cls, text } = _fmtSignalBadge(pred);
  badge.className   = cls;
  badge.textContent = text;
  meta.textContent  = _fmtSignalMeta(pred);
  popup.classList.remove('hidden');
  clearTimeout(_popupTimer);
  _popupTimer = setTimeout(_hideSignalPopup, 7000);
  badge.classList.add('signal-pop');
  setTimeout(() => badge.classList.remove('signal-pop'), 300);
}

function _hideSignalPopup() {
  const popup = document.getElementById('signal-popup');
  if (popup) popup.classList.add('hidden');
  clearTimeout(_popupTimer);
}

// ── Signal history mini-list — sidebar's "Recent Signals" panel ────────────
// Fetches the last 5 resolved signals from /api/signals and renders compact
// cards. Called on boot and after every EOC (candle close) to stay current.
let _lastHistoryMiniKey = '';
// ── Visibility-aware polling ────────────────────────────────────────────────
// Every poll timer below used to keep firing while the tab sat in the
// background, which is how an idle app still produced ~95k requests/day.
// _pollGuard skips the network call when the tab is hidden and fires one
// immediate catch-up refresh the moment it becomes visible again, so coming
// back to the tab still feels instant.
const _visibilityCatchUp = [];
function _pollGuard(fn) {
  _visibilityCatchUp.push(fn);
  return () => {
    if (document.hidden) return;   // tab backgrounded: skip this tick
    fn();
  };
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  for (const fn of _visibilityCatchUp) {
    try { fn(); } catch (e) { console.error('catch-up poll failed', e); }
  }
});

async function _renderSignalHistoryMini() {
  const wrap = document.getElementById('signal-history-mini');
  if (!wrap) return;
  try {
    const data = await fetch('/api/signals?limit=5').then((r) => r.json());
    if (!Array.isArray(data) || !data.length) {
      wrap.innerHTML = '<div class="mini-empty">No signals yet</div>';
      return;
    }
    // Dedup key — skip re-render if the latest signal hasn't changed
    const key = data.map(s => `${s.ctime}-${s.asset}`).join('|');
    if (key === _lastHistoryMiniKey) return;
    _lastHistoryMiniKey = key;

    wrap.innerHTML = data.map((s) => {
      const sigCls = (s.signal || 'neutral').toLowerCase();
      const resCls = s.result || '';
      const resIcon = s.result === 'correct' ? '✓'
                    : s.result === 'draw'    ? '–'
                    : s.result === 'wrong'   ? '✗' : '·';
      const time = new Date(s.ctime * 1000).toLocaleTimeString(
        [], { hour: '2-digit', minute: '2-digit' });
      const pair = _fmtPairLabel(s.asset || '');
      return `<div class="mini-signal ${sigCls}">
        <div class="mini-signal-top">
          <span class="mini-signal-dir ${sigCls}">${s.signal || '–'}</span>
          <span class="mini-signal-pair">${pair}</span>
          <span class="mini-signal-result ${resCls}">${resIcon}</span>
        </div>
        <div class="mini-signal-bottom">
          <span class="mini-signal-time">${time}</span>
          <span class="mini-signal-strength">${s.strength || '–'}</span>
        </div>
      </div>`;
    }).join('');
  } catch (_) {
    // keep whatever was last rendered
  }
}

// ── Share Signal table — live signal table for all 16 pairs ────────────────
// Fetches /api/share-signals and renders one row per pair in #share-signal-rows.
// Columns: Pair | Type | Time | Buyer% | Seller% | Signal | Prediction Candle.
// Polled every 10s while the Share Signal tab is active (see _setActiveTab).
let _sharePollTimer = null;
let _lastShareKey = '';

async function _loadShareSignals() {
  const tbody = document.getElementById('share-signal-rows');
  if (!tbody) return;
  try {
    const r = await fetch('/api/share-signals');
    if (!r.ok) return;
    const data = await r.json();
    const signals = data.signals || [];
    // Backend always returns 16 rows (one per wanted pair). The empty state
    // we actually care about is "no row has a real signal yet" — that means
    // the feed hasn't closed a candle on any pair yet (cold start).
    const hasAnySignal = signals.some(s => s.signal === 'CALL' || s.signal === 'PUT');
    if (!signals.length || !hasAnySignal) {
      tbody.innerHTML = '<tr><td colspan="7" class="history-empty">Waiting for first candle close — connect Quotex in Settings to start streaming.</td></tr>';
      _lastShareKey = '';  // reset so the next real update re-renders
      return;
    }
    // Dedup — skip re-render if nothing changed. Include strength + confidence
    // + prediction_candle.close so a strength upgrade (WEAK→STRONG) or a new
    // ghost candle re-renders even when buy_pct/signal are unchanged.
    const key = signals.map(s =>
      `${s.asset}:${s.signal || ''}:${s.strength || ''}:${s.time || ''}:${s.buy_pct ?? ''}:${s.prediction_candle?.close ?? ''}`
    ).join('|');
    if (key === _lastShareKey) return;
    _lastShareKey = key;

    tbody.innerHTML = signals.map((s) => {
      const sigCls = (s.signal || 'neutral').toLowerCase();
      const rowCls = s.signal === 'CALL' ? 'row-call' : s.signal === 'PUT' ? 'row-put' : '';
      const typeBadge = s.type === 'otc'
        ? '<span class="ss-type-badge otc">OTC</span>'
        : '<span class="ss-type-badge real">Real</span>';
      // Time formatting
      const timeStr = s.time
        ? new Date(s.time * 1000).toLocaleTimeString('en-GB', {
            hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'
          })
        : '–';
      // Buyer/seller pressure
      const buyPct = s.buy_pct != null ? s.buy_pct : '–';
      const sellPct = s.sell_pct != null ? s.sell_pct : '–';
      const buyBar = s.buy_pct != null
        ? `<span class="ss-pressure">
             <span class="ss-pressure-bar"><span class="ss-pressure-bar-fill buyer" style="width:${s.buy_pct}%"></span></span>
             <span class="ss-pressure-pct buyer">${s.buy_pct}%</span>
           </span>`
        : '<span class="ss-pressure-pct">–</span>';
      const sellBar = s.sell_pct != null
        ? `<span class="ss-pressure">
             <span class="ss-pressure-bar"><span class="ss-pressure-bar-fill seller" style="width:${s.sell_pct}%"></span></span>
             <span class="ss-pressure-pct seller">${s.sell_pct}%</span>
           </span>`
        : '<span class="ss-pressure-pct">–</span>';
      // Signal badge
      const sigBadge = s.signal
        ? `<span class="ss-signal-badge ${sigCls}">${s.signal === 'CALL' ? '▲ CALL' : s.signal === 'PUT' ? '▼ PUT' : s.signal}</span>`
        : '<span class="ss-signal-badge neutral">–</span>';
      // Prediction candle
      let predCandle = '–';
      if (s.prediction_candle && s.prediction_candle.open != null) {
        const pc = s.prediction_candle;
        const fmt = (v) => v >= 100 ? v.toFixed(2) : v >= 1 ? v.toFixed(5) : v.toFixed(8);
        predCandle = `<span class="ss-pred-candle">` +
                     `<span class="pc-o">O:${fmt(pc.open)}</span> ` +
                     `<span class="pc-h">H:${fmt(pc.high)}</span> ` +
                     `<span class="pc-l">L:${fmt(pc.low)}</span> ` +
                     `<span class="pc-c">C:${fmt(pc.close)}</span>` +
                     `</span>`;
      }

      return `<tr class="${rowCls}">
        <td class="col-pair">${s.display || s.asset}</td>
        <td class="col-type">${typeBadge}</td>
        <td class="col-time">${timeStr}</td>
        <td class="col-buyer">${buyBar}</td>
        <td class="col-seller">${sellBar}</td>
        <td class="col-signal">${sigBadge}</td>
        <td class="col-pred">${predCandle}</td>
      </tr>`;
    }).join('');
  } catch (_) {
    // keep whatever was last rendered
  }
}

// ── Live price ticker — topbar real-time price display ─────────────────────
// Updated on every tick message. Flashes green/red based on direction vs
// the previous tick. Hidden until the first real price arrives.
let _lastTickerPrice = 0;
function _updateTicker(price, asset) {
  const ticker  = document.getElementById('live-price-ticker');
  const tPair   = document.getElementById('ticker-pair');
  const tPrice  = document.getElementById('ticker-price');
  if (!ticker || !tPair || !tPrice) return;
  if (!price || price <= 0) return;

  // Format price — strip trailing zeros, show 5 decimal places for forex
  const formatted = price >= 100 ? price.toFixed(2)
                   : price >= 1   ? price.toFixed(5)
                                  : price.toFixed(8);

  // Flash direction — green if price went up, red if down
  if (_lastTickerPrice > 0 && price !== _lastTickerPrice) {
    const up = price > _lastTickerPrice;
    tPrice.classList.remove('flash-up', 'flash-down');
    void tPrice.offsetWidth;  // force reflow to restart animation
    tPrice.classList.add(up ? 'flash-up' : 'flash-down');
  }
  _lastTickerPrice = price;

  // Pair label — strip _otc suffix and pretty-print the conventional
  // XXXYYY pair code as XXX/YYY. The previous naive `.replace('_','/')`
  // had no underscore to match after stripping _otc, so EURUSD_otc
  // rendered as the raw "EURUSD" rather than the user-friendly "EUR/USD".
  const pairLabel = _fmtPairLabel(asset || currentAsset);
  tPair.textContent  = pairLabel;
  tPrice.textContent = formatted;
  ticker.classList.remove('hidden');
}

// ── Sidebar pair list — compact pair list in the sidebar ────────────────────
// Rendered from the same pairsList that loadPairs() populates. Clicking a
// pair switches to it (same as the topbar pair-btn dropdown).
function _renderSidebarPairs() {
  const list  = document.getElementById('sidebar-pair-list');
  const count = document.getElementById('sidebar-pairs-count');
  if (!list) return;
  if (count) count.textContent = pairsList.length || '–';
  if (!pairsList.length) {
    list.innerHTML = '<div class="sidebar-pair-empty">No pairs</div>';
    return;
  }
  list.innerHTML = pairsList.map((p) => {
    const isActive = p.asset === currentAsset;
    const statusDot = p.status === 'live' ? 'live'
                    : p.status === 'otc'  ? 'otc'
                    : 'closed';
    const lockIcon = p.locked ? '🔒' : '';
    const pay = p.payout ? `${p.payout}%` : '–';
    // Countdown slot — updated live by _updateSidebarCountdowns()
    return `<button type="button" class="sidebar-pair-row ${isActive ? 'active' : ''}"
            data-asset="${p.asset}">
      <span class="sidebar-pair-dot ${statusDot}"></span>
      <span class="sidebar-pair-name">${p.display}</span>
      <span class="sidebar-pair-cd" data-asset="${p.asset}">--</span>
      <span class="sidebar-pair-pay">${pay}</span>
      ${lockIcon ? `<span class="sidebar-pair-lock">${lockIcon}</span>` : ''}
    </button>`;
  }).join('');

  // Wire click handlers
  list.querySelectorAll('.sidebar-pair-row').forEach((row) => {
    row.addEventListener('click', () => {
      const asset = row.dataset.asset;
      if (!asset || asset === currentAsset) return;
      const pair = pairsList.find(p => p.asset === asset);
      if (!pair || pair.locked) return;
      currentAsset = asset;
      _savePairPrefs();
      _updatePairBtn();
      _renderSidebarPairs();  // update active highlight
      resetAndSubscribe();
    });
  });
}

// Update the per-pair countdown seconds in the sidebar list. Called from
// tickCountdown (4×/s) so every pair's "time to next candle close" stays
// live. All pairs share the same period (60s default), so the countdown
// value is the same for all — but displaying it per-row lets the user see
// at a glance when the next candle arrives for any pair.
function _updateSidebarCountdowns() {
  const nowSec = _brokerNowSec();
  const left = currentPeriod - (Math.floor(nowSec) % currentPeriod);
  const cls  = left <= 5 ? 'danger' : left <= 15 ? 'warn' : '';
  const txt  = left + 's';
  document.querySelectorAll('.sidebar-pair-cd').forEach((el) => {
    el.textContent = txt;
    el.className = 'sidebar-pair-cd ' + cls;
  });
}


// ── Controls ───────────────────────────────────────────────────────────────
async function loadPairs() {
  try {
    const data = await fetch('/api/pairs').then((r) => r.json());
    pairsList = data.pairs || [];
    if (typeof data.payout_floor === 'number') payoutFloor = data.payout_floor;
  } catch (_) {
    // FIX: fallback pairs must be REAL variants (EURUSD/GBPUSD/USDJPY
    // are NOT _otc per _WANTED_PAIRS in feed.py). The old fallback listed
    // EURUSD_otc / GBPUSD_otc / USDJPY_otc — none of those exist in
    // _VALID_ASSETS — so /api/subscribe returned {"ok":false,"status":"invalid"}
    // and the chart was stuck on "No data" until the real /api/pairs fetch
    // succeeded. Now we use the actual real pair codes.
    pairsList = [
      { asset: 'EURUSD', display: 'EUR/USD', status: 'real', payout: null, locked: false },
      { asset: 'GBPUSD', display: 'GBP/USD', status: 'real', payout: null, locked: false },
      { asset: 'USDJPY', display: 'USD/JPY', status: 'real', payout: null, locked: false },
    ];
  }
  renderPairSelect();
}

// ── Pair picker (custom dropdown: button + fixed panel w/ search) ──────────
// Replaces the old native <select> + separate search input. The panel is
// position:fixed at body level because #controls scrolls horizontally
// (overflow-x:auto) and would clip an absolute child.
// Server only sends forex pairs, already sorted active-before-closed,
// unlocked-before-locked, highest payout first.
function renderPairSelect() {
  // Keep the selection valid (list refreshes every 5 min: payouts drift,
  // real/otc codes swap at market open/close).
  const has = pairsList.some((p) => p.asset === currentAsset && p.status !== 'closed' && !p.locked);
  if (!has) {
    const first = pairsList.find((p) => p.status !== 'closed' && !p.locked);
    currentAsset = first?.asset || pairsList[0]?.asset || currentAsset;
  }
  _updatePairBtn();
  _updateMktBadge();
  _renderPairRows();
}

function _updatePairBtn() {
  const p = pairsList.find((x) => x.asset === currentAsset);
  // Only one pair button now (#pair-btn) — Signals tab was removed.
  const label = document.getElementById('pair-btn-label');
  if (label) {
    if (!p) {
      label.textContent = currentAsset;
    } else {
      const pay = typeof p.payout === 'number' ? ` · ${p.payout}%` : '';
      label.innerHTML =
        (p.status === 'live' ? '<span class="pair-live-dot">●</span> ' : '') +
        `${p.display} <span class="pair-btn-sub">${p.status === 'live' ? 'Real' : 'Otc'}${pay}</span>`;
    }
  }
  // Also update the sidebar pair list's active highlight
  _renderSidebarPairs();
}

function _payClass(p) {
  if (typeof p.payout !== 'number') return 'pr-pay-low';
  if (p.payout >= 90) return 'pr-pay-hi';
  if (!p.locked)      return 'pr-pay-ok';
  return 'pr-pay-low';
}

function _renderPairRows() {
  const ul = document.getElementById('pair-list');
  if (!ul) return;
  const term = pairSearchTerm.trim().toLowerCase();
  const shown = term
    ? pairsList.filter((p) =>
        p.display.toLowerCase().includes(term) || p.asset.toLowerCase().includes(term))
    : pairsList;
  ul.innerHTML = '';
  if (!shown.length) {
    ul.innerHTML = '<li class="pair-row pr-empty">No pair matches</li>';
    return;
  }
  for (const p of shown) {
    const li = document.createElement('li');
    const disabled = p.status === 'closed' || p.locked;
    li.className = 'pair-row'
      + (p.asset === currentAsset ? ' active' : '')
      + (disabled ? ' disabled' : '');
    const mkt = p.status === 'closed' ? 'closed' : p.status;
    li.innerHTML =
      `<span class="pr-name">${p.display}</span>` +
      `<span class="pr-badges">` +
        (p.locked ? `<span class="pr-lock">🔒 needs ${payoutFloor}%</span>` : '') +
        `<span class="pr-mkt ${mkt}">${p.status === 'live' ? 'Real' : p.status === 'otc' ? 'Otc' : 'Closed'}</span>` +
        (typeof p.payout === 'number' ? `<span class="pr-pay ${_payClass(p)}">${p.payout}%</span>` : '') +
      `</span>`;
    if (!disabled) {
      li.addEventListener('click', () => {
        if (p.asset !== currentAsset) {
          currentAsset = p.asset;
          _updatePairBtn();
          _updateMktBadge();
          resetAndSubscribe();
        }
        _closePairPanel();
      });
    }
    ul.appendChild(li);
  }
}

function _updateMktBadge() {
  const badge = document.getElementById('mkt-badge');
  if (!badge) return;
  const p  = pairsList.find((x) => x.asset === currentAsset);
  const st = p?.status || '';
  badge.className   = `mkt-badge ${st}`;
  badge.textContent = st === 'live' ? 'Real' : st === 'otc' ? 'Otc' : st === 'closed' ? 'CLOSED' : '';
  badge.classList.toggle('hidden', !st || st === 'unknown');
}

let _panelOpenWidth = 0;   // see the resize listener below — mobile-keyboard guard
let _panelOpenTrigger = null;   // whichever button opened it — see _closePairPanel

// There's only ever one (asset,period) subscription per browser tab
// server-side, so one panel/state serves the single #pair-btn trigger.
function _openPairPanel(triggerBtn) {
  const panel = document.getElementById('pair-panel');
  const rect  = triggerBtn.getBoundingClientRect();
  panel.style.left     = `${Math.max(8, Math.min(rect.left, window.innerWidth - 328))}px`;
  panel.style.top      = `${rect.bottom + 6}px`;
  panel.style.maxHeight = `${Math.max(180, window.innerHeight - rect.bottom - 20)}px`;
  panel.classList.remove('hidden');
  triggerBtn.classList.add('open');
  _panelOpenTrigger = triggerBtn;
  _renderPairRows();
  _panelOpenWidth = window.innerWidth;
  const search = document.getElementById('pair-search');
  search.value = pairSearchTerm = '';
  _renderPairRows();
  search.focus();
}

function _closePairPanel() {
  document.getElementById('pair-panel').classList.add('hidden');
  if (_panelOpenTrigger) _panelOpenTrigger.classList.remove('open');
  _panelOpenTrigger = null;
}

// Guarded wiring: if any picker element is missing (e.g. a stale
// index.html cached across a deploy paired with fresh chart.js), skip the
// picker instead of throwing at top level — an uncaught error here would
// kill the ENTIRE script (WS, chart, everything), which reads as "the app
// crashed" even though only one widget was unavailable.
(() => {
  const btn    = document.getElementById('pair-btn');
  const panel  = document.getElementById('pair-panel');
  const search = document.getElementById('pair-search');
  if (!btn || !panel || !search) return;

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (panel.classList.contains('hidden')) _openPairPanel(btn);
    else _closePairPanel();
  });
  // Only one pair button now — Signals tab was removed.
  panel.addEventListener('click', (e) => e.stopPropagation());
  document.addEventListener('click', () => _closePairPanel());
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') _closePairPanel();
  });
  // Real device rotation/window resize closes the panel (its position was
  // computed for the old layout) — but on mobile, focusing #pair-search
  // (right below, on open) pops the on-screen keyboard, which ALSO fires
  // a resize (viewport height shrinks, width doesn't). That used to close
  // the panel we'd just opened a moment earlier — looked exactly like the
  // app "wouldn't open the pair list" / "crashed" on phones (reported by
  // user 2026-07-06; a desktop/Playwright mobile-viewport test couldn't
  // catch it because emulation has no real virtual keyboard to trigger
  // the resize). Only close on a WIDTH change — that's an actual
  // rotation/resize; a keyboard never changes viewport width.
  window.addEventListener('resize', () => {
    if (window.innerWidth !== _panelOpenWidth) _closePairPanel();
  });
  search.addEventListener('input', (e) => {
    pairSearchTerm = e.target.value;
    _renderPairRows();
  });
})();

// ── Tab navigation — 4-tab layout (Home / Chart / History / Settings) ──────
// Declared as `let` (not `function`) so the token-status polling wrapper
// below can reassign it to add Settings-tab polling without throwing in
// strict mode.
let _setActiveTab = function(tab) {
  _activeTab = tab;
  try { localStorage.setItem('plybit_tab', tab); } catch (_) {}

  // Delegate page switching to nav.js (sidebar + bottom tabs stay in sync)
  if (typeof Nav !== 'undefined' && Nav.setPage) {
    Nav.setPage(tab);
  } else {
    // Fallback if nav.js didn't load
    const _fallbackMap = { home: 'tab-home', chart: 'tab-advance',
                           history: 'tab-history', settings: 'tab-settings' };
    for (const [key, id] of Object.entries(_fallbackMap)) {
      const el = document.getElementById(id);
      if (!el) continue;
      el.classList.toggle('hidden', key !== tab);
    }
  }

  document.querySelectorAll('.tab-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  // The header-stats cluster is Chart-specific — hide on mobile for other tabs
  const stats = document.getElementById('header-stats');
  if (stats) stats.classList.toggle('hidden', tab !== 'chart');

  // When switching to History tab, auto-load the history table
  if (tab === 'history') {
    loadHistory();
  }
  // When switching to Home tab, load share-signals + home stats and start polling.
  // (Share-signal table now lives on the Home overview page, not its own tab.)
  if (tab === 'home') {
    _loadShareSignals();
    _loadHomeStats();
    if (_sharePollTimer) clearInterval(_sharePollTimer);
    _sharePollTimer = setInterval(_pollGuard(() => {
      _loadShareSignals();
      _loadHomeStats();
    }), 10000);  // 10s poll, paused while the tab is hidden
  } else {
    if (_sharePollTimer) {
      clearInterval(_sharePollTimer);
      _sharePollTimer = null;
    }
  }
}

// ── Home stats overview — populate the 4 quick-stat cards on the Home tab ──
// Pulled from /api/stream-status (pairs count, connection state) and
// /api/stats (overall win-rate). Cheap calls, run alongside share-signals.
async function _loadHomeStats() {
  try {
    const [ss, st] = await Promise.all([
      fetch('/api/stream-status').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/api/stats').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    const setStatus = document.getElementById('home-stat-status');
    const setPairs  = document.getElementById('home-stat-pairs');
    const setWR     = document.getElementById('home-stat-winrate');
    const setActive = document.getElementById('home-stat-active');
    if (ss) {
      // FIX: stream-status returns {active, count, max, cooldown_until, cooldown_reason}.
      // The old code read `ss.connected` / `ss.active_streams` which DON'T EXIST
      // in that response — so the Home tab always showed "Offline" + "0 / 16"
      // regardless of the actual feed state. The `active` field is the live
      // stream count, and `active > 0` is the de-facto "connected" indicator.
      const live = (ss.active ?? 0) > 0;
      if (setStatus) setStatus.textContent = live ? '● Live' : '● Offline';
      if (setStatus) setStatus.className = 'home-stat-value ' + (live ? 'live' : 'offline');
      if (setPairs)  setPairs.textContent = (ss.active ?? ss.count ?? 0) + ' / ' + (ss.max ?? pairsList.length ?? 16);
    }
    if (st) {
      // FIX: db.get_stats() returns {rate, total, correct, wrong, ...}.
      // The old code read `st.overall_winrate` / `st.winrate` which DON'T EXIST
      // — so the Home win-rate card always showed "–".
      const wr = (typeof st.rate === 'number' && st.total > 0) ? st.rate : null;
      if (setWR && wr != null) setWR.textContent = (wr * 100).toFixed(1) + '%';
      else if (setWR) setWR.textContent = '–';
    }
    // Active CALL/PUT signals count — read from the share-signal rows
    // rendered by _loadShareSignals (re-uses the same fetch result).
    const r = await fetch('/api/share-signals');
    if (r.ok) {
      const data = await r.json();
      const sigs = data.signals || [];
      const active = sigs.filter(s => s.signal === 'CALL' || s.signal === 'PUT').length;
      if (setActive) setActive.textContent = active + ' / ' + sigs.length;
    }
  } catch (_) {
    // silently — home stats are non-critical
  }
}

document.querySelectorAll('.tab-btn').forEach((b) => {
  b.addEventListener('click', () => _setActiveTab(b.dataset.tab));
});
// Expose _setActiveTab globally so nav.js (sidebar clicks) can route through
// it instead of bypassing the per-tab loaders. The `chart` global holds the
// LightweightCharts instance, so we can't hang this off `chart`.
//
// This MUST be a proxy, not a direct reference: _setActiveTab is later
// reassigned (see the token-status-polling wrapper below) to point at a
// new function object. Capturing `_setActiveTab` by value here would have
// frozen window._setActiveTab on the pre-wrap original forever — which is
// exactly what happened before this fix: nav.js's sidebar clicks (desktop,
// >=768px) call window._setActiveTab and so never started/stopped the
// Settings tab's 5s token-status poll, while bottom-tab clicks (mobile)
// call the bare identifier and picked up the wrapper correctly via normal
// closure lookup. The proxy re-reads the current `_setActiveTab` binding
// on every call, so both nav paths always get the latest wrapper.
window._setActiveTab = (tab) => _setActiveTab(tab);
// NOTE: do NOT call _setActiveTab(_activeTab) here — the token-status
// wrapper below does that once it's fully wired up. Calling it here too
// just fires every per-tab loader (share-signals, home-stats) twice on
// every page load for no benefit.

// ── Settings: Preferences card ← userPrefs (localStorage) ────────────────
for (const [id, key] of [['pref-popup', 'popup'], ['pref-klines', 'klines'],
                         ['pref-wwalls', 'wwalls'], ['pref-ghost', 'ghost']]) {
  const el = document.getElementById(id);
  if (!el) continue;
  el.checked = userPrefs[key];
  el.addEventListener('change', () => {
    userPrefs[key] = el.checked;
    _savePrefs();
    if (lastPrediction) applyPrediction(lastPrediction);
  });
}

// ── Settings: Quotex Connection card (session token) ─────────────────────
// Lets the operator paste an SSID cookie directly into the UI instead of
// redeploying on Railway every time the token expires. The token is sent
// to POST /api/token (stored in-memory server-side) and the connection
// status is polled from /api/token-status every 5s while on the Settings tab.
const tokenForm   = document.getElementById('token-form');
const tokenInput  = document.getElementById('token-input');
const tokenMsg    = document.getElementById('token-msg');
const tokenClear  = document.getElementById('token-clear-btn');
const tokenBadge  = document.getElementById('token-status-badge');
const tokenSource = document.getElementById('token-source-label');

if (tokenForm) {
  tokenForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const token = (tokenInput.value || '').trim();
    if (!tokenMsg) return;
    tokenMsg.className = 'pw-msg';
    if (!token) {
      tokenMsg.textContent = 'Paste a token first';
      tokenMsg.classList.add('err');
      return;
    }
    tokenMsg.textContent = 'Sending token…';
    try {
      const r = await fetch('/api/token', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ token }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        tokenMsg.textContent = d.message || 'Token stored — reconnecting…';
        tokenMsg.classList.add('ok');
        tokenInput.value = '';  // clear the textarea for security
        // The backend now starts connecting within milliseconds of this
        // POST, but the badge used to sit on the 5s poll and still read
        // "Waiting…" long after the socket was actually live. Burst-poll at
        // 1s for the next 30s so the UI reflects reality immediately.
        _startTokenBurstPoll();
        // Start polling for connection status
        _pollTokenStatus();
      } else {
        tokenMsg.textContent = d.error || 'Could not store token';
        tokenMsg.classList.add('err');
      }
    } catch (_) {
      tokenMsg.textContent = 'Network error — try again';
      tokenMsg.classList.add('err');
    }
  });
}

if (tokenClear) {
  tokenClear.addEventListener('click', async () => {
    if (tokenMsg) {
      tokenMsg.className = 'pw-msg';
      tokenMsg.textContent = 'Clearing…';
    }
    try {
      const r = await fetch('/api/token', { method: 'DELETE' });
      const d = await r.json().catch(() => ({}));
      if (tokenInput) tokenInput.value = '';
      if (tokenMsg) {
        tokenMsg.textContent = d.message || 'Token cleared.';
        tokenMsg.classList.add(d.cleared ? 'ok' : 'err');
      }
      _pollTokenStatus();  // refresh the status badge
    } catch (_) {
      if (tokenMsg) {
        tokenMsg.textContent = 'Network error — try again';
        tokenMsg.classList.add('err');
      }
    }
  });
}

// ── Token status polling ──────────────────────────────────────────────────
// Polls /api/token-status every 5s while the Settings tab is active, and
// updates the status badge. Stops polling when the user leaves the tab to
// avoid unnecessary requests. Hooked into _setActiveTab by wrapping the
// original function — we save a reference, redefine it, and call through.
let _tokenPollTimer = null;

async function _pollTokenStatus() {
  try {
    const r = await fetch('/api/token-status');
    if (!r.ok) return;
    const d = await r.json();
    _renderTokenStatus(d);
  } catch (_) {}
}

// Burst poll: 1s cadence for ~30s straight after a token paste, then the
// normal 5s tab poll takes back over. Without this the badge lagged the
// actual connection by up to 5s and made a fast connect look slow.
let _tokenBurstTimer = null;
function _startTokenBurstPoll() {
  if (_tokenBurstTimer) clearInterval(_tokenBurstTimer);
  let ticks = 0;
  _tokenBurstTimer = setInterval(async () => {
    ticks++;
    await _pollTokenStatus();
    if (ticks >= 30) {
      clearInterval(_tokenBurstTimer);
      _tokenBurstTimer = null;
    }
  }, 1000);
  _pollTokenStatus();
}

function _renderTokenStatus(d) {
  if (!tokenBadge) return;
  const connected = d.connected;
  const hasToken = d.has_user_token || d.has_env_token;
  if (connected) {
    tokenBadge.textContent = '● Connected';
    tokenBadge.className = 'token-status-badge connected';
    // Connected — stop the burst early, nothing left to watch for.
    if (_tokenBurstTimer) { clearInterval(_tokenBurstTimer); _tokenBurstTimer = null; }
  } else if (d.login_failed) {
    // Previously this rendered as an indefinite "Connecting…" even though
    // the backend had given up — the operator had no way to know a re-paste
    // was required. Surface the actual reason instead.
    tokenBadge.textContent = '● Failed — paste token again';
    tokenBadge.className = 'token-status-badge disconnected';
    if (_tokenBurstTimer) { clearInterval(_tokenBurstTimer); _tokenBurstTimer = null; }
    if (tokenMsg && d.login_fail_reason) {
      tokenMsg.className = 'pw-msg err';
      tokenMsg.textContent = d.login_fail_reason;
    }
  } else if (hasToken) {
    tokenBadge.textContent = '● Connecting…';
    tokenBadge.className = 'token-status-badge connecting';
  } else {
    tokenBadge.textContent = '● Disconnected';
    tokenBadge.className = 'token-status-badge disconnected';
  }
  if (tokenSource) {
    if (d.token_source === 'user') {
      tokenSource.textContent = '(token from Settings page)';
    } else if (d.token_source === 'env') {
      tokenSource.textContent = '(token from Railway env var)';
    } else {
      tokenSource.textContent = '(no token set)';
    }
  }
}

// Wrap _setActiveTab to start/stop token-status polling when entering/
// leaving the Settings tab. _setActiveTab is a `let` (see above) so it
// can be reassigned. We save the original, then replace it with a wrapper
// that calls through and manages the polling timer.
const _origSetActiveTab = _setActiveTab;
_setActiveTab = function(tab) {
  _origSetActiveTab(tab);
  if (tab === 'settings') {
    _pollTokenStatus();
    if (_tokenPollTimer) clearInterval(_tokenPollTimer);
    _tokenPollTimer = setInterval(_pollGuard(_pollTokenStatus), 5000);
  } else {
    if (_tokenPollTimer) {
      clearInterval(_tokenPollTimer);
      _tokenPollTimer = null;
    }
  }
};
// Re-apply the active tab so the wrapper picks up the current state
_setActiveTab(_activeTab);

const signalPopupClose = document.getElementById('signal-popup-close');
if (signalPopupClose) signalPopupClose.addEventListener('click', _hideSignalPopup);

document.getElementById('tf-select').addEventListener('change', (e) => {
  currentPeriod = parseInt(e.target.value, 10);
  if (chart) chart.applyOptions({ timeScale: { secondsVisible: currentPeriod <= 60 } });
  resetAndSubscribe();
});

function resetAndSubscribe() {
  _savePairPrefs();
  _userTouchedChart = false;   // fresh selection — resume auto-scroll-to-latest
  _awaitingSnapshot = true;
  if (mainSeries) mainSeries.setData([]);
  if (predSeries) predSeries.setData([]);
  _resetRaf(null);
  _clearKLLines();
  _recentResults = [];
  _renderAccuracyStrip();
  document.getElementById('signal-bar').classList.add('hidden');
  document.getElementById('accuracy-wrap').classList.add('hidden');
  document.getElementById('reasons-list').innerHTML = '';
  document.getElementById('chart-loading').classList.remove('hidden');
  const rc = document.getElementById('running-conf');
  if (rc) rc.className = 'running-conf hidden';
  const et = document.getElementById('entry-timing');
  if (et) et.className = 'entry-timing hidden';
  const rb = document.getElementById('regime-badge');
  if (rb) rb.className = 'regime-badge hidden';
  // (micro-zigzag hide removed 2026-08-17 — element no longer in the DOM)
  renderMarketState(null);
  renderMicro(null);
  showNoData(false);
  lastDataAt     = Date.now();
  lastPrediction = null;
  sendSubscribe();
  loadStats();
}

// ── No-data overlay ──────────────────────────────────────────────────────────
function showNoData(on, subText) {
  const el = document.getElementById('no-data');
  if (!el) return;
  el.classList.toggle('hidden', !on);
  const sub = document.getElementById('no-data-sub-text');
  if (sub) sub.textContent = subText || "This pair isn't streaming — select an OTC pair";
}

const NO_DATA_MS = 9000;
let _lastResubscribeAt = 0;
setInterval(() => {
  if (lastDataAt && Date.now() - lastDataAt > NO_DATA_MS) {
    showNoData(true);
    // Self-heal: the initial /api/subscribe call can be lost to a transient
    // network blip with no other retry path (fetch errors are swallowed in
    // sendSubscribe). Re-poke the server periodically instead of leaving the
    // user stuck on a permanently stale/blank chart until they manually
    // switch pairs.
    if (Date.now() - _lastResubscribeAt > NO_DATA_MS) {
      _lastResubscribeAt = Date.now();
      sendSubscribe();
    }
  }
}, 2000);

// ── Win-rate + TRUE theory accuracy ─────────────────────────────────────────
async function loadStats() {
  try {
    const q = `?asset=${encodeURIComponent(currentAsset)}&period=${currentPeriod}`;

    // Fetch overall stats AND true theory accuracy in parallel
    const [s, tr] = await Promise.all([
      fetch('/api/stats' + q).then((r) => r.json()),
      fetch('/api/theory-report' + q).then((r) => r.json()).catch(() => ({})),
    ]);

    // Header win-rate
    const wr  = document.getElementById('winrate');
    const wro = document.getElementById('winrate-overall');
    const txt = s.total ? `${s.rate}% (${s.correct}/${s.total})` : '--';
    if (wr)  { wr.textContent  = txt; wr.className = _wrClass(s.rate, s.total); }
    if (wro) { wro.textContent = txt; wro.className = 'wr-overall ' + _wrClass(s.rate, s.total); }

    // Per-theory TRUE accuracy list (right_codes/wrong_codes based).
    // Theories currently benched by the live mute gate (7-day accuracy
    // below the floor) get a MUTED tag — their votes are shown in reasons
    // but excluded from the score until they recover.
    const muted = s.muted_theories || {};
    const ul = document.getElementById('theory-stats');
    if (ul) {
      ul.innerHTML = '';
      // Only show theories that are still active in the engine — old
      // removed theories (T7, T2, TRAP, STAR, STREAK, OUTSIDE, SPIN,
      // ZIGZAG, MTF, ANOMALY, OBLOCK) may still have rows in the DB from
      // past logs, but they no longer vote and shouldn't clutter the panel.
      const ACTIVE_THEORIES = new Set([
        'RUN', 'WICKWALL', 'MICRO', 'DIVERGENCE',
        'LIVE', 'TICKSWEEP', 'ABSORBWALL', 'LATEFLIP',
        'MARKET_STATE'
      ]);
      const entries = Object.entries(tr || {})
        .filter(([code]) => ACTIVE_THEORIES.has(code));
      if (!entries.length) {
        ul.innerHTML = '<li class="empty">Collecting results…</li>';
      } else {
        // Sort by sample count
        entries.sort((a, b) => b[1].n - a[1].n);
        for (const [code, t] of entries) {
          const isMuted = Object.prototype.hasOwnProperty.call(muted, code);
          const li = document.createElement('li');
          li.className = 'theory-stat ' + _wrClass(t.rate, t.n) +
                         (isMuted ? ' ts-muted' : '');
          li.title = isMuted
            ? `Auto-muted by the live accuracy gate (${muted[code]}) — votes excluded until it recovers to 48%+`
            : '';
          li.innerHTML =
            `<span class="ts-code">${code}</span>` +
            (isMuted ? '<span class="ts-mute-tag">MUTED</span>' : '') +
            `<span class="ts-rate">${t.rate}%</span>` +
            `<span class="ts-n">${t.right}/${t.n}</span>`;
          ul.appendChild(li);
        }
      }
    }
  } catch (_) {}
}

function _wrClass(rate, n) {
  if (!n || n < 10) return 'wr-low';
  if (rate >= 55)   return 'wr-good';
  if (rate >= 45)   return 'wr-mid';
  return 'wr-bad';
}

// ── Helpers ────────────────────────────────────────────────────────────────
function toBar(c) {
  return { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
}

function setStatus(cls, text) {
  const el = document.getElementById('status');
  el.className   = `status ${cls}`;
  el.textContent = text;
}

// ── Boot ───────────────────────────────────────────────────────────────────
// The chart library loads from a CDN (index.html) — on a slow/flaky
// connection it may not be ready the instant this script runs. Poll briefly
// instead of failing outright on the very first load.
function bootChart(attempt) {
  attempt = attempt || 0;
  // The timeframe <select> hardcodes 1m as `selected` in the HTML — if a
  // restored currentPeriod (see localStorage restore above) differs, sync
  // the dropdown to match so it doesn't show "1m" while actually running,
  // say, 5m.
  const tfSel = document.getElementById('tf-select');
  if (tfSel && tfSel.value !== String(currentPeriod)) tfSel.value = String(currentPeriod);
  if (typeof LightweightCharts === 'undefined') {
    if (attempt >= 20) {   // ~10s of retrying
      showFatalError('Chart library failed to load',
                     'Check your connection, then reload the page.');
      return;
    }
    setTimeout(() => bootChart(attempt + 1), 500);
    return;
  }
  try {
    initChart();
    hideFatalError();
  } catch (err) {
    showFatalError('Chart failed to start', 'Reloading…');
    setTimeout(() => location.reload(), 2500);
    return;
  }
  loadPairs().then(() => connect()).catch(() => {
    showFatalError('Failed to start', 'Reloading…');
    setTimeout(() => location.reload(), 2500);
  });
  loadStats();
  setInterval(_pollGuard(loadStats), 30000);
  // Initial load of the sidebar's "Recent Signals" mini-list + periodic refresh
  _renderSignalHistoryMini();
  setInterval(_pollGuard(_renderSignalHistoryMini), 60000);  // refresh every 60s
}
bootChart();

// ── History modal — browse past resolved signals from the DB ──────────────
function _fmtHistTime(ctime) {
  const d = new Date(ctime * 1000);
  return d.toLocaleString([], { month: 'short', day: 'numeric',
                                hour: '2-digit', minute: '2-digit' });
}

function _renderHistoryFilterOptions() {
  const sel = document.getElementById('history-pair-filter');
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = '<option value="">All pairs</option>';
  pairsList.forEach((p) => {
    const opt = document.createElement('option');
    opt.value = p.asset;
    opt.textContent = p.display;
    sel.appendChild(opt);
  });
  sel.value = keep;
}

async function loadHistory() {
  const rows = document.getElementById('history-rows');
  if (!rows) return;
  // Populate the filter dropdown if it's empty (first visit to the history tab)
  const sel = document.getElementById('history-pair-filter');
  if (sel && sel.options.length <= 1) {
    _renderHistoryFilterOptions();
  }
  const asset = sel ? sel.value : '';
  try {
    const q = asset ? `?asset=${encodeURIComponent(asset)}&limit=150` : '?limit=150';
    const data = await fetch('/api/signals' + q).then((r) => r.json());
    if (!Array.isArray(data) || !data.length) {
      rows.innerHTML = '<tr><td colspan="6" class="history-empty">No resolved signals yet</td></tr>';
      return;
    }
    rows.innerHTML = data.map((s) => {
      const sigCls  = (s.signal || 'neutral').toLowerCase();
      const resCls  = s.result || '';
      const resTxt  = s.result === 'correct' ? '✓ Correct'
                    : s.result === 'draw'    ? '– Draw'
                    : s.result === 'wrong'   ? '✗ Wrong' : '–';
      return `<tr>
        <td>${_fmtHistTime(s.ctime)}</td>
        <td>${s.asset}</td>
        <td class="hist-signal ${sigCls}">${s.signal || '–'}</td>
        <td>${s.strength || '–'}</td>
        <td class="hist-result ${resCls}">${resTxt}</td>
        <td class="hist-why">${s.postmortem || ''}</td>
      </tr>`;
    }).join('');
  } catch (_) {
    rows.innerHTML = '<tr><td colspan="6" class="history-empty">Failed to load — try Refresh</td></tr>';
  }
}

// Desktop entry point into Settings — now navigates to the Settings page
// History is now a full page (tab-history), not a modal. loadHistory() is
// called automatically when the History tab is activated (see _setActiveTab).
// The filter dropdown and refresh button live inside the history page.

// Full page reload — simplest reliable fix for a chart that's stuck showing
// stale/missing data (bad WS state, a pair that never got its snapshot,
// etc.): re-establishes the WS connection, refetches pairs, and rebuilds
// the chart from scratch rather than trying to patch whatever's wrong.
document.getElementById('refresh-btn').addEventListener('click', () => location.reload());

// History page controls — guarded (only exist when history tab is in the DOM)
const histRefresh = document.getElementById('history-refresh');
if (histRefresh) histRefresh.addEventListener('click', loadHistory);
const histFilter = document.getElementById('history-pair-filter');
if (histFilter) histFilter.addEventListener('change', loadHistory);

// Share Signal page controls
const shareRefresh = document.getElementById('share-refresh');
if (shareRefresh) shareRefresh.addEventListener('click', () => {
  _lastShareKey = '';  // force re-render
  _loadShareSignals();
});
