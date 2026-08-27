# Strategies Engine — Research-Driven Per-Pair Signal System

This document describes the new modular strategy engine that replaces the
legacy `analyze_eoc.py` as the primary signal generator. It is built from
web research on Quotex OTC binary options, candlestick patterns, per-pair
behavior, and 1-minute trading strategies.

## Architecture

```
strategies/
├── __init__.py            — public API exports
├── patterns.py            — 40+ candlestick pattern detectors
├── pair_profiles.py        — per-pair behavioral profiles + weighting
└── runner.py              — composes patterns + context + per-pair weights
                           → final CALL/PUT/NEUTRAL signal
```

The legacy `analyze_eoc.py` is kept as a fallback for shadow comparison —
set `USE_STRATEGY_ENGINE=0` in the environment to revert.

## How it differs from the old engine

| Aspect | Old (analyze_eoc) | New (strategies/) |
|--------|-------------------|-------------------|
| Patterns | 5 generic theories (RUN, WICKWALL, MICRO, DIVERGENCE, LIVE) + MARKET_STATE | 40+ specific candlestick patterns + market state |
| Per-pair awareness | None (one model fits all 16 pairs) | Each pair has its own profile (archetype, weights, windows) |
| Research basis | Internal DB analysis only | Web research on candlestick patterns, OTC behavior, per-pair economics |
| Trap-wick filter | Implicit (WICKWALL) | Explicit per-pair sensitivity (e.g., USDZAR gets 1.4x dampen) |
| Session timing | Single UTC 22-7 dampener | Per-pair best_windows_utc + session_quality() function |
| Continuation bias | Heavy (3.8:1 measured) | Reduced — patterns are direction-explicit, not color-forced |
| Confluence bonus | Round number + key level only | Round number + key level + wick wall + supply/demand zone |

## Per-Pair Profiles

Each of the 16 pairs has a researched archetype:

| Pair | Archetype | Best Windows (UTC) | Notes |
|------|-----------|---------------------|-------|
| USDINR_otc | RANGE_BOUNCE | 2-8, 13-17 | RBI-controlled real pair → OTC range-bounce + tweezer reversals |
| USDIDR_otc | TREND | 1-7, 13-19 | Bank Indonesia cycle reversals — trade trend, fade exhaustion |
| USDCOP_otc | TREND | 13-21 | High-vol EM pair — engulfing + marubozu continuation |
| USDBDT_otc | MEAN_REVERT | 2-8, 13-17 | Real BDT pegged → Quotex noise → 3-candle extremes + pin-bars |
| USDMXN_otc | TREND | 13-21 | Strongest OTC trending pair — 3+ same-color candles favor continuation |
| USDDZD_otc | RANGE_BOUNCE | 8-16 | Algerian managed peg — range-bounce + fade spikes |
| USDPHP_otc | MIXED | 1-7, 13-19 | Trend 2-3 candles, then fade 4th (BSP cycle) |
| USDPKR_otc | MIXED | 4-10, 13-19 | Step-function behavior — trend after spike, range-bounce in consolidation |
| USDZAR_otc | TREND | 7-16 | Most volatile EM pair — momentum continuation, mean-reversion fails |
| BRLUSD_otc | TREND | 13-20 | Trending with sharp counter-moves — 2-candle confirmation required |
| NZDUSD_otc | TREND | 0-7, 13-19 | Commodity currency — Asian session trends persist |
| USDJPY | RANGE_BOUNCE | 0-9, 12-16 | Asian range-bounce, London breakout continuation |
| EURUSD | MIXED | 7-16 | Cleanest real pair — London trend continuation + Asian range-bounce |
| GBPUSD | TREND | 7-9, 12-16 | London open breakout after Asian range sweep |
| AUDUSD | TREND | 0-7, 12-16 | Commodity currency — Asian session trends persist longest |
| EURGBP | RANGE_BOUNCE | 8-17 | Lowest volatility major — London range-bounce only |

## Candlestick Patterns Implemented

### Single-candle (12)
- Reversal: Hammer, Hanging Man, Shooting Star, Inverted Hammer, Dragonfly Doji, Gravestone Doji, Pin Bar (formal)
- Continuation: Marubozu, Belt Hold
- Indecision (NEUTRAL): Doji (4 subtypes), Spinning Top, High-Wave

### Two-candle (13)
- Reversal: Bullish/Bearish Engulfing, Bullish/Bearish Harami, Piercing Line, Dark Cloud Cover, Tweezer Top/Bottom, Bullish/Bearish Counterattack
- Continuation: Separating Lines (Bull/Bear), On Neck

