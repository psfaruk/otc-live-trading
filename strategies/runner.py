"""
CONFLUENCE COUNCIL ENGINE — the signal core (2026-09 rewrite).

═══════════════════════════════════════════════════════════════════════════
WHY THE OLD ENGINE PRODUCED WRONG SIGNALS (diagnosed, measured):

  1. It FORCED a CALL/PUT on every single candle ("every candle must
     signal"). When no edge existed, tiebreaks manufactured a direction
     from the last candle's colour or an unconfirmed regime — measured at
     47-53% (coin flip or worse). The bulk of emitted signals WERE these
     forced coin flips.  → REMOVED. This engine has NO tiebreak and NO
     fallback of any kind. When the council does not agree, the signal is
     NEUTRAL ("NO TRADE") and nothing is emitted, logged, or graded.
  2. The prediction was silently REPLACED mid-candle by a re-eval that
     used the running candle's own ticks — lookahead contamination: the
     direction graded at close was not the direction anyone could trade.
     → The feed now freezes the prediction at candle open. This engine
     treats `running_ticks` as DISPLAY-ONLY information (never part of
     the emitted signal).
  3. Same-anatomy detectors stacked votes (one long-wick candle counted
     as HAMMER + PIN_BAR + DRAGONFLY = 3 "theories"), manufacturing fake
     multi-strategy agreement. → detect_all() now dedups anatomy families
     and resolves context-twins (hammer/hanging-man) before returning.
  4. Nine detectors could never fire on the gapless 1-minute feed and
     continuation detectors contradicted the feed's own measured
     statistics. → patterns.py cleaned; continuation votes are gated by
     regime here.

═══════════════════════════════════════════════════════════════════════════
NEW ARCHITECTURE — six independent voters, one council decision:

  STAT      Empirical per-pair continuation rate (streak-bucket z-test)
            over the trailing 120 candles + archetype prior. The only
            measurable next-candle statistic on a near-memoryless feed.
  REGIME    Least-squares trend (R² + steepness). Follows CONFIRMED trends,
            fades exhausted ones, abstains on SIDEWAYS.
  POSITION  Position-aware read ("অবস্থান বোঝে"): where price sits inside
            its 40-candle range, how far it is stretched from SMA20, and
            whether the rejection wick is touching a real key level.
  PATTERN   Cleaned candlestick evidence (deduped, context-gated, regime-
            gated continuation family). ONE net vote, never a stack.
  STATE     Market-state read (CONTINUATION / EXHAUSTION / REVERSAL / TRAP)
            with conviction.
  FLOW      Closed-candle tick absorption (buyer/seller pressure vs close
            position). Only present when >= 15 real ticks exist.

EMISSION RULES (ALL must pass — else NEUTRAL, no exceptions):

  R1  |net_score| >= SCORE_FLOOR          (weighted conviction exists)
  R2  agree >= MIN_AGREE                  (several strategies agree —
                                           the user's explicit requirement:
                                           "High confidence বেশ কয়েকটি
                                           স্ট্রাটেজি এক মত হতে হবে")
  R3  agree_weight >= WEIGHT_FLOOR        (the agreement has weight)
  R4  no veto                             (no voter of weight >= VETO_WEIGHT
                                           points the other way)
  R5  liquidity gates pass                (ATR floor, session quality)

Output schema (consumed by feed.py + chart UI):
  { signal: "CALL"|"PUT"|"NEUTRAL", score, confidence, agree, agree_weight,
    strength: "STRONG"|"MEDIUM"|"NONE", reasons, patterns_fired, voters,
    confluence, key_levels, wick_walls, regime, market_state, context }
"""
from __future__ import annotations
import os
import time
from dataclasses import dataclass

from .patterns import (
    Signal, detect_all, candle_anatomy, FAMILY_OF,
)
from .pair_profiles import (
    get_profile, session_quality, PairProfile,
)


# ── Council thresholds (env-tunable so live ops can tighten/loosen) ──────────

MIN_AGREE      = int(os.environ.get("QX_MIN_AGREE", "3"))
SCORE_FLOOR    = float(os.environ.get("QX_SCORE_FLOOR", "4.0"))
WEIGHT_FLOOR   = float(os.environ.get("QX_WEIGHT_FLOOR", "4.0"))
VETO_WEIGHT    = float(os.environ.get("QX_VETO_WEIGHT", "2.5"))
REQUIRE_STAT   = os.environ.get("QX_REQUIRE_STAT", "1") == "1"
MIN_CANDLES    = int(os.environ.get("QX_MIN_CANDLES", "25"))
STRONG_AGREE   = int(os.environ.get("QX_STRONG_AGREE", "4"))
STRONG_SCORE   = float(os.environ.get("QX_STRONG_SCORE", "5.0"))


