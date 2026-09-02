"""
Plybit AI — OTC live trading signal server.

Auth/signup/login fully removed: the app is a single-tenant public
dashboard. Every visitor gets the full chart + signals feed with no
login wall, no admin panel, no per-user data. The ONLY credential is
a Quotex SSID token pasted into the Settings tab — there is no API-key
system, no admin key, no email/password fallback (Cloudflare blocks
the HTTP sign-in page on every non-browser client, so retrying was
just hammering Quotex with bad credentials).

Read endpoints (GET /api/*, /ws, /, /healthz) are fully public.
Write endpoints (POST /api/token, DELETE /api/token, POST /api/subscribe)
are also public — the operator pastes a token and the feed connects
with it. There is nothing else to gate.
"""
import asyncio
import os
import time
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

load_dotenv()

from feed import QuotexFeed

feed = QuotexFeed()
_clients: set[WebSocket] = set()


async def _broadcast(data: dict) -> None:
    """Fan-out one payload to every connected WS client concurrently.
    Uses asyncio.gather with return_exceptions=True so a single slow
    client can't block the others (the old serial loop with a 2s timeout
    per client could stall the feed for N×2s on bad networks)."""
    if not _clients:
        return
    # Snapshot the set — _clients mutates while we await sends, and
    # iterating the live set raises "Set changed size during iteration".
    targets = list(_clients)
    async def _safe_send(ws: WebSocket) -> None:
        try:
            await asyncio.wait_for(ws.send_json(data), timeout=2.0)
        except Exception:
            # Swallowed — caller handles dead-client cleanup below.
            raise
    results = await asyncio.gather(
        *(_safe_send(ws) for ws in targets), return_exceptions=True)
    # Drop any client whose send raised.
    dead = {ws for ws, res in zip(targets, results) if isinstance(res, Exception)}
    if dead:
        _clients.difference_update(dead)
        print(f"[ws] dropped {len(dead)} dead client(s) "
              f"(total now {len(_clients)})")


