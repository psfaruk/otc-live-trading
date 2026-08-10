"""
Plybit AI — OTC live trading signal server.

Auth/signup/login fully removed: the app is now a single-tenant public
dashboard. Every visitor gets the full chart + signals feed with no
login wall, no admin panel, no per-user data.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import uvicorn

load_dotenv()

from feed import QuotexFeed

feed = QuotexFeed()
_clients: set[WebSocket] = set()


async def _broadcast(data: dict) -> None:
    """Fan-out one payload to every connected WS client. No tier filtering —
    every viewer now gets the full candle + prediction + microstructure
    payload (the old _tier_payload / _client_category machinery is gone
    along with the user accounts it existed to gate)."""
    if not _clients:
        return
    dead = set()
    # Snapshot: _clients mutates while we await sends (connect/disconnect),
    # iterating the live set raises "Set changed size during iteration" and
    # kills the feed loop that called us.
    for ws in list(_clients):
        try:
            await asyncio.wait_for(ws.send_json(data), timeout=2.0)
        except asyncio.TimeoutError:
            dead.add(ws)
            print("[ws] send timeout, disconnecting client")
        except Exception as e:
            dead.add(ws)
            if str(e) != "WebSocket is not connected":
                print(f"[ws] send error: {type(e).__name__}")
    if dead:
        _clients.difference_update(dead)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(feed.run(_broadcast))
    yield
    await feed.shutdown()
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
    """Open / refresh a stream — no auth, no tier trimming."""
    return await feed.ensure_stream(req.asset, req.period, req.cid)


@app.get("/api/pairs")
async def pairs():
    return feed.available_pairs()


@app.get("/api/stream-status")
async def stream_status():
    return feed.stream_status()


# ── Session token management (frontend → backend) ───────────────────────────
# Lets the operator paste an SSID cookie directly into the Settings page
# instead of redeploying on Railway every time the token expires. The token
# is stored in-memory only (feed._user_token) — never written to disk or
# env vars, and cleared on process restart.

class TokenReq(BaseModel):
    token: str = ""


@app.post("/api/token")
def set_token(req: TokenReq):
    """Store a user-supplied SSID token. Takes priority over QX_TOKEN env
    var on the next connect attempt. Pass an empty string to clear."""
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
        "message": "Token stored — reconnecting now. Check status in a few seconds.",
        "token_preview": token[:8] + "...",
    }


@app.get("/api/token-status")
def token_status():
    """Returns the current token state without exposing the token value.
    Polled by the frontend Settings page to show connect/disconnect status."""
    return feed.token_status()


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
        # Show presence (not value) of each auth env var so the user can
        # tell at a glance which auth path the feed is trying.
        "env": {
            "QX_EMAIL_set":    bool(os.environ.get("QX_EMAIL", "").strip()),
            "QX_PASSWORD_set": bool(os.environ.get("QX_PASSWORD", "").strip()),
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
        # The single most useful piece — tells the user exactly which auth
        # path to set up next.
        "hint": _debug_hint(feed),
    }


def _debug_hint(feed) -> str:
    import os
    if feed._connected:
        return "Feed is connected — live data should be streaming."
    # User-supplied token (from frontend) takes priority over env vars
    if feed.has_token():
        return ("User token is set but the WebSocket rejected it. The token "
                "may be expired — re-extract it from a fresh browser session "
                "and paste it into the Settings page.")
    if not (os.environ.get("QX_TOKEN", "").strip()
            or (os.environ.get("QX_EMAIL", "").strip()
                and os.environ.get("QX_PASSWORD", "").strip())):
        return ("No Quotex credentials set. Open the Settings page and paste "
                "your SSID token, OR set QX_TOKEN as a Railway env var.")
    if os.environ.get("QX_TOKEN", "").strip():
        return ("QX_TOKEN is set but the WebSocket rejected it. The token may "
                "be expired — re-extract it from a fresh browser session.")
    return ("QX_EMAIL/QX_PASSWORD are set but login is failing — this is "
            "almost always Cloudflare blocking the login page. Switch to "
            "QX_TOKEN (extract SSID cookie from a logged-in browser session).")



# NOTE: these four are deliberately plain `def`, not `async def` — they do
# synchronous sqlite work (db.py), and an async endpoint would run that ON
# the event loop, stalling every live stream's tick processing while a
# query runs. FastAPI executes sync endpoints in its threadpool instead;
# db.py's threading.Lock makes that safe.
@app.get("/api/stats")
def stats(asset: str | None = None, period: int | None = None):
    import db as _db
    s = _db.get_stats(asset, period)
    s["muted_theories"] = dict(feed._muted_theories)
    return s


@app.get("/api/theory-report")
def theory_report(asset: str | None = None, period: int | None = None):
    import db as _db
    return _db.theory_report(asset, period)


@app.get("/api/signals")
def signals(asset: str | None = None, period: int | None = None,
            limit: int = 50):
    """Recent resolved signals with full postmortem (why won / why lost)."""
    import db as _db
    return _db.get_signals(asset, period, limit)


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
