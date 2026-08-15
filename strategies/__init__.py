"""Strategies package — modular candlestick + per-pair signal engine.

Public API:
  - run_strategy(candles, asset, period, ticks, running_ticks, muted)
      -> standard signal dict consumed by feed.py
  - list_profiles() -> list of PairProfile
  - get_profile(asset) -> PairProfile (fallback: neutral MIXED)
  - detect_all(candles) -> list of fired pattern Signals

This package is the research-driven replacement for analyze_eoc.py.
The legacy analyze_eoc is kept for backward compatibility / shadow
comparison; the live path uses run_strategy.
"""
from .runner import run_strategy, _empty_signal
from .pair_profiles import (
    PairProfile, get_profile, list_profiles,
    is_in_best_window, session_quality,
)
from .patterns import Signal, detect_all, candle_anatomy

__all__ = [
    "run_strategy",
    "PairProfile", "get_profile", "list_profiles",
    "is_in_best_window", "session_quality",
    "Signal", "detect_all", "candle_anatomy",
]
