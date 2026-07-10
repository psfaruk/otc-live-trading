"""
End-of-candle (EOC) analysis — DEEP-ANALYSIS-FIRST predictor (refactored 2026-07-10).

ARCHITECTURE OVERHAUL (2026-07-10):
  Phase-1/2 history proved that piling many small OHLC-based theories
  (T7, T2, TRAP, STAR, STREAK, OUTSIDE, SPIN, ZIGZAG, MTF, ANOMALY,
  OBLOCK) onto one candle does NOT improve accuracy — it amplifies the
  continuation bias (3.8:1 measured), inflates overfitting surfaces,
  and obscures what the market is actually doing.

  This refactor keeps the engine LEAN:
    - 5 core confirming theories (RUN, WICKWALL, MICRO, DIVERGENCE, LIVE)
    - 1 main predictor: MARKET STATE deep analysis
      (CONTINUATION / EXHAUSTION / REVERSAL / TRAP / RANGE)
    - Each market state contributes its OWN directional bias + conviction
      directly to score (the old "informational only" coupling is gone —
      every prior hand-tuned coupling regressed, but the prior couplings
      were ONE theory's vote re-routed through state; this refactor uses
      state's OWN directional read, voted ONCE per candle, scaled by
      conviction, never re-counted by other theories).

  What was removed (and why):
    - T7 / OUTSIDE / STAR     : color-forced continuation bias
    - T2 / SPIN               : subsumed by REVERSAL state's pin-bar read
    - STREAK / ZIGZAG         : subsumed by EXHAUSTION / RANGE states
    - TRAP                    : subsumed by TRAP state (was already a vote
                                here; the duplicate vote is gone)
    - MTF / ANOMALY / OBLOCK  : below coin-flip on live measurement
    - COLOR-GATED CAP         : no longer needed — color-forced theories
                                are gone, so there's no pile-up to cap
    - PARROT GUARD            : same reason — the parrot-bias source is gone
    - UNSTABLE BASE / OVERHEATED : with fewer theories, score scale is
                                naturally smaller and the strength
                                calibration handles this directly
    - TICK_VOL / SESSION_WEIGHT / ATR_WEIGHT : replaced by a single
                                INFORMATION_WEIGHT applied at the end

  What stays:
    - _round_level, _key_levels, _wick_wall helpers
    - _market_regime, _key_touches, _sr_bonus helpers
    - _parse_votes (used by feed.py for theory_perf grading)
    - RUN / WICKWALL / MICRO / DIVERGENCE / LIVE theory blocks
    - MARKET STATE block — now PROMOTED to main predictor

Signal flow now:
  1. Compute context (regime, key levels, wick walls, round numbers)
  2. Run 5 confirming theories → score, indep_dirs, reasons
  3. Run MARKET STATE deep analysis → state, directional bias, conviction
  4. Apply state's directional vote to score (scaled by conviction)
  5. Apply INFORMATION_WEIGHT (tick count + ATR + session, unified)
  6. Calibrate strength from final score + agreement

Note: 1-minute binary options remain near-random. The point of this
refactor is HONESTY — fewer theories, each with a real job, no pile-on,
no self-deception. Accuracy claims beyond ~52-55% are still fiction.
"""
import math
import os
import re
import time


ENABLE_LIVE_THEORY = os.environ.get('ENABLE_LIVE_THEORY', '1') == '1'
# Running-candle reaction theories (Phase 2, 2026-07-10). Each detects a
# specific real-time reaction pattern on the running (still-open) candle.
# All three are x3 weight — they catch high-conviction reversal patterns
# that the LIVE theory misses (LIVE focuses on absorption + momentum).
ENABLE_TICKSWEEP   = os.environ.get('ENABLE_TICKSWEEP',   '1') == '1'
ENABLE_ABSORBWALL  = os.environ.get('ENABLE_ABSORBWALL',  '1') == '1'
ENABLE_LATEFLIP    = os.environ.get('ENABLE_LATEFLIP',    '1') == '1'


# ── Helpers ───────────────────────────────────────────────────────────────────

def _round_level(price: float) -> tuple[float, float, str]:
    """
    Nearest significant round number → (level, distance, strength).
    BIG  = every 1% of magnitude  (e.g. 1.08, 1.09 for EURUSD; 155, 156 for JPY)
    MID  = every 0.5% of magnitude (e.g. 1.085, 155.5)
    SMALL= every 0.1% of magnitude (e.g. 1.081, 155.1)
    NONE = not near any round level
    """
    if price <= 0:
        return price, 0.0, "NONE"
    mag = 10.0 ** math.floor(math.log10(abs(price)))
    def _snap(price: float, step: float) -> float:
        decimals = max(8, -math.floor(math.log10(step)) + 2)
        return round(round(price / step) * step, decimals)

    best: tuple[float, float, str] | None = None
    for frac, label, thr in [(0.01, "BIG", 0.05), (0.005, "MID", 0.06), (0.001, "SMALL", 0.10)]:
        step  = mag * frac
        level = _snap(price, step)
        dist  = abs(price - level)
        if dist < step * thr and (best is None or dist < best[1]):
            best = (level, dist, label)
    if best:
        return best
    step  = mag * 0.01
    level = _snap(price, step)
    return level, abs(price - level), "NONE"


def _key_levels(candles: list[dict], lookback: int = 40) -> list[tuple[float, int]]:
    """
    Detect KEY support/resistance levels from recent price action.
    A key level is a price that has been TESTED multiple times — every swing
    high / swing low is a test, and nearby tests cluster into one level. The
    more touches, the stronger the level (and the more powerful a candle
    reaction there). Returns [(price, touches), ...] for levels with 2+ touches.
    """
    recent = candles[-lookback:]
    if len(recent) < 5:
        return []

    pivots: list[float] = []
    for i in range(1, len(recent) - 1):
        hi, lo = recent[i]["high"], recent[i]["low"]
        if hi >= recent[i - 1]["high"] and hi >= recent[i + 1]["high"]:
            pivots.append(hi)
        if lo <= recent[i - 1]["low"] and lo <= recent[i + 1]["low"]:
            pivots.append(lo)
    if not pivots:
        return []

    pivots.sort()
    levels: list[tuple[float, int]] = []
    cluster = [pivots[0]]
    for p in pivots[1:]:
        if abs(p - cluster[0]) <= cluster[0] * 0.0006:     # ~0.06% wide max
            cluster.append(p)
        else:
            if len(cluster) >= 2:
                levels.append((sum(cluster) / len(cluster), len(cluster)))
            cluster = [p]
    if len(cluster) >= 2:
        levels.append((sum(cluster) / len(cluster), len(cluster)))
    return levels


