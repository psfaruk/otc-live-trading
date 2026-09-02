"""
Candlestick pattern detection library (research-driven).

Each function returns: (matched: bool, direction: int, strength: float, reason: str)
  direction : +1 = CALL bias, -1 = PUT bias, 0 = neutral (pattern is indecision)
  strength  : 0..1 (relative conviction; how textbook the pattern looks)
  reason    : human-readable explanation

Thresholds are sourced from web research (Bulkowski's Encyclopedia,
Strike.money's 60-pattern guide, LuxAlgo, Candlescanner) and adapted
for the 1-minute OTC binary-options context — where reversal candles
require confluence at a key level to outperform coin-flip.

This module is intentionally PURE (no I/O, no DB, no async). The
runtime engine (analyze_eoc.py / strategies/runner.py) composes
these detectors and weights them with market context.
"""
from __future__ import annotations
from dataclasses import dataclass


# ── Anatomy helpers ──────────────────────────────────────────────────────────

def candle_anatomy(c: dict) -> dict:
    """Decompose one OHLC candle into its anatomy.

    Returns dict with body, upper_wick, lower_wick, total_range, brr (body
    to range ratio), is_bull, close_pos (0..1), open_pos (0..1).
    All ratios are protected against zero-range candles by flooring range
    to 1e-9 so callers never divide by zero.
    """
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    # Use max() instead of `or` to also catch negative (corrupted) ranges.
    rng = max(h - l, 1e-9)
    body = abs(cl - o)
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    return {
        "open": o, "high": h, "low": l, "close": cl,
        "body": body,
        "upper": upper,
        "lower": lower,
        "range": rng,
        "brr": body / rng,                    # body-to-range ratio
        "upper_ratio": upper / rng,            # upper wick share of range
        "lower_ratio": lower / rng,            # lower wick share of range
        # Doji (cl == o) must be NEUTRAL, not bull. Otherwise every bearish
        # reversal pattern (engulfing, dark_cloud_cover, evening_star, ...)
        # accepts a doji as the prior "green" candle and over-fires.
        "is_bull": cl > o,
        "close_pos": (cl - l) / rng,          # 0 = closed at low, 1 = at high
        "open_pos":  (o - l) / rng,
    }


# ── Single-candle detectors ──────────────────────────────────────────────────

@dataclass
class Signal:
    """A pattern-detector result."""
    matched: bool
    direction: int        # +1 CALL, -1 PUT, 0 neutral
    strength: float       # 0..1
    reason: str
    name: str = ""


def _ok(name: str, direction: int, strength: float, reason: str) -> Signal:
    return Signal(True, direction, strength, reason, name)


def _no() -> Signal:
    return Signal(False, 0, 0.0, "")


# ── Single-candle reversal patterns ───────────────────────────────────────────

def hammer(c: dict) -> Signal:
    """Hammer — bullish reversal.

    Anatomy: body <= 25% of range near the top; lower wick >= 50% of range
    (the textbook "2x body" guard breaks for tiny/doji bodies, so the test
    is switched to a range-relative threshold); little/no upper wick;
    close in top 30% of range.
    Win rate (research): ~60–65% at support, ~50% standalone.
    """
    a = candle_anatomy(c)
    # Don't reject doji outright — a perfect dragonfly (body=0) is still a
    # hammer-shaped candle. Use range-relative wick test instead of `2*body`.
    if (a["brr"] <= 0.25 and a["lower_ratio"] >= 0.50
            and a["upper_ratio"] <= 0.10 and a["close_pos"] >= 0.70):
        # Scale strength by lower-wick share of range (robust to body=0).
        s = 0.55 + min(0.20, max(0.0, a["lower_ratio"] - 0.50) * 0.40)
        return _ok("HAMMER", +1, min(0.75, s),
                   f"HAMMER: body {a['brr']:.0%} of range, lower wick "
                   f"{a['lower_ratio']:.0%} of range -> CALL reversal")
    return _no()


def hanging_man(c: dict) -> Signal:
    """Hanging Man — bearish reversal (hammer shape after an uptrend).

    Context (uptrend) is checked by the caller via confluence; this fn
    only validates anatomy. Mirror of hammer — uses the SAME range-relative
    wick threshold and the SAME strength scaling so the caller's
    "pick strongest signal" resolution is balanced (otherwise hammer-shape
    candles in an uptrend always win as CALL).
    """
    a = candle_anatomy(c)
    if (a["brr"] <= 0.25 and a["lower_ratio"] >= 0.50
            and a["upper_ratio"] <= 0.10 and a["close_pos"] >= 0.70):
        s = 0.55 + min(0.20, max(0.0, a["lower_ratio"] - 0.50) * 0.40)
        return _ok("HANGING_MAN", -1, min(0.75, s),
                   f"HANGING_MAN: hammer-shaped candle at top of up-move "
                   f"-> PUT reversal (needs uptrend context)")
    return _no()


def shooting_star(c: dict) -> Signal:
    """Shooting Star — bearish reversal.

    Body <= 25% of range near the bottom; upper wick >= 50% of range
    (range-relative, robust to tiny/doji bodies); little/no lower wick;
    close in bottom 30% of range.
    Win rate (research): ~57%.
    """
    a = candle_anatomy(c)
    if (a["brr"] <= 0.25 and a["upper_ratio"] >= 0.50
            and a["lower_ratio"] <= 0.10 and a["close_pos"] <= 0.30):
        s = 0.55 + min(0.20, max(0.0, a["upper_ratio"] - 0.50) * 0.40)
        return _ok("SHOOTING_STAR", -1, min(0.75, s),
                   f"SHOOTING_STAR: body {a['brr']:.0%}, upper wick "
                   f"{a['upper_ratio']:.0%} of range -> PUT reversal")
    return _no()


