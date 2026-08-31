"""
Strategy runner — composes candlestick pattern detections with per-pair
profiles, market context, and tick microstructure to produce a final
CALL/PUT signal for the next 1-minute candle.

This is the new signal engine. It REPLACES the role of analyze_eoc for
the live trading path while keeping analyze_eoc available for backward
compatibility and shadow comparison.

Design principles (from web research + live measurement):
  1. Each pattern detector returns (matched, direction, strength, reason).
  2. Per-pair profile boosts/suppresses patterns based on what works
     on that pair (USDINR range-bounce, USDMXN momentum, etc.).
  2b. SAME-ANATOMY FAMILY DEDUP — detectors that validate byte-identical
     candle anatomy (PIN_BAR_BULL / HAMMER / DRAGONFLY_DOJI all describe
     one long-lower-wick shape; MARUBOZU / BELT_HOLD describe one big
     body) are collapsed to the strongest member of their family before
     scoring. Stacking clones triple-counted one piece of evidence and
     produced fake "multi-theory agreement" — the measured result was
     MEDIUM signals running 43-47% (worse than a coin flip, live n>1000).
  2c. CONTINUATION-FAMILY REGIME GATE — momentum-continuation votes
     (marubozu/belt-hold/soldiers/crows/separating-lines/on-neck) are
     dampened when the regime does not actually support them (SIDEWAYS,
     or counter-regime direction). Live measurement showed continuation
     bets with no regime support win ~47-48% — an anti-signal.
  3. Context gate: every directional signal is dampened unless it has
     confluence at a key level (S/R, wick wall, round number, supply/
     demand zone, FVG, prior swing).
  4. Trap-wick filter: pairs with high trap_wick_sensitivity dampen
     pin-bar / wick-rejection signals to prevent trading engineered
     stop-hunts.
  5. Session filter: signals outside best_windows get strength dampened
     to 0.5..0.8.
  6. Tick-volume confirmation: when ticks are available, check that the
     directional pressure matches the pattern direction.
  7. EVERY closed candle emits a CALL or PUT signal — never NEUTRAL.
     The always-emit tiebreak guarantees this on any non-empty candle
     window. When no pattern fires, the tiebreak falls through to:
       (a) indep_net lean (color-independent evidence)
       (b) regime direction (UPTREND/DOWNTREND) — FOLLOWED on TREND
           profiles, FADED on mean-reverting archetypes
       (c) final fallback: the just-closed candle's own color — FOLLOWED
           on TREND profiles (continuation_edge >= 1.10), FADED on
           RANGE_BOUNCE / MEAN_REVERT / MIXED profiles whose own research
           notes (and the live measurements in analyze_eoc.py: doji base
           48.3% n=232, spinning-top 47.3% n=237, marubozu 47.3% n=859)
           show color-following wins BELOW 50% on the OTC feed.
     Marked WEAK so the user knows it's a low-confidence tiebreak. This
     matches the user's explicit requirement:
       "প্রত্যেক পেয়ার এ প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে।"

Output schema (consumed by feed.py + chart.js):
  {
    signal: "CALL" | "PUT",     # never "NEUTRAL" on non-empty candles
    score: int,                  # signed vote sum
    confidence: float,           # 0..1
    strength: "STRONG" | "MEDIUM" | "WEAK" | "NONE",
    reasons: list[str],
    patterns_fired: list[str],  # detector names that matched
    context: {...},
    market_state: {...},
  }
"""
from __future__ import annotations
import time
from dataclasses import dataclass

from .patterns import (
    Signal, detect_all, candle_anatomy,
)
from .pair_profiles import (
    get_profile, session_quality, PairProfile,
)


# ── Key-level / context helpers ──────────────────────────────────────────────

def _key_levels(candles: list[dict], lookback: int = 40) -> list[tuple[float, int]]:
    """Same swing-high/low clustering logic as analyze_eoc._key_levels.

    Returns [(price, touches), ...] for levels with 2+ touches.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
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
        if abs(p - cluster[0]) <= cluster[0] * 0.0006:
            cluster.append(p)
        else:
            if len(cluster) >= 2:
                levels.append((sum(cluster) / len(cluster), len(cluster)))
            cluster = [p]
    if len(cluster) >= 2:
        levels.append((sum(cluster) / len(cluster), len(cluster)))
    return levels


def _wick_walls(candles: list[dict], lookback: int = 20
                ) -> tuple[list[tuple[float, float]], list[tuple[float, float]], float]:
    """Same wick-wall clustering as analyze_eoc._wick_wall."""
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if len(recent) < 4:
        return [], [], 0.0
    n = len(recent)
    avg_rng = sum(c["high"] - c["low"] for c in recent) / n
    tol = avg_rng * 0.25
    def _cluster(tips):
        s = sorted(tips, key=lambda x: x[0])
        out = []
        grp_p, grp_w = [s[0][0]], [s[0][1]]
        for t, w in s[1:]:
            if t - grp_p[0] <= tol:
                grp_p.append(t)
                grp_w.append(w)
            else:
                tw = sum(grp_w)
                if tw >= 2.5:
                    out.append((sum(p * wt for p, wt in zip(grp_p, grp_w)) / tw, tw))
                grp_p, grp_w = [t], [w]
        tw = sum(grp_w)
        if tw >= 2.5:
            out.append((sum(p * wt for p, wt in zip(grp_p, grp_w)) / tw, tw))
        return out
    low_tips = [(c["low"], 1.0 - (n - 1 - i) / n) for i, c in enumerate(recent)]
    high_tips = [(c["high"], 1.0 - (n - 1 - i) / n) for i, c in enumerate(recent)]
    return _cluster(low_tips), _cluster(high_tips), avg_rng


def _round_level(price: float) -> tuple[float, str]:
    """Detect proximity to a round number. Returns (level, strength).
    Strengths: BIG (1%), MID (0.5%), SMALL (0.1%), NONE."""
    import math
    if price <= 0:
        return price, "NONE"
    mag = 10.0 ** math.floor(math.log10(abs(price)))
    def _snap(price, step):
        decimals = max(8, -math.floor(math.log10(step)) + 2)
        return round(round(price / step) * step, decimals)
    for frac, label, thr in [(0.01, "BIG", 0.05),
                             (0.005, "MID", 0.06),
                             (0.001, "SMALL", 0.10)]:
        step = mag * frac
        level = _snap(price, step)
        if abs(price - level) < step * thr:
            return level, label
    step = mag * 0.01
    return _snap(price, step), "NONE"


def _market_regime(candles: list[dict], lookback: int = 24) -> tuple[str, str]:
    """UPTREND / DOWNTREND / SIDEWAYS + SUPPORT / RESISTANCE / NEUTRAL zone.

    FIX (regime detector rewrite): the old split-half high/low comparison
    needed only two structurally higher highs/lows to declare a trend — on a
    noisy 1-minute OTC feed that is 2-3 candles of ordinary drift, so chop
    was constantly labelled UPTREND/DOWNTREND and the continuation paths
    traded the END of engineered runs (live replay: following an unconfirmed
    regime scored 48.1-49.5%).

    The detector now fits a least-squares line over the last `lookback`
    closes and demands BOTH:
      - trendiness:  R^2 >= 0.25 (a straight-line move, not two half-window
        extremes), and
      - steepness:   |slope| >= 0.12 ATR per candle (a drift that actually
        moves, not noise).
    Everything else is SIDEWAYS — the honest default on this feed.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if len(recent) < 8:
        return "SIDEWAYS", "NEUTRAL"
    closes = [c["close"] for c in recent]
    n = len(closes)
    mean_x = (n - 1) / 2.0
    mean_y = sum(closes) / n
    sxx = sum((x - mean_x) ** 2 for x in range(n))
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(range(n), closes))
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in closes)
    ss_res = sum((y - (intercept + slope * x)) ** 2
                 for x, y in zip(range(n), closes))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    atr = sum(c["high"] - c["low"] for c in recent) / n
    slope_atr = slope / atr if atr > 0 else 0.0
    if r2 >= 0.25 and abs(slope_atr) >= 0.12:
        regime = "UPTREND" if slope > 0 else "DOWNTREND"
    else:
        regime = "SIDEWAYS"
    full_hi = max(x["high"] for x in recent)
    full_lo = min(x["low"] for x in recent)
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