# ── Key-level / context helpers ──────────────────────────────────────────────

def _key_levels(candles: list[dict], lookback: int = 40) -> list[tuple[float, int]]:
    """Swing high/low clustering. Returns [(price, touches), ...] for levels
    with 2+ touches. Zero-range (synthetic flat) candles are skipped — their
    high==low is a fake level that pollutes clustering."""
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    recent = [c for c in recent if c["high"] > c["low"]]
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
    """Wick-tip clustering → (support walls, resistance walls, avg range)."""
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    recent = [c for c in recent if c["high"] > c["low"]]
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
    """Proximity to a round number. Returns (level, strength)."""
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


def _market_regime(candles: list[dict], lookback: int = 24
                   ) -> tuple[str, str, float]:
    """UPTREND / DOWNTREND / SIDEWAYS + SUPPORT / RESISTANCE / NEUTRAL zone.

    Least-squares line over the last `lookback` closes; a trend requires
    BOTH R² >= 0.25 (straightness) and |slope| >= 0.12 ATR/candle (steepness).
    Everything else is SIDEWAYS — the honest default on this feed.
    Also returns R² so the REGIME voter can scale conviction.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if len(recent) < 8:
        return "SIDEWAYS", "NEUTRAL", 0.0
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
    atr = sum(c["high"] - c["low"] for c in recent if c["high"] > c["low"])
    atr_n = max(1, sum(1 for c in recent if c["high"] > c["low"]))
    atr = atr / atr_n if atr_n else 0.0
    slope_atr = slope / atr if atr > 0 else 0.0
    if r2 >= 0.25 and abs(slope_atr) >= 0.12:
        regime = "UPTREND" if slope > 0 else "DOWNTREND"
    else:
        regime = "SIDEWAYS"
    full_hi = max(x["high"] for x in recent)
    full_lo = min(x["low"] for x in recent)
    rng = full_hi - full_lo
    if rng == 0:
        return regime, "NEUTRAL", r2
    pos = (candles[-1]["close"] - full_lo) / rng
    if pos <= 0.25:
        zone = "SUPPORT"
    elif pos >= 0.75:
        zone = "RESISTANCE"
    else:
        zone = "NEUTRAL"
    return regime, zone, r2


# ── Candle colour / streak helpers ───────────────────────────────────────────

def _candle_color(c: dict) -> int:
    """+1 bull, -1 bear, 0 doji (close == open)."""
    if c["close"] > c["open"]:
        return 1
    if c["close"] < c["open"]:
        return -1
    return 0


def _current_streak(candles: list[dict]) -> int:
    """Length of the same-color run ENDING at the last candle.
    Dojis are skipped (they neither extend nor break a directional run)."""
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
    capped at 3."""
    start = max(1, len(candles) - window)
    agg = [0, 0]
    buckets: dict[int, list[int]] = {1: [0, 0], 2: [0, 0], 3: [0, 0]}
    for i in range(start, len(candles)):
        pc = _candle_color(candles[i - 1])
        cc = _candle_color(candles[i])
        if pc == 0 or cc == 0:
            continue
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


# ── VOTER 1: STAT — empirical continuation / fade ────────────────────────────

