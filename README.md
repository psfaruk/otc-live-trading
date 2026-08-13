# otc-live-trading (Plybit AI)

Live OTC binary-options signal dashboard. Streams forex candles from Quotex,
runs a multi-theory price-action blend on every closed candle, and broadcasts
CALL/PUT signals with strength (STRONG / MEDIUM / WEAK) + a deep market-state
read (continuation / exhaustion / reversal / trap).

Public dashboard — **no signup, no login**. Every visitor sees every chart,
every signal, every theory reason.

## ⚠ Read this first — live data connection

The dashboard will show "Waiting…" forever until Quotex credentials are set
on Railway. The recommended path is `QX_TOKEN` (a pre-extracted SSID cookie),
NOT `QX_EMAIL`/`QX_PASSWORD` — the Quotex sign-in page is now Cloudflare-
protected and rejects login attempts from non-browser HTTP clients with a JS
challenge ("Just a moment…") that httpx cannot solve. The token path
bypasses the login form entirely and opens the WebSocket directly.

### How to extract QX_TOKEN (one-time, ~30 seconds)

1. Open https://qxbroker.com/en/sign-in in Chrome/Firefox
2. Log in with your Quotex email + password
3. Once you're on the trading dashboard, press **F12** to open DevTools
4. Go to **Application** tab → **Storage** → **Cookies** → `https://qxbroker.com`
5. Find the cookie named **`session`** (or `ssid` on some accounts)
6. Copy its **Value** (a long string starting with `eyJ...` or similar)
7. Set it as the `QX_TOKEN` env var on Railway (see below)

Alternatively, in DevTools **Console** after logging in:
```js
document.cookie.split(';').filter(c => c.includes('session') || c.includes('ssid'))[0]
```

### How to set env vars on Railway

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
  "feed": {"connected": true, "pairs_loaded": 15, "active_streams": 15},
  "hint": "Feed is connected — live data should be streaming."
}
```

## Environment variables

| Var | Required? | Purpose |
|-----|-----------|---------|
| `QX_TOKEN` | **Recommended** | SSID cookie from a logged-in browser session. Bypasses the Cloudflare-protected login form entirely. |
| `QX_EMAIL` | Fallback | Quotex account email. Will likely fail with HTTP 403 due to Cloudflare. |
| `QX_PASSWORD` | Fallback | Quotex account password. Same caveat as QX_EMAIL. |
| `QX_HOST` | Optional | Defaults to `qxbroker.com`. |
| `QX_PAYOUT_FLOOR` | Optional | Min payout % for a pair to be streamable. Default `81`. |
| `QX_DB_PATH` | Optional | SQLite path. Set this to a Railway volume for persistence across redeploys. |
| `QX_ROOT` | Optional | Browser profile cache dir. Defaults to a temp dir. |
| `QX_UA` | Optional | Override User-Agent string. |
| `PLYBIT_API_KEYS` | Optional | Comma-separated API keys. When set, write endpoints (POST /api/token, DELETE /api/token, POST /api/subscribe) require an `X-API-Key` header. Default empty = open access for everyone. |
| `PORT` | Set by Railway | — |

## Pair list

The app streams exactly 16 pairs (whitelist-curated, see `_WANTED_PAIRS` in `feed.py`):

**OTC (11):** BRL/USD, USD/INR, USD/IDR, USD/COP, USD/BDT, USD/MXN, NZD/USD, USD/DZD, USD/PHP, USD/PKR, USD/ZAR

**Real (5):** USD/JPY, EUR/USD, GBP/USD, AUD/USD, EURGBP

## Signal guarantee

Every closed candle emits a CALL or PUT signal — no NEUTRAL is ever shown.
When the multi-theory analyzer returns a zero score (no theory voted, no
color-independent evidence, no regime continuation), a layered tiebreak
picks a direction in this order:

1. Color-independent evidence lean (e.g. RUN absorption).
2. Regime continuation with deep-state conviction ≥ 25%.
3. Any regime direction (UPTREND/DOWNTREND) even without state confirmation.
4. Final fallback: the just-closed candle's own color (bull → CALL, bear → PUT).

Every fallback layer is marked WEAK strength so the user knows the
difference between a strong-agreement signal and a tiebreak.

## Tab navigation

Four tabs (mobile = bottom bar, desktop = sidebar):

1. **Home** — overview dashboard: connection status, pairs streaming count,
   overall win-rate, active signals count, plus the live Share Signal table
   for all 16 pairs.
2. **Chart Signal** — main chart + deep-analysis side panel (market state,
   theory accuracy, key levels, live micro flow, EOC analysis).
3. **History** — resolved signal log with pair filter + postmortem.
4. **Settings** — Quotex SSID token input, preferences, legal, about.

## Optional API-key system

By default the app is fully open — anyone with the URL can view every chart,
signal, and theory reason. To gate write endpoints (POST /api/token, DELETE
/api/token, POST /api/subscribe) behind an API key, set the env var:

```
PLYBIT_API_KEYS=key1,key2,key3
```

When set, write requests must include `X-API-Key: <key>` header. Reads
(GET /api/*, /ws, /, /healthz) stay public.

## Deploy

Railway auto-deploys from `main`. Healthcheck at `/healthz`.

```
web: python -u -m uvicorn server:app --host 0.0.0.0 --port $PORT
```
