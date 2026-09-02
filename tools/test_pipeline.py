"""E2E pipeline test: synthetic ticks -> frozen prediction -> two-phase log
-> honest stats, using the REAL feed methods (no broker connection)."""
import os, sys, time, json, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["QX_DB_PATH"] = "/tmp/nova_pipe_test.db"
if os.path.exists("/tmp/nova_pipe_test.db"):
    os.remove("/tmp/nova_pipe_test.db")

import db as _db
_db.init()
import feed as F
import random


class FakeClient:
    """Bare-minimum client stand-in — the pipeline under test never dials out."""
    class _API:
        timesync = type("T", (), {"server_timestamp": time.time()})()
    api = _API()


async def main():
    qf = F.QuotexFeed()
    qf._client = FakeClient()
    qf._connected = True

    s = F._AssetStream(asset="BRLUSD_otc", period=60, always_on=True)
    qf._streams[("BRLUSD_otc", 60)] = s

    rng = random.Random(7)
    t0 = int(time.time()) - 400 * 60
    t0 = t0 - (t0 % 60)

    # Seed 120 closed candles of history so the council has data
    px = 5.5
    hist = []
    for i in range(120):
        o = px
        c = o + rng.gauss(0, 0.002)
        h = max(o, c) + abs(rng.gauss(0, 0.001))
        l = min(o, c) - abs(rng.gauss(0, 0.001))
        hist.append({"time": t0 + i * 60, "open": o, "high": h, "low": l, "close": c})
        px = c

    s.candles = hist
    s.candle_open_time = hist[-1]["time"] + 60
    s.candle_open_price = px
    s.candle_open_is_real = False
    s.last_tick_ts = 0.0

    broadcast_log = []
    async def bc(msg):
        broadcast_log.append(msg)

    qf._broadcast = bc

    # ── Run 60 candle closes through the REAL close path ────────────────
    price = px
    for k in range(60):
        open_t = s.candle_open_time
        # simulate the running candle's ticks
        o = price
        ticks = [o]
        cur = o
        for _ in range(30):
            cur += rng.gauss(0, 0.0008)
            ticks.append(cur)
        price = cur
        s.ticks.clear()
        s.ticks.extend(ticks)
        s.candle_open_price = o
        first_tick = price + rng.gauss(0, 0.0004)
        acc = qf._close_running_and_start_new(
            s, open_t + 60, first_tick, open_is_real=True)
        price = first_tick

    # ── Verify ──────────────────────────────────────────────────────────
    with _db._lock:
        con = _db._connect()
        rows = con.execute(
            "SELECT ctime, signal, result, strength, agree, emitted_at, graded_at "
            "FROM signal_log ORDER BY ctime").fetchall()
        con.close()

    results = [r[2] for r in rows]
    n_pending = results.count("pending")
    n_final = sum(1 for r in results if r in ("correct", "wrong", "draw", "no_data"))
    n_calls = sum(1 for r in rows if r[1] == "CALL")
    n_puts = sum(1 for r in rows if r[1] == "PUT")

    print(f"signal_log rows: {len(rows)} (60 candles ran)")
    print(f"  CALL={n_calls} PUT={n_puts}")
    print(f"  pending={n_pending} finalized={n_final}")
    from collections import Counter
    print(f"  results: {dict(Counter(results))}")

    # Integrity checks
    assert len(rows) <= 60, "more rows than candles!"
    assert n_final + n_pending == len(rows)
    # every finalized row must have graded_at > emitted_at
    bad = [r for r in rows if r[5] and r[6] and r[6] < r[5]]
    assert not bad, f"graded before emitted: {bad}"
    # strength values must be the new vocabulary
    strengths = {r[3] for r in rows}
    assert strengths <= {"STRONG", "MEDIUM", "NONE", None}, f"bad strengths: {strengths}"

    # stats must reconcile
    st = _db.get_stats(days=None, period=60)
    print("stats:", {k: st[k] for k in ("total", "decided", "correct", "wrong", "draws", "no_data", "pending", "rate")})
    assert st["total"] == len(rows)
    assert st["decided"] + st["draws"] + st["no_data"] + st["pending"] == st["total"]

    # signals logged must match the frozen prediction objects returned by
    # the close path (broadcast happens in the stream loop — the frozen
    # guarantee is what matters: DB row == prediction at emission).
    traded = [r for r in rows if r[1] in ("CALL", "PUT")]
    assert all(r[3] in ("STRONG", "MEDIUM") for r in traded), "bad strength"
    assert all(r[4] >= 3 for r in traded), "emitted below MIN_AGREE!"
    # Frozen guarantee: if the CURRENT prediction is CALL/PUT, the DB must
    # hold exactly one pending row for exactly that candle+signal.
    if s.prediction and s.prediction["signal"] in ("CALL", "PUT"):
        target = s.candle_open_time
        cur = [r for r in rows if r[0] == target]
        assert len(cur) == 1, f"expected 1 pending row for {target}, got {len(cur)}"
        assert cur[0][1] == s.prediction["signal"], (
            f"drift: db={cur[0][1]} pred={s.prediction['signal']}")
        assert cur[0][2] == "pending"
    elif s.prediction and s.prediction["signal"] == "NEUTRAL":
        cur = [r for r in rows if r[0] == s.candle_open_time]
        assert not cur, "NEUTRAL prediction must NOT be logged"

    print("\nALL PIPELINE INTEGRITY CHECKS PASSED")
    print(f"emission share: {len(rows)}/60 candles = {len(rows)/60:.0%}")

asyncio.run(main())
