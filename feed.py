"""
Quotex live data feed — multi-asset concurrent version.

Flow (per asset, all sharing ONE Quotex connection/login):
  1. connect() → pyquotex WebSocket (shared, connection-level)
  2. start_candles_stream()  → server pushes ticks for that asset
  3. get_realtime_price()    → poll in-memory tick buffer (no extra WS request)
  4. Aggregate ticks → running OHLC candle
  5. On new candle period → EOC analysis → prediction

Only forex pairs are ever streamed (see _FOREX_BASES). Each forex pair whose
live 1-minute payout is >= PAYOUT_FLOOR runs as an ALWAYS-ON 1m stream,
started at boot / on each pairs refresh and never idle-evicted (see
_reconcile_always_on) — this exists so switching between tradeable pairs is
instant instead of hitting a cold-start gap. Pairs below the payout floor are
blocked from streaming entirely (ensure_stream rejects them outright).
Everything else (other timeframes on an always-on pair, or any pair a viewer
opens directly) is still created ON DEMAND (only when a viewer requests it
via /api/subscribe) and torn down when idle — see the manager-level
capacity/cooldown/staggering logic below, which exists specifically so many
viewers sharing one personal Quotex account can't accidentally hammer Quotex
into looking like a bot/signal-service and risking the account.
"""
import asyncio
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from analyze_eoc import analyze_eoc, _round_level, _key_levels, _parse_votes
import db as _db

# New modular strategy engine (per-pair profiles + candlestick patterns).
# Used as the PRIMARY signal generator; analyze_eoc stays as legacy fallback
# for shadow comparison (see _STRATEGY_ENGINE env flag below).
try:
    from strategies import run_strategy as _run_strategy_engine
    _STRATEGY_ENGINE_AVAILABLE = True
except Exception as _e:
    print(f"[feed] WARNING: strategies package unavailable, "
          f"falling back to analyze_eoc only: {_e}")
    _STRATEGY_ENGINE_AVAILABLE = False
    _run_strategy_engine = None

# Master kill switch: when "1" (default), use the new modular strategy engine.
# When "0", fall back to the legacy analyze_eoc analyzer for shadow comparison.
USE_STRATEGY_ENGINE = os.environ.get("USE_STRATEGY_ENGINE", "1") == "1"

# Minimum live 1-minute payout % for a forex pair to be tradeable in this
# app — pairs below this are blocked from streaming outright (not just from
# always-on pre-warming), matching the win-rate-needs-to-clear-payout math
# already shown in the signal bar (see stream.payout / signal-payout in
# chart.js). Overridable per-deployment since Quotex's payout schedule can
# vary by broker account/region.
PAYOUT_FLOOR = int(os.environ.get("QX_PAYOUT_FLOOR", "81"))

# Method A (LIVE running-candle theory) / Method B (strength gating) rollout
# flags — both untested, added 2026-07-10. Zero-redeploy killswitch: set
# either to "0" via the platform's env var UI to fall back to prior behavior.
ENABLE_LIVE_THEORY   = os.environ.get("ENABLE_LIVE_THEORY",   "1") == "1"
ENABLE_STRENGTH_GATE = os.environ.get("ENABLE_STRENGTH_GATE", "1") == "1"

# ── Fallback display-name helper ─────────────────────────────────────────────
def _api_to_display(api_name: str) -> str:
    """Convert a Quotex forex asset code to a readable display string, e.g.
    "EURUSD_otc" -> "EUR/USD". Only used before a live connection exists (no
    Quotex-supplied display string to draw on yet) — see _clean_display for
    why the live path doesn't reconstruct names from the code this way."""
    base = api_name[:-4] if api_name.endswith("_otc") else api_name
    if len(base) == 6 and base.isalpha():
        return base[:3] + "/" + base[3:]
    return base


_OTC_SUFFIX_RE = re.compile(r"\s*\(otc\)\s*$", re.IGNORECASE)

def _clean_display(raw_display: str) -> str:
    """Strip Quotex's own "(OTC)" suffix from its raw instrument display
    string — the frontend adds its own "Otc"/"Real" suffix uniformly (see
    renderPairSelect in chart.js), so keeping Quotex's would double it up.
    Deliberately uses Quotex's own display string rather than reconstructing
    one from the asset code (_api_to_display): a pair's code doesn't always
    match the base/quote ORDER Quotex itself displays it in — confirmed live,
    BRLUSD_otc's actual Quotex display is "USD/BRL", not "BRL/USD"."""
    return _OTC_SUFFIX_RE.sub("", raw_display.replace("\n", "")).strip()


# ── Pair catalog ─────────────────────────────────────────────────────────────
# Curated whitelist: only these pairs ever appear in the app. Each entry is
# base symbol → which variant ("otc" or "real") the user wants shown for
# that base. _load_pairs filters the live Quotex instrument list against
# this dict — anything not listed here is dropped before it reaches the UI.
_WANTED_PAIRS = {
    # OTC pairs (asset code ends in _otc) — 11 pairs
    "BRLUSD":  "otc",
    "USDINR":  "otc",
    "USDIDR":  "otc",
    "USDCOP":  "otc",
    "USDBDT":  "otc",
    "USDMXN":  "otc",
    "NZDUSD":  "otc",
    "USDDZD":  "otc",
    "USDPHP":  "otc",
    "USDPKR":  "otc",
    "USDZAR":  "otc",
    # Real pairs (no _otc suffix — real market only, not OTC) — 5 pairs
    "USDJPY":  "real",
    "EURUSD":  "real",
    "GBPUSD":  "real",
    "AUDUSD":  "real",
    "EURGBP":  "real",
}

# Derived: set of base symbols this app ever streams. Used by _load_pairs to
# filter the live Quotex instrument list (kept as a set for O(1) lookup).
_FOREX_BASES = set(_WANTED_PAIRS.keys())

# The only (asset, period) combinations /api/subscribe is allowed to create a
# brand-new stream for. Without this, POST /api/subscribe (public, unauth'd
# per server.py) accepts ANY asset string and ANY period: an unknown asset
# skips the payout-lock check entirely (it only runs `if pair and ...`) and
# consumes a slot out of the hard _max_streams cap on pure garbage, while
# period=0 reaches the boundary-flooring math as a division by zero, which
# the stream loop's blanket except/sleep(2)/retry turns into a permanent,
# CPU-burning crash loop for that one stream.
_VALID_ASSETS = frozenset(
    base + ("_otc" if variant == "otc" else "")
    for base, variant in _WANTED_PAIRS.items()
)
_VALID_PERIODS = frozenset({30, 60, 300, 900})  # matches static/index.html's tf-select

# Fallback pair list (shown while Quotex instruments load) — built from the
# same whitelist so the dropdown matches what _load_pairs serves once
# connected. Status is the variant the user wants ("otc"/"real"); Quotex
# will refine it to "live"/"otc"/"closed" once it returns actual open state.
_DEFAULT_PAIRS: list[dict] = [
    {
        "asset":   base + ("_otc" if variant == "otc" else ""),
        "display": _api_to_display(base + ("_otc" if variant == "otc" else "")),
        "status":  "otc" if variant == "otc" else "live",
        "payout":  None,
        "locked":  False,
    }
    for base, variant in _WANTED_PAIRS.items()
]


# ── Small helpers ─────────────────────────────────────────────────────────────

def _atr(candles: list[dict]) -> float:
    if not candles:
        return 0.0001
    return sum(c["high"] - c["low"] for c in candles) / len(candles)


def _pred_candle(candles: list[dict], signal: str, period: int, actual_open: float | None = None) -> dict:
    last = candles[-1]
    op   = actual_open if actual_open is not None else last["close"]
    atr  = _atr(candles[-20:]) if len(candles) >= 20 else (last["high"] - last["low"]) or 0.0001
    t    = last["time"] + period

    # NEUTRAL — draw a flat doji-style "ghost" candle so the chart
    # still shows the next-candle slot, but visually distinct from a
    # real CALL/PUT prediction. The UI looks for signal == "NEUTRAL"
    # to render a "WAIT — no clear edge" badge instead of CALL/PUT.
    if signal == "NEUTRAL":
        # Tiny body at the open, tiny wick both sides — clearly
        # different from a real directional prediction candle.
        tiny = atr * 0.05
        return {"time":  t, "open":  op,
                "high":  round(op + tiny, 6),
                "low":   round(op - tiny, 6),
                "close": round(op, 6)}

    # Realistic candle proportions (fractions of ATR)
    # Body is the main body, wick extends from body tip (signal direction),
    # tail extends from open (opposite direction).
    # Total range = body + wick + tail = 0.85 * ATR (close to average candle)
    body = atr * 0.45   # ~45% of ATR — typical for a moderately strong candle
    wick = atr * 0.25   # ~25% of ATR — main wick in signal direction (from close)
    tail = atr * 0.15   # ~15% of ATR — opposite wick (from open)

    if signal == "CALL":
        # Green candle: open at bottom, close at top
        # upper wick extends FROM close upward, lower tail extends FROM open downward
        return {"time":  t, "open":  op,
                "high":  round(op + body + wick, 6),
                "low":   round(op - tail, 6),
                "close": round(op + body, 6)}
    # PUT — red candle: open at top, close at bottom
    # lower wick extends FROM close downward, upper tail extends FROM open upward
    return {"time":  t, "open":  op,
            "high":  round(op + tail, 6),
            "low":   round(op - body - wick, 6),
            "close": round(op - body, 6)}


def _normalise(raw) -> list[dict]:
    """Accept whatever format pyquotex returns, produce sorted OHLC list."""
    if not raw:
        return []
    if isinstance(raw, dict):
        for key in ("candles", "data", "history"):
            if key in raw:
                raw = raw[key]; break
        else:
            raw = list(raw.values())[0] if raw else []
    seen: dict[int, dict] = {}
    for c in raw:
        try:
            bar = {
                "time":  int(c.get("time",  c.get("from", 0))),
                "open":  float(c.get("open",  0)),
                "high":  float(c.get("high",  0)),
                "low":   float(c.get("low",   0)),
                "close": float(c.get("close", 0)),
            }
            seen[bar["time"]] = bar   # deduplicate: later entry wins
        except (TypeError, ValueError):
            continue
    return sorted(seen.values(), key=lambda x: x["time"])


