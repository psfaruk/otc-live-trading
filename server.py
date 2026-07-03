import asyncio
import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

load_dotenv()

from feed import QuotexFeed

feed = QuotexFeed()
_clients: set[WebSocket] = set()

# ── Access gate ───────────────────────────────────────────────────────────
# Single shared password for the whole site (no per-user accounts — matches
# the rest of this app's "one shared feed, many anonymous viewers" model).
# APP_PASSWORD unset => gate disabled (local dev doesn't need a login).
# Cookie-based (not HTTP Basic) so the login screen is our own styled page
# (static/login.html) instead of the browser's unstylable native popup.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
_COOKIE_NAME = "plybit_auth"


def _session_token() -> str:
    # Derived from the password: changing APP_PASSWORD invalidates every
    # previously-issued cookie at once, with no server-side session store.
    return hmac.new(APP_PASSWORD.encode(), b"plybit-session-v1",
                    hashlib.sha256).hexdigest()


def _cookie_ok(cookie_header: str | None) -> bool:
    if not APP_PASSWORD:
        return True
    if not cookie_header:
        return False
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == _COOKIE_NAME:
            return hmac.compare_digest(value, _session_token())
    return False


# Paths reachable WITHOUT a session: the health probe, the login page
# itself, the login API it posts to, and the brand logo the login page
# displays (it renders BEFORE authentication, so its image can't sit
# behind the gate). Everything else on the login page is inline.
_OPEN_PATHS = {"/healthz", "/login", "/api/login", "/logo.png"}


async def _broadcast(data: dict) -> None:
    dead = set()
    # Snapshot: _clients mutates while we await sends (connect/disconnect),
    # iterating the live set raises "Set changed size during iteration" and
    # kills the feed loop that called us.
    for ws in list(_clients):
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
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


@app.middleware("http")
async def auth_middleware(request, call_next):
    # Covers everything: the static-file mount (index.html/chart.js/style.css)
    # AND every /api/* route, since it runs before routing decides which one
    # handles the request. /healthz stays open — Railway's healthcheck can't
    # authenticate, and gating it made password-protected deployments get
    # marked unhealthy (observed live).
    if (request.url.path in _OPEN_PATHS
            or _cookie_ok(request.headers.get("cookie"))):
        return await call_next(request)
    # Page loads go to the styled login screen; API calls get a plain 401
    # (a redirect would just confuse fetch() callers).
    if request.method == "GET" and not request.url.path.startswith("/api"):
        return RedirectResponse("/login", status_code=302)
    return Response(status_code=401)


@app.get("/healthz")
async def healthz():
    # Deliberately data-free (no pair list, no stats) so leaving it open
    # reveals nothing — just "the process is up and serving HTTP".
    return {"ok": True}


@app.get("/login")
async def login_page():
    resp = FileResponse("static/login.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


class LoginReq(BaseModel):
    password: str = ""


@app.post("/api/login")
async def login(req: LoginReq):
    if not APP_PASSWORD or hmac.compare_digest(req.password, APP_PASSWORD):
        resp = Response(status_code=200, content='{"ok":true}',
                        media_type="application/json")
        resp.set_cookie(
            _COOKIE_NAME, _session_token(),
            max_age=30 * 86400, httponly=True, samesite="lax")
        return resp
    return Response(status_code=401, content='{"ok":false}',
                    media_type="application/json")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    # Cookies ride along on the WS handshake automatically (same-origin),
    # so the same session check works here — checked explicitly since
    # @app.middleware("http") doesn't wrap the WebSocket protocol.
    if not _cookie_ok(ws.headers.get("cookie")):
        await ws.close(code=1008)
        return
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


class SubReq(BaseModel):
    asset: str = "EURUSD_otc"
    period: int = 60
    cid: str | None = None


@app.post("/api/subscribe")
async def subscribe(req: SubReq):
    return await feed.ensure_stream(req.asset, req.period, req.cid)


@app.get("/api/pairs")
async def pairs():
    return feed.available_pairs()


@app.get("/api/stream-status")
async def stream_status():
    return feed.stream_status()


# NOTE: these four are deliberately plain `def`, not `async def` — they do
# synchronous sqlite work (db.py), and an async endpoint would run that ON
# the event loop, stalling every live stream's tick processing while a
# query runs. FastAPI executes sync endpoints in its threadpool instead;
# db.py's threading.Lock makes that safe.
@app.get("/api/stats")
def stats(asset: str | None = None, period: int | None = None):
    import db as _db
    return _db.get_stats(asset, period)


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
    identical to a real bug (several sessions were lost to this)."""
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