def inverted_hammer(c: dict) -> Signal:
    """Inverted Hammer — bullish reversal (after downtrend).

    Body <= 25% near low; upper wick >= 50% of range (range-relative,
    robust to tiny/doji bodies); little/no lower wick.
    Win rate: ~53–65% in confirmed downtrend.
    """
    a = candle_anatomy(c)
    if (a["brr"] <= 0.25 and a["upper_ratio"] >= 0.50
            and a["lower_ratio"] <= 0.10 and a["close_pos"] <= 0.30):
        # Mirror shooting_star's scaling so the caller's strongest-signal
        # resolution is balanced (otherwise shooting-star shapes always win).
        s = 0.55 + min(0.20, max(0.0, a["upper_ratio"] - 0.50) * 0.40)
        return _ok("INVERTED_HAMMER", +1, min(0.75, s),
                   "INVERTED_HAMMER: upper wick rejection at downtrend bottom "
                   "-> CALL reversal (needs downtrend context)")
    return _no()


def dragonfly_doji(c: dict) -> Signal:
    """Dragonfly Doji — bullish reversal.

    Body <= 10% of range (open≈close at top 10%), long lower wick >= 50%
    of range, no upper wick. Win rate: ~55–60%.
    """
    a = candle_anatomy(c)
    # Use range-relative wick threshold so a textbook dragonfly (body=0)
    # is RECOGNIZED rather than rejected by `2*body` degenerating to 0.
    if (a["brr"] <= 0.10 and a["lower_ratio"] >= 0.50
            and a["upper_ratio"] <= 0.05 and a["close_pos"] >= 0.90):
        return _ok("DRAGONFLY_DOJI", +1, 0.58,
                   "DRAGONFLY_DOJI: T-shape at bottom -> CALL reversal")
    return _no()


def gravestone_doji(c: dict) -> Signal:
    """Gravestone Doji — bearish reversal.

    Body <= 10%, open≈close at bottom, long upper wick >= 50% of range,
    no lower wick. Win rate: ~55–60%.
    """
    a = candle_anatomy(c)
    # Range-relative wick threshold — see dragonfly_doji comment.
    if (a["brr"] <= 0.10 and a["upper_ratio"] >= 0.50
            and a["lower_ratio"] <= 0.05 and a["close_pos"] <= 0.10):
        return _ok("GRAVESTONE_DOJI", -1, 0.58,
                   "GRAVESTONE_DOJI: inverted T at top -> PUT reversal")
    return _no()


def doji(c: dict) -> Signal:
    """Standard / long-legged / four-price Doji — neutral.

    Body <= 10% of range. Direction is 0 (caller must wait for breakout
    confirmation on the next candle). Marked NEUTRAL.

    FIX (double-fire): this function previously DELEGATED to dragonfly_doji
    / gravestone_doji, so a dragonfly candle produced TWO signals named
    DRAGONFLY_DOJI in detect_all() output — one from the standalone
    detector and one from here. Inflated confluence counts. Now this
    returns only neutral indecision labels; the directional T-shapes come
    exclusively from the standalone detectors.
    """
    a = candle_anatomy(c)
    if a["brr"] <= 0.10:
        if a["upper_ratio"] <= 0.05 and a["lower_ratio"] <= 0.05:
            sub = "FOUR_PRICE_DOJI"
        elif a["lower_ratio"] >= 0.50 and a["upper_ratio"] <= 0.05:
            sub = "DOJI_DRAGONFLY_SHAPE"   # neutral info — NOT the reversal signal
        elif a["upper_ratio"] >= 0.50 and a["lower_ratio"] <= 0.05:
            sub = "DOJI_GRAVESTONE_SHAPE"  # neutral info — NOT the reversal signal
        elif a["upper_ratio"] >= 0.40 and a["lower_ratio"] >= 0.40:
            sub = "LONG_LEGGED_DOJI"
        else:
            sub = "DOJI"
        return _ok(sub, 0, 0.40,
                   f"{sub}: body {a['brr']:.0%} of range -> indecision, "
                   f"wait for breakout confirmation")
    return _no()


def spinning_top(c: dict) -> Signal:
    """Spinning Top — small body 10-25% of range with both wicks roughly equal.

    Direction: weak, requires context. Marked NEUTRAL.
    """
    a = candle_anatomy(c)
    if 0.10 < a["brr"] <= 0.25:
        # Both wicks present
        if (a["upper"] > 0.5 * a["body"] and a["lower"] > 0.5 * a["body"]
                and abs(a["upper"] - a["lower"]) <= max(a["upper"], a["lower"]) * 0.40):
            return _ok("SPINNING_TOP", 0, 0.42,
                       f"SPINNING_TOP: body {a['brr']:.0%}, balanced wicks "
                       f"-> indecision")
    return _no()


def marubozu(c: dict) -> Signal:
    """Marubozu — body >= 90% of range, no/very small wicks.

    Continuation in trend, potential reversal at exhaustion.
    Direction = candle color.
    """
    a = candle_anatomy(c)
    if a["brr"] >= 0.90:
        d = +1 if a["is_bull"] else -1
        return _ok("MARUBOZU", d, 0.62,
                   f"MARUBOZU: body {a['brr']:.0%} of range, "
                   f"{'bullish' if d > 0 else 'bearish'} full-power candle "
                   f"-> continuation")
    return _no()