def _stat_bias(candles: list[dict], profile: PairProfile) -> tuple[int, float, list[str], dict]:
    """VOTER 1 — measured statistical bias. Returns (dir, weight, reasons, meta).

    dir: -1/0/+1 (bet: fade or follow the last candle's colour).
    weight in [0..3]; 0 = abstain (no measurable edge).

    1. Empirical continuation rate conditioned on the current run length
       (z-test, weight scales with |z|).
    2. Aggregate continuation rate (higher bar).
    3. Archetype prior when the pair's own history is inconclusive:
       fade on RANGE_BOUNCE / MEAN_REVERT, weak follow on OTC TREND pegs
       (documented to exhaust in 2-3 candles), follow on real TREND majors.
    Streak exhaustion and SMA extension are POSITION voter's job — this
    voter ONLY answers "does this pair's next candle repeat the colour?".
    """
    cur = candles[-1]
    col = _candle_color(cur)
    streak = _current_streak(candles)
    meta: dict = {"streak": streak, "p_cont": None, "bias_src": None}

    if col == 0:
        return 0, 0.0, [], meta

    agg, buckets = _continuation_table(candles)
    n_agg = agg[1]

    # Archetype prior (direction of the follow/fade bet)
    if profile.archetype == "TREND":
        prior = 1.0 if not profile.asset.endswith("_otc") else 0.6
    elif profile.archetype == "RANGE_BOUNCE":
        prior = -1.0
    elif profile.archetype == "MEAN_REVERT":
        prior = -1.2
    else:  # MIXED
        prior = -0.4 if profile.asset.endswith("_otc") else 0.0

    follow = 0.0
    src = None

    # 1a. streak-conditional empirical estimate (most specific).
    b = min(max(streak, 1), 3)
    bc, bt = buckets[b]
    if bt >= 25:
        z_b = (bc - bt / 2.0) * 2.0 / (bt ** 0.5)
        _bar = 1.6 if abs(prior) >= 0.5 else 1.0
        if abs(z_b) >= _bar:
            follow = max(-2.0, min(2.0, z_b * 0.7))
            src = (f"streak-{b} empirical: continuation "
                   f"{bc}/{bt} ({bc / bt:.0%})")

    # 1b. aggregate empirical estimate (same override logic, higher bar).
    if follow == 0.0 and n_agg >= 60:
        z_a = (agg[0] - n_agg / 2.0) * 2.0 / (n_agg ** 0.5)
        _bar = 1.8 if abs(prior) >= 0.5 else 1.2
        if abs(z_a) >= _bar:
            follow = max(-1.8, min(1.8, z_a * 0.6))
            src = f"empirical p_cont={agg[0] / n_agg:.0%} (n={n_agg})"

    # 1c. fall back to the archetype prior
    if follow == 0.0:
        follow = prior
        src = f"archetype prior ({profile.archetype})"

    meta["bias_src"] = src
    meta["p_cont"] = round(0.5 + follow / 4.4, 3)

    weight = min(3.0, abs(follow))
    if weight < 0.4:
        return 0, 0.0, [], meta

    direction = 1 if follow > 0 else -1        # +1 = bet colour repeats
    vote_dir = direction * col                  # actual CALL/PUT direction
    reason = (f"{src} -> {'follow' if direction > 0 else 'fade'} the "
              f"{'bull' if col > 0 else 'bear'} candle -> "
              f"{'CALL' if vote_dir > 0 else 'PUT'}")
    return vote_dir, weight, [f"STAT    {reason}"], meta


# ── VOTER 2: REGIME — confirmed trend follow / exhaustion fade ───────────────

def _regime_vote(candles: list[dict], regime: str, r2: float,
                 profile: PairProfile) -> tuple[int, float, list[str]]:
    """VOTER 2 — trend regime. Returns (dir, weight, reasons).

    MEASURED (backtest attribution, 5206-signal portfolio): naive confirmed-
    trend following graded 47.7% — an ANTI-SIGNAL. By the time R²+steepness
    confirm a 1-minute trend, the move is largely spent. The voter therefore:

      - follows ONLY very strong trends (R² >= 0.40, i.e. the top slice of
        confirmations) and with a capped weight (1.6);
      - fades the trend once a run of 3+ same-colour candles has printed
        INSIDE it (the measured "run 2-3 then fade" OTC behaviour) — the
        exhaustion fade is the regime voter's real edge;
      - abstains on SIDEWAYS (honest default).
    """
    streak = _current_streak(candles)
    if regime == "SIDEWAYS":
        return 0, 0.0, []
    tdir = 1 if regime == "UPTREND" else -1

    # Exhaustion fade: 3+ same-colour candles inside a confirmed trend.
    col = _candle_color(candles[-1])
    if col != 0 and streak >= 3 and (col == tdir):
        w = min(2.4, 1.2 + (streak - 2) * 0.4)
        return (-tdir, w,
                [f"REGIME  {regime} confirmed (R²={r2:.2f}) but "
                 f"{streak}-candle run is exhausted -> fade to "
                 f"{'CALL' if -tdir > 0 else 'PUT'}"])

    # Follow only top-slice confirmations, capped weight.
    if r2 >= 0.40:
        w = min(1.6, 0.8 + (r2 - 0.40) * 3.0)
        return (tdir, w,
                [f"REGIME  {regime} strong (R²={r2:.2f}) -> "
                 f"{'CALL' if tdir > 0 else 'PUT'} continuation"])
    # Confirmed but unremarkable trend — measured <50% to follow. Abstain.
    return 0, 0.0, []


# ── VOTER 3: POSITION — where price IS (the অবস্থান voter) ───────────────────