# ── Statistical base-bias layer ────────────────────────────────────────────

def _candle_color(c: dict) -> int:
    """+1 bull, -1 bear, 0 doji (close == open)."""
    if c["close"] > c["open"]:
        return 1
    if c["close"] < c["open"]:
        return -1
    return 0


def _current_streak(candles: list[dict]) -> int:
    """Length of the same-color run ENDING at the last candle.
    Dojis are skipped (they neither extend nor break a directional run);
    an opposite-colored candle breaks it."""
    if not candles:
        return 0
    col = _candle_color(candles[-1])
    if col == 0:
        return 0
    s = 0
    for c in reversed(candles):
        cc = _candle_color(c)
        if cc == col:
            s += 1
        elif cc == 0:
            continue
        else:
            break
    return s


def _continuation_table(candles: list[dict], window: int = 120
                        ) -> tuple[list[int], dict[int, list[int]]]:
    """Measure THIS pair's own color-continuation behaviour over the trailing
    window. Returns (aggregate [cont, total], buckets {streak: [cont, total]})
    where the bucket key is the run length KNOWN at the prediction moment,
    capped at 3. This is the raw material for the empirical p_cont estimate —
    the only next-candle predictor on a near-memoryless feed that is actually
    measurable from OHLC alone."""
    start = max(1, len(candles) - window)
    agg = [0, 0]
    buckets: dict[int, list[int]] = {1: [0, 0], 2: [0, 0], 3: [0, 0]}
    for i in range(start, len(candles)):
        pc = _candle_color(candles[i - 1])
        cc = _candle_color(candles[i])
        if pc == 0 or cc == 0:
            continue
        # run length as known at the END of candle i-1
        s = 1
        j = i - 2
        while j >= 0:
            pj = _candle_color(candles[j])
            if pj == pc:
                s += 1
                j -= 1
            elif pj == 0:
                j -= 1
            else:
                break
        b = min(s, 3)
        buckets[b][1] += 1
        agg[1] += 1
        if cc == pc:
            buckets[b][0] += 1
            agg[0] += 1
    return agg, buckets