def belt_hold(c: dict) -> Signal:
    """Belt Hold — long body opens at one extreme and closes at the other.

    Bullish belt hold: opens at low, closes near high, body >= 80% of range,
    no lower wick. Bearish mirror.
    """
    a = candle_anatomy(c)
    if a["brr"] >= 0.80 and a["upper_ratio"] <= 0.10 and a["lower_ratio"] <= 0.10:
        # one side zero
        if a["lower_ratio"] <= 0.05 and a["close_pos"] >= 0.70:
            return _ok("BELT_HOLD_BULL", +1, 0.60,
                       "BELT_HOLD_BULL: opens at low, body >= 80%, no lower wick "
                       "-> CALL")
        if a["upper_ratio"] <= 0.05 and a["close_pos"] <= 0.30:
            return _ok("BELT_HOLD_BEAR", -1, 0.60,
                       "BELT_HOLD_BEAR: opens at high, body >= 80%, no upper wick "
                       "-> PUT")
    return _no()


def pin_bar(c: dict) -> Signal:
    """Pin Bar (formal) — body <= 15% of range, main wick >= 66% of range,
    close within top/bottom 25% opposite the wick.

    This is the strictest pin-bar definition and the highest-conviction
    single-candle reversal pattern (~60–65%).
    """
    a = candle_anatomy(c)
    if a["body"] == 0:
        return _no()
    if a["brr"] <= 0.15:
        if a["lower"] >= a["range"] * 0.66 and a["close_pos"] >= 0.75:
            s = 0.60 + min(0.15, (a["lower_ratio"] - 0.66) * 0.45)
            return _ok("PIN_BAR_BULL", +1, min(0.75, s),
                       f"PIN_BAR_BULL: lower wick {a['lower_ratio']:.0%} of "
                       f"range, body {a['brr']:.0%} -> CALL reversal")
        if a["upper"] >= a["range"] * 0.66 and a["close_pos"] <= 0.25:
            s = 0.60 + min(0.15, (a["upper_ratio"] - 0.66) * 0.45)
            return _ok("PIN_BAR_BEAR", -1, min(0.75, s),
                       f"PIN_BAR_BEAR: upper wick {a['upper_ratio']:.0%} of "
                       f"range, body {a['brr']:.0%} -> PUT reversal")
    return _no()


def high_wave(c: dict) -> Signal:
    """High-Wave candle — small body <= 20% of range, both wicks >= 2x body.

    Indicates high volatility indecision. Direction = NEUTRAL.
    Win rate: ~42% standalone (skip without confirmation).
    """
    a = candle_anatomy(c)
    if a["body"] == 0:
        return _no()
    if a["brr"] <= 0.20 and a["upper"] >= 2 * a["body"] and a["lower"] >= 2 * a["body"]:
        return _ok("HIGH_WAVE", 0, 0.30,
                   "HIGH_WAVE: high volatility indecision -> wait")
    return _no()


# ── Two-candle patterns ───────────────────────────────────────────────────────

def bullish_engulfing(c1: dict, c2: dict) -> Signal:
    """Bullish Engulfing — C2 green body fully engulfs C1 red body, C2 body
    >= 1.3x C1, C2 closes in top 25% of its range, small upper wick, C2 has
    strong body-to-range (>= 55%). C1 must be a real red candle (body > 0,
    is_bull=False), not a doji.

    Win rate (research): ~65% (U. Michigan backtest).
    """
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    # C1 must be a real RED candle (body > 0, is_bull=False) — bullish
    # engulfing means a green C2 body swallows a red C1 body.
    # FIX: the old guard `if a1["body"] <= 1e-9 or not a1["is_bull"]`
    # rejected C1 when it was NOT bullish — i.e. it rejected exactly the
    # candles this pattern requires — so BULLISH_ENGULFING could never fire
    # (and THREE_OUTSIDE_UP, which reuses it, was dead too). While
    # BEARISH_ENGULFING fired normally this silently biased every
    # engulfing-weighted pair (USDINR/USDBDT/USDDZD/EURGBP/BRLUSD profiles
    # weight engulfing 1.20-1.30) toward PUT-side signals only.
    if a1["body"] <= 1e-9 or a1["is_bull"]:
        return _no()
    if (a2["is_bull"]
            # C2 body engulfs C1 body
            and a2["open"] <= a1["close"] and a2["close"] >= a1["open"]
            and a2["body"] >= 1.3 * a1["body"]
            and a2["close_pos"] >= 0.70
            and a2["upper_ratio"] <= 0.20
            and a2["brr"] >= 0.55):
        s = 0.60 + min(0.15, (a2["body"] / a1["body"] - 1.3) * 0.10)
        return _ok("BULLISH_ENGULFING", +1, min(0.78, s),
                   f"BULLISH_ENGULFING: C2 body {a2['body']:.5f} engulfs "
                   f"C1 {a1['body']:.5f} (ratio {a2['body']/a1['body']:.1f}x) "
                   f"-> CALL reversal")
    return _no()