async def _periodic_diagnosis(interval: int = 900) -> None:
    """Print the accuracy diagnosis to stdout every 15 minutes.

    The same data /api/diagnosis serves, but pushed to the logs so the
    performance picture is visible without a browser -- and so it survives
    in the log history even after the DB is rotated or the app restarts.
    """
    import db as _db
    while True:
        await asyncio.sleep(interval)
        try:
            d = _db.diagnosis(days=7)
            if not d["graded"]:
                print("[diag] no graded signals yet")
                continue
            _es = d.get("effective_sample", {})
            print(f"[diag] === {d['graded']} graded / {d['draws']} draws | "
                  f"overall {d['overall_accuracy']}% "
                  f"CI95 {d.get('overall_ci95')} | "
                  f"break-even {d['break_even_at_85pct_payout']}% ===")
            print(f"[diag] VERDICT: {d['verdict']}")
            print(f"[diag] effective sample: {_es.get('rows')} rows across "
                  f"{_es.get('distinct_timestamps')} timestamps x "
                  f"{_es.get('distinct_assets')} correlated pairs "
                  f"-> intervals above are OPTIMISTIC")
            # This warning existed in the API payload but was never printed,
            # so the log kept showing a blended by_strength with no hint that
            # a third of the rows had a poisoned strength column.
            if _es.get("warning"):
                print(f"[diag] WARNING: {_es['warning']}")
            # Only buckets whose whole interval clears break-even mean
            # anything. Printing the point estimate alone is what made a
            # 54.07% coin flip read as "PROFITABLE".
            for axis in ("by_strength", "by_tag", "by_regime", "by_signal"):
                parts = []
                for r in d[axis][:8]:
                    mark = "*" if r.get("beats_breakeven") else ""
                    ph = "!" if r.get("post_hoc") else ""
                    parts.append(f"{r['key']}{ph}={r['accuracy']}%"
                                 f"(n={r['n']},CI{r['ci95']}){mark}")
                print(f"[diag] {axis}: " + "  ".join(parts))
            print("[diag] legend: * = whole CI beats break-even (tradeable)  "
                  "! = post-hoc, derived from the outcome, NOT a filter")
            _live = d.get("by_strength_live") or []
            if _live:
                print("[diag] by_strength_live (LOOKAHEAD — display only): "
                      + "  ".join(f"{r['key']}={r['accuracy']}%(n={r['n']})"
                                  for r in _live[:8]))
        except Exception as exc:
            print(f"[diag] failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(feed.run(_broadcast))
    diag = asyncio.create_task(_periodic_diagnosis())
    yield
    await feed.shutdown()
    diag.cancel()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Plybit AI", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    """Railway healthcheck probe — deliberately data-free."""
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """Open WebSocket — no auth check. Every visitor is treated as a full
    viewer: they receive every candle/tick/prediction the feed emits."""
    await ws.accept()
    _clients.add(ws)
    cid    = ws.query_params.get("cid")
    asset  = ws.query_params.get("asset")
    period = ws.query_params.get("period")
    if asset and period and period.isdigit():
        snap = feed.snapshot(asset, int(period))
        if snap:
            await ws.send_json(snap)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
        if cid:
            await feed.drop_interest(cid)


from pydantic import BaseModel


class SubReq(BaseModel):
    asset: str = "EURUSD_otc"
    period: int = 60
    cid: str | None = None


@app.post("/api/subscribe")
async def subscribe(req: SubReq):
    """Open / refresh a stream. Public endpoint — no API key required.
    The only credential in the system is the Quotex SSID token (POST /api/token)."""
    return await feed.ensure_stream(req.asset, req.period, req.cid)


@app.get("/api/pairs")
async def pairs():
    return feed.available_pairs()


@app.get("/api/stream-status")
async def stream_status():
    return feed.stream_status()


# ── Session token management (frontend → backend) ───────────────────────
# Lets the operator paste an SSID cookie directly into the Settings page
# instead of redeploying on Railway every time the token expires. The token
# is stored in-memory only (feed._user_token) — never written to disk or
# env vars, and cleared on process restart. This is the ONLY credential
# in the system — there is no admin key, no API key, no email/password.

class TokenReq(BaseModel):
    token: str = ""


@app.post("/api/token")
def set_token(req: TokenReq):
    """Store a user-supplied SSID token. The token is the ONLY auth path —
    there is no email/password fallback. The feed does exactly ONE connect
    attempt with this token; if it fails for ANY reason, the operator must
    paste a fresh SSID (or the same one again) to retry once. No automatic
    retry."""
    token = (req.token or "").strip()
    if not token:
        return {"ok": False, "error": "Token is empty"}
    # Basic sanity check — Quotex SSID tokens are long JWT-like strings.
    # Reject obviously wrong inputs (too short, contains spaces, etc.)
    # without being so strict that a valid token gets rejected.
    if len(token) < 20:
        return {"ok": False, "error": "Token looks too short — a valid Quotex SSID is typically 100+ characters"}
    if " " in token or "\n" in token:
        return {"ok": False, "error": "Token contains whitespace — copy just the cookie value, no spaces or line breaks"}
    feed.set_token(token)
    return {
        "ok": True,
        "message": "Token stored — connecting now (single attempt, no auto-retry on failure).",
    }


@app.get("/api/token-status")
def token_status():
    """Returns the current token state without exposing the token value.
    Polled by the frontend Settings page to show connect/disconnect status."""
    return feed.token_status()


@app.get("/api/server-time")
def server_time():
    """Returns the Quotex broker's current Unix timestamp (seconds) and the
    local server's timestamp. The frontend uses the broker time to sync its
    candle countdown so it matches the broker's period rollover EXACTLY —
    no ms drift from local NTP skew. Falls back to local time when the
    feed isn't connected yet."""
    import time
    broker_ts = feed._broker_time()
    return {
        "broker_time":  int(broker_ts),
        "local_time":   int(time.time()),
        "offset_ms":    int((broker_ts - time.time()) * 1000),
        "connected":    feed._connected,
    }


@app.delete("/api/token")
def clear_token():
    """Clear a previously-set user token. The feed will fall back to
    QX_TOKEN env var (if set) or disconnect entirely."""
    had_token = feed.has_token()
    feed.set_token("")  # empty string clears it
    return {
        "ok": True,
        "cleared": had_token,
        "message": "Token cleared." if had_token else "No user token was set.",
    }


@app.get("/api/debug")
async def debug():
    """Public diagnostic endpoint — shows exactly why the feed isn't
    connecting. Returns the env-var state (without leaking secrets), the
    Quotex client connection state, and the last connect error if any.
    Use this when the dashboard is stuck on 'Waiting'."""
    import os
    return {
        # Show presence (not value) of QX_TOKEN only — email/password are
        # no longer auth paths (Cloudflare blocks the HTTP sign-in page).
        "env": {
            "QX_TOKEN_set":    bool(os.environ.get("QX_TOKEN", "").strip()),
            "QX_HOST":         os.environ.get("QX_HOST", "qxbroker.com (default)"),
        },
        "feed": {
            "connected":          feed._connected,
            "reconnect_attempts": feed._reconnect_attempts,
            "client_created":     feed._client is not None,
            "active_streams":     len(feed._streams),
            "pairs_loaded":       len(feed._pairs_list),
        },
        "token": feed.token_status(),
        # The single most useful piece — tells the user exactly what to do next.
        "hint": _debug_hint(feed),
    }


def _debug_hint(feed) -> str:
    import os
    if feed._connected:
        return "Feed is connected — live data should be streaming."
    # One-shot fail gate is the most actionable signal — tells the operator
    # they need to paste a fresh SSID (the system is NOT retrying on its own).
    if feed._login_failed_permanently:
        return (f"Login failed and is NOT being retried: "
                f"{feed._login_fail_reason} "
                f"Open the Settings page and paste a fresh SSID to retry once.")
    # User-supplied token (from frontend) takes priority over env vars
    if feed.has_token():
        return ("User token is set but the feed has not connected yet. "
                "If it stays disconnected, re-extract the SSID from a fresh "
                "browser session and paste it into the Settings page.")
    if not (os.environ.get("QX_TOKEN", "").strip()
            or feed.has_token()):
        return ("No Quotex SSID set. Open the Settings page and paste your "
                "SSID token (extract it from a logged-in browser session — "
                "see the help link on the Settings page). There is no "
                "email/password fallback (Cloudflare blocks the HTTP sign-in).")
    if os.environ.get("QX_TOKEN", "").strip():
        return ("QX_TOKEN env var is set but the feed has not connected yet. "
                "If it stays disconnected, re-extract the SSID from a fresh "
                "browser session and paste it into the Settings page.")
    return "Unexpected state — check the logs for details."



# NOTE: these four are deliberately plain `def`, not `async def` — they do
# synchronous sqlite work (db.py), and an async endpoint would run that ON
# the event loop, stalling every live stream's tick processing while a
# query runs. FastAPI executes sync endpoints in its threadpool instead;
# db.py's threading.Lock makes that safe.
#
# /api/stats and /api/theory-report each do a full-table scan over
# signal_log/theory_votes (db.py:423-487, 490-525). The Home tab polls
# /api/stats every 10s from EVERY visitor (chart.js:1637-1640), so without
# caching, N concurrent visitors means N full scans every 10s, each holding
# db.py's global lock — the exact contention pattern that stalls live-stream
# processing under load. A short TTL cache collapses that back down to one
# scan per TTL window regardless of visitor count; the underlying data only
# changes once per candle close (60s) anyway, so 5s freshness costs nothing
# real.
_STATS_CACHE_TTL = 5.0
_stats_cache: dict[tuple, tuple[float, dict]] = {}


def _cached(cache: dict, key: tuple, ttl: float, compute):
    now = time.time()
    hit = cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = compute()
    cache[key] = (now, value)
    return value


@app.get("/api/stats")
def stats(asset: str | None = None, period: int | None = None,
          days: int | None = None):
    """Canonical win-rate source. `days` (1/3/7/30) scopes the window so the
    headline number matches the Analytics tab instead of being a lifetime
    blend that no other surface agrees with."""
    import db as _db

    def _compute():
        s = _db.get_stats(asset, period, days=days)
        s["muted_theories"] = dict(feed._muted_theories)
        return s
    return _cached(_stats_cache, ("stats", asset, period, days), _STATS_CACHE_TTL, _compute)


@app.get("/api/theory-report")
def theory_report(asset: str | None = None, period: int | None = None):
    import db as _db
    return _cached(_stats_cache, ("theory-report", asset, period), _STATS_CACHE_TTL,
                   lambda: _db.theory_report(asset, period))


@app.get("/api/signals")
def signals(asset: str | None = None, period: int | None = None,
            limit: int = 50, direction: str | None = None,
            result: str | None = None):
    """Recent resolved signals with full postmortem (why won / why lost).

    direction: "CALL" or "PUT" — show only that side (the user's explicit
    requirement: Call ও put সিগন্যালগুলো আলাদা আলাদা দেখা)।
    result: "correct" | "wrong" | "draw" | "no_data" | "pending" — outcome filter.
    """
    import db as _db
    return _db.get_signals(asset, period, limit,
                           direction=direction, result=result)


@app.get("/api/share-signals")
def share_signals():
    """Live signal table for all 16 pairs. One row per pair — the latest
    closed-candle prediction + buyer/seller pressure. Polled by the Share
    Signal tab every 10s while active."""
    return {"signals": feed.get_share_signals()}


# ── OPEN API: anyone can fetch live signals without authentication ──────────
# These endpoints are deliberately PUBLIC (no API-key gate) so the app's
# Call/Put signals can be consumed by external scripts, bots, dashboards,
# or anyone with the URL. The original design already had open read access
# for /api/share-signals — these endpoints expose the same data in formats
# optimized for automated consumption (single pair, JSON, plaintext).

@app.get("/api/v1/signals")
def api_v1_signals(asset: str | None = None):
    """Open API: get the current signal for one pair, or all pairs.

    Usage:
      GET /api/v1/signals                  -> all pairs (array)
      GET /api/v1/signals?asset=EURUSD_otc -> one pair

    Response shape per pair:
      {
        "asset": "EURUSD_otc",
        "display": "EUR/USD",
        "type": "otc",
        "time": 1700000000,         # Unix timestamp of signal
        "signal": "CALL",           # CALL | PUT | NEUTRAL
        "strength": "MEDIUM",       # STRONG | MEDIUM | WEAK | NONE
        "confidence": 0.65,         # 0..1
        "score": 4,                 # signed vote sum
        "agree": 3,                 # # theories agreeing
        "buy_pct": 65,              # last closed candle buyer %
        "sell_pct": 35,
        "prediction_candle": {"open":1.0850,"high":1.0855,"low":1.0845,"close":1.0854},
        "patterns_fired": ["BULLISH_ENGULFING","MARKET_STATE"],
        "archetype": "MIXED",       # pair archetype
        "session_quality": 1.0,     # 0..1 timing quality
        "candle_expires_in": 45,    # seconds until candle close (if known)
      }
    """
    rows = feed.get_share_signals()
    if asset:
        rows = [r for r in rows if r["asset"] == asset]
    # Enrich with strategy-engine metadata
    import time as _time
    out = []
    for r in rows:
        stream = feed._streams.get((r["asset"], 60))
        candle_expires = None
        patterns_fired = []
        archetype = None
        session_q = None
        score = None
        agree = None
        if stream:
            now = feed._broker_time()
            if stream.candle_open_time > 0:
                # FIX: hardcoded +60 assumed 1-minute candles. Use the
                # stream's actual period so 30s/300s/900s candles report
                # the correct expiry to /api/v1/signals consumers.
                candle_expires = max(0, int(stream.candle_open_time
                                            + stream.period - now))
            pred = stream.prediction or {}
            patterns_fired = pred.get("patterns_fired", [])
            ctx = pred.get("context") or {}
            archetype = ctx.get("archetype")
            session_q = ctx.get("session_quality")
            score = pred.get("score")
            agree = pred.get("agree")
        out.append({
            "asset": r["asset"],
            "display": r.get("display"),
            "type": r.get("type"),
            "time": r.get("time"),
            "signal": r.get("signal"),
            "strength": r.get("strength"),
            "confidence": r.get("confidence"),
            "score": score,
            "agree": agree,
            # Confluence council detail: which voters agreed, and why a
            # NEUTRAL ("NO TRADE") blocked the signal.
            "voters": (stream.prediction or {}).get("voters", []) if stream else [],
            "confluence": (stream.prediction or {}).get("confluence", {}) if stream else {},
            "buy_pct": r.get("buy_pct"),
            "sell_pct": r.get("sell_pct"),
            "prediction_candle": r.get("prediction_candle"),
            "patterns_fired": patterns_fired,
            "archetype": archetype,
            "session_quality": session_q,
            "candle_expires_in": candle_expires,
            "server_time": int(_time.time()),
        })
    return {"count": len(out), "signals": out}


@app.get("/api/v1/signals/{asset}")
def api_v1_signal_one(asset: str):
    """Open API: get the current signal for a single pair by asset code.

    Example: GET /api/v1/signals/EURUSD_otc

    Returns WAIT/NEUTRAL with reason if the stream is not active yet.
    """
    rows = feed.get_share_signals()
    row = next((r for r in rows if r["asset"] == asset), None)
    if not row:
        # Stream not active OR pairs list not yet loaded from Quotex.
        # Return a NEUTRAL "WAIT" response so consumers don't 500.
        from strategies import get_profile
        try:
            p = get_profile(asset)
            return {
                "asset": asset,
                "display": p.display,
                "type": "otc" if asset.endswith("_otc") else "real",
                "time": None,
                "signal": "NEUTRAL",
                "strength": "NONE",
                "confidence": 0.0,
                "score": None,
                "agree": 0,
                "buy_pct": None,
                "sell_pct": None,
                "prediction_candle": None,
                "patterns_fired": [],
                "archetype": p.archetype,
                "session_quality": None,
                "candle_expires_in": None,
                "server_time": int(__import__('time').time()),
                "note": "Stream not active — pair is not currently streaming. "
                        "Call POST /api/subscribe to start the stream.",
            }
        except Exception:
            raise HTTPException(status_code=404,
                                detail=f"Asset '{asset}' not found in pair catalog")
    # Reuse the list endpoint's enrichment logic
    return api_v1_signals(asset=asset)


@app.get("/api/v1/signals/{asset}/plain")
def api_v1_signal_plain(asset: str):
    """Open API: get the current signal as a single-line plain-text response,
    ideal for quick `curl` checks or simple bots.

    Format:
      EURUSD_otc CALL MEDIUM conf=0.65 score=4 expires_in=45
    """
    rows = feed.get_share_signals()
    row = next((r for r in rows if r["asset"] == asset), None)
    if not row:
        # Fall back to NEUTRAL if no stream active yet — never 404.
        from strategies import get_profile
        try:
            get_profile(asset)
        except Exception:
            raise HTTPException(status_code=404,
                                detail=f"Asset '{asset}' not found in catalog")
        return PlainTextResponse(f"{asset} NEUTRAL NONE conf=0.00 expires_in=?\n")
    stream = feed._streams.get((asset, 60))
    expires = "?"
    if stream and stream.candle_open_time > 0:
        now = feed._broker_time()
        expires = max(0, int(stream.candle_open_time + 60 - now))
    sig = row.get("signal") or "WAIT"
    strength = row.get("strength") or "-"
    conf = row.get("confidence")
    conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
    return PlainTextResponse(
        f"{asset} {sig} {strength} conf={conf_str} expires_in={expires}\n")


@app.get("/api/v1/pairs")
def api_v1_pairs():
    """Open API: list all tradeable pairs with their per-pair profile
    (archetype, best windows, notes). Useful for automated bots to know
    which pairs to subscribe to."""
    from strategies import list_profiles
    return {
        "count": len(list_profiles()),
        "pairs": [
            {
                "asset": p.asset,
                "display": p.display,
                "archetype": p.archetype,
                "best_windows_utc": p.best_windows_utc,
                "notes": p.notes,
                "trap_wick_sensitivity": p.trap_wick_sensitivity,
                "continuation_edge": p.continuation_edge,
                "min_atr_pct": p.min_atr_pct,
            } for p in list_profiles()
        ],
    }


@app.get("/api/v1/strategies")
def api_v1_strategies():
    """Open API: list all candlestick pattern detectors available in the
    strategy engine. Each entry documents the pattern's name, expected
    direction, and reliability rating from web research."""
    from strategies.patterns import (
        SINGLE_CANDLE_DETECTORS, TWO_CANDLE_DETECTORS,
        THREE_CANDLE_DETECTORS,
        rising_three_methods, falling_three_methods,
        ladder_bottom, ladder_top,
    )
    detectors = []
    for fn in SINGLE_CANDLE_DETECTORS:
        detectors.append({"name": fn.__name__, "type": "single"})
    for fn in TWO_CANDLE_DETECTORS:
        detectors.append({"name": fn.__name__, "type": "two"})
    for fn in THREE_CANDLE_DETECTORS:
        detectors.append({"name": fn.__name__, "type": "three"})
    for fn in (rising_three_methods, falling_three_methods,
               ladder_bottom, ladder_top):
        detectors.append({"name": fn.__name__, "type": "four_plus"})
    return {"count": len(detectors), "detectors": detectors}


@app.get("/api/v1/backtest")
def api_v1_backtest(asset: str | None = None):
    """Open API: return the latest backtest report (if generated).
    Run `python tools/backtest_strategies.py --all` to regenerate."""
    import os, json
    # FIX: was a hardcoded absolute path that only works on one dev machine.
    # On Railway or any other deploy the file never exists there, so the
    # endpoint always returned {"available": False}. Now respects the
    # BACKTEST_REPORT_PATH env var and falls back to ./backtest_report.json
    # (relative to the project root) so it works in any deployment.
    path = os.environ.get("BACKTEST_REPORT_PATH") or "backtest_report.json"
    if not os.path.exists(path):
        return {"available": False,
                "message": "Run `python tools/backtest_strategies.py --all` to generate"}
    with open(path) as f:
        data = json.load(f)
    if asset:
        # FIX: build a new dict instead of mutating the loaded one in-place.
        # The old code mutated `data["per_pair"]` which would add an empty
        # key if it was missing, and would corrupt the cache if caching
        # was ever added.
        return {**data, "per_pair": [p for p in data.get("per_pair", [])
                                       if p["asset"] == asset]}
    return data


@app.get("/api/diagnosis")
def diagnosis(days: int = 7, asset: str | None = None):
    """Why are the predictions wrong — accuracy sliced by strength, tag,
    regime, zone, asset and hour, with the payout break-even included."""
    import db as _db
    return _cached(_stats_cache, ("diagnosis", days, asset),
                   _STATS_CACHE_TTL, lambda: _db.diagnosis(days, asset))


@app.get("/api/pair-winrate")
def pair_winrate(days: int = 7, period: int = 60):
    """Per-pair win rate for the sidebar, with sample size and a 95% Wilson
    interval judged against the payout break-even (not 50%).

    Cached like the other stats endpoints: every open tab polls this, and the
    underlying numbers only move once per closed candle."""
    import db as _db
    return _cached(_stats_cache, ("pair_winrate", days, period),
                   _STATS_CACHE_TTL, lambda: _db.pair_winrate(days, period))


@app.get("/api/winrate-calls")
def winrate_calls(days: int = 7, period: int = 60):
    """Per-pair CALL vs PUT win rates + overall buckets.

    The user's explicit requirement: "Call ও put কোনো সিগন্যাল গুলো কেমন
    win রেট দিচ্ছে। সেই গুলো আলাদা আলাদা করে নিজের মতো করে দেখতে পারবো।"
    Backs the Analytics tab's per-direction breakdown. Cached like the
    other stats endpoints (numbers only move once per candle close)."""
    import db as _db
    return _cached(_stats_cache, ("winrate_calls", days, period),
                   _STATS_CACHE_TTL,
                   lambda: _db.direction_winrate(days, period))


@app.get("/api/theory-perf")
def theory_perf(asset: str | None = None, period: int | None = None,
                days: int = 7):
    """Live per-theory accuracy — the data feeding the disable gate."""
    import db as _db
    return _db.theory_perf(asset, period, days=days)


class NoCacheStaticFiles(StaticFiles):
    """Force browsers to revalidate on every load instead of using their own
    heuristic cache — this app's static files change constantly during
    development and a stale chart.js/index.html in the browser looks
    identical to a real bug."""
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", NoCacheStaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    # Railway (and most PaaS hosts) assign the port dynamically via $PORT —
    # 8000 stays the local-dev default when that's unset.
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
