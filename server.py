import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

load_dotenv()

import db as _db
from feed import QuotexFeed

feed = QuotexFeed()
_clients: set[WebSocket] = set()
# Populated at /ws accept, read by _broadcast — lets the shared fan-out send
# a different (full vs tier-trimmed) payload per socket without feed.py
# knowing anything about user accounts. See _tier_payload below.
_client_category: dict[WebSocket, str] = {}

# ── Access gate — real per-user accounts (email + password), 3 categories:
# normal / premium / admin. Replaces the old single-shared-password gate.
#
# SESSION_SECRET signs the identity cookie; it must be a STABLE value you
# set yourself (Railway env var) — unlike a per-process random fallback,
# a stable secret means sessions survive a redeploy (this app redeploys
# often). If unset, a random secret is generated for this process only —
# fine for local dev, but every restart logs everyone out.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    print("[auth] SESSION_SECRET not set — using a random per-process secret "
          "(sessions will NOT survive a restart/redeploy). Set SESSION_SECRET "
          "in your environment for real deployments.")

_COOKIE_NAME = "plybit_session"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _sign(user_id: int, token_version: int) -> str:
    msg = f"{user_id}:{token_version}"
    return hmac.new(SESSION_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _make_cookie_value(user_id: int, token_version: int) -> str:
    return f"{user_id}:{token_version}:{_sign(user_id, token_version)}"


def _parse_cookie_value(cookie_header: str | None) -> tuple[int, int] | None:
    """Verify the signature and return (user_id, token_version) — pure
    compute, no DB access, so this stays as fast as the old static-HMAC
    check even though sessions are now per-user. Actual authorization
    (does this user/token_version still exist right now) is checked
    separately, once per request, in auth_middleware — see its comment
    for why that check needs a DB read and this one deliberately doesn't."""
    if not cookie_header:
        return None
    value = None
    for part in cookie_header.split(";"):
        name, _, v = part.strip().partition("=")
        if name == _COOKIE_NAME:
            value = v
            break
    if not value:
        return None
    try:
        uid_s, tv_s, sig = value.split(":", 2)
        user_id, token_version = int(uid_s), int(tv_s)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(user_id, token_version)):
        return None
    return user_id, token_version


# Paths reachable WITHOUT a session: the health probe, the login page and
# its signup/login APIs, and the brand logo the login page displays (it
# renders BEFORE authentication, so its image can't sit behind the gate).
_OPEN_PATHS = {"/healthz", "/login", "/api/login", "/api/signup", "/logo.png"}


async def _broadcast(data: dict) -> None:
    dead = set()
    # Both payload variants are built at most once per broadcast (not once
    # per client) — feed.py stays completely unaware of user accounts;
    # _tier_payload is the one place that knows what 'normal' can't see.
    reduced = None
    # Snapshot: _clients mutates while we await sends (connect/disconnect),
    # iterating the live set raises "Set changed size during iteration" and
    # kills the feed loop that called us.
    for ws in list(_clients):
        category = _client_category.get(ws, "normal")
        if category == "normal":
            if reduced is None:
                reduced = _tier_payload(data, category)
            payload = reduced
        else:
            payload = data
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


def _tier_payload(data: dict, category: str) -> dict:
    """The one place that trims candle/prediction/microstructure data for
    'normal' accounts — applied at every point this data reaches a client
    (the WS initial snapshot, every WS broadcast via _client_category, and
    /api/subscribe's response). 'premium' and 'admin' always get the
    untrimmed dict unchanged. Covers both message shapes feed.py emits:
    'snapshot'/'eoc' (candles + prediction) and 'tick' (micro +
    running_conf, and occasionally a re-anchored prediction)."""
    if category in ("premium", "admin"):
        return data
    out = dict(data)
    if isinstance(out.get("candles"), list):
        out["candles"] = out["candles"][-100:]
    pred = out.get("prediction")
    if isinstance(pred, dict):
        pred = dict(pred)
        pred.pop("reasons", None)
        pred.pop("key_levels", None)
        pred.pop("wick_walls", None)
        pred.pop("market_state", None)
        out["prediction"] = pred
    out.pop("micro", None)
    out.pop("running_conf", None)
    return out


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