def bearish_engulfing(c1: dict, c2: dict) -> Signal:
    """Bearish Engulfing — mirror of bullish. Win rate: ~79% (StockCharts).
    C1 must be a real green candle (body > 0), not a doji.
    """
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    # Reject doji / tiny-body GREEN C1 (correct here: bearish engulfing
    # needs C1 green). The bullish mirror had this guard inverted — see
    # the FIX note in bullish_engulfing.
    if a1["body"] <= 1e-9 or not a1["is_bull"]:
        return _no()
    if (a1["is_bull"] and not a2["is_bull"]
            and a2["open"] >= a1["close"] and a2["close"] <= a1["open"]
            and a2["body"] >= 1.3 * a1["body"]
            and a2["close_pos"] <= 0.30
            and a2["lower_ratio"] <= 0.20
            and a2["brr"] >= 0.55):
        # Strength scale kept IDENTICAL to bullish_engulfing — the old
        # +0.05/+0.04 asymmetry was an unexplained systematic PUT-side bias.
        s = 0.60 + min(0.15, (a2["body"] / a1["body"] - 1.3) * 0.10)
        return _ok("BEARISH_ENGULFING", -1, min(0.78, s),
                   f"BEARISH_ENGULFING: C2 engulfs C1 (ratio "
                   f"{a2['body']/a1['body']:.1f}x) -> PUT reversal")
    return _no()


def bullish_harami(c1: dict, c2: dict) -> Signal:
    """Bullish Harami — C1 large red, C2 small green fully inside C1 body.

    C1 must be a REAL candle (body >= 55% of its range) and C2's body under
    60% of C1's — the old version had no C1-size guard and fired on 8% of
    all candles (high-frequency, low-information vote).
    """
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if (not a1["is_bull"] and a2["is_bull"]
            and a1["brr"] >= 0.55
            and a2["open"] >= a1["close"] and a2["close"] <= a1["open"]
            and a2["body"] < 0.60 * a1["body"]):
        return _ok("BULLISH_HARAMI", +1, 0.50,
                   f"BULLISH_HARAMI: small green inside large red "
                   f"(C2 body {a2['body']/max(a1['body'],1e-9):.0%} of C1) "
                   f"-> CALL reversal (pause)")
    return _no()


def bearish_harami(c1: dict, c2: dict) -> Signal:
    """Bearish Harami — C1 large green, C2 small red inside. Win rate: ~47% (weak)."""
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if (a1["is_bull"] and not a2["is_bull"]
            and a1["brr"] >= 0.55
            and a2["open"] <= a1["close"] and a2["close"] >= a1["open"]
            and a2["body"] < 0.60 * a1["body"]):
        return _ok("BEARISH_HARAMI", -1, 0.42,
                   "BEARISH_HARAMI: small red inside large green -> "
                   "PUT (weak, needs confluence)")
    return _no()


def piercing_line(c1: dict, c2: dict) -> Signal:
    """Piercing Line — C1 long red, C2 opens at/below C1 close (gapless-adapted:
    1-minute OTC candles open at the prior close, so the textbook gap-down
    open is `open == close` here), closes >= 50% into C1 body.

    Win rate: ~64–80% (ATAS).
    """
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if (not a1["is_bull"] and a1["brr"] >= 0.55 and a2["is_bull"]
            and a2["open"] <= a1["close"] * 1.0002   # at/below C1 close (gapless)
            and a2["close"] > (a1["open"] + a1["close"]) / 2   # closes above mid
            and a2["close"] < a1["open"]):             # but below C1 open
        # Penetration depth
        pen = (a2["close"] - a1["close"]) / max(a1["open"] - a1["close"], 1e-9)
        s = 0.55 + min(0.20, (pen - 0.50) * 0.20)
        return _ok("PIERCING_LINE", +1, min(0.75, s),
                   f"PIERCING_LINE: closes {pen:.0%} into C1 body -> CALL reversal")
    return _no()


def dark_cloud_cover(c1: dict, c2: dict) -> Signal:
    """Dark Cloud Cover — C1 long green, C2 opens at/above C1 close
    (gapless-adapted, mirror of piercing_line), closes below midpoint of
    C1 body but above C1 open.

    Win rate: ~60–64%.
    """
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if (a1["is_bull"] and a1["brr"] >= 0.55 and not a2["is_bull"]
            and a2["open"] >= a1["close"] * 0.9998   # at/above C1 close (gapless)
            and a2["close"] < (a1["open"] + a1["close"]) / 2
            and a2["close"] > a1["open"]):
        # FIX: penetration is how far C2's close pushes INTO C1's body,
        # measured from C1's high (close, since C1 is green) downward.
        # Previous formula `(a1[open] - a2[close]) / max(a1[close] - a1[open], 1e-9)`
        # produced a NEGATIVE result for every valid match (a1[open] < a2[close]
        # by precondition), capping strength near 0.55-0.20 = 0.35.
        c1_body = max(a1["close"] - a1["open"], 1e-9)  # green C1: close > open
        pen = (a1["close"] - a2["close"]) / c1_body
        s = 0.55 + min(0.20, max(0.0, pen - 0.50) * 0.20)
        return _ok("DARK_CLOUD_COVER", -1, min(0.75, s),
                   f"DARK_CLOUD_COVER: closes {pen:.0%} into C1 body "
                   f"-> PUT reversal")
    return _no()


def tweezer_top(c1: dict, c2: dict, tol: float = 0.0006) -> Signal:
    """Tweezer Top — matching highs within tolerance (0.06% default),
    C1 bullish, C2 bearish. Win rate: ~56%.
    """
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if a1["is_bull"] and not a2["is_bull"]:
        if abs(c1["high"] - c2["high"]) <= c1["high"] * tol:
            return _ok("TWEEZER_TOP", -1, 0.52,
                       f"TWEEZER_TOP: matched highs at {c1['high']:.5f} "
                       f"-> PUT reversal")
    return _no()


