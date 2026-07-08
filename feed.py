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

# Minimum live 1-minute payout % for a forex pair to be tradeable in this
# app — pairs below this are blocked from streaming outright (not just from
# always-on pre-warming), matching the win-rate-needs-to-clear-payout math
# already shown in the signal bar (see stream.payout / signal-payout in
# chart.js). Overridable per-deployment since Quotex's payout schedule can
# vary by broker account/region.
PAYOUT_FLOOR = int(os.environ.get("QX_PAYOUT_FLOOR", "81"))

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


# ── Pair catalog, split by category ──────────────────────────────────────────
# The app now only ever streams/lists forex pairs (see _load_pairs) — the
# other categories are kept here only as documentation of what's excluded,
# and so re-adding a category later is a one-line change.
_FOREX_OTC = [
    # Forex majors OTC
    "EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "USDCHF_otc",
    "AUDUSD_otc", "NZDUSD_otc", "USDCAD_otc",
    # Forex minors OTC
    "EURGBP_otc", "EURJPY_otc", "EURAUD_otc", "EURCHF_otc", "EURCAD_otc",
    "GBPJPY_otc", "GBPAUD_otc", "GBPCAD_otc", "GBPCHF_otc", "GBPNZD_otc",
    "AUDJPY_otc", "AUDCAD_otc", "AUDNZD_otc", "AUDCHF_otc",
    "CADJPY_otc", "CADCHF_otc",
    "NZDJPY_otc", "NZDCAD_otc", "NZDCHF_otc",
    "CHFJPY_otc", "EURNZD_otc",
    # Forex exotics OTC
    "USDMXN_otc", "USDTRY_otc", "USDPKR_otc", "USDCOP_otc",
    "USDBDT_otc", "INRUSD_otc", "EURSGD_otc",
    "BRLUSD_otc", "USDARS_otc", "USDDZD_otc",
]
_STOCKS_OTC = [
    "MSFT_otc", "INTC_otc", "JNJ_otc", "AXP_otc",
    "BA_otc",   "META_otc", "MCD_otc", "PFE_otc",
]
_CRYPTO_OTC = ["BTCUSD_otc", "ETHUSD_otc"]
_COMMODITIES_OTC = ["XAUUSD_otc", "XAGUSD_otc", "USOIL_otc", "UKBRENT_otc"]

# Logical base symbols (no _otc suffix) that count as forex — used to filter
# the REAL Quotex instrument list in _load_pairs, not just this fallback. A
# curated whitelist rather than a "3-letter currency code" regex deliberately:
# XAU/XAG are real ISO-4217 codes too, so a currency-code heuristic would
# misclassify gold/silver (XAUUSD/XAGUSD) as forex.
_FOREX_BASES = {a[:-4] if a.endswith("_otc") else a for a in _FOREX_OTC}

# Fallback pair list (shown while Quotex instruments load) — forex only, to
# match what _load_pairs serves once connected.
_FALLBACK_ASSETS = _FOREX_OTC

