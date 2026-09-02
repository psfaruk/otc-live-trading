# otc-live-trading (NOVA · OTC Signal Terminal)

Live OTC binary-options signal dashboard. Streams forex candles from Quotex,
runs a **confluence council** of six independent strategy voters on every
closed candle, and broadcasts a signal **only when several voters agree with
enough weight and no veto**. Everything else is honestly reported as
**NO TRADE**.

Public dashboard — **no signup, no login, no admin panel, no API keys**.
The ONLY credential is a Quotex SSID token pasted into the Settings tab.

## ⚠ Read this first — live data connection

The dashboard will show "Waiting…" forever until a Quotex SSID token is set.
There is **no email/password fallback** — Cloudflare's JS challenge on the
`qxbroker.com/en/sign-in` page rejects every non-browser HTTP client with a
403.

### How to extract QX_TOKEN (one-time, ~30 seconds)

1. Open https://qxbroker.com/en/sign-in in Chrome/Firefox and log in
2. Press **F12** → **Application** → **Cookies** → `https://qxbroker.com`
3. Copy the value of the **`session`** (or `ssid`) cookie
4. Paste it into **Settings → Quotex Connection**, or set it as the `QX_TOKEN`
   env var on Railway

### Login behavior — single attempt, no auto-retry

The login system tries **EXACTLY ONCE** per token paste (the operator's
explicit requirement). On a real `authorization/reject` the token is cleared;
on transient failures it is preserved — paste the same token again to retry.

## Signal policy — confluence or nothing

> "সেই স্ট্রাটেজি high confidence থাকবে... ফলব্যাক সিগন্যাল থাকবে না।
> High confidence বেশ কয়েকটি স্ট্রাটেজি এক মত হতে হবে। এটা অবস্থান বোঝে
> সিদ্ধান্ত নিবে।" — the owner, 2026-09

The council emits **CALL/PUT only when ALL of these hold** (else NEUTRAL,
nothing is broadcast/logged/graded):

| Rule | Threshold (env-tunable) |
|------|-------------------------|
| R1 weighted net score | `\|net\| ≥ 4.0` (`QX_SCORE_FLOOR`) |
| R2 several voters agree | agree ≥ 3 (`QX_MIN_AGREE`) |
| R3 the agreement has weight | agree_weight ≥ 4.0 (`QX_WEIGHT_FLOOR`) |
| R4 no veto | no opposing voter with weight ≥ 2.5 (`QX_VETO_WEIGHT`) |
| R5 liquidity gates | ATR above pair floor, session alive |
| R6 **STAT anchor** | the measured statistical layer must be among the agreeing voters (`QX_REQUIRE_STAT=1`) |

**There are NO tiebreaks and NO fallback signals.** The old
"every candle must emit" behaviour was the single largest source of wrong
predictions (forced coin flips measured 47-53%) and was removed entirely.

### The six voters (each one independent information)

1. **STAT** — empirical per-pair continuation rate conditioned on the current
   run length (streak-bucket z-test over the trailing 120 candles) + archetype
   prior. The only measurable next-candle statistic on a near-memoryless feed.
2. **REGIME** — least-squares trend (R² + steepness). Follows only top-slice
   confirmations (R² ≥ 0.40), fades runs of 3+ inside a trend (measured
   "run 2-3 then fade" OTC behaviour). Naive trend-following measured 47.7%
   in attribution — an anti-signal — and is now gated.
3. **POSITION** — the অবস্থান voter: where price sits inside its 40-candle
   range, how far it is stretched from SMA20 (in ATRs), and whether the
   rejection wick touches a real key level / wick wall.
4. **PATTERN** — cleaned candlestick evidence as ONE vote (same-anatomy
   families deduped, hammer/hanging-man twins context-resolved, momentum
   patterns regime-gated).
5. **STATE** — market state (CONTINUATION / EXHAUSTION / REVERSAL / TRAP)
   with honest conviction.
6. **FLOW** — closed-candle tick absorption (buyers/sellers vs close
   position). Only when ≥ 15 real ticks exist.

### Backtest (deterministic, 4 seeds × 16 pairs × 1500 candles ≈ 96k candles)

Run `python tools/backtest_strategies.py --all --seeds 4`. Reports land in
`reports/backtest_report.{json,md}`. Key results:

- **portfolio 53.85%** over 7,621 decided signals — 95% CI **[52.73, 54.97]**,
  entirely above the 52.08% break-even at 92% payout