def tweezer_bottom(c1: dict, c2: dict, tol: float = 0.0006) -> Signal:
    """Tweezer Bottom — matching lows, C1 bearish, C2 bullish."""
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if not a1["is_bull"] and a2["is_bull"]:
        if abs(c1["low"] - c2["low"]) <= c1["low"] * tol:
            return _ok("TWEEZER_BOTTOM", +1, 0.52,
                       f"TWEEZER_BOTTOM: matched lows at {c1['low']:.5f} "
                       f"-> CALL reversal")
    return _no()


def bullish_counterattack(c1: dict, c2: dict) -> Signal:
    """Bullish Counterattack — C1 strong red, C2 opens at/below C1 close
    (gapless-adapted: the textbook gap-down open is `open == close` on the
    1-minute OTC feed) and closes within ±0.15% of C1 close. Win rate:
    ~56–59%.
    """
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if (not a1["is_bull"] and a1["brr"] >= 0.55 and a2["is_bull"]
            and a2["open"] <= a1["close"] * 1.0002   # at/below C1 close
            and abs(a2["close"] - a1["close"]) <= a1["close"] * 0.0015):
        return _ok("BULLISH_COUNTERATTACK", +1, 0.50,
                   "BULLISH_COUNTERATTACK: opens at C1 close, closes back "
                   "at C1 close -> CALL reversal")
    return _no()


def bearish_counterattack(c1: dict, c2: dict) -> Signal:
    """Bearish Counterattack — gapless-adapted mirror."""
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if (a1["is_bull"] and a1["brr"] >= 0.55 and not a2["is_bull"]
            and a2["open"] >= a1["close"] * 0.9998   # at/above C1 close
            and abs(a2["close"] - a1["close"]) <= a1["close"] * 0.0015):
        return _ok("BEARISH_COUNTERATTACK", -1, 0.50,
                   "BEARISH_COUNTERATTACK: opens at C1 close, closes back "
                   "at C1 close -> PUT reversal")
    return _no()


def separating_lines_bull(c1: dict, c2: dict) -> Signal:
    """Bullish Separating Lines — C1 bearish, C2 opens at C1 open and closes
    strongly bullish. Win rate: ~78% (continuation).
    """
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if (not a1["is_bull"] and a2["is_bull"]
            and abs(a1["open"] - a2["open"]) <= a1["open"] * 0.0005
            and a2["brr"] >= 0.60):
        return _ok("SEPARATING_LINES_BULL", +1, 0.62,
                   "SEPARATING_LINES_BULL: same open, opposite direction, "
                   "strong C2 -> CALL continuation")
    return _no()


def separating_lines_bear(c1: dict, c2: dict) -> Signal:
    """Bearish Separating Lines."""
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if (a1["is_bull"] and not a2["is_bull"]
            and abs(a1["open"] - a2["open"]) <= a1["open"] * 0.0005
            and a2["brr"] >= 0.60):
        return _ok("SEPARATING_LINES_BEAR", -1, 0.62,
                   "SEPARATING_LINES_BEAR: same open, opposite direction, "
                   "strong C2 -> PUT continuation")
    return _no()


def on_neck(c1: dict, c2: dict) -> Signal:
    """On-Neck — bearish continuation (gapless-adapted). C1 long red, C2
    small green opens at/below C1 close and closes at/near C1 low
    (<=0.15% above) — the 'neckline' the textbooks expect to break.
    """
    a1, a2 = candle_anatomy(c1), candle_anatomy(c2)
    if (not a1["is_bull"] and a1["brr"] >= 0.55 and a2["is_bull"]
            and a2["open"] <= a1["close"] * 1.0002
            and abs(a2["close"] - c1["low"]) <= c1["low"] * 0.0015
            and a2["close"] >= c1["low"]):
        return _ok("ON_NECK", -1, 0.50,
                   "ON_NECK: small green closes at C1 low -> PUT continuation")
    return _no()


# ── Three-candle patterns ────────────────────────────────────────────────────

def morning_star(c1: dict, c2: dict, c3: dict) -> Signal:
    """Morning Star — C1 long red, C2 small body (<=25% of C1), C3 long green
    closing >= 50-67% into C1 body.

    Win rate: ~60-75% (LuxAlgo).
    """
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    if (not a1["is_bull"] and a3["is_bull"]
            and a2["brr"] <= 0.40 and a2["body"] <= 0.30 * a1["body"]
            and a3["close"] > (a1["open"] + a1["close"]) / 2):
        pen = (a3["close"] - a1["close"]) / max(a1["open"] - a1["close"], 1e-9)
        s = 0.60 + min(0.20, (pen - 0.50) * 0.20)
        return _ok("MORNING_STAR", +1, min(0.78, s),
                   f"MORNING_STAR: small star + green closes {pen:.0%} into "
                   f"C1 body -> CALL reversal")
    return _no()


def evening_star(c1: dict, c2: dict, c3: dict) -> Signal:
    """Evening Star — mirror of morning star. Win rate: ~60-72%."""
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    if (a1["is_bull"] and not a3["is_bull"]
            and a2["brr"] <= 0.40 and a2["body"] <= 0.30 * a1["body"]
            and a3["close"] < (a1["open"] + a1["close"]) / 2):
        pen = (a1["close"] - a3["close"]) / max(a1["close"] - a1["open"], 1e-9)
        s = 0.60 + min(0.20, (pen - 0.50) * 0.20)
        return _ok("EVENING_STAR", -1, min(0.78, s),
                   f"EVENING_STAR: small star + red closes {pen:.0%} into "
                   f"C1 body -> PUT reversal")
    return _no()