_DEFAULT_PAIRS: list[dict] = [
    {"asset": a, "display": _api_to_display(a), "status": "otc",
     "payout": None, "locked": False}
    for a in _FALLBACK_ASSETS
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
    interested_cids: set = field(default_factory=set)   # viewer client-ids watching
    idle_since: float | None = None
    created_at: float = field(default_factory=time.time)


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
        self._stagger_gap     = float(os.environ.get("QX_STAGGER_GAP_SEC", "1.5"))
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

        # DB row-count housekeeping. Was startup-only for a long time (see
        # run()'s initial _db.cleanup() call) — this service can stay up for
        # weeks without a redeploy, so unbounded growth between restarts
        # filled the Railway volume to 83% (2026-07-08 incident). Now also
        # re-run periodically from the manager loop.
        self._last_db_cleanup: float = 0.0

    # ── Public ────────────────────────────────────────────────────────────────

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

    async def ensure_stream(self, asset: str, period: int,
                            cid: str | None = None) -> dict:
        """
        Called from /api/subscribe. Starts a stream for (asset, period) if one
        isn't already running, subject to the capacity cap / error cooldown.
        An already-running stream is NEVER rejected or torn down here — those
        guards only gate the creation of a brand-new stream.
        """
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
        pair = next((p for p in self._pairs_list if p["asset"] == asset), None)
        if pair and pair.get("locked"):
            return {"ok": False, "status": "locked", "payout": pair.get("payout"),
                    "reason": f"Needs {PAYOUT_FLOOR}% payout "
                              f"(currently {pair.get('payout', '?')}%)"}

        if time.time() < self._cooldown_until:
            return {"ok": False, "status": "cooldown",
                    "retry_after": round(self._cooldown_until - time.time(), 1),
                    "reason": self._cooldown_reason}
        if len(self._streams) >= self._max_streams:
            return {"ok": False, "status": "at_capacity", "max": self._max_streams}

        stream = _AssetStream(asset=asset, period=period)
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
        for s in list(self._streams.values()):
            if s.task:
                s.task.cancel()

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

    def _clear_stale_token(self) -> None:
        """
        Auto-heal the "authorization/reject" loop (documented project issue):
        pyquotex persists the session to session.json on disk, and its own
        internal connect() logic replays that token on the NEXT attempt even
        for a brand-new client — so a rejected/expired token keeps getting
        rejected forever unless it's cleared. Previously this required a
        manual fix each time; now it runs automatically right after a
        rejection so the exponential-backoff retry in run() self-heals.
        """
        import json as _json
        # pyquotex writes session.json relative to the process's CURRENT
        # WORKING DIRECTORY (pyquotex/config.py's base_dir = Path.cwd(), NOT
        # the root_path constructor arg — an upstream quirk, confirmed by
        # reading config.py/stable_api.py directly) — so this must match cwd,
        # not __file__, for the two to ever agree on the same file. Both
        # local dev (`cd` into the project first) and Railway's default
        # working directory satisfy this.
        path = os.path.join(os.getcwd(), "session.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            changed = False
            for acct in data.values():
                if isinstance(acct, dict) and acct.get("token"):
                    acct["token"] = None
                    changed = True
            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump(data, f)
                print("[feed] cleared stale session token after auth rejection "
                      "— next retry will do a fresh login")
        except FileNotFoundError:
            pass
        except Exception as _e:
            print(f"[feed] could not clear stale token: {_e}")

    def _make_client(self, ua: str, root: str):
        from pyquotex.stable_api import Quotex
        from pyquotex.types import ReconnectPolicy
        return Quotex(
            email    = os.environ.get("QX_EMAIL",    ""),
            password = os.environ.get("QX_PASSWORD", ""),
            host     = "market-qx.trade",
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

    async def _connect(self) -> bool:
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

            # ── Attempt 1: TOKEN (fast path, skipped if no token) ─────────────
            env_token = os.environ.get("QX_TOKEN", "").strip()
            if env_token:
                self._client = self._make_client(ua, root)
                self._client.set_session(user_agent=ua, ssid=env_token)
                print(f"[feed] connecting with session token={env_token[:8]}...")
                try:
                    ok, reason = await asyncio.wait_for(
                        self._client.connect(), timeout=30)
                    if ok:
                        self._remember_token()
                        print(f"[feed] connect -> ok=True  reason={reason}")
                        return True
                    print(f"[feed] token auth failed ({reason}) — trying login")
                except Exception as _te:
                    print(f"[feed] token attempt error: {_te}")
                # Token failed — close this client cleanly before making a new one
                await self._close_client(self._client)
                # Invalidate the stale token so next reconnect skips this path
                os.environ.pop("QX_TOKEN", None)

            # ── Attempt 2: FRESH client, email/password only ──────────────────
            # A brand-new Quotex instance has no leftover WebSocket state from
            # the failed token attempt, so pyquotex does a clean HTTP login.
            print("[feed] connecting via email/password (fresh client)...")
            self._client = self._make_client(ua, root)
            ok, reason = await asyncio.wait_for(
                self._client.connect(), timeout=45)
            print(f"[feed] connect -> ok={ok}  reason={reason}")
            if ok:
                self._remember_token()
                return True
            if reason and "reject" in str(reason).lower():
                self._clear_stale_token()

            # ── Attempt 3: auth may have succeeded internally but connect()
            #    returned False (pyquotex race condition). If session_data now
            #    holds a fresh token, one more connect() often succeeds. ────────
            new_tok = (self._client.session_data or {}).get("token", "")
            if new_tok and new_tok != env_token:
                print(f"[feed] retrying with fresh token={new_tok[:8]}...")
                try:
                    ok, reason = await asyncio.wait_for(
                        self._client.connect(), timeout=30)
                    print(f"[feed] retry -> ok={ok}  reason={reason}")
                    if ok:
                        self._remember_token()
                        return True
                    if reason and "reject" in str(reason).lower():
                        self._clear_stale_token()
                except Exception as _re:
                    print(f"[feed] retry error: {_re}")

            return False
        except Exception as exc:
            print(f"[feed] connect error: {exc}")
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
                      ticks: list[float]) -> tuple[dict | None, list]:
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
        """
        if len(candles) < 5:
            return None, []
        micro_hist = _db.get_micro_history(
            asset, period, n=5,
            before_ctime=candles[-1]["time"])
        result = analyze_eoc(candles, ticks,
                             micro_history=micro_hist,
                             period=period,
                             muted=self._muted_theories,
                             asset=asset)
        return result, micro_hist

    def _run_eoc(self, stream: _AssetStream,
                actual_open: float | None = None) -> dict | None:
        closed = stream.candles
        result, micro_hist = self._analyze_core(
            stream.asset, stream.period, closed, list(stream.ticks))
        if result is None:
            return None

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

        # Neutral signals should remain neutral; do not force a fake CALL/PUT
        # just to keep a ghost candle on screen.
        if result["signal"] == "NEUTRAL":
            return {**result, "candle": None, "payout": stream.payout}
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

    async def _smart_sleep(self, stream: _AssetStream) -> None:
        """Sleep until next tick poll, but wake up early at candle boundary."""
        if stream.candle_open_time > 0:
            close_at     = stream.candle_open_time + stream.period
            until_close  = close_at - time.time()
            sleep_dur    = max(0.01, min(0.05, until_close))
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
        await asyncio.sleep(1)  # let first ticks arrive

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
                now = time.time()
                if (stream.candle_open_time > 0
                        and now >= stream.candle_open_time + stream.period + TIMER_GRACE):
                    expected_new = _floor_to_period(now, stream.period)
                    # Only ever move FORWARD in time (never reopen an older candle)
                    if expected_new > stream.candle_open_time:
                        last_px = (list(stream.ticks)[-1] if stream.ticks
                                   else stream.candle_open_price)
                        print(f"[feed] timer-close {stream.asset}@{stream.period}s "
                              f"{stream.candle_open_time} -> {expected_new}")
                        accuracy = self._close_running_and_start_new(
                            stream, expected_new, last_px, open_is_real=False)
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

                # ── Find if any tick crossed a candle boundary ────────────────
                boundary_idx = None
                for i, t in enumerate(new_ticks):
                    t_open = _floor_to_period(float(t["time"]), stream.period)
                    if stream.candle_open_time > 0 and t_open != stream.candle_open_time:
                        boundary_idx = i
                        break

                if boundary_idx is not None:
                    # ── TICK-BASED CANDLE CLOSE ───────────────────────────────
                    # A tick crossed the period boundary — use its price as open.
                    # (Timer-close may have already fired; skip if so.)
                    boundary_tick = new_ticks[boundary_idx]
                    tick_new_open = _floor_to_period(
                        float(boundary_tick["time"]), stream.period)

                    if tick_new_open > stream.candle_open_time:
                        # Timer hasn't fired yet — do a tick-based close.
                        # boundary_tick is a REAL first tick → open_is_real=True.
                        for t in new_ticks[:boundary_idx]:
                            stream.ticks.append(float(t["price"]))

                        first_px = float(boundary_tick["price"])
                        print(f"[feed] tick-close  {stream.asset}@{stream.period}s "
                              f"{stream.candle_open_time} -> {tick_new_open}  "
                              f"(ticks: {len(stream.ticks)})")

                        accuracy = self._close_running_and_start_new(
                            stream, tick_new_open, first_px, open_is_real=True)

                        for t in new_ticks[boundary_idx + 1:]:
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
                            print(f"[feed] dropped {n_drop} late tick(s) from "
                                  f"closed candle ({stream.asset}@{stream.period}s)")

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
                        # Carry the re-anchored prediction so the client redraws
                        # it from the true open on this first real tick.
                        if reanchored:
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
                await asyncio.sleep(0.5)
            async with self._new_stream_gate:
                await self._start_stream(stream)
                await asyncio.sleep(self._stagger_gap)   # paces the NEXT waiting stream
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
        """
        eligible = {(p["asset"], 60) for p in self._pairs_list
                    if p["status"] in ("live", "otc") and not p.get("locked")}

        for key, s in self._streams.items():
            if s.always_on and key not in eligible:
                s.always_on = False

        for key in eligible:
            s = self._streams.get(key)
            if s is None:
                asset, period = key
                s = _AssetStream(asset=asset, period=period, always_on=True)
                self._streams[key] = s
                s.task = asyncio.create_task(self._run_stream(s))
            else:
                s.always_on = True
                s.idle_since = None

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
                    print("[feed] connecting...")
                    self._connected = await self._connect()
                    if not self._connected:
                        # Exponential backoff (10→20→40→60s, capped) so repeated
                        # failures don't hammer Quotex into a 429 rate-limit.
                        self._reconnect_attempts += 1
                        delay = min(10 * (2 ** (self._reconnect_attempts - 1)), 60)
                        print(f"[feed] reconnect attempt {self._reconnect_attempts} "
                              f"failed — retrying in {delay}s")
                        self._record_stream_error()
                        await asyncio.sleep(delay)
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
                        asyncio.create_task(self._rearm_stream(stream))

                    # Pre-warm every payout-eligible forex pair's 1m stream —
                    # runs AFTER the rearm loop above so freshly-created
                    # streams here don't also get caught by that loop (which
                    # only means to re-issue already-existing subscriptions).
                    self._reconcile_always_on()

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

            await asyncio.sleep(HOUSEKEEP_SECS)
