# otc-live-trading (Plybit AI)

Live OTC binary-options signal dashboard. Streams forex candles from Quotex,
runs a multi-theory price-action blend on every closed candle, and broadcasts
CALL/PUT signals with strength (STRONG / MEDIUM / WEAK) + a deep market-state
read (continuation / exhaustion / reversal / trap).

Public dashboard — **no signup, no login**. Every visitor sees every chart,
every signal, every theory reason.

## Required environment variables (Railway → Variables)

| Var | Purpose |
|-----|---------|
| `QX_EMAIL` | **Required.** Your Quotex account email. Without this, no live data. |
| `QX_PASSWORD` | **Required.** Your Quotex account password. |
| `QX_HOST` | Optional. Defaults to `qxbroker.co` (the working mirror). Switch only if Quotex changes the host again. |
| `QX_TOKEN` | Optional. A pre-extracted SSID token — fast path, skips email/password login. Useful if the account has 2FA enabled. |
| `QX_PAYOUT_FLOOR` | Optional. Min payout % for a pair to be streamable. Default `81`. |
| `QX_DB_PATH` | Optional. SQLite path. Set this to a persistent volume on Railway, otherwise the DB resets on every redeploy. |
| `QX_ROOT` | Optional. Browser profile cache dir. Defaults to a temp dir. |
| `QX_UA` | Optional. Override User-Agent string. |
| `PORT` | Set automatically by Railway. |

## How to set QX_EMAIL / QX_PASSWORD on Railway

1. Open your project on https://railway.com
2. Click the `otc-live-trading` service
3. Go to the **Variables** tab
4. Add two variables:
   - `QX_EMAIL` = your Quotex login email
   - `QX_PASSWORD` = your Quotex password
5. Railway will auto-redeploy with the new env vars.

If login still fails after setting both:
- Verify the email/password by logging in to https://qxbroker.co/en/sign-in manually in a browser
- If the account uses 2FA / OTP, log in once in a browser, extract the SSID cookie, and set `QX_TOKEN` instead of `QX_EMAIL`/`QX_PASSWORD`

## Pair list

The app streams exactly 15 pairs (whitelist-curated, see `_WANTED_PAIRS` in `feed.py`):

**OTC (11):** BRL/USD, USD/INR, USD/IDR, USD/COP, USD/BDT, USD/MXN, NZD/USD, USD/DZD, USD/PHP, USD/PKR, USD/ZAR

**Real (4):** USD/JPY, EUR/USD, GBP/USD, AUD/USD

## Signal guarantee

Every closed candle emits a CALL or PUT signal. Edge cases (zero-range candle, score=0, insufficient history) fall through to a coin-flip tiebreak that still emits a direction — marked WEAK strength. There is no NEUTRAL on screen.

## Deploy

Railway auto-deploys from `main`. Healthcheck at `/healthz`.

```
web: python -u -m uvicorn server:app --host 0.0.0.0 --port $PORT
```
