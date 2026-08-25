# otc-live-trading (Plybit AI)

Live OTC binary-options signal dashboard. Streams forex candles from Quotex,
runs a multi-theory price-action blend on every closed candle, and broadcasts
CALL/PUT signals with strength (STRONG / MEDIUM / WEAK) + a deep market-state
read (continuation / exhaustion / reversal / trap).

Public dashboard — **no signup, no login, no admin panel, no API keys**.
The ONLY credential is a Quotex SSID token pasted into the Settings tab.
Every visitor sees every chart, every signal, every theory reason.

## ⚠ Read this first — live data connection

The dashboard will show "Waiting…" forever until a Quotex SSID token is set.
There is **no email/password fallback** — Cloudflare's JS challenge on the
`qxbroker.com/en/sign-in` page rejects every non-browser HTTP client with a
403, so the legacy email/password login path was removed entirely (retrying
it just hammered Quotex with bad credentials and never succeeded).

The token path bypasses the login form entirely and opens the WebSocket
directly with the SSID cookie.

### How to extract QX_TOKEN (one-time, ~30 seconds)

1. Open https://qxbroker.com/en/sign-in in Chrome/Firefox
2. Log in with your Quotex email + password
3. Once you're on the trading dashboard, press **F12** to open DevTools
4. Go to **Application** tab → **Storage** → **Cookies** → `https://qxbroker.com`
5. Find the cookie named **`session`** (or `ssid` on some accounts)
6. Copy its **Value** (a long string starting with `eyJ...` or similar)
7. Paste it into the **Settings → Quotex Connection → Session Token (SSID)**
   field on the dashboard, or set it as the `QX_TOKEN` env var on Railway.

Alternatively, in DevTools **Console** after logging in:
```js
document.cookie.split(';').filter(c => c.includes('session') || c.includes('ssid'))[0]
```

### How to set env vars on Railway (optional — for headless deploys)

The recommended path is to paste the token in the Settings tab — it takes
priority over the env var and can be refreshed without a redeploy. If you
prefer the env var:

1. Open your project on https://railway.com
2. Click the `otc-live-trading` service
3. Go to the **Variables** tab
4. Add `QX_TOKEN` = the cookie value from step 6 above
5. Railway auto-redeploys with the new env var

### Diagnostic endpoint

Once deployed, open `https://<your-app>.up.railway.app/api/debug` in a
browser. It returns JSON showing exactly which auth path the feed is using
and what's wrong if it isn't connecting:

```json
{
  "env": {"QX_TOKEN_set": true, "QX_HOST": "qxbroker.com (default)"},
  "feed": {"connected": true, "pairs_loaded": 16, "active_streams": 16},
  "token": {"has_user_token": true, "login_failed": false, "connected": true},
  "hint": "Feed is connected — live data should be streaming."
}
```

## Login behavior — single attempt, no auto-retry

The login system tries **EXACTLY ONCE** per token. If the connect attempt
fails for ANY reason (auth reject, timeout, network error), the system does
NOT retry automatically — the manager loop idles until the operator pastes
a fresh SSID (or the same one again) in the Settings tab.

This is the user's explicit requirement:
> "যদি কোনো কারণে প্রথম বারে লগিং ফেইল হলে, সেকেন্ড টাইম আর লগিং ট্রাই করা যাবে না।"

The token is preserved on transient failures (timeouts, network blips) so
the operator doesn't have to re-extract the SSID — just paste the same token
again to retry once. On a real `authorization/reject` from the server (the
token is genuinely invalid/expired), the token is cleared and the operator
must re-extract a fresh SSID.

## Environment variables

| Var | Required? | Purpose |
|-----|-----------|---------|
| `QX_TOKEN` | **Recommended** | SSID cookie from a logged-in browser session. Bypasses the Cloudflare-protected login form entirely. Can also be pasted in the Settings tab (takes priority). |
| `QX_HOST` | Optional | Defaults to `qxbroker.com`. |
| `QX_PAYOUT_FLOOR` | Optional | Min payout % for a pair to be streamable. Default `81`. |
| `QX_DB_PATH` | Optional | SQLite path. Set this to a Railway volume for persistence across redeploys. |
| `QX_ROOT` | Optional | Browser profile cache dir. Defaults to a temp dir. |
| `QX_UA` | Optional | Override User-Agent string. |
| `QX_MAX_STREAMS` | Optional | Default `45`. |
| `QX_STAGGER_GAP_SEC` | Optional | Default `1.5`. |
| `PORT` | Set by Railway | — |

There is **no** `QX_EMAIL`, `QX_PASSWORD`, `PLYBIT_API_KEYS`, `SESSION_SECRET`,
or `ADMIN_EMAILS` env var — those legacy paths were removed.

## Pair list

The app streams exactly 16 pairs (whitelist-curated, see `_WANTED_PAIRS` in `feed.py`):

**OTC (11):** BRL/USD, USD/INR, USD/IDR, USD/COP, USD/BDT, USD/MXN, NZD/USD, USD/DZD, USD/PHP, USD/PKR, USD/ZAR

**Real (5):** USD/JPY, EUR/USD, GBP/USD, AUD/USD, EURGBP

## Signal guarantee

**Every closed candle emits a CALL or PUT signal — no NEUTRAL is ever shown.**
This is the user's explicit requirement:
> "প্রত্যেক পেয়ার এ প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে।"

When the multi-theory analyzer returns a zero score (no theory voted, no
color-independent evidence, no regime continuation), a layered tiebreak
picks a direction in this order:

1. Color-independent evidence lean (e.g. RUN absorption).
2. Regime continuation with deep-state conviction ≥ 25%.
3. Any regime direction (UPTREND/DOWNTREND) even without state confirmation.
4. Final fallback: the just-closed candle's own color (bull → CALL, bear → PUT).

Every fallback layer is marked WEAK strength so the user knows the
difference between a strong-agreement signal and a tiebreak. The
backtest (`tools/backtest_strategies.py`) enforces this invariant with a
hard assertion — if any candle returns NEUTRAL, the backtest fails loudly.

## Tab navigation

Four tabs (mobile = bottom bar, desktop = sidebar):

1. **Home** — overview dashboard: connection status, pairs streaming count,
   overall win-rate, active signals count, plus the live Share Signal table
   for all 16 pairs.
2. **Chart Signal** — main chart + deep-analysis side panel (market state,
   theory accuracy, key levels, live micro flow, EOC analysis).
3. **History** — resolved signal log with pair filter + postmortem.
4. **Settings** — Quotex SSID token input, preferences, legal, about.

## Open API

All read endpoints are fully public — anyone with the URL can fetch live
signals via `/api/v1/signals`, `/api/v1/signals/{asset}`, or
`/api/v1/signals/{asset}/plain`. The Open API shape is documented in the
endpoint docstrings (see `server.py`).

## Backtest

Run `python tools/backtest_strategies.py --all` to generate a per-pair
synthetic-candle backtest that verifies the every-candle signal guarantee
and reports per-pattern accuracy. Output is saved to
`reports/backtest_report.json` and `.md` inside the repo. Override the
location with `--out DIR` or the `BACKTEST_OUT` env var.

## Deploy

Railway auto-deploys from `main`. Healthcheck at `/healthz`.

```
web: python -u -m uvicorn server:app --host 0.0.0.0 --port $PORT
```