def _position_vote(candles: list[dict], atr: float, klevels,
                   sup_walls, res_walls
                   ) -> tuple[int, float, list[str]]:
    """VOTER 3 — position-aware mean reversion & level rejection.

    Three independent positional facts, combined into ONE vote:
      A. Range position — where the close sits inside the 40-candle range.
         Extremes (>= 85% / <= 15%) favour a snap back toward the middle.
      B. SMA20 extension — |z| >= 1.2 ATR from the mean favours reversion.
      C. Key-level rejection — the candle's rejection wick touched a
         clustered swing level (2+ touches) or a wick wall.

    Abstains unless at least one fact is measurable. This is the voter that
    makes the engine "understand the position" — the same candle pattern at
    range-bottom means reversal, mid-range means nothing, range-top means
    fade. Weight caps at 3.
    """
    cur = candles[-1]
    a = candle_anatomy(cur)
    score = 0.0
    parts: list[str] = []

    # A. Range position over the last 40 candles (zero-range candles skipped
    # for the high/low window — synthetic flat bars must not shrink it).
    win = [c for c in candles[-40:] if c["high"] > c["low"]]
    if len(win) >= 10:
        hi = max(c["high"] for c in win)
        lo = min(c["low"] for c in win)
        rng = hi - lo
        if rng > 0:
            pos = (cur["close"] - lo) / rng
            if pos >= 0.85:
                w = min(2.0, 1.0 + (pos - 0.85) * 6.0)
                score -= w
                parts.append(f"at {pos:.0%} of 40-candle range (top) -> "
                             f"PUT snap-back")
            elif pos <= 0.15:
                w = min(2.0, 1.0 + (0.15 - pos) * 6.0)
                score += w
                parts.append(f"at {pos:.0%} of 40-candle range (bottom) -> "
                             f"CALL snap-back")

    # B. SMA20 extension (only measurable with a real ATR)
    if atr > 0 and len(candles) >= 20:
        sma = sum(c["close"] for c in candles[-20:]) / 20
        z = (cur["close"] - sma) / atr
        if z >= 1.2:
            score -= min(2.0, 1.0 + (z - 1.2) * 0.5)
            parts.append(f"{z:+.1f} ATR above SMA20 (stretched) -> PUT")
        elif z <= -1.2:
            score += min(2.0, 1.0 + (-1.2 - z) * 0.5)
            parts.append(f"{z:+.1f} ATR below SMA20 (stretched) -> CALL")

    # C. Key-level rejection via the candle's own rejection wick
    if atr > 0:
        test_low = a["lower"] > a["upper"]
        test_price = cur["low"] if test_low else cur["high"]
        rej_dir = 1 if test_low else -1        # wick rejected downward → CALL side
        rej_lbl = "CALL" if rej_dir > 0 else "PUT"
        for lvl, touches in klevels:
            if abs(test_price - lvl) <= lvl * 0.0006 and touches >= 2:
                score += rej_dir * min(2.5, 0.8 + touches * 0.4)
                parts.append(f"rejection wick at key level x{touches} "
                             f"({lvl:.5f}) -> {rej_lbl}")
                break
        else:
            walls = sup_walls if test_low else res_walls
            for lvl, weight in walls:
                if abs(test_price - lvl) <= atr * 0.25 and weight >= 2.5:
                    score += rej_dir * min(2.0, 0.6 + weight * 0.3)
                    parts.append(f"rejection wick at wick wall "
                                 f"({lvl:.5f}) -> {rej_lbl}")
                    break

    if not parts or abs(score) < 0.8:
        return 0, 0.0, []
    direction = 1 if score > 0 else -1
    weight = min(3.0, abs(score))
    reason = "; ".join(parts)
    return (direction, weight,
            [f"POS     {reason} -> {'CALL' if direction > 0 else 'PUT'}"])


# ── VOTER 5: STATE — market-state conviction ────────────────────────────────