def three_white_soldiers(c1: dict, c2: dict, c3: dict) -> Signal:
    """Three White Soldiers — 3 consecutive green candles, each body >= 60%
    of its range, each open within prior body, small upper wicks.

    Win rate: ~80-90% under ideal criteria (LuxAlgo).
    """
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    if not (a1["is_bull"] and a2["is_bull"] and a3["is_bull"]):
        return _no()
    if (a1["brr"] >= 0.55 and a2["brr"] >= 0.55 and a3["brr"] >= 0.55
            and a2["open"] >= a1["open"] and a2["open"] <= a1["close"]
            and a3["open"] >= a2["open"] and a3["open"] <= a2["close"]
            and a3["close_pos"] >= 0.70
            and a1["upper_ratio"] <= 0.30 and a2["upper_ratio"] <= 0.30
            and a3["upper_ratio"] <= 0.30):
        # bodies growing = strong
        growing = a1["body"] < a2["body"] < a3["body"]
        s = 0.75 if growing else 0.70
        return _ok("THREE_WHITE_SOLDIERS", +1, s,
                   f"THREE_WHITE_SOLDIERS: 3 strong greens"
                   f"{' (growing bodies)' if growing else ''} -> CALL continuation")
    return _no()


def three_black_crows(c1: dict, c2: dict, c3: dict) -> Signal:
    """Three Black Crows — mirror of three white soldiers."""
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    if not (not a1["is_bull"] and not a2["is_bull"] and not a3["is_bull"]):
        return _no()
    if (a1["brr"] >= 0.55 and a2["brr"] >= 0.55 and a3["brr"] >= 0.55
            and a2["open"] <= a1["open"] and a2["open"] >= a1["close"]
            and a3["open"] <= a2["open"] and a3["open"] >= a2["close"]
            and a3["close_pos"] <= 0.30
            and a1["lower_ratio"] <= 0.30 and a2["lower_ratio"] <= 0.30
            and a3["lower_ratio"] <= 0.30):
        growing = a1["body"] < a2["body"] < a3["body"]
        s = 0.75 if growing else 0.70
        return _ok("THREE_BLACK_CROWS", -1, s,
                   f"THREE_BLACK_CROWS: 3 strong reds"
                   f"{' (growing bodies)' if growing else ''} -> PUT continuation")
    return _no()


def three_inside_up(c1: dict, c2: dict, c3: dict) -> Signal:
    """Three Inside Up — C1 long red, C2 small green inside (Harami),
    C3 green closes above C1 high.

    Win rate: ~65-70%.
    """
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    harami = bullish_harami(c1, c2)
    if (harami.matched and a3["is_bull"]
            and a3["close"] > c1["high"]):
        return _ok("THREE_INSIDE_UP", +1, 0.66,
                   "THREE_INSIDE_UP: harami + green closes above C1 high "
                   "-> CALL reversal")
    return _no()


def three_inside_down(c1: dict, c2: dict, c3: dict) -> Signal:
    """Three Inside Down — mirror."""
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    harami = bearish_harami(c1, c2)
    if (harami.matched and not a3["is_bull"]
            and a3["close"] < c1["low"]):
        return _ok("THREE_INSIDE_DOWN", -1, 0.62,
                   "THREE_INSIDE_DOWN: bearish harami + red closes below C1 low "
                   "-> PUT reversal")
    return _no()


def three_outside_up(c1: dict, c2: dict, c3: dict) -> Signal:
    """Three Outside Up — C1 small red, C2 long green engulfing C1, C3 green
    closes higher. Win rate: ~60-65%.
    """
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    eng = bullish_engulfing(c1, c2)
    if (eng.matched and a3["is_bull"] and a3["close"] > c2["close"]):
        return _ok("THREE_OUTSIDE_UP", +1, 0.63,
                   "THREE_OUTSIDE_UP: bullish engulfing + green continuation "
                   "-> CALL reversal")
    return _no()


def three_outside_down(c1: dict, c2: dict, c3: dict) -> Signal:
    """Three Outside Down — mirror."""
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    eng = bearish_engulfing(c1, c2)
    if (eng.matched and not a3["is_bull"] and a3["close"] < c2["close"]):
        return _ok("THREE_OUTSIDE_DOWN", -1, 0.68,
                   "THREE_OUTSIDE_DOWN: bearish engulfing + red continuation "
                   "-> PUT reversal")
    return _no()


def abandoned_baby_bull(c1: dict, c2: dict, c3: dict) -> Signal:
    """Bullish Abandoned Baby — C1 long red, C2 Doji gapping down with no
    overlap, C3 long green gapping up, closing well into C1 body.

    Rare but high conviction (~70%).
    """
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    doji_sig = doji(c2)
    if (not a1["is_bull"] and a3["is_bull"] and doji_sig.matched
            and c2["high"] < c1["low"]           # gap down, no overlap
            and c3["low"] > c2["high"]          # gap up
            and a3["close"] > (a1["open"] + a1["close"]) / 2):
        return _ok("ABANDONED_BABY_BULL", +1, 0.70,
                   "ABANDONED_BABY_BULL: doji gap + green closes >50% into C1 "
                   "-> CALL reversal (rare, high conviction)")
    return _no()