async def _authenticate(request: Request) -> dict | None:
    """Verify the cookie's signature (pure compute), then confirm — via ONE
    indexed DB read — that the account still exists AND this exact
    token_version hasn't been revoked. Done here, once per request, rather
    than left to individual route handlers: that's what closes both gaps a
    per-route check would leave open — a route that forgets the check
    would otherwise silently skip authorization entirely, and a deleted
    account's still-correctly-SIGNED cookie would keep passing everywhere
    that never separately re-checks it. asyncio.to_thread is required
    (not a bare call) because this middleware is on the same event loop
    that also drives feed.py's live tick processing — see the sync-def
    convention on the /api/stats-style endpoints below for the same reason."""
    parsed = _parse_cookie_value(request.headers.get("cookie"))
    if not parsed:
        return None
    return await asyncio.to_thread(_db.get_user_for_session, *parsed)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Covers everything: the static-file mount (index.html/chart.js/style.css)
    # AND every /api/* route, since it runs before routing decides which one
    # handles the request. /healthz stays open — Railway's healthcheck can't
    # authenticate, and gating it made password-protected deployments get
    # marked unhealthy (observed live).
    if request.url.path in _OPEN_PATHS:
        return await call_next(request)
    user = await _authenticate(request)
    if user:
        # Route handlers read these instead of re-querying the DB — e.g.
        # the /api/admin/* routes just check request.state.category.
        request.state.user_id    = user["id"]
        request.state.email      = user["email"]
        request.state.category   = user["category"]
        request.state.created_at = user["created_at"]
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


def _set_session_cookie(response: Response, user: dict) -> None:
    response.set_cookie(
        _COOKIE_NAME, _make_cookie_value(user["id"], user["token_version"]),
        max_age=30 * 86400, httponly=True, samesite="lax")


class SignupReq(BaseModel):
    email: str = ""
    password: str = ""


@app.post("/api/signup")
def signup(req: SignupReq, response: Response):
    # sync def, not async: hash_password() runs PBKDF2 at 200k iterations —
    # genuinely expensive blocking CPU work. Same reasoning as the
    # /api/stats-style endpoints further down: FastAPI runs sync endpoints
    # in its threadpool, so this can't stall feed.py's live tick processing
    # the way a blocking call inside an async def would.
    email = req.email.strip()
    if not _EMAIL_RE.match(email):
        response.status_code = 400
        return {"ok": False, "error": "Enter a valid email address"}
    if len(req.password) < 8:
        response.status_code = 400
        return {"ok": False, "error": "Password must be at least 8 characters"}
    user = _db.create_user(email, req.password)
    if not user:
        response.status_code = 409
        return {"ok": False, "error": "That email is already registered"}
    _set_session_cookie(response, user)
    return {"ok": True, "email": user["email"], "category": user["category"]}


class LoginReq(BaseModel):
    email: str = ""
    password: str = ""


@app.post("/api/login")
def login(req: LoginReq, response: Response):
    user = _db.verify_login(req.email, req.password)
    if not user:
        response.status_code = 401
        return {"ok": False, "error": "Wrong email or password"}
    _set_session_cookie(response, user)
    return {"ok": True, "email": user["email"], "category": user["category"]}


@app.post("/api/logout")
async def logout():
    # The cookie only ever proves identity (see _parse_cookie_value) — this
    # clears it from THIS browser only. Bumping the user's token_version
    # (db.py — no UI calls this yet) is the actual revoke-everywhere lever.
    resp = Response(status_code=200, content='{"ok":true}',
                    media_type="application/json")
    resp.delete_cookie(_COOKIE_NAME)
    return resp


@app.get("/api/me")
async def me(request: Request):
    # Populated by auth_middleware — no extra DB call needed here.
    return {"email": request.state.email,
            "category": request.state.category,
            "created_at": request.state.created_at}


class PasswordChangeReq(BaseModel):
    current: str = ""
    new: str = ""


@app.post("/api/account/password")
def change_password(req: PasswordChangeReq, request: Request,
                    response: Response):
    # sync def: two PBKDF2 runs (verify old + hash new) — same threadpool
    # reasoning as /api/signup.
    if len(req.new) < 8:
        response.status_code = 400
        return {"ok": False,
                "error": "New password must be at least 8 characters"}
    user = _db.change_password(request.state.user_id, req.current, req.new)
    if not user:
        response.status_code = 401
        return {"ok": False, "error": "Current password is wrong"}
    # change_password just bumped token_version (revoking every issued
    # cookie) — re-issue THIS session's so only other devices sign out.
    _set_session_cookie(response, user)
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    # Cookies ride along on the WS handshake automatically (same-origin),
    # so the same session check works here — checked explicitly since
    # @app.middleware("http") doesn't wrap the WebSocket protocol.
    parsed = _parse_cookie_value(ws.headers.get("cookie"))
    user = await asyncio.to_thread(_db.get_user_for_session, *parsed) if parsed else None
    if not user:
        await ws.close(code=1008)
        return
    await ws.accept()
    _clients.add(ws)
    _client_category[ws] = user["category"]
    cid    = ws.query_params.get("cid")
    asset  = ws.query_params.get("asset")
    period = ws.query_params.get("period")
    if asset and period and period.isdigit():
        snap = feed.snapshot(asset, int(period))
        if snap:
            await ws.send_json(_tier_payload(snap, user["category"]))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
        _client_category.pop(ws, None)
        if cid:
            await feed.drop_interest(cid)


