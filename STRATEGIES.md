# Confluence Council — Signal Engine Architecture (2026-09)

This document describes the signal engine that replaced the pattern-vote
stack and the every-candle tiebreak. Built from the measured failure modes
of the previous two engines.

## Why predictions were wrong — the diagnosed record

### Engine v1 (pattern votes + every-candle tiebreak)
- 40+ candlestick detectors stacked votes as the "signal"; the backtest
  measured that path at ~50.4% (coin flip) while STRONG signals scored
  WORSE than WEAK (49.4% vs 51.0%) — stacking noise manufactured false
  confidence.
- Same-anatomy detectors triple-counted one candle (HAMMER + PIN_BAR +
  DRAGONFLY_DOJI = 3 votes for one long lower wick; measured 520/520
  co-fires for the hammer/hanging-man twins).
- Nine detectors could NEVER fire on the gapless 1-minute feed (they
  require inter-candle gaps: piercing line, dark cloud cover, both
  counterattacks, on-neck, both abandoned babies, both three-methods).
- The always-emit tiebreak (indep lean → regime → last-candle colour)
  forced a CALL/PUT on every candle; forced coin flips were the bulk of
  all emitted signals and the bulk of the losses.

### Engine v2 (STAT base + capped evidence) — better, still forced
- Inverted the weighting (measured statistics first) — correct.
- Still kept the every-candle guarantee: every no-edge candle got a
  WEAK tiebreak signal anyway.
- The mid-candle LIVE re-eval REPLACED the emitted direction using the
  running candle's own ticks — the graded signal was not the broadcast
  signal (lookahead).
- The strength gate folded the outcome candle's ticks into `strength`
  (by_strength once read ~80% for MEDIUM — a mirage).
- `INSERT OR REPLACE` on signal_log meant history could be silently
  rewritten.
- Backtest seeded with `hash(asset)` — Python randomizes string hashes
  per process, so every run tested a DIFFERENT dataset.

### Engine v3 (this one) — confluence council, no fallbacks
Removes the entire class of forced-signal problems: **when the council
does not agree, nothing is emitted**. See README for the emission rules.

## Architecture

```
strategies/
├── __init__.py            — public API exports
├── patterns.py            — candlestick detectors + detect_all() cleanup
│                            pipeline (twin resolution + family dedup)
├── pair_profiles.py       — per-pair profiles (archetype, weights, ATR floor)
└── runner.py              — the council: 6 voters → decision rules →
                             CALL/PUT or NEUTRAL (NO TRADE)
```

### The voters

| Voter | Information source | Notes |
|-------|--------------------|-------|
| STAT | empirical continuation rate (streak-bucket z-test, trailing 120 candles) + archetype prior | the anchor; measured 53.6% on its own votes |
| REGIME | least-squares trend (R² + steepness) | follows only R²≥0.40, fades 3+ runs; naive following measured 47.7% — an anti-signal |
| POSITION | range position (40-candle), SMA20 extension (ATRs), key-level/wick-wall rejection | the অবস্থান voter |
| PATTERN | cleaned candlestick net vote | one vote, never a stack; momentum family regime-gated |
| STATE | market state (CONTINUATION/EXHAUSTION/REVERSAL/TRAP) conviction | |
| FLOW | closed-candle tick absorption | only with ≥15 real ticks |

### Emission rules (all must pass — else NEUTRAL)

```
R1 |net| >= 4.0            (QX_SCORE_FLOOR)
R2 agree >= 3              (QX_MIN_AGREE)
R3 agree_weight >= 4.0     (QX_WEIGHT_FLOOR)
R4 no opposing voter with weight >= 2.5   (QX_VETO_WEIGHT)
R5 ATR floor + session quality gates
R6 STAT among the agreeing voters         (QX_REQUIRE_STAT)
```

R6 was chosen by a threshold grid-search over 46k recorded signals and
validated out-of-sample: pattern/microstructure agreement alone graded
~51.4% (below the 52.08% break-even at 92% payout); with the STAT anchor
the same candles graded 53.5%, and fresh seeds confirmed 54.0%.

### Signal lifecycle (frozen-at-open guarantee)

```
candle N closes
  └─ council runs on CLOSED candles only → prediction for N+1
       ├─ phase 1: open_signal()  → signal_log row, result='pending'
       └─ signal_start broadcast (signal, voters, confluence)
candle N+1 opens … running ticks may drive a DISPLAY-ONLY live_view
candle N+1 closes
  └─ phase 2: finalize_signal()  → outcome fields ONLY, exactly once
       (correct / wrong / draw / no_data)
```

Nothing may mutate the prediction between broadcast and grading. The old
mid-candle re-eval (direction replacement) and the strength gate (outcome
leak into `strength`) are deleted.

## Grading semantics (identical live + backtest)

A signal computed from candles[0..i] predicts candle i+1 and is graded on
candle i+1's own open→close. `close == open` (±1e-10) is a **draw** (broker
refund). A candle that closed with **zero ticks** is **no_data** — the
broker's real candle may have moved while the feed was silent; the row is
kept for audit but excluded from every win-rate. NEUTRAL is never graded.

## Detector cleanup (patterns.py)

- `detect_all()` runs every detector, then:
  1. **twin resolution** — HAMMER/HANGING_MAN and SHOOTING_STAR/
     INVERTED_HAMMER share byte-identical anatomy with opposite meaning;
     the local 3-candle move decides which one is meaningful (or neither).
  2. **family dedup** — same-anatomy families collapse to the strongest
     member (one shape = one vote).
- Gap-requiring detectors were gapless-adapted (piercing line, dark cloud
  cover, counterattacks, on-neck) or retired (abandoned babies).
- Three-methods now uses the published range-based definition.
- Harami requires a real C1 (body ≥ 55% of range).
- Bearish/bullish engulfing strength scales are symmetric (the old +0.05
  PUT-side asymmetry is gone).
- `doji()` no longer delegates to the dragonfly/gravestone detectors (the
  old double-fire counted one T-shape twice).

## Per-pair profiles

16 profiles with archetype (TREND / RANGE_BOUNCE / MEAN_REVERT / MIXED),
per-strategy weights, trap-wick sensitivity and ATR floors. Session
quality now applies to REAL pairs only — OTC feeds are broker-synthesised
24/7 with no measurable hour-of-day structure, and dampening them by
wall-clock sessions was unvalidated noise.

## Backtest

`python tools/backtest_strategies.py --all --seeds 4`

- deterministic (crc32 seeding), non-circular generator (fixed published
  continuation table), confluence-aware (NEUTRAL allowed and counted),
  voter attribution by own vote, complete multi-seed aggregation.
- asserts the council's own guarantees: every emitted signal has
  agree ≥ MIN_AGREE and matches its confluence block.
- latest report: `reports/backtest_report.{json,md}`.

## Honest expectations

The calibrated-backtest portfolio sits at ~53.9% (CI 52.7-55.0) with an
~8% emission share. That is above the 92%-payout break-even (52.08%) but
it is NOT a guarantee, and the synthetic feed cannot capture everything
live does. Per-pair results vary (13 of 16 pairs above break-even in the
shipped report; USDIDR_otc and EURUSD below). Check `/api/diagnosis` and
the Win Rates tab after a few days of live streaming before trusting any
bucket, and never risk what you cannot lose. No martingale.