def _wick_wall(candles: list[dict], lookback: int = 20
               ) -> tuple[list[tuple[float, int]], list[tuple[float, int]], float]:
    """
    Detect repeated wick rejection zones — the 'wick wall'.

    Returns (support_walls, resistance_walls, avg_range):
      support_walls    : [(price, count), ...] — lower wick clusters
      resistance_walls : [(price, count), ...] — upper wick clusters
      avg_range        : mean candle range (for scoring tolerance calibration)
    """
    recent = candles[-lookback:]
    if len(recent) < 4:
        return [], [], 0.0

    n = len(recent)
    avg_rng = sum(c["high"] - c["low"] for c in recent) / n
    tol     = avg_rng * 0.25   # 25% of ATR — instrument-agnostic cluster width

    def _cluster(tips_with_weights: list[tuple[float, float]]
                 ) -> list[tuple[float, float]]:
        s = sorted(tips_with_weights, key=lambda x: x[0])
        out: list[tuple[float, float]] = []
        grp_p: list[float] = [s[0][0]]
        grp_w: list[float] = [s[0][1]]
        for t, w in s[1:]:
            if t - grp_p[0] <= tol:
                grp_p.append(t)
                grp_w.append(w)
            else:
                total_w = sum(grp_w)
                if total_w >= 2.5:
                    out.append((sum(p * wt for p, wt in zip(grp_p, grp_w)) / total_w,
                                total_w))
                grp_p = [t]
                grp_w = [w]
        total_w = sum(grp_w)
        if total_w >= 2.5:
            out.append((sum(p * wt for p, wt in zip(grp_p, grp_w)) / total_w,
                        total_w))
        return out

    _low_tips  = [(recent[i]["low"],  1.0 - i / n) for i in range(n)]
    _high_tips = [(recent[i]["high"], 1.0 - i / n) for i in range(n)]

    return (_cluster(_low_tips),
            _cluster(_high_tips),
            avg_rng)


def _market_regime(candles: list[dict], lookback: int = 20) -> tuple[str, str]:
    """
    Detect market regime + current price zone from recent candle structure.

    Splits the lookback window in half: if the second half shows higher highs
    AND higher lows vs the first half -> UPTREND; lower highs + lower lows ->
    DOWNTREND; mixed -> SIDEWAYS.

    Zone measures where the current close sits in the full lookback range:
    bottom 25% = SUPPORT, top 25% = RESISTANCE, middle = NEUTRAL.
    """
    recent = candles[-lookback:]
    if len(recent) < 6:
        return "SIDEWAYS", "NEUTRAL"

    mid = len(recent) // 2
    first, second = recent[:mid], recent[mid:]
    f_hi = max(x["high"] for x in first)
    f_lo = min(x["low"]  for x in first)
    s_hi = max(x["high"] for x in second)
    s_lo = min(x["low"]  for x in second)

    if s_hi > f_hi and s_lo > f_lo:
        regime = "UPTREND"
    elif s_hi < f_hi and s_lo < f_lo:
        regime = "DOWNTREND"
    else:
        regime = "SIDEWAYS"

    full_hi = max(x["high"] for x in recent)
    full_lo = min(x["low"]  for x in recent)
    rng = full_hi - full_lo
    if rng == 0:
        return regime, "NEUTRAL"
    pos = (candles[-1]["close"] - full_lo) / rng
    if pos <= 0.25:
        zone = "SUPPORT"
    elif pos >= 0.75:
        zone = "RESISTANCE"
    else:
        zone = "NEUTRAL"
    return regime, zone


# ── Theory set (LEAN — refactored 2026-07-10) ────────────────────────────────
# 5 confirming theories + market state (main predictor). Removed: T7, T2,
# TRAP, STAR, STREAK, OUTSIDE, SPIN, ZIGZAG, MTF, ANOMALY, OBLOCK — all
# either color-forced (continuation bias) or below coin-flip on live data.
_ALL_THEORIES = {
    # Closed-candle theories
    "RUN", "WICKWALL", "MICRO", "DIVERGENCE",
    # Running-candle theories (Phase 1 + Phase 2)
    "LIVE", "TICKSWEEP", "ABSORBWALL", "LATEFLIP",
    # Main predictor
    "MARKET_STATE",
}

# Signal-cooldown state — module-level so it persists across calls for the
# life of the process. Keyed by "asset:period"; a repeat non-neutral signal
# for the same key within _COOLDOWN_SECONDS is capped to WEAK.
_last_signal_time: dict[str, float] = {}
_COOLDOWN_SECONDS = 30


def _parse_votes(reasons: list[str],
                 include_muted: bool = True
                 ) -> list[tuple[str, int, int]]:
    """
    Parse reason lines into (theory_code, direction, magnitude) tuples.
    Used by feed.py to grade theory_perf and by this module to compute
    `agree`. Recognises the standard vote-line shape:
        "RUN  ... -> CALL (x4)"
        "WICKWALL  ... -> PUT (x2)"
        "MARKET_STATE  REVERSAL (bias PUT, conviction 67%) -> PUT (x3)"
    Lines without "(xN)" are not votes — modifiers, context, or
    informational only — and are skipped.

    include_muted=False filters out lines suffixed "[MUTED ...]" so the
    `agree` count (the STRONG/MEDIUM gate) and theory_perf grading both
    ignore muted theories entirely.
    """
    out: list[tuple[str, int, int]] = []
    for r in reasons:
        if not include_muted and "[MUTED" in r:
            continue
        m = re.search(r"(?<!\w)([A-Z][A-Z0-9 _]*?[A-Z0-9])\s{2,}.*?"
                      r"->\s*(CALL|PUT)\s*\(x(\d+)\)", r)
        if not m:
            continue
        code = m.group(1).strip()
        d = 1 if m.group(2) == "CALL" else -1
        mag = int(m.group(3))
        out.append((code, d, mag))
    return out


