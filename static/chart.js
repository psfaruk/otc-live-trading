/* ═══════════════════════════════════════════════════════════════════════
   NOVA — OTC Signal Terminal · chart.js (2026-09 full redesign)

   Contract (must match server.py / feed.py):
   WS events: pairs, snapshot, eoc, signal_start, tick, stale
   REST: /api/pairs /api/share-signals /api/stream-status /api/stats
         /api/theory-report /api/pair-winrate /api/winrate-calls
         /api/signals /api/token-status /api/server-time POST|DELETE /api/token
         POST /api/subscribe
   The prediction object is FROZEN at candle open by the backend; the UI
   renders it verbatim and never mutates or re-derives the direction.
   NEUTRAL renders as NO TRADE — the honest default.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  /* ── state ─────────────────────────────────────────────────────── */
  const VOTERS = ["STAT", "REGIME", "POSITION", "PATTERN", "STATE", "FLOW"];
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // localStorage migration from the previous branding
  try {
    ["asset", "period", "prefs"].forEach((k) => {
      const old = localStorage.getItem("plybit_" + k);
      if (old != null && localStorage.getItem("nova_" + k) == null) {
        localStorage.setItem("nova_" + k, old);
      }
    });
  } catch (e) {}

  const state = {
    asset: "BRLUSD_otc",   // first whitelist entry — always a valid OTC asset
    period: 60,
    cid: Math.random().toString(36).slice(2) + Date.now().toString(36),
    ws: null,
    wsUp: false,
    pairs: [],
    pairsByAsset: {},
    brokerOffsetMs: 0,
    candleOpenTime: 0,
    prediction: null,          // FROZEN prediction for the running candle
    lastResults: [],           // last 15 outcomes on the active pair
    prefs: { popup: true, ghost: true, klines: true },
    wrDays: 7,
    breakEven: 52.08,
    minN: 20,
    pollTimers: [],
    micro: null,
    runningConf: null,
    liveView: null,
    priceLines: [],
    tween: null,
  };
  try {
    const a = localStorage.getItem("nova_asset"); if (a) state.asset = a;
    const p = parseInt(localStorage.getItem("nova_period")); if (p) state.period = p;
    const pr = JSON.parse(localStorage.getItem("nova_prefs") || "null");
    if (pr) state.prefs = Object.assign(state.prefs, pr);
  } catch (e) {}
  const savePrefs = () => { try { localStorage.setItem("nova_prefs", JSON.stringify(state.prefs)); } catch (e) {} };

  const brokerNow = () => Date.now() + state.brokerOffsetMs;
  const fmt = (px) => (px == null ? "—" :
    px >= 100 ? px.toFixed(3) : px >= 1 ? px.toFixed(5) : px.toFixed(6));

  /* ═══════════════════════ CHART ═══════════════════════ */
  let chart = null, mainSeries = null, predSeries = null;

  function initChart() {
    if (chart || !window.LightweightCharts) return;
    chart = LightweightCharts.createChart($("#chart"), {
      layout: {
        background: { type: "solid", color: "transparent" },
        textColor: "#7d8bb4",
        fontFamily: getComputedStyle(document.body).fontFamily,
      },
      grid: {
        vertLines: { color: "rgba(110,130,220,.06)" },
        horzLines: { color: "rgba(110,130,220,.06)" },
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "rgba(120,150,230,.15)" },
      timeScale: { borderColor: "rgba(120,150,230,.15)", timeVisible: true, secondsVisible: false },
      handleScroll: { axisPressedMouseMove: false },
    });
    mainSeries = chart.addCandlestickSeries({
      upColor: "#00e28a", downColor: "#ff4d6d",
      borderUpColor: "#00e28a", borderDownColor: "#ff4d6d",
      wickUpColor: "rgba(0,226,138,.7)", wickDownColor: "rgba(255,77,109,.7)",
    });
    predSeries = chart.addCandlestickSeries({
      upColor: "rgba(0,226,138,.32)", downColor: "rgba(255,77,109,.32)",
      borderUpColor: "rgba(0,226,138,.55)", borderDownColor: "rgba(255,77,109,.55)",
      wickUpColor: "rgba(0,226,138,.45)", wickDownColor: "rgba(255,77,109,.45)",
    });
    new ResizeObserver(() => {
      const el = $("#chart");
      if (chart && el) chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    }).observe($("#chart"));
  }

  function renderCandles(candles) {
    if (!mainSeries || !Array.isArray(candles)) return;
    mainSeries.setData(candles.map((c) => ({
      time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
    })));
  }

  function renderGhost() {
    if (!predSeries) return;
    const p = state.prediction;
    if (!p || !state.prefs.ghost || !p.candle || p.candle.open == null) {
      predSeries.setData([]);
      return;
    }
    predSeries.setData([{
      time: p.candle.time, open: p.candle.open, high: p.candle.high,
      low: p.candle.low, close: p.candle.close,
    }]);
  }

  function renderLevelLines() {
    if (!chart) return;
    state.priceLines.forEach((pl) => { try { mainSeries.removePriceLine(pl); } catch (e) {} });
    state.priceLines = [];
    if (!state.prefs.klines) return;
    const p = state.prediction;
    if (!p) return;
    const add = (price, color, title) => {
      if (!price) return;
      try {
        state.priceLines.push(mainSeries.createPriceLine({
          price, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
          axisLabelVisible: true, title,
        }));
      } catch (e) {}
    };
    (p.key_levels || []).slice(0, 6).forEach(([lvl, touches]) =>
      add(lvl, "rgba(139,92,246,.55)", `KL×${touches}`));
    (p.wick_walls?.support || []).slice(0, 3).forEach(([lvl, w]) =>
      add(lvl, "rgba(0,226,138,.4)", `wall×${Math.round(w)}`));
    (p.wick_walls?.resistance || []).slice(0, 3).forEach(([lvl, w]) =>
      add(lvl, "rgba(255,77,109,.4)", `wall×${Math.round(w)}`));
  }

  /* running candle tween — smooth last-candle updates */
  function tweenRunning(candle) {
    if (!mainSeries || !candle) return;
    state.tween = candle;
    const base = { time: candle.time, open: candle.open };
    const target = candle;
    const start = performance.now(), dur = 420;
    const from = { close: candle.close, high: candle.high, low: candle.low };
    function frame(t) {
      const k = Math.min(1, (t - start) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      const cur = state.tween;
      if (!cur || cur.time !== candle.time) return;
      mainSeries.update({
        time: candle.time, open: cur.open,
        high: cur.high, low: cur.low, close: cur.close,
      });
      if (k < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  /* ═══════════════════════ SIGNAL DECK ═══════════════════════ */

  function ring(el, frac) {
    if (!el) return;
    // circumference from the circle's own radius — works for any ring size
    let C = 119.4;
    try { C = 2 * Math.PI * el.r.baseVal.value; } catch (e) {}
    el.style.strokeDasharray = String(C);
    el.style.strokeDashoffset = String(C * (1 - Math.max(0, Math.min(1, frac))));
  }

  function renderSignal() {
    const p = state.prediction;
    const badge = $("#signal-badge");
    const dir = $("#signal-dir");
    const stg = $("#signal-strength");
    if (!p) {
      badge.className = "sig-badge sig-none";
      dir.innerHTML = "—";
      stg.textContent = "";
      $("#signal-conf").textContent = "—";
      $("#signal-agree").textContent = "—";
      $("#signal-score").textContent = "—";
      ring($("#conf-ring-fg"), 0);
      renderVoterChips(null);
      return;
    }
    const sig = p.signal;
    if (sig === "CALL" || sig === "PUT") {
      badge.className = "sig-badge " + (sig === "CALL" ? "sig-call" : "sig-put");
      badge.classList.remove("sig-pop"); void badge.offsetWidth; badge.classList.add("sig-pop");
      dir.textContent = sig;
      stg.textContent = p.strength || "MEDIUM";
    } else {
      badge.className = "sig-badge sig-none";
      dir.innerHTML = 'NO TRADE<span class="no-trade-sub">COUNCIL DECLINED</span>';
      stg.textContent = "";
    }
    $("#signal-conf").textContent = p.confidence != null ? Math.round(p.confidence * 100) + "%" : "—";
    ring($("#conf-ring-fg"), p.confidence || 0);
    const nv = (p.voters || []).length;
    $("#signal-agree").textContent = `${p.agree ?? 0}/${nv}`;
    $("#signal-score").textContent = (p.score > 0 ? "+" : "") + (p.score ?? 0);
    renderVoterChips(p);
    renderConfluence(p);
    renderReasons(p);
    renderMarketState(p);
    renderGhost();
    renderLevelLines();
    renderLiveRecheck();
  }

  function renderVoterChips(p) {
    const box = $("#voter-chips");
    if (!box) return;
    if (!p || !(p.voters || []).length) {
      box.innerHTML = VOTERS.map((v) =>
        `<span class="voter-chip v-absent">${v}</span>`).join("");
      return;
    }
    const byName = {};
    (p.voters || []).forEach((v) => { byName[v.name] = v; });
    const sigDir = p.signal === "CALL" ? 1 : p.signal === "PUT" ? -1 : 0;
    box.innerHTML = VOTERS.map((name) => {
      const v = byName[name];
      if (!v || v.dir === 0) return `<span class="voter-chip v-absent">${name}</span>`;
      if (!sigDir) return `<span class="voter-chip v-off">${name} ${v.dir > 0 ? "▲" : "▼"}</span>`;
      const agrees = v.dir === sigDir;
      return `<span class="voter-chip ${agrees ? (sigDir > 0 ? "v-on-c" : "v-on-p") : "v-off"}">` +
        `${name} ${v.dir > 0 ? "▲" : "▼"}${agrees ? "" : " ✕"}</span>`;
    }).join("");
  }

  function renderConfluence(p) {
    const c = p.confluence || {};
    const fill = $("#confluence-fill");
    const note = $("#confluence-note");
    if (!fill) return;
    if (p.signal === "CALL" || p.signal === "PUT") {
      const pct = Math.min(100, (p.agree_weight / 10) * 100);
      fill.style.width = pct + "%";
      note.textContent = `${p.agree} voters agree · weight ${p.agree_weight} · net ${p.score}`;
    } else {
      fill.style.width = "0%";
      const why = (c.blocked_by || [])[0];
      note.textContent = why ? "no trade: " + why : "no trade — council thresholds not met";
    }
  }

  function renderReasons(p) {
    const ul = $("#reasons-list");
    if (!ul) return;
    const chip = $("#council-verdict");
    const sig = p.signal;
    if (chip) {
      chip.className = "chip " + (sig === "CALL" ? "chip-green" : sig === "PUT" ? "chip-red" : "chip-slate");
      chip.textContent = sig === "NEUTRAL" ? "NO TRADE" : sig;
    }
    const reasons = p.reasons || [];
    if (!reasons.length) {
      ul.innerHTML = '<li class="dim">no council activity</li>';
      return;
    }
    ul.innerHTML = reasons.slice(0, 14).map((r) => {
      let cls = "r-neutral";
      if (/->\s*CALL/.test(r) || /CALL (STRONG|MEDIUM)/.test(r)) cls = "r-call";
      else if (/->\s*PUT/.test(r) || /PUT (STRONG|MEDIUM)/.test(r)) cls = "r-put";
      if (/^NO-TRADE/.test(r)) cls = "r-neutral";
      return `<li class="${cls}">${esc(r)}</li>`;
    }).join("");
  }

  function renderMarketState(p) {
    const ms = p.market_state || {};
    const chip = $("#mstate-chip");
    const body = $("#mstate-body");
    if (!chip) return;
    chip.textContent = ms.state || "—";
    chip.className = "chip " + (
      ms.bias === "CALL" ? "chip-green" : ms.bias === "PUT" ? "chip-red" : "chip-slate");
    const pts = ms.points || {};
    const rows = Object.entries(pts)
      .filter(([, v]) => v > 0)
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `<div class="theory-row"><span class="t-name">${k}</span>` +
        `<span class="t-track"><span class="t-fill" style="width:${Math.min(100, v * 12)}%;background:var(--violet)"></span></span>` +
        `<span class="t-rate">${v}</span></div>`);
    body.innerHTML =
      `<div style="margin-bottom:8px">bias <b style="color:${ms.bias === "CALL" ? "var(--up)" : ms.bias === "PUT" ? "var(--down)" : "var(--t-2)"}">${ms.bias || "NEUTRAL"}</b> · conviction <b class="mono">${ms.conviction ?? 0}%</b></div>` +
      (rows.join("") || '<span class="dim">no active states</span>');
  }

  function renderLiveRecheck() {
    const card = $("#live-recheck-card");
    const body = $("#live-recheck-body");
    if (!card || !body) return;
    const lv = state.liveView;
    if (!lv || !lv.signal) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    const col = lv.signal === "CALL" ? "var(--up)" : lv.signal === "PUT" ? "var(--down)" : "var(--t-2)";
    body.innerHTML = `<div class="lr-sig" style="color:${col}">${esc(lv.signal)}${lv.strength && lv.strength !== "NONE" ? " · " + esc(lv.strength) : ""}</div>` +
      (lv.reasons || []).slice(0, 3).map((r) => `<div class="dim small">${esc(r)}</div>`).join("") +
      `<div class="hint" style="margin-top:4px">re-check only — the traded signal stays frozen at candle open</div>`;
  }

  function renderMicro() {
    const m = state.micro;
    const chip = $("#micro-pressure");
    if (!chip) return;
    if (!m) {
      chip.textContent = "—"; chip.className = "chip";
      $("#flow-buy").style.width = "50%";
      $("#flow-buy-pct").textContent = "—";
      $("#micro-tags").innerHTML = "";
      return;
    }
    const bp = m.buy_pct != null ? m.buy_pct : 50;
    $("#flow-buy").style.width = bp + "%";
    $("#flow-buy-pct").textContent = `${bp}% / ${100 - bp}%`;
    chip.textContent = m.pressure || "—";
    chip.className = "chip " + (m.pressure === "BUYER" ? "chip-green" : m.pressure === "SELLER" ? "chip-red" : "chip-slate");
    const tags = [];
    if (m.is_fight) tags.push(["FIGHT", "chip-amber"]);
    if (m.last_react) tags.push([m.last_react, m.last_react === "EXHAUST" ? "chip-amber" : "chip-slate"]);
    if (m.gap_type && m.gap_type !== "NONE") tags.push([m.gap_type, "chip-slate"]);
    $("#micro-tags").innerHTML = tags.map(([t, cls]) =>
      `<span class="chip ${cls}">${esc(t)}</span>`).join("");
  }

  /* ── accuracy strip ────────────────────────────────────────────── */
  function pushResult(acc) {
    if (!acc) return;
    state.lastResults.push(acc);
    if (state.lastResults.length > 15) state.lastResults.shift();
    renderAccuracyStrip();
  }
  function renderAccuracyStrip() {
    const el = $("#accuracy-strip");
    if (!el) return;
    el.innerHTML = state.lastResults.map((a) =>
      `<span class="acc-dot ${a === "correct" ? "a-c" : a === "wrong" ? "a-w" : "a-d"}" title="${a}"></span>`).join("");
  }

  /* ── entry timing + countdowns ─────────────────────────────────── */
  function entryTiming() {
    const el = $("#entry-timing");
    if (!el) return;
    if (!state.prediction || state.prediction.signal === "NEUTRAL" || !state.candleOpenTime) {
      el.className = "et et-skip";
      el.textContent = state.prediction && state.prediction.signal === "NEUTRAL" ? "SKIP" : "—";
      return;
    }
    const pct = (brokerNow() / 1000 - state.candleOpenTime) / state.period;
    if (pct <= 0.25) { el.className = "et et-go"; el.textContent = "⚡ ENTER NOW"; }
    else if (pct <= 0.8) { el.className = "et et-wait"; el.textContent = "NEXT CANDLE"; }
    else { el.className = "et et-late"; el.textContent = "⏱ TOO LATE"; }
  }

  function tickClock() {
    const now = brokerNow();
    // UTC clock
    const d = new Date(now);
    const cl = $("#utc-clock");
    if (cl) cl.textContent =
      String(d.getUTCHours()).padStart(2, "0") + ":" +
      String(d.getUTCMinutes()).padStart(2, "0") + ":" +
      String(d.getUTCSeconds()).padStart(2, "0") + " UTC";
    // countdown ring
    if (state.candleOpenTime > 0) {
      const closeAt = state.candleOpenTime + state.period;
      const left = Math.max(0, closeAt - now / 1000);
      const mm = Math.floor(left / 60), ss = Math.floor(left % 60);
      const cd = $("#chart-countdown");
      if (cd) cd.textContent = state.period >= 60
        ? `${mm}:${String(ss).padStart(2, "0")}` : String(ss).padStart(2, "0") + "s";
      ring($("#cd-ring-fg"), left / state.period);
      const svg = $("#cd-ring-fg")?.closest("svg");
      if (svg) svg.classList.toggle("ring-danger", left < state.period * 0.15);
      // sidebar countdowns
      $$(".sp-cd").forEach((el) => {
        const t = parseInt(el.dataset.open || "0");
        if (!t) return;
        const l = Math.max(0, t + state.period - now / 1000);
        el.textContent = state.period >= 60
          ? `${Math.floor(l / 60)}:${String(Math.floor(l % 60)).padStart(2, "0")}` : Math.ceil(l) + "s";
        el.classList.toggle("danger", l < 5);
      });
      entryTiming();
    }
  }

  /* ═══════════════════════ SIDEBAR + PAIRS ═══════════════════════ */

  function pairMeta(asset) {
    return state.pairsByAsset[asset] || { display: asset, status: "", payout: null, locked: false };
  }
  function dotClass(status) {
    return status === "live" ? "pd-live" : status === "otc" ? "pd-otc" : "pd-closed";
  }

  function renderSidebarPairs() {
    const box = $("#sidebar-pair-list");
    if (!box) return;
    box.innerHTML = state.pairs.map((p) => `
      <div class="side-pair-row ${p.asset === state.asset ? "active" : ""}" data-asset="${esc(p.asset)}">
        <span class="pair-dot ${dotClass(p.status)}"></span>
        <span class="sp-name">${esc(p.display)}</span>
        <span class="sp-wr mono" data-wr="${esc(p.asset)}">—</span>
        <span class="sp-cd mono" data-open="0">—</span>
      </div>`).join("");
    box.querySelectorAll(".side-pair-row").forEach((row) =>
      row.addEventListener("click", () => selectPair(row.dataset.asset)));
    $("#side-pair-count").textContent = state.pairs.length;
  }

  function updateSidebarWR(pairWinrate) {
    const map = {};
    (pairWinrate.pairs || []).forEach((r) => { map[r.asset] = r; });
    $$("[data-wr]").forEach((el) => {
      const r = map[el.dataset.wr];
      if (!r || r.rate == null) { el.textContent = "—"; el.style.color = ""; return; }
      el.textContent = r.rate.toFixed(1) + "%";
      el.style.color = r.status === "proven_win" ? "var(--up)"
        : r.status === "proven_loss" ? "var(--down)"
        : r.status === "thin" ? "var(--t-3)" : "var(--t-1)";
    });
    // active pair countdown anchors
    const rows = $$(".sp-cd");
    rows.forEach((el) => { if (el.dataset.open === "0") el.textContent = "—"; });
  }

  function renderPairPanel(filter) {
    const box = $("#pair-list");
    if (!box) return;
    const f = (filter || "").toLowerCase();
    box.innerHTML = state.pairs
      .filter((p) => !f || p.display.toLowerCase().includes(f) || p.asset.toLowerCase().includes(f))
      .map((p) => `
        <div class="pair-row ${p.asset === state.asset ? "selected" : ""}" data-asset="${esc(p.asset)}">
          <span class="pair-dot ${dotClass(p.status)}"></span>
          <span><b>${esc(p.display)}</b> <span class="dim small">${esc(p.asset)}</span></span>
          <span class="pr-payout">${p.payout != null ? p.payout + "%" : ""}</span>
          <span class="pr-lock">${p.locked ? "🔒" : ""}</span>
        </div>`).join("") || '<div class="dim" style="padding:14px">no match</div>';
    box.querySelectorAll(".pair-row").forEach((row) =>
      row.addEventListener("click", () => {
        hidePairPanel();
        selectPair(row.dataset.asset);
      }));
  }
  function showPairPanel() { $("#pair-panel").classList.remove("hidden"); $("#pair-search").value = ""; renderPairPanel(""); $("#pair-search").focus(); }
  function hidePairPanel() { $("#pair-panel").classList.add("hidden"); }

  function selectPair(asset) {
    if (!asset || asset === state.asset) return;
    state.asset = asset;
    try { localStorage.setItem("nova_asset", asset); } catch (e) {}
    state.lastResults = [];
    renderAccuracyStrip();
    resetAndSubscribe();
    window._setActiveTab("terminal");
  }

  function selectPeriod(p) {
    state.period = p;
    try { localStorage.setItem("nova_period", String(p)); } catch (e) {}
    state.lastResults = [];
    resetAndSubscribe();
  }

  /* ═══════════════════════ WEBSOCKET ═══════════════════════ */

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws?cid=${state.cid}&asset=${encodeURIComponent(state.asset)}&period=${state.period}`;
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => { state.wsUp = true; setStatus(true); sendSubscribe(); };
    ws.onmessage = (ev) => { try { handleMsg(JSON.parse(ev.data)); } catch (e) { console.warn("bad ws msg", e); } };
    ws.onclose = () => {
      state.wsUp = false; setStatus(false);
      setTimeout(connect, 3000);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  async function sendSubscribe() {
    try {
      const res = await fetch("/api/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset: state.asset, period: state.period, cid: state.cid }),
      });
      const j = await res.json();
      if (j && j.ok === false) {
        const el = $("#fatal-error");
        el.classList.remove("hidden");
        el.textContent = `stream unavailable: ${j.status || "error"}${j.reason ? " — " + j.reason : ""}`;
        return;
      }
      // snapshot-in-response fast path
      if (j && j.candles && j.candles.length) {
        renderCandles(j.candles);
        $("#chart-loading").classList.add("hidden");
        $("#no-data").classList.add("hidden");
      }
      if (j && j.prediction) { state.prediction = j.prediction; renderSignal(); }
    } catch (e) { /* ws will carry */ }
  }

  function resetAndSubscribe() {
    state.prediction = null;
    state.candleOpenTime = 0;
    state.micro = null;
    state.liveView = null;
    $("#pair-btn-label").textContent = pairMeta(state.asset).display || state.asset;
    $("#pair-dot").className = "pair-dot " + dotClass(pairMeta(state.asset).status);
    renderSignal();
    renderMicro();
    $("#chart-loading").classList.remove("hidden");
    $("#no-data").classList.add("hidden");
    $("#fatal-error").classList.add("hidden");
    if (mainSeries) mainSeries.setData([]);
    if (predSeries) predSeries.setData([]);
    if (state.ws && state.wsUp) sendSubscribe();
  }

  function setStatus(on) {
    const pill = $("#status");
    pill.className = "status-pill " + (on ? "st-on" : "st-off");
    $("#status-label").textContent = on ? "LIVE" : "OFF";
    const fd = $("#foot-dot");
    fd.className = "foot-dot" + (on ? " on" : "");
    $("#foot-status").textContent = on ? "feed connected" : "connecting…";
    $("#home-stat-status").innerHTML =
      `<span class="pulse-dot${on ? " on" : ""}"></span>${on ? "Live" : "Offline"}`;
  }

  /* ── event handling ────────────────────────────────────────────── */
  function handleMsg(m) {
    if (m.asset && m.period && (m.asset !== state.asset || m.period !== state.period)) {
      // other-pair broadcast — still refresh sidebar countdown anchors
      if (m.type === "signal_start") anchorCountdown(m);
      return;
    }
    switch (m.type) {
      case "pairs": {
        state.pairs = m.pairs || [];
        state.pairsByAsset = {};
        state.pairs.forEach((p) => { state.pairsByAsset[p.asset] = p; });
        renderSidebarPairs();
        $("#pair-btn-label").textContent = pairMeta(state.asset).display || state.asset;
        $("#pair-dot").className = "pair-dot " + dotClass(pairMeta(state.asset).status);
        break;
      }
      case "snapshot": {
        renderCandles(m.candles || []);
        state.prediction = m.prediction || null;
        if (m.prediction?.candle?.time) {
          state.candleOpenTime = m.prediction.candle.time;
          anchorCountdown(m.prediction);
        }
        $("#chart-loading").classList.add("hidden");
        renderSignal();
        break;
      }
      case "eoc": {
        // 1) grade the PREVIOUS prediction first (msg.accuracy refers to it)
        pushResult(m.accuracy);
        // 2) apply the new candles + the NEW candle's frozen prediction
        renderCandles(m.candles || []);
        state.prediction = m.prediction || null;
        if (m.prediction?.candle?.time) {
          state.candleOpenTime = m.prediction.candle.time;
          anchorCountdown(m.prediction);
        }
        $("#no-data").classList.add("hidden");
        renderSignal();
        refreshAfterEoc();
        break;
      }
      case "signal_start": {
        if (m.prediction_candle) {
          state.candleOpenTime = m.candle_open_time;
        }
        if (m.signal) {
          state.prediction = Object.assign({}, state.prediction || {}, m);
        }
        renderSignal();
        if (m.signal === "CALL" || m.signal === "PUT") showPopup(m);
        break;
      }
      case "tick": {
        if (m.candle) { tweenRunning(m.candle); }
        if (m.micro !== undefined) { state.micro = m.micro; renderMicro(); }
        if (m.running_conf !== undefined) state.runningConf = m.running_conf;
        if (m.live_view !== undefined) { state.liveView = m.live_view; renderLiveRecheck(); }
        if (m.prediction) { state.prediction = m.prediction; renderGhost(); }
        // live price ticker
        if (m.candle?.close != null) {
          const el = $("#ticker-price");
          const prev = parseFloat(el.dataset.px || "0");
          el.textContent = fmt(m.candle.close);
          el.dataset.px = m.candle.close;
          if (prev) {
            el.classList.remove("flash-up", "flash-down");
            void el.offsetWidth;
            el.classList.add(m.candle.close >= prev ? "flash-up" : "flash-down");
          }
        }
        break;
      }
      case "stale": {
        $("#no-data").classList.remove("hidden");
        break;
      }
    }
  }

  function showPopup(m) {
    if (!state.prefs.popup) return;
    const pop = $("#signal-popup");
    if (!pop) return;
    const badge = $("#signal-popup-badge");
    badge.className = "sig-badge " + (m.signal === "CALL" ? "sig-call" : "sig-put");
    badge.textContent = m.signal + (m.strength ? " · " + m.strength : "");
    $("#signal-popup-meta").innerHTML =
      `<b>${esc(pairMeta(state.asset).display || state.asset)}</b><br>` +
      `confidence ${m.confidence != null ? Math.round(m.confidence * 100) + "%" : "—"} · agree ${m.agree ?? "—"}` +
      `<br><span class="dim">${esc((m.reasons || [])[0] || "")}</span>`;
    pop.classList.remove("hidden");
    clearTimeout(showPopup._t);
    showPopup._t = setTimeout(() => pop.classList.add("hidden"), 7000);
  }

  function anchorCountdown(m) {
    const t = m.candle_open_time || (m.candle && m.candle.time) || 0;
    if (!t) return;
    $$(".side-pair-row").forEach((row) => {
      const a = row.querySelector("[data-wr]")?.dataset.wr;
      if (a === (m.asset || state.asset)) {
        row.querySelector(".sp-cd").dataset.open = String(t);
      }
    });
  }

  /* ═══════════════════════ DATA LOADERS ═══════════════════════ */

  async function api(path) {
    const res = await fetch(path);
    if (!res.ok) throw new Error(path + " -> " + res.status);
    return res.json();
  }

  async function loadPairs() {
    try {
      const j = await api("/api/pairs");
      state.pairs = j.pairs || [];
      state.pairsByAsset = {};
      state.pairs.forEach((p) => { state.pairsByAsset[p.asset] = p; });
      // If the stored/default asset is not in the broker catalog (e.g. a
      // real↔OTC variant swap, or stale localStorage), fall back to the
      // first available pair so the terminal never points at a rejected
      // asset.
      if (state.pairs.length && !state.pairsByAsset[state.asset]) {
        state.asset = state.pairs[0].asset;
        try { localStorage.setItem("nova_asset", state.asset); } catch (e) {}
      }
      renderSidebarPairs();
      $("#pair-btn-label").textContent = pairMeta(state.asset).display || state.asset;
      $("#pair-dot").className = "pair-dot " + dotClass(pairMeta(state.asset).status);
      // populate history filter
      const sel = $("#history-pair-filter");
      if (sel && sel.options.length <= 1) {
        state.pairs.forEach((p) => {
          const o = document.createElement("option");
          o.value = p.asset; o.textContent = p.display;
          sel.appendChild(o);
        });
      }
    } catch (e) { console.warn("pairs load failed", e); }
  }

  function poll(fn, ms) {
    fn().catch(() => {});
    const t = setInterval(() => {
      if (document.hidden) return;   // catch-up handled by _pollGuard pattern
      fn().catch(() => {});
    }, ms);
    state.pollTimers.push(t);
    return fn;
  }

  /* ── radar (home) ──────────────────────────────────────────────── */
  async function loadShareSignals() {
    const j = await api("/api/share-signals");
    const grid = $("#share-signal-grid");
    if (!grid) return;
    const sigs = j.signals || [];
    let active = 0;
    grid.innerHTML = sigs.map((s) => {
      const sig = s.signal;
      if (sig === "CALL" || sig === "PUT") active++;
      const cls = sig === "CALL" ? "sig-call emitted-call" : sig === "PUT" ? "sig-put emitted-put" : "sig-none";
      const dir = sig === "CALL" ? "CALL ▲" : sig === "PUT" ? "PUT ▼" : "NO TRADE";
      const conf = s.confidence != null ? Math.round(s.confidence * 100) : null;
      const typeTag = s.type === "otc" ? '<span class="sig-tag otc">OTC</span>' : '<span class="sig-tag real">REAL</span>';
      const buy = s.buy_pct != null ? s.buy_pct : 50;
      return `<div class="sig-card ${sig === "CALL" || sig === "PUT" ? "emitted-" + sig.toLowerCase() : ""}" data-asset="${esc(s.asset)}">
        <div class="sig-card-top">
          <span class="sig-card-pair"><span class="pair-dot ${dotClass(pairMeta(s.asset).status)}"></span>${esc(s.display || s.asset)}</span>
          ${typeTag}
        </div>
        <div class="sig-card-mid">
          <span class="sig-badge ${cls}" style="padding:6px 12px"><span class="sig-dir" style="font-size:13px">${dir}</span>${s.strength && s.strength !== "NONE" ? `<span class="sig-strength">${esc(s.strength)}</span>` : ""}</span>
          <div style="flex:1;margin-left:12px">
            <div class="sig-conf-track"><div class="sig-conf-fill" style="width:${conf ?? 0}%"></div></div>
            <div class="hint" style="margin-top:4px">${conf != null ? conf + "% confidence" : "waiting for first candle"}</div>
          </div>
        </div>
        <div class="sig-card-foot">
          <span class="mono">${fmt(s.prediction_candle?.close)}</span>
          <span>buy ${buy}%</span>
        </div>
      </div>`;
    }).join("") || '<div class="dim" style="padding:20px">no market data yet — connect a token in Settings</div>';
    grid.querySelectorAll(".sig-card").forEach((c) =>
      c.addEventListener("click", () => selectPair(c.dataset.asset)));
    $("#home-stat-active").textContent = active;
  }

  async function loadHomeStats() {
    try {
      const [stats, ss] = await Promise.all([
        api("/api/stats?days=7&period=60"),
        api("/api/stream-status"),
      ]);
      $("#home-stat-pairs").textContent = `${ss.count}/${ss.max ?? "—"}`;
      const wrEl = $("#home-stat-winrate");
      if (stats.decided > 0) {
        wrEl.textContent = stats.rate.toFixed(1) + "%";
        wrEl.style.color = stats.rate >= state.breakEven ? "var(--up)" : "var(--down)";
        $("#home-stat-winrate-n").textContent =
          `${stats.decided} signals · CI ${stats.ci95 ? stats.ci95[0].toFixed(1) + "–" + stats.ci95[1].toFixed(1) + "%" : "—"}${stats.pending ? ` · ${stats.pending} pending` : ""}`;
      } else {
        wrEl.textContent = "—";
        $("#home-stat-winrate-n").textContent = "no graded signals yet";
      }
    } catch (e) {}
  }

  /* ── voter accuracy (theory report) ────────────────────────────── */
  async function loadTheoryStats() {
    const box = $("#theory-stats");
    if (!box) return;
    try {
      const [j, stats] = await Promise.all([
        api(`/api/theory-report?asset=${encodeURIComponent(state.asset)}&period=${state.period}`),
        api(`/api/stats?asset=${encodeURIComponent(state.asset)}&period=${state.period}&days=7`),
      ]);
      const muted = stats.muted_theories || {};
      const rows = Object.entries(j)
        .filter(([k]) => VOTERS.includes(k) || !VOTERS.some((v) => k.startsWith(v)))
        .sort((a, b) => b[1].n - a[1].n)
        .slice(0, 8);
      if (!rows.length) {
        box.innerHTML = '<div class="dim">no voter history yet — it builds as candles close</div>';
        return;
      }
      box.innerHTML = rows.map(([code, v]) => {
        const col = v.rate >= state.breakEven ? "var(--up)" : v.rate >= 48 ? "var(--warn)" : "var(--down)";
        const isMuted = muted[code] != null;
        return `<div class="theory-row">
          <span class="t-name">${esc(code)}${isMuted ? '<span class="t-muted">MUTED</span>' : ""}</span>
          <span class="t-track"><span class="t-fill" style="width:${Math.min(100, v.rate)}%;background:${col}"></span></span>
          <span class="t-rate">${v.rate.toFixed(1)}% <span class="dim">n${v.n}</span></span>
        </div>`;
      }).join("");
    } catch (e) {
      box.innerHTML = '<div class="dim">unavailable</div>';
    }
  }

  async function loadMiniHistory() {
    const box = $("#signal-history-mini");
    if (!box) return;
    try {
      const rows = await api(`/api/signals?limit=6&asset=${encodeURIComponent(state.asset)}&period=${state.period}`);
      if (!rows.length) {
        box.innerHTML = '<div class="dim">no signals yet</div>';
        return;
      }
      box.innerHTML = rows.map((r) => `
        <div class="mini-row">
          <span><span class="m-pair">${esc(r.asset.replace("_otc", ""))}</span> <span class="dim">${new Date(r.ctime * 1000).toISOString().slice(11, 16)} UTC</span></span>
          <span class="m-sig ${r.signal === "CALL" ? "s-c" : "s-p"}">${r.signal}</span>
          <span class="res-badge res-${r.result}">${r.result === "correct" ? "WIN" : r.result === "wrong" ? "LOSS" : r.result.toUpperCase()}</span>
        </div>`).join("");
    } catch (e) { box.innerHTML = '<div class="dim">unavailable</div>'; }
  }

  function refreshAfterEoc() {
    loadMiniHistory().catch(() => {});
    loadTheoryStats().catch(() => {});
    loadPairWinrate().catch(() => {});
  }

  /* ── win rates page ────────────────────────────────────────────── */
  async function loadPairWinrate() {
    try {
      const j = await api(`/api/pair-winrate?days=${state.wrDays}&period=60`);
      state.breakEven = j.break_even || state.breakEven;
      state.minN = j.min_n || state.minN;
      $("#wr-breakeven-note").textContent =
        `break-even ≈ ${state.breakEven}% (payout-derived) · buckets under n=${state.minN} marked thin` +
        (j.no_data ? ` · ${j.no_data} no-data excluded` : "") +
        (j.pending ? ` · ${j.pending} pending` : "");
      updateSidebarWR(j);
    } catch (e) {}
  }

  async function loadWinrateCalls() {
    try {
      const j = await api(`/api/winrate-calls?days=${state.wrDays}&period=60`);
      state.breakEven = j.break_even || state.breakEven;
      state.minN = j.min_n || state.minN;
      const paint = (elId, b) => {
        const el = $(elId);
        el.textContent = b.rate != null ? b.rate.toFixed(1) + "%" : "—";
        const cls = b.status === "proven_win" ? "var(--up)" : b.status === "proven_loss" ? "var(--down)" : "var(--t-0)";
        el.style.color = cls;
      };
      paint("#wr-call-rate", j.overall.call);
      paint("#wr-put-rate", j.overall.put);
      paint("#wr-all-rate", j.overall.all);
      const meta = (b) => `${b.correct}W / ${b.wrong}L · n=${b.n}${b.draws ? ` · ${b.draws} draws` : ""}`;
      $("#wr-call-meta").textContent = meta(j.overall.call);
      $("#wr-put-meta").textContent = meta(j.overall.put);
      $("#wr-all-meta").textContent = meta(j.overall.all);
      const box = $("#wr-rows");
      if (!box) return;
      const statusLbl = { proven_win: "PROVEN WIN", proven_loss: "PROVEN LOSS", unproven: "UNPROVEN", thin: "THIN DATA", none: "—" };
      const biasLbl = { call: "CALL-leaning", put: "PUT-leaning", none: "—" };
      const cell = (b) => {
        const col = b.status === "proven_win" ? "var(--up)" : b.status === "proven_loss" ? "var(--down)" : "var(--t-1)";
        return `<td class="num mono" style="color:${col}">${b.rate != null ? b.rate.toFixed(1) + "%" : "—"} <span class="dim">n${b.n}</span></td>`;
      };
      box.innerHTML = (j.pairs || []).map((r) => `
        <tr>
          <td><b>${esc((r.display || r.asset).replace(" (OTC)", ""))}</b>${r.asset.endsWith("_otc") ? ' <span class="sig-tag otc">OTC</span>' : ""}</td>
          <td class="num mono">${r.all.n}${r.all.draws ? ` <span class="dim">+${r.all.draws}d</span>` : ""}</td>
          <td class="num mono" title="CI95 ${r.all.ci95 ? r.all.ci95[0] + "–" + r.all.ci95[1] + "%" : ""}">${r.all.rate != null ? r.all.rate.toFixed(1) + "%" : "—"}</td>
          ${cell(r.call)}
          ${cell(r.put)}
          <td class="dim">${biasLbl[r.direction_bias] || "—"}</td>
          <td class="wr-status ws-${r.all.status}">${statusLbl[r.all.status]}</td>
        </tr>`).join("") || '<tr><td colspan="7" class="dim">no graded signals in this window</td></tr>';
    } catch (e) {}
  }

  /* ── history page ──────────────────────────────────────────────── */
  async function loadHistory() {
    const box = $("#history-rows");
    if (!box) return;
    const asset = $("#history-pair-filter").value;
    const dir = $("#history-direction-filter").value;
    const result = $("#history-result-filter").value;
    const q = new URLSearchParams({ limit: "150", period: String(state.period) });
    if (asset) q.set("asset", asset);
    if (dir) q.set("direction", dir);
    if (result) q.set("result", result);
    try {
      const rows = await api("/api/signals?" + q.toString());
      if (!rows.length) {
        box.innerHTML = '<tr><td colspan="6" class="dim">no signals match</td></tr>';
        return;
      }
      box.innerHTML = rows.map((r) => `
        <tr>
          <td class="mono dim">${new Date(r.ctime * 1000).toISOString().replace("T", " ").slice(5, 16)}</td>
          <td><b>${esc(r.asset.replace("_otc", ""))}</b></td>
          <td class="mono" style="color:${r.signal === "CALL" ? "var(--up)" : "var(--down)"};font-weight:800">${r.signal}</td>
          <td>${esc(r.strength || "—")}</td>
          <td><span class="res-badge res-${r.result}">${r.result === "correct" ? "WIN" : r.result === "wrong" ? "LOSS" : r.result.toUpperCase()}</span></td>
          <td class="small dim" style="max-width:420px">${esc(r.postmortem || "")}</td>
        </tr>`).join("");
    } catch (e) {
      box.innerHTML = '<tr><td colspan="6" class="dim">unavailable</td></tr>';
    }
  }

  /* ── settings ──────────────────────────────────────────────────── */
  async function pollTokenStatus() {
    try {
      const j = await api("/api/token-status");
      const badge = $("#token-status-badge");
      const on = !!j.connected;
      badge.textContent = on ? "CONNECTED" : "DISCONNECTED";
      badge.className = "chip " + (on ? "chip-green" : "chip-red");
      badge.title = j.login_fail_reason || "";
      setStatus(on);
      const perf = $("#settings-perf");
      if (perf && !perf.dataset.skip) {
        const s = await api("/api/stats?days=7&period=60");
        perf.innerHTML = s.decided > 0
          ? `<span class="mono" style="font-size:20px;font-weight:800;color:${s.rate >= state.breakEven ? "var(--up)" : "var(--down)"}">${s.rate.toFixed(1)}%</span>
             <span class="dim"> win rate · 7d · ${s.decided} graded signals (${s.correct}W/${s.wrong}L, ${s.draws} draws${s.no_data ? `, ${s.no_data} no-data` : ""}${s.pending ? `, ${s.pending} pending` : ""})</span>`
          : '<span class="dim">no graded signals yet — history builds as candles close</span>';
      }
    } catch (e) {}
  }

  async function submitToken(e) {
    e.preventDefault();
    const token = $("#token-input").value.trim();
    const msg = $("#token-msg");
    if (token.length < 20) { msg.textContent = "that does not look like an SSID"; return; }
    msg.textContent = "connecting… (one attempt per paste)";
    try {
      const res = await fetch("/api/token", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const j = await res.json();
      msg.textContent = j.ok ? "token accepted — connecting" : (j.reason || "rejected");
      if (j.ok) $("#token-input").value = "";
      // burst status polling for 30s
      let n = 0;
      const iv = setInterval(async () => {
        await pollTokenStatus();
        if (++n > 15) clearInterval(iv);
      }, 2000);
    } catch (err) { msg.textContent = "network error"; }
  }

  async function clearToken() {
    try { await fetch("/api/token", { method: "DELETE" }); } catch (e) {}
    $("#token-msg").textContent = "cleared";
    pollTokenStatus();
  }

  /* ── broker clock sync ─────────────────────────────────────────── */
  async function syncClock() {
    try {
      const j = await api("/api/server-time");
      if (j.broker_time) {
        state.brokerOffsetMs = j.broker_time * 1000 - Date.now();
      }
    } catch (e) {}
  }

  /* ── boot ──────────────────────────────────────────────────────── */
  function boot() {
    if (!window.LightweightCharts) { setTimeout(boot, 120); return; }
    initChart();
    $("#tf-select").value = String(state.period);
    $("#pair-btn").addEventListener("click", showPairPanel);
    $("#pair-search").addEventListener("input", (e) => renderPairPanel(e.target.value));
    $("#pair-panel").addEventListener("click", (e) => { if (e.target.id === "pair-panel") hidePairPanel(); });
    $("#tf-select").addEventListener("change", (e) => selectPeriod(parseInt(e.target.value)));
    $("#radar-refresh").addEventListener("click", () => { loadShareSignals(); loadHomeStats(); });
    $("#history-refresh").addEventListener("click", loadHistory);
    ["#history-pair-filter", "#history-direction-filter", "#history-result-filter"].forEach(
      (s) => $(s).addEventListener("change", loadHistory));
    $("#token-form").addEventListener("submit", submitToken);
    $("#token-clear-btn").addEventListener("click", clearToken);
    $("#signal-popup-close").addEventListener("click", () => $("#signal-popup").classList.add("hidden"));
    // prefs checkboxes
    [["pref-popup", "popup"], ["pref-ghost", "ghost"], ["pref-klines", "klines"]].forEach(([id, key]) => {
      const el = $("#" + id);
      el.checked = state.prefs[key];
      el.addEventListener("change", () => {
        state.prefs[key] = el.checked; savePrefs();
        if (key === "ghost") renderGhost();
        if (key === "klines") renderLevelLines();
      });
    });
    // win-rate window seg
    $$("#wr-days button").forEach((b) => b.addEventListener("click", () => {
      $$("#wr-days button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.wrDays = parseInt(b.dataset.days);
      loadWinrateCalls(); loadPairWinrate();
    }));

    loadPairs();
    connect();
    syncClock();
    setInterval(syncClock, 30000);
    setInterval(tickClock, 250);

    poll(() => loadShareSignals(), 10000);
    poll(() => loadHomeStats(), 10000);
    poll(() => loadPairWinrate(), 30000);
    poll(() => pollTokenStatus(), 8000);
    poll(() => loadMiniHistory(), 60000);

    // tab-aware loads
    document.addEventListener("nova:tab", (e) => {
      const tab = e.detail;
      if (tab === "winrates") { loadWinrateCalls(); loadPairWinrate(); }
      if (tab === "history") loadHistory();
      if (tab === "terminal") { loadTheoryStats(); loadMiniHistory(); }
      if (tab === "settings") pollTokenStatus();
    });

    tickClock();
    renderSignal();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else boot();
})();