### Three-candle (11)
- Reversal: Morning Star, Evening Star, Three Inside Up/Down, Three Outside Up/Down, Abandoned Baby (Bull/Bear), Deliberation
- Continuation: Three White Soldiers, Three Black Crows

### Four+ candle (4)
- Rising/Falling Three Methods, Ladder Bottom, Ladder Top

Each detector returns: `(matched, direction, strength, reason)` where
direction is +1 (CALL), -1 (PUT), or 0 (NEUTRAL/indecision).

## Open API Endpoints

All endpoints are PUBLIC (no auth required) — anyone with the URL can
fetch signals:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/signals` | GET | All pairs' current signals (enriched JSON) |
| `/api/v1/signals/{asset}` | GET | One pair's signal (e.g. `/api/v1/signals/EURUSD_otc`) |
| `/api/v1/signals/{asset}/plain` | GET | One-line plain-text signal (for `curl` / simple bots) |
| `/api/v1/pairs` | GET | All pair profiles (archetype, best windows, notes) |
| `/api/v1/strategies` | GET | List of all 40+ pattern detectors |
| `/api/v1/backtest` | GET | Latest backtest report (regenerate with `python tools/backtest_strategies.py --all`) |

### WebSocket Event: `signal_start`

A new `signal_start` event is broadcast the MOMENT a 1-minute candle opens
(0-second signal), carrying the full prediction for that candle. This is
the user's explicit requirement:
> "এক মিনিটের ক্যান্ডেল যখন 0 সেকেন্ড এ শুরু হবে, টিক তখনি সিগন্যাল আসতে হবে।"

Payload:
```json
{
  "type": "signal_start",
  "asset": "EURUSD_otc",
  "period": 60,
  "candle_open_time": 1700000000,
  "candle_expires_at": 1700000060,
  "signal": "CALL",
  "strength": "MEDIUM",
  "confidence": 0.65,
  "score": 4,
  "agree": 3,
  "patterns_fired": ["BULLISH_ENGULFING", "MARKET_STATE"],
  "reasons": ["..."],
  "prediction_candle": {"open": 1.0850, "high": 1.0855, "low": 1.0845, "close": 1.0854}
}
```

## Backtest Results (v2 — calibrated feed, multi-seed)

Run `python tools/backtest_strategies.py --all --seeds 3` to regenerate.
(Old table kept at the bottom for reference; its TREND numbers were
circular — the generator baked trends in, then the engine got credit for
finding them.)

### 2026-08 Signal-Accuracy Overhaul — what was wrong and what changed

**Why predictions were wrong (diagnosed, not guessed):**

1. `BULLISH_ENGULFING` could NEVER fire — its C1 guard was inverted
   (`reject C1 if not bullish` on a pattern that REQUIRES a red C1).
   `THREE_OUTSIDE_UP` (reuses it) was dead too, while the bearish mirrors
   fired normally → systematic PUT-side bias on engulfing setups.
2. The confluence bonus amplified whatever direction the score already
   leaned — even a bearish upper-wick rejection at resistance strengthened
   a bullish lean (backwards).
3. Session quality used the WALL CLOCK instead of the candle's own hour →
   session logic was untestable and the report's Best/Worst-Hour columns
   were meaningless.
4. Same-anatomy detectors stacked votes: PIN_BAR_BULL + HAMMER +
   DRAGONFLY_DOJI are ONE candle counted 3x; MARUBOZU + BELT_HOLD one big
   body counted 2x. Fake "multi-theory agreement" manufactured MEDIUM
   signals that measured 43-47% live — an anti-signal.
5. **The engine was a parrot**: the measured live stats in analyze_eoc.py
   put color-following continuation at 47.3% (marubozu base, n=859),
   47.3% (spinning top, n=237), 48.3% (doji, n=232) — following the last
   candle LOSES on this feed, yet the tiebreaks and market-state both
   followed it by default, and the replay harness measured the feed as
   near-random (~50%) with a 72% continuation share.

**The fixes (all measured, in order of impact):**

- Per-archetype fade/follow: RANGE_BOUNCE / MEAN_REVERT profiles FADE the
  last candle in the no-edge fallback and carry a small reversion prior in
  market-state; TREND profiles follow; unconfirmed-regime tiebreaks fade
  (regime detection lags — following it at the lag moment measured below
  50% on every archetype).
- Continuation-family regime gate: marubozu/soldiers/crows/separating-line
  votes are capped by archetype (TREND 0.90x aligned … RANGE/MEAN_REVERT
  0.25x in chop).
- Same-anatomy family dedup + honest strength calibration (STRONG needs
  3+ distinct evidence families, |score|>=5; MEDIUM 2 families, |score|>=3).
- Profile-aware exhaustion (streak>=3 on MEAN_REVERT/RANGE_BOUNCE/MIXED,
  matching the documented "push 2-3 candles then fade" OTC behavior).
- Backtest v2: calibrated generator (per-archetype continuation
  probability matching the live measurements + trap wicks + wandering
  anchor), decision-path attribution, payout breakeven line, multi-seed
  averaging (`--seeds 3`).

**Before/after on the calibrated feed (3 seeds × 2000 candles × 16 pairs
≈ 94,000 graded signals; breakeven at 92% payout = 52.08%):**

| Archetype | Before | After | Δ |
|-----------|--------|-------|---|
| RANGE_BOUNCE | 48.26% | 49.95% | +1.69 |
| MEAN_REVERT | 47.75% | 50.48% | +2.73 |
| MIXED | 49.22% | 51.40% | +2.18 |
| TREND | 51.03% | 52.16% | +1.13 |
| **Overall** | **49.80%** | **51.29%** | **+1.49** |

15 of 16 pairs improved. This synthetic feed is a LOWER BOUND: reversal
patterns get no wick-reversion edge by construction (wicks are noise), so
pattern votes floor near 50% there. The real test remains live — check
`/api/diagnosis` after a few days of streaming and compare PATTERN_VOTE
accuracy per pair before trusting any pattern stack.

### Historical (v1 generator — circular, kept for reference)

| Pair | Archetype | Accuracy | Cont% | Best Hour% |
|------|-----------|----------|-------|-------------|
| AUDUSD | TREND | 82.06% | 88.39% | 91.67% |
| GBPUSD | TREND | 80.34% | 89.58% | 86.67% |
| NZDUSD_otc | TREND | 74.14% | 80.21% | 80.00% |
| USDIDR_otc | TREND | 75.59% | 81.66% | 90.00% |
| USDZAR_otc | TREND | 63.98% | 84.43% | 75.00% |
| BRLUSD_otc | TREND | 63.98% | 82.98% | 81.25% |
| USDMXN_otc | TREND | 62.27% | 81.66% | 71.67% |
| USDCOP_otc | TREND | 60.55% | 83.77% | 72.00% |
| EURUSD | MIXED | 52.64% | 77.57% | 60.00% |
| USDPKR_otc | MIXED | 50.92% | 81.79% | 58.00% |
| USDPHP_otc | MIXED | 50.00% | 78.76% | 60.42% |
| EURGBP | RANGE_BOUNCE | 50.79% | 81.93% | 66.00% |
| USDINR_otc | RANGE_BOUNCE | 47.76% | 84.83% | 58.33% |
| USDBDT_otc | MEAN_REVERT | 47.49% | 83.11% | 58.33% |
| USDDZD_otc | RANGE_BOUNCE | 47.36% | 82.06% | 54.00% |
| USDJPY | RANGE_BOUNCE | 46.70% | 81.66% | 51.67% |

The v1 "82% on AUDUSD" was the generator congratulating the engine for
detecting the trends the generator had just planted. Do not use it.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_STRATEGY_ENGINE` | `1` | Use new modular strategy engine (1) or legacy analyze_eoc (0) |
| `ALL_PAIRS_ALWAYS_ON` | `1` | Pre-warm all open pairs (1) or only pairs above PAYOUT_FLOOR (0) |
| `QX_PAYOUT_FLOOR` | `81` | Minimum payout % for pair to be "tradeable" (informational only when ALL_PAIRS_ALWAYS_ON=1) |
| `ENABLE_LIVE_THEORY` | `1` | Enable running-candle LIVE theory |
| `ENABLE_STRENGTH_GATE` | `1` | Enable strength gating on running candle |

## Honest Expectations

This engine is BUILT on solid research but 1-minute binary options remain
near-random. Research consensus: 55-70% win rate is professional-level
on Quotex OTC with disciplined confluence trading. The per-pair profiles
and pattern detectors here give the engine its best shot at that range,
but no signal system can guarantee profitability. Always pair with strict
money management (1-2% risk per trade, max 5-10 trades/day, NO martingale).