def _market_state(candles: list[dict], regime: str, zone: str,
                  klevels, atr: float, ticks: list[float] | None = None,
                  profile: PairProfile | None = None
                  ) -> dict:
    """Identify CONTINUATION / EXHAUSTION / REVERSAL / TRAP / RANGE / UNCLEAR.
    Returns {state, bias, conviction, points}. The STATE voter consumes it."""
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
    if a["brr"] < 0.05:
        cand_dir = 0
    else:
        cand_dir = +1 if a["is_bull"] else -1

    # Streak (dojis skipped — consistent with _current_streak)
    streak = 0
    if cand_dir != 0:
        for i in range(len(candles) - 1, -1, -1):
            cc = _candle_color(candles[i])
            if cc == 0:
                if i == len(candles) - 1:
                    break
                continue
            if cc == cand_dir:
                streak += 1
            else:
                break

    pts = {"CONTINUATION": 0.0, "EXHAUSTION": 0.0,
           "REVERSAL": 0.0, "TRAP": 0.0, "RANGE": 0.0}
    dirs = {k: 0.0 for k in pts}

    def add(state, p, d):
        pts[state] += p
        dirs[state] += d * p

    if trend_dir:
        add("CONTINUATION", 2, trend_dir)
        if cand_dir == trend_dir and a["brr"] >= 0.55:
            add("CONTINUATION", 2, trend_dir)

    if streak >= exh_streak and cand_dir != 0:
        add("EXHAUSTION", 2 + (1 if streak >= exh_streak + 2 else 0), -cand_dir)
    if ticks and len(ticks) >= 15:
        bp, _ = _tick_pressure(ticks)
        if bp >= 0.78 or bp <= 0.22:
            add("EXHAUSTION", 1, -cand_dir)

    if a["range"] > 1e-9 and a["upper"] / a["range"] > 0.55 and a["brr"] < 0.25:
        add("REVERSAL", 3 if zone == "RESISTANCE" else 2, -1)
    elif a["range"] > 1e-9 and a["lower"] / a["range"] > 0.55 and a["brr"] < 0.25:
        add("REVERSAL", 3 if zone == "SUPPORT" else 2, +1)

    if ticks and len(ticks) >= 15:
        abs_dir, _ = _absorption(cur, ticks)
        if abs_dir:
            add("REVERSAL", 3, abs_dir)

    if len(candles) >= 2:
        prev = candles[-2]
        for lvl, t in klevels:
            if prev["close"] > lvl >= cur["close"]:
                add("TRAP", 2, -1)
                break
            if prev["close"] < lvl <= cur["close"]:
                add("TRAP", 2, +1)
                break

    if trend_dir == 0:
        add("RANGE", 2, 0)
        if zone == "RESISTANCE":
            add("RANGE", 1, -1)
        elif zone == "SUPPORT":
            add("RANGE", 1, +1)
    if a["brr"] <= 0.10:
        add("RANGE", 1, 0)

    prio = ["TRAP", "REVERSAL", "EXHAUSTION", "CONTINUATION", "RANGE"]
    win = max(prio, key=lambda k: (pts[k], -prio.index(k)))
    tot = sum(pts.values())
    if pts[win] < 3 or tot == 0:
        return {"state": "UNCLEAR", "bias": "NEUTRAL", "conviction": 0,
                "points": {k: round(v, 1) for k, v in pts.items()}}
    bd = dirs[win]
    bias = "CALL" if bd > 0 else "PUT" if bd < 0 else "NEUTRAL"
    # Honest conviction: winner's share of (all points + 2 prior). A single
    # state with no competition must NOT read 100% — the +2 denominator
    # keeps the number monotonic and below certainty.
    conv = round(100 * pts[win] / (tot + 2.0))
    return {"state": win, "bias": bias, "conviction": conv,
            "points": {k: round(v, 1) for k, v in pts.items()}}


def _state_vote(ms: dict) -> tuple[int, float, list[str]]:
    """VOTER 5 — market-state conviction → one vote."""
    if ms.get("bias") not in ("CALL", "PUT") or ms.get("conviction", 0) < 40:
        return 0, 0.0, []
    direction = 1 if ms["bias"] == "CALL" else -1
    weight = min(2.6, 1.0 + ms["conviction"] / 45.0)
    return (direction, weight,
            [f"STATE   {ms['state']} @ {ms['conviction']}% conviction -> "
             f"{ms['bias']}"])


# ── VOTER 6: FLOW — closed-candle tick absorption ────────────────────────────

def _tick_pressure(ticks: list[float]) -> tuple[float, str]:
    """Return (buyer_pct, classification) from a list of tick prices."""
    if not ticks or len(ticks) < 5:
        return 0.5, "INSUFFICIENT"
    up = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i - 1])
    dn = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i - 1])
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
    """Absorption: candle closed one way but ticks pushed the other.
    Returns (direction, reason) — +1 = CALL, -1 = PUT, 0 = none.

    Grading note: `ticks` here are the JUST-CLOSED candle's ticks. The vote
    is formed at candle-open time for the NEXT candle, so this is legitimate
    closed information — no lookahead."""
    if not ticks or len(ticks) < 15:
        return 0, ""
    a = candle_anatomy(cur)
    bp, _ = _tick_pressure(ticks)
    if a["close_pos"] >= 0.72 and bp <= 0.40:
        return -1, (f"closed up at {a['close_pos']:.0%} but only "
                    f"{bp:.0%} up-ticks -> sellers absorbed")
    if a["close_pos"] <= 0.20 and bp >= 0.60:
        return +1, (f"closed down at {a['close_pos']:.0%} but "
                    f"{bp:.0%} up-ticks -> buyers absorbed")
    return 0, ""


def _flow_vote(cur: dict, ticks: list[float] | None) -> tuple[int, float, list[str]]:
    """VOTER 6 — absorption from the just-closed candle's ticks."""
    if not ticks or len(ticks) < 15:
        return 0, 0.0, []
    direction, reason = _absorption(cur, ticks)
    if not direction:
        return 0, 0.0, []
    return (direction, 2.0,
            [f"FLOW    ABSORPTION: {reason} -> "
             f"{'CALL' if direction > 0 else 'PUT'}"])


# ── VOTER 4: PATTERN — cleaned candlestick evidence ──────────────────────────