class SubReq(BaseModel):
    asset: str = "EURUSD_otc"
    period: int = 60
    cid: str | None = None


@app.post("/api/subscribe")
async def subscribe(req: SubReq, request: Request):
    result = await feed.ensure_stream(req.asset, req.period, req.cid)
    return _tier_payload(result, request.state.category)


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
    s = _db.get_stats(asset, period)
    # Live mute-gate state (feed-side, not in the DB) — lets the UI mark
    # theories the feedback loop has currently benched.
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


def _require_admin(request: Request) -> Response | None:
    """Shared 403 check for the /api/admin/* routes below. request.state.category
    is already fresh for THIS request (set by auth_middleware's DB read) —
    no extra query needed here."""
    if request.state.category != "admin":
        return Response(status_code=403, content='{"ok":false,"error":"Admin only"}',
                        media_type="application/json")
    return None


@app.get("/api/admin/users")
def admin_list_users(request: Request):
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    return {"users": _db.list_users()}


class CategoryReq(BaseModel):
    category: str = ""


@app.post("/api/admin/users/{user_id}/category")
def admin_set_category(user_id: int, req: CategoryReq, request: Request):
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    if not _db.set_user_category(user_id, req.category):
        return Response(status_code=400,
                        content='{"ok":false,"error":"Unknown user or category"}',
                        media_type="application/json")
    return {"ok": True}


@app.get("/api/admin/analytics")
def admin_analytics(request: Request):
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    return {
        "live_viewers": len(_clients),
        "streams": feed.stream_status(),
        "stats": _db.get_stats(),
    }


# ── Promos + notifications (admin CMS, redesign Phase 4) ─────────────────────

_CMS_TARGETS = {"all", "normal", "premium", "admin"}


class PromoReq(BaseModel):
    title: str = ""
    body: str = ""
    code: str = ""
    target: str = "all"
    days: int = 0        # promo lifetime in days; 0 = no expiry


@app.get("/api/admin/promos")
def admin_list_promos(request: Request):
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    return {"promos": _db.list_promos_admin()}


@app.post("/api/admin/promos")
def admin_create_promo(req: PromoReq, request: Request, response: Response):
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    title = req.title.strip()
    if not title or req.target not in _CMS_TARGETS:
        response.status_code = 400
        return {"ok": False, "error": "Title and a valid target are required"}
    ends_at = int(time.time()) + req.days * 86400 if req.days > 0 else None
    pid = _db.create_promo(title, req.body.strip(),
                           req.code.strip() or None, req.target, ends_at)
    return {"ok": True, "id": pid}


class PromoActiveReq(BaseModel):
    active: bool = True


@app.post("/api/admin/promos/{promo_id}/active")
def admin_promo_active(promo_id: int, req: PromoActiveReq, request: Request):
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    return {"ok": _db.set_promo_active(promo_id, req.active)}


@app.delete("/api/admin/promos/{promo_id}")
def admin_delete_promo(promo_id: int, request: Request):
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    return {"ok": _db.delete_promo(promo_id)}


class NoticeReq(BaseModel):
    title: str = ""
    body: str = ""
    target: str = "all"


@app.get("/api/admin/notices")
def admin_list_notices(request: Request):
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    return {"notices": _db.list_notices_admin()}


@app.post("/api/admin/notices")
async def admin_create_notice(req: NoticeReq, request: Request,
                              response: Response):
    # async (unlike its CRUD siblings): after the insert it pushes a WS nudge
    # so every open client refreshes its bell immediately — _broadcast needs
    # the event loop, so the quick DB write goes through to_thread instead
    # of the usual sync-def threadpool convention.
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    title = req.title.strip()
    if not title or req.target not in _CMS_TARGETS:
        response.status_code = 400
        return {"ok": False, "error": "Title and a valid target are required"}
    nid = await asyncio.to_thread(
        _db.create_notice, title, req.body.strip(), req.target)
    # Content-free nudge: clients refetch /api/notices, where tier/read
    # filtering happens per-user server-side (a broadcast payload can't
    # carry per-tier content — it goes to everyone).
    await _broadcast({"type": "notice"})
    return {"ok": True, "id": nid}


@app.delete("/api/admin/notices/{notice_id}")
def admin_delete_notice(notice_id: int, request: Request):
    if (forbidden := _require_admin(request)) is not None:
        return forbidden
    return {"ok": _db.delete_notice(notice_id)}


@app.get("/api/promos")
def my_promos(request: Request):
    return {"promos": _db.list_promos_for(request.state.category)}


@app.get("/api/notices")
def my_notices(request: Request):
    items, unread = _db.list_notices_for(request.state.user_id,
                                         request.state.category)
    return {"notices": items, "unread": unread}


@app.post("/api/notices/read")
def my_notices_read(request: Request):
    _db.mark_notices_read(request.state.user_id, request.state.category)
    return {"ok": True}


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