def _stat_bias(candles: list[dict], profile: PairProfile, atr: float,
               sess_q: float
               ) -> tuple[float, list[str], dict]:
    """Statistical next-candle bias — the engine's BASE layer.

    WHY THIS EXISTS: the backtest of the previous engine showed the pattern
    paths sitting at ~50.4% (noise) while the despised fade-the-color
    tiebreak was the BEST path (53.7%). The live feed's own measurements
    agree (color-following bases 47.3-48.3% on the OTC pegs, n=859/237/232).
    The base layer therefore bets on the ONLY thing that is actually
    measurable from 1-minute OHLC on this feed:

      1. EMPIRICAL CONTINUATION RATE — how often has THIS pair's next candle
         repeated the last candle's color, overall and conditioned on the
         current run length (streak buckets 1/2/3+), over the trailing 120
         candles. A one-sided z-test decides whether the estimate is far
         enough from 0.5 to bet on; the bet weight scales with |z|. When the
         pair's own history is inconclusive, the archetype prior applies
         (fade on RANGE_BOUNCE/MEAN_REVERT, follow on real TREND majors,
         weak follow on OTC "trenders" whose runs are documented to exhaust
         in 2-3 candles).
      2. STREAK EXHAUSTION — 3+ same-color candles on range-persistent
         archetypes ("run 2-3 then fade" is the documented OTC peg pattern).
      3. EXTENSION FADE — price stretched from its SMA20 by >= 0.9 ATR on
         range archetypes (>= 2.2 ATR on trenders) fades back toward the
         mean.

    Returns (score, reasons, meta). Score sign IS the directional bias;
    magnitude is conviction in [-5, +5].
    """
    cur = candles[-1]
    col = _candle_color(cur)
    streak = _current_streak(candles)
    look = candles[-20:]
    sma = sum(c["close"] for c in look) / len(look)
    z = (cur["close"] - sma) / atr if atr > 0 else 0.0

    reasons: list[str] = []
    score = 0.0
    meta: dict = {"streak": streak, "z": round(z, 2), "p_cont": None,
                  "bias_src": None}

    agg, buckets = _continuation_table(candles)
    n_agg = agg[1]

    follow = 0.0        # >0 bet color repeats, <0 bet it flips, 0 no bet
    src = None

    # 1a. ARCHETYPE PRIOR first (the documented, measured behavior of this
    # pair class). The empirical estimator may only OVERRIDE it when the
    # deviation is clearly significant — a noisy 75-candle bucket flips
    # sign ~30% of the time, and every wrong flip bets against the prior
    # that matches the pair's actual character (measured cost: several
    # accuracy points on both RANGE and TREND archetypes).
    if profile.archetype == "TREND":
        prior = 1.0 if not profile.asset.endswith("_otc") else 0.6
    elif profile.archetype == "RANGE_BOUNCE":
        prior = -1.1
    elif profile.archetype == "MEAN_REVERT":
        prior = -1.3
    else:  # MIXED
        prior = -0.4 if profile.asset.endswith("_otc") else 0.0

    # 1b. streak-conditional empirical estimate (most specific).
    # Override bar scales with how strong the prior is: z >= 1.6 to
    # override a directional prior (≈ two-sided p < 0.06), z >= 1.0 when
    # no directional prior exists (MIXED real pairs) so the estimator
    # still does its job as the live self-corrector when the feed's
    # character genuinely changes.
    b = min(max(streak, 1), 3)
    bc, bt = buckets[b]
    if bt >= 25:
        z_b = (bc - bt / 2.0) * 2.0 / (bt ** 0.5)
        _bar = 1.6 if abs(prior) >= 0.5 else 1.0
        if abs(z_b) >= _bar:
            follow = max(-2.0, min(2.0, z_b * 0.7))
            src = (f"streak-{b} empirical: continuation "
                   f"{bc}/{bt} ({bc / bt:.0%})")

    # 1c. aggregate empirical estimate (same override logic, higher bar).
    if follow == 0.0 and n_agg >= 60:
        z_a = (agg[0] - n_agg / 2.0) * 2.0 / (n_agg ** 0.5)
        _bar = 1.8 if abs(prior) >= 0.5 else 1.2
        if abs(z_a) >= _bar:
            follow = max(-1.8, min(1.8, z_a * 0.6))
            src = f"empirical p_cont={agg[0] / n_agg:.0%} (n={n_agg})"

    # 1d. fall back to the prior
    if follow == 0.0:
        follow = prior
        src = f"archetype prior ({profile.archetype})"

    meta["bias_src"] = src
    if col != 0 and follow != 0.0:
        score += follow * col
        meta["p_cont"] = round(0.5 + follow / 4.4, 3)

    # 2. streak exhaustion on range-persistent archetypes
    if (col != 0 and streak >= 3
            and profile.archetype in ("RANGE_BOUNCE", "MEAN_REVERT", "MIXED")):
        w = min(1.8, (streak - 2) * 0.6)
        if profile.archetype == "MIXED":
            w *= 0.6
        score += -col * w
        reasons.append(f"STAT  {streak}-candle {'bull' if col > 0 else 'bear'} "
                       f"run on {profile.archetype} pair (run-then-fade) -> "
                       f"{'CALL' if -col > 0 else 'PUT'} (x{max(1, round(w))})")

    # 3. extension fade from SMA20
    if profile.archetype == "TREND":
        if z > 2.2:
            score -= min(1.0, (z - 2.2) * 0.8)
        elif z < -2.2:
            score += min(1.0, (-2.2 - z) * 0.8)
    else:
        if z > 0.9:
            score -= min(1.6, (z - 0.9) * 0.9)
            reasons.append(f"STAT  price {z:+.1f} ATR above SMA20 "
                           f"(stretched) -> PUT (x{max(1, round(min(1.6, (z - 0.9) * 0.9)))})")
        elif z < -0.9:
            score += min(1.6, (-0.9 - z) * 0.9)
            reasons.append(f"STAT  price {z:+.1f} ATR below SMA20 "
                           f"(stretched) -> CALL (x{max(1, round(min(1.6, (-0.9 - z) * 0.9)))})")

    # main color-bias vote line (parsed as a theory vote code "STAT")
    if col != 0 and abs(follow) >= 0.3:
        _dir = follow * col
        _mag = max(1, round(abs(follow)))
        reasons.insert(0, f"STAT  {src} -> "
                          f"{'CALL' if _dir > 0 else 'PUT'} (x{_mag})")

    # dead sessions reduce trust in the statistical edge too (softly — the
    # base layer must never fully disappear or the tiebreak coin-flips)
    score *= (0.55 + 0.45 * sess_q)

    return max(-5.0, min(5.0, score)), reasons, meta


# ── Confluence detection ──────────────────────────────────────────────────────

def _confluence_score(cur: dict, candles: list[dict], klevels, sup_walls, res_walls,
                      atr: float) -> tuple[float, str]:
    """Score how close the current candle's REJECTION wick is to a confluence
    of key levels. Returns (bonus 0..3, description string).
    """
    if atr <= 0:
        return 0.0, ""
    a = candle_anatomy(cur)
    # Test the wick tip closest to the candle's "rejection" direction.
    # For a bullish pin bar (long lower wick), test the LOW.
    # For a bearish pin bar (long upper wick), test the HIGH.
    test_low = a["lower"] > a["upper"]
    test_price = cur["low"] if test_low else cur["high"]
    bonus = 0.0
    desc_parts = []

    # Key level touches
    for lvl, touches in klevels:
        if abs(test_price - lvl) <= lvl * 0.0006:
            bonus += min(2.0, touches * 0.7)
            desc_parts.append(f"key level x{touches} @ {lvl:.5f}")
            break

    # Wick wall cluster
    walls = sup_walls if test_low else res_walls
    for lvl, weight in walls:
        if abs(test_price - lvl) <= atr * 0.25:
            bonus += min(1.5, weight * 0.4)
            desc_parts.append(f"wick wall @ {lvl:.5f}")
            break

    # Round number
    _, rnd_strength = _round_level(test_price)
    if rnd_strength == "BIG":
        bonus += 1.0
        desc_parts.append("BIG round number")
    elif rnd_strength == "MID":
        bonus += 0.5
        desc_parts.append("MID round number")

    return min(3.0, bonus), "; ".join(desc_parts)


# ── Trap-wick filter ──────────────────────────────────────────────────────────

def _trap_wick_dampen(cur: dict, profile: PairProfile, atr: float) -> float:
    """Return a 0..1 multiplier to dampen wick-rejection signals when the
    current candle looks like a Quotex-engineered trap wick.

    Trap wick = extreme wick (>3x body) on a small-bodied candle that closed
    far from the wick tip — characteristic of engineered stop-hunts.
    """
    if atr <= 0 or profile.trap_wick_sensitivity <= 1.0:
        return 1.0
    a = candle_anatomy(cur)
    if a["body"] == 0:
        return 1.0
    # Trap wick detection
    max_wick = max(a["upper"], a["lower"])
    if max_wick <= 2.5 * a["body"]:
        return 1.0
    # Ratio of wick to body — the larger, the more suspicious
    ratio = max_wick / a["body"]
    # Range should also be a noticeable fraction of ATR
    if a["range"] < atr * 0.6:
        return 1.0
    # Dampen: ratio 2.5x = 1.0 (no dampen); 5x = 0.7; 8x = 0.4
    dampen_factor = max(0.3, 1.0 - (ratio - 2.5) * 0.12 * profile.trap_wick_sensitivity)
    return dampen_factor