_MOMENTUM_FAMILY = {
    "MARUBOZU", "BELT_HOLD_BULL", "BELT_HOLD_BEAR",
    "THREE_WHITE_SOLDIERS", "THREE_BLACK_CROWS",
    "SEPARATING_LINES_BULL", "SEPARATING_LINES_BEAR", "ON_NECK",
    "RISING_THREE_METHODS", "FALLING_THREE_METHODS",
}

_WICK_REV_FAMILY = {
    "PIN_BAR_BULL", "PIN_BAR_BEAR", "HAMMER", "HANGING_MAN",
    "SHOOTING_STAR", "INVERTED_HAMMER", "DRAGONFLY_DOJI", "GRAVESTONE_DOJI",
}


def _trap_wick_dampen(cur: dict, profile: PairProfile, atr: float) -> float:
    """0..1 multiplier dampening wick-rejection votes when the closed candle
    looks like an engineered trap wick (extreme wick, small body, closed far
    from the wick tip)."""
    if atr <= 0 or profile.trap_wick_sensitivity <= 1.0:
        return 1.0
    a = candle_anatomy(cur)
    if a["body"] == 0:
        return 1.0
    max_wick = max(a["upper"], a["lower"])
    if max_wick <= 2.5 * a["body"]:
        return 1.0
    ratio = max_wick / a["body"]
    if a["range"] < atr * 0.6:
        return 1.0
    return max(0.3, 1.0 - (ratio - 2.5) * 0.12 * profile.trap_wick_sensitivity)


def _pattern_vote(candles: list[dict], profile: PairProfile, regime: str,
                  zone: str, atr: float, sess_q: float, atr_floor: float,
                  trap_mult: float, muted: dict[str, str] | None) -> tuple[
        int, float, list[str], list[str], list[Signal]]:
    """VOTER 4 — the candlestick layer as ONE vote (never a stack).

    detect_all() already dedups same-anatomy families and resolves the
    hammer/hanging-man context twins. Remaining gates here:
      - per-pair strategy weights
      - continuation-family regime gate (momentum votes without regime
        support measured 47-48% live — an anti-signal)
      - trap-wick dampener for the wick-reversal family
      - session quality + ATR floor
    Returns (dir, weight, reasons, patterns_fired, fired_signals).
    """
    fired = detect_all(candles)
    if muted:
        fired = [s for s in fired if s.name not in muted]

    patterns_fired: list[str] = []
    net = 0.0
    reasons: list[str] = []

    for sig in fired:
        patterns_fired.append(sig.name)
        w = profile.strategy_weights.get(sig.name, 1.0)
        if sig.name in _MOMENTUM_FAMILY:
            if regime == "SIDEWAYS":
                w *= 0.15 if profile.archetype in (
                    "RANGE_BOUNCE", "MEAN_REVERT", "MIXED") else 0.35
            elif (regime == "UPTREND" and sig.direction < 0) or \
                    (regime == "DOWNTREND" and sig.direction > 0):
                w *= 0.40
            else:
                w *= {"TREND": 0.85, "MIXED": 0.55,
                      "RANGE_BOUNCE": 0.35, "MEAN_REVERT": 0.30
                      }.get(profile.archetype, 0.55)
        if sig.name in _WICK_REV_FAMILY:
            w *= trap_mult
        w *= sess_q
        w *= atr_floor
        mag = sig.strength * 4 * w
        if sig.direction == 0 or mag < 0.35:
            continue
        net += sig.direction * mag
        reasons.append(
            f"{sig.name}  {sig.reason} (w{mag:.1f})")

    if abs(net) < 1.0:
        return 0, 0.0, reasons, patterns_fired, fired
    direction = 1 if net > 0 else -1
    weight = min(3.0, abs(net) / 1.5)
    reasons.append(
        f"PATTERN net {net:+.1f} across {len(patterns_fired)} detector(s) -> "
        f"{'CALL' if direction > 0 else 'PUT'}")
    return direction, weight, reasons, patterns_fired, fired


# ── Council decision ─────────────────────────────────────────────────────────

@dataclass
class _Voter:
    name: str
    direction: int
    weight: float
    reasons: list[str]


