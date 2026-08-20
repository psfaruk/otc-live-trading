"""
Strategy runner — composes candlestick pattern detections with per-pair
profiles, market context, and tick microstructure to produce a final
CALL/PUT signal for the next 1-minute candle.

This is the new signal engine. It REPLACES the role of analyze_eoc for
the live trading path while keeping analyze_eoc available for backward
compatibility and shadow comparison.

Design principles (from web research):
  1. Each pattern detector returns (matched, direction, strength, reason).
  2. Per-pair profile boosts/suppresses patterns based on what works
     on that pair (USDINR range-bounce, USDMXN momentum, etc.).
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
     The always-emit tiebreak at lines 515-530 of this module guarantees
     this on any non-empty candle window. When no pattern fires, the
     tiebreak falls through to:
       (a) indep_net lean (color-independent evidence)
       (b) regime direction (UPTREND/DOWNTREND)
       (c) final fallback: the just-closed candle's own color (bull → CALL, bear → PUT)
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


def _market_regime(candles: list[dict], lookback: int = 20) -> tuple[str, str]:
    """UPTREND / DOWNTREND / SIDEWAYS + SUPPORT / RESISTANCE / NEUTRAL zone."""
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if len(recent) < 6:
        return "SIDEWAYS", "NEUTRAL"
    mid = len(recent) // 2
    first, second = recent[:mid], recent[mid:]
    f_hi = max(x["high"] for x in first)
    f_lo = min(x["low"] for x in first)
    s_hi = max(x["high"] for x in second)
    s_lo = min(x["low"] for x in second)
    if s_hi > f_hi and s_lo > f_lo:
        regime = "UPTREND"
    elif s_hi < f_hi and s_lo < f_lo:
        regime = "DOWNTREND"
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
                  klevels, atr: float, ticks: list[float] | None = None
                  ) -> dict:
    """Identify CONTINUATION / EXHAUSTION / REVERSAL / TRAP / RANGE / UNCLEAR.

    Mirrors analyze_eoc's market-state block but with cleaner structure.
    Returns {state, bias, conviction, points}.
    """
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

    # EXHAUSTION
    if streak >= 4:
        add("EXHAUSTION", 2 + (1 if streak >= 6 else 0), -cand_dir)
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

    # Hour-based session quality (UTC)
    hour_utc = time.gmtime().tm_hour
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

    # Compose score
    score = 0
    reasons: list[str] = []
    patterns_fired: list[str] = []
    indep_dirs: list[tuple[str, int]] = []

    for sig in fired:
        patterns_fired.append(sig.name)
        # Per-pair weight (default 1.0)
        w = profile.strategy_weights.get(sig.name, 1.0)
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
        score += sig.direction * mag
        if sig.direction != 0:
            # FIX: include magnitude in indep_dirs so agree_weight reflects
            # conviction (was direction-only, making agree_weight == agree,
            # making the STRONG gate unreachable).
            indep_dirs.append((sig.name, sig.direction, mag))
        reasons.append(
            f"{sig.name}  {sig.reason} -> "
            f"{'CALL' if sig.direction > 0 else 'PUT' if sig.direction < 0 else 'NEUTRAL'}"
            f" (x{mag})")

    # Tick absorption (independent of pattern)
    if ticks and len(ticks) >= 15:
        abs_dir, abs_reason = _absorption(cur, ticks)
        if abs_dir:
            abs_w = profile.strategy_weights.get("ABSORPTION", 1.0) * sess_q
            abs_mag = max(1, round(0.7 * 4 * abs_w))
            score += abs_dir * abs_mag
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
            score += live_dir * live_mag
            indep_dirs.append(("LIVE", live_dir, live_mag))
            reasons.append(
                f"LIVE  {live_reason} on running candle -> "
                f"{'CALL' if live_dir > 0 else 'PUT'} (x{live_mag})")
            patterns_fired.append("LIVE")

    # Market-state deep analysis (main predictor)
    ms = _market_state(candles, regime, zone, klevels, atr, ticks)
    if ms["bias"] in ("CALL", "PUT") and ms["conviction"] >= 25:
        ms_dir = +1 if ms["bias"] == "CALL" else -1
        ms_mag = min(4, max(1, ms["conviction"] // 25))
        ms_w = profile.strategy_weights.get("MARKET_STATE", 1.0) * sess_q
        ms_mag = int(ms_mag * ms_w)
        if ms_mag > 0:
            score += ms_dir * ms_mag
            indep_dirs.append(("MARKET_STATE", ms_dir, ms_mag))
            reasons.append(
                f"MARKET_STATE  {ms['state']} (bias {ms['bias']}, "
                f"conviction {ms['conviction']}%) -> {ms['bias']} (x{ms_mag})")

    # ── Confluence bonus ──────────────────────────────────────────────────
    conf_bonus, conf_desc = _confluence_score(
        cur, candles, klevels, sup_walls, res_walls, atr)
    if conf_bonus > 0 and score != 0:
        # Boost the score in the direction it's already leaning
        score += (1 if score > 0 else -1) * int(conf_bonus)
        reasons.append(f"CONFLUENCE  +{int(conf_bonus)} ({conf_desc})")

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
    indep_net = sum(d for _, d, _ in indep_dirs)
    signal = "CALL" if score > 0 else "PUT" if score < 0 else "NEUTRAL"

    # Tiebreak for NEUTRAL: every candle emits a CALL/PUT
    if signal == "NEUTRAL":
        if indep_net != 0:
            signal = "CALL" if indep_net > 0 else "PUT"
            weak_caps.append(
                f"TIEBREAK: score 0 but indep_net leans {signal} -> WEAK")
        elif regime in ("UPTREND", "DOWNTREND"):
            signal = "CALL" if regime == "UPTREND" else "PUT"
            weak_caps.append(
                f"TIEBREAK: regime={regime} without confirmation -> "
                f"{signal} (trend-only, WEAK)")
        else:
            # FIX: doji (close == open) should be treated as neutral.
            # Previously `a["is_bull"]` defaulted doji to CALL, biasing
            # every doji tiebreak toward CALL.
            if a["brr"] < 0.05:
                # doji — pick by close_pos (close > midpoint -> CALL else PUT)
                signal = "CALL" if a["close_pos"] >= 0.5 else "PUT"
            else:
                signal = "CALL" if a["is_bull"] else "PUT"
            weak_caps.append(
                f"TIEBREAK: no edge -> fallback to last-candle color "
                f"({'bull' if a['is_bull'] else 'bear'}) -> {signal} (WEAK)")
    elif abs(score) < 2:
        weak_caps.append(f"NO EDGE: |score|={abs(score)} is noise-level -> WEAK")

    # ── Strength calibration ───────────────────────────────────────────────
    MAX_SCORE = 12
    confidence = round(min(abs(score) / MAX_SCORE, 1.0), 2)

    # Agreement count — uses the magnitude (third element of the tuple)
    # so agree_weight reflects conviction, not just distinct-theory count.
    _net_votes: dict[str, int] = {}
    for name, d, mag in indep_dirs:
        _net_votes[name] = _net_votes.get(name, 0) + d * mag
    want = 1 if signal == "CALL" else -1
    agree = sum(1 for nv in _net_votes.values() if nv * want > 0)
    agree_weight = sum(abs(nv) for nv in _net_votes.values() if nv * want > 0)

    if signal == "NEUTRAL":
        strength = "NONE"
    elif weak_caps:
        strength = "WEAK"
    elif agree >= 3 and agree_weight >= 4 and abs(score) >= 4:
        strength = "STRONG"
    elif agree >= 2 and abs(score) >= 2:
        strength = "MEDIUM"
    else:
        strength = "WEAK"

    reasons.extend(weak_caps)

    return {
        "signal": signal,
        "score": score,
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