def _drop_price_contamination(candles: list[dict]) -> list[dict]:
    """
    Defend against a stale/wrong-asset candle batch getting spliced into a
    fresh history fetch. Symptom: chart shows an old price-level cluster,
    a big blank jump, then the new pair's real candles — permanently, since
    it's baked into the one-shot history snapshot and never self-heals.
    Detect a jump between consecutive candles far bigger than the local
    range and keep only the freshest contiguous segment after it.
    """
    if len(candles) < 6:
        return candles
    ranges = sorted(c["high"] - c["low"] for c in candles if c["high"] > c["low"])
    if not ranges:
        return candles
    median_rng = ranges[len(ranges) // 2]
    if median_rng <= 0:
        return candles

    cut = 0
    for i in range(1, len(candles)):
        jump = abs(candles[i]["close"] - candles[i - 1]["close"])
        if jump > median_rng * 25:
            cut = i   # a later jump wins — keep only the freshest segment
    if cut:
        print(f"[feed] dropped {cut} contaminated candle(s) "
              f"(price jump > 25x median range) before index {cut}")
        return candles[cut:]
    return candles


def _floor_to_period(ts: float, period: int) -> int:
    """Floor a Unix timestamp to the start of its candle period."""
    return (int(ts) // period) * period


# ── Per-asset stream state ────────────────────────────────────────────────────
# Everything that used to live directly on QuotexFeed (one asset at a time)
# now lives on its own _AssetStream instance, owned for its whole life by one
# asyncio.Task (see QuotexFeed._run_stream) — nothing else can ever mutate it
# mid-await, which structurally rules out the cross-asset contamination bugs
# the old singleton design needed manual guards against.
@dataclass
class _AssetStream:
    asset: str
    period: int
    candles: list = field(default_factory=list)
    ticks: deque = field(default_factory=lambda: deque(maxlen=500))
    candle_open_time: int = 0
    candle_open_price: float = 0.0
    candle_open_is_real: bool = False
    last_tick_ts: float = 0.0
    last_real_tick_wall: float = 0.0
    prediction: dict | None = None
    # Chop guard: consecutive losses in the CURRENT (regime, zone). See
    # ZONE_LOSS_GUARD / QuotexFeed._run_eoc.
    zone_streak: dict = field(
        default_factory=lambda: {"regime": None, "zone": None, "losses": 0})
    payout: int | None = None
    sub_started: bool = False           # start_candles_stream() issued at least once
    task: object = None                 # the asyncio.Task running this stream
    # Server pre-warmed this pair (payout >= PAYOUT_FLOOR) — immune to idle
    # eviction while true. See QuotexFeed._reconcile_always_on.
    always_on: bool = False
    # A viewer is actively waiting on this pair's chart (created via
    # ensure_stream, not the bulk pre-warm). Priority streams take the FAST
    # lane in _run_stream — they never queue behind the ~40-pair always-on
    # backlog, which used to leave a watched chart blank for minutes.
    priority: bool = False
    # Late-tick drop accounting. The drop itself is correct and must stay,
    # but exotic OTC pairs (USDPKR/USDZAR/USDPHP) deliver lagging timestamps
    # constantly, so logging every batch emitted several unbuffered lines per
    # second per pair and buried every real error in the Railway logs.
    late_drop_count: int = 0
    late_drop_last_log: float = 0.0
    interested_cids: set = field(default_factory=set)   # viewer client-ids watching
    idle_since: float | None = None
    created_at: float = field(default_factory=time.time)
    # Snapshot of the (closed candles, just-closed-candle ticks) that fed the
    # LAST _run_eoc call — reused by the LIVE-theory periodic re-eval so it
    # can re-score with fresh running_ticks without re-deriving stale state
    # from stream.ticks, which has since been cleared for the new candle.
    base_candles: list = field(default_factory=list)
    base_ticks: list = field(default_factory=list)
    _live_reeval_ticks: int = 0   # last tick-count LIVE re-eval fired at


# ── Feed ──────────────────────────────────────────────────────────────────────

# Consecutive wrong predictions in the SAME (regime, zone) before the signal
# is suppressed to NEUTRAL. Live data showed the model whipsawing (CALL wrong,
# PUT wrong, CALL wrong...) while price chops sideways at one level — neither
# continuation nor reversal theories have a real edge there (see project
# history), so once a zone proves itself unreadable N times running, stop
# guessing in it rather than keep flipping a coin. Resets the moment the
# regime/zone classification actually changes.
ZONE_LOSS_GUARD = 3


class QuotexFeed:
    def __init__(self):
        self._client              = None
        self._connected           = False
        self._reconnect_attempts  = 0        # for exponential backoff
        self._broadcast           = None     # set once in run()

        # ── Multi-asset stream management (replaces the old singleton
        # asset/candles/ticks/... fields) ───────────────────────────────────
        self._streams: dict[tuple[str, int], _AssetStream] = {}
        # Default covers ~38 always-on forex pairs (see _reconcile_always_on)
        # plus headroom for on-demand non-1m streams and the brief overlap
        # window when a pair's real/otc asset code swaps.
        self._max_streams     = int(os.environ.get("QX_MAX_STREAMS", "45"))
        # Held across a stream's whole start sequence (start_candles_stream +
        # history fetch) — staggers concurrent starts AND serializes history
        # fetches, closing a real race in pyquotex's Strategy-2 history
        # fallback (a shared, non-asset-keyed scratch attribute).
        self._new_stream_gate = asyncio.Semaphore(1)
        # Fast lane for viewer-requested streams. The bulk always-on pre-warm
        # holds _new_stream_gate for ~4s per pair × ~40 pairs; a chart the
        # user is actually staring at must never sit behind that queue, so
        # priority streams get their own single-slot gate instead.
        self._prio_stream_gate = asyncio.Semaphore(1)
        self._stagger_gap     = float(os.environ.get("QX_STAGGER_GAP_SEC", "1.5"))
        # Priority streams pace much tighter — one viewer switching pairs is
        # nowhere near the burst risk that the 40-pair pre-warm is.
        self._prio_stagger    = float(os.environ.get("QX_PRIO_STAGGER_SEC", "0.2"))
        # Delay the bulk always-on pre-warm after connect so the first
        # viewer-requested stream(s) win the race to the socket. 0 disables.
        self._prewarm_delay   = float(os.environ.get("QX_PREWARM_DELAY_SEC", "3"))
        # Rolling error window -> temporary cooldown on starting NEW streams
        # (existing streams are never affected) — the safety net against
        # hammering Quotex if something starts failing repeatedly.
        self._recent_errors: list[float] = []
        self._cooldown_until: float = 0.0
        self._cooldown_reason: str  = ""

        # Unified pair list — one entry per logical pair, status=live/otc/closed
        # (connection-wide, not per-asset — kept as-is)
        self._pairs_list: list[dict] = list(_DEFAULT_PAIRS)
        self._last_pairs_refresh: float = 0.0

        # Theory mute gate — live per-theory accuracy feedback loop.
        # {theory_code: "43% n=212/7d"} built from db.theory_perf with
        # hysteresis (see _refresh_theory_mutes); passed into every
        # analyze_eoc call. Deliberately a cached snapshot refreshed from
        # the manager loop, NEVER queried inline at EOC time: ~42 always-on
        # streams close simultaneously each minute and theory_perf holds
        # db._lock. Empty until the first refresh => no muting at startup.
        self._muted_theories: dict[str, str] = {}
        self._last_perf_refresh: float = 0.0

        # get_share_signals() computes all 16 pairs (one DB round-trip each)
        # and is hit by the Share Signal tab's 10s poll from every visitor
        # PLUS every /api/v1/signals* call, including single-asset lookups
        # that only needed one of the 16 rows. Values only actually change
        # once per candle close (60s) per pair, so a short TTL cache
        # collapses concurrent/rapid callers onto one computation instead of
        # each paying the full 16x DB-connection cost.
        self._share_signals_cache: tuple[float, list[dict]] | None = None
        self._SHARE_SIGNALS_TTL: float = 2.0

        # Strong references for fire-and-forget background tasks (client
        # teardown on token swap, per-stream rearm on stale-client rebuild).
        # asyncio only holds a WEAK reference to a task via create_task — with
        # nothing else referencing it, the task can be garbage-collected
        # mid-execution before its cleanup/re-subscribe logic finishes. See
        # https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
        self._bg_tasks: set[asyncio.Task] = set()

        # DB row-count housekeeping. Was startup-only for a long time (see
        # run()'s initial _db.cleanup() call) — this service can stay up for
        # weeks without a redeploy, so unbounded growth between restarts
        # filled the Railway volume to 83% (2026-07-08 incident). Now also
        # re-run periodically from the manager loop.
        self._last_db_cleanup: float = 0.0

        # ── User-supplied session token (in-memory, NOT persisted) ────────
        # Set via POST /api/token from the frontend Settings page. This is
        # the ONLY auth path the live app ever uses — email/password login
        # was removed because Cloudflare blocks the HTTP sign-in page on
        # every non-browser client, and retrying a failed login made the
        # failure worse (it kept hammering Quotex with bad credentials).
        # Empty string = no user token.
        self._user_token: str = ""
        self._user_token_at: float = 0.0   # when it was set (for status display)

        # ── One-shot login gate ─────────────────────────────────────────────
        # The login system tries EXACTLY ONCE per token. If _connect() returns
        # False for ANY reason (auth reject, timeout, network error), this flag
        # flips True and the manager loop stops retrying. Only set_token()
        # (called when the user pastes a fresh SSID) clears it — that's the
        # explicit "try again" signal from the operator.
        self._login_failed_permanently: bool = False
        self._login_fail_reason: str = ""

        # ── Manager-loop wake signal ────────────────────────────────────────
        # set_token() used to only flip flags and then wait for the manager
        # loop to notice on its next 5s housekeeping tick — so every token
        # paste burned 0-5s doing nothing before the connect even started.
        # The loop now sleeps on this Event instead of a bare sleep(), so a
        # paste wakes it within milliseconds.
        self._wake = asyncio.Event()

        # ── Transient-failure retry budget ──────────────────────────────────
        # A REJECTED token is final (the credential is genuinely bad) and
        # still trips the one-shot gate immediately. But a timeout or socket
        # error says nothing about the token — the old code treated both the
        # same and left the app dead until a human re-pasted, which is the
        # single biggest cause of "token দিলাম, কানেক্টই হয় না". Transient
        # failures now get a small bounded, backed-off retry budget before
        # the gate trips. Set QX_TRANSIENT_RETRIES=0 to restore the old
        # strict single-attempt behaviour.
        self._transient_retries_max = int(
            os.environ.get("QX_TRANSIENT_RETRIES", "3"))
        self._transient_retries_used = 0

    def _spawn(self, coro) -> asyncio.Task:
        """Fire-and-forget a coroutine while keeping a strong reference to
        the resulting Task, so it can't be garbage-collected mid-execution
        (see self._bg_tasks docstring in __init__)."""
        task = asyncio.get_event_loop().create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            # The old callback only discarded the task, so it never
            # retrieved the exception — any crash inside a spawned coroutine
            # disappeared with no traceback anywhere. That is how a broken
            # pre-warm can look exactly like "the app is fine, there are
            # just no candles".
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                import traceback
                print(f"[feed] background task {t.get_name()} crashed: {exc}")
                traceback.print_exception(type(exc), exc, exc.__traceback__)

        task.add_done_callback(_done)
        return task

    # ── Public ────────────────────────────────────────────────────────────────

    def set_token(self, token: str) -> None:
        """Store a user-supplied SSID token (from the frontend Settings page).

        This is the ONLY auth path — the app does NOT fall back to email/
        password login (Cloudflare blocks it on every non-browser client).
        Stored in-memory only — never written to disk or env. Pass an empty
        string to clear a previously-set token.

        Pasting a new token also clears the one-shot login-fail flag, which
        is the explicit operator "try again" signal — _connect() will run
        exactly once more with the new token. If that attempt fails for ANY
        reason, the system stops retrying until the operator pastes another
        token.
        """
        self._user_token = (token or "").strip()
        self._user_token_at = time.time() if self._user_token else 0.0
        # Clearing the one-shot fail gate is the ONLY way _connect() will run
        # again after a failure. The operator pasting a new token is the
        # explicit "try again" signal — we honor it unconditionally.
        self._login_failed_permanently = False
        self._login_fail_reason = ""
        self._transient_retries_used = 0   # fresh paste = fresh retry budget
        if self._user_token:
            print(f"[feed] user token set ({self._user_token[:8]}...) — "
                  "will use on next connect")
        else:
            print("[feed] user token cleared")
        # Force a reconnect on the next manager-loop iteration: mark the
        # current connection as down and tear down the existing client so
        # _connect() runs again with the new token. Without this, a stale
        # (but still "connected") client would keep serving old data and
        # the new token wouldn't take effect until the next organic drop.
        if self._connected:
            self._connected = False
            self._reconnect_attempts = 0   # reset backoff
            # Tear down the old client asynchronously — can't await here
            # (this method is sync, called from a sync route handler), so
            # schedule the close on the running event loop.
            old_client = self._client
            self._client = None
            if old_client:
                try:
                    self._spawn(self._close_client(old_client))
                except RuntimeError:
                    pass  # no event loop yet — will be cleaned up on next connect

        # Kick the manager loop out of its housekeeping sleep NOW instead of
        # waiting up to HOUSEKEEP_SECS. This is what turns "paste → up to 5s
        # of dead air → connect starts" into "paste → connect starts".
        try:
            self._wake.set()
        except RuntimeError:
            pass  # no loop yet (token set before startup) — loop reads flags anyway

    def has_token(self) -> bool:
        """True if a user-supplied token is currently stored."""
        return bool(self._user_token)

    def token_status(self) -> dict:
        """Return the current token state for the /api/token-status endpoint.
        Never exposes the token value itself — only whether one is set,
        when it was set, whether the feed is currently connected, and whether
        the one-shot login gate has tripped (so the UI can show "paste again"
        instead of an opaque "Waiting…")."""
        return {
            "has_user_token": bool(self._user_token),
            "has_env_token":  bool(os.environ.get("QX_TOKEN", "").strip()),
            "token_source":   ("user" if self._user_token
                               else "env" if os.environ.get("QX_TOKEN", "").strip()
                               else "none"),
            "set_at":         self._user_token_at if self._user_token else None,
            "connected":      self._connected,
            "login_failed":   self._login_failed_permanently,
            "login_fail_reason": self._login_fail_reason,
        }

    def available_pairs(self) -> dict:
        return {"pairs": self._pairs_list, "payout_floor": PAYOUT_FLOOR}

    async def _load_pairs(self, broadcast=None) -> None:
        """
        Fetch all Quotex instruments and build a UNIFIED, FOREX-ONLY pair list.

        Each logical forex pair (e.g. EUR/USD) appears exactly ONCE:
          - status="live"   → real market open  → asset = "EURUSD"
          - status="otc"    → real closed, OTC open → asset = "EURUSD_otc"
          - status="closed" → both closed → asset = real (or OTC) name, disabled

        Non-forex instruments (crypto/commodities/stocks) are dropped
        entirely — this app only ever streams forex (see _FOREX_BASES).

        Each live/otc pair also carries its 1-minute payout % and a
        "locked" flag (payout < PAYOUT_FLOOR) — locked pairs are shown
        disabled and ensure_stream() refuses to start a stream for them.
        """
        try:
            instruments = await self._client.get_instruments()
            if not instruments:
                return

            # Group by logical base name (forex only)
            by_base: dict[str, dict] = {}
            for i in instruments:
                name   = i[1]
                is_otc = name.endswith("_otc")
                base   = name[:-4] if is_otc else name
                if base not in _FOREX_BASES:
                    continue
                # Variant filter: only keep the variant the user asked for
                # (e.g. wants "NZDUSD_otc" → drop real "NZDUSD"; wants real
                # "AUDUSD" → drop "AUDUSD_otc"). Without this, _load_pairs
                # would pick whichever variant is open, ignoring the user's
                # OTC-vs-real choice.
                wanted = _WANTED_PAIRS[base]
                variant = "otc" if is_otc else "real"
                if variant != wanted:
                    continue

                is_open = bool(i[14])
                payout  = i[-9]   # 1-minute payout %, same field pyquotex's
                try:              # own get_payout_by_asset()/get_payment() read
                    payout = int(payout) if payout is not None else None
                except (TypeError, ValueError):
                    payout = None

                if base not in by_base:
                    by_base[base] = {}

                key = "otc" if is_otc else "real"
                by_base[base][key] = {
                    "asset":   name,
                    "display": _clean_display(i[2]) or _api_to_display(name),
                    "open":    is_open,
                    "payout":  payout,
                }

            # Build unified list: one entry per logical pair
            pairs: list[dict] = []
            for base, v in by_base.items():
                real = v.get("real")
                otc  = v.get("otc")

                if real and real["open"]:
                    chosen, status = real, "live"
                elif otc and otc["open"]:
                    chosen, status = otc, "otc"
                else:
                    chosen, status = (real or otc), "closed"

                # Missing payout data defaults to locked (safe default, not
                # an accidental bypass of the payout gate).
                payout = chosen["payout"]
                locked = status in ("live", "otc") and (
                    payout is None or payout < PAYOUT_FLOOR)

                pairs.append({
                    "asset":   chosen["asset"],
                    "display": chosen["display"],
                    "status":  status,
                    "payout":  payout,
                    "locked":  locked,
                })

            # Sort: active (live/otc) before closed, unlocked before locked,
            # then highest payout first — the pairs actually worth picking
            # float to the top instead of being buried alphabetically.
            pairs.sort(key=lambda x: (
                x["status"] == "closed", x["locked"],
                -(x["payout"] or 0), x["display"].upper()))

            self._pairs_list        = pairs
            self._last_pairs_refresh = time.time()
            print(f"[feed] pairs loaded: {len(pairs)} forex pairs "
                  f"({sum(1 for p in pairs if p['status']=='live')} live, "
                  f"{sum(1 for p in pairs if p['status']=='otc')} OTC, "
                  f"{sum(1 for p in pairs if p['status']=='closed')} closed, "
                  f"{sum(1 for p in pairs if p['locked'])} locked <{PAYOUT_FLOOR}%)")

            if broadcast:
                await broadcast({"type": "pairs", "pairs": pairs,
                                  "payout_floor": PAYOUT_FLOOR})

        except Exception as exc:
            print(f"[feed] pairs load error: {exc}")

    def snapshot(self, asset: str, period: int) -> dict | None:
        stream = self._streams.get((asset, period))
        if not stream or not stream.candles:
            return None
        return {
            "type":       "snapshot",
            "asset":      stream.asset,
            "period":     stream.period,
            "candles":    stream.candles[-300:],
            "prediction": stream.prediction,
        }

    def get_share_signals(self) -> list[dict]:
        """Return the latest signal for all 16 wanted pairs, for the Share
        Signal table. One row per pair — the most recent closed-candle
        prediction + the closed candle's buyer/seller pressure.

        Each row contains:
          asset            — e.g. "EURUSD_otc"
          display          — e.g. "EUR/USD"
          type             — "otc" or "real"
          time             — Unix timestamp of the signal (candle close time)
          buy_pct          — buyer tick % from the closed candle's micro
          sell_pct         — seller tick % (100 - buy_pct)
          signal           — "CALL" / "PUT" / "NEUTRAL"
          strength         — "STRONG" / "MEDIUM" / "WEAK"
          confidence       — 0.0–1.0
          prediction_candle — {open, high, low, close} of the ghost candle
                             (None if no prediction yet)

        Pairs with no stream or no closed candle yet return a row with
        signal=None so the table always shows all 16 rows.
        """
        if self._share_signals_cache is not None:
            ts, cached_rows = self._share_signals_cache
            if time.time() - ts < self._SHARE_SIGNALS_TTL:
                return cached_rows

        rows = []
        # Build a quick lookup of asset → Quotex-supplied display name (so the
        # share table matches the rest of the app, not the reconstructed
        # _api_to_display which gets BRL/USD backwards vs Quotex's USD/BRL).
        _display_by_asset = {p["asset"]: p.get("display") or _api_to_display(p["asset"])
                             for p in self._pairs_list}
        for base, variant in _WANTED_PAIRS.items():
            asset = base + ("_otc" if variant == "otc" else "")
            # Look for a 60s stream (the default/always-on period)
            stream = self._streams.get((asset, 60))
            row = {
                "asset":             asset,
                "display":           _display_by_asset.get(asset) or _api_to_display(asset),
                "type":              variant,
                "time":              None,
                "buy_pct":           None,
                "sell_pct":          None,
                "signal":            None,
                "strength":          None,
                "confidence":        None,
                "prediction_candle": None,
            }
            if stream:
                # Latest prediction (for the NEXT candle)
                pred = stream.prediction
                if pred:
                    row["signal"]     = pred.get("signal")
                    row["strength"]   = pred.get("strength")
                    row["confidence"] = pred.get("confidence")
                    pc = pred.get("candle")
                    if pc and isinstance(pc, dict):
                        row["prediction_candle"] = {
                            "open":  pc.get("open"),
                            "high":  pc.get("high"),
                            "low":   pc.get("low"),
                            "close": pc.get("close"),
                        }
                # Last closed candle's time + buyer/seller pressure.
                # stream.candles holds closed candles; the last one is the
                # most recent close. Its micro data (buy_pct/sell_pct) is
                # fetched from the DB via get_micro_history.
                if stream.candles:
                    last_closed = stream.candles[-1]
                    row["time"] = last_closed.get("time")
                    # Fetch the micro summary for this closed candle
                    try:
                        micro_hist = _db.get_micro_history(
                            asset, 60, n=1,
                            before_ctime=last_closed.get("time", 0) + 1)
                        if micro_hist:
                            m = micro_hist[-1]  # most recent
                            row["buy_pct"]  = m.get("buy_pct")
                            row["sell_pct"] = (100 - m["buy_pct"]
                                               if m.get("buy_pct") is not None
                                               else None)
                    except Exception:
                        pass
            rows.append(row)
        self._share_signals_cache = (time.time(), rows)
        return rows


    async def ensure_stream(self, asset: str, period: int,
                            cid: str | None = None) -> dict:
        """
        Called from /api/subscribe. Starts a stream for (asset, period) if one
        isn't already running, subject to the capacity cap / error cooldown.
        An already-running stream is NEVER rejected or torn down here — those
        guards only gate the creation of a brand-new stream.
        """
        if asset not in _VALID_ASSETS or period not in _VALID_PERIODS:
            return {"ok": False, "status": "invalid",
                    "reason": f"Unknown asset/period {asset}@{period}s"}

        key = (asset, period)
        stream = self._streams.get(key)
        if stream is not None:
            if cid:
                stream.interested_cids.add(cid)
                for k, s in self._streams.items():   # a cid watches one pair at a time
                    if k != key:
                        s.interested_cids.discard(cid)
            stream.idle_since = None
            # A joining viewer only gets ongoing tick/eoc broadcasts from here
            # on — without handing back the CURRENT candles/prediction, their
            # chart stays empty until the next candle close (up to a full
            # period away) even though the stream has been live the whole
            # time. Include the snapshot directly in the response so the
            # frontend can paint immediately, same as a brand-new stream's
            # first broadcast.
            return {"ok": True, "status": "streaming", "asset": asset, "period": period,
                    "candles": stream.candles[-300:], "prediction": stream.prediction}

        # Payout gate — only blocks starting a BRAND NEW stream, same as the
        # cooldown/capacity checks below. If a pair's payout later drifts
        # below the floor, anyone already watching keeps their stream (see
        # _reconcile_always_on, which only ever demotes always_on, never
        # tears the stream down).
        #
        # 2026-08-15: When ALL_PAIRS_ALWAYS_ON=1 (default), the payout gate
        # is bypassed — the user's explicit requirement is that EVERY pair
        # gets a signal on EVERY candle. Set ALL_PAIRS_ALWAYS_ON=0 to
        # restore the payout-gated behavior.
        pair = next((p for p in self._pairs_list if p["asset"] == asset), None)
        if pair and pair.get("locked"):
            all_pairs = os.environ.get("ALL_PAIRS_ALWAYS_ON", "1") == "1"
            if not all_pairs:
                return {"ok": False, "status": "locked", "payout": pair.get("payout"),
                        "reason": f"Needs {PAYOUT_FLOOR}% payout "
                                  f"(currently {pair.get('payout', '?')}%)"}
            # else: warn but proceed — user explicitly wants all pairs tradable
            print(f"[feed] WARNING: {asset} payout {pair.get('payout')}% < "
                  f"{PAYOUT_FLOOR}% floor, but ALL_PAIRS_ALWAYS_ON=1 -> "
                  f"streaming anyway (user requirement)")

        if time.time() < self._cooldown_until:
            return {"ok": False, "status": "cooldown",
                    "retry_after": round(self._cooldown_until - time.time(), 1),
                    "reason": self._cooldown_reason}
        if len(self._streams) >= self._max_streams:
            return {"ok": False, "status": "at_capacity", "max": self._max_streams}

        # priority=True: a real viewer is sitting in front of a blank chart
        # waiting for this exact pair. It takes the fast lane in _run_stream
        # rather than queueing behind the always-on pre-warm backlog.
        stream = _AssetStream(asset=asset, period=period, priority=True)
        if cid:
            stream.interested_cids.add(cid)
        self._streams[key] = stream
        stream.task = asyncio.create_task(self._run_stream(stream))
        return {"ok": True, "status": "starting"}

    async def drop_interest(self, cid: str) -> None:
        """A viewer disconnected — stop counting it toward any stream's
        interested_cids (idle-eviction sweep does the rest)."""
        for s in self._streams.values():
            s.interested_cids.discard(cid)

    def stream_status(self) -> dict:
        now = time.time()
        return {
            "active": [{"asset": s.asset, "period": s.period,
                        "viewers": len(s.interested_cids),
                        "age_sec": round(now - s.created_at)}
                       for s in self._streams.values()],
            "count": len(self._streams),
            "max":   self._max_streams,
            "cooldown_until":  self._cooldown_until if self._cooldown_until > now else None,
            "cooldown_reason": self._cooldown_reason if self._cooldown_until > now else None,
        }

    async def shutdown(self) -> None:
        tasks = [s.task for s in self._streams.values() if s.task]
        for t in tasks:
            t.cancel()
        # Await cancellation so each stream's finally block (broker
        # unsubscribe, see stop_candles_stream) actually runs before the
        # process exits. Without this, cancel() only requests cancellation —
        # the caller (server.py's lifespan) only awaited the manager loop
        # task, not individual stream tasks, so on every Railway redeploy
        # these unsubscribes raced process exit and often never ran, leaving
        # subscriptions to expire on the broker's own timeout instead.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Connection (shared across all streams) ──────────────────────────────

    def _remember_token(self) -> None:
        """Cache the latest working SSID so reconnects reuse it (no manual token).
        pyquotex also persists it to session.json, so it survives restarts."""
        try:
            tok = (self._client.session_data or {}).get("token")
            if tok:
                os.environ["QX_TOKEN"] = tok
        except Exception:
            pass

    def _make_client(self, ua: str, root: str):
        from pyquotex.stable_api import Quotex
        from pyquotex.types import ReconnectPolicy
        # Host: defaults to qxbroker.com — the official Quotex host that
        # also serves the WebSocket endpoint at wss://ws2.qxbroker.com.
        # The earlier switch to qxbroker.co was a workaround for a 403 on
        # the sign-in page, but the real cause turned out to be
        # Cloudflare's bot check flagging the Firefox-default UA paired
        # with Chrome Sec-Ch-Ua-* headers — fixed properly in navigator.py
        # by pinning the whole header family to consistent Chrome values.
        # Override via QX_HOST only if Quotex changes its primary host.
        host = os.environ.get("QX_HOST", "qxbroker.com")
        # NOTE: email/password are required by the Quotex() constructor signature
        # but are NEVER used as an auth path — we always authenticate via the SSID
        # token passed to set_session() before connect(). Empty strings are fine
        # here because pyquotex's account.py:51-54 only calls authenticate() when
        # self.session_data.get("token") is falsy, and we always set it via
        # set_session(ssid=token) before connect(). See _connect() for the
        # single-attempt, token-only login flow.
        return Quotex(
            email    = "",
            password = "",
            host     = host,
            lang     = "en",
            root_path= root,
            reconnect_policy=ReconnectPolicy(
                enabled=True, max_attempts=0,
                base_delay=2.0, max_delay=30.0, stale_timeout=45.0),
        )

    @staticmethod
    async def _close_client(client) -> None:
        """Best-effort close — pyquotex versions vary on the API."""
        for meth in ("close", "disconnect", "close_connect"):
            fn = getattr(client, meth, None)
            if callable(fn):
                try:
                    result = fn()
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await asyncio.wait_for(result, timeout=3)
                except Exception:
                    pass
                return

    def _note_transient_failure(self, detail: str) -> None:
        """Handle a NON-reject connect failure (timeout, socket error, etc).

        A rejected token is final and trips the one-shot gate straight away —
        that logic is untouched. This path is for failures that say nothing
        about the credential. We spend a bounded retry budget first so a
        single slow handshake doesn't take the whole app down until a human
        notices and re-pastes.
        """
        self._transient_retries_used += 1
        left = self._transient_retries_max - self._transient_retries_used
        if left >= 0 and self._transient_retries_max > 0:
            print(f"[feed] {detail} — transient, retrying "
                  f"({self._transient_retries_used}/"
                  f"{self._transient_retries_max})")
            # Gate stays OPEN — the manager loop retries with backoff.
            self._login_fail_reason = (
                f"{detail} — retrying automatically "
                f"({self._transient_retries_used}/"
                f"{self._transient_retries_max})")
            return
        print(f"[feed] {detail} — transient retry budget exhausted, "
              "NOT retrying. Paste the token again in the Settings tab.")
        self._login_failed_permanently = True
        self._login_fail_reason = (
            f"{detail}. Tried {self._transient_retries_max} times. "
            f"Token kept — paste it again to retry.")

    async def _connect(self) -> bool:
        """Token-only login.

        The login system tries EXACTLY ONCE per token. There is NO email/
        password fallback (Cloudflare blocks the HTTP sign-in page on every
        non-browser client, so retrying it was just hammering Quotex with
        bad credentials) and NO second retry — if this attempt fails for
        any reason (auth reject, timeout, network error), the manager loop
        stops calling _connect() until set_token() clears the fail gate.

        The fail gate is the explicit operator "try again" signal: paste a
        fresh SSID in the Settings tab and _connect() runs exactly once more.
        """
        # If a previous attempt already failed, don't even try — the
        # manager loop checks this flag before calling _connect(), but
        # belt-and-braces here too so a stray call is a no-op.
        if self._login_failed_permanently:
            return False

        try:
            from pyquotex.types import ReconnectPolicy  # noqa: ensure importable
            # Cross-platform default (was a hardcoded Windows path — broke
            # immediately on any Linux deployment, e.g. Railway).
            import tempfile
            root = os.environ.get(
                "QX_ROOT", os.path.join(tempfile.gettempdir(), "plybit_cache")
            )
            ua = os.environ.get("QX_UA", "").strip() or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

            # ── Token precedence: user-supplied (frontend) > QX_TOKEN env ──
            # This is the ONLY auth path. Email/password was removed because
            # Cloudflare's JS challenge on the /en/sign-in page rejects every
            # non-browser HTTP client with HTTP 403, and retrying it just
            # made the failure louder.
            token = self._user_token or os.environ.get("QX_TOKEN", "").strip()
            if not token:
                # No credentials at all — this is the boot state on a fresh
                # Railway deploy before the operator has pasted a token. Don't
                # set the fail gate here: the manager loop will keep polling
                # so the moment a token lands via POST /api/token, _connect()
                # runs immediately. We just sleep this cycle.
                print("[feed] no token set — paste your SSID in the Settings "
                      "tab (or set QX_TOKEN env var) to start streaming")
                return False

            self._client = self._make_client(ua, root)
            self._client.set_session(user_agent=ua, ssid=token)
            src = "user" if self._user_token else "env"
            print(f"[feed] connecting with session token={token[:8]}... "
                  f"(source={src}) — single attempt, no retry on failure")

            try:
                ok, reason = await asyncio.wait_for(
                    self._client.connect(), timeout=30)
                if ok:
                    self._remember_token()
                    print(f"[feed] connect -> ok=True  reason={reason}")
                    return True
                # Connect returned False. The user requirement is strict:
                # "if the first login fails, the second time cannot be tried".
                # We do NOT fall back to email/password and we do NOT retry
                # — we set the one-shot fail gate and wait for the operator
                # to paste a fresh token via set_token().
                _reason_str = str(reason or "")
                if ("authorization/reject" in _reason_str
                        or _reason_str == "authorization/reject"):
                    # Real reject — token is genuinely invalid (expired, wrong
                    # account, etc.). Clear it so the next attempt starts
                    # clean and the token_status endpoint reflects reality.
                    print(f"[feed] token REJECTED by server ({reason}) — "
                          "token is genuinely invalid. Clearing and waiting "
                          "for a fresh SSID paste.")
                    if self._user_token:
                        self._user_token = ""
                        self._user_token_at = 0.0
                    else:
                        os.environ.pop("QX_TOKEN", None)
                    self._login_failed_permanently = True
                    self._login_fail_reason = (
                        "Token rejected by Quotex — re-extract the SSID "
                        "from a fresh browser session and paste it in "
                        "the Settings tab.")
                else:
                    # Timeout / transient slowness / network error. The token
                    # itself is almost certainly fine here — Quotex was slow
                    # or the socket dropped. Burning the one-shot gate on
                    # this was the #1 reason the app sat dead with a
                    # perfectly valid token in memory. Retry a bounded number
                    # of times with backoff, THEN trip the gate.
                    self._note_transient_failure(f"Connect failed: {reason}")
            except Exception as _te:
                self._note_transient_failure(f"Network/connect error: {_te}")

            # Best-effort client teardown — the client object is left for
            # the manager loop to clean up if needed, but the connection
            # itself is dead.
            await self._close_client(self._client)
            return False
        except Exception as exc:
            err = str(exc)
            print(f"[feed] connect error: {exc} — NOT retrying.")
            self._login_failed_permanently = True
            self._login_fail_reason = f"Connect error: {err}"
            return False

    async def _load_history(self, asset: str, period: int) -> list[dict]:
        """Fetch candle history with adaptive candle count per timeframe."""
        # How many candles to target per timeframe
        if period <= 60:
            target = 200
        elif period <= 300:
            target = 150
        else:
            target = 100
        window = target * period

        # Strategy 1: get_historical_candles with max_workers=1
        # (sequential — avoids the 3× data explosion that caused slowness before)
        try:
            raw = await asyncio.wait_for(
                self._client.get_historical_candles(
                    asset,
                    amount_of_seconds = window,
                    period            = period,
                    max_workers       = 1,
                ),
                timeout = 15.0,
            )
            candles = _normalise(raw)
            if candles:
                result = _drop_price_contamination(candles[-target:])
                print(f"[feed] history: {len(result)} candles for {asset}@{period}s")
                return result
        except asyncio.TimeoutError:
            print(f"[feed] history timeout (batch) for {asset}@{period}s")
        except Exception as exc:
            print(f"[feed] history batch error: {exc}")

        # Strategy 2: single get_candles fallback
        try:
            raw = await asyncio.wait_for(
                self._client.get_candles(
                    asset,
                    end_from_time = None,
                    offset        = window,
                    period        = period,
                ),
                timeout = 10.0,
            )
            candles = _normalise(raw)
            if candles:
                result = _drop_price_contamination(candles[-target:])
                print(f"[feed] history (single): {len(result)} candles for {asset}@{period}s")
                return result
        except asyncio.TimeoutError:
            print(f"[feed] history timeout (single) for {asset}@{period}s")
        except Exception as exc:
            print(f"[feed] history single error: {exc}")

        print(f"[feed] history FAILED for {asset}@{period}s")
        return []

    # ── EOC helpers ──────────────────────────────────────────────────────────

    def _analyze_core(self, asset: str, period: int, candles: list[dict],
                      ticks: list[float],
                      running_ticks: list[float] | None = None
                      ) -> tuple[dict | None, list]:
        """
        Shared EOC analysis: pure analyze_eoc theory blend, nothing else.
        Used by the watched asset (via _run_eoc) AND background trackers, so
        evidence collected in the background goes through the exact same
        pipeline as the on-screen signal. Returns (result, micro_hist).

        candles[-1] is the just-closed candle at this point. micro_history is
        fetched BEFORE the just-closed candle is saved to DB (we save it right
        after this call), so the history contains only the candles PRIOR to
        the current one — no double-counting with ticks/RUN. before_ctime
        restricts it to the 5 candle-slots immediately before the just-closed
        candle: a restart / asset switch can no longer feed hours-old rows to
        MICRO as if they were the previous candle.

        EVERY closed candle emits a CALL or PUT signal — the user's explicit
        requirement ("প্রত্যেক পেয়ার এ প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে").
        The strategy engine's tiebreak (strategies/runner.py:515-530) and the
        legacy analyze_eoc tiebreak (analyze_eoc.py:1196-1233) both guarantee
        this — when no theory voted, the engine falls back to (a) independent
        evidence lean, (b) regime direction, (c) last-candle color. NEUTRAL is
        only ever returned when `candles` is completely empty (cold-start with
        zero history and zero accumulated ticks — an edge case that should
        never happen on an always-on 1m stream).

        Previously this method had a `len(candles) < 5` early-None guard that
        suppressed signals during cold-start (~5 min per pair). That guard
        violated the user's every-candle requirement and is now removed —
        the runner handles 1-2 candle windows fine via its tiebreak.
        """
        if not candles:
            # Truly empty — no candle has closed yet. The runner's _empty_signal
            # path returns NEUTRAL for this, but we want to suppress the broadcast
            # entirely (no candle = nothing to predict on). The next closed candle
            # will have len(candles) >= 1 and go through the full pipeline.
            return None, []
        micro_hist = _db.get_micro_history(
            asset, period, n=5,
            before_ctime=candles[-1]["time"]) if len(candles) >= 2 else []

        # ── Primary path: new modular strategy engine (per-pair profiles +
        # candlestick patterns + market-state deep analysis).
        # Falls back to legacy analyze_eoc when USE_STRATEGY_ENGINE=0 or
        # when the strategies package failed to import at startup.
        if USE_STRATEGY_ENGINE and _STRATEGY_ENGINE_AVAILABLE:
            try:
                result = _run_strategy_engine(
                    candles, asset=asset, period=period,
                    ticks=ticks,
                    running_ticks=running_ticks if ENABLE_LIVE_THEORY else None,
                    muted=self._muted_theories,
                )
                # analyze_eoc compat: ensure all keys the feed expects exist.
                result.setdefault("candle", None)  # filled in by _run_eoc
                return result, micro_hist
            except Exception as _e:
                print(f"[feed] strategy engine error for {asset}@{period}s: "
                      f"{_e} — falling back to analyze_eoc")

        # Legacy path (also used as fallback when the new engine raises)
        result = analyze_eoc(candles, ticks,
                             micro_history=micro_hist,
                             period=period,
                             muted=self._muted_theories,
                             asset=asset,
                             running_ticks=running_ticks if ENABLE_LIVE_THEORY else None)
        return result, micro_hist

    def _run_eoc(self, stream: _AssetStream,
                actual_open: float | None = None) -> dict | None:
        closed = stream.candles
        base_ticks = list(stream.ticks)
        # running_ticks=None here: the NEW candle's ticks are empty at this
        # exact moment (they accumulate after this call). LIVE theory picks
        # up once ticks come in, via the periodic re-eval in the stream loop.
        result, micro_hist = self._analyze_core(
            stream.asset, stream.period, closed, base_ticks,
            running_ticks=None)
        if result is None:
            return None
        # Snapshot for the periodic LIVE-theory re-eval (see stream loop).
        stream.base_candles = closed
        stream.base_ticks   = base_ticks
        stream._live_reeval_ticks = 0

        # Chop guard: this exact (regime, zone) has been wrong ZONE_LOSS_GUARD+
        # times in a row — a spot that's proven itself unreadable. Under
        # every-candle mode (2026-07-06) the signal direction stands but is
        # demoted to WEAK instead of being withheld as NEUTRAL. Clears
        # itself the moment the regime/zone classification changes (see the
        # streak update in _close_running_and_start_new), not on a timer.
        _reg = result.get("regime") or {}
        _key = (_reg.get("trend"), _reg.get("zone"))
        if (result["signal"] != "NEUTRAL"
                and _key == (stream.zone_streak["regime"], stream.zone_streak["zone"])
                and stream.zone_streak["losses"] >= ZONE_LOSS_GUARD):
            result["strength"] = "WEAK"
            result.setdefault("reasons", []).append(
                f"CHOP GUARD: {_key[0]}/{_key[1]} wrong "
                f"{stream.zone_streak['losses']}x running -> WEAK until zone changes")

        # ── Every-candle signal guarantee ──────────────────────────────────
        # The strategy engine (strategies/runner.py) and the legacy analyze_eoc
        # both end with an always-emit tiebreak that GUARANTEES a CALL or PUT
        # on every closed candle — the user's explicit requirement:
        #   "প্রত্যেক পেয়ার এ প্রত্যেক ক্যান্ডেল এ সিগন্যাল আসতে হবে।"
        # When no theory voted and no market state had conviction, the tiebreak
        # falls through to the just-closed candle's own color (bull → CALL,
        # bear → PUT), marked WEAK so the user knows it's a low-confidence
        # tiebreak, not a strong-agreement signal. NEUTRAL is unreachable on
        # any non-empty candle window.
        #
        # The earlier 2026-08-11 note here claimed NEUTRAL was first-class —
        # that was an aspirational refactor that was never actually applied
        # to the engines' tiebreak code (both still force CALL/PUT). The
        # user's every-candle requirement (re-confirmed 2026-08-17) ratifies
        # the existing behavior, so this comment now matches what the code
        # actually does.
        return {**result, "candle": _pred_candle(closed, result["signal"], stream.period, actual_open),
                "payout": stream.payout}

    def _accuracy(self, just_closed: dict, pred: dict | None) -> str | None:
        # Compare the candle that just closed against the prediction that was
        # made FOR it (pred), NOT the one before it. `pred` is captured
        # immediately before it is reassigned in the close handler.
        # NEUTRAL is not a direction — it must never be graded (the old code
        # fell through to pred_up=False, silently grading NEUTRAL as PUT).
        if not pred or pred["signal"] not in ("CALL", "PUT"):
            return None
        # Zero-move candle = broker refund (draw), not a win or a loss.
        # Grading close>=open as UP silently counted draws as CALL wins.
        if just_closed["close"] == just_closed["open"]:
            return "draw"
        actual_up = just_closed["close"] > just_closed["open"]
        pred_up   = pred["signal"] == "CALL"
        return "correct" if actual_up == pred_up else "wrong"

    def _grade_and_log(self, asset: str, period: int, closed: dict,
                       prediction: dict | None, micro_snap: dict | None,
                       candles: list[dict]) -> str | None:
        """
        Grade `closed` against the prediction that was made FOR it and write
        the full postmortem row to signal_log. Shared by the watched asset's
        close path and background trackers. `candles` must already contain
        `closed` as its last element (ATR history reads candles[-11:-1]).
        Returns the accuracy string (correct/wrong/draw) or None.

        NEUTRAL predictions get no signal_log row (NEUTRAL is not a
        direction and must never be graded), but their per-theory votes ARE
        shadow-graded into theory_votes — with the dead band + parrot guard
        producing NEUTRAL on a large share of candles, dropping those votes
        would starve theory_perf's 7-day window exactly when the mute gate
        depends on it, and a muted theory could never earn its way back.
        """
        accuracy = self._accuracy(closed, prediction)
        if not prediction:
            return accuracy

        # Log the resolved prediction with a full WHY report. For each theory
        # vote we record whether it called THIS candle right or wrong, so later
        # analysis can see exactly why a signal won or lost.
        try:
            import json as _json
            reasons   = prediction.get("reasons", [])
            is_draw   = closed["close"] == closed["open"]
            actual_up = closed["close"] > closed["open"]

            # Per-theory votes, AGGREGATED per theory. Muted lines are
            # INCLUDED deliberately (include_muted default) — shadow-grading
            # them is what lets a muted theory keep its track record alive.
            # A theory like RUN can emit several sub-votes in one candle —
            # summing them into one NET vote per theory prevents (a) the
            # theory_votes PK overwriting earlier sub-votes and (b) the same
            # theory landing in right_codes AND wrong_codes at once. A theory
            # whose sub-votes cancel out (net 0) casts no vote. Draw candles
            # are refunds: theories are neither right nor wrong on them.
            _net: dict[str, int] = {}
            for code, vdir, mag in _parse_votes(reasons):
                _net[code] = _net.get(code, 0) + vdir * mag

            votes = []          # (theory, CALL/PUT, mag, right/wrong/draw)
            fired, right, wrong = set(), set(), set()
            for code, net in _net.items():
                fired.add(code)
                if net == 0:
                    continue    # internally conflicted — no net vote
                voted_up = net > 0
                if is_draw:
                    outcome = "draw"
                else:
                    outcome = "right" if voted_up == actual_up else "wrong"
                    (right if outcome == "right" else wrong).add(code)
                votes.append((code, "CALL" if voted_up else "PUT",
                              abs(net), outcome))

            # NEUTRAL final — shadow-grade the votes, skip the postmortem.
            if not accuracy:
                _db.log_theory_votes(asset, period, closed["time"], votes)
                return accuracy

            # ── Postmortem: WHY did this trade win or lose ─────────────
            move  = closed["close"] - closed["open"]
            c_rng = closed["high"] - closed["low"]
            _hist = candles[-11:-1]
            atr   = (sum(x["high"] - x["low"] for x in _hist) / len(_hist)
                     if _hist else c_rng)
            _reg  = (prediction.get("regime") or {})
            regime, zone = _reg.get("trend"), _reg.get("zone")
            sig   = prediction["signal"]

            tags = []
            if is_draw:
                tags.append("DRAW")              # zero move = broker refund
            if atr > 0 and c_rng < atr * 0.40:
                tags.append("NOISE_CANDLE")      # sub-noise range: coin flip
            if atr > 0 and abs(move) >= atr * 0.80:
                tags.append("BIG_MOVE")
            if ((regime == "UPTREND" and sig == "PUT") or
                    (regime == "DOWNTREND" and sig == "CALL")):
                tags.append("COUNTER_REGIME")
            elif ((regime == "UPTREND" and sig == "CALL") or
                    (regime == "DOWNTREND" and sig == "PUT")):
                tags.append("WITH_REGIME")
            if micro_snap and micro_snap.get("last_react") == "EXHAUST":
                tags.append("LATE_FLIP")         # candle flipped at the close
            if not is_draw and len(wrong) > len(right):
                tags.append("MAJORITY_WRONG")
            # Market-state deep-analysis layer: log which state was named and
            # whether its own directional bias called this candle — the ONLY
            # honest way to learn if any state reads better than coin-flip
            # before it is ever allowed to influence the signal.
            _ms = prediction.get("market_state") or {}
            if _ms.get("state"):
                tags.append(f"ST_{_ms['state']}")
                if _ms.get("bias") in ("CALL", "PUT") and not is_draw:
                    tags.append("STBIAS_" + (
                        "RIGHT" if (_ms["bias"] == "CALL") == actual_up
                        else "WRONG"))

            # Method B (2026-07-10, untested): log whether the running-candle
            # strength gate fired on this prediction, and whether the gate's
            # implied call (confirming = same direction, opposing = flip)
            # matched the actual outcome — the only honest way to learn if
            # RUNCONF gating tracks real accuracy before trusting it further.
            _runconf_tag = (prediction or {}).get("_runconf_tag")
            if _runconf_tag:
                tags.append(_runconf_tag)   # RUNCONF_UP or RUNCONF_DOWN
                if not is_draw:
                    _gate_correct = (
                        (_runconf_tag == "RUNCONF_UP"   and actual_up ==
                         (sig == "CALL")) or
                        (_runconf_tag == "RUNCONF_DOWN" and actual_up !=
                         (sig == "CALL"))
                    )
                    tags.append("RUNCONF_" + ("RIGHT" if _gate_correct else "WRONG"))

            _atr_note = (f" ({abs(move) / atr * 100:.0f}% of ATR)"
                         if atr > 0 else "")
            _actual_lbl = ("FLAT" if is_draw
                           else "UP" if actual_up else "DOWN")
            pm = (
                f"{sig} s={prediction['score']:+d}"
                f" {prediction.get('strength')}"
                f" agree={prediction.get('agree')}"
                f" | actual {_actual_lbl}"
                f" move={move:+.5f}{_atr_note}"
                f" | {accuracy.upper()}"
                f" | right: {','.join(sorted(right)) or '-'}"
                f" | wrong: {','.join(sorted(wrong)) or '-'}"
                f" | regime {regime}/{zone}"
                f"{' | ' + ','.join(tags) if tags else ''}"
            )

            # Log whenever a theory fired — this is the only evidence
            # source now that analyze_eoc is the sole signal generator.
            if fired:
                _db.log_signal(
                    asset, period, closed["time"],
                    sig, prediction["score"],
                    prediction["confidence"], ",".join(sorted(fired)),
                    _actual_lbl, accuracy,
                    strength=prediction.get("strength"),
                    agree=prediction.get("agree"),
                    right_codes=",".join(sorted(right)),
                    wrong_codes=",".join(sorted(wrong)),
                    reasons=_json.dumps(reasons),
                    a_open=closed["open"], a_close=closed["close"],
                    regime=regime, zone=zone,
                    tags=",".join(tags), postmortem=pm,
                    votes=votes,
                )
        except Exception as _e:
            print(f"[db] log_signal error: {_e}")
        return accuracy

    def _save_micro(self, asset: str, period: int, closed: dict,
                    micro_snap: dict, candles: list[dict],
                    ticks: list[float]) -> None:
        """
        Persist a closed candle's microstructure + gap classification + key
        levels + downsampled ticks. `candles` must already contain `closed`
        as its last element (gap reads candles[-2] as the previous close).
        """
        try:
            # ── Gap classification for this candle ─────────────────
            _gap_pct  = 0.0
            _gap_type = "NONE"
            if len(candles) >= 2:
                _pc = candles[-2]["close"]
                if _pc > 0:
                    _raw_gap = closed["open"] - _pc
                    _gp      = _raw_gap / _pc          # signed %
                    if abs(_gp) >= 0.0001:             # ≥ 0.01% threshold
                        _gap_pct  = _gp
                        _gap_up   = _gp > 0
                        _is_bull_c = closed["close"] >= closed["open"]
                        _w_fill = ((_gap_up and closed["low"]  <= _pc) or
                                   (not _gap_up and closed["high"] >= _pc))
                        _b_fill = ((_gap_up and closed["close"] <= _pc) or
                                   (not _gap_up and closed["close"] >= _pc))
                        if _b_fill:
                            _gap_type = "FILLED"
                        elif _w_fill:
                            # Wick reached gap zone — was it rejected?
                            _gap_type = ("REJECTED"
                                         if _gap_up == _is_bull_c
                                         else "WICK_FILL")
                        elif _gap_up == _is_bull_c:
                            _gap_type = "PURE"       # gap unvisited, continuation
                        else:
                            _gap_type = "FLIP"       # gap up but closed down (rare)
            micro_snap["gap_pct"]   = _gap_pct
            micro_snap["gap_type"]  = _gap_type
            micro_snap["key_levels"] = _key_levels(candles)
            # Persist the candle's raw ticks (downsampled to <=240 points)
            # so backtest can replay RUN/TRAP with the same input as live.
            import json as _tick_json
            _tl = list(ticks)
            if len(_tl) > 240:
                _st = len(_tl) / 240
                _tl = [_tl[int(i * _st)] for i in range(240)]
            micro_snap["ticks_json"] = _tick_json.dumps(
                [round(x, 6) for x in _tl])
            _db.save(asset, period, closed, micro_snap)
        except Exception as _me:
            print(f"[db] micro save error: {_me}")

    # ── Running candle ────────────────────────────────────────────────────────

    def _analyze_microstructure(self, ticks: list[float],
                                open_price: float) -> dict | None:
        """
        Real-time tick microstructure analysis of the running candle.
        Identifies buyer/seller pressure, fight zones, hold levels, and reactions.
        """
        ticks = list(ticks)
        if len(ticks) < 10:
            return None

        op  = open_price
        hi  = max(ticks)
        lo  = min(ticks)
        cur = ticks[-1]
        rng = hi - lo

        # ── 1. Buyer vs Seller tick count ─────────────────────────────────────
        up_t = sum(1 for i in range(1, len(ticks)) if ticks[i] > ticks[i - 1])
        dn_t = sum(1 for i in range(1, len(ticks)) if ticks[i] < ticks[i - 1])
        moves = up_t + dn_t
        buy_pct  = round(up_t / moves * 100) if moves else 50
        sell_pct = 100 - buy_pct

        # ── 2. Dominant pressure ──────────────────────────────────────────────
        if buy_pct >= 62:
            pressure = "BUYER"
        elif sell_pct >= 62:
            pressure = "SELLER"
        else:
            pressure = "FIGHT"

        # ── 3. Fight zone: how many times price crosses candle midpoint ────────
        mid     = (hi + lo) / 2
        crosses = sum(
            1 for i in range(1, len(ticks))
            if (ticks[i - 1] < mid) != (ticks[i] < mid)
        )
        is_fight = crosses >= 4

        # ── 4. Hold level: most visited price zone ────────────────────────────
        hold_price = None
        if rng > 0:
            bin_size = rng / 8
            bins: dict[int, int] = {}
            for t in ticks:
                b = int((t - lo) / bin_size)
                bins[b] = bins.get(b, 0) + 1
            top_bin    = max(bins, key=bins.get)
            hold_price = round(lo + top_bin * bin_size + bin_size / 2, 6)
            hold_visits = bins[top_bin]
        else:
            hold_price  = round(cur, 6)
            hold_visits = len(ticks)

        # ── 5. Phase momentum (early / mid / late thirds) ─────────────────────
        n  = len(ticks)
        t3 = max(n // 3, 1)
        early = ticks[t3]     - ticks[0]
        mid_m = ticks[2 * t3] - ticks[t3]
        late  = ticks[-1]     - ticks[2 * t3]

        def _dir(v: float) -> str:
            return "UP" if v > 0 else ("DOWN" if v < 0 else "FLAT")

        phases = [_dir(early), _dir(mid_m), _dir(late)]

        # ── 6. Buyer / Seller reaction ────────────────────────────────────────
        # Reaction = price visited extreme then reversed. We confirm with LATE tick
        # direction (last 25% of ticks) to avoid flagging mid-candle wicks.
        reaction = None
        if rng > 0:
            from_hi   = (hi  - cur) / rng
            from_lo   = (cur - lo)  / rng
            net       = cur - op
            late_q    = max(n // 4, 2)
            late_move = ticks[-1] - ticks[-late_q]  # direction of last 25% ticks
            # SELLER reaction: fell far from high AND late ticks confirm selling
            if from_hi > 0.50 and late_move <= 0 and net < 0:
                reaction = "SELLER"
            # BUYER reaction: rose far from low AND late ticks confirm buying
            elif from_lo > 0.50 and late_move >= 0 and net > 0:
                reaction = "BUYER"

        # ── 7. Final-tick recovery / exhaustion ──────────────────────────────────
        # Real-time version of the LAST theory: last 15% of running candle ticks.
        last_react = None
        if n >= 15:
            last_n2 = max(n // 6, 6)   # min 6 so fi_tot can reach 5 (matches LAST theory)
            fin2    = ticks[-last_n2:]
            fi2_up  = sum(1 for i in range(1, len(fin2)) if fin2[i] > fin2[i - 1])
            fi2_dn  = sum(1 for i in range(1, len(fin2)) if fin2[i] < fin2[i - 1])
            fi2_tot = fi2_up + fi2_dn
            if fi2_tot >= 3:
                fbp2       = fi2_up / fi2_tot
                net_run    = cur - op
                is_bull_rt = net_run > 0
                if is_bull_rt:
                    if fbp2 <= 0.30:
                        last_react = "EXHAUST"
                    elif fi2_tot >= 5 and fbp2 >= 0.90:
                        last_react = "EXHAUST"
                    elif 0.55 <= fbp2 <= 0.85 and fi2_dn >= 2:
                        last_react = "RECOVERY"
                elif net_run < 0:
                    if fbp2 >= 0.70:
                        last_react = "EXHAUST"
                    elif fi2_tot >= 5 and fbp2 <= 0.10:
                        last_react = "EXHAUST"
                    elif 0.15 <= fbp2 <= 0.45 and fi2_up >= 2:
                        last_react = "RECOVERY"

        # ── 8. Round number proximity ─────────────────────────────────────────
        # Check if current price, candle high, or candle low is near a round level.
        def _rnd(p):
            lvl, _, str_ = _round_level(p)
            return (lvl, str_) if str_ != "NONE" else (None, None)

        cur_lvl, cur_str = _rnd(cur)
        hi_lvl,  hi_str  = _rnd(hi)
        lo_lvl,  lo_str  = _rnd(lo)
        round_info = {
            "near_level":    cur_lvl,
            "near_strength": cur_str,
            "hi_level":      hi_lvl  if hi_str  in ("BIG", "MID") else None,
            "hi_strength":   hi_str  if hi_str  in ("BIG", "MID") else None,
            "lo_level":      lo_lvl  if lo_str  in ("BIG", "MID") else None,
            "lo_strength":   lo_str  if lo_str  in ("BIG", "MID") else None,
        }

        return {
            "buy_pct":    buy_pct,
            "sell_pct":   sell_pct,
            "pressure":   pressure,
            "is_fight":   is_fight,
            "crosses":    crosses,
            "hold_price": hold_price,
            "hold_visits":hold_visits,
            "phases":     phases,
            "reaction":   reaction,
            "net":        round(cur - op, 6),
            "tick_count": len(ticks),
            "last_react": last_react,
            "round":      round_info,
        }

    def _running_confirmation(self, stream: _AssetStream) -> str | None:
        """
        Check if the running candle's tick movement confirms the current prediction.

        Idea: after EOC sets a CALL/PUT prediction, the new candle's first ticks
        either move in the predicted direction (CONFIRMING) or against it (OPPOSING).
        This gives real-time validation of the EOC signal.

        Returns: 'CONFIRMING', 'OPPOSING', or None.
        """
        if not stream.prediction or len(stream.ticks) < 5:
            return None
        pred = stream.prediction.get("signal")
        if pred == "NEUTRAL":
            return None

        ticks  = list(stream.ticks)
        open_p = stream.candle_open_price

        # Overall direction from open
        net = ticks[-1] - open_p

        # Momentum consistency: first half vs second half
        mid         = len(ticks) // 2
        first_half  = ticks[mid] - ticks[0]
        second_half = ticks[-1]  - ticks[mid]

        # Strong momentum: both halves same direction
        if first_half > 0 and second_half > 0:
            running_dir = "UP"
        elif first_half < 0 and second_half < 0:
            running_dir = "DOWN"
        else:
            # Mixed — use net direction from open
            running_dir = "UP" if net >= 0 else "DOWN"

        if (pred == "CALL" and running_dir == "UP") or \
           (pred == "PUT"  and running_dir == "DOWN"):
            return "CONFIRMING"
        return "OPPOSING"

    def _apply_strength_gate(self, stream: _AssetStream,
                             prediction: dict) -> dict:
        """
        Method B (2026-07-10, untested) — gate prediction strength using the
        running candle's tick confirmation. Returns a NEW prediction dict
        (does not mutate the input).

        Decision matrix:
          WEAK   + CONFIRMING + 10+ ticks -> MEDIUM
          MEDIUM + OPPOSING   + 10+ ticks -> WEAK
          STRONG + OPPOSING   + 10+ ticks -> MEDIUM
        All other cases: strength unchanged.
        """
        if not prediction or prediction.get("signal") not in ("CALL", "PUT"):
            return prediction

        conf = self._running_confirmation(stream)
        if conf is None:
            return prediction

        tick_count = len(stream.ticks)
        if tick_count < 10:
            return prediction  # not enough live evidence yet

        current = prediction.get("strength", "WEAK")
        new_strength = current
        gate_tag = None
        gate_reason = None

        if current == "WEAK" and conf == "CONFIRMING":
            new_strength = "MEDIUM"
            gate_tag = "RUNCONF_UP"
            gate_reason = (f"RUNCONF: WEAK + 10+ confirming ticks "
                          f"({tick_count}) -> upgraded to MEDIUM")
        elif current == "MEDIUM" and conf == "OPPOSING":
            new_strength = "WEAK"
            gate_tag = "RUNCONF_DOWN"
            gate_reason = (f"RUNCONF: MEDIUM + 10+ opposing ticks "
                          f"({tick_count}) -> demoted to WEAK")
        elif current == "STRONG" and conf == "OPPOSING":
            new_strength = "MEDIUM"
            gate_tag = "RUNCONF_DOWN"
            gate_reason = (f"RUNCONF: STRONG + 10+ opposing ticks "
                          f"({tick_count}) -> demoted to MEDIUM")
        else:
            return prediction  # no change

        new_pred = dict(prediction)
        new_pred["strength"] = new_strength
        new_pred["reasons"] = [*prediction.get("reasons", []), gate_reason]
        new_pred["_runconf_tag"] = gate_tag
        return new_pred

    def _running_candle(self, stream: _AssetStream) -> dict:
        op = stream.candle_open_price
        ticks = list(stream.ticks)
        if not ticks:
            return {"time": stream.candle_open_time, "open": op,
                    "high": op, "low": op, "close": op}
        return {
            "time":  stream.candle_open_time,
            "open":  op,
            "high":  max(ticks),
            "low":   min(ticks),
            "close": ticks[-1],
        }

    def _close_running_and_start_new(self, stream: _AssetStream,
                                     new_open_time: int, first_tick: float,
                                     open_is_real: bool = True):
        """Finalize the running candle and begin a new one.

        open_is_real=False marks the new open as a placeholder (used by the
        timer-close, which fires before any real tick of the new candle exists).
        The first real tick later re-anchors the open in the stream loop.
        """
        # Time guard: never let a candle go backwards in time — LightweightCharts
        # throws on out-of-order data and the chart breaks.
        if new_open_time <= stream.candle_open_time:
            return None

        closed = self._running_candle(stream)

        # Replace or append the closed candle in the history list
        if stream.candles and stream.candles[-1]["time"] == closed["time"]:
            stream.candles[-1] = closed
        elif not stream.candles or stream.candles[-1]["time"] < closed["time"]:
            stream.candles.append(closed)

        # Keep list bounded
        if len(stream.candles) > 500:
            stream.candles = stream.candles[-400:]

        # Microstructure of the just-closed candle — computed ONCE here while
        # stream.ticks is still intact; used by the postmortem and persisted
        # to candle_micro further down.
        _micro_snap = (self._analyze_microstructure(stream.ticks, stream.candle_open_price)
                       if len(stream.ticks) >= 10 else None)

        # Grade the candle that just closed against the prediction that was
        # made FOR it (stream.prediction, before we overwrite it below) and
        # write the full postmortem row (shared with background trackers).
        accuracy = self._grade_and_log(stream.asset, stream.period, closed,
                                       stream.prediction, _micro_snap,
                                       stream.candles)

        # Update the chop-guard streak using the regime/zone the JUST-RESOLVED
        # prediction was made under (stream.prediction, before _run_eoc below
        # overwrites it with the next one). A win, or the zone itself changing,
        # clears the streak; a loss in the SAME zone extends it.
        if accuracy in ("correct", "wrong"):
            _reg = (stream.prediction or {}).get("regime") or {}
            _key = (_reg.get("trend"), _reg.get("zone"))
            if _key == (stream.zone_streak["regime"], stream.zone_streak["zone"]):
                stream.zone_streak["losses"] = (
                    stream.zone_streak["losses"] + 1 if accuracy == "wrong" else 0)
            else:
                stream.zone_streak = {"regime": _key[0], "zone": _key[1],
                                      "losses": 1 if accuracy == "wrong" else 0}

        stream.prediction = self._run_eoc(stream, actual_open=first_tick)

        # Persist microstructure NOW — after EOC (so DB was clean during analysis)
        # but BEFORE ticks.clear() so the tick buffer is still fully intact.
        if _micro_snap:
            self._save_micro(stream.asset, stream.period, closed, _micro_snap,
                             stream.candles, list(stream.ticks))

        # Start new candle
        stream.candle_open_time    = new_open_time
        stream.candle_open_price   = first_tick
        stream.candle_open_is_real = open_is_real
        stream.ticks.clear()
        stream.ticks.append(first_tick)

        return accuracy

    def _broker_time(self) -> float:
        """Return the Quotex server's current timestamp when available,
        falling back to the local clock.

        The broker timestamp is what every tick message carries (set on
        api.timesync.server_timestamp in pyquotex/api.py:570) and is the
        authoritative clock for candle boundaries — using the local clock
        instead would drift on hosts with skewed NTP and cause candles to
        close a few hundred ms early/late vs the broker's actual period
        rollover. Returns time.time() if the client isn't connected yet
        or timesync hasn't received its first tick.
        """
        try:
            if self._client and self._client.api:
                ts = self._client.api.timesync.server_timestamp
                # Sanity: a valid Unix timestamp from this millennium
                if ts and ts > 1_000_000_000:
                    return float(ts)
        except Exception:
            pass
        return time.time()

    async def _smart_sleep(self, stream: _AssetStream) -> None:
        """Sleep until next tick poll, but wake up early at candle boundary.
        Uses the broker's server timestamp (not the local clock) so the
        sleep aligns exactly with the broker's candle period rollover —
        no ms drift from local NTP skew.

        Poll rate is scaled by how far we are from the boundary: fine-grained
        (10-50ms) only in the last ~2s where exact boundary timing matters,
        coarser (150-350ms) otherwise. Every always-on stream (16-45 of them)
        runs this loop for its entire life, so polling at a flat 20-100Hz
        regardless of proximity to close was pure wasted CPU/broadcast churn
        for the ~98% of each candle's life spent far from the boundary.
        """
        if stream.candle_open_time > 0:
            close_at     = stream.candle_open_time + stream.period
            until_close  = close_at - self._broker_time()
            if until_close > 2.0:
                sleep_dur = 0.35
            elif until_close > 0.5:
                sleep_dur = 0.15
            else:
                sleep_dur = max(0.01, min(0.05, until_close))
        else:
            sleep_dur = 0.05
        await asyncio.sleep(sleep_dur)

    # ── Per-stream lifecycle ──────────────────────────────────────────────────

    async def _start_stream(self, stream: _AssetStream) -> None:
        """Subscribe + load history for one stream. Raises on failure so the
        caller (_run_stream) can count it toward the error cooldown."""
        if self._client is None:
            raise RuntimeError("Quotex client not connected yet")

        asset, period = stream.asset, stream.period
        print(f"[feed] starting stream {asset}@{period}s")

        await self._client.start_candles_stream(asset, period)
        stream.sub_started = True
        # Brief settle so get_payout_by_asset below sees a populated table.
        # Was a flat 1s — held INSIDE the start gate, so it cost 1s × every
        # pair (~40s of pure dead gate time on the always-on pre-warm) for a
        # value that is display-only and never feeds a signal. The history
        # fetch right after provides the real settle window anyway.
        await asyncio.sleep(float(os.environ.get("QX_TICK_SETTLE_SEC", "0.15")))

        # Payout is informational only (breakeven display) — never affects
        # signal/score.
        try:
            pay = self._client.get_payout_by_asset(asset)
            stream.payout = int(pay) if pay is not None else None
        except Exception:
            stream.payout = None

        history = await self._load_history(asset, period)
        stream.last_real_tick_wall = time.time()

        if not history:
            # History unavailable (live pair or API timeout). Don't retry-loop
            # — mark started and let tick streaming build the chart from
            # scratch.
            print(f"[feed] no history for {asset}@{period}s "
                  f"— starting from ticks only")
            await self._broadcast({
                "type":       "snapshot",
                "asset":      asset,
                "period":     period,
                "candles":    [],
                "prediction": None,
            })
            return

        last = history[-1]
        stream.candles           = history
        stream.candle_open_time  = last["time"] + period
        stream.candle_open_price = last["close"]
        stream.ticks.clear()
        stream.ticks.append(last["close"])
        stream.candle_open_is_real = False
        stream.last_tick_ts         = 0.0
        # Generate initial prediction from history so the ghost candle
        # appears immediately without waiting for the first EOC.
        stream.prediction = self._run_eoc(stream, actual_open=last["close"])
        await self._broadcast({
            "type":       "snapshot",
            "asset":      asset,
            "period":     period,
            "candles":    history,
            "prediction": stream.prediction,
        })

    async def _stream_loop(self, stream: _AssetStream) -> None:
        """Runs 'forever' for one (asset, period) — timer-close fallback,
        tick polling, tick-based close, same-candle updates. Direct per-asset
        port of what used to be the single shared run() loop's body."""
        TIMER_GRACE = 1.5   # seconds past boundary before forcing close
        STALE_SECS  = 90

        while True:
            try:
                # ── Per-stream stale re-arm ────────────────────────────────
                # Only re-issues THIS stream's own subscription (cheap) — never
                # tears down self._client, which would kill every other
                # viewer's stream too. A GLOBAL "everything is stale" backstop
                # lives in the manager loop (run()) instead.
                if (stream.last_real_tick_wall > 0
                        and time.time() - stream.last_real_tick_wall > STALE_SECS):
                    print(f"[feed] STALE: {stream.asset}@{stream.period}s "
                          f"— re-arming stream")
                    try:
                        if self._client:
                            await self._client.start_candles_stream(
                                stream.asset, stream.period)
                    except Exception:
                        pass
                    stream.last_real_tick_wall = time.time()
                    await self._broadcast({"type": "stale", "asset": stream.asset,
                                           "period": stream.period})
                    await asyncio.sleep(2)
                    continue

                # ── Timer-based candle close (fallback after a grace window) ──
                # OTC ticks can be sparse (5-10s gaps). A tick that crosses the
                # boundary closes the candle immediately (tick-close below, the
                # accurate path). The timer is only the FALLBACK for silent
                # feeds — it waits a short grace past the boundary so a late
                # final tick can still shape the true close before we grade and
                # log the candle.
                # Uses the BROKER's server timestamp (self._broker_time) so the
                # fallback aligns exactly with the broker's period rollover —
                # no ms drift from local NTP skew vs the broker clock.
                now = self._broker_time()
                if (stream.candle_open_time > 0
                        and now >= stream.candle_open_time + stream.period + TIMER_GRACE):
                    # FIX: previously only ONE timer-close fired per loop
                    # iteration. If the stream was silent for 2+ periods
                    # (network blip, stale re-arm took 90s, etc.), the
                    # intermediate candles were NEVER closed, NEVER graded,
                    # NEVER logged, NEVER broadcast — directly violating
                    # "every candle must signal". Now we loop: while the
                    # boundary has been crossed by another full period,
                    # close-and-start a new candle for EACH intermediate one.
                    while True:
                        expected_new = _floor_to_period(now, stream.period)
                        if expected_new <= stream.candle_open_time:
                            break
                        # Only close one period at a time — if `now` is
                        # 2+ periods past the open, we close each candle
                        # in sequence so all intermediate candles get
                        # recorded with a proper OHLC + a fresh prediction.
                        single_target = stream.candle_open_time + stream.period
                        last_px = (list(stream.ticks)[-1] if stream.ticks
                                   else stream.candle_open_price)
                        print(f"[feed] timer-close {stream.asset}@{stream.period}s "
                              f"{stream.candle_open_time} -> {single_target}")
                        accuracy = self._close_running_and_start_new(
                            stream, single_target, last_px, open_is_real=False)
                        running  = self._running_candle(stream)
                        all_c    = stream.candles + [running]
                        await self._broadcast({
                            "type":       "eoc",
                            "asset":      stream.asset,
                            "period":     stream.period,
                            "candles":    all_c[-300:],
                            "prediction": stream.prediction,
                            "accuracy":   accuracy,
                        })
                        # ── Signal-at-candle-start (timer-close path) ──────────
                        # Same 0-second signal broadcast as the tick-close path
                        # above. Timer-close fires when no boundary tick arrived
                        # within the grace window — we still need to push the
                        # signal_start event so external API clients receive it.
                        if stream.prediction and stream.prediction.get("signal") in (
                                "CALL", "PUT", "NEUTRAL"):
                            await self._broadcast({
                                "type":          "signal_start",
                                "asset":         stream.asset,
                                "period":        stream.period,
                                "candle_open_time": single_target,
                                "candle_expires_at": single_target + stream.period,
                                "signal":        stream.prediction["signal"],
                                "strength":      stream.prediction.get("strength"),
                                "confidence":    stream.prediction.get("confidence"),
                                "score":         stream.prediction.get("score"),
                                "agree":         stream.prediction.get("agree"),
                                "patterns_fired": stream.prediction.get("patterns_fired", []),
                                "reasons":       stream.prediction.get("reasons", [])[:5],
                                "prediction_candle": stream.prediction.get("candle"),
                            })

                        # If we've caught up to `now` (within one period),
                        # exit the multi-period close loop. Otherwise iterate
                        # again to close the next intermediate candle.
                        if (stream.candle_open_time + stream.period + TIMER_GRACE
                                > self._broker_time()):
                            break

                if self._client is None:
                    await asyncio.sleep(1)
                    continue

                # ── Poll in-memory tick buffer (no extra WS request) ──────────
                price_data = await self._client.get_realtime_price(stream.asset)

                if not price_data:
                    await self._smart_sleep(stream)
                    continue

                # ── Collect EVERY new tick since last processed ───────────────
                if stream.last_tick_ts <= 0.0:
                    # Fresh subscribe / reconnect: process the current buffer once
                    # so the first live tick can seed the running candle without
                    # reusing stale data from a previous feed session.
                    new_ticks = list(price_data)
                    stream.last_tick_ts = max(
                        (float(p["time"]) for p in new_ticks if float(p["time"]) > 0),
                        default=0.0,
                    )
                else:
                    new_ticks = [
                        p for p in price_data
                        if float(p["time"]) > stream.last_tick_ts
                    ]

                if not new_ticks:
                    await self._smart_sleep(stream)
                    continue

                # Mark all these ticks as seen
                stream.last_tick_ts = float(new_ticks[-1]["time"])
                stream.last_real_tick_wall = time.time()   # feed is alive

                # ── Find every tick that crossed a candle boundary ─────────────
                # Walk ALL crossings in this batch, not just the first. A
                # single _smart_sleep gap only ever spans one boundary, but
                # after a stall/rearm (90s per-stream stale re-arm, or the
                # global stale-client rebuild) a batch can span two or more
                # closed periods — treating only the first crossing folds
                # every candle after it into the next one's OHLC, silently
                # dropping it from stream.candles/DB/grading entirely.
                boundaries: list[tuple[int, float]] = []
                _last_open = stream.candle_open_time
                for i, t in enumerate(new_ticks):
                    t_open = _floor_to_period(float(t["time"]), stream.period)
                    if _last_open > 0 and t_open != _last_open:
                        boundaries.append((i, t_open))
                        _last_open = t_open
                boundary_idx = boundaries[0][0] if boundaries else None

                if boundary_idx is not None:
                    # ── TICK-BASED CANDLE CLOSE ───────────────────────────────
                    # A tick crossed the period boundary — use its price as open.
                    # (Timer-close may have already fired; skip if so.)
                    tick_new_open = boundaries[0][1]

                    if tick_new_open > stream.candle_open_time:
                        # Timer hasn't fired yet — do a tick-based close, once
                        # per boundary crossed in this batch (normally exactly
                        # one; only >1 after a stall recovery — see above).
                        seg_start = 0
                        accuracy = None
                        for idx, new_open in boundaries:
                            for t in new_ticks[seg_start:idx]:
                                stream.ticks.append(float(t["price"]))

                            first_px = float(new_ticks[idx]["price"])
                            print(f"[feed] tick-close  {stream.asset}@{stream.period}s "
                                  f"{stream.candle_open_time} -> {new_open}  "
                                  f"(ticks: {len(stream.ticks)})")

                            accuracy = self._close_running_and_start_new(
                                stream, new_open, first_px, open_is_real=True)
                            seg_start = idx + 1

                        for t in new_ticks[seg_start:]:
                            stream.ticks.append(float(t["price"]))

                        new_running = self._running_candle(stream)
                        all_candles = stream.candles + [new_running]
                        await self._broadcast({
                            "type":       "eoc",
                            "asset":      stream.asset,
                            "period":     stream.period,
                            "candles":    all_candles[-300:],
                            "prediction": stream.prediction,
                            "accuracy":   accuracy,
                        })
                        # ── Signal-at-candle-start event ───────────────────────
                        # 0-second signal broadcast — fires IMMEDIATELY when a
                        # new 1-minute candle opens, carrying the prediction for
                        # that candle. External API clients (and the open API
                        # endpoint /api/v1/signals) can listen on /ws to receive
                        # this the moment the candle starts, then count down
                        # the candle's full duration as the signal's expiry.
                        # This is the user's explicit requirement:
                        #   "এক মিনিটের ক্যান্ডেল যখন 0 সেকেন্ড এ শুরু হবে,
                        #    টিক তখনি সিগন্যাল আসতে হবে।"
                        if stream.prediction and stream.prediction.get("signal") in (
                                "CALL", "PUT", "NEUTRAL"):
                            await self._broadcast({
                                "type":          "signal_start",
                                "asset":         stream.asset,
                                "period":        stream.period,
                                # Use the FINAL boundary's open time (the candle
                                # that's actually now running), not the first —
                                # matters when this batch closed more than one.
                                "candle_open_time": stream.candle_open_time,
                                "candle_expires_at": stream.candle_open_time + stream.period,
                                "signal":        stream.prediction["signal"],
                                "strength":      stream.prediction.get("strength"),
                                "confidence":    stream.prediction.get("confidence"),
                                "score":         stream.prediction.get("score"),
                                "agree":         stream.prediction.get("agree"),
                                "patterns_fired": stream.prediction.get("patterns_fired", []),
                                "reasons":       stream.prediction.get("reasons", [])[:5],
                                "prediction_candle": stream.prediction.get("candle"),
                            })
                    else:
                        # Timer already fired for this boundary. new_ticks can
                        # contain LATE ticks that belong to the candle we already
                        # closed and graded — appending those to the running
                        # candle corrupts its open/high/low (and every prediction
                        # built on it). Keep only ticks whose timestamp falls in
                        # the CURRENT candle window; drop the stale ones.
                        cur_ticks = [
                            t for t in new_ticks
                            if _floor_to_period(float(t["time"]), stream.period)
                            == stream.candle_open_time
                        ]
                        n_drop = len(new_ticks) - len(cur_ticks)
                        if n_drop:
                            # Aggregate instead of one line per batch: the
                            # drop is routine broker behaviour, not an error,
                            # and at several batches/second/pair it drowned
                            # the log. One summary line per minute per stream
                            # keeps the signal without the flood.
                            stream.late_drop_count += n_drop
                            _now_l = time.time()
                            if _now_l - stream.late_drop_last_log >= 60:
                                if stream.late_drop_last_log:
                                    print(f"[feed] dropped "
                                          f"{stream.late_drop_count} late "
                                          f"tick(s) from closed candles in "
                                          f"the last "
                                          f"{int(_now_l - stream.late_drop_last_log)}s "
                                          f"({stream.asset}@{stream.period}s)")
                                stream.late_drop_last_log = _now_l
                                stream.late_drop_count = 0

                        # First CURRENT-window tick after a timer-close is the
                        # candle's true open — re-anchor exactly like the
                        # same-candle branch does.
                        reanchored = False
                        if cur_ticks and not stream.candle_open_is_real:
                            real_open = float(cur_ticks[0]["price"])
                            stream.candle_open_price   = real_open
                            stream.candle_open_is_real = True
                            stream.ticks.clear()
                            stream.ticks.append(real_open)
                            cur_ticks = cur_ticks[1:]
                            if stream.prediction:
                                stream.prediction["candle"] = _pred_candle(
                                    stream.candles, stream.prediction["signal"],
                                    stream.period, real_open)
                            reanchored = True

                        for t in cur_ticks:
                            stream.ticks.append(float(t["price"]))
                        running = self._running_candle(stream)
                        if stream.candles and stream.candles[-1]["time"] == running["time"]:
                            stream.candles[-1] = running
                        msg = {
                            "type":   "tick",
                            "asset":  stream.asset,
                            "period": stream.period,
                            "candle": running,
                        }
                        if reanchored:
                            msg["prediction"] = stream.prediction
                        await self._broadcast(msg)

                else:
                    # ── SAME CANDLE — feed ALL new ticks, broadcast once ──────

                    # Bootstrap running candle from the very first tick when
                    # history was unavailable (live pair, API timeout, etc.)
                    if stream.candle_open_time == 0 and new_ticks:
                        ft = new_ticks[0]
                        stream.candle_open_time    = _floor_to_period(
                            float(ft["time"]), stream.period)
                        stream.candle_open_price   = float(ft["price"])
                        stream.candle_open_is_real = True
                        print(f"[feed] bootstrapped candle from tick "
                              f"({stream.asset}@{stream.period}s): "
                              f"t={stream.candle_open_time} "
                              f"open={stream.candle_open_price}")

                    # Re-anchor a timer-opened candle to its first REAL tick.
                    # After a timer-close the open was a placeholder, so the
                    # prediction candle was drawn from the wrong price. The first
                    # real tick fixes the open AND redraws the prediction candle
                    # so it starts exactly where the new market candle starts.
                    reanchored = False
                    if (not stream.candle_open_is_real) and new_ticks:
                        real_open = float(new_ticks[0]["price"])
                        stream.candle_open_price   = real_open
                        stream.candle_open_is_real = True
                        stream.ticks.clear()
                        stream.ticks.append(real_open)
                        new_ticks = new_ticks[1:]   # first tick became the open
                        if stream.prediction:
                            stream.prediction["candle"] = _pred_candle(
                                stream.candles, stream.prediction["signal"],
                                stream.period, real_open)
                        reanchored = True

                    for t in new_ticks:
                        stream.ticks.append(float(t["price"]))

                    running = self._running_candle(stream)

                    if not stream.candles:
                        stream.candles.append(running)
                    elif stream.candles[-1]["time"] < running["time"]:
                        stream.candles.append(running)
                    # Keep historical closed candles intact; the live candle is
                    # rendered from tick updates and does not need to overwrite
                    # the last completed bar in history.

                    # ── LIVE theory periodic re-eval (Method A, 2026-07-10) ──
                    # Re-run analyze_eoc with the running candle's own ticks
                    # every ~30 ticks so the LIVE vote can update mid-candle.
                    # Reuses the (closed candles, just-closed ticks) snapshot
                    # taken by _run_eoc at candle-open — stream.ticks now
                    # holds the NEW (still-open) candle's ticks instead.
                    #
                    # 2026-08-11: also accept NEUTRAL updates — if the
                    # fresh analysis says "no edge" mid-candle, we should
                    # surface that to the UI (show "WAIT") rather than
                    # keep displaying a stale CALL/PUT from candle open.
                    pred_changed = False
                    if (ENABLE_LIVE_THEORY and stream.base_candles
                            and len(stream.ticks) >= 15
                            and len(stream.ticks) - stream._live_reeval_ticks >= 30):
                        try:
                            fresh, _ = self._analyze_core(
                                stream.asset, stream.period,
                                stream.base_candles, stream.base_ticks,
                                running_ticks=list(stream.ticks))
                            stream._live_reeval_ticks = len(stream.ticks)
                            if fresh and fresh.get("signal") in ("CALL", "PUT", "NEUTRAL"):
                                # Only mark changed if the signal ACTUALLY
                                # differs — avoids spurious broadcasts when
                                # the LIVE vote is stable.
                                _old_sig = (stream.prediction or {}).get("signal")
                                if _old_sig != fresh.get("signal"):
                                    stream.prediction = {
                                        **(stream.prediction or {}), **fresh}
                                    # Re-render the prediction candle for
                                    # the new signal (esp. NEUTRAL's flat
                                    # doji shape — see _pred_candle).
                                    if stream.prediction:
                                        stream.prediction["candle"] = _pred_candle(
                                            stream.candles,
                                            stream.prediction["signal"],
                                            stream.period,
                                            stream.candle_open_price)
                                    pred_changed = True
                        except Exception as exc:
                            print(f"[feed] LIVE re-eval error "
                                  f"({stream.asset}@{stream.period}s): {exc}")

                    # ── Strength gate (Method B, 2026-07-10, untested) ──────────
                    if (ENABLE_STRENGTH_GATE and stream.prediction
                            and stream.prediction.get("signal") != "NEUTRAL"):
                        gated = self._apply_strength_gate(stream, stream.prediction)
                        if gated is not stream.prediction:
                            stream.prediction = gated
                            pred_changed = True

                    # Skip broadcast if open price is still 0 (no valid tick yet)
                    # — prevents LightweightCharts "Value is null" on the client
                    if stream.candle_open_price > 0:
                        msg = {
                            "type":          "tick",
                            "asset":         stream.asset,
                            "period":        stream.period,
                            "candle":        running,
                            "running_conf":  self._running_confirmation(stream),
                            "micro":         self._analyze_microstructure(
                                                 stream.ticks, stream.candle_open_price),
                        }
                        # Carry the re-anchored/re-evaluated/gated prediction
                        # so the client redraws its signal panel from it.
                        if reanchored or pred_changed:
                            msg["prediction"] = stream.prediction
                        await self._broadcast(msg)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                import traceback
                print(f"[feed] stream {stream.asset}@{stream.period}s "
                      f"loop error: {exc}")
                traceback.print_exc()
                self._record_stream_error()
                await asyncio.sleep(2)
                continue

            await self._smart_sleep(stream)

    async def _run_stream(self, stream: _AssetStream) -> None:
        """Owns one _AssetStream for its whole life: start, run, clean up.
        Nothing else ever mutates this stream's state, which structurally
        rules out the cross-asset contamination bugs the old singleton design
        needed manual mid-await guards against."""
        key = (stream.asset, stream.period)
        try:
            # Wait for the shared Quotex connection FIRST. Viewers' tabs
            # subscribe the instant the server comes up after a deploy —
            # before connect() has finished — and starting then meant
            # start_candles_stream went out on a not-yet-authorized socket
            # (a dead subscription: zero ticks until the 90s stale re-arm),
            # the history fetch burned its full ~25s of timeouts, and the
            # resulting _record_stream_error hits tripped the cooldown,
            # blocking every OTHER pair for 2 more minutes. Observed live
            # on Railway as "blank chart for minutes after every deploy".
            while not (self._connected and self._client):
                await asyncio.sleep(0.1)
            # Two lanes. Bulk always-on pre-warm holds _new_stream_gate for
            # several seconds per pair, ~40 pairs deep — a viewer-requested
            # stream that queued behind it used to wait minutes for its first
            # candle. Priority streams get their own gate and a much tighter
            # stagger, so a watched chart paints while the pre-warm grinds on
            # in the background.
            gate    = self._prio_stream_gate if stream.priority else self._new_stream_gate
            stagger = self._prio_stagger     if stream.priority else self._stagger_gap
            async with gate:
                await self._start_stream(stream)
                await asyncio.sleep(stagger)   # paces the NEXT waiting stream
            await self._stream_loop(stream)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            import traceback
            print(f"[feed] stream {key} failed to start: {exc}")
            traceback.print_exc()
            self._record_stream_error()
        finally:
            try:
                if self._client and stream.sub_started:
                    await self._client.stop_candles_stream(stream.asset)
            except Exception:
                pass
            self._streams.pop(key, None)
            print(f"[feed] stream {key} stopped")

    async def _rearm_stream(self, stream: _AssetStream) -> None:
        """After our own full client rebuild only — native reconnects
        self-heal via pyquotex's own subscription replay and need nothing
        here. Deliberately does NOT refetch history (existing candles/ticks
        are kept, ticks just resume) — refetching N histories on every
        rebuild is exactly the kind of burst this design exists to avoid."""
        async with self._new_stream_gate:
            try:
                if self._client:
                    await self._client.start_candles_stream(stream.asset, stream.period)
                    stream.sub_started = True
                    stream.last_real_tick_wall = time.time()
            except Exception:
                self._record_stream_error()
            await asyncio.sleep(self._stagger_gap)

    async def _rebuild_client(self) -> None:
        for s in self._streams.values():
            await self._broadcast({"type": "stale", "asset": s.asset, "period": s.period})
        try:
            if self._client:
                await self._client.close()
        except Exception:
            pass
        self._client, self._connected = None, False
        self._record_stream_error()

    async def _refresh_theory_mutes(self) -> None:
        """
        Refresh the theory mute set from live 7-day per-theory accuracy
        (db.theory_perf over theory_votes — includes shadow-graded NEUTRAL
        predictions, so muted theories keep building the record that can
        un-mute them).

        Hysteresis: mute below 45% (n>=100 so a true-coin-flip theory rarely
        false-trips), un-mute at 48%+ — the gap stops borderline theories
        from flapping in and out every refresh.
        """
        MUTE_BELOW, UNMUTE_AT, MIN_N = 45.0, 48.0, 100
        try:
            perf = await asyncio.to_thread(
                _db.theory_perf, None, None, 7, MIN_N)
        except Exception as exc:
            print(f"[feed] theory_perf refresh error: {exc}")
            return
        for code, st in perf.items():
            rate, n = st["rate"], st["n"]
            note = f"{rate:.0f}% n={n}/7d"
            if code in self._muted_theories:
                if rate >= UNMUTE_AT:
                    del self._muted_theories[code]
                    print(f"[feed] theory {code} UN-MUTED ({note})")
                else:
                    self._muted_theories[code] = note   # keep annotation fresh
            elif rate < MUTE_BELOW:
                self._muted_theories[code] = note
                print(f"[feed] theory {code} MUTED ({note})")

    def _reconcile_always_on(self) -> None:
        """
        Keep the always-on set in sync with the latest payout/market data
        (called right after _load_pairs). Pre-warms every eligible forex
        pair's 1m stream and never idle-evicts it, so switching between
        tradeable pairs is instant instead of a cold start.

        A stream stops being always_on the moment its (asset, 60) key is no
        longer in the eligible set — that covers BOTH a payout dropping
        below PAYOUT_FLOOR AND a pair's asset code swapping real<->otc (the
        unified pairs list always represents a logical pair with a single
        CURRENT asset code, so a real/otc flip makes the OLD code vanish
        from `eligible` on its own). Demoted streams simply become normal
        on-demand streams, subject to the usual idle sweep — never killed
        outright, matching ensure_stream's "never tear down a running
        stream" philosophy.

        ALL_PAIRS_ALWAYS_ON (default "1"): when set, ALL open pairs (live
        + OTC) are kept always-on regardless of payout floor — the user's
        explicit requirement is "every pair every candle gets a signal".
        Set to "0" to restore the payout-gated behavior.
        """
        # When ALL_PAIRS_ALWAYS_ON is enabled, every open pair (live + OTC)
        # is pre-warmed regardless of payout. User requirement: "every pair
        # every candle must have a signal".
        all_pairs = os.environ.get("ALL_PAIRS_ALWAYS_ON", "1") == "1"

        if all_pairs:
            eligible = {(p["asset"], 60) for p in self._pairs_list
                        if p["status"] in ("live", "otc")}
        else:
            eligible = {(p["asset"], 60) for p in self._pairs_list
                        if p["status"] in ("live", "otc") and not p.get("locked")}

        for key, s in self._streams.items():
            if s.always_on and key not in eligible:
                s.always_on = False

        # `eligible` is a set, so iterating it directly started the ~40
        # pre-warm streams in arbitrary hash order — the pair a viewer was
        # actually watching could land anywhere in the queue, which is why
        # "sometimes it connects fast, sometimes it takes minutes" looked
        # random. Sort deterministically, and float any pair that already has
        # a viewer (or is already known to the stream table) to the front.
        watched = {k for k, s in self._streams.items() if s.interested_cids}

        def _rank(key):
            return (0 if key in watched else 1, key[0])

        started = 0
        for key in sorted(eligible, key=_rank):
            s = self._streams.get(key)
            if s is None:
                asset, period = key
                s = _AssetStream(asset=asset, period=period, always_on=True)
                self._streams[key] = s
                s.task = asyncio.create_task(self._run_stream(s))
                started += 1
            else:
                s.always_on = True
                s.idle_since = None

        # Single most useful diagnostic line in the whole boot sequence: if
        # eligible=0 the pairs list never loaded, and no amount of staring at
        # the frontend will explain the empty charts.
        print(f"[feed] prewarm: eligible={len(eligible)} "
              f"newly_started={started} existing={len(eligible) - started} "
              f"total_streams={len(self._streams)}")

    async def _deferred_prewarm(self) -> None:
        """Run _reconcile_always_on after a short head start for viewers.

        REGRESSION NOTE: the first version of this simply did
            if self._connected: self._reconcile_always_on()
        which turned a guaranteed inline call into a fire-and-forget task
        that SILENTLY did nothing if the socket happened to be mid-blip at
        the 3s mark. The only recovery was the 300s pairs refresh, so a
        single unlucky moment meant up to five minutes with zero always-on
        streams and no log line explaining why. Now it waits for the
        connection instead of giving up, and always says what it did.
        """
        if self._prewarm_delay > 0:
            await asyncio.sleep(self._prewarm_delay)

        # Wait out a transient blip rather than skipping the pre-warm.
        deadline = time.time() + 30
        while not self._connected and time.time() < deadline:
            await asyncio.sleep(0.5)

        if not self._connected:
            print("[feed] prewarm: still not connected after 30s — "
                  "skipping; the 300s pairs refresh will retry")
            return
        if not self._pairs_list:
            print("[feed] prewarm: pairs list is EMPTY — nothing to "
                  "pre-warm. _load_pairs likely failed; no candles will "
                  "flow until it succeeds.")
            return
        try:
            self._reconcile_always_on()
        except Exception as exc:
            # Previously this raised into a fire-and-forget task whose
            # exception nobody retrieved, so it vanished without a trace.
            import traceback
            print(f"[feed] prewarm: _reconcile_always_on FAILED: {exc}")
            traceback.print_exc()

    def _sweep_idle_streams(self) -> None:
        IDLE_TIMEOUT = 300   # 5 minutes with no interested viewers
        now = time.time()
        for key, s in list(self._streams.items()):
            if s.always_on:
                continue
            if s.interested_cids:
                s.idle_since = None
                continue
            if s.idle_since is None:
                s.idle_since = now
            elif now - s.idle_since > IDLE_TIMEOUT:
                print(f"[feed] evicting idle stream {key} "
                      f"(no viewers for {IDLE_TIMEOUT}s)")
                if s.task:
                    s.task.cancel()
                self._streams.pop(key, None)

    def _record_stream_error(self) -> None:
        """Rolling error window -> temporary cooldown on starting NEW streams.
        Existing streams are never torn down by this — only ensure_stream()'s
        capacity/cooldown gate for brand-new pairs is affected."""
        WINDOW, THRESHOLD, DURATION = 60, 4, 120
        now = time.time()
        self._recent_errors.append(now)
        self._recent_errors[:] = [t for t in self._recent_errors if t > now - WINDOW]
        if len(self._recent_errors) >= THRESHOLD and now >= self._cooldown_until:
            self._cooldown_until  = now + DURATION
            self._cooldown_reason = "connection errors"
            print(f"[feed] error spike ({len(self._recent_errors)}/{WINDOW}s) — "
                  f"cooling down new streams for {DURATION}s")

    async def _idle(self, secs: float) -> None:
        """Sleep, but wake early if set_token() fires the wake Event.

        Every place the manager loop used to sit on a bare asyncio.sleep()
        now goes through here. That is the difference between "operator
        pastes a token and the connect starts within milliseconds" and the
        old "operator pastes a token, then waits out the rest of a 5s
        housekeeping tick before anything happens".
        """
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass          # normal path: nothing woke us, full sleep elapsed
        finally:
            self._wake.clear()

    # ── Manager loop ──────────────────────────────────────────────────────────

    async def run(self, broadcast) -> None:
        self._broadcast = broadcast
        _db.init()          # create DB tables if not exist
        _db.cleanup()       # prune rows older than 7 days

        HOUSEKEEP_SECS    = 5
        GLOBAL_STALE_SECS = 90

        while True:
            try:
                # ── Connect (shared across all streams) ───────────────────
                if not self._connected:
                    # One-shot login gate: if the previous _connect() attempt
                    # failed for ANY reason (reject, timeout, network error),
                    # we do NOT retry automatically. The operator must paste
                    # a fresh SSID via POST /api/token to clear the gate and
                    # trigger exactly one more attempt. This is the user's
                    # explicit requirement:
                    #   "যদি কোনো কারণে প্রথম বারে লগিং ফেইল হলে,
                    #    সেকেন্ড টাইম আর লগিং ট্রাই করা যাবে না।"
                    if self._login_failed_permanently:
                        # Idle until set_token() clears the gate. Print once
                        # every ~5 min (60 manager cycles × 5s) so the log
                        # isn't spammed but the operator still sees a heartbeat.
                        if self._reconnect_attempts % 60 == 0:
                            print(f"[feed] login previously failed — waiting "
                                  f"for a fresh SSID paste via Settings tab "
                                  f"(reason: {self._login_fail_reason})")
                        self._reconnect_attempts += 1
                        await self._idle(HOUSEKEEP_SECS)
                        continue
                    print("[feed] connecting...")
                    self._connected = await self._connect()
                    if not self._connected:
                        if self._login_failed_permanently:
                            # _connect set the fail gate — stop retrying.
                            # The above branch will pick it up next iteration.
                            self._reconnect_attempts += 1
                            await self._idle(HOUSEKEEP_SECS)
                            continue
                        # Soft-fail: either no token yet, or a transient
                        # connect error that still has retry budget left.
                        # Either way we sleep on the wake Event, so a token
                        # paste cuts the wait short instead of the old fixed
                        # sleep.
                        self._reconnect_attempts += 1
                        if self._transient_retries_used > 0:
                            # Exponential backoff on real transient failures:
                            # 2s, 4s, 8s (capped) — enough to ride out a
                            # broker hiccup without hammering it.
                            delay = min(8.0, 2.0 ** self._transient_retries_used)
                        else:
                            # No token yet — poll tightly so the instant one
                            # lands (or the env var appears) we connect.
                            delay = 1.0
                        print(f"[feed] connect attempt {self._reconnect_attempts} "
                              f"failed (soft) — retrying in {delay}s")
                        await self._idle(delay)
                        continue
                    self._reconnect_attempts = 0          # reset on success
                    print("[feed] connected OK")
                    await self._load_pairs(broadcast)

                    # A brand-new client has an empty subscription set — any
                    # streams that were already running (e.g. survived a
                    # previous client's death) need their subscription
                    # re-issued, staggered like any other stream start.
                    for stream in list(self._streams.values()):
                        stream.sub_started = False
                        self._spawn(self._rearm_stream(stream))

                    # Pre-warm every payout-eligible forex pair's 1m stream —
                    # runs AFTER the rearm loop above so freshly-created
                    # streams here don't also get caught by that loop (which
                    # only means to re-issue already-existing subscriptions).
                    #
                    # Deferred by QX_PREWARM_DELAY_SEC: right after connect
                    # the viewers' tabs are firing /api/subscribe for the pair
                    # they're actually looking at. Kicking off ~40 background
                    # streams in the same tick made those viewer requests race
                    # a saturated socket. A few seconds of head start costs
                    # the pre-warm nothing (it is background work by
                    # definition) and gets the visible chart painting sooner.
                    self._spawn(self._deferred_prewarm())

                # ── Global stale watchdog (backstop) ──────────────────────
                # pyquotex's native ReconnectPolicy handles most drops itself.
                # This is the LAST resort: if EVERY active stream has been
                # silent for a while, the native layer failed — tear the
                # whole client down and rebuild it (per-stream staleness is
                # handled inside each stream's own loop and never reaches
                # here, since it only re-arms that one stream).
                if self._streams:
                    newest = max((s.last_real_tick_wall
                                 for s in self._streams.values()), default=0.0)
                    if newest > 0 and time.time() - newest > GLOBAL_STALE_SECS:
                        print("[feed] GLOBAL STALE: every active stream silent "
                              "— rebuilding client")
                        await self._rebuild_client()

                self._sweep_idle_streams()

                # ── Refresh pair list every 5 minutes (market open/close) ──
                if time.time() - self._last_pairs_refresh > 300:
                    await self._load_pairs(broadcast)
                    self._reconcile_always_on()

                # ── Refresh theory mute set every 5 minutes ────────────────
                if time.time() - self._last_perf_refresh > 300:
                    self._last_perf_refresh = time.time()
                    await self._refresh_theory_mutes()

                # ── DB row-count cleanup every 6 hours ─────────────────────
                # asyncio.to_thread: _db.cleanup() is blocking sqlite3 I/O
                # (holds db._lock) — same reasoning as every other DB call
                # on this event loop (see _authenticate in server.py).
                if time.time() - self._last_db_cleanup > 6 * 3600:
                    self._last_db_cleanup = time.time()
                    try:
                        await asyncio.to_thread(_db.cleanup)
                    except Exception as exc:
                        print(f"[feed] periodic db.cleanup() failed: {exc}")

            except Exception as exc:
                import traceback
                print(f"[feed] manager loop error: {exc}")
                traceback.print_exc()

            await self._idle(HOUSEKEEP_SECS)