def _decide(voters: list[_Voter]) -> tuple[str, int, float, float, list[str]]:
    """Apply the emission rules. Returns (signal, agree, agree_weight,
    net_score, notes). NO fallback of any kind: when the rules fail the
    answer is NEUTRAL.

    RULE R6 (STAT ANCHOR — grid-searched on 46k candles, +2pp on every
    bucket): the STAT voter — the measured statistical layer — must be
    AMONG the agreeing voters. Candlestick/microstructure agreement alone
    measured ~51.4% (below the 52.08% break-even at 92% payout); with the
    statistical edge anchored in, the same candles graded 53.5%. When STAT
    abstains, the council does not trade — there is no measured edge to
    agree WITH."""
    net = sum(v.direction * v.weight for v in voters)
    if net == 0:
        return "NEUTRAL", 0, 0.0, 0.0, ["no voter cast a directional vote"]
    candidate = 1 if net > 0 else -1
    agreeing = [v for v in voters if v.direction == candidate]
    opposing = [v for v in voters if v.direction == -candidate]
    agree = len(agreeing)
    agree_weight = sum(v.weight for v in agreeing)
    notes: list[str] = []

    if abs(net) < SCORE_FLOOR:
        notes.append(f"|net|={abs(net):.1f} < {SCORE_FLOOR} — conviction too low")
    if agree < MIN_AGREE:
        notes.append(f"only {agree} voter(s) agree (need {MIN_AGREE})")
    if agree_weight < WEIGHT_FLOOR:
        notes.append(f"agree weight {agree_weight:.1f} < {WEIGHT_FLOOR}")
    if any(v.weight >= VETO_WEIGHT for v in opposing):
        notes.append("VETO: strong opposing voter")
    if REQUIRE_STAT and "STAT" not in {v.name for v in agreeing}:
        notes.append("no STAT anchor — the measured statistical layer "
                     "did not join the agreement")
    if notes:
        return "NEUTRAL", agree, agree_weight, net, notes
    return "CALL" if candidate > 0 else "PUT", agree, agree_weight, net, notes


# ── Public entry: run_strategy ───────────────────────────────────────────────

