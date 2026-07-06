/* ── Plybit AI — chart.js ─────────────────────────────────────────────────── *
 * Smooth candle: ease-out cubic tween @ 60 fps                               *
 *                                                                             *
 * Each server tick sets a TARGET.  The RAF loop tweens the rendered candle   *
 * from wherever it currently is → that target using easeOutCubic so the      *
 * body/wicks glide to their destination instead of snapping.                 *
 * ─────────────────────────────────────────────────────────────────────────── */

'use strict';

// ── Chart / WS globals ────────────────────────────────────────────────────
let chart       = null;
let mainSeries  = null;
let predSeries  = null;
let ws          = null;
let reconnTimer = null;

let currentAsset   = 'EURUSD_otc';
let currentPeriod  = 60;
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
      background: { color: '#090910' },
      textColor:  '#666677',
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
    rightPriceScale: { borderColor: '#25252f' },
    timeScale: {
      borderColor:    '#25252f',
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
    priceLineVisible: true,
    lastValueVisible: true,
  });

  // Auto-resize
  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: wrap.clientWidth, height: wrap.clientHeight });
  });
  ro.observe(wrap);
  chart.applyOptions({ width: wrap.clientWidth, height: wrap.clientHeight });

  _startRaf();
}

// ── Countdown ──────────────────────────────────────────────────────────────
function tickCountdown() {
  const now  = Math.floor(Date.now() / 1000);
  const left = currentPeriod - (now % currentPeriod);
  const el   = document.getElementById('countdown');
  el.textContent = left + 's';
  el.className   = left <= 5 ? 'danger' : left <= 15 ? 'warn' : '';
  updateEntryTiming();
}
setInterval(tickCountdown, 1000);
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

    case 'eoc':
      if (msg.candles && msg.candles.length) { lastDataAt = Date.now(); showNoData(false); }
      applySnapshot(msg.candles, msg.prediction);
      if (msg.accuracy) {
        showAccuracy(msg.accuracy);
        if (lastPrediction && lastPrediction.signal !== 'NEUTRAL') {
          _pushResult(lastPrediction.signal, msg.accuracy);
        }
      }
      loadStats();
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

  chart.timeScale().scrollToRealTime();
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
  if (isNeutral) {
    badge.textContent = '– NO TRADE';
  } else {
    const tag = strength === 'STRONG' ? '★ ' : strength === 'WEAK' ? '· ' : '';
    badge.textContent = (pred.signal === 'CALL' ? '▲ CALL' : '▼ PUT') + `  ${tag}${strength}`;
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

  // Zigzag detection display (in micro panel)
  const zzEl = document.getElementById('micro-zigzag');
  if (zzEl) {
    const zz = pred.zigzag;
    if (zz && zz.length >= 4) {
      const zzDir = zz.predict > 0 ? '▲ CALL' : '▼ PUT';
      zzEl.className   = `micro-tag zz-${zz.predict > 0 ? 'call' : 'put'}`;
      zzEl.textContent = `↕ Zigzag ${zz.length}-candle -> ${zzDir}`;
    } else {
      zzEl.classList.add('hidden');
    }
  }

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
}

// ── Controls ───────────────────────────────────────────────────────────────
async function loadPairs() {
  try {
    const data = await fetch('/api/pairs').then((r) => r.json());
    pairsList = data.pairs || [];
    if (typeof data.payout_floor === 'number') payoutFloor = data.payout_floor;
  } catch (_) {
    pairsList = [
      { asset: 'EURUSD_otc', display: 'EUR/USD', status: 'otc', payout: null, locked: false },
      { asset: 'GBPUSD_otc', display: 'GBP/USD', status: 'otc', payout: null, locked: false },
      { asset: 'USDJPY_otc', display: 'USD/JPY', status: 'otc', payout: null, locked: false },
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
  const label = document.getElementById('pair-btn-label');
  if (!label) return;
  const p = pairsList.find((x) => x.asset === currentAsset);
  if (!p) { label.textContent = currentAsset; return; }
  const pay = typeof p.payout === 'number' ? ` · ${p.payout}%` : '';
  label.innerHTML =
    (p.status === 'live' ? '<span class="pair-live-dot">●</span> ' : '') +
    `${p.display} <span class="pair-btn-sub">${p.status === 'live' ? 'Real' : 'Otc'}${pay}</span>`;
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

function _openPairPanel() {
  const btn   = document.getElementById('pair-btn');
  const panel = document.getElementById('pair-panel');
  const rect  = btn.getBoundingClientRect();
  panel.style.left     = `${Math.max(8, Math.min(rect.left, window.innerWidth - 328))}px`;
  panel.style.top      = `${rect.bottom + 6}px`;
  panel.style.maxHeight = `${Math.max(180, window.innerHeight - rect.bottom - 20)}px`;
  panel.classList.remove('hidden');
  btn.classList.add('open');
  _renderPairRows();
  const search = document.getElementById('pair-search');
  search.value = pairSearchTerm = '';
  _renderPairRows();
  search.focus();
}

function _closePairPanel() {
  document.getElementById('pair-panel').classList.add('hidden');
  document.getElementById('pair-btn').classList.remove('open');
}

document.getElementById('pair-btn').addEventListener('click', (e) => {
  e.stopPropagation();
  const panel = document.getElementById('pair-panel');
  if (panel.classList.contains('hidden')) _openPairPanel();
  else _closePairPanel();
});

document.getElementById('pair-panel').addEventListener('click', (e) => e.stopPropagation());
document.addEventListener('click', () => _closePairPanel());
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') _closePairPanel();
});
window.addEventListener('resize', () => _closePairPanel());

document.getElementById('pair-search').addEventListener('input', (e) => {
  pairSearchTerm = e.target.value;
  _renderPairRows();
});

document.getElementById('tf-select').addEventListener('change', (e) => {
  currentPeriod = parseInt(e.target.value, 10);
  if (chart) chart.applyOptions({ timeScale: { secondsVisible: currentPeriod <= 60 } });
  resetAndSubscribe();
});

function resetAndSubscribe() {
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
  const zzEl = document.getElementById('micro-zigzag');
  if (zzEl) zzEl.className = 'micro-tag hidden';
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
      const entries = Object.entries(tr || {});
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
  setInterval(loadStats, 30000);
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
  const asset = document.getElementById('history-pair-filter').value;
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

function openHistory() {
  _renderHistoryFilterOptions();
  document.getElementById('history-modal').classList.remove('hidden');
  loadHistory();
}

function closeHistory() {
  document.getElementById('history-modal').classList.add('hidden');
}

// Full page reload — simplest reliable fix for a chart that's stuck showing
// stale/missing data (bad WS state, a pair that never got its snapshot,
// etc.): re-establishes the WS connection, refetches pairs, and rebuilds
// the chart from scratch rather than trying to patch whatever's wrong.
document.getElementById('refresh-btn').addEventListener('click', () => location.reload());

document.getElementById('history-btn').addEventListener('click', openHistory);
document.getElementById('history-close').addEventListener('click', closeHistory);
document.getElementById('history-backdrop').addEventListener('click', closeHistory);
document.getElementById('history-refresh').addEventListener('click', loadHistory);
document.getElementById('history-pair-filter').addEventListener('change', loadHistory);