def abandoned_baby_bear(c1: dict, c2: dict, c3: dict) -> Signal:
    """Bearish Abandoned Baby — mirror."""
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    doji_sig = doji(c2)
    if (a1["is_bull"] and not a3["is_bull"] and doji_sig.matched
            and c2["low"] > c1["high"]
            and c3["high"] < c2["low"]
            and a3["close"] < (a1["open"] + a1["close"]) / 2):
        return _ok("ABANDONED_BABY_BEAR", -1, 0.70,
                   "ABANDONED_BABY_BEAR: doji gap + red closes >50% into C1 "
                   "-> PUT reversal (rare, high conviction)")
    return _no()


def deliberation(c1: dict, c2: dict, c3: dict) -> Signal:
    """Deliberation / Advance Block — 3 green candles with progressively
    smaller bodies and rising upper wicks; C3 opens near C2 close.

    Bearish exhaustion. Moderate; rare.
    """
    a1, a2, a3 = candle_anatomy(c1), candle_anatomy(c2), candle_anatomy(c3)
    if (a1["is_bull"] and a2["is_bull"] and a3["is_bull"]
            and a1["body"] > a2["body"] > a3["body"]
            and a3["upper"] > a2["upper"]
            and a3["upper_ratio"] >= 0.30):
        return _ok("DELIBERATION", -1, 0.55,
                   "DELIBERATION: 3 greens, body shrinking + upper wick rising "
                   "-> exhaustion -> PUT reversal")
    return _no()


def ladder_bottom(c1: dict, c2: dict, c3: dict, c4: dict, c5: dict) -> Signal:
    """Ladder Bottom — 5 candles:
    C1-C3: consecutive long reds (climbing down ladder)
    C4: small indecision (doji/spinning top)
    C5: long green closing above C4 and into C2 body.

    Bullish reversal. ~63%.
    """
    a1 = candle_anatomy(c1); a2 = candle_anatomy(c2); a3 = candle_anatomy(c3)
    a4 = candle_anatomy(c4); a5 = candle_anatomy(c5)
    if not (not a1["is_bull"] and not a2["is_bull"] and not a3["is_bull"]):
        return _no()
    if not (a1["brr"] >= 0.55 and a2["brr"] >= 0.55 and a3["brr"] >= 0.55):
        return _no()
    if a4["brr"] > 0.40:
        return _no()
    if not (a5["is_bull"] and a5["brr"] >= 0.55
            and a5["close"] > c4["high"]):
        return _no()
    return _ok("LADDER_BOTTOM", +1, 0.63,
               "LADDER_BOTTOM: 3 reds + indecision + green breakout "
               "-> CALL reversal")


def ladder_top(c1: dict, c2: dict, c3: dict, c4: dict, c5: dict) -> Signal:
    """Ladder Top — mirror of ladder bottom."""
    a1 = candle_anatomy(c1); a2 = candle_anatomy(c2); a3 = candle_anatomy(c3)
    a4 = candle_anatomy(c4); a5 = candle_anatomy(c5)
    if not (a1["is_bull"] and a2["is_bull"] and a3["is_bull"]):
        return _no()
    if not (a1["brr"] >= 0.55 and a2["brr"] >= 0.55 and a3["brr"] >= 0.55):
        return _no()
    if a4["brr"] > 0.40:
        return _no()
    if not (not a5["is_bull"] and a5["brr"] >= 0.55
            and a5["close"] < c4["low"]):
        return _no()
    return _ok("LADDER_TOP", -1, 0.63,
               "LADDER_TOP: 3 greens + indecision + red breakdown "
               "-> PUT reversal")


# ── Four+ candle patterns ────────────────────────────────────────────────────

def rising_three_methods(c1: dict, c2: dict, c3: dict, c4: dict, c5: dict) -> Signal:
    """Rising Three Methods — C1 long green; C2-C4 small bearish candles
    inside C1's RANGE (the published definition — the old body-only test
    made the pattern unreachable on a gapless feed); C5 long green closing
    above C1 high. Bullish continuation. ~65-70%.
    """
    a1 = candle_anatomy(c1); a5 = candle_anatomy(c5)
    if not (a1["is_bull"] and a1["brr"] >= 0.55 and a5["is_bull"]
            and a5["brr"] >= 0.55 and a5["close"] > c1["high"]):
        return _no()
    c1_lo, c1_hi = c1["low"], c1["high"]
    inside_count = 0
    for c in (c2, c3, c4):
        a = candle_anatomy(c)
        # Counter-trend: small reds inside C1's RANGE.
        if (not a["is_bull"]
                and a["body"] <= 0.30 * a1["body"]
                and c["low"] >= c1_lo
                and c["high"] <= c1_hi):
            inside_count += 1
    if inside_count == 3:
        return _ok("RISING_THREE_METHODS", +1, 0.66,
                   f"RISING_THREE_METHODS: long green + {inside_count} small reds inside "
                   f"+ green breakout -> CALL continuation")
    return _no()


def falling_three_methods(c1: dict, c2: dict, c3: dict, c4: dict, c5: dict) -> Signal:
    """Falling Three Methods — mirror (counter-candles inside C1's RANGE)."""
    a1 = candle_anatomy(c1); a5 = candle_anatomy(c5)
    if not (not a1["is_bull"] and a1["brr"] >= 0.55 and not a5["is_bull"]
            and a5["brr"] >= 0.55 and a5["close"] < c1["low"]):
        return _no()
    c1_lo, c1_hi = c1["low"], c1["high"]
    inside_count = 0
    for c in (c2, c3, c4):
        a = candle_anatomy(c)
        if (a["is_bull"]
                and a["body"] <= 0.30 * a1["body"]
                and c["low"] >= c1_lo
                and c["high"] <= c1_hi):
            inside_count += 1
    if inside_count == 3:
        return _ok("FALLING_THREE_METHODS", -1, 0.66,
                   f"FALLING_THREE_METHODS: long red + {inside_count} small greens inside "
                   f"+ red breakdown -> PUT continuation")
    return _no()