def run_strategy(candles: list[dict],
                 asset: str,
                 period: int = 60,
                 ticks: list[float] | None = None,
                 running_ticks: list[float] | None = None,
                 muted: dict[str, str] | None = None,
                 chop_zone: tuple[str, str] | None = None) -> dict:
    """Compute the next-candle signal for `asset` via the confluence council.

    `ticks` = the JUST-CLOSED candle's ticks (legitimate closed information).
    `running_ticks` = the CURRENTLY OPEN candle's ticks. They NEVER influence
    the emitted signal — the feed may pass them for display-only live views,
    but the council deliberately ignores them so the graded signal is exactly
    what was broadcast at candle open.

    Returns the standard signal dict. "NEUTRAL" means NO TRADE — the feed
    emits, logs and grades nothing for it.
    """
    if not candles or len(candles) < MIN_CANDLES:
        return _neutral_signal(
            f"warming up ({len(candles)}/{MIN_CANDLES} candles)",
            {"phase": "warmup"})

    cur = candles[-1]

    # A zero-range candle (synthetic flat bar or totally dead minute) carries
    # zero information — every pattern on it is an artefact.
    if cur["high"] <= cur["low"]:
        return _neutral_signal("zero-range candle (no market data)",
                               {"phase": "no_data"})

    profile = get_profile(asset)

    # Context
    klevels = _key_levels(candles)
    sup_walls, res_walls, atr10 = _wick_walls(candles)
    atr = atr10 or (cur["high"] - cur["low"]) or 0.0001
    regime, zone, r2 = _market_regime(candles)
    trap_mult = _trap_wick_dampen(cur, profile, atr)

    # Liquidity gates
    cur_atr_pct = atr / cur["close"] if cur["close"] else 0
    atr_floor_mult = min(1.0, max(0.3, cur_atr_pct / max(profile.min_atr_pct, 1e-9)))
    _candle_time = cur.get("time") if isinstance(cur, dict) else None
    hour_utc = (time.gmtime(_candle_time).tm_hour
                if _candle_time else time.gmtime().tm_hour)
    sess_q = session_quality(profile, hour_utc)

    gate_notes: list[str] = []
    if atr_floor_mult < 0.5:
        gate_notes.append(
            f"ATR {cur_atr_pct:.4%} below pair floor "
            f"{profile.min_atr_pct:.4%} — market too quiet to trade")
    if sess_q < 0.5:
        gate_notes.append(
            f"dead session (UTC {hour_utc:02d}h, quality {sess_q:.2f}) — "
            f"standing down")

    # ── Convene the council ───────────────────────────────────────────────
    voters: list[_Voter] = []

    s_dir, s_w, s_reasons, s_meta = _stat_bias(candles, profile)
    if s_dir:
        voters.append(_Voter("STAT", s_dir, s_w, s_reasons))

    r_dir, r_w, r_reasons = _regime_vote(candles, regime, r2, profile)
    if r_dir:
        voters.append(_Voter("REGIME", r_dir, r_w, r_reasons))

    p_dir, p_w, p_reasons = _position_vote(candles, atr, klevels,
                                           sup_walls, res_walls)
    if p_dir:
        voters.append(_Voter("POSITION", p_dir, p_w, p_reasons))

    pat_dir, pat_w, pat_reasons, patterns_fired, fired = _pattern_vote(
        candles, profile, regime, zone, atr, sess_q, atr_floor_mult,
        trap_mult, muted)
    if pat_dir:
        voters.append(_Voter("PATTERN", pat_dir, pat_w, pat_reasons))

    ms = _market_state(candles, regime, zone, klevels, atr, ticks,
                       profile=profile)
    st_dir, st_w, st_reasons = _state_vote(ms)
    if st_dir:
        voters.append(_Voter("STATE", st_dir, st_w, st_reasons))

    f_dir, f_w, f_reasons = _flow_vote(cur, ticks)
    if f_dir:
        voters.append(_Voter("FLOW", f_dir, f_w, f_reasons))

    # ── Decision ──────────────────────────────────────────────────────────
    signal, agree, agree_weight, net_score, notes = _decide(voters)

    # Liquidity gates veto the whole council (they are facts about
    # tradability, not opinion).
    if gate_notes and signal != "NEUTRAL":
        signal = "NEUTRAL"
        notes.extend(gate_notes)

    # Chop guard (passed by the feed from its zone-loss streak): the exact
    # (regime, zone) has proven unreadable — stand down here too.
    if chop_zone is not None and signal != "NEUTRAL":
        _reg = (regime, zone)
        if _reg == chop_zone:
            signal = "NEUTRAL"
            notes.append("chop guard: this zone is on a loss streak")

    # ── Strength calibration ──────────────────────────────────────────────
    opposing = [v for v in voters
                if signal in ("CALL", "PUT")
                and v.direction == (-1 if signal == "CALL" else 1)]
    if signal == "NEUTRAL":
        strength = "NONE"
    elif (agree >= STRONG_AGREE and abs(net_score) >= STRONG_SCORE
            and not opposing):
        strength = "STRONG"
    else:
        strength = "MEDIUM"

    # Honest confidence: agreement-driven, capped, never manufactured.
    n_active = len(voters)
    conf = 0.0 if signal == "NEUTRAL" else round(
        min(0.95, 0.42 + 0.055 * agree_weight + 0.02 * agree), 2)

    # Reasons: voter lines + decision notes
    reasons: list[str] = []
    for v in voters:
        reasons.extend(v.reasons)
    reasons.extend(pat_reasons if not pat_dir else [])
    if signal == "NEUTRAL":
        for n in notes:
            reasons.append(f"NO-TRADE  {n}")
    else:
        reasons.append(
            f"COUNCIL  {agree}/{n_active} voters agree "
            f"(weight {agree_weight:.1f}, net {net_score:+.1f}) -> "
            f"{signal} {strength}")

    return {
        "signal": signal,
        "score": int(round(max(-9.9, min(9.9, net_score)))),
        "confidence": conf,
        "agree": agree,
        "agree_weight": round(agree_weight, 2),
        "strength": strength,
        "reasons": reasons,
        "patterns_fired": patterns_fired,
        "voters": [{"name": v.name, "dir": v.direction,
                    "weight": round(v.weight, 2)} for v in voters],
        "confluence": {
            "min_agree": MIN_AGREE,
            "score_floor": SCORE_FLOOR,
            "weight_floor": WEIGHT_FLOOR,
            "veto_weight": VETO_WEIGHT,
            "net_score": round(net_score, 2),
            "emitted": signal != "NEUTRAL",
            "blocked_by": notes,
        },
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
        "stat_meta": s_meta,
        "context": {
            "archetype": profile.archetype,
            "session_quality": round(sess_q, 2),
            "trap_wick_dampen": round(trap_mult, 2),
            "atr_pct": round(cur_atr_pct, 6),
            "min_atr_pct": profile.min_atr_pct,
            "pair_notes": profile.notes,
            "voters_active": n_active,
        },
    }


def _neutral_signal(reason: str, extra: dict | None = None) -> dict:
    """NO TRADE. NEUTRAL is a first-class output: nothing is emitted,
    logged or graded for it. There is deliberately NO direction here."""
    return {
        "signal": "NEUTRAL",
        "score": 0,
        "confidence": 0.0,
        "agree": 0,
        "agree_weight": 0.0,
        "strength": "NONE",
        "reasons": [f"NO-TRADE  {reason}"],
        "patterns_fired": [],
        "voters": [],
        "confluence": {"emitted": False, "blocked_by": [reason]},
        "key_levels": [],
        "wick_walls": {"support": [], "resistance": []},
        "regime": {"trend": "SIDEWAYS", "zone": "NEUTRAL"},
        "market_state": {"state": "UNCLEAR", "bias": "NEUTRAL",
                         "conviction": 0, "points": {}},
        "context": extra or {},
    }
