"""
End-of-candle (EOC) analysis — price-action multi-signal predictor.

Design: classic lagging indicators (EMA, RSI) are intentionally NOT used.
On binary color-prediction they lag and add noise. Signals are built only
from what price actually does — see _ALL_THEORIES for the active vote set:

  - Tick microstructure : buy/sell pressure, effort vs result, wick rejection
                          (RUN), exhausted one-sided candles (TRAP), prior-
                          candle tick patterns from the DB (MICRO)
  - Candle patterns      : engulfing/piercing at S/R (T7), pin bar (T2),
                           marubozu (MARB), morning/evening star (STAR),
                           three outside up (OUTSIDE), spinning top (SPIN),
                           streak exhaustion (STREAK), alternation (ZIGZAG)
  - Market structure     : liquidity sweep (SWEEP), repeated wick-rejection
                           zones (WICKWALL), regime/zone context (REGIME —
                           score filter only, not a graded vote)

Bias controls (2026-07-04 audit — 87% of live signals were just repeating
the last candle's color at coin-flip accuracy):
  - COLOR-GATED CAP  : votes whose direction is mechanically forced by the
                       closed candle's color contribute at most ±1 total
  - PARROT GUARD     : a signal pointing with the candle needs color-
                       independent theories to net-agree, else it is capped
                       to WEAK
  - NOISE/TIEBREAK   : |score| < 2, and zero-score forced picks, are capped
                       to WEAK
  - THEORY MUTE GATE : live 7-day per-theory accuracy (db.theory_perf via
                       feed.py) mutes theories proven below coin-flip

Every-candle mode (2026-07-06, user decision): a direction is emitted on
EVERY analyzed candle; the guards above demote quality to WEAK instead of
withholding the signal, so STRONG/MEDIUM remain the honest subset.

Note: Academic research (and this account's own logged history) confirms
      1-minute binary options are near-random. Accuracy claims beyond ~50%
      would be self-deception — the strength label, not signal frequency,
      is what separates evidence from forced picks.
"""
import math
import re


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
    best: tuple[float, float, str] | None = None
    for frac, label, thr in [(0.01, "BIG", 0.05), (0.005, "MID", 0.06), (0.001, "SMALL", 0.10)]:
        step  = mag * frac
        level = round(round(price / step) * step, 8)
        dist  = abs(price - level)
        if dist < step * thr and (best is None or dist < best[1]):
            best = (level, dist, label)
    if best:
        return best
    step  = mag * 0.01
    level = round(round(price / step) * step, 8)
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

    # Swing pivots: a high higher than both neighbours, a low lower than both.
    pivots: list[float] = []
    for i in range(1, len(recent) - 1):
        hi, lo = recent[i]["high"], recent[i]["low"]
        if hi >= recent[i - 1]["high"] and hi >= recent[i + 1]["high"]:
            pivots.append(hi)
        if lo <= recent[i - 1]["low"] and lo <= recent[i + 1]["low"]:
            pivots.append(lo)
    if not pivots:
        return []

    # Cluster pivots within a small tolerance → one level, N touches. Compare to
    # the cluster ANCHOR (first point), not the last, so dense pivots can't
    # chain-merge into one giant cluster spanning a wide price range.
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


def _wick_wall(candles: list[dict], lookback: int = 12
               ) -> tuple[list[tuple[float, int]], list[tuple[float, int]], float]:
    """
    Detect repeated wick rejection zones — the 'wick wall'.

    _key_levels() uses formal swing PIVOTS (a low lower than BOTH neighbours).
    It misses the common case where 4-6 CONSECUTIVE RANGING candles all put
    wicks at the same tight zone without any one being a formal pivot.

    This function clusters ALL lower/upper wick tips from the last N candles.
    Clustering tolerance = 25% of avg candle ATR — auto-adjusts across all
    instruments (EUR/USD 0.0001 scale, USD/JPY 0.01 scale, crypto, etc.)
    without hardcoded pip values.

    Returns (support_walls, resistance_walls, avg_range):
      support_walls    : [(price, count), ...] — lower wick clusters
      resistance_walls : [(price, count), ...] — upper wick clusters
      avg_range        : mean candle range (for scoring tolerance calibration)
    """
    recent = candles[-lookback:]
    if len(recent) < 4:
        return [], [], 0.0

    avg_rng = sum(c["high"] - c["low"] for c in recent) / len(recent)
    tol     = avg_rng * 0.25   # 25% of ATR — instrument-agnostic cluster width

    def _cluster(tips: list[float]) -> list[tuple[float, int]]:
        s = sorted(tips)
        out: list[tuple[float, int]] = []
        grp = [s[0]]
        for t in s[1:]:
            if t - grp[0] <= tol:   # anchor-based: first element sets the window
                grp.append(t)
            else:
                if len(grp) >= 3:
                    out.append((sum(grp) / len(grp), len(grp)))
                grp = [t]
        if len(grp) >= 3:
            out.append((sum(grp) / len(grp), len(grp)))
        return out

    return (_cluster([c["low"]  for c in recent]),
            _cluster([c["high"] for c in recent]),
            avg_rng)


def _market_regime(candles: list[dict], lookback: int = 20) -> tuple[str, str]:
    """
    Detect market regime + current price zone from recent candle structure.

    Splits the lookback window in half: if the second half shows higher highs
    AND higher lows vs the first half -> UPTREND; lower highs + lower lows ->
    DOWNTREND; mixed -> SIDEWAYS.

    Zone measures where the current close sits in the full lookback range:
    bottom 25% = SUPPORT, top 25% = RESISTANCE, middle = NEUTRAL.

    Returns (regime, zone).
    """
    recent = candles[-lookback:]
    if len(recent) < 8:
        return "SIDEWAYS", "NEUTRAL"

    half        = len(recent) // 2
    first_half  = recent[:half]
    second_half = recent[half:]

    fh_hi = max(c["high"] for c in first_half)
    fh_lo = min(c["low"]  for c in first_half)
    sh_hi = max(c["high"] for c in second_half)
    sh_lo = min(c["low"]  for c in second_half)

    if sh_hi > fh_hi and sh_lo > fh_lo:
        regime = "UPTREND"
    elif sh_hi < fh_hi and sh_lo < fh_lo:
        regime = "DOWNTREND"
    else:
        regime = "SIDEWAYS"

    full_hi = max(fh_hi, sh_hi)
    full_lo = min(fh_lo, sh_lo)
    full_rng = full_hi - full_lo
    if full_rng <= 0:
        zone = "NEUTRAL"
    else:
        close_pos = (recent[-1]["close"] - full_lo) / full_rng
        if close_pos <= 0.25:
            zone = "SUPPORT"
        elif close_pos >= 0.75:
            zone = "RESISTANCE"
        else:
            zone = "NEUTRAL"

    return regime, zone


def _zigzag_signal(candles: list[dict], min_len: int = 4) -> tuple[int, int]:
    """
    Detect alternating bull/bear zigzag pattern in the last 8 candles.

    OTC RNG often produces alternating candles (G-R-G-R ...). When 4+ candles
    alternate consistently, predicting opposite of the last candle has edge.

    Returns (predict, length):
      predict : +1 (CALL), -1 (PUT), 0 (no pattern)
      length  : length of the alternating sequence (0 if no pattern)
    """
    window = candles[-8:]
    if len(window) < min_len:
        return 0, 0

    directions = [1 if c["close"] >= c["open"] else -1 for c in window]

    # Count alternating run from the end backwards
    seq_len = 1
    for i in range(len(directions) - 2, -1, -1):
        if directions[i] != directions[i + 1]:
            seq_len += 1
        else:
            break

    if seq_len < min_len:
        return 0, 0

    predict = -directions[-1]   # opposite of last candle direction
    return predict, seq_len


# ── Main analysis ─────────────────────────────────────────────────────────────

def _parse_votes(reasons: list[str],
                 include_muted: bool = True) -> list[tuple[str, int, int]]:
    """
    Extract individual theory votes from reason strings.

    Returns [(theory_code, direction, magnitude), ...] where direction is
    +1 (CALL) / -1 (PUT). Attenuation/coordination adjustment lines are
    skipped — they are score corrections, not theory votes.

    include_muted: "[MUTED ...]"-suffixed lines are votes that the live
    theory-performance gate excluded from the score. Grading/shadow-logging
    wants them INCLUDED (default — a muted theory keeps building its track
    record so it can earn its way back); the `agree` count wants them
    EXCLUDED (a muted vote must not lend strength to a signal).
    """
    out: list[tuple[str, int, int]] = []
    for r in reasons:
        if "(attenuation)" in r or "(coordination" in r:
            continue
        if not include_muted and "[MUTED" in r:
            continue
        code = r.split()[0]
        if code not in _ALL_THEORIES:
            continue
        if "-> CALL" in r:
            d = +1
        elif "-> PUT" in r:
            d = -1
        else:
            continue
        m = re.search(r"\(x(\d+)\)", r)
        out.append((code, d, int(m.group(1)) if m else 1))
    return out


_ALL_THEORIES = {"RUN", "T7", "T2", "SWEEP", "MARB", "TRAP", "STAR", "STREAK",
                 "MICRO", "OUTSIDE", "SPIN",
                 "ZIGZAG", "WICKWALL", "MTF"}