- out-of-sample seeds 2-3: 54.04%, CI [52.47, 55.60]
- emission share ~8% of candles — the council declines ~92% of them
- the harness is **deterministic** (crc32 seeding — the old `hash()` seeding
  made every run test a different dataset) and **non-circular** (the
  generator's continuation table is a fixed, published constant)
- per-voter attribution is computed from each voter's OWN vote, exposing
  e.g. the regime anti-signal that got fixed

Honest note: this is a synthetic-feed result that validates CALIBRATION and
REGRESSION, not live profitability. 1-minute OTC remains near-memoryless.

## History integrity — no overrides, ever

The user's explicit requirement: "অ্যাপ এ কোনো প্রকার ওভার রাইট থাকতে পারবে না।"

- **Two-phase logging**: `open_signal()` writes the signal AS EMITTED the
  instant the candle opens (result=`pending`); `finalize_signal()` writes
  ONLY the outcome fields exactly once (`WHERE result='pending'`).
  Signal fields written at emission can never be rewritten — `INSERT OR
  REPLACE` on signal_log is gone.
- **Frozen predictions**: the mid-candle re-eval that used to REPLACE the
  direction (grading a signal nobody could trade) now produces a
  display-only `live_view`. The strength gate (which folded the outcome
  candle's own ticks into `strength` — pure lookahead) is deleted.
- **Honest buckets**: `correct / wrong / draw (broker refund) / no_data
  (candle closed with zero ticks — ungradeable) / pending`. Draw and no_data
  are excluded from every win-rate, shown separately.
- One canonical stats core (Wilson 95% intervals, payout break-even,
  unified MIN_N=20) feeds every surface — no more disagreeing numbers
  between tabs.
- SQLite runs WAL + busy_timeout so graded rows can no longer vanish into
  `database is locked` errors.

## Win-rate analytics

- **Win Rates tab** — per-pair CALL/PUT/ALL buckets with Wilson CIs judged
  against the payout break-even; a bucket is "PROVEN" only when the whole
  interval clears it.
- **History tab** — filter by pair / direction / outcome (incl. draws and
  no_data), full postmortem per row.
- Endpoints: `GET /api/winrate-calls?days=7`, `GET /api/pair-winrate?days=7`,
  `GET /api/stats?asset=&period=&days=`, `GET /api/signals?...`.

## Pair list

16 pairs (whitelist, see `_WANTED_PAIRS` in `feed.py`):

**OTC (11):** BRL/USD, USD/INR, USD/IDR, USD/COP, USD/BDT, USD/MXN, NZD/USD, USD/DZD, USD/PHP, USD/PKR, USD/ZAR

**Real (5):** USD/JPY, EUR/USD, GBP/USD, AUD/USD, EURGBP

## WebSocket events

| type | payload |
|------|---------|
| `pairs` | pair catalog + payout floor |
| `snapshot` | `{asset, period, candles, prediction}` on subscribe |
| `eoc` | candle closed: new candles + the NEW candle's frozen prediction + `accuracy` of the previous one |
| `signal_start` | the frozen prediction broadcast the moment the candle opens (incl. `voters`, `confluence`) |
| `tick` | running candle + microstructure + optional display-only `live_view` |
| `stale` | stream produced no data |

`signal` values are `CALL`, `PUT`, or `NEUTRAL` (**NO TRADE** — not a
direction, never graded).

## Open API

All read endpoints are public: `/api/v1/signals`, `/api/v1/signals/{asset}`,
`/api/v1/signals/{asset}/plain`, `/api/v1/pairs`, `/api/v1/strategies`,
`/api/v1/backtest`, plus the `/api/*` endpoints the UI consumes.

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `QX_TOKEN` | — | SSID cookie (or paste in Settings; takes priority) |
| `QX_HOST` | `qxbroker.com` | broker host |
| `QX_PAYOUT_FLOOR` | `81` | min payout % for streaming |
| `QX_DB_PATH` | `./candle_micro.db` | SQLite path (use a Railway volume) |
| `QX_MIN_AGREE` | `3` | council: min agreeing voters |
| `QX_SCORE_FLOOR` | `4.0` | council: min weighted net score |
| `QX_WEIGHT_FLOOR` | `4.0` | council: min agreeing weight |
| `QX_VETO_WEIGHT` | `2.5` | council: opposing-voter veto threshold |
| `QX_REQUIRE_STAT` | `1` | council: require the STAT anchor |
| `QX_MAX_STREAMS` | `45` | stream cap |
| `PORT` | set by Railway | — |

## Deploy

Railway auto-deploys from `main`. Healthcheck at `/healthz`.

```
web: python -u -m uvicorn server:app --host 0.0.0.0 --port $PORT
```