# ── Convenience: enumerate ALL detectors ─────────────────────────────────────

SINGLE_CANDLE_DETECTORS = [
    hammer, hanging_man, shooting_star, inverted_hammer,
    dragonfly_doji, gravestone_doji, doji, spinning_top,
    marubozu, belt_hold, pin_bar, high_wave,
]

TWO_CANDLE_DETECTORS = [
    bullish_engulfing, bearish_engulfing, bullish_harami, bearish_harami,
    piercing_line, dark_cloud_cover, tweezer_top, tweezer_bottom,
    bullish_counterattack, bearish_counterattack,
    separating_lines_bull, separating_lines_bear, on_neck,
]

THREE_CANDLE_DETECTORS = [
    morning_star, evening_star, three_white_soldiers, three_black_crows,
    three_inside_up, three_inside_down, three_outside_up, three_outside_down,
    # Abandoned Baby removed: it requires a full gap between candles, which
    # never exists on the gapless 1-minute feed (0 fires in 20k). The
    # detector functions remain for reference but are not run.
    deliberation,
]

# Same-anatomy family map — detectors that validate the SAME candle shape.
# One shape = one piece of evidence. Exported for reuse by the runner.
FAMILY_OF: dict[str, str] = {
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

# Context twins: byte-identical anatomy, opposite meaning. Both fire on the
# same shape and their votes cancel — the direction is decided by the LOCAL
# 3-candle move, exactly one twin is kept.
_TWIN_PAIRS = {
    ("HAMMER", "HANGING_MAN"),                 # hammer-shaped body low
    ("SHOOTING_STAR", "INVERTED_HAMMER"),      # star-shaped body high
}


def _local_move(candles: list[dict], lookback: int = 3) -> int:
    """Sign of the net close-to-close move over the last `lookback` candles."""
    if len(candles) < lookback + 1:
        return 0
    net = candles[-1]["close"] - candles[-1 - lookback]["close"]
    if net > 0:
        return 1
    if net < 0:
        return -1
    return 0


def _resolve_twins(signals: list[Signal], candles: list[dict]) -> list[Signal]:
    """Keep exactly one twin per pair, chosen by the local move direction.

    HAMMER (bullish reversal) is meaningful after a DOWN-move;
    HANGING_MAN (bearish reversal) after an UP-move. With no local move the
    shape carries no directional information and BOTH twins are dropped.
    """
    move = _local_move(candles)
    by_name = {s.name: s for s in signals}
    drop: set[str] = set()
    for a, b in _TWIN_PAIRS:
        sa, sb = by_name.get(a), by_name.get(b)
        if sa is None and sb is None:
            continue
        if sa is not None and sb is not None:
            # both fired — keep the one matching the local move
            if move > 0:      # up-move → bearish twin is the reversal read
                drop.add(a)
            elif move < 0:    # down-move → bullish twin
                drop.add(b)
            else:
                drop.add(a); drop.add(b)
        elif sa is not None and move > 0:
            drop.add(a)       # bull twin in an up-move = wrong context
        elif sb is not None and move < 0:
            drop.add(b)       # bear twin in a down-move = wrong context
    return [s for s in signals if s.name not in drop]


def _dedup_families(signals: list[Signal]) -> list[Signal]:
    """Collapse same-anatomy families to their strongest member."""
    best: dict[str, Signal] = {}
    for s in signals:
        key = FAMILY_OF.get(s.name, s.name)
        prev = best.get(key)
        if prev is None or s.strength > prev.strength:
            best[key] = s
    return list(best.values())


def detect_all(candles: list[dict]) -> list[Signal]:
    """Run every pattern detector over the most recent candles.

    CLEANUP PIPELINE (fixes the measured vote-stacking problems):
      1. run all detectors
      2. resolve context twins (hammer/hanging-man, star family) via the
         local 3-candle move — never emit both directions of one shape
      3. dedup same-anatomy families — one shape, one vote
    Direction-0 (indecision) signals are included: callers that count
    "agreement" must only count signals with direction != 0.
    """
    out: list[Signal] = []
    if not candles:
        return out
    cur = candles[-1]
    for fn in SINGLE_CANDLE_DETECTORS:
        s = fn(cur)
        if s.matched:
            out.append(s)
    if len(candles) >= 2:
        c1, c2 = candles[-2], candles[-1]
        for fn in TWO_CANDLE_DETECTORS:
            s = fn(c1, c2)
            if s.matched:
                out.append(s)
    if len(candles) >= 3:
        c1, c2, c3 = candles[-3], candles[-2], candles[-1]
        for fn in THREE_CANDLE_DETECTORS:
            s = fn(c1, c2, c3)
            if s.matched:
                out.append(s)
    if len(candles) >= 5:
        c1, c2, c3, c4, c5 = candles[-5], candles[-4], \
                             candles[-3], candles[-2], candles[-1]
        for fn in (rising_three_methods, falling_three_methods,
                   ladder_bottom, ladder_top):
            s = fn(c1, c2, c3, c4, c5)
            if s.matched:
                out.append(s)
    out = _resolve_twins(out, candles)
    out = _dedup_families(out)
    return out
