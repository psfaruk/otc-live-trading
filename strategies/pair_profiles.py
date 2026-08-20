"""
Per-pair profile and strategy registry.

Research shows each forex pair (especially OTC exotics) has dramatically
different 1-minute behavior — USDINR_otc mean-reverts because the real
RBI-controlled pair has a tight peg, USDMXN_otc trends hard, USDBDT_otc
amplifies noise to make a barely-moving peg tradeable.

This module encodes, per pair:
  - Volatility profile (typical ATR multiple)
  - Best session windows (UTC hours when the pair "behaves")
  - Dominant strategy archetype (TREND / MEAN_REVERT / RANGE_BOUNCE / MIXED)
  - Strategy scoring weights (which patterns to weight up/down)

The runner.py composes pattern detections with this per-pair weighting
to produce the final CALL/PUT/NEUTRAL signal.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal


StrategyArchetype = Literal["TREND", "MEAN_REVERT", "RANGE_BOUNCE", "MIXED"]


@dataclass
class PairProfile:
    """Per-pair behavior profile used to weight strategy signals."""
    asset: str
    display: str
    archetype: StrategyArchetype
    # (start_hour_utc, end_hour_utc) windows when this pair behaves best.
    # Signals emitted outside these windows have strength dampened.
    best_windows_utc: list[tuple[int, int]] = field(default_factory=list)
    # Strategy weight overrides: pattern_name -> multiplier (1.0 default).
    # >1 = boost this strategy on this pair; <1 = suppress.
    strategy_weights: dict[str, float] = field(default_factory=dict)
    # Trap-wick sensitivity: how aggressively to dampen wick-rejection
    # signals. Higher = more skeptical of pin-bar patterns.
    trap_wick_sensitivity: float = 1.0
    # Whether continuation bias has positive expectancy (OTC trend feed).
    continuation_edge: float = 1.0
    # Minimum candle ATR (as fraction of price) required before any
    # directional signal is emitted. Used to suppress noise on dead pairs.
    min_atr_pct: float = 0.0001
    notes: str = ""


# ── Per-pair profiles (research-driven) ───────────────────────────────────────

_PROFILES: dict[str, PairProfile] = {
    # ── OTC exotic pairs (synthetic Quotex feeds) ──────────────────────────────
    "USDINR_otc": PairProfile(
        asset="USDINR_otc", display="USD/INR (OTC)",
        archetype="RANGE_BOUNCE",
        best_windows_utc=[(2, 8), (13, 17)],
        strategy_weights={
            # RBI-controlled real pair -> range-bounce dominates on OTC
            "TWEEZER_BOTTOM": 1.30, "TWEEZER_TOP": 1.30,
            "BULLISH_ENGULFING": 1.20, "BEARISH_ENGULFING": 1.20,
            "PIN_BAR_BULL": 1.25, "PIN_BAR_BEAR": 1.25,
            # Breakout strategies fail (trap wick pierces levels) — no
            # detector is named "BREAKOUT" (removed a dead weight entry
            # for it that never matched anything); the closest real
            # breakout-like continuation patterns are dampened directly:
            "THREE_WHITE_SOLDIERS": 0.80, "THREE_BLACK_CROWS": 0.80,
        },
        trap_wick_sensitivity=1.30,     # Quotex amplifies trap wicks here
        continuation_edge=1.05,
        min_atr_pct=0.00005,
        notes="RBI-controlled real pair. OTC injects noise. Range-bounce + "
              "tweezer reversals dominate; breakouts fail."
    ),
    "USDIDR_otc": PairProfile(
        asset="USDIDR_otc", display="USD/IDR (OTC)",
        archetype="TREND",
        best_windows_utc=[(1, 7), (13, 19)],
        strategy_weights={
            # Bank Indonesia cycle reversals are common — trade trend but
            # fade exhaustion.
            "MORNING_STAR": 1.25, "EVENING_STAR": 1.25,
            "THREE_WHITE_SOLDIERS": 1.20, "THREE_BLACK_CROWS": 1.20,
            "DELIBERATION": 1.30,                # exhaustion pattern is key
            "RISING_THREE_METHODS": 1.20, "FALLING_THREE_METHODS": 1.20,
        },
        trap_wick_sensitivity=1.10,
        continuation_edge=1.10,
        min_atr_pct=0.00005,
        notes="Long-term IDR weakness = trend bias. Trade momentum, fade "
              "exhaustion (deliberation pattern)."
    ),
    "USDCOP_otc": PairProfile(
        asset="USDCOP_otc", display="USD/COP (OTC)",
        archetype="TREND",
        best_windows_utc=[(13, 21)],
        strategy_weights={
            "BULLISH_ENGULFING": 1.25, "BEARISH_ENGULFING": 1.25,
            "MARUBOZU": 1.20,
            "THREE_WHITE_SOLDIERS": 1.15, "THREE_BLACK_CROWS": 1.15,
            "TWEEZER_BOTTOM": 0.80, "TWEEZER_TOP": 0.80,
        },
        trap_wick_sensitivity=1.40,    # high-volatility + frequent traps
        continuation_edge=1.10,
        min_atr_pct=0.00008,
        notes="High volatility EM pair. Engulfing + marubozu continuation "
              "work; mean-reversion fails. Larger trap wicks."
    ),
    "USDBDT_otc": PairProfile(
        asset="USDBDT_otc", display="USD/BDT (OTC)",
        archetype="MEAN_REVERT",
        best_windows_utc=[(2, 8), (13, 17)],
        strategy_weights={
            # Real BDT is pegged — Quotex amplifies noise, so mean-reversion
            # dominates.
            "PIN_BAR_BULL": 1.30, "PIN_BAR_BEAR": 1.30,
            "HAMMER": 1.25, "SHOOTING_STAR": 1.25,
            "DRAGONFLY_DOJI": 1.25, "GRAVESTONE_DOJI": 1.25,
            "BULLISH_ENGULFING": 1.20, "BEARISH_ENGULFING": 1.20,
            "THREE_WHITE_SOLDIERS": 0.70, "THREE_BLACK_CROWS": 0.70,
        },
        trap_wick_sensitivity=1.50,
        continuation_edge=0.95,
        min_atr_pct=0.00003,
        notes="Real BDT barely moves (peg). Quotex injects false volatility — "
              "3-candle extremes + pin-bar reversals win."
    ),
    "USDMXN_otc": PairProfile(
        asset="USDMXN_otc", display="USD/MXN (OTC)",
        archetype="TREND",
        best_windows_utc=[(13, 21)],
        strategy_weights={
            # Strong trending — continuation dominates.
            "THREE_WHITE_SOLDIERS": 1.30, "THREE_BLACK_CROWS": 1.30,
            "MARUBOZU": 1.25,
            "RISING_THREE_METHODS": 1.25, "FALLING_THREE_METHODS": 1.25,
            "MORNING_STAR": 1.15, "EVENING_STAR": 1.15,
            "TWEEZER_BOTTOM": 0.70, "TWEEZER_TOP": 0.70,
        },
        trap_wick_sensitivity=1.10,
        continuation_edge=1.20,
        min_atr_pct=0.00010,
        notes="Strongest OTC trending pair. 3+ same-color candles favor "
              "continuation. Mean-reversion fails."
    ),
    "USDDZD_otc": PairProfile(
        asset="USDDZD_otc", display="USD/DZD (OTC)",
        archetype="RANGE_BOUNCE",
        best_windows_utc=[(8, 16)],
        strategy_weights={
            # Like USDBDT — managed peg, amplifies noise.
            "PIN_BAR_BULL": 1.30, "PIN_BAR_BEAR": 1.30,
            "TWEEZER_BOTTOM": 1.25, "TWEEZER_TOP": 1.25,
            "BULLISH_ENGULFING": 1.20, "BEARISH_ENGULFING": 1.20,
            # No detector is named "BREAKOUT" (see USDINR_otc's note) — the
            # dampen intent lands on the real continuation patterns instead:
            "THREE_WHITE_SOLDIERS": 0.80, "THREE_BLACK_CROWS": 0.80,
        },
        trap_wick_sensitivity=1.50,
        continuation_edge=0.95,
        min_atr_pct=0.00004,
        notes="Algerian managed peg. Range-bounce + fade spikes. Breakouts "
              "are trap-wick bait."
    ),
    "USDPHP_otc": PairProfile(
        asset="USDPHP_otc", display="USD/PHP (OTC)",
        archetype="MIXED",
        best_windows_utc=[(1, 7), (13, 19)],
        strategy_weights={
            # Trend-then-revert cycle: 2-3 candles continuation, then fade.
            "THREE_WHITE_SOLDIERS": 1.10, "THREE_BLACK_CROWS": 1.10,
            "DELIBERATION": 1.30,
            "BULLISH_ENGULFING": 1.20, "BEARISH_ENGULFING": 1.20,
            "PIN_BAR_BULL": 1.15, "PIN_BAR_BEAR": 1.15,
        },
        trap_wick_sensitivity=1.20,
        continuation_edge=1.05,
        min_atr_pct=0.00006,
        notes="Trend 2-3 candles, then fade 4th. Engulfing + exhaustion "
              "patterns key. Moderate volatility."
    ),
    "USDPKR_otc": PairProfile(
        asset="USDPKR_otc", display="USD/PKR (OTC)",
        archetype="MIXED",
        best_windows_utc=[(4, 10), (13, 19)],
        strategy_weights={
            # Step-function behavior — trend after spike, range-bounce after.
            "MARUBOZU": 1.25,
            "BULLISH_ENGULFING": 1.20, "BEARISH_ENGULFING": 1.20,
            "PIN_BAR_BULL": 1.20, "PIN_BAR_BEAR": 1.20,
            "TWEEZER_BOTTOM": 1.15, "TWEEZER_TOP": 1.15,
        },
        trap_wick_sensitivity=1.30,
        continuation_edge=1.10,
        min_atr_pct=0.00005,
        notes="Step-function behavior (mimics real PKR devaluations). Trade "
              "trend after spike, range-bounce in consolidation."
    ),
    "USDZAR_otc": PairProfile(
        asset="USDZAR_otc", display="USD/ZAR (OTC)",
        archetype="TREND",
        best_windows_utc=[(7, 16)],
        strategy_weights={
            # Most volatile EM pair — momentum continuation, mean-reversion
            # fails.
            "MARUBOZU": 1.25,
            "THREE_WHITE_SOLDIERS": 1.20, "THREE_BLACK_CROWS": 1.20,
            "RISING_THREE_METHODS": 1.20, "FALLING_THREE_METHODS": 1.20,
            "MORNING_STAR": 1.20, "EVENING_STAR": 1.20,
            "TWEEZER_BOTTOM": 0.70, "TWEEZER_TOP": 0.70,
        },
        trap_wick_sensitivity=1.40,
        continuation_edge=1.15,
        min_atr_pct=0.00012,
        notes="Highest volatility EM pair. Momentum continuation wins, "
              "mean-reversion fails. Large trap wicks."
    ),
    "BRLUSD_otc": PairProfile(
        asset="BRLUSD_otc", display="USD/BRL (OTC)",
        archetype="TREND",
        best_windows_utc=[(13, 20)],
        strategy_weights={
            # Trending with sharp counter-moves — needs 2-candle confirmation.
            "BULLISH_ENGULFING": 1.30, "BEARISH_ENGULFING": 1.30,
            "MORNING_STAR": 1.25, "EVENING_STAR": 1.25,
            "THREE_OUTSIDE_UP": 1.25, "THREE_OUTSIDE_DOWN": 1.25,
            "MARUBOZU": 1.15,
        },
        trap_wick_sensitivity=1.35,
        continuation_edge=1.10,
        min_atr_pct=0.00010,
        notes="Trending with frequent sharp counter-moves. ALWAYS require "
              "2-candle confirmation — single-candle entries fail."
    ),
    "NZDUSD_otc": PairProfile(
        asset="NZDUSD_otc", display="NZD/USD (OTC)",
        archetype="TREND",
        best_windows_utc=[(0, 7), (13, 19)],
        strategy_weights={
            # Commodity currency — trends persist 5-10 candles.
            "MARUBOZU": 1.20,
            "THREE_WHITE_SOLDIERS": 1.25, "THREE_BLACK_CROWS": 1.25,
            "RISING_THREE_METHODS": 1.20, "FALLING_THREE_METHODS": 1.20,
            "MORNING_STAR": 1.15, "EVENING_STAR": 1.15,
        },
        trap_wick_sensitivity=1.10,
        continuation_edge=1.10,
        min_atr_pct=0.00007,
        notes="Commodity currency. Asian session cleanest trends. "
              "Continuation patterns dominate."
    ),

    # ── Real major pairs ────────────────────────────────────────────────────────
    "USDJPY": PairProfile(
        asset="USDJPY", display="USD/JPY",
        archetype="RANGE_BOUNCE",
        best_windows_utc=[(0, 9), (12, 16)],
        strategy_weights={
            # Asian range-bounce, London breakout continuation.
            "TWEEZER_BOTTOM": 1.20, "TWEEZER_TOP": 1.20,
            "BULLISH_ENGULFING": 1.15, "BEARISH_ENGULFING": 1.15,
            "PIN_BAR_BULL": 1.20, "PIN_BAR_BEAR": 1.20,
        },
        trap_wick_sensitivity=1.20,    # false breakouts common
        continuation_edge=1.05,
        min_atr_pct=0.00005,
        notes="Asian session range-bounce; London breakout continuation. "
              "False breakouts common. BoJ intervention risk."
    ),
    "EURUSD": PairProfile(
        asset="EURUSD", display="EUR/USD",
        archetype="MIXED",
        best_windows_utc=[(7, 16)],
        strategy_weights={
            # Trend continuation after pullback during London; range-bounce
            # during Asian.
            "MARUBOZU": 1.10,
            "BULLISH_ENGULFING": 1.20, "BEARISH_ENGULFING": 1.20,
            "MORNING_STAR": 1.20, "EVENING_STAR": 1.20,
            "PIN_BAR_BULL": 1.20, "PIN_BAR_BEAR": 1.20,
            "TWEEZER_BOTTOM": 1.15, "TWEEZER_TOP": 1.15,
        },
        trap_wick_sensitivity=1.00,
        continuation_edge=1.00,
        min_atr_pct=0.00004,
        notes="Cleanest real pair. London trend continuation + Asian "
              "range-bounce. Engulfing at session opens high-conviction."
    ),
    "GBPUSD": PairProfile(
        asset="GBPUSD", display="GBP/USD",
        archetype="TREND",
        best_windows_utc=[(7, 9), (12, 16)],
        strategy_weights={
            # London open breakout after Asian sweep; 3+ same-color candles.
            "THREE_WHITE_SOLDIERS": 1.20, "THREE_BLACK_CROWS": 1.20,
            "MARUBOZU": 1.15,
            "BULLISH_ENGULFING": 1.25, "BEARISH_ENGULFING": 1.25,
            "MORNING_STAR": 1.20, "EVENING_STAR": 1.20,
        },
        trap_wick_sensitivity=1.20,
        continuation_edge=1.10,
        min_atr_pct=0.00006,
        notes="London open breakout after Asian range sweep. Strong trending "
              "tendency with sharp reversals."
    ),
    "AUDUSD": PairProfile(
        asset="AUDUSD", display="AUD/USD",
        archetype="TREND",
        best_windows_utc=[(0, 7), (12, 16)],
        strategy_weights={
            # Commodity currency, Asian session.
            "MARUBOZU": 1.15,
            "THREE_WHITE_SOLDIERS": 1.20, "THREE_BLACK_CROWS": 1.20,
            "RISING_THREE_METHODS": 1.20, "FALLING_THREE_METHODS": 1.20,
            "MORNING_STAR": 1.15, "EVENING_STAR": 1.15,
        },
        trap_wick_sensitivity=1.10,
        continuation_edge=1.10,
        min_atr_pct=0.00005,
        notes="Commodity currency. Asian session trends persist longest. "
              "Avoid China data release windows."
    ),
    "EURGBP": PairProfile(
        asset="EURGBP", display="EUR/GBP",
        archetype="RANGE_BOUNCE",
        best_windows_utc=[(8, 17)],
        strategy_weights={
            # Slow range-bound pair during London.
            "TWEEZER_BOTTOM": 1.30, "TWEEZER_TOP": 1.30,
            "PIN_BAR_BULL": 1.25, "PIN_BAR_BEAR": 1.25,
            "BULLISH_ENGULFING": 1.20, "BEARISH_ENGULFING": 1.20,
            "THREE_WHITE_SOLDIERS": 0.80, "THREE_BLACK_CROWS": 0.80,
        },
        trap_wick_sensitivity=1.00,
        continuation_edge=0.95,
        min_atr_pct=0.00003,
        notes="Lowest volatility major pair. Range-bounce during London "
              "only. Dead during Asian session."
    ),
}


def get_profile(asset: str) -> PairProfile:
    """Look up a per-pair profile. Falls back to a neutral MIXED profile
    for unknown assets so the engine never crashes."""
    p = _PROFILES.get(asset)
    if p is not None:
        return p
    # FIX: previously the unknown-asset fallback used best_windows_utc=[(0, 24)]
    # which made session_quality return 1.0 (full strength) for EVERY hour —
    # meaning unknown pairs got NO session dampening while curated pairs did.
    # If a new pair was added to _WANTED_PAIRS without a matching _PROFILES
    # entry, it would silently run at full signal strength 24/7. Now the
    # fallback is NEUTRAL (empty windows -> session_quality falls back to its
    # 0.45-0.75 dead-session path) so unknown pairs get treated with the
    # same skepticism as off-window curated pairs.
    return PairProfile(
        asset=asset, display=asset, archetype="MIXED",
        best_windows_utc=[],
        strategy_weights={}, trap_wick_sensitivity=1.0,
        continuation_edge=1.0, min_atr_pct=0.00005,
        notes="Unknown asset — neutral MIXED profile applied."
    )


def list_profiles() -> list[PairProfile]:
    """Return all known profiles (used by the /api/pairs endpoint)."""
    return list(_PROFILES.values())


def is_in_best_window(profile: PairProfile, hour_utc: int) -> bool:
    """Check whether the current UTC hour falls inside any best window."""
    for start, end in profile.best_windows_utc:
        # windows can wrap midnight (e.g. (22, 4))
        if start <= end:
            if start <= hour_utc < end:
                return True
        else:
            if hour_utc >= start or hour_utc < end:
                return True
    return False


def session_quality(profile: PairProfile, hour_utc: int) -> float:
    """Return 0..1 multiplier for signal strength based on session timing.

    Inside a best window = 1.0; outside but in active forex hours = 0.7;
    dead hours (Asian for majors, dead session for EURGBP) = 0.4.
    """
    if is_in_best_window(profile, hour_utc):
        return 1.0
    # Active forex hours (London + NY) = 7..17 UTC
    if 7 <= hour_utc < 21:
        return 0.75
    # Asian session active for AUD/NZD/JPY
    if 0 <= hour_utc < 9 and profile.asset in (
            "AUDUSD", "NZDUSD_otc", "USDJPY", "USDINR_otc",
            "USDIDR_otc", "USDPHP_otc", "USDPKR_otc"):
        return 0.75
    # Late-night dead session
    return 0.45
