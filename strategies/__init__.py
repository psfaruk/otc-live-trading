"""Strategies package — CONFLUENCE COUNCIL signal engine.

Public API:
  - run_strategy(candles, asset, period, ticks, running_ticks, muted,
                 chop_zone) -> standard signal dict consumed by feed.py
  - list_profiles() -> list of PairProfile
  - get_profile(asset) -> PairProfile (fallback: neutral MIXED)
  - detect_all(candles) -> list of fired pattern Signals (deduped,
    context-resolved)

The council emits CALL/PUT only when several independent voters agree.
NEUTRAL ("NO TRADE") is a first-class output — there are NO tiebreaks and
NO fallbacks. The legacy analyze_eoc.py is retained for reference only and
is no longer part of the live path.
"""
from .runner import (
    run_strategy, _neutral_signal,
    MIN_AGREE, SCORE_FLOOR, WEIGHT_FLOOR, VETO_WEIGHT,
)
from .pair_profiles import (
    PairProfile, get_profile, list_profiles,
    is_in_best_window, session_quality,
)
from .patterns import Signal, detect_all, candle_anatomy, FAMILY_OF

__all__ = [
    "run_strategy", "_neutral_signal",
    "MIN_AGREE", "SCORE_FLOOR", "WEIGHT_FLOOR", "VETO_WEIGHT",
    "PairProfile", "get_profile", "list_profiles",
    "is_in_best_window", "session_quality",
    "Signal", "detect_all", "candle_anatomy", "FAMILY_OF",
]