def analyze_eoc(candles: list[dict], ticks: list[float] | None = None,
                micro_history: list[dict] | None = None,
                period: int | None = None,
                muted: dict[str, str] | None = None,
                asset: str | None = None,
                running_ticks: list[float] | None = None) -> dict:
    """
    Predict next candle direction from the just-closed candle.

    Architecture (refactored 2026-07-10):
      1. Context (regime, key levels, wick walls, round numbers)
      2. 5 confirming theories → score, indep_dirs, reasons
      3. MARKET STATE deep analysis → state + directional bias + conviction
         (the MAIN predictor — its directional vote goes to score directly)
      4. INFORMATION_WEIGHT (tick count + ATR + session, unified modifier)
      5. Strength calibration

    Parameters:
      candles       : OHLC history, most-recent last. Need 2+ minimum.
      ticks         : price values from the just-closed candle (for RUN).
      micro_history : list of micro dicts for the N candles BEFORE the
                      just-closed one (from db.get_micro_history). Oldest first.
      period        : candle period in seconds.
      muted         : {theory_code: annotation} from the live theory-performance
                      gate (feed.py builds it from db.theory_perf with
                      hysteresis). Muted theory votes still appear in reasons
                      (suffixed "[MUTED <annotation>]") but contribute nothing
                      to score, `agree`, or strength.
      asset         : asset symbol, e.g. "EURUSD_otc".
      running_ticks : tick values from the CURRENTLY OPEN candle — feeds LIVE.

    Returns:
      {signal, score, confidence, agree, agree_weight, strength, reasons,
       key_levels, wick_walls, regime, market_state}
    """
    # MAX_SCORE calibrates the confidence% shown to the user (confidence =
    # |score|/MAX_SCORE). With fewer theories, score scale is smaller; this
    # is set so the typical MEDIUM signal (|score| 3-5) shows ~40-60%.
    MAX_SCORE = 10

    if len(candles) < 2:
        return {"signal": "NEUTRAL", "score": 0,
                "confidence": 0.0, "reasons": ["insufficient candles"]}

    cur  = candles[-1]
    prev = candles[-2]
    o, h, l, c = cur["open"], cur["high"], cur["low"], cur["close"]
    total_range = h - l

    if total_range == 0:
        return {"signal": "NEUTRAL", "score": 0,
                "confidence": 0.0, "reasons": ["zero-range candle"]}

    body       = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    is_bull    = c >= o

    score   = 0
    reasons = []
    indep_dirs: list[tuple[str, int]] = []   # color-INDEPENDENT votes (parrot guard gone,
                                             # but kept for `agree` counting)
    forced_score = 0  # legacy: kept for compatibility, always 0 in refactor
                      # (no color-forced theories remain)

    # Market regime / zigzag state (populated later; defaults allow safe return)
    _regime     = "SIDEWAYS"
    _zone       = "NEUTRAL"
    _zz_predict = 0
    _zz_len     = 0

    # ═══════════════════════════════════════════════════════════════════════
    # CONTEXT — key levels, wick walls, regime, round numbers
    # ═══════════════════════════════════════════════════════════════════════

    _klevels = _key_levels(candles)
    _sup_walls, _res_walls, _atr10 = _wick_wall(candles)

    def _key_touches(price: float) -> int:
        best = 0
        for lvl, touches in _klevels:
            if abs(price - lvl) <= lvl * 0.0006:
                best = max(best, touches)
        return best

    def _sr_bonus(price: float, is_support: bool) -> tuple[int, str]:
        k = _key_touches(price)
        _, _, rnd = _round_level(price)
        big_round = rnd == "BIG"
        any_round = rnd in ("BIG", "MID")
        if k >= 2 and any_round:
            return 3, f"CONFLUENCE key x{k}+{rnd.lower()} round"
        if k >= 3 or big_round:
            return 2, (f"KEY x{k}" if k >= 3 else "BIG round")
        if k == 2 or rnd == "MID":
            return 1, ("key x2" if k == 2 else "round")
        return 0, ""

    _regime, _zone = _market_regime(candles)

    # ═══════════════════════════════════════════════════════════════════════
    # THEORY 1 — RUN (just-closed candle tick microstructure)
    # ═══════════════════════════════════════════════════════════════════════

    _cur_bpct     = None
    _cur_fin_bpct = None
    if ticks and len(ticks) >= 15:
        up_t = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i - 1])
        dn_t = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i - 1])
        tot  = up_t + dn_t
        bpct = (up_t / tot) if tot else 0.5
        _cur_bpct = bpct
        close_pos = (c - l) / total_range
        bull_closed = ticks[-1] >= ticks[0]

        eff_buyer  = bpct >= 0.60
        eff_seller = bpct <= 0.40
        res_buyer  = close_pos >= 0.72
        res_seller = close_pos <= 0.20

        # ABSORPTION — single most reliable read (83-92% in DB analysis)
        if res_buyer and eff_seller:
            score -= 4
            indep_dirs.append(("RUN", -1))
            reasons.append(
                f"RUN  ABSORPTION: closed up but sellers pushed ({(1-bpct):.0%})"
                f" -> reversal -> PUT (x4)")
        elif res_seller and eff_buyer:
            score += 4
            indep_dirs.append(("RUN", +1))
            reasons.append(
                f"RUN  ABSORPTION: closed down but buyers pushed ({bpct:.0%})"
                f" -> reversal -> CALL (x4)")
        elif res_buyer and eff_buyer:
            score += 2
            forced_score += 2
            reasons.append(
                f"RUN  Buyers WON (close {close_pos:.0%} hi, {bpct:.0%} up-ticks)"
                f" -> CALL (x2)")
        elif res_seller and eff_seller:
            # OTC exhaustion reversal — sellers spent all capital → bounce
            score += 1
            indep_dirs.append(("RUN", +1))
            reasons.append(
                f"RUN  Sellers WON but exhausted (close {close_pos:.0%} lo,"
                f" {(1-bpct):.0%} dn-ticks) -> OTC reversal -> CALL (x1)")

        # Wick rejection
        if upper_wick > total_range * 0.45:
            score -= 1
            indep_dirs.append(("RUN", -1))
            reasons.append("RUN  Long upper wick: sellers rejected the top -> PUT (x1)")
        if lower_wick > total_range * 0.45:
            score += 1
            indep_dirs.append(("RUN", +1))
            reasons.append("RUN  Long lower wick: buyers defended the low -> CALL (x1)")

        # Final ~15% tick invasion
        fn   = max(len(ticks) // 6, 6)
        fin  = ticks[-fn:]
        fu   = sum(1 for i in range(1, len(fin)) if fin[i] > fin[i - 1])
        fd   = sum(1 for i in range(1, len(fin)) if fin[i] < fin[i - 1])
        ftot = fu + fd
        if ftot >= 3:
            fbp = fu / ftot
            _cur_fin_bpct = fbp
            if bull_closed and fbp <= 0.30:
                _mag = 2 if (1 - fbp) >= 0.80 else 1
                score -= _mag
                indep_dirs.append(("RUN", -1))
                reasons.append(
                    f"RUN  Sellers invaded the close ({1-fbp:.0%})"
                    f" -> PUT (x{_mag})")
            elif (not bull_closed) and fbp >= 0.70:
                _mag = 2 if fbp >= 0.80 else 1
                score += _mag
                indep_dirs.append(("RUN", +1))
                reasons.append(
                    f"RUN  Buyers invaded the close ({fbp:.0%})"
                    f" -> CALL (x{_mag})")

    # ═══════════════════════════════════════════════════════════════════════
    # THEORY 2 — WICKWALL (repeated wick rejection zones)
    # ═══════════════════════════════════════════════════════════════════════

    if _sup_walls:
        _best_sup = max(_sup_walls, key=lambda x: x[1])
        _sup_lvl, _sup_tch = _best_sup
        if _sup_tch >= 3 and abs(l - _sup_lvl) <= _atr10 * 0.25:
            bonus, lbl = _sr_bonus(_sup_lvl, True)
            mag = 2 + min(bonus, 1)  # base x2, +1 for confluence
            score += mag
            indep_dirs.append(("WICKWALL", +1))
            reasons.append(
                f"WICKWALL  Lower-wick cluster x{_sup_tch:.1f} at {_sup_lvl:.5g}"
                f"{', @' + lbl if lbl else ''} -> CALL (x{mag})")
    if _res_walls:
        _best_res = max(_res_walls, key=lambda x: x[1])
        _res_lvl, _res_tch = _best_res
        if _res_tch >= 3 and abs(h - _res_lvl) <= _atr10 * 0.25:
            bonus, lbl = _sr_bonus(_res_lvl, False)
            mag = 2 + min(bonus, 1)
            score -= mag
            indep_dirs.append(("WICKWALL", -1))
            reasons.append(
                f"WICKWALL  Upper-wick cluster x{_res_tch:.1f} at {_res_lvl:.5g}"
                f"{', @' + lbl if lbl else ''} -> PUT (x{mag})")

    # ═══════════════════════════════════════════════════════════════════════
    # THEORY 3 — MICRO (multi-candle microstructure from DB tick history)
    # ═══════════════════════════════════════════════════════════════════════
    # Kept compact: only the highest-value micro reads — pressure chain +
    # exhaustion chain + late-phase inversion. The full chain/recovery/fight/
    # hold/persistent variants were removed with the other theories.

    if micro_history and len(micro_history) >= 2:
        prev_m = micro_history[-1]
        hist3  = micro_history[-3:]

        # (a) PRESSURE CHAIN — sustained buyer/seller tick pressure across
        #     multiple candles is a genuine trend signal invisible in closes.
        if len(hist3) >= 2:
            _p_buys  = [m.get("buy_pct", 50) for m in hist3]
            _p_chain = sum(1 for b in _p_buys if b >= 60)
            _s_chain = sum(1 for b in _p_buys if b <= 40)
            if _p_chain >= 2:
                score += 2
                indep_dirs.append(("MICRO", +1))
                reasons.append(
                    f"MICRO  {_p_chain}-candle buyer pressure chain -> CALL (x2)")
            elif _s_chain >= 2:
                score -= 2
                indep_dirs.append(("MICRO", -1))
                reasons.append(
                    f"MICRO  {_s_chain}-candle seller pressure chain -> PUT (x2)")

        # (b) EXHAUSTION CHAIN — 2+ consecutive candles ended their final
        #     ticks in exhaustion → trend running out of fuel.
        _exn = sum(1 for _m in micro_history[-3:]
                   if _m.get("last_react") == "EXHAUST")
        if _exn >= 2:
            _ex_dir = -1 if is_bull else +1  # reversal direction
            score += _ex_dir * 2
            indep_dirs.append(("MICRO", _ex_dir))
            reasons.append(
                f"MICRO  {_exn} of last 3 candles ended in exhaustion"
                f" -> {'PUT' if _ex_dir < 0 else 'CALL'} (x2)")

        # (c) LATE-PHASE INVERSION — the final third of ticks in the previous
        #     candle moved AGAINST its close direction → smart money
        #     defending the reversal.
        if prev_m.get("last_react") == "EXHAUST":
            _inv_dir = -1 if is_bull else +1
            # Wait — this is about the PREVIOUS candle, not current. Read
            # prev candle direction from micro_history (buy_pct > 50 = was bull).
            _prev_was_bull = (prev_m.get("buy_pct", 50) >= 50)
            _inv_dir = -1 if _prev_was_bull else +1
            score += _inv_dir * 1
            indep_dirs.append(("MICRO", _inv_dir))
            reasons.append(
                f"MICRO  Prior candle late-phase inversion"
                f" -> {'PUT' if _inv_dir < 0 else 'CALL'} (x1)")

    # ═══════════════════════════════════════════════════════════════════════
    # THEORY 4 — DIVERGENCE (price vs momentum/tick-pressure)
    # ═══════════════════════════════════════════════════════════════════════

    _DIV_LOOKBACK = 12
    if len(candles) >= _DIV_LOOKBACK:
        _div_cands = candles[-_DIV_LOOKBACK:]
        _sw_highs: list[tuple[int, float, float, float | None]] = []
        _sw_lows:  list[tuple[int, float, float, float | None]] = []
        _micro_by_time: dict[int, dict] = {}
        if micro_history:
            for _mh in micro_history:
                _mt = _mh.get("time")
                if _mt is not None:
                    _micro_by_time[int(_mt)] = _mh

        for i in range(1, len(_div_cands) - 1):
            _sh = _div_cands[i]["high"]
            _sl = _div_cands[i]["low"]
            _sr = _div_cands[i]["high"] - _div_cands[i]["low"]
            _sb = abs(_div_cands[i]["close"] - _div_cands[i]["open"])
            _mom = _sb / _sr if _sr > 0 else 0
            _mh_row = _micro_by_time.get(int(_div_cands[i]["time"]))
            _tick_bp = _mh_row.get("buy_pct") if _mh_row else None
            if _sh >= _div_cands[i - 1]["high"] and _sh >= _div_cands[i + 1]["high"]:
                _sw_highs.append((i, _sh, _mom, _tick_bp))
            if _sl <= _div_cands[i - 1]["low"] and _sl <= _div_cands[i + 1]["low"]:
                _sw_lows.append((i, _sl, _mom, _tick_bp))

        # Bearish divergence: higher high but momentum weaker
        if len(_sw_highs) >= 2:
            _h_last = _sw_highs[-1]
            _h_prev = _sw_highs[-2]
            _h_sep = _h_last[0] - _h_prev[0]
            _h_fresh = (len(_div_cands) - 1 - _h_last[0]) <= 5
            if (_h_sep >= 3 and _h_fresh
                    and _h_last[1] > _h_prev[1]
                    and _h_last[2] < _h_prev[2] * 0.75
                    and _h_prev[2] > 0.15):
                _div_mag = 2
                _div_detail = f"momentum {_h_prev[2]:.0%} -> {_h_last[2]:.0%}"
                if (_h_last[3] is not None and _h_prev[3] is not None
                        and _h_last[3] < _h_prev[3] * 0.85):
                    _div_mag = 3
                    _div_detail += (f" + tick pressure {_h_prev[3]:.0%}"
                                    f" -> {_h_last[3]:.0%}")
                score -= _div_mag
                indep_dirs.append(("DIVERGENCE", -1))
                reasons.append(
                    f"DIVERGENCE Bearish: higher high ({_h_prev[1]:.5g}"
                    f" -> {_h_last[1]:.5g}), {_div_detail} -> PUT (x{_div_mag})")

        # Bullish divergence: lower low but momentum stronger
        if len(_sw_lows) >= 2:
            _l_last = _sw_lows[-1]
            _l_prev = _sw_lows[-2]
            _l_sep = _l_last[0] - _l_prev[0]
            _l_fresh = (len(_div_cands) - 1 - _l_last[0]) <= 5
            if (_l_sep >= 3 and _l_fresh
                    and _l_last[1] < _l_prev[1]
                    and _l_last[2] > _l_prev[2] * 1.25
                    and _l_prev[2] > 0.15):
                _div_mag = 2
                _div_detail = f"momentum {_l_prev[2]:.0%} -> {_l_last[2]:.0%}"
                if (_l_last[3] is not None and _l_prev[3] is not None
                        and _l_last[3] > _l_prev[3] * 1.15):
                    _div_mag = 3
                    _div_detail += (f" + tick pressure {_l_prev[3]:.0%}"
                                    f" -> {_l_last[3]:.0%}")
                score += _div_mag
                indep_dirs.append(("DIVERGENCE", +1))
                reasons.append(
                    f"DIVERGENCE Bullish: lower low ({_l_prev[1]:.5g}"
                    f" -> {_l_last[1]:.5g}), {_div_detail} -> CALL (x{_div_mag})")

    # ═══════════════════════════════════════════════════════════════════════
    # THEORY 5 — LIVE (running candle real-time tick vote, Phase 1)
    # ═══════════════════════════════════════════════════════════════════════

    if (ENABLE_LIVE_THEORY and running_ticks
            and len(running_ticks) >= 15):
        _rt = running_ticks
        _r_up = sum(1 for i in range(1, len(_rt)) if _rt[i] > _rt[i-1])
        _r_dn = sum(1 for i in range(1, len(_rt)) if _rt[i] < _rt[i-1])
        _r_tot = _r_up + _r_dn
        _r_bpct = (_r_up / _r_tot) if _r_tot else 0.5

        _r_open = _rt[0]
        _r_close = _rt[-1]
        _r_hi = max(_rt)
        _r_lo = min(_rt)
        _r_range = _r_hi - _r_lo
        _r_close_pos = ((_r_close - _r_lo) / _r_range) if _r_range > 0 else 0.5
        _r_bull_closed = _r_close >= _r_open

        _r_eff_buyer  = _r_bpct >= 0.60
        _r_eff_seller = _r_bpct <= 0.40
        _r_res_buyer  = _r_close_pos >= 0.72
        _r_res_seller = _r_close_pos <= 0.20

        # LIVE ABSORPTION — mirrors RUN x4 logic but on running candle
        if _r_res_buyer and _r_eff_seller:
            score -= 3
            indep_dirs.append(('LIVE', -1))
            reasons.append(
                f'LIVE  ABSORPTION on running candle: closed up but '
                f'sellers pushed ({(1-_r_bpct):.0%}) -> PUT (x3)')
        elif _r_res_seller and _r_eff_buyer:
            score += 3
            indep_dirs.append(('LIVE', +1))
            reasons.append(
                f'LIVE  ABSORPTION on running candle: closed down but '
                f'buyers pushed ({_r_bpct:.0%}) -> CALL (x3)')

        # LIVE MOMENTUM — three phases all same direction = continuation
        _n = len(_rt)
        _t3 = max(_n // 3, 1)
        _r_early = _rt[_t3] - _rt[0]
        _r_mid   = _rt[2*_t3] - _rt[_t3]
        _r_late  = _rt[-1] - _rt[2*_t3]
        if _r_early > 0 and _r_mid > 0 and _r_late > 0:
            score += 1
            forced_score += 1
            reasons.append('LIVE  3-phase UP momentum -> CALL (x1)')
        elif _r_early < 0 and _r_mid < 0 and _r_late < 0:
            score -= 1
            forced_score -= 1
            reasons.append('LIVE  3-phase DOWN momentum -> PUT (x1)')

        # LIVE INVASION — final ~1/6 ticks moving opposite to close
        _fn = max(_n // 6, 6)
        _fin = _rt[-_fn:]
        _fu = sum(1 for i in range(1, len(_fin)) if _fin[i] > _fin[i-1])
        _fd = sum(1 for i in range(1, len(_fin)) if _fin[i] < _fin[i-1])
        _ftot = _fu + _fd
        if _ftot >= 3:
            _fbp = _fu / _ftot
            if _r_bull_closed and _fbp <= 0.30:
                score -= 2
                indep_dirs.append(('LIVE', -1))
                reasons.append(
                    f'LIVE  Sellers invaded final {_fn} ticks ({1-_fbp:.0%})'
                    f' of a green running candle -> PUT (x2)')
            elif (not _r_bull_closed) and _fbp >= 0.70:
                score += 2
                indep_dirs.append(('LIVE', +1))
                reasons.append(
                    f'LIVE  Buyers invaded final {_fn} ticks ({_fbp:.0%})'
                    f' of a red running candle -> CALL (x2)')

        # LIVE WICK REJECTION — running candle's long wick
        if _r_range > 0:
            _r_upper_wick = _r_hi - max(_r_open, _r_close)
            _r_lower_wick = min(_r_open, _r_close) - _r_lo
            if _r_upper_wick > _r_range * 0.45:
                score -= 1
                indep_dirs.append(('LIVE', -1))
                reasons.append(
                    'LIVE  Long upper wick on running candle -> PUT (x1)')
            if _r_lower_wick > _r_range * 0.45:
                score += 1
                indep_dirs.append(('LIVE', +1))
                reasons.append(
                    'LIVE  Long lower wick on running candle -> CALL (x1)')

    # ═══════════════════════════════════════════════════════════════════════
    # THEORY 6 — TICKSWEEP (running candle stop hunt detection)
    #
    # Detects a spike beyond a recent local extreme that snaps back within
    # a few ticks — classic stop-run before reversal. OTC markets are full
    # of retail stop clusters that get hunted. This catches the footprint
    # in real time on the running candle.
    # ═══════════════════════════════════════════════════════════════════════

    if (ENABLE_TICKSWEEP and running_ticks
            and len(running_ticks) >= 20):
        _ts_rt = running_ticks
        _ts_n = len(_ts_rt)
        _ts_hi_idx = _ts_rt.index(max(_ts_rt))
        _ts_lo_idx = _ts_rt.index(min(_ts_rt))
        # Extreme must be in the middle 60% of the candle timeline (not at
        # the open/close edge — an edge extreme is just the candle forming,
        # not a hunt).
        _ts_in_middle = lambda idx: _ts_n * 0.20 <= idx <= _ts_n * 0.80

        # Upper sweep: high in middle, then price retraced ≥50% of the spike
        if _ts_in_middle(_ts_hi_idx):
            _ts_peak = _ts_rt[_ts_hi_idx]
            _ts_after = _ts_rt[_ts_hi_idx:_ts_hi_idx + 8]
            _ts_retrace = (_ts_peak - min(_ts_after)) if _ts_after else 0
            _ts_excursion = _ts_peak - _ts_rt[0]
            if (_ts_excursion > 0
                    and _ts_retrace >= 0.50 * _ts_excursion
                    and _ts_excursion >= (_ts_peak - _ts_rt[_ts_lo_idx]) * 0.30):
                score -= 3
                indep_dirs.append(('TICKSWEEP', -1))
                reasons.append(
                    f'TICKSWEEP  Upper stop-hunt at tick {_ts_hi_idx}'
                    f' (retraced {_ts_retrace / _ts_excursion:.0%})'
                    f' -> PUT (x3)')

        # Lower sweep: low in middle, then price retraced ≥50% of the drop
        if _ts_in_middle(_ts_lo_idx):
            _ts_trough = _ts_rt[_ts_lo_idx]
            _ts_after = _ts_rt[_ts_lo_idx:_ts_lo_idx + 8]
            _ts_retrace = (max(_ts_after) - _ts_trough) if _ts_after else 0
            _ts_excursion = _ts_rt[0] - _ts_trough
            if (_ts_excursion > 0
                    and _ts_retrace >= 0.50 * _ts_excursion
                    and _ts_excursion >= (_ts_rt[_ts_hi_idx] - _ts_trough) * 0.30):
                score += 3
                indep_dirs.append(('TICKSWEEP', +1))
                reasons.append(
                    f'TICKSWEEP  Lower stop-hunt at tick {_ts_lo_idx}'
                    f' (retraced {_ts_retrace / _ts_excursion:.0%})'
                    f' -> CALL (x3)')

    # ═══════════════════════════════════════════════════════════════════════
    # THEORY 7 — ABSORBWALL (running candle price-band absorption)
    #
    # Detects heavy opposing pressure absorbed at a single price band on
    # the running candle — e.g. many sell ticks hit the upper band but
    # price refuses to break through. Smart-money footprint. This is the
    # localized version of RUN ABSORPTION (which is whole-candle).
    # ═══════════════════════════════════════════════════════════════════════

    if (ENABLE_ABSORBWALL and running_ticks
            and len(running_ticks) >= 25):
        _aw_rt = running_ticks
        _aw_hi = max(_aw_rt)
        _aw_lo = min(_aw_rt)
        _aw_range = _aw_hi - _aw_lo
        if _aw_range > 0:
            _aw_band_size = _aw_range * 0.10  # 10% of range = band width
            _aw_hi_band = _aw_hi - _aw_band_size  # upper band lower edge
            _aw_lo_band = _aw_lo + _aw_band_size  # lower band upper edge

            # Count opposing ticks at upper band (sellers hitting resistance)
            _aw_upper_sells = sum(
                1 for i in range(1, len(_aw_rt))
                if _aw_rt[i] > _aw_hi_band and _aw_rt[i] < _aw_rt[i-1])
            _aw_upper_total = sum(1 for t in _aw_rt if t > _aw_hi_band)

            # Count opposing ticks at lower band (buyers hitting support)
            _aw_lower_buys = sum(
                1 for i in range(1, len(_aw_rt))
                if _aw_rt[i] < _aw_lo_band and _aw_rt[i] > _aw_rt[i-1])
            _aw_lower_total = sum(1 for t in _aw_rt if t < _aw_lo_band)

            _aw_threshold = 0.35  # 35% of band-ticks must be opposing

            # Upper absorption wall: sellers rejected at top -> PUT (reversal)
            if (_aw_upper_total >= 8
                    and _aw_upper_sells / _aw_upper_total >= _aw_threshold
                    and _aw_rt[-1] < _aw_hi_band):  # closed back below
                score -= 3
                indep_dirs.append(('ABSORBWALL', -1))
                reasons.append(
                    f'ABSORBWALL  {_aw_upper_sells} sell-ticks absorbed at'
                    f' upper band ({_aw_upper_sells / _aw_upper_total:.0%}'
                    f' of {_aw_upper_total} band ticks) -> PUT (x3)')

            # Lower absorption wall: buyers rejected at bottom -> CALL
            elif (_aw_lower_total >= 8
                    and _aw_lower_buys / _aw_lower_total >= _aw_threshold
                    and _aw_rt[-1] > _aw_lo_band):  # closed back above
                score += 3
                indep_dirs.append(('ABSORBWALL', +1))
                reasons.append(
                    f'ABSORBWALL  {_aw_lower_buys} buy-ticks absorbed at'
                    f' lower band ({_aw_lower_buys / _aw_lower_total:.0%}'
                    f' of {_aw_lower_total} band ticks) -> CALL (x3)')

    # ═══════════════════════════════════════════════════════════════════════
    # THEORY 8 — LATEFLIP (running candle 70/30 control transfer)
    #
    # Detects a clean intrabar control transfer: first 70% of ticks
    # dominated by one side, final 30% dominated by the opposite. This is
    # a stricter, higher-conviction variant of LIVE INVASION (which only
    # looks at the final 15%). Both segments must show clear dominance.
    # ═══════════════════════════════════════════════════════════════════════

    if (ENABLE_LATEFLIP and running_ticks
            and len(running_ticks) >= 20):
        _lf_rt = running_ticks
        _lf_n = len(_lf_rt)
        _lf_split = int(_lf_n * 0.70)
        _lf_seg_a = _lf_rt[:_lf_split]
        _lf_seg_b = _lf_rt[_lf_split:]

        def _lf_bpct(seg):
            if len(seg) < 2:
                return 0.5
            _u = sum(1 for i in range(1, len(seg)) if seg[i] > seg[i-1])
            _d = sum(1 for i in range(1, len(seg)) if seg[i] < seg[i-1])
            _t = _u + _d
            return _u / _t if _t else 0.5

        _lf_a = _lf_bpct(_lf_seg_a)
        _lf_b = _lf_bpct(_lf_seg_b)
        # Both segments must show clear dominance (≥65% one side)
        _lf_a_dom = abs(_lf_a - 0.5) >= 0.15
        _lf_b_dom = abs(_lf_b - 0.5) >= 0.15
        # And opposite directions
        _lf_opposite = ((_lf_a - 0.5) * (_lf_b - 0.5)) < 0

        if _lf_a_dom and _lf_b_dom and _lf_opposite:
            # Vote with segment B (the new control side — the flip direction)
            _lf_dir = +1 if _lf_b > 0.5 else -1
            score += _lf_dir * 3
            indep_dirs.append(('LATEFLIP', _lf_dir))
            _lf_a_lbl = (f'{_lf_a:.0%} buy' if _lf_a > 0.5
                         else f'{1 - _lf_a:.0%} sell')
            _lf_b_lbl = (f'{_lf_b:.0%} buy' if _lf_b > 0.5
                         else f'{1 - _lf_b:.0%} sell')
            reasons.append(
                f'LATEFLIP  Control transfer: first 70% {_lf_a_lbl},'
                f' last 30% {_lf_b_lbl}'
                f' -> {"CALL" if _lf_dir > 0 else "PUT"} (x3)')

    # ═══════════════════════════════════════════════════════════════════════
    # MAIN PREDICTOR — MARKET STATE deep analysis
    #
    # This is the heart of the refactor. The 8 theories above provide
    # confirming votes (4 closed-candle + 4 running-candle); market state
    # provides the PRIMARY directional read. State is named from the same
    # structural facts, organized as one coherent read instead of a pile.
    # State's own directional bias is voted ONCE, scaled by conviction,
    # never re-counted.
    # ═══════════════════════════════════════════════════════════════════════

    _st_pts: dict[str, float] = {"CONTINUATION": 0.0, "EXHAUSTION": 0.0,
                                 "REVERSAL": 0.0, "TRAP": 0.0, "RANGE": 0.0}
    _st_dir: dict[str, float] = {k: 0.0 for k in _st_pts}
    _st_ev:  dict[str, list[str]] = {k: [] for k in _st_pts}
    _trend_dir = (+1 if _regime == "UPTREND"
                  else -1 if _regime == "DOWNTREND" else 0)
    _cand_dir  = +1 if is_bull else -1
    _close_pos_ms = (c - l) / total_range
    _avg_body10 = (sum(abs(x["close"] - x["open"]) for x in candles[-10:])
                   / min(10, len(candles))) or 1e-9
    _streak = 0  # computed inline where needed
    if len(candles) >= 2:
        for _i in range(len(candles) - 1, 0, -1):
            _d = (candles[_i]["close"] >= candles[_i]["open"]) == is_bull
            if _d:
                _streak += 1
            else:
                break

    def _st(state: str, pts: float, direction: int, why: str) -> None:
        _st_pts[state] += pts
        _st_dir[state] += direction * pts
        _st_ev[state].append(why)

    # CONTINUATION — trend structure still healthy, move has fuel.
    if _trend_dir:
        _st("CONTINUATION", 2, _trend_dir,
            f"20-candle structure is a {_regime.lower()}"
            f" (second half made {'higher highs+lows' if _trend_dir > 0 else 'lower highs+lows'})")
        if _cand_dir == _trend_dir and body / total_range >= 0.55:
            _st("CONTINUATION", 2, _trend_dir,
                f"Impulse candle with the trend (body {body/total_range:.0%} of range)")
        elif _cand_dir != _trend_dir and body <= _avg_body10 * 0.6 and (
                (lower_wick >= body and lower_wick > upper_wick)
                if _trend_dir > 0 else
                (upper_wick >= body and upper_wick > lower_wick)):
            _st("CONTINUATION", 2, _trend_dir,
                "Healthy pullback: small counter-candle already wicked back"
                " in the trend direction")

    # EXHAUSTION — the move is running out of participants.
    if _streak >= 4:
        _st("EXHAUSTION", 2 + (1 if _streak >= 6 else 0), -_cand_dir,
            f"{_streak} same-color candles in a row — the move is aging")
    if len(candles) >= 3:
        _b3   = candles[-3:]
        _dir3 = [1 if x["close"] >= x["open"] else -1 for x in _b3]
        _bod3 = [abs(x["close"] - x["open"]) for x in _b3]
        if _dir3[0] == _dir3[1] == _dir3[2] and _bod3[0] > _bod3[1] > _bod3[2] > 0:
            _st("EXHAUSTION", 2, -_dir3[2],
                "Three pushes, each body smaller than the last — momentum fading")
    if micro_history:
        _exn = sum(1 for _m in micro_history[-3:]
                   if _m.get("last_react") == "EXHAUST")
        if _exn >= 2:
            _st("EXHAUSTION", 3, -_cand_dir,
                f"{_exn} of the last 3 candles ended their ticks in exhaustion")
    if body / total_range >= 0.75:
        _mb_bon, _mb_lbl = _sr_bonus(h if is_bull else l, not is_bull)
        if _mb_bon >= 1:
            _st("EXHAUSTION", 2, -_cand_dir,
                f"Full-power candle ran straight into a tested level ({_mb_lbl})")
    if _trend_dir > 0 and _zone == "RESISTANCE" and upper_wick > total_range * 0.45:
        _st("EXHAUSTION", 2, -1,
            "Long upper rejection wick right at the top of the up-move")
    elif _trend_dir < 0 and _zone == "SUPPORT" and lower_wick > total_range * 0.45:
        _st("EXHAUSTION", 2, +1,
            "Long lower rejection wick right at the bottom of the down-move")
    if _cur_bpct is not None and (_cur_bpct >= 0.78 or _cur_bpct <= 0.22):
        _st("EXHAUSTION", 1, -_cand_dir,
            f"{max(_cur_bpct, 1 - _cur_bpct):.0%} of ticks were one-sided —"
            f" that side is out of ammo")

    # REVERSAL — exhaustion PLUS a confirming counter-pattern.
    _rev_conf = 0
    if _cur_bpct is not None:
        if _close_pos_ms >= 0.72 and _cur_bpct <= 0.40:
            _st("REVERSAL", 3, -1,
                "Absorption: closed near the high but most ticks pushed DOWN"
                " — buyers are being sold into")
            _rev_conf += 1
        elif _close_pos_ms <= 0.20 and _cur_bpct >= 0.60:
            _st("REVERSAL", 3, +1,
                "Absorption: closed near the low but most ticks pushed UP"
                " — sellers are being bought into")
            _rev_conf += 1
    if upper_wick / total_range > 0.55 and body / total_range < 0.25:
        _pin_anch = _zone == "RESISTANCE"
        _st("REVERSAL", 3 if _pin_anch else 2, -1,
            "Shooting star: the push above was rejected"
            + (" — right at the resistance zone" if _pin_anch else ""))
        _rev_conf += 1
    elif lower_wick / total_range > 0.55 and body / total_range < 0.25:
        _pin_anch = _zone == "SUPPORT"
        _st("REVERSAL", 3 if _pin_anch else 2, +1,
            "Hammer: the push below was rejected"
            + (" — right at the support zone" if _pin_anch else ""))
        _rev_conf += 1
    prev_body = abs(prev["close"] - prev["open"])
    prev_bull = prev["close"] >= prev["open"]
    if (prev_body > 0 and is_bull != prev_bull and body / prev_body >= 1.0
            and _trend_dir and _cand_dir != _trend_dir):
        _st("REVERSAL", 2, _cand_dir,
            "Counter-trend engulfing: the reply candle swallowed the whole"
            " prior body")
        _rev_conf += 1
    if _rev_conf and _st_pts["EXHAUSTION"] < 2 and _zone == "NEUTRAL":
        _st_pts["REVERSAL"] *= 0.5
        _st_dir["REVERSAL"] *= 0.5
        _st_ev["REVERSAL"].append(
            "(unanchored: no exhaustion context, mid-range — weight halved)")

    # TRAP — someone was just baited into a losing position.
    if body / total_range >= 0.68 and _cur_bpct is not None and (
            (is_bull and _cur_bpct >= 0.78)
            or (not is_bull and _cur_bpct <= 0.22)):
        _st("TRAP", 2, -_cand_dir,
            "Big one-sided candle invites chasers exactly when its fuel is spent")
    if _cur_fin_bpct is not None:
        if is_bull and _cur_fin_bpct <= 0.30:
            _st("TRAP", 1, -1,
                "Sellers invaded the final seconds of a green candle")
        elif (not is_bull) and _cur_fin_bpct >= 0.70:
            _st("TRAP", 1, +1,
                "Buyers invaded the final seconds of a red candle")
    for _fb_lvl, _fb_tch in _klevels:
        if prev["close"] > _fb_lvl >= c:
            _st("TRAP", 2, -1,
                f"Failed breakout above {_fb_lvl:.5g} (tested x{_fb_tch})"
                f" — closed back below it")
            break
        if prev["close"] < _fb_lvl <= c:
            _st("TRAP", 2, +1,
                f"Failed breakdown below {_fb_lvl:.5g} (tested x{_fb_tch})"
                f" — closed back above it")
            break

    # RANGE — no direction to continue or reverse; oscillation.
    if _trend_dir == 0:
        _st("RANGE", 2, 0,
            "No directional structure in the last 20 candles (sideways)")
        if _zone == "RESISTANCE":
            _st("RANGE", 1, -1, "Price at the top of the range — fade zone")
        elif _zone == "SUPPORT":
            _st("RANGE", 1, +1, "Price at the bottom of the range — fade zone")
    if _streak <= 1 and len(candles) >= 4:
        _zz_len_local = 1
        for _i in range(len(candles) - 2, max(len(candles) - 8, 0), -1):
            _d = (candles[_i]["close"] >= candles[_i]["open"])
            if _d != is_bull:
                _zz_len_local += 1
            else:
                break
        if _zz_len_local >= 4:
            _st("RANGE", 2, -_cand_dir,
                f"{_zz_len_local} candles alternating color — oscillation, not a move")
    if body / total_range <= 0.08 or (
            body / total_range <= 0.30 and upper_wick / total_range >= 0.28
            and lower_wick / total_range >= 0.28):
        _st("RANGE", 1, 0, "Indecision candle (doji / spinning top)")

    # ── State winner ───────────────────────────────────────────────────────
    _st_prio = ["TRAP", "REVERSAL", "EXHAUSTION", "CONTINUATION", "RANGE"]
    _st_win  = max(_st_prio, key=lambda k: (_st_pts[k], -_st_prio.index(k)))
    _st_tot  = sum(_st_pts.values())

    if _st_pts[_st_win] < 3:
        market_state = {
            "state": "UNCLEAR", "bias": "NEUTRAL", "conviction": 0,
            "points": {k: round(v, 1) for k, v in _st_pts.items()},
            "evidence": ["Not enough structural evidence for any single"
                         " market state this candle"],
        }
        _ms_vote_dir = 0
        _ms_vote_mag = 0
    else:
        _st_bd = _st_dir[_st_win]
        _ms_bias = ("CALL" if _st_bd > 0 else "PUT" if _st_bd < 0 else "NEUTRAL")
        _ms_conv = round(100 * _st_pts[_st_win] / _st_tot) if _st_tot else 0
        market_state = {
            "state": _st_win,
            "bias": _ms_bias,
            "conviction": _ms_conv,
            "points": {k: round(v, 1) for k, v in _st_pts.items()},
            "evidence": _st_ev[_st_win],
        }
        # ── MARKET STATE VOTE (NEW in this refactor) ──────────────────────
        # The deep-analysis state now CONTRIBUTES to score — scaled by
        # conviction. This is the ONLY place state touches score; the 5
        # theories above do not see state and state does not see them.
        # Vote weight = floor(conviction / 25) + 1, capped at 4.
        #   conviction 0-24  → no vote (UNCLEAR)
        #   conviction 25-49 → x1
        #   conviction 50-74 → x2
        #   conviction 75-99 → x3
        #   conviction 100   → x4
        if _ms_bias in ("CALL", "PUT") and _ms_conv >= 25:
            _ms_vote_dir = 1 if _ms_bias == "CALL" else -1
            _ms_vote_mag = min(4, max(1, _ms_conv // 25))
            score += _ms_vote_dir * _ms_vote_mag
            indep_dirs.append(("MARKET_STATE", _ms_vote_dir))
            reasons.append(
                f"MARKET_STATE  {_st_win} (bias {_ms_bias},"
                f" conviction {_ms_conv}%) -> {_ms_bias} (x{_ms_vote_mag})")
        else:
            _ms_vote_dir = 0
            _ms_vote_mag = 0

    # ═══════════════════════════════════════════════════════════════════════
    # THEORY MUTE GATE — applies to the 5 confirming theories + MARKET_STATE
    # ═══════════════════════════════════════════════════════════════════════

    if muted:
        _FORCED_ONLY = set()  # no color-forced theories in this refactor
        for _mi, _mr in enumerate(reasons):
            _mv = _parse_votes([_mr])
            if not _mv:
                continue
            _mc, _md, _mm = _mv[0]
            if _mc in muted:
                score -= _md * _mm
                if _mc in _FORCED_ONLY:
                    forced_score -= _md * _mm
                reasons[_mi] = _mr + f" [MUTED {muted[_mc]}]"
        indep_dirs = [(_t, _d) for (_t, _d) in indep_dirs if _t not in muted]

    # ═══════════════════════════════════════════════════════════════════════
    # INFORMATION WEIGHT — unified dampen for low-information candles
    # (replaces the old ATR_WEIGHT / TICK_VOL / SESSION_WEIGHT trio)
    # ═══════════════════════════════════════════════════════════════════════

    _weak_cap_reasons: list[str] = []

    # (a) Tick-count dampen — too few ticks = unreliable RUN/LIVE reads
    if ticks is not None and len(ticks) < 15:
        _tc_damp = max(1, int(abs(score) * 0.30))
        score += -_tc_damp if score > 0 else _tc_damp
        reasons.append(
            f"(low ticks) only {len(ticks)} ticks -> -{_tc_damp} dampen")

    # (b) Tiny-range candle dampen — no information content
    if _atr10 > 0 and total_range < _atr10 * 0.30:
        _tr_damp = max(1, int(abs(score) * 0.25))
        score += -_tr_damp if score > 0 else _tr_damp
        reasons.append(
            f"(tiny range) range {total_range/_atr10:.0%} of ATR"
            f" -> -{_tr_damp} dampen")

    # (c) Low-liquidity session dampen (22:00-07:00 UTC)
    _hour_utc = time.gmtime().tm_hour
    if _hour_utc >= 22 or _hour_utc < 7:
        _ss_damp = max(1, int(abs(score) * 0.20))
        score += -_ss_damp if score > 0 else _ss_damp
        reasons.append(
            f"(low-liquidity session) UTC {_hour_utc:02d}h"
            f" -> -{_ss_damp} dampen")

    # ═══════════════════════════════════════════════════════════════════════
    # FINAL — signal, strength calibration
    # ═══════════════════════════════════════════════════════════════════════

    _indep_net = sum(_d for _t, _d in indep_dirs)
    signal = "CALL" if score > 0 else "PUT" if score < 0 else "NEUTRAL"

    # Signal cooldown (per-asset+period, 30s)
    _cooldown_key = f"{asset}:{period}" if asset and period else None
    if _cooldown_key and score != 0:
        _now = time.time()
        if _now - _last_signal_time.get(_cooldown_key, 0) < _COOLDOWN_SECONDS:
            _weak_cap_reasons.append(
                f"(coordination cooldown) repeat signal within"
                f" {_COOLDOWN_SECONDS}s -> WEAK")
        _last_signal_time[_cooldown_key] = _now

    # NEUTRAL tiebreak — weakest-first honesty
    if signal == "NEUTRAL":
        if _indep_net != 0:
            signal = "CALL" if _indep_net > 0 else "PUT"
            _weak_cap_reasons.append(
                f"TIEBREAK: score 0 — color-independent evidence leans"
                f" {signal} -> {signal} (forced pick, WEAK)")
        elif _regime in ("UPTREND", "DOWNTREND"):
            signal = "CALL" if _regime == "UPTREND" else "PUT"
            _weak_cap_reasons.append(
                f"TIEBREAK: score 0, no independent lean — following"
                f" {_regime} -> {signal} (forced pick, WEAK)")
        else:
            signal = "CALL" if is_bull else "PUT"
            _weak_cap_reasons.append(
                f"TIEBREAK: zero evidence — repeating last candle color"
                f" -> {signal} (coin flip, WEAK)")
    elif abs(score) < 2:
        _weak_cap_reasons.append(
            f"NO EDGE: |score|={abs(score)} is noise-level -> WEAK")

    # UNSTABLE BASE — doji / spinning-top / marubozu base candles measured
    # 47-48% live (kept from old code; still a real effect).
    _base_body = body / total_range
    _base_uw   = upper_wick / total_range
    _base_lw   = lower_wick / total_range
    if _base_body <= 0.08:
        _weak_cap_reasons.append(
            "UNSTABLE BASE: doji base candle — measured 48.3% live (n=232)"
            " -> WEAK")
    elif _base_body <= 0.30 and _base_uw >= 0.28 and _base_lw >= 0.28:
        _weak_cap_reasons.append(
            "UNSTABLE BASE: spinning-top base candle — measured 47.3% live"
            " (n=237) -> WEAK")
    elif _base_body >= 0.75:
        _weak_cap_reasons.append(
            "UNSTABLE BASE: marubozu base candle — measured 47.3% live"
            " (n=859) -> WEAK")

    reasons.extend(_weak_cap_reasons)
    confidence = round(min(abs(score) / MAX_SCORE, 1.0), 2)

    # AGREEMENT — distinct theories net-voting the winning side
    _net_votes: dict[str, int] = {}
    for _code, _vdir, _vmag in _parse_votes(reasons, include_muted=False):
        _net_votes[_code] = _net_votes.get(_code, 0) + _vdir * _vmag
    _want = 1 if signal == "CALL" else -1
    agree = sum(1 for _nv in _net_votes.values() if _nv * _want > 0)
    agree_weight = sum(abs(_nv) for _nv in _net_votes.values()
                       if _nv * _want > 0)

    # Strength calibration. With 6 theories max, score scale is smaller:
    #   |score| 0-1 → WEAK (noise or tiebreak)
    #   |score| 2-3 + agree 2+ → MEDIUM
    #   |score| 4+  + agree 3+ + agree_weight 4+ → STRONG
    # OVERHEATED guard removed (no pile-on possible with 6 theories).
    if _weak_cap_reasons:
        strength = "WEAK"
    elif agree >= 3 and agree_weight >= 4 and abs(score) >= 4:
        strength = "STRONG"
    elif agree >= 2 and abs(score) >= 2:
        strength = "MEDIUM"
    else:
        strength = "WEAK"

    return {
        "signal":     signal,
        "score":      score,
        "confidence": confidence,
        "agree":      agree,
        "agree_weight": agree_weight,
        "strength":   strength,
        "reasons":    reasons,
        "key_levels": [[round(p, 6), t] for p, t in
                       sorted(_klevels, key=lambda x: -x[1])[:20]],
        "wick_walls": {
            "support":    [[round(p, 6), t] for p, t in
                           sorted(_sup_walls, key=lambda x: -x[1])[:10]],
            "resistance": [[round(p, 6), t] for p, t in
                           sorted(_res_walls, key=lambda x: -x[1])[:10]],
        },
        "regime":     {"trend": _regime, "zone": _zone},
        # Deep-analysis market-state read — MAIN PREDICTOR (refactored 2026-07-10).
        # Its directional vote is already in score/reasons above; this dict is
        # the human-readable card shown in the UI.
        "market_state": market_state,
    }