# ── Tick microstructure (RUN/LIVE equivalent) ────────────────────────────────

def _tick_pressure(ticks: list[float]) -> tuple[float, str]:
    """Return (buyer_pct, classification) from a list of tick prices.

    buyer_pct > 0.6 = buyer dominated; < 0.4 = seller dominated; else neutral.
    """
    if not ticks or len(ticks) < 5:
        return 0.5, "INSUFFICIENT"
    up = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i-1])
    dn = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i-1])
    tot = up + dn
    if tot == 0:
        return 0.5, "INSUFFICIENT"
    bp = up / tot
    if bp >= 0.60:
        return bp, "BUYER"
    if bp <= 0.40:
        return bp, "SELLER"
    return bp, "MIXED"


def _absorption(cur: dict, ticks: list[float]) -> tuple[int, str]:
    """Detect absorption pattern: candle closed one way but ticks pushed the
    other. Returns (direction, reason) — direction +1 = CALL (buyers absorbed
    selling), -1 = PUT, 0 = none.
    """
    if not ticks or len(ticks) < 15:
        return 0, ""
    a = candle_anatomy(cur)
    bp, cls = _tick_pressure(ticks)
    # Closed up but sellers pushed -> bearish absorption
    if a["close_pos"] >= 0.72 and bp <= 0.40:
        return -1, (f"ABSORPTION: closed up at {a['close_pos']:.0%} but only "
                   f"{bp:.0%} up-ticks -> sellers absorbed -> PUT")
    # Closed down but buyers pushed -> bullish absorption
    if a["close_pos"] <= 0.20 and bp >= 0.60:
        return +1, (f"ABSORPTION: closed down at {a['close_pos']:.0%} but "
                    f"{bp:.0%} up-ticks -> buyers absorbed -> CALL")
    return 0, ""


# ── Market-state deep analysis (compressed version) ──────────────────────────

def _market_state(candles: list[dict], regime: str, zone: str,
                  klevels, atr: float, ticks: list[float] | None = None,
                  profile: PairProfile | None = None
                  ) -> dict:
    """Identify CONTINUATION / EXHAUSTION / REVERSAL / TRAP / RANGE / UNCLEAR.

    Mirrors analyze_eoc's market-state block but with cleaner structure.
    Returns {state, bias, conviction, points}.

    FIX (profile-aware exhaustion): the exhaustion threshold is now per
    archetype. The per-pair research notes are explicit that the OTC pegs
    push 2-3 candles then fade ("trend 2-3 candles, then fade 4th" on
    USDPHP; "3-candle extremes + pin-bar reversals win" on USDBDT), but
    the old hardcoded streak >= 4 only started fading after the move was
    already over on those pairs. MEAN_REVERT / RANGE_BOUNCE / MIXED
    profiles now exhaust at streak >= 3; TREND profiles keep 4.
    """
    exh_streak = 4
    if profile is not None and profile.archetype in (
            "MEAN_REVERT", "RANGE_BOUNCE", "MIXED"):
        exh_streak = 3
    if len(candles) < 2:
        return {"state": "UNCLEAR", "bias": "NEUTRAL", "conviction": 0,
                "points": {}}
    cur = candles[-1]
    a = candle_anatomy(cur)
    trend_dir = +1 if regime == "UPTREND" else -1 if regime == "DOWNTREND" else 0
    # FIX: treat doji (close == open) as non-directional so EXHAUSTION
    # doesn't fire wrongly for a streak of dojis.
    if a["brr"] < 0.05:
        cand_dir = 0
    else:
        cand_dir = +1 if a["is_bull"] else -1

    # streak
    # FIX: iterate down to index 0 (was 0 exclusive — off-by-one that
    # suppressed EXHAUSTION for exactly-4 streaks).
    streak = 0
    for i in range(len(candles) - 1, -1, -1):
        if a["brr"] < 0.05:
            # doji doesn't extend a directional streak
            break
        same_color = (candles[i]["close"] >= candles[i]["open"]) == a["is_bull"]
        if same_color:
            streak += 1
        else:
            break

    pts = {"CONTINUATION": 0.0, "EXHAUSTION": 0.0,
           "REVERSAL": 0.0, "TRAP": 0.0, "RANGE": 0.0}
    dirs = {k: 0.0 for k in pts}

    def add(state, p, d):
        pts[state] += p
        dirs[state] += d * p

    # CONTINUATION
    if trend_dir:
        add("CONTINUATION", 2, trend_dir)
        if cand_dir == trend_dir and a["brr"] >= 0.55:
            add("CONTINUATION", 2, trend_dir)

    # EXHAUSTION — threshold per archetype (see docstring)
    if streak >= exh_streak:
        add("EXHAUSTION", 2 + (1 if streak >= exh_streak + 2 else 0), -cand_dir)
    if ticks and len(ticks) >= 15:
        bp, _ = _tick_pressure(ticks)
        if bp >= 0.78 or bp <= 0.22:
            add("EXHAUSTION", 1, -cand_dir)

    # REVERSAL — pin bar shapes
    # NOTE: candle_anatomy() floors "range" to 1e-9, so it's never falsy —
    # the old `X if a["range"] else False and Y` form parsed (by Python's
    # conditional-expression precedence) as `X if range else (False and Y)`,
    # silently dropping the `and a["brr"] < 0.25` body-size filter entirely.
    # Any large-bodied candle with a merely-large wick was wrongly counted
    # as a pin-bar REVERSAL. Parenthesized correctly below.
    if a["range"] and a["upper"] / a["range"] > 0.55 and a["brr"] < 0.25:
        add("REVERSAL", 3 if zone == "RESISTANCE" else 2, -1)
    elif a["range"] and a["lower"] / a["range"] > 0.55 and a["brr"] < 0.25:
        add("REVERSAL", 3 if zone == "SUPPORT" else 2, +1)

    # absorption reversal
    if ticks and len(ticks) >= 15:
        abs_dir, _ = _absorption(cur, ticks)
        if abs_dir:
            add("REVERSAL", 3, abs_dir)

    # TRAP — failed breakout
    if len(candles) >= 2:
        prev = candles[-2]
        for lvl, t in klevels:
            if prev["close"] > lvl >= cur["close"]:
                add("TRAP", 2, -1)
                break
            if prev["close"] < lvl <= cur["close"]:
                add("TRAP", 2, +1)
                break

    # RANGE
    if trend_dir == 0:
        add("RANGE", 2, 0)
        if zone == "RESISTANCE":
            add("RANGE", 1, -1)
        elif zone == "SUPPORT":
            add("RANGE", 1, +1)
        # FIX (archetype prior): wire the per-pair profile's documented
        # persistence belief into the SCORE, not just the tiebreaks.
        # The live measurements (color-following bases 47.3-48.3%) and the
        # per-pair research notes (range-bounce/mean-revert dominate the
        # OTC pegs; TREND pairs persist) define a small per-candle prior:
        #   RANGE_BOUNCE / MEAN_REVERT: fade the last candle,
        #   TREND (continuation_edge >= 1.10): follow it,
        #   MIXED: no prior (p ~= 0.5 — nothing to encode).
        # Without this the pattern votes (noise around 50% on chop)
        # diluted the only measured edge those pairs have.
        if profile is not None and cand_dir != 0:
            if profile.archetype in ("RANGE_BOUNCE", "MEAN_REVERT"):
                add("RANGE", 1.5, -cand_dir)
            elif profile.archetype == "TREND":
                add("CONTINUATION", 1.0, cand_dir)
    if a["brr"] <= 0.10:
        add("RANGE", 1, 0)

    # Pick winner
    prio = ["TRAP", "REVERSAL", "EXHAUSTION", "CONTINUATION", "RANGE"]
    win = max(prio, key=lambda k: (pts[k], -prio.index(k)))
    tot = sum(pts.values())
    if pts[win] < 3 or tot == 0:
        return {"state": "UNCLEAR", "bias": "NEUTRAL", "conviction": 0,
                "points": {k: round(v, 1) for k, v in pts.items()}}
    bd = dirs[win]
    bias = "CALL" if bd > 0 else "PUT" if bd < 0 else "NEUTRAL"
    conv = round(100 * pts[win] / tot)
    return {"state": win, "bias": bias, "conviction": conv,
            "points": {k: round(v, 1) for k, v in pts.items()}}


