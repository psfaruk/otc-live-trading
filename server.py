import asyncio
import base64
import hmac
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
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
APP_USERNAME = os.environ.get("APP_USERNAME", "plybit")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


def _check_basic_auth(header_value: str | None) -> bool:
    if not APP_PASSWORD:
        return True
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return False
    return (hmac.compare_digest(username, APP_USERNAME)
            and hmac.compare_digest(password, APP_PASSWORD))


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
async def basic_auth_middleware(request, call_next):
    # Covers everything: the static-file mount (index.html/chart.js/style.css)
    # AND every /api/* route, since it runs before routing decides which one
    # handles the request.
    if _check_basic_auth(request.headers.get("authorization")):
        return await call_next(request)
    return Response(status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Plybit AI"'})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    # Browsers resend the SAME cached Basic-Auth header on the WS handshake
    # once the page itself required it — checked here since @app.middleware
    # ("http") only wraps HTTP routes, not the WebSocket protocol.
    if not _check_basic_auth(ws.headers.get("authorization")):
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