# HARAMI, THREE, GAP: theories removed 2026-07-03 (see inline comments where
# their scoring blocks used to be). REGIME: converted from an independent
# vote to a score-only filter (2026-07-03) — its reasons still adjust score
# but are intentionally excluded here so it's no longer graded as its own
# theory or counted toward `agree` (WITH_REGIME measured 44.6% vs
# COUNTER_REGIME 54.5% — a real signal, but on being right about the
# ensemble's OTHER votes, not on REGIME being a reliable standalone caller).


def analyze_eoc(candles: list[dict], ticks: list[float] | None = None,
                micro_history: list[dict] | None = None,
                period: int | None = None,
                muted: dict[str, str] | None = None) -> dict:
    """
    Predict next candle direction from the just-closed candle.

    Signal sources — the graded vote set is _ALL_THEORIES (see the module
    docstring for the one-line description of each); REGIME adjusts score
    as a context filter without being graded as a vote. Ensemble-level
    bias controls (color-gated cap, parrot guard, dead band, theory mute
    gate) are documented at their blocks below.

    candles       : OHLC history, most-recent last. Need 2+ minimum.
    ticks         : price values from the just-closed candle (for RUN).
    micro_history : list of micro dicts for the N candles BEFORE the
                    just-closed one (from db.get_micro_history). Oldest first.
    period        : candle period in seconds — used to verify micro_history[-1]
                    really is the immediately-previous candle (freshness gate).
    muted         : {theory_code: annotation} from the live theory-performance
                    gate (feed.py builds it from db.theory_perf with
                    hysteresis). A muted theory's votes are still computed and
                    listed in reasons (suffixed "[MUTED <annotation>]") and
                    still shadow-graded, but contribute nothing to score,
                    `agree`, or the parrot guard.
    Returns : {signal, score, confidence, agree, strength, reasons}
    """
    # MAX_SCORE calibrates the confidence% shown to the user (confidence =
    # |score|/MAX_SCORE), set to the empirical p99 of |score| plus small
    # headroom so the number actually spans ~0-100% (a naive theoretical
    # ceiling squashed it near zero — see git history). Recalibrated
    # 2026-07-04 after the bias-audit rework (COLOR-GATED CAP, WICKWALL
    # de-gate, T7 cap): full-history replay (tools/replay_eoc.py) measured
    # p50=2, p90=5, p99=9, max=12 under the new weights. Re-check via the
    # replay harness if the theory set changes materially.
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
    # (theory, direction) of votes that are NOT mechanically forced by the
    # just-closed candle's color (reversal-class or color-independent
    # branches; direction +1 CALL / -1 PUT). Feeds the PARROT GUARD in the
    # Final section: a signal that merely repeats the candle's color needs
    # these to net-agree, otherwise it's just "last candle was green so
    # CALL" — the exact bias this 2026-07-04 audit was about (87% of live
    # signals did that). The theory code is carried so a MUTED theory's
    # votes can't enable the guard either.
    # Color-FORCED sites (never tagged): T7/MARB/OUTSIDE/STAR, RUN result/
    # lean reads, MICRO chains/recovery/fight/hold/persistent, since their
    # branch conditions require is_bull (or close-position, which implies it).
    indep_dirs: list[tuple[str, int]] = []
    # Running sum of those color-FORCED votes' score contribution. Capped to
    # ±FORCED_CAP after the vote sections — this is the direct fix for the
    # measured 3.8:1 continuation:reversal weight imbalance: however many
    # color-gated theories pile onto one candle, "the candle was green" is
    # worth at most 2 points, and color-independent evidence decides the rest.
    forced_score = 0

    # Market regime / zigzag state (populated later; defaults allow safe return)
    _regime     = "SIDEWAYS"
    _zone       = "NEUTRAL"
    _zz_predict = 0
    _zz_len     = 0

    # ═══════════════════════════════════════════════════════════════════════
    # PRIMARY — evaluated FIRST, weighted HIGHEST.
    #
    # Core thesis (user): the next candle's colour is decided INSIDE the last
    # 4-7 candles. So the recent window drives everything. We read that window
    # four ways (R47) and use its high/low as the live support/resistance for
    # the engulfing (T7) and pin-bar (T2) reactions — the web-researched most
    # reliable reversals, which only beat a coin-flip when they fire AT an S/R.
    # ═══════════════════════════════════════════════════════════════════════

    # ── RUN  WHO WON the running candle → NEXT candle's colour (#1 method) ───
    # Read the candle the way order-flow traders do (web-researched):
    #   RESULT  = where price CLOSED in its range (close near high = buyers won,
    #             near low = sellers won) — the scoreboard.
    #   EFFORT  = tick pressure (what % of ticks pushed up vs down) — who fought
    #             harder.
    #   ABSORPTION = effort vs result DIVERGE (closed up but sellers pushed, or
    #             closed down but buyers pushed). The visible winner is being
    #             faded by smart money → reversal. (The single most powerful read.)
    #   WICK    = a long wick = that side was rejected at the extreme.
    #   EXHAUST = the final ~15% of ticks ran out of steam → reversal.
    _cur_bpct     = None   # buying tick % of just-closed candle (used by TRAP too)
    _cur_fin_bpct = None   # final-phase buying %, only set when ftot >= 3
    if ticks and len(ticks) >= 15:
        up_t = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i - 1])
        dn_t = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i - 1])
        tot  = up_t + dn_t
        bpct = (up_t / tot) if tot else 0.5          # EFFORT: buying tick pressure
        _cur_bpct   = bpct
        close_pos = (c - l) / total_range            # RESULT: 0=low … 1=high
        bull_closed = ticks[-1] >= ticks[0]

        # DB analysis (906 predictions):
        #   "Buyers WON"  → 61.1% CALL  — valid continuation, keep.
        #   "Sellers WON" → 45.5% PUT   — anti-signal: sellers exhaust themselves in
        #   OTC synthetic markets. After extreme selling the next candle bounces UP.
        #   Fix: Sellers WON is now a CALL signal (exhaustion reversal).
        #   ABSORPTION remains the most reliable (83-92%) — keep at ×4.
        eff_buyer  = bpct >= 0.60       # clear tick majority (60%+ up-ticks)
        eff_seller = bpct <= 0.40       # clear tick majority (60%+ dn-ticks)
        res_buyer  = close_pos >= 0.72  # close in top 28% of range
        res_seller = close_pos <= 0.20  # close in bottom 20% of range

        # (1) AGREEMENT — result and effort both agree.
        if res_buyer and eff_buyer:
            score += 2
            forced_score += 2
            reasons.append(
                f"RUN  Buyers WON (close {close_pos:.0%} hi, {bpct:.0%} up-ticks)"
                f" -> CALL (x2)")
        elif res_seller and eff_seller:
            # OTC exhaustion: sellers used up all capital → next candle reverses UP.
            # DB: 45.5% for PUT = anti-signal → flipped to CALL (exhaustion reversal).
            score += 1
            indep_dirs.append(("RUN", +1))
            reasons.append(
                f"RUN  Sellers WON but exhausted (close {close_pos:.0%} lo,"
                f" {(1-bpct):.0%} dn-ticks) -> OTC reversal -> CALL (x1)")

        # (2) ABSORPTION — effort vs result DIVERGE — the single most reliable
        #     signal: 92% accuracy (closed up + sellers pushed), 83% (closed down +
        #     buyers pushed). Score tripled to reflect actual predictive power.
        elif res_buyer and eff_seller:
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

        # (3) Slight directional lean — fires when only one of (result, effort) is
        #     clear. 54.7% accuracy: worth ±1 but not a strong signal on its own.
        elif res_buyer or eff_buyer:
            score += 1
            forced_score += 1   # close-position-derived — treated as color-forced
            reasons.append("RUN  Buyers slightly ahead -> CALL")
        elif res_seller or eff_seller:
            score -= 1
            forced_score -= 1
            reasons.append("RUN  Sellers slightly ahead -> PUT")

        # (4) Wick rejection — 60% accuracy, decent confirming signal.
        if upper_wick > total_range * 0.45:
            score -= 1
            indep_dirs.append(("RUN", -1))
            reasons.append("RUN  Long upper wick: sellers rejected the top -> PUT")
        if lower_wick > total_range * 0.45:
            score += 1
            indep_dirs.append(("RUN", +1))
            reasons.append("RUN  Long lower wick: buyers defended the low -> CALL")

        # (5) Final ~15% of ticks invaded by the opposite side — 72.6% accuracy.
        #     Strong invasion (>=80%) is even more reliable: score +2 instead of +1.
        fn   = max(len(ticks) // 6, 6)
        fin  = ticks[-fn:]
        fu   = sum(1 for i in range(1, len(fin)) if fin[i] > fin[i - 1])
        fd   = sum(1 for i in range(1, len(fin)) if fin[i] < fin[i - 1])
        ftot = fu + fd
        if ftot >= 3:
            fbp = fu / ftot
            _cur_fin_bpct = fbp
            _inv_seller = (1 - fbp)
            _inv_buyer  = fbp
            if bull_closed and fbp <= 0.30:
                _mag = 2 if _inv_seller >= 0.80 else 1
                score -= _mag
                indep_dirs.append(("RUN", -1))
                reasons.append(
                    f"RUN  Sellers invaded the close ({_inv_seller:.0%})"
                    f" -> PUT (x{_mag})")
            elif (not bull_closed) and fbp >= 0.70:
                _mag = 2 if _inv_buyer >= 0.80 else 1
                score += _mag
                indep_dirs.append(("RUN", +1))
                reasons.append(
                    f"RUN  Buyers invaded the close ({_inv_buyer:.0%})"
                    f" -> CALL (x{_mag})")

    # Recent 4-7 candle window + its mini support/resistance edges
    _win  = candles[-7:] if len(candles) >= 7 else candles[:]
    w_hi  = max(x["high"] for x in _win)
    w_lo  = min(x["low"]  for x in _win)
    w_rng = w_hi - w_lo

    # KEY LEVELS — prices tested 2+ times in recent history (stronger = more
    # touches). A candle reaction AT a key level is far more reliable.
    _klevels = _key_levels(candles)

    def _key_touches(price: float) -> int:
        """Touch-count of the strongest key level this price sits on (0=none)."""
        best = 0
        for lvl, touches in _klevels:
            if abs(price - lvl) <= lvl * 0.0006:
                best = max(best, touches)
        return best

    def _sr_bonus(price: float, is_support: bool) -> tuple[int, str]:
        """
        Graded support/resistance bonus for a reaction at `price`. Price-action
        principle: the more factors that line up at one price (CONFLUENCE), the
        stronger the level — and the more powerful the reaction there.
          +3  CONFLUENCE — a tested key level AND a round number at the same price
          +2  heavily-tested KEY level (3+ touches)  OR  a BIG round number
          +1  key level (2 touches) OR MID round number OR 4-7 window edge
          +0  nowhere significant
        Returns (bonus, label).
        """
        k = _key_touches(price)
        _, _, rnd = _round_level(price)        # BIG / MID / SMALL / NONE
        big_round = rnd == "BIG"
        any_round = rnd in ("BIG", "MID")

        # Confluence — strongest: a multiply-tested level sitting on a round number
        if k >= 2 and any_round:
            return 3, f"CONFLUENCE key x{k}+{rnd.lower()} round"
        if k >= 3 or big_round:
            return 2, (f"KEY x{k}" if k >= 3 else "BIG round")
        if k == 2 or rnd == "MID":
            return 1, ("key x2" if k == 2 else "round")
        if w_rng > 0:
            edge = w_lo if is_support else w_hi
            if abs(price - edge) <= w_rng * 0.12:
                return 1, "window"
        return 0, ""

    # R47 (the 4-7 candle window theory) was REMOVED after live data showed it
    # consistently below coin-flip (37.5% then 42% over 31 trades) — it was the
    # main drag on accuracy. The window high/low is still used as fallback S/R
    # context in _sr_bonus above; it just no longer votes on its own.

    # ── T7   Engulfing / Piercing — reaction at the key S/R ──────────────────
    # 50% Rule: reacting candle must cover ≥50% of the previous body, else the
    # pattern lacks energy and is discarded.
    prev_body = abs(prev["close"] - prev["open"])
    prev_bull = prev["close"] >= prev["open"]
    if prev_body > 0 and is_bull != prev_bull:
        coverage = body / prev_body
        if coverage >= 1.0:
            # Full Engulfing — graded S/R bonus.
            # DB analysis (856 predictions): Bull Engulfing = 55.8% accurate (keep).
            # Bear Engulfing = 43.2% accurate — anti-signal in OTC (trap candle):
            # big bear move AT a key level absorbs sellers → next candle bounces UP.
            # Fix: Bull Engulfing keeps base ×3 + bonus; Bear Engulfing is capped
            # at ×1 regardless of S/R level to reduce its negative influence.
            # Bear Engulfing's "OTC trap -> CALL" flip was REMOVED (2026-07-03):
            # aggregate T7 accuracy degraded to 47.8% (n=494, below coin-flip)
            # after this flip was added — it doesn't cast a vote either way now.
            # Bull Engulfing (validated 55.8%, n=856) is unaffected and kept.
            if is_bull:
                bonus, lbl = _sr_bonus(l, True)
                # S/R bonus capped at +1 (2026-07-04 bias audit): 3+bonus
                # reached x6, the single biggest vote in the system, and T7
                # only ever fires in the just-closed candle's direction —
                # letting it outvote everything fed the continuation bias
                # (live T7 accuracy 48.5%, n=526 — no edge to justify x6).
                mag  = 3 + min(bonus, 1)
                score += mag
                forced_score += mag
                reasons.append(
                    f"T7   Bull Engulfing ({coverage:.0%} body"
                    f"{', @' + lbl if lbl else ''}) -> CALL (x{mag})")
        elif coverage >= 0.50:
            # Piercing Line (bull) is kept — partial bull recovery is valid.
            # Dark Cloud Cover (bear) removed — OTC seller partial cover = exhaustion,
            # not continuation; consistent with Bear Engulfing flip above.
            if is_bull:
                score += 2
                forced_score += 2
                reasons.append(
                    f"T7   Piercing Line ({coverage:.0%} of prev body)"
                    f" -> CALL (x2)")

    # ── T2   Pin Bar (Hammer / Shooting Star) — #3 (reaction at key S/R) ─────
    if upper_wick / total_range > 0.55 and body / total_range < 0.25:
        # Shooting Star — bearish; strongest rejecting a resistance key level.
        bonus, lbl = _sr_bonus(h, False)
        mag = 2 + bonus
        score -= mag
        indep_dirs.append(("T2", -1))
        reasons.append(
            f"T2   Shooting Star (upper wick >55%{', @' + lbl if lbl else ''})"
            f" -> PUT (x{mag})")
    elif lower_wick / total_range > 0.55 and body / total_range < 0.25:
        # Hammer — bullish; strongest bouncing off a support key level.
        bonus, lbl = _sr_bonus(l, True)
        mag = 2 + bonus
        score += mag
        indep_dirs.append(("T2", +1))
        reasons.append(
            f"T2   Hammer (lower wick >55%{', @' + lbl if lbl else ''})"
            f" -> CALL (x{mag})")

    # ── SWEEP  Liquidity sweep / stop-hunt — pierce a key level then reclaim ─
    # Smart money pushes price BEYOND a key level (or round number) to trigger
    # the stop-losses resting there, then reverses. The wick pierces the level
    # but the candle CLOSES back inside, trapping the breakout traders → fuel
    # for the reversal. This is NOT a breakout (which closes beyond the level);
    # the pierce must be REJECTED (a wick), and the close must reclaim the level.
    _sweep_cands = list(_klevels)                       # (price, touches)
    for _ref in (l, h):
        _rl, _, _rs = _round_level(_ref)
        if _rs in ("BIG", "MID"):
            _sweep_cands.append((_rl, 3 if _rs == "BIG" else 2))
    _best_sweep = None                                  # (touches, dir, level)
    for _lvl, _tch in _sweep_cands:
        # Bullish sweep: low pierced below a support, closed back above, with a
        # rejection (lower wick) — stops below were grabbed → CALL.
        if l < _lvl <= c and (min(o, c) - l) > total_range * 0.30:
            if _best_sweep is None or _tch > _best_sweep[0]:
                _best_sweep = (_tch, +1, _lvl)
        # Bearish sweep: high pierced above a resistance, closed back below.
        elif c <= _lvl < h and (h - max(o, c)) > total_range * 0.30:
            if _best_sweep is None or _tch > _best_sweep[0]:
                _best_sweep = (_tch, -1, _lvl)
    if _best_sweep:
        _tch, _sdir, _lvl = _best_sweep
        # DB analysis: SWEEP = 46.1% accurate (anti-signal at ±3).
        # Root cause: OTC fake sweeps are common; many pierce-and-reclaim patterns
        # are random price noise, not genuine stop-hunts.
        # Fix: score reduced to ±1 regardless of touches. The pattern still provides
        # directional information but no longer drives the signal on its own.
        _mag = 1
        if _sdir > 0:
            score += _mag
            indep_dirs.append(("SWEEP", +1))
            reasons.append(
                f"SWEEP Liquidity grab below {_lvl:.5g} then reclaim -> CALL (x{_mag})")
        else:
            score -= _mag
            indep_dirs.append(("SWEEP", -1))
            reasons.append(
                f"SWEEP Liquidity grab above {_lvl:.5g} then reject -> PUT (x{_mag})")

    # ── MARB  Marubozu — strong body ─────────────────────────────────────────────
    # A marubozu has a dominant body (≥75% of range) with tiny wicks.
    # OTC asymmetry: Bull marubozu → buyers dominated → continuation (CALL valid).
    # Bear marubozu → sellers dominated → OTC exhaustion: sellers used all capital
    # → next candle reverses UP. Do NOT vote PUT for bear marubozus in OTC.
    if body / total_range >= 0.75 and total_range > 0:
        prev_range = prev["high"] - prev["low"]
        if prev_range > 0 and body >= prev_range * 0.80:
            if is_bull:
                # Weight cut 2 -> 1 (2026-07-03): live data showed 47.9%
                # (n=190, below coin-flip) — the continuation thesis is
                # weaker in practice than assumed, kept only as a light lean.
                score += 1
                forced_score += 1
                reasons.append(
                    f"MARB Bull marubozu (body {body/total_range:.0%} of range)"
                    f" -> CALL (x1)")
            # Bear marubozu: no PUT vote — treat as exhaustion neutral in OTC.
            # The TRAP signal will handle it if tick data confirms extreme seller dominance.

    # ── TRAP  Liquidity Trap — exhausted one-sided candle → reversal ────────────
    # In OTC markets, when a candle is BOTH large-bodied AND overwhelmingly one-
    # sided in tick pressure (≥78% same-direction ticks), it means ALL available
    # buyers/sellers participated in that single candle.  When they run out,
    # there is nobody left to push price further → the next candle REVERSES.
    #
    # This is the "trap" pattern: traders see a big red candle and pile in SHORT,
    # but sellers are already exhausted — so the next candle closes green.
    #
    # Required (tick-based only — no tick data = no TRAP signal):
    #   body/range >= 0.68           big directional body
    #   bpct <= 0.22 (bear) or       78%+ of ticks one-sided
    #   bpct >= 0.78 (bull)
    #
    # Bonus:
    #   final_ticks show OPPOSITE side (exhaustion confirmed)  → +1
    #   body > 1.6× avg of last 10 candles (abnormally large)  → +1
    if _cur_bpct is not None and body / total_range >= 0.68:
        _bratio   = body / total_range
        _avg_body = (sum(abs(cc["close"] - cc["open"])
                        for cc in candles[-10:]) / min(10, len(candles))) or 1e-9
        _size_big  = body >= _avg_body * 1.6

        _trap_bear = (not is_bull) and _cur_bpct <= 0.22   # big red, seller-dominated
        _trap_bull = is_bull       and _cur_bpct >= 0.78   # big green, buyer-dominated

        if _trap_bear or _trap_bull:
            _ts = 2
            _fin_exhaust = (
                (_trap_bear and _cur_fin_bpct is not None and _cur_fin_bpct >= 0.65)
                or
                (_trap_bull and _cur_fin_bpct is not None and _cur_fin_bpct <= 0.35)
            )
            if _fin_exhaust:
                _ts += 1   # final ticks already reversed → strongest confirmation
            if _size_big:
                _ts += 1   # abnormally large candle = deeper exhaustion

            _size_note = f", {body / _avg_body:.1f}x avg body" if _size_big else ""
            if _trap_bear:
                _fin_note  = f", final buyers {_cur_fin_bpct:.0%}" if _fin_exhaust else ""
                score  += _ts
                indep_dirs.append(("TRAP", +1))
                reasons.append(
                    f"TRAP Bear candle (body={_bratio:.0%}"
                    f", sellers={100-round(_cur_bpct*100)}%"
                    f"{_fin_note}{_size_note}"
                    f") sellers exhausted -> CALL (x{_ts})")
            else:
                _fin_note  = f", final sellers {1-_cur_fin_bpct:.0%}" if _fin_exhaust else ""
                score  -= _ts
                indep_dirs.append(("TRAP", -1))
                reasons.append(
                    f"TRAP Bull candle (body={_bratio:.0%}"
                    f", buyers={round(_cur_bpct*100)}%"
                    f"{_fin_note}{_size_note}"
                    f") buyers exhausted -> PUT (x{_ts})")

    # ── STAR  Morning Star / Evening Star — 3-candle reversal ────────────────
    # Pattern (oldest→newest): [big directional] → [small/doji = indecision]
    #                          → [current candle closes back into candle-1]
    # This is one of the statistically most reliable candlestick reversal signals.
    # The middle small candle = the market "pausing and questioning the move".
    if len(candles) >= 3:
        c1 = candles[-3]
        c2 = candles[-2]
        c1_body  = abs(c1["close"] - c1["open"])
        c2_body  = abs(c2["close"] - c2["open"])
        c1_range = c1["high"] - c1["low"]
        c2_range = c2["high"] - c2["low"]
        if c1_range > 0 and c2_range > 0:
            c1_bull   = c1["close"] >= c1["open"]
            c1_strong = c1_body / c1_range >= 0.50    # decisive first candle
            c2_small  = c2_body / c2_range <= 0.35    # indecision / doji middle
            c1_mid    = (c1["open"] + c1["close"]) / 2

            # Morning Star: big bear → small body → current closes above c1 midpoint
            if (not c1_bull) and c1_strong and c2_small and is_bull and c >= c1_mid:
                bonus, lbl = _sr_bonus(l, True)
                mag = 2 + (1 if bonus >= 1 else 0)
                score += mag
                forced_score += mag
                reasons.append(
                    f"STAR Morning Star (3-candle rev"
                    f"{', @' + lbl if lbl else ''}) -> CALL (x{mag})")

            # Evening Star: big bull → small body → current closes below c1 midpoint
            elif c1_bull and c1_strong and c2_small and (not is_bull) and c <= c1_mid:
                bonus, lbl = _sr_bonus(h, False)
                mag = 2 + (1 if bonus >= 1 else 0)
                score -= mag
                forced_score -= mag
                reasons.append(
                    f"STAR Evening Star (3-candle rev"
                    f"{', @' + lbl if lbl else ''}) -> PUT (x{mag})")

    # ── STREAK  Consecutive streak exhaustion ─────────────────────────────────
    # Count consecutive same-color candles up to and including the just-closed
    # candle. After 4+ candles of the same colour, momentum is usually exhausted
    # and mean-reversion pressure builds. Strongest when the streak end price
    # sits at a key level or round number (rejected after the run = stop-hunt fuel).
    _streak = 1
    for _cn in reversed(candles[:-1]):      # walk back from second-to-last
        if (_cn["close"] >= _cn["open"]) == is_bull:
            _streak += 1
        else:
            break
        if _streak >= 7:
            break

    if _streak >= 4:
        _smag = min(_streak - 2, 3)         # 4→2, 5→3, 6+→3
        _sbns, _slbl = _sr_bonus(h if is_bull else l, not is_bull)
        if _sbns >= 1:
            _smag = min(_smag + 1, 3)       # extra +1 if streak ended at S/R
        if is_bull:
            score -= _smag
            indep_dirs.append(("STREAK", -1))
            reasons.append(
                f"STREAK {_streak} bull candles exhausted"
                f"{', @' + _slbl if _slbl else ''} -> PUT (x{_smag})")
        else:
            score += _smag
            indep_dirs.append(("STREAK", +1))
            reasons.append(
                f"STREAK {_streak} bear candles exhausted"
                f"{', @' + _slbl if _slbl else ''} -> CALL (x{_smag})")

    # HARAMI (small opposite candle inside prior body) was REMOVED (2026-07-03)
    # after live data showed it below coin-flip (44.4%, n=45) — the "textbook"
    # 2-candle reversal doesn't hold up in OTC. Same fate as R47 above.

    # THREE (Three White Soldiers) was REMOVED (2026-07-03) after live data
    # showed it far below coin-flip (20%, n=5) — too rare and unreliable to
    # keep even at its already-reduced x1 weight.

    # ── OUTSIDE  Three Outside Up (bull only) ────────────────────────────────────
    # OTC revision: Three Outside Down (bear confirmation of a bear engulf) is
    # REMOVED — 3 bear candles in OTC = deep seller exhaustion, not continuation.
    # Three Outside Up (bull confirmation) is kept because buyer momentum is valid.
    if len(candles) >= 3:
        _e1 = candles[-3]
        _e2 = candles[-2]   # = prev
        _e1b = abs(_e1["close"] - _e1["open"])
        _e2b = abs(_e2["close"] - _e2["open"])
        _e1_bull = _e1["close"] >= _e1["open"]
        _e2_bull = _e2["close"] >= _e2["open"]
        _e2_engulfed = (
            _e1b > 0
            and _e2_bull != _e1_bull
            and _e2b / _e1b >= 1.0
        )
        # Only fire for bull confirmation (Three Outside Up)
        if _e2_engulfed and is_bull and _e2_bull:
            bonus, lbl = _sr_bonus(l, True)
            mag = 2 + (1 if bonus >= 1 else 0)
            score += mag
            forced_score += mag
            reasons.append(
                f"OUTSIDE Three Outside Up (engulf + bull confirm"
                f"{', @' + lbl if lbl else ''}) -> CALL (x{mag})")

    # ── SPIN  Spinning Top / Doji — indecision at key level → reversal ──────────
    # A spinning top has a small body (≤30% of range) with significant wicks on
    # BOTH sides, signalling neither buyers nor sellers could dominate. A doji
    # is the extreme case (body ≤8%). By itself it's weak noise — but AT a key
    # level or after a directional streak, it marks the exact moment the trend
    # ran into a wall, and next candle often reverses.
    _is_doji   = body / total_range <= 0.08
    _is_spin   = (
        body / total_range <= 0.30
        and upper_wick / total_range >= 0.28
        and lower_wick / total_range >= 0.28
    )
    if _is_doji or _is_spin:
        _tag = "Doji" if _is_doji else "Spinning Top"
        _h_bon, _h_lbl = _sr_bonus(h, False)
        _l_bon, _l_lbl = _sr_bonus(l, True)
        _prior_bull = prev["close"] >= prev["open"]

        if _h_bon >= 1:
            _smag = 1 + (1 if _h_bon >= 2 else 0)
            score -= _smag
            indep_dirs.append(("SPIN", -1))
            reasons.append(
                f"SPIN {_tag} at resistance @{_h_lbl}"
                f" -> PUT (x{_smag})")
        elif _l_bon >= 1:
            _smag = 1 + (1 if _l_bon >= 2 else 0)
            score += _smag
            indep_dirs.append(("SPIN", +1))
            reasons.append(
                f"SPIN {_tag} at support @{_l_lbl}"
                f" -> CALL (x{_smag})")
        elif _streak >= 3 and _is_doji:
            # Doji after 3+ same-colour candles without S/R — exhaustion context
            if _prior_bull:
                score -= 1
                indep_dirs.append(("SPIN", -1))
                reasons.append(
                    f"SPIN Doji after {_streak}-candle bull streak"
                    f" -> PUT")
            else:
                score += 1
                indep_dirs.append(("SPIN", +1))
                reasons.append(
                    f"SPIN Doji after {_streak}-candle bear streak"
                    f" -> CALL")

    # GAP (candle-to-candle gap reaction) was REMOVED (2026-07-03) after live
    # data showed it the worst theory in the ensemble (31.2%, n=32) — the
    # wick-fill/continuation logic did not hold up despite the user-confirmed
    # OTC behaviour it was modeled on.

    # ── WICKWALL  Repeated wick rejection zone ────────────────────────────────
    # Problem: _key_levels() needs a SWING PIVOT (a low lower than BOTH
    # neighbours). Consecutive ranging candles that all put lower wicks at the
    # same price zone (215.80, 215.80, 215.79, 215.80 ...) are NOT pivots — none
    # is lower than its neighbors. So that "invisible wall" is missed entirely.
    #
    # WICKWALL fixes this: it clusters ALL lower/upper wick tips from the last 12
    # candles.  3+ tips within ±0.08% = a defended zone. The more touches, the
    # stronger the wall. When the current candle tests that zone and holds/rejects,
    # the next candle is very likely to bounce/continue from it.
    #
    # Key difference from existing signals:
    #   _key_levels / T2 / T7  — need formal swing pivots
    #   MICRO (e/f)             — use tick-level data from DB (not OHLC wicks)
    #   WICKWALL                — pure OHLC wick clustering, last 12 candles only
    #
    # DE-GATED from candle color (2026-07-04 bias audit): every branch used
    # to require is_bull for CALL / not is_bull for PUT, which made 99.9% of
    # WICKWALL's live votes point the same way as the just-closed candle —
    # a disguised "repeat last color" vote, part of the measured 87%
    # continuation bias. The wall test itself is what matters: touched the
    # wall and CLOSED on the defended side = the wall held, whatever color
    # the candle body was (a bear candle dipping to support and closing back
    # above it is the classic rejection read the old gate threw away).
    # Clusters are built from the candles BEFORE the current one — its own
    # wick must not count as a prior "touch" of the wall it is testing.
    # The old CALL x1-x3 vs PUT x1 asymmetry (measured under the color gate,
    # so unusable now) is replaced by symmetric x1-x2 both sides.
    _sup_walls, _res_walls, _ww_atr = _wick_wall(candles[:-1])
    # ATR-based touch tolerance: current candle's l/h must be within half an
    # average candle range of the wall to count as "testing" it.
    _ww_tol   = _ww_atr * 0.50 if _ww_atr > 0 else total_range * 0.50

    def _ww_mag(n: int) -> int:
        return 2 if n >= 5 else 1

    # A wall vote needs BOTH a touch AND a real rejection: the low must reach
    # the wall zone and the close must clear it by a full tolerance. A bare
    # "closed on the right side of the wall" (first de-gate attempt) fired on
    # nearly every ranging candle — replay showed it enabling 59% of the
    # parrot signals the guard below was supposed to stop.
    for _wp, _wn in sorted(_sup_walls, key=lambda x: -x[1])[:2]:
        if l <= _wp + _ww_tol and c >= _wp + _ww_tol:
            _mag = _ww_mag(_wn)
            score  += _mag
            indep_dirs.append(("WICKWALL", +1))
            reasons.append(
                f"WICKWALL {_wn}x lower wicks @{_wp:.5g}"
                f" low tested + close rejected away -> CALL (x{_mag})")
            break

    # No support-first short-circuit: if the candle tested BOTH walls (wide
    # range bar in a tight box), both votes fire and net out.
    for _wp, _wn in sorted(_res_walls, key=lambda x: -x[1])[:2]:
        if h >= _wp - _ww_tol and c <= _wp - _ww_tol:
            _mag = _ww_mag(_wn)
            score  -= _mag
            indep_dirs.append(("WICKWALL", -1))
            reasons.append(
                f"WICKWALL {_wn}x upper wicks @{_wp:.5g}"
                f" high tested + close rejected away -> PUT (x{_mag})")
            break

    # ── REGIME  Market regime (trend + zone) context ──────────────────────────
    # Classifies the last 20 candles as UPTREND/DOWNTREND/SIDEWAYS and detects
    # whether the current price is in a SUPPORT, RESISTANCE or NEUTRAL zone.
    #
    # FLIPPED (2026-07-03): a 1564-row audit showed WITH_REGIME (final signal
    # matches this theory's own original direction) at 44.3% (n=212) vs
    # COUNTER_REGIME at 54.9% (n=184) — a statistically significant gap
    # (z≈2.1) in the OPPOSITE direction from the original assumption below.
    # Same anti-signal pattern already handled for RUN's "Sellers WON" and
    # T7's "Bear Engulfing" — the raw trend/zone read is real, but OTC
    # continuation logic on top of it was backwards, so the vote is now
    # inverted rather than removed outright.
    _regime, _zone = _market_regime(candles)
    if _regime == "UPTREND":
        if _zone == "SUPPORT":
            score -= 2
            reasons.append("REGIME UPTREND + SUPPORT zone -> PUT (x2)")
        elif _zone == "RESISTANCE":
            score += 1
            reasons.append("REGIME UPTREND + RESISTANCE zone -> CALL (x1)")
        elif is_bull:
            score -= 1
            reasons.append("REGIME UPTREND + bull candle -> PUT (x1)")
    elif _regime == "DOWNTREND":
        if _zone == "RESISTANCE":
            score += 2
            reasons.append("REGIME DOWNTREND + RESISTANCE zone -> CALL (x2)")
        elif _zone == "SUPPORT":
            score -= 1
            reasons.append("REGIME DOWNTREND + SUPPORT zone -> PUT (x1)")
        elif not is_bull:
            score += 1
            reasons.append("REGIME DOWNTREND + bear candle -> CALL (x1)")
    else:  # SIDEWAYS
        if _zone == "SUPPORT":
            score -= 2
            reasons.append("REGIME SIDEWAYS + SUPPORT zone -> PUT (x2)")
        elif _zone == "RESISTANCE":
            score += 2
            reasons.append("REGIME SIDEWAYS + RESISTANCE zone -> CALL (x2)")

    # ── ZIGZAG  Alternating candle pattern detection ───────────────────────────
    # OTC RNG frequently produces alternating green/red candles because the
    # random walk oscillates. When 4+ consecutive candles alternate direction,
    # predicting OPPOSITE of the last candle has meaningful edge.
    # 6+ alternating: stronger signal (the oscillation is deeply established).
    # CONTEXT GATE: alternation is a RANGING-market phenomenon — betting on a
    # reversal every candle inside a trend fights the trend, so ZIGZAG only
    # votes when the regime is SIDEWAYS. (Live data: ZIGZAG 25% overall.)
    _zz_predict, _zz_len = _zigzag_signal(candles)
    if _zz_predict != 0 and _regime == "SIDEWAYS":
        _zz_mag = 2 if _zz_len >= 6 else 1
        if _zz_predict > 0:
            score += _zz_mag
            indep_dirs.append(("ZIGZAG", +1))
            reasons.append(
                f"ZIGZAG {_zz_len}-candle alternating pattern"
                f" -> CALL (x{_zz_mag})")
        else:
            score -= _zz_mag
            indep_dirs.append(("ZIGZAG", -1))
            reasons.append(
                f"ZIGZAG {_zz_len}-candle alternating pattern"
                f" -> PUT (x{_zz_mag})")

    # ── MICRO  Multi-candle microstructure (DB tick history of prior candles) ─
    #
    # These fields come from the tick-level data that was alive INSIDE each
    # previous candle — information that OHLC alone cannot show:
    #
    #   (a) PRESSURE CHAIN   — sustained buyer/seller tick pressure across
    #                          multiple candles is a genuine trend signal
    #                          invisible in closes alone.
    #   (b) EXHAUSTION CHAIN — if 2+ consecutive candles ended their final
    #                          ticks in exhaustion (opposite side invaded),
    #                          the trend is genuinely running out of fuel.
    #   (c) FIGHT BREAKOUT   — two ranging (midpoint-crossing) candles
    #                          followed by a decisive close = coiled spring.
    #   (d) LATE-PHASE INVERSION — the final third of ticks in the previous
    #                          candle moved AGAINST its close direction: smart
    #                          money defending the reversal, absorbed by the
    #                          visible candle colour. Next candle likely flips.
    #   (e) HOLD LEVEL S/R   — the "hold_price" (most-visited price zone
    #                          inside a candle's ticks) acts as micro S/R. A
    #                          bounce or rejection there adds confluence.
    #   (f) PERSISTENT S/R   — a key level appearing in 3+ of the last 5
    #                          candle snapshots is an extra-strong zone; a
    #                          reaction there gets a +1 bonus signal.
    #
    # micro_history is ordered oldest→newest; [-1] = candle just before current.
    # Sub-blocks (a)-(f) below are independent `if`s, so several can fire on
    # one candle — live data showed MICRO stacking up to ~±10 raw score
    # (aggregate weight ~4x the next-biggest theory) while counting as ONE
    # theory in `agree`. Its color-gated sub-votes (chains/recovery/fight/
    # hold/persistent) are bounded by the COLOR-GATED CAP after this block.
    if micro_history and len(micro_history) >= 2:
        prev_m = micro_history[-1]
        hist3  = micro_history[-3:]  # up to 3 most recent prior candles

        # Freshness gate: signals that read prev_m as "the candle immediately
        # before this one" (recovery, late-phase inversion) are only valid when
        # it actually IS that candle. After a restart or asset switch the DB
        # row can be much older — those signals must stay silent then.
        _per = period or (cur.get("time", 0) - prev.get("time", 0))
        _prev_fresh = bool(_per) and (
            prev_m.get("time") == cur.get("time", 0) - _per)

        # ── (a) Pressure Chain ─────────────────────────────────────────────
        pcts = [m["buy_pct"] for m in hist3 if m.get("buy_pct") is not None]
        if len(pcts) >= 2:
            trend  = pcts[-1] - pcts[0]
            avg    = sum(pcts) / len(pcts)

            # Buyer chain: 3 candles all buyer-dominated + bull candle = continuation.
            # Seller chain: mirrors RUN "Sellers WON" issue — in OTC, sustained seller
            # pressure often exhausts sellers. Seller chain score halved to ±1.
            if len(pcts) >= 3 and all(p >= 62 for p in pcts) and is_bull:
                score += 2
                forced_score += 2
                reasons.append(
                    f"MICRO 3-candle buyer chain"
                    f" ({'/'.join(str(p) for p in pcts)}% up-ticks)"
                    f" -> CALL (x2)")
            elif len(pcts) >= 3 and all(p <= 38 for p in pcts) and not is_bull:
                score -= 1
                forced_score -= 1
                reasons.append(
                    f"MICRO 3-candle seller chain"
                    f" ({'/'.join(str(p) for p in pcts)}% up-ticks)"
                    f" -> PUT (x1, OTC-reduced)")
            elif trend >= 20 and pcts[-1] >= 58 and is_bull:
                score += 1
                forced_score += 1
                reasons.append(
                    f"MICRO Buyer pressure rising"
                    f" ({pcts[0]}%->{pcts[-1]}%) -> CALL")
            elif trend <= -20 and pcts[-1] <= 42 and not is_bull:
                score -= 1
                forced_score -= 1
                reasons.append(
                    f"MICRO Seller pressure rising"
                    f" ({pcts[0]}%->{pcts[-1]}%) -> PUT")
            # Cross-candle absorption: sustained pressure but candle closed opposite
            elif avg >= 62 and not is_bull:
                score += 1
                indep_dirs.append(("MICRO", +1))
                reasons.append(
                    f"MICRO Buyer pressure ({avg:.0f}% avg) + bear close"
                    f" = cross-candle absorption -> CALL")
            elif avg <= 38 and is_bull:
                score -= 1
                indep_dirs.append(("MICRO", -1))
                reasons.append(
                    f"MICRO Seller pressure ({avg:.0f}% avg) + bull close"
                    f" = cross-candle absorption -> PUT")

        # ── (b) Exhaustion Chain ───────────────────────────────────────────
        # DB: 2x exhaustion in last 3 candles = 6R/0W = 100% accuracy.
        # Score raised from ±2 to ±3 to reflect its actual predictive power.
        ex_n = sum(1 for m in hist3 if m.get("last_react") == "EXHAUST")
        if ex_n >= 2:
            if is_bull:
                score -= 3
                indep_dirs.append(("MICRO", -1))
                reasons.append(
                    f"MICRO {ex_n}x exhaustion in last"
                    f" {len(hist3)} candles -> reversal -> PUT (x3)")
            else:
                score += 3
                indep_dirs.append(("MICRO", +1))
                reasons.append(
                    f"MICRO {ex_n}x exhaustion in last"
                    f" {len(hist3)} candles -> reversal -> CALL (x3)")

        # ── (b2) Recovery Continuation ────────────────────────────────────
        # A recovery in the previous candle means the losing side re-engaged
        # at the close — they often carry momentum into the next candle.
        if _prev_fresh and prev_m.get("last_react") == "RECOVERY":
            p_o    = prev_m.get("open")  or 0
            p_c    = prev_m.get("close") or 0
            p_bull = p_c >= p_o
            if p_bull and is_bull:
                score += 1
                forced_score += 1
                reasons.append(
                    "MICRO Prev candle recovery (bull) confirms continuation"
                    " -> CALL")
            elif not p_bull and not is_bull:
                score -= 1
                forced_score -= 1
                reasons.append(
                    "MICRO Prev candle recovery (bear) confirms continuation"
                    " -> PUT")

        # ── (c) Fight-zone Breakout ────────────────────────────────────────
        # DB: bull breakout = 80% (4R/1W); bear breakout = 50% (6R/6W).
        # Bull breakout is reliable; bear breakout is coin-flip. Differentiate.
        fight_n = sum(1 for m in micro_history[-2:] if m.get("is_fight"))
        if fight_n >= 2 and body / total_range >= 0.50:
            if is_bull:
                score += 2
                forced_score += 2
                reasons.append(
                    "MICRO 2 fight candles + bull breakout -> CALL (x2)")
            else:
                # Bear breakout = 50% in DB: only +1 to avoid dominating signal
                score -= 1
                forced_score -= 1
                reasons.append(
                    "MICRO 2 fight candles + bear breakout -> PUT (x1)")

        # ── (d) Phase Inversion (Early/Mid/Late) ────────────────────────────
        # A candle's own close direction can hide an internal fight: if one
        # third of its ticks pushed the OPPOSITE way to how it closed, that
        # invading side often carries into the NEXT candle. Originally only
        # the LATE third was checked (weighted x3, based on a small early
        # sample claiming 90%+ accuracy). A fresh 1372-row audit
        # (2026-07-03) measured all three thirds independently: Early 51.1%
        # (n=174), Mid 51.9% (n=183), Late 54.3% (n=186) — real (all three
        # point the same direction) but far more modest than the original
        # claim, so all three now vote at the same x1 weight instead of
        # Late's old x3.
        phases = prev_m.get("phases") or []
        if _prev_fresh and len(phases) >= 3:
            p_o    = prev_m.get("open")  or 0
            p_c    = prev_m.get("close") or 0
            p_bull = p_c >= p_o
            for _pidx, _pname in ((0, "early"), (1, "mid"), (2, "late")):
                _ph = phases[_pidx]
                # Bear candle but buyers already invaded this third → reversal
                if _ph == "UP" and not p_bull:
                    score += 1
                    indep_dirs.append(("MICRO", +1))
                    reasons.append(
                        f"MICRO Prev candle: bear close but buyers invaded"
                        f" {_pname}-phase -> CALL (x1)")
                # Bull candle but sellers already invaded this third → reversal
                elif _ph == "DOWN" and p_bull:
                    score -= 1
                    indep_dirs.append(("MICRO", -1))
                    reasons.append(
                        f"MICRO Prev candle: bull close but sellers invaded"
                        f" {_pname}-phase -> PUT (x1)")

        # ── (e) Congestion Hold Level as S/R ──────────────────────────────
        # hold_price = most-visited price zone inside a prior candle's ticks.
        # A bounce or rejection at that zone is meaningful micro S/R.
        for _m in reversed(micro_history[-4:]):
            hp = _m.get("hold_price")
            if not hp or hp <= 0:
                continue
            tol = hp * 0.0010          # ±0.10% tolerance
            if abs(l - hp) <= tol and is_bull:
                _hbonus = 1 if _key_touches(hp) >= 2 else 0
                _hmag   = 1 + _hbonus
                score  += _hmag
                forced_score += _hmag
                reasons.append(
                    f"MICRO Low bounced off congestion hold @{hp:.5g}"
                    f"{' (key lvl)' if _hbonus else ''}"
                    f" -> CALL (x{_hmag})")
                break
            if abs(h - hp) <= tol and not is_bull:
                _hbonus = 1 if _key_touches(hp) >= 2 else 0
                _hmag   = 1 + _hbonus
                score  -= _hmag
                forced_score -= _hmag
                reasons.append(
                    f"MICRO High rejected at congestion hold @{hp:.5g}"
                    f"{' (key lvl)' if _hbonus else ''}"
                    f" -> PUT (x{_hmag})")
                break

        # ── (f) Persistent Key Level — price zone appearing in 3+ of last 5 snapshots ──
        # A level that keeps reappearing as the "most congested zone" across multiple
        # candle snapshots is an extra-strong S/R (the market keeps returning there).
        # If the current candle's low or high reacts at one of these persistent zones,
        # boost the signal by ±1 (on top of whatever (e) already added).
        if len(micro_history) >= 3:
            # Bucket into 0.05%-of-price zones so near-identical levels merge.
            # (The old code computed a bucket but keyed the dict on the raw
            # float — exact-equality only — so persistence almost never fired.)
            _step = (c * 0.0005) or 1e-9
            _pzones: dict[int, list[float]] = {}
            for _mh in micro_history[-5:]:
                for _kl_pair in (_mh.get("key_levels") or []):
                    try:
                        _pl, _pt = float(_kl_pair[0]), int(_kl_pair[1])
                    except (TypeError, IndexError, ValueError):
                        continue
                    if _pt < 2:
                        continue
                    _pzones.setdefault(round(_pl / _step), []).append(_pl)
            _persistent = [(sum(v) / len(v), len(v))
                           for v in _pzones.values() if len(v) >= 3]
            for _pp, _pn in _persistent:
                _tol = _pp * 0.0012   # ±0.12% — slightly wider than hold-level check
                if abs(l - _pp) <= _tol and is_bull:
                    score += 1
                    forced_score += 1
                    reasons.append(
                        f"MICRO Persistent S/R @{_pp:.5g} (seen {_pn}x)"
                        f" low bounced -> CALL")
                    break
                if abs(h - _pp) <= _tol and not is_bull:
                    score -= 1
                    forced_score -= 1
                    reasons.append(
                        f"MICRO Persistent S/R @{_pp:.5g} (seen {_pn}x)"
                        f" high rejected -> PUT")
                    break

    # ── MTF  Multi-timeframe confluence (user request 2026-07-06) ────────────
    # Reads two extra timeframes derived from data already in hand — no new
    # broker streams (which would multiply account load ~3x):
    #   HIGHER: current period × 5 (on the 1m chart -> 5m), aggregated from
    #           the candle history with a rolling 5-bar window; its 20-bar
    #           regime trend votes ±1.
    #   LOWER : the just-closed candle's second-half tick drift (on the 1m
    #           chart -> the final ~30s); votes ±1 when it moved decisively
    #           (>= 25% of the candle's range). Second half only — a
    #           "both halves agree" read would mechanically imply the
    #           candle's own color and just feed the parrot bias.
    # Graded like any theory (code MTF), so the live mute gate silences it
    # automatically if it proves below coin-flip.
    if len(candles) >= 25:
        _htf: list[dict] = []
        _hi = len(candles)
        while _hi - 5 >= 0 and len(_htf) < 24:
            _grp = candles[_hi - 5:_hi]
            _htf.append({
                "time":  _grp[0]["time"], "open": _grp[0]["open"],
                "high":  max(g["high"] for g in _grp),
                "low":   min(g["low"] for g in _grp),
                "close": _grp[-1]["close"],
            })
            _hi -= 5
        _htf.reverse()
        _htf_trend, _ = _market_regime(_htf)
        if _htf_trend in ("UPTREND", "DOWNTREND"):
            _p5 = (period or 60) * 5
            _mtf_lbl = f"{_p5 // 60}m" if _p5 >= 60 else f"{_p5}s"
            if _htf_trend == "UPTREND":
                score += 1
                indep_dirs.append(("MTF", +1))
                reasons.append(f"MTF  {_mtf_lbl} trend UPTREND -> CALL (x1)")
            else:
                score -= 1
                indep_dirs.append(("MTF", -1))
                reasons.append(f"MTF  {_mtf_lbl} trend DOWNTREND -> PUT (x1)")

    if ticks and len(ticks) >= 20 and total_range > 0:
        _half2 = ticks[-1] - ticks[len(ticks) // 2]
        if abs(_half2) >= total_range * 0.25:
            _lo_lbl = f"{max((period or 60) // 2, 1)}s"
            if _half2 > 0:
                score += 1
                indep_dirs.append(("MTF", +1))
                reasons.append(
                    f"MTF  last {_lo_lbl} momentum up -> CALL (x1)")
            else:
                score -= 1
                indep_dirs.append(("MTF", -1))
                reasons.append(
                    f"MTF  last {_lo_lbl} momentum down -> PUT (x1)")

    # ── THEORY MUTE GATE  — live per-theory accuracy feedback loop ───────────
    # Theories whose recent live accuracy is proven bad (db.theory_perf via
    # feed.py's cached snapshot, hysteresis there) get their votes excluded
    # from the score AFTER the fact: the vote lines stay in reasons (marked
    # "[MUTED ...]") so they remain visible and shadow-gradeable, letting a
    # muted theory earn its way back in.
    if muted:
        # T7/MARB/OUTSIDE/STAR votes are 100% color-forced, so un-counting
        # them must also come out of forced_score. RUN/MICRO mix forced and
        # independent sub-votes; their forced share is left in forced_score
        # (slight over-capping toward NEUTRAL — the safe direction — and
        # both hover ~50%, far from the mute threshold, so this path is
        # unlikely to matter in practice).
        _FORCED_ONLY = {"T7", "MARB", "OUTSIDE", "STAR"}
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
        indep_dirs = [(_t, _d) for _t, _d in indep_dirs if _t not in muted]

    # ── COLOR-GATED CAP  — the direct fix for the continuation bias ──────────
    # Every vote whose direction is mechanically forced by the just-closed
    # candle's color was accumulated into forced_score alongside the normal
    # score. Cap that stack at ±FORCED_CAP: however many color-gated theories
    # pile onto one candle (live data measured them outweighing everything
    # else 3.8:1), "the candle was green/red" is worth at most 2 points —
    # color-INDEPENDENT evidence decides the rest of the signal.
    # History: first shipped at 1 (strictest de-biasing, when the parrot
    # guard still withheld signals as NEUTRAL); relaxed back to 2 on
    # 2026-07-06 after the every-candle switch left continuation signals
    # almost never able to rank above WEAK — the parrot guard now handles
    # the pure color-echo case by strength-capping it, so the cap only
    # needs to stop pile-on domination, not starve continuation entirely.
    # The "(coordination" marker is skipped by _parse_votes, so per-theory
    # grading and `agree` still see the individual votes unchanged.
    FORCED_CAP = 2
    if abs(forced_score) > FORCED_CAP:
        _fcap    = FORCED_CAP if forced_score > 0 else -FORCED_CAP
        _fexcess = forced_score - _fcap
        score   -= _fexcess
        reasons.append(
            f"COLOR-GATED votes net {forced_score:+d} -> capped {_fcap:+d}"
            f" (coordination cap)")

    # ── ATTENUATION  Regime context dampening TREND-FOLLOWING signals ─────────
    # FLIPPED (2026-07-03) alongside REGIME's own flip above: the 1564-row
    # audit showed the final signal doing WORSE when it follows the raw trend
    # (WITH_REGIME 44.3%, n=212) than when it fights it (COUNTER_REGIME
    # 54.9%, n=184) — and replaying history with REGIME's vote already
    # flipped showed the SAME gap persisted (44.6% vs 53.8%), proving the
    # bias wasn't specific to REGIME's own vote but structural (this block
    # was actively protecting trend-following and suppressing counter-trend,
    # exactly backwards). Now it dampens/caps the TREND-FOLLOWING side
    # instead, so counter-trend reads aren't drowned out by it.
    #
    # Two cases:
    #   (a) NEUTRAL zone -> a trend-following vote with no S/R support -> -2
    #   (b) SUPPORT/RESISTANCE zone -> if a STRONG trend-following signal
    #       forms right at a zone that would normally favor a bounce, cap it
    #       to MEDIUM so it doesn't drown out the counter-trend read.
    if _regime == "DOWNTREND":
        if _zone == "NEUTRAL" and score < 0:
            _att = min(2, abs(score))
            score += _att
            reasons.append(
                f"REGIME DOWNTREND+NEUTRAL dampens PUT -> +{_att} (attenuation)")
        elif _zone == "SUPPORT" and score < -8:
            # Cap |score| to 8 — just below the OVERHEATED threshold (9),
            # recalibrated 2026-07-04 with the shrunken score distribution.
            _att = abs(score) - 8
            if _att > 0:
                score += _att
                reasons.append(
                    f"REGIME DOWNTREND+SUPPORT PUT capped to MEDIUM"
                    f" -> +{_att} (attenuation)")
    elif _regime == "UPTREND":
        if _zone == "NEUTRAL" and score > 0:
            _att = min(2, score)
            score -= _att
            reasons.append(
                f"REGIME UPTREND+NEUTRAL dampens CALL -> -{_att} (attenuation)")
        elif _zone == "RESISTANCE" and score > 8:
            # Cap score to 8 — just below the OVERHEATED threshold (9),
            # recalibrated 2026-07-04 with the shrunken score distribution.
            _att = score - 8
            if _att > 0:
                score -= _att
                reasons.append(
                    f"REGIME UPTREND+RESISTANCE CALL capped to MEDIUM"
                    f" -> -{_att} (attenuation)")

    # ── Final ─────────────────────────────────────────────────────────────────
    # EVERY-CANDLE MODE (2026-07-06, user decision): a direction is emitted
    # on every candle — quality lives in the STRENGTH label instead of in
    # NEUTRAL abstention. The 2026-07-04 bias findings still apply exactly
    # as measured; they now demote strength to WEAK rather than withholding
    # the signal, so "trade only STRONG/MEDIUM" preserves the honest subset
    # while WEAK carries the forced picks.
    _weak_cap_reasons: list[str] = []
    _indep_net = sum(_d for _t, _d in indep_dirs)
    signal = "CALL" if score > 0 else "PUT" if score < 0 else "NEUTRAL"

    if signal == "NEUTRAL":
        # score == 0 — no net evidence. Tiebreak chain, weakest-first
        # honesty: independent net -> regime trend -> last candle color.
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
        # Old dead band — |score| of 1 is one noise-level vote. Direction
        # stands (every-candle mode) but it can never rank above WEAK.
        _weak_cap_reasons.append(
            f"NO EDGE: |score|={abs(score)} is noise-level -> WEAK")

    # UNSTABLE BASE (2026-07-06, USER observation, then verified on 3,475
    # graded live signals): predictions made off a doji / spinning-top /
    # marubozu base candle measured 47.3-48.3% vs 51.6% off normal candles
    # (combined z≈2.4 — the first candle-shape effect to show real
    # statistical support in this project). Direction stands (every-candle
    # mode) but strength is capped to WEAK after these shapes.
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

    # PARROT GUARD (2026-07-04 bias audit): 87% of live signals simply
    # repeated the just-closed candle's color, because most theories are
    # mechanically color-gated (they CAN'T vote the other way). A signal
    # that points with the candle carries no information beyond "the last
    # candle was green/red" unless color-INDEPENDENT votes net-agree —
    # such parrot signals are capped to WEAK (was: demoted to NEUTRAL,
    # before every-candle mode). Signals AGAINST the candle's color are
    # untouched (they already beat the stacked continuation weight).
    _sig_dir = 1 if signal == "CALL" else -1
    _closed_dir = 1 if is_bull else -1
    if _sig_dir == _closed_dir and _indep_net * _sig_dir <= 0:
        _weak_cap_reasons.append(
            "PARROT GUARD: signal only repeats the closed candle's color"
            " (color-independent theories don't net-agree) -> WEAK")

    reasons.extend(_weak_cap_reasons)
    confidence = round(min(abs(score) / MAX_SCORE, 1.0), 2)

    # AGREEMENT — how many DISTINCT theories NET-vote the winning side.
    # Counted from per-theory NET votes (same _parse_votes aggregation that
    # feed.py uses to grade theory_votes), NOT raw reason lines — a theory
    # like RUN emits sub-votes on BOTH sides in one candle ("Buyers WON ->
    # CALL" plus "Long upper wick -> PUT"), and the old line-based count
    # credited it to the winning side even when its NET vote opposed the
    # signal, silently inflating `agree` (the STRONG/MEDIUM gate) with
    # theories that actually disagreed. REGIME is excluded the same way it
    # already is from theory_votes grading (filter, not a theory).
    _net_votes: dict[str, int] = {}
    for _code, _vdir, _vmag in _parse_votes(reasons, include_muted=False):
        _net_votes[_code] = _net_votes.get(_code, 0) + _vdir * _vmag
    _want = 1 if signal == "CALL" else -1   # score can be 0 (tiebroken picks)
    agree = sum(1 for _nv in _net_votes.values() if _nv * _want > 0)

    # Strength calibration. The OVERHEATED demotion (ensemble piling on =
    # trend-echo failure mode, measured anti-signal ~40% under the old
    # weights) originally sat at |score| >= 10 when p99 was ~14; after the
    # 2026-07-04 bias rework shrank the distribution (replay: p99=9, max=12)
    # it is re-anchored to the new p99 so it stays a tail guard rather than
    # becoming unreachable. The trend-echo pile-on itself is also largely
    # prevented now by the COLOR-GATED CAP upstream.
    # _weak_cap_reasons (tiebreak / noise dead band / parrot guard) hard-cap
    # strength at WEAK — these are the every-candle-mode forced picks.
    if _weak_cap_reasons:
        strength = "WEAK"
    elif abs(score) >= 9:
        strength = "WEAK"
        reasons.append(
            f"OVERHEATED: |score|={abs(score)} >= 9 (p99 tail) — pile-on"
            f" scores measured as anti-signal => strength capped to WEAK")
    elif agree >= 3 and abs(score) >= 3:
        strength = "STRONG"
    elif agree >= 2 and abs(score) >= 2:
        # MEDIUM floor lowered 3 -> 2 (2026-07-06): the bias rework
        # compressed the score scale (p50 = 2), so requiring |score| >= 3
        # left almost nothing between STRONG and WEAK — two distinct
        # theories net-agreeing at score 2 is real (if modest) evidence,
        # not noise.
        strength = "MEDIUM"
    else:
        strength = "WEAK"

    # ── MARKET STATE  Deep-analysis read (2026-07-07, user request) ──────────
    # Names WHAT the market is doing right now — CONTINUATION / EXHAUSTION /
    # REVERSAL / TRAP / RANGE — from the same structural facts the theories
    # above vote on, organized as one market-state read instead of a score
    # pile. Purely informational: it never touches score/signal/strength
    # (every hand-tuned coupling into the calibrated vote pipeline has
    # regressed before). feed.py logs the state + its directional bias into
    # signal_log.tags (ST_* / STBIAS_*) so each state's real accuracy is
    # measurable from live data before anyone trusts it.
    _st_pts: dict[str, float] = {"CONTINUATION": 0.0, "EXHAUSTION": 0.0,
                                 "REVERSAL": 0.0, "TRAP": 0.0, "RANGE": 0.0}
    _st_dir: dict[str, float] = {k: 0.0 for k in _st_pts}
    _st_ev:  dict[str, list[str]] = {k: [] for k in _st_pts}
    _trend_dir = (+1 if _regime == "UPTREND"
                  else -1 if _regime == "DOWNTREND" else 0)
    _cand_dir  = +1 if is_bull else -1
    _close_pos_ms = (c - l) / total_range          # 0 = low … 1 = high
    _avg_body10 = (sum(abs(x["close"] - x["open"]) for x in candles[-10:])
                   / min(10, len(candles))) or 1e-9

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
            # The with-trend wick must DOMINATE — a counter-candle whose
            # opposite wick is the big one (e.g. a hammer at the bottom of a
            # downtrend) is a reversal shape, not a pullback being absorbed.
            _st("CONTINUATION", 2, _trend_dir,
                "Healthy pullback: small counter-candle already wicked back"
                " in the trend direction")
        for _r in reasons:
            if _r.startswith("MTF") and "trend" in _r:
                if (+1 if "-> CALL" in _r else -1) == _trend_dir:
                    _st("CONTINUATION", 1, _trend_dir,
                        "Higher timeframe (5x) trend points the same way")
                break

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
        _pin_anch = _zone == "RESISTANCE"    # pin bar AT its zone = the
        _st("REVERSAL", 3 if _pin_anch else 2, -1,   # textbook strong read
            "Shooting star: the push above was rejected"
            + (" — right at the resistance zone" if _pin_anch else ""))
        _rev_conf += 1
    elif lower_wick / total_range > 0.55 and body / total_range < 0.25:
        _pin_anch = _zone == "SUPPORT"
        _st("REVERSAL", 3 if _pin_anch else 2, +1,
            "Hammer: the push below was rejected"
            + (" — right at the support zone" if _pin_anch else ""))
        _rev_conf += 1
    if (prev_body > 0 and is_bull != prev_bull and body / prev_body >= 1.0
            and _trend_dir and _cand_dir != _trend_dir):
        _st("REVERSAL", 2, _cand_dir,
            "Counter-trend engulfing: the reply candle swallowed the whole"
            " prior body")
        _rev_conf += 1
    for _r in reasons:
        if _r.startswith("STAR"):
            _st("REVERSAL", 2, +1 if "-> CALL" in _r else -1,
                "Morning/Evening Star three-candle turn completed")
            _rev_conf += 1
            break
    # Context gate: a reversal pattern with no exhaustion behind it and no
    # S/R under it is just a shape in the middle of nowhere — half weight.
    if _rev_conf and _st_pts["EXHAUSTION"] < 2 and _zone == "NEUTRAL":
        _st_pts["REVERSAL"] *= 0.5
        _st_dir["REVERSAL"] *= 0.5
        _st_ev["REVERSAL"].append(
            "(unanchored: no exhaustion context, mid-range — weight halved)")

    # TRAP — someone was just baited into a losing position.
    if _best_sweep:
        _tw_tch, _tw_dir, _tw_lvl = _best_sweep
        _st("TRAP", 3, _tw_dir,
            f"Stop-hunt through {_tw_lvl:.5g}: stops grabbed, close reclaimed"
            f" the level — breakout traders trapped")
    if _cur_bpct is not None and body / total_range >= 0.68 and (
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
    if _zz_len >= 4:
        _st("RANGE", 2, -_cand_dir,
            f"{_zz_len} candles alternating color — oscillation, not a move")
    if _is_doji or _is_spin:
        _st("RANGE", 1, 0, "Indecision candle (doji / spinning top)")

    # Winner: most evidence points; ties break toward the more specific
    # state (a trap IS an exhaustion IS a failed continuation).
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
    else:
        _st_bd = _st_dir[_st_win]
        market_state = {
            "state": _st_win,
            "bias": "CALL" if _st_bd > 0 else "PUT" if _st_bd < 0 else "NEUTRAL",
            "conviction": round(100 * _st_pts[_st_win] / _st_tot) if _st_tot else 0,
            "points": {k: round(v, 1) for k, v in _st_pts.items()},
            "evidence": _st_ev[_st_win],
        }

    return {
        "signal":     signal,
        "score":      score,
        "confidence": confidence,
        "agree":      agree,
        "strength":   strength,
        "reasons":    reasons,
        # Formal swing-pivot levels (40-candle lookback, 2+ touches).
        "key_levels": [[round(p, 6), t] for p, t in
                       sorted(_klevels, key=lambda x: -x[1])[:20]],
        # Wick-clustering levels (12-candle lookback, 3+ touches) — a SEPARATE,
        # looser detection that catches repeated-wick zones with no formal
        # pivot (see WICKWALL's own docstring above). Purely visual/context —
        # only WICKWALL's own vote (already in `reasons`/score) uses these for
        # scoring; exposing the raw clusters lets the chart draw them too.
        "wick_walls": {
            "support":    [[round(p, 6), t] for p, t in
                           sorted(_sup_walls, key=lambda x: -x[1])[:10]],
            "resistance": [[round(p, 6), t] for p, t in
                           sorted(_res_walls, key=lambda x: -x[1])[:10]],
        },
        "regime":     {"trend": _regime, "zone": _zone},
        "zigzag":     {"length": _zz_len, "predict": _zz_predict},
        # Deep-analysis market-state read (see MARKET STATE block above) —
        # informational layer, independent of signal/score/strength.
        "market_state": market_state,
    }