# ── Public entry: run_strategy ───────────────────────────────────────────────

# Momentum-continuation patterns: their direction bet is "the last move
# keeps going". Without regime support that bet measured below 50% on the
# live OTC feed (see module docstring 2c).
_CONTINUATION_FAMILY = {
    "MARUBOZU", "BELT_HOLD_BULL", "BELT_HOLD_BEAR",
    "THREE_WHITE_SOLDIERS", "THREE_BLACK_CROWS",
    "SEPARATING_LINES_BULL", "SEPARATING_LINES_BEAR", "ON_NECK",
    "RISING_THREE_METHODS", "FALLING_THREE_METHODS",
}


def run_strategy(candles: list[dict],
                 asset: str,
                 period: int = 60,
                 ticks: list[float] | None = None,
                 running_ticks: list[float] | None = None,
                 muted: dict[str, str] | None = None) -> dict:
    """Compute the next-candle signal for `asset`.

    Returns the standard signal dict consumed by feed.py:
      {signal, score, confidence, strength, reasons, patterns_fired,
       context, market_state, key_levels, wick_walls, regime}

    Always returns a CALL/PUT/NEUTRAL signal — never raises.
    """
    if not candles:
        return _empty_signal("no candles provided")

    profile = get_profile(asset)
    cur = candles[-1]
    a = candle_anatomy(cur)

    # Context: key levels, wick walls, regime, ATR
    klevels = _key_levels(candles)
    sup_walls, res_walls, atr10 = _wick_walls(candles)
    atr = atr10 or (cur["high"] - cur["low"]) or 0.0001
    regime, zone = _market_regime(candles)

    # Per-pair ATR floor — dampen signal if candle range is too small.
    # FIX: cap multiplier at 1.0 so it ONLY dampens weak signals,
    # never amplifies strong ones 20x for volatile pairs.
    cur_atr_pct = atr / cur["close"] if cur["close"] else 0
    atr_floor_mult = min(1.0, max(0.3, cur_atr_pct / max(profile.min_atr_pct, 1e-9)))

    # Hour-based session quality (UTC).
    # FIX: derive the hour from the CANDLE's own timestamp, not the wall
    # clock. The wall-clock version made the session filter untestable in
    # backtests (every synthetic candle was graded at "now"), desynced it
    # from replayed history, and produced meaningless Best/Worst-Hour
    # report columns. Falls back to wall clock only if `time` is missing.
    _candle_time = cur.get("time") if isinstance(cur, dict) else None
    hour_utc = (time.gmtime(_candle_time).tm_hour
                if _candle_time else time.gmtime().tm_hour)
    sess_q = session_quality(profile, hour_utc)

    # Trap-wick dampener
    trap_mult = _trap_wick_dampen(cur, profile, atr)

    # Pattern detection
    fired = detect_all(candles)

    # Filter out muted patterns
    if muted:
        fired = [s for s in fired if s.name not in muted]

    # Context-gate the hammer/shooting-star family. HAMMER/HANGING_MAN (and
    # SHOOTING_STAR/INVERTED_HAMMER) share byte-identical anatomy checks —
    # the same small-body-long-wick shape means the OPPOSITE thing depending
    # on whether it follows a down-move (bullish reversal) or an up-move
    # (bearish reversal). Both docstrings in patterns.py say this context
    # check is "the caller's job", but detect_all() runs unconditionally and
    # nothing downstream ever applied it — so both fired together on every
    # hammer-shaped candle and their opposite-signed votes partially
    # cancelled regardless of the real context. regime/zone are already
    # computed above (_market_regime).
    _CONTEXT_GATE = {
        "HAMMER":          lambda: regime == "DOWNTREND" or zone == "SUPPORT",
        "INVERTED_HAMMER": lambda: regime == "DOWNTREND" or zone == "SUPPORT",
        "HANGING_MAN":     lambda: regime == "UPTREND" or zone == "RESISTANCE",
        "SHOOTING_STAR":   lambda: regime == "UPTREND" or zone == "RESISTANCE",
    }
    fired = [s for s in fired
             if s.name not in _CONTEXT_GATE or _CONTEXT_GATE[s.name]()]

    # ── Same-anatomy family dedup (see module docstring 2b) ──────────────
    # Detectors that validate the SAME shape must not stack votes:
    # PIN_BAR_BULL + HAMMER + DRAGONFLY_DOJI are one long-lower-wick candle
    # counted three times; MARUBOZU + BELT_HOLD are one big body counted
    # twice. Keep the strongest member per family.
    _FAMILY_OF = {
        "MARUBOZU": "body_cont", "BELT_HOLD_BULL": "body_cont",
        "BELT_HOLD_BEAR": "body_cont",
        "THREE_WHITE_SOLDIERS": "trend3", "THREE_BLACK_CROWS": "trend3",
        "HAMMER": "wick_rev", "HANGING_MAN": "wick_rev",
        "SHOOTING_STAR": "wick_rev", "INVERTED_HAMMER": "wick_rev",
        "DRAGONFLY_DOJI": "wick_rev", "GRAVESTONE_DOJI": "wick_rev",
        "PIN_BAR_BULL": "wick_rev", "PIN_BAR_BEAR": "wick_rev",
        "BULLISH_ENGULFING": "engulf", "BEARISH_ENGULFING": "engulf",
        "THREE_OUTSIDE_UP": "engulf", "THREE_OUTSIDE_DOWN": "engulf",
        "TWEEZER_TOP": "tweezer", "TWEEZER_BOTTOM": "tweezer",
        "BULLISH_HARAMI": "harami", "BEARISH_HARAMI": "harami",
        "THREE_INSIDE_UP": "harami", "THREE_INSIDE_DOWN": "harami",
        "MORNING_STAR": "star", "EVENING_STAR": "star",
        "SEPARATING_LINES_BULL": "sep_cont", "SEPARATING_LINES_BEAR": "sep_cont",
        "BULLISH_COUNTERATTACK": "counter", "BEARISH_COUNTERATTACK": "counter",
    }
    _dedup: dict[str, Signal] = {}
    for _s in fired:
        _key = _FAMILY_OF.get(_s.name, _s.name)
        _prev = _dedup.get(_key)
        if _prev is None or _s.strength > _prev.strength:
            _dedup[_key] = _s
    fired = list(_dedup.values())

    # ── Compose score: STAT base layer + capped evidence layer ────────────
    # ARCHITECTURE (signal-accuracy overhaul): the previous engine summed
    # pattern votes as the primary signal and only faded when the sum was
    # exactly 0 — the backtest measured that path mix at PATTERN_VOTE 50.4%,
    # TIEBREAK_COLOR 53.7%. The weighting is now inverted:
    #   score = stat_score (measured statistical bias, always present)
    #         + evidence_score (patterns / ticks / market state, CAPPED)
    # Patterns measured ~50% on this feed — they are EVIDENCE that can
    # refine the base bias, never a reason to override it.
    stat_score, stat_reasons, stat_meta = _stat_bias(
        candles, profile, atr, sess_q)
    stat_dir = 1 if stat_score > 0 else -1 if stat_score < 0 else 0

    reasons: list[str] = list(stat_reasons)
    patterns_fired: list[str] = []
    evidence_score = 0.0
    indep_dirs: list[tuple[str, int]] = []
    if abs(stat_score) >= 0.5:
        # register the base bias as an independent voter (mag 1..3)
        indep_dirs.append(("STAT", stat_dir,
                           max(1, min(3, round(abs(stat_score))))))

    for sig in fired:
        patterns_fired.append(sig.name)
        # Per-pair weight (default 1.0)
        w = profile.strategy_weights.get(sig.name, 1.0)
        # ── Continuation-family regime gate ─────────────────────────────
        # Momentum-continuation votes without regime support measured
        # ~47-48% live (marubozu base n=859) — an anti-signal. On the
        # range-persistent archetypes the base layer already fades; the
        # patterns are dampened to near-zero so they cannot drag the
        # score back toward the losing follow-side. Aligned continuation
        # on a real TREND regime keeps the most weight, still < 1.0.
        if sig.name in _CONTINUATION_FAMILY:
            if regime == "SIDEWAYS":
                w *= 0.15 if profile.archetype in (
                    "RANGE_BOUNCE", "MEAN_REVERT", "MIXED") else 0.35
            elif (regime == "UPTREND" and sig.direction < 0) or \
                    (regime == "DOWNTREND" and sig.direction > 0):
                w *= 0.40
            else:
                # vote ALIGNED with regime — still capped by archetype
                w *= {"TREND": 0.85, "MIXED": 0.55,
                      "RANGE_BOUNCE": 0.35, "MEAN_REVERT": 0.30
                      }.get(profile.archetype, 0.55)
        # Trap-wick dampen for pin-bar-like patterns (long-wick single candles)
        if sig.name in ("PIN_BAR_BULL", "PIN_BAR_BEAR", "HAMMER",
                        "HANGING_MAN", "SHOOTING_STAR", "INVERTED_HAMMER",
                        "DRAGONFLY_DOJI", "GRAVESTONE_DOJI"):
            w *= trap_mult
        # Session quality
        w *= sess_q
        # ATR floor
        w *= atr_floor_mult
        # Convert strength (0..1) to integer vote magnitude (1..4).
        # FIX: don't floor mag=1 for marginal matches (strength near 0).
        # The old `max(1, round(s*4))` cast 0-strength matches as full +1 votes,
        # defeating the strength gating entirely.
        mag = round(sig.strength * 4)
        if mag <= 0:
            continue
        mag = int(mag * w)
        if mag == 0:
            continue
        evidence_score += sig.direction * mag
        if sig.direction != 0:
            # FIX: include magnitude in indep_dirs so agree_weight reflects
            # conviction (was direction-only, making agree_weight == agree,
            # making the STRONG gate unreachable).
            indep_dirs.append((sig.name, sig.direction, mag))
        reasons.append(
            f"{sig.name}  {sig.reason} -> "
            f"{'CALL' if sig.direction > 0 else 'PUT' if sig.direction < 0 else 'NEUTRAL'}"
            f" (x{mag})")

    # EVIDENCE CAP: patterns/ticks may refine the base bias but must never
    # be able to overpower it (they measured ~50% — no better than noise).
    # Without the cap, 3-4 coincident noise patterns still flipped the
    # signal against the measured edge — the exact mechanism that made
    # STRONG signals score WORSE than WEAK (49.4% vs 51.0%).
    _EV_CAP = 2.5
    evidence_score = max(-_EV_CAP, min(_EV_CAP, evidence_score))
    # OPPOSING-EVIDENCE DISCOUNT: evidence that fights the measured base
    # bias is noise until proven otherwise (patterns ~50% live). Discount
    # it to 35% so it can refine conviction but can almost never flip the
    # statistical edge — the single biggest measured leak (naive fade beat
    # the old engine by 5-6pp on RANGE pairs purely through flips).
    if (stat_dir != 0 and evidence_score != 0
            and (evidence_score > 0) != (stat_dir > 0)):
        evidence_score *= 0.35
    score = stat_score + evidence_score

    # Tick absorption (independent of pattern — real tick information)
    if ticks and len(ticks) >= 15:
        abs_dir, abs_reason = _absorption(cur, ticks)
        if abs_dir:
            abs_w = profile.strategy_weights.get("ABSORPTION", 1.0) * sess_q
            abs_mag = max(1, round(0.7 * 4 * abs_w))
            evidence_score += abs_dir * abs_mag
            indep_dirs.append(("ABSORPTION", abs_dir, abs_mag))
            reasons.append(
                f"ABSORPTION  {abs_reason} -> "
                f"{'CALL' if abs_dir > 0 else 'PUT'} (x{abs_mag})")
            patterns_fired.append("ABSORPTION")

    # LIVE running-tick vote — uses the CURRENTLY OPEN candle's ticks
    # (separate from `ticks` which are the JUST-CLOSED candle's ticks).
    # Without this block the running_ticks parameter was accepted but
    # never used, making the feed's mid-candle re-eval a complete no-op.
    if running_ticks and len(running_ticks) >= 15:
        # Build a synthetic "running candle" from the running ticks so
        # _absorption sees the right close_pos / anatomy. The just-closed
        # `cur` candle is NOT what the running ticks describe.
        _r_open  = running_ticks[0]
        _r_close = running_ticks[-1]
        _r_hi    = max(running_ticks)
        _r_lo    = min(running_ticks)
        _running_candle = {"open": _r_open, "high": _r_hi,
                           "low": _r_lo, "close": _r_close, "time": cur["time"]}
        live_dir, live_reason = _absorption(_running_candle, running_ticks)
        if live_dir:
            live_w = profile.strategy_weights.get("LIVE", 1.0) * sess_q
            live_mag = max(1, round(0.7 * 4 * live_w))
            evidence_score += live_dir * live_mag
            indep_dirs.append(("LIVE", live_dir, live_mag))
            reasons.append(
                f"LIVE  {live_reason} on running candle -> "
                f"{'CALL' if live_dir > 0 else 'PUT'} (x{live_mag})")
            patterns_fired.append("LIVE")

    # Market-state deep analysis (context read on the improved regime
    # detector; evidence-layer voter)
    ms = _market_state(candles, regime, zone, klevels, atr, ticks,
                       profile=profile)
    if ms["bias"] in ("CALL", "PUT") and ms["conviction"] >= 30:
        ms_dir = +1 if ms["bias"] == "CALL" else -1
        ms_mag = min(3, max(1, ms["conviction"] // 30))
        ms_w = profile.strategy_weights.get("MARKET_STATE", 1.0) * sess_q
        ms_mag = int(ms_mag * ms_w)
        if ms_mag > 0:
            evidence_score += ms_dir * ms_mag
            indep_dirs.append(("MARKET_STATE", ms_dir, ms_mag))
            reasons.append(
                f"MARKET_STATE  {ms['state']} (bias {ms['bias']}, "
                f"conviction {ms['conviction']}%) -> {ms['bias']} (x{ms_mag})")

    # ── Confluence bonus ──────────────────────────────────────────────────
    conf_bonus, conf_desc = _confluence_score(
        cur, candles, klevels, sup_walls, res_walls, atr)
    if conf_bonus > 0 and score != 0:
        # FIX: apply the bonus in the direction of the REJECTION WICK —
        # not blindly in the direction the score already leans. The old
        # `score += sign(score) * bonus` strengthened a bullish lean even
        # when the confluence was a bearish upper-wick rejection at a key
        # resistance level — amplifying the exact wrong signal.
        _conf_test_low = a["lower"] > a["upper"]  # same test _confluence_score uses
        _conf_dir = 1 if _conf_test_low else -1
        if (score > 0) == (_conf_dir > 0):
            evidence_score += min(1.5, conf_bonus)   # capped — evidence layer
            score = stat_score + evidence_score
            reasons.append(
                f"CONFLUENCE  +{int(conf_bonus)} agrees with "
                f"{'CALL' if _conf_dir > 0 else 'PUT'} rejection ({conf_desc})")
        else:
            # Conflicting confluence: dampen, but never flip the signal
            # here (cap at half the current magnitude) — the flip decision
            # belongs to the pattern votes, not a proximity heuristic.
            _damp = min(int(conf_bonus * 0.6), max(0, abs(score) // 2))
            if _damp > 0:
                score += -_damp if score > 0 else _damp
            reasons.append(
                f"CONFLUENCE  conflict: rejection wick points "
                f"{'CALL' if _conf_dir > 0 else 'PUT'} vs score lean "
                f"-> -{_damp} dampen ({conf_desc})")

    # Information weight dampeners (low ticks, tiny range, dead session).
    # FIX: scale dampener by min(1, |score|) so a |score|=1 weak signal
    # is dampened by 0 (not 1) — otherwise a tiny dampener flips the
    # signal to score=0, which then hits the last-candle-color tiebreak
    # and can FLIP direction.
    weak_caps: list[str] = []
    if ticks is not None and len(ticks) < 15:
        d = int(abs(score) * 0.30)  # may be 0 for |score|<=3
        if d > 0:
            score += -d if score > 0 else d
            weak_caps.append(f"(low ticks) only {len(ticks)} ticks -> -{d} dampen")

    if atr > 0 and a["range"] < atr * 0.30:
        d = int(abs(score) * 0.25)
        if d > 0:
            score += -d if score > 0 else d
            weak_caps.append(
                f"(tiny range) range {a['range']/atr:.0%} of ATR -> -{d} dampen")

    if sess_q < 0.6:
        weak_caps.append(
            f"(low-liquidity session) UTC {hour_utc:02d}h -> strength dampened")

    # ── Final signal decision ─────────────────────────────────────────────
    # The STAT base layer makes score == 0 rare (it only happens when the
    # archetype prior is 0.0 on MIXED real pairs AND no evidence voted).
    # The tiebreak below therefore only fires on genuinely edge-less
    # candles — and it keeps the measured fade/follow preference.
    indep_net = sum(d for _, d, _ in indep_dirs)
    signal = "CALL" if score > 0 else "PUT" if score < 0 else "NEUTRAL"

    # Tiebreak for NEUTRAL: every candle emits a CALL/PUT (the user's hard
    # requirement). Order:
    #   1. independent evidence net lean,
    #   2. confirmed regime continuation / faded unconfirmed regime,
    #   3. archetype-aware fade-or-follow of the last candle's color
    #      (TREND majors follow; range/mean-revert/mixed fade — the only
    #      measured >50% behavior on the OTC pegs).
    follow_pref = profile.continuation_edge >= 1.10
    if signal == "NEUTRAL":
        if indep_net != 0:
            signal = "CALL" if indep_net > 0 else "PUT"
            weak_caps.append(
                f"TIEBREAK: score 0 but indep_net leans {signal} -> WEAK")
        elif regime in ("UPTREND", "DOWNTREND"):
            _ms_confirms = (ms.get("state") == "CONTINUATION"
                            and ms.get("conviction", 0) >= 30
                            and ms.get("bias") in ("CALL", "PUT"))
            if _ms_confirms:
                signal = "CALL" if regime == "UPTREND" else "PUT"
                weak_caps.append(
                    f"TIEBREAK: regime={regime} + market_state continuation "
                    f"@ {ms.get('conviction')}% -> {signal} (WEAK)")
            else:
                signal = "PUT" if regime == "UPTREND" else "CALL"
                weak_caps.append(
                    f"TIEBREAK: regime={regime} UNCONFIRMED (exhaustion-lag "
                    f"moment) -> fade to {signal} (WEAK)")
        else:
            if a["brr"] < 0.05:
                # doji — lean by close_pos (close > midpoint -> up-lean);
                # a doji must not default to CALL (the old is_bull bias).
                _lean = 1 if a["close_pos"] >= 0.5 else -1
                _lean_lbl = f"doji (close_pos {a['close_pos']:.0%})"
            else:
                _lean = 1 if a["is_bull"] else -1
                _lean_lbl = "bull" if a["is_bull"] else "bear"
            if follow_pref:
                signal = "CALL" if _lean > 0 else "PUT"
                weak_caps.append(
                    f"TIEBREAK: no edge -> follow last-candle color "
                    f"({_lean_lbl}) -> {signal} (WEAK)")
            else:
                signal = "PUT" if _lean > 0 else "CALL"
                weak_caps.append(
                    f"TIEBREAK: no edge -> {profile.archetype} fade of "
                    f"last-candle color ({_lean_lbl}) -> {signal} (WEAK)")
    elif abs(score) < 1.2:
        weak_caps.append(f"NO EDGE: |score|={abs(score):.1f} is noise-level -> WEAK")

    # ── Strength calibration ──────────────────────────────────────────────
    # Recalibrated (fixes the measured INVERSION): the old STRONG gate
    # required 3+ agreeing theories, but patterns are ~50% noise, so
    # stacking three of them selected the WORST candles (STRONG 49.4% <
    # WEAK 51.0% in backtest). Strength now demands STAT+EVIDENCE
    # agreement — the measured statistical edge and the candle/tick
    # structure must point the SAME way before a signal is called STRONG.
    MAX_SCORE = 9
    confidence = round(min(abs(score) / MAX_SCORE, 1.0), 2)

    # Agreement count — distinct evidence FAMILIES (post-dedup) weighted
    # by conviction.
    _net_votes: dict[str, int] = {}
    for name, d, mag in indep_dirs:
        _net_votes[name] = _net_votes.get(name, 0) + d * mag
    want = 1 if signal == "CALL" else -1
    agree = sum(1 for nv in _net_votes.values() if nv * want > 0)
    agree_weight = sum(abs(nv) for nv in _net_votes.values() if nv * want > 0)
    ev_agrees = (stat_dir != 0 and evidence_score != 0
                 and (evidence_score > 0) == (stat_dir > 0))
    ev_opposes = (evidence_score != 0 and stat_dir != 0
                  and (evidence_score > 0) != (stat_dir > 0))

    if signal == "NEUTRAL":
        strength = "NONE"
    elif weak_caps:
        strength = "WEAK"
    elif ev_agrees and abs(score) >= 4.5 and abs(stat_score) >= 1.5:
        strength = "STRONG"
    elif (abs(score) >= 2.5 and not ev_opposes
          and (agree >= 2 or abs(stat_score) >= 2.0)):
        strength = "MEDIUM"
    else:
        strength = "WEAK"

    reasons.extend(weak_caps)

    return {
        "signal": signal,
        # int — feed.py postmortem formats it with :+d and the DB column is
        # INTEGER; evidence votes are ints and stat is capped, so rounding
        # never crosses a strength boundary materially.
        "score": int(round(score)),
        "confidence": confidence,
        "agree": agree,
        "agree_weight": agree_weight,
        "strength": strength,
        "reasons": reasons,
        "patterns_fired": patterns_fired,
        "key_levels": [[round(p, 6), t] for p, t in
                       sorted(klevels, key=lambda x: -x[1])[:20]],
        "wick_walls": {
            "support": [[round(p, 6), t] for p, t in
                        sorted(sup_walls, key=lambda x: -x[1])[:10]],
            "resistance": [[round(p, 6), t] for p, t in
                           sorted(res_walls, key=lambda x: -x[1])[:10]],
        },
        "regime": {"trend": regime, "zone": zone},
        "market_state": ms,
        "context": {
            "archetype": profile.archetype,
            "session_quality": round(sess_q, 2),
            "trap_wick_dampen": round(trap_mult, 2),
            "atr_pct": round(cur_atr_pct, 6),
            "min_atr_pct": profile.min_atr_pct,
            "pair_notes": profile.notes,
        },
    }


def _empty_signal(reason: str) -> dict:
    # "Every candle gets CALL/PUT, never NEUTRAL" is a hard product
    # requirement (README's "Signal guarantee", enforced by an assertion in
    # tools/backtest_strategies.py). feed.py intercepts truly-empty calls
    # before they reach run_strategy so this path is currently unreachable
    # in production, but run_strategy is also this package's public entry
    # point (re-exported by strategies/__init__.py) — any other/future
    # caller invoking it directly with an empty candle list would silently
    # get NEUTRAL and violate the guarantee. Default to CALL (a coin-flip
    # pick, same fallback analyze_eoc.py uses) instead.
    return {
        "signal": "CALL", "score": 0, "confidence": 0.0,
        "agree": 0, "agree_weight": 0, "strength": "WEAK",
        "reasons": [f"TIEBREAK: {reason} -> CALL (default pick)"],
        "patterns_fired": [], "key_levels": [],
        "wick_walls": {"support": [], "resistance": []},
        "regime": {"trend": "SIDEWAYS", "zone": "NEUTRAL"},
        "market_state": {"state": "UNCLEAR", "bias": "NEUTRAL",
                         "conviction": 0, "points": {}},
        "context": {},
    }
