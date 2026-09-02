"""
SQLite store for closed-candle microstructure data + signal log.

HISTORY INTEGRITY CONTRACT (the user's explicit requirement — no result
overrides, ever):
  - open_signal()    inserts a PENDING row the moment a signal is emitted
                     (candle open) with the exact signal/strength/score.
  - finalize_signal() updates ONLY the outcome fields (result, actual,
                     a_open/a_close, tags, postmortem) on a PENDING row.
  - Signal fields written at emission are NEVER rewritten. INSERT OR
                     REPLACE on signal_log is gone — history cannot be
                     silently rewritten by a retry or a re-grade.
  - result values:   'correct' | 'wrong' | 'draw' (broker refund) |
                     'no_data' (candle closed with zero ticks — cannot be
                     graded; excluded from win-rate) | 'pending' (open).
"""
import os
import sqlite3
import threading
import time

# QX_DB_PATH lets a deployment point this at a persistent volume (e.g.
# Railway's ephemeral filesystem otherwise loses this file on every redeploy)
# — unset falls back to the local-dev default next to this file.
DB_PATH = os.environ.get("QX_DB_PATH") or os.path.join(
    os.path.dirname(__file__), "candle_micro.db")
_lock   = threading.Lock()

# SQLite robustness: WAL lets the per-candle writes on the event loop
# coexist with the threadpool stats readers without 'database is locked'
# surfacing as silent row loss, and busy_timeout makes a contested write
# wait instead of failing.
_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=8000;",
    "PRAGMA synchronous=NORMAL;",
)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=8.0)
    for pragma in _SQLITE_PRAGMAS:
        try:
            con.execute(pragma)
        except sqlite3.OperationalError:
            pass          # WAL unsupported on some filesystems — degrade fine
    return con

# If QX_DB_PATH points at a directory that doesn't exist yet (e.g. a Railway
# Volume mount path configured before the volume itself is attached),
# sqlite3.connect raises "unable to open database file" on every request
# instead of just creating the file. Create the parent dir up front so a
# misconfigured/missing volume degrades to a non-persistent local file
# instead of a 500 on every stats endpoint.
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS candle_micro (
    asset      TEXT    NOT NULL,
    period     INTEGER NOT NULL,
    ctime      INTEGER NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    buy_pct    INTEGER,
    pressure   TEXT,
    last_react TEXT,
    is_fight   INTEGER DEFAULT 0,
    phase_e    TEXT,
    phase_m    TEXT,
    phase_l    TEXT,
    hold_price REAL,
    tick_count INTEGER,
    gap_pct    REAL    DEFAULT 0,
    gap_type   TEXT    DEFAULT 'NONE',
    key_levels TEXT    DEFAULT NULL,
    ticks      TEXT    DEFAULT NULL,   -- JSON tick prices (downsampled) — lets
                                       -- backtest replay RUN/TRAP like live
    PRIMARY KEY (asset, period, ctime)
);
CREATE INDEX IF NOT EXISTS idx_asset_period_ctime
    ON candle_micro (asset, period, ctime DESC);

CREATE TABLE IF NOT EXISTS signal_log (
    asset       TEXT    NOT NULL,
    period      INTEGER NOT NULL,
    ctime       INTEGER NOT NULL,  -- candle the prediction was FOR
    signal      TEXT,              -- CALL / PUT
    score       INTEGER,
    confidence  REAL,
    codes       TEXT,              -- theory codes that fired, e.g. "RUN,T7"
    actual      TEXT,              -- UP / DOWN (actual candle colour)
    result      TEXT,              -- correct / wrong
    strength    TEXT,              -- STRONG/MEDIUM/WEAK AS OF SIGNAL TIME.
                                   -- Safe to filter and backtest on.
    strength_live TEXT,            -- strength after the running-candle tick
                                   -- confirmation (_apply_runconf) folded in.
                                   -- Contains LOOKAHEAD: it partly encodes the
                                   -- outcome. Display only -- never a filter.
    agree       INTEGER,           -- # distinct theories backing the signal
    right_codes TEXT,              -- theories that CALLED IT RIGHT this candle
    wrong_codes TEXT,              -- theories that were WRONG this candle
    reasons     TEXT,              -- full vote list (JSON) — the "why"
    a_open      REAL,              -- actual outcome candle open
    a_close     REAL,              -- actual outcome candle close
    regime      TEXT,              -- UPTREND / DOWNTREND / SIDEWAYS at signal time
    zone        TEXT,              -- SUPPORT / RESISTANCE / NEUTRAL at signal time
    tags        TEXT,              -- comma flags: NOISE_CANDLE,COUNTER_REGIME,LATE_FLIP,...
    postmortem  TEXT,              -- one-line human report: why it won / lost
    market      TEXT,              -- reaction_engine market: adds ZIGZAG/NOISE to regime
    reaction_type TEXT,            -- BOUNCE/REJECTION/SWEEP/BREAKOUT/ABSORPTION/EXHAUSTION/CONTINUATION/NONE
    reaction_quality INTEGER,      -- 0-100 reaction_engine quality score
    setup_id    TEXT,              -- e.g. OTC_SIDEWAYS_SUPPORT_BOUNCE — the trade-gate key
    trade_ok    INTEGER,           -- 1 if the trade gate approved at signal time
    trade_why   TEXT,              -- gate's stated reason (proven edge / why blocked)
    -- ── History-integrity columns (2026-09 confluence engine) ──
    emitted_at  REAL,              -- unix ts when the signal was broadcast (candle open)
    graded_at   REAL,              -- unix ts when the outcome was written
    agree_weight REAL,             -- summed weight of the agreeing voters
    voters      TEXT,              -- JSON [{name, dir, weight}] — the council
    payout      INTEGER,           -- broker payout % at emission (break-even context)
    PRIMARY KEY (asset, period, ctime)
);
CREATE INDEX IF NOT EXISTS idx_signal_ctime
    ON signal_log (asset, period, ctime DESC);
CREATE INDEX IF NOT EXISTS idx_signal_result
    ON signal_log (result, ctime DESC);

-- One row per individual theory vote — normalized version of right/wrong_codes.
-- Lets SQL answer "how is RUN doing on AUDCAD this week at x2+ weight" directly,
-- and feeds the live theory-performance gate in analyze_eoc.
CREATE TABLE IF NOT EXISTS theory_votes (
    asset   TEXT    NOT NULL,
    period  INTEGER NOT NULL,
    ctime   INTEGER NOT NULL,      -- candle the vote was FOR
    theory  TEXT    NOT NULL,      -- RUN / T7 / MICRO / ...
    vote    TEXT    NOT NULL,      -- CALL / PUT
    mag     INTEGER NOT NULL,      -- vote weight (x1, x2, ...)
    outcome TEXT    NOT NULL,      -- right / wrong
    PRIMARY KEY (asset, period, ctime, theory)
);
CREATE INDEX IF NOT EXISTS idx_votes_theory
    ON theory_votes (theory, ctime DESC);
"""

# ── Public API ────────────────────────────────────────────────────────────────

def init() -> None:
    """Create tables if they don't exist + migrate signal_log columns."""
    with _lock:
        con = _connect()
        try:
            con.executescript(_DDL)
            # Migrate older signal_log tables: add any missing report columns.
            have = {r[1] for r in con.execute("PRAGMA table_info(signal_log)")}
            # Migrate candle_micro: add new columns if missing
            micro_cols = {r[1] for r in con.execute("PRAGMA table_info(candle_micro)")}
            for col, decl in [("gap_pct",    "REAL DEFAULT 0"),
                               ("gap_type",   "TEXT DEFAULT 'NONE'"),
                               ("key_levels", "TEXT DEFAULT NULL"),
                               ("ticks",      "TEXT DEFAULT NULL")]:
                if col not in micro_cols:
                    con.execute(f"ALTER TABLE candle_micro ADD COLUMN {col} {decl}")

            for col, decl in [
                ("strength", "TEXT"), ("strength_live", "TEXT"),
                ("agree", "INTEGER"),
                ("right_codes", "TEXT"), ("wrong_codes", "TEXT"),
                ("reasons", "TEXT"), ("a_open", "REAL"), ("a_close", "REAL"),
                ("regime", "TEXT"), ("zone", "TEXT"),
                ("tags", "TEXT"), ("postmortem", "TEXT"),
                ("market", "TEXT"), ("reaction_type", "TEXT"),
                ("reaction_quality", "INTEGER"), ("setup_id", "TEXT"),
                ("trade_ok", "INTEGER"), ("trade_why", "TEXT"),
                ("emitted_at", "REAL"), ("graded_at", "REAL"),
                ("agree_weight", "REAL"), ("voters", "TEXT"),
                ("payout", "INTEGER"),
            ]:
                if col not in have:
                    con.execute(f"ALTER TABLE signal_log ADD COLUMN {col} {decl}")
            # setup_id is added by the ALTER above on pre-existing databases —
            # its index must be created AFTER that, never inside _DDL's initial
            # CREATE TABLE/INDEX block, or it 500s on any DB older than this
            # column ("no such column: setup_id").
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_setup
                ON signal_log (setup_id, ctime DESC)
            """)
            con.commit()
        finally:
            con.close()


def save(asset: str, period: int, candle: dict, micro: dict) -> None:
    """
    Persist a closed candle's micro summary including gap data and key levels.
    Called right before self._ticks.clear() so tick data is still available.
    gap_pct    : signed gap % (positive=gap-up, negative=gap-down), 0 if none
    gap_type   : PURE | REJECTED | FILLED | FLIP | NONE
    key_levels : list of [price, touches] — active S/R snapshot at close time
    """
    import json as _json
    phases = micro.get("phases") or []
    kl_raw  = micro.get("key_levels") or []
    kl_json = _json.dumps([[round(p, 8), t] for p, t in kl_raw]) if kl_raw else None
    row = (
        asset, period,
        candle["time"],
        candle.get("open"),  candle.get("high"),
        candle.get("low"),   candle.get("close"),
        micro.get("buy_pct"),
        micro.get("pressure"),
        micro.get("last_react"),
        1 if micro.get("is_fight") else 0,
        phases[0] if len(phases) > 0 else None,
        phases[1] if len(phases) > 1 else None,
        phases[2] if len(phases) > 2 else None,
        micro.get("hold_price"),
        micro.get("tick_count"),
        micro.get("gap_pct", 0.0),
        micro.get("gap_type", "NONE"),
        kl_json,
        micro.get("ticks_json"),
    )
    with _lock:
        con = _connect()
        try:
            con.execute("""
                INSERT OR REPLACE INTO candle_micro
                (asset, period, ctime, open, high, low, close,
                 buy_pct, pressure, last_react, is_fight,
                 phase_e, phase_m, phase_l, hold_price, tick_count,
                 gap_pct, gap_type, key_levels, ticks)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, row)
            con.commit()
        finally:
            con.close()


def get_micro_history(asset: str, period: int, n: int = 6,
                      before_ctime: int | None = None) -> list[dict]:
    """
    Return last N closed candles' micro summaries, oldest first.
    Used by analyze_eoc() to read previous-candle patterns.
    Includes gap_pct / gap_type for the GAP signal and key_levels JSON.

    before_ctime: ctime of the candle the caller is analysing. When given,
    only rows from the N candle-slots immediately before it are returned —
    without this, a restart or asset switch made "previous candle" data
    silently come from hours or days earlier (stale-history bug).
    """
    import json as _json
    where  = "asset=? AND period=?"
    params: list = [asset, period]
    if before_ctime is not None:
        where += " AND ctime < ? AND ctime >= ?"
        params += [before_ctime, before_ctime - n * period]
    with _lock:
        con = _connect()
        try:
            rows = con.execute(f"""
                SELECT ctime, open, high, low, close,
                       buy_pct, pressure, last_react, is_fight,
                       phase_e, phase_m, phase_l, hold_price, tick_count,
                       gap_pct, gap_type, key_levels
                FROM candle_micro
                WHERE {where}
                ORDER BY ctime DESC LIMIT ?
            """, (*params, n)).fetchall()
        finally:
            con.close()

    result = []
    for r in reversed(rows):   # oldest → newest
        kl_raw = r[16]
        try:
            key_levels = _json.loads(kl_raw) if kl_raw else []
        except Exception:
            key_levels = []
        result.append({
            "time":       r[0],
            "open":       r[1],  "high":  r[2],
            "low":        r[3],  "close": r[4],
            "buy_pct":    r[5],
            "pressure":   r[6],
            "last_react": r[7],
            "is_fight":   bool(r[8]),
            "phases":     [r[9], r[10], r[11]],
            "hold_price": r[12],
            "tick_count": r[13],
            "gap_pct":    r[14] or 0.0,
            "gap_type":   r[15] or "NONE",
            "key_levels": key_levels,
        })
    return result


def cleanup(keep_days: int = 7) -> None:
    """Delete rows older than keep_days to prevent unbounded growth."""
    cutoff = int(time.time()) - keep_days * 86400
    with _lock:
        con = _connect()
        try:
            con.execute("DELETE FROM candle_micro WHERE ctime < ?", (cutoff,))
            # Keep signal_log longer (30 days) so win-rate has history.
            con.execute("DELETE FROM signal_log WHERE ctime < ?",
                        (int(time.time()) - 30 * 86400,))
            # theory_votes mirrors signal_log rows — same 30-day retention
            con.execute("DELETE FROM theory_votes WHERE ctime < ?",
                        (int(time.time()) - 30 * 86400,))
            con.commit()
        finally:
            con.close()


# ── Signal logging / win-rate ──────────────────────────────────────────────────

# Signals are logged in TWO phases so the history is audit-proof:
#   phase 1  open_signal()      at candle open, result='pending'
#   phase 2  finalize_signal()  at candle close, outcome fields ONLY
# A pending row can be finalized exactly once (WHERE result='pending'), so
# a retry / re-grade / crash loop can never rewrite a settled outcome.

def open_signal(asset: str, period: int, ctime: int, signal: str,
                score: int, confidence: float, codes: str,
                strength: str, agree: int, agree_weight: float,
                reasons: str = "", regime: str | None = None,
                zone: str | None = None, voters_json: str = "",
                payout: int | None = None,
                emitted_at: float | None = None) -> bool:
    """Phase 1 — insert the emitted signal as PENDING. Returns True when a
    new row was created (False = already open: idempotent, never overwritten)."""
    with _lock:
        con = _connect()
        try:
            cur = con.execute("""
                INSERT OR IGNORE INTO signal_log
                (asset, period, ctime, signal, score, confidence, codes,
                 result, strength, strength_live, agree, agree_weight,
                 reasons, regime, zone, voters, payout, emitted_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (asset, period, ctime, signal, score, confidence, codes,
                  "pending", strength, strength, agree, agree_weight,
                  reasons, regime, zone, voters_json, payout,
                  emitted_at or time.time()))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def finalize_signal(asset: str, period: int, ctime: int, result: str,
                    actual: str, a_open, a_close,
                    right_codes: str = "", wrong_codes: str = "",
                    tags: str = "", postmortem: str = "",
                    graded_at: float | None = None) -> bool:
    """Phase 2 — write the outcome onto the PENDING row. Touches ONLY
    outcome fields; the signal/strength/score recorded at emission are
    untouchable. Returns True if a pending row was finalized."""
    with _lock:
        con = _connect()
        try:
            cur = con.execute("""
                UPDATE signal_log
                SET result=?, actual=?, a_open=?, a_close=?,
                    right_codes=?, wrong_codes=?, tags=?, postmortem=?,
                    graded_at=?
                WHERE asset=? AND period=? AND ctime=? AND result='pending'
            """, (result, actual, a_open, a_close, right_codes, wrong_codes,
                  tags, postmortem, graded_at or time.time(),
                  asset, period, ctime))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def log_signal(asset: str, period: int, ctime: int, signal: str,
               score: int, confidence: float, codes: str,
               actual: str, result: str,
               strength: str | None = None,
               strength_live: str | None = None,
               agree: int | None = None,
               right_codes: str = "", wrong_codes: str = "",
               reasons: str = "", a_open: float | None = None,
               a_close: float | None = None,
               regime: str | None = None, zone: str | None = None,
               tags: str = "", postmortem: str = "",
               votes: list | None = None,
               market: str | None = None, reaction_type: str | None = None,
               reaction_quality: int | None = None,
               setup_id: str | None = None,
               trade_ok: int | None = None,
               trade_why: str | None = None,
               agree_weight: float | None = None,
               voters_json: str = "", payout: int | None = None,
               late_logged: bool = False) -> None:
    """One-shot full-row write (used when no pending row exists — e.g. a
    late-catch or a grading path that skipped phase 1).

    NO-REWRITE GUARANTEE: INSERT OR IGNORE — if a row already exists for
    (asset, period, ctime) this is a no-op, so a retry can never replace
    a settled outcome. `late_logged` tags the row LATE_LOGGED so the audit
    trail shows it was not written through the two-phase path."""
    if late_logged and "LATE_LOGGED" not in tags:
        tags = ",".join(x for x in (tags, "LATE_LOGGED") if x)
    with _lock:
        con = _connect()
        try:
            con.execute("""
                INSERT OR IGNORE INTO signal_log
                (asset, period, ctime, signal, score, confidence,
                 codes, actual, result, strength, strength_live, agree,
                 agree_weight, right_codes, wrong_codes, reasons, a_open,
                 a_close, regime, zone, tags, postmortem,
                 market, reaction_type, reaction_quality, setup_id,
                 trade_ok, trade_why, voters, payout,
                 emitted_at, graded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (asset, period, ctime, signal, score, confidence,
                  codes, actual, result, strength, strength_live, agree,
                  agree_weight, right_codes, wrong_codes, reasons, a_open,
                  a_close, regime, zone, tags, postmortem,
                  market, reaction_type, reaction_quality, setup_id,
                  trade_ok, trade_why, voters_json, payout,
                  time.time(), time.time()))
            if votes:
                con.executemany("""
                    INSERT OR IGNORE INTO theory_votes
                    (asset, period, ctime, theory, vote, mag, outcome)
                    VALUES (?,?,?,?,?,?,?)
                """, [(asset, period, ctime, t, v, m, o)
                      for t, v, m, o in votes])
            con.commit()
        finally:
            con.close()


def log_theory_votes(asset: str, period: int, ctime: int,
                     votes: list[tuple[str, str, int, str]]) -> None:
    """
    Shadow-grade a prediction's per-theory votes WITHOUT a signal_log row.
    """
    if not votes:
        return
    with _lock:
        con = _connect()
        try:
            con.executemany("""
                INSERT OR REPLACE INTO theory_votes
                (asset, period, ctime, theory, vote, mag, outcome)
                VALUES (?,?,?,?,?,?,?)
            """, [(asset, period, ctime, t, v, m, o)
                  for t, v, m, o in votes])
            con.commit()
        finally:
            con.close()


def theory_perf(asset: str | None = None, period: int | None = None,
                days: int = 7, min_n: int = 40) -> dict:
    """
    Recent per-theory accuracy — the feedback loop consumed by analyze_eoc's
    theory mute gate (via feed.py's cached snapshot) and /api/theory-perf.
    """
    cutoff = int(time.time()) - days * 86400
    where, params = ["ctime >= ?", "outcome IN ('right','wrong')"], [cutoff]
    if asset:
        where.append("asset=?");  params.append(asset)
    if period:
        where.append("period=?"); params.append(period)
    wsql = " WHERE " + " AND ".join(where)

    with _lock:
        con = _connect()
        try:
            rows = con.execute(f"""
                SELECT theory,
                       SUM(outcome = 'right') AS r,
                       SUM(outcome = 'wrong') AS w
                FROM theory_votes{wsql}
                GROUP BY theory
            """, params).fetchall()
        finally:
            con.close()

    return {
        code: {"n": r + w, "rate": round(r / (r + w) * 100, 1)}
        for code, r, w in rows if (r + w) >= min_n
    }


def get_signals(asset: str | None = None, period: int | None = None,
                limit: int = 50, direction: str | None = None,
                result: str | None = None) -> list[dict]:
    """Most recent resolved signals with their full postmortem, newest first.

    direction: filter by the signal itself — "CALL" or "PUT" (the History /
    Analytics tabs use this to show per-direction performance).
    result:    filter by outcome — "correct", "wrong", "draw", "no_data"
               or "pending".
    """
    where, params = [], []
    if asset:
        where.append("asset=?");  params.append(asset)
    if period:
        where.append("period=?"); params.append(period)
    if direction in ("CALL", "PUT"):
        where.append("signal=?"); params.append(direction)
    if result in ("correct", "wrong", "draw", "no_data", "pending"):
        where.append("result=?"); params.append(result)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        con = _connect()
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(f"""
                SELECT asset, period, ctime, signal, score, confidence,
                       strength, agree, codes, actual, result,
                       right_codes, wrong_codes, a_open, a_close,
                       regime, zone, tags, postmortem,
                       market, reaction_type, reaction_quality, setup_id
                FROM signal_log{wsql}
                ORDER BY ctime DESC LIMIT ?
            """, (*params, min(limit, 500))).fetchall()
        finally:
            con.close()
    return [dict(r) for r in rows]


def get_stats(asset: str | None = None, period: int | None = None,
              days: int | None = None) -> dict:
    """Overall + per-theory win-rate from signal_log — THE canonical stats
    source. Every win-rate surface in the app reads through this or the
    same bucket helper so numbers can never disagree between tabs.

    days:    trailing window in days (None = all history).
    period:  candle period filter (None = all periods).
    rate = correct / (correct + wrong). Draws are broker refunds and
    no_data candles are ungradeable — both are reported but excluded
    from the rate, exactly like the broker's P&L.
    """
    where, params = [], []
    if asset:
        where.append("asset=?");  params.append(asset)
    if period:
        where.append("period=?"); params.append(period)
    if days:
        where.append("ctime >= ?")
        params.append(int(time.time()) - days * 86400)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""

    with _lock:
        con = _connect()
        try:
            (total, correct, wrong_n, draws, no_data,
             pending) = con.execute(
                f"SELECT COUNT(*), "
                f"COALESCE(SUM(result='correct'),0), "
                f"COALESCE(SUM(result='wrong'),0), "
                f"COALESCE(SUM(result='draw'),0), "
                f"COALESCE(SUM(result='no_data'),0), "
                f"COALESCE(SUM(result='pending'),0) "
                f"FROM signal_log{wsql}", params).fetchone()
            rows = con.execute(
                f"SELECT codes, result FROM signal_log{wsql}", params).fetchall()
            strength_rows = con.execute(
                f"SELECT strength, COALESCE(SUM(result='correct'),0), "
                f"COALESCE(SUM(result IN ('correct','wrong')),0) "
                f"FROM signal_log{wsql}"
                f"{' AND' if wsql else ' WHERE'} strength IS NOT NULL "
                f"AND result IN ('correct','wrong') "
                f"AND strength_live IS NOT NULL "
                f"GROUP BY strength", params).fetchall()
        finally:
            con.close()

    lo, hi = _wilson(correct or 0, (correct or 0) + (wrong_n or 0))
    by_strength = {}
    for s, w, n in strength_rows:
        if not n:
            continue
        s_lo, s_hi = _wilson(w, n)
        by_strength[s] = {"n": n, "correct": w,
                          "rate": round(w / n * 100, 1) if n else 0.0,
                          "ci95": [s_lo, s_hi]}

    theory: dict[str, list[int]] = {}
    for codes, result in rows:
        if result not in ("correct", "wrong"):
            continue
        for code in (codes or "").split(","):
            if not code:
                continue
            t = theory.setdefault(code, [0, 0])
            t[0] += 1
            if result == "correct":
                t[1] += 1

    per_theory = {
        k: {"n": v[0], "win": v[1],
            "rate": round(v[1] / v[0] * 100, 1) if v[0] else 0.0}
        for k, v in theory.items()
    }
    decided = (correct or 0) + (wrong_n or 0)
    return {
        "days":       days,
        "total":      (total or 0),
        "decided":    decided,
        "correct":    correct or 0,
        "wrong":      wrong_n or 0,
        "draws":      draws or 0,
        "no_data":    no_data or 0,
        "pending":    pending or 0,
        "rate":       round((correct or 0) / decided * 100, 1) if decided else 0.0,
        "ci95":       [lo, hi] if decided else None,
        "by_strength": by_strength,
        "per_theory": dict(sorted(per_theory.items(),
                                  key=lambda x: -x[1]["n"])),
    }


def theory_report(asset: str | None = None, period: int | None = None) -> dict:
    """
    TRUE per-theory accuracy from the WHY report: for each theory, how often its
    OWN vote matched the actual candle (right vs wrong), independent of the final
    blended signal. This is the number to trust when deciding what to keep/cut.
    """
    where, params = [], []
    if asset:
        where.append("asset=?");  params.append(asset)
    if period:
        where.append("period=?"); params.append(period)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""

    with _lock:
        con = _connect()
        try:
            rows = con.execute(
                f"SELECT right_codes, wrong_codes FROM signal_log{wsql}",
                params).fetchall()
        finally:
            con.close()

    rep: dict[str, list[int]] = {}
    for right, wrong in rows:
        for code in (right or "").split(","):
            if code:
                rep.setdefault(code, [0, 0])[0] += 1   # right
        for code in (wrong or "").split(","):
            if code:
                rep.setdefault(code, [0, 0])[1] += 1   # wrong
    out = {}
    for code, (r, w) in rep.items():
        n = r + w
        out[code] = {"right": r, "wrong": w, "n": n,
                     "rate": round(r / n * 100, 1) if n else 0.0}
    return dict(sorted(out.items(), key=lambda x: -x[1]["n"]))


def diagnosis(days: int = 7, asset: str | None = None) -> dict:
    """Answer 'why are the predictions wrong' directly from signal_log.

    Slices resolved signals along every axis the signal itself already
    records, so the answer is measured rather than argued:

      by_strength  — does STRONG actually beat WEAK? This is the decisive
                     one. If STRONG is not clearly above break-even while
                     WEAK sits at ~50%, the confidence calibration carries
                     no information and filtering by it cannot help.
      by_tag       — NOISE_CANDLE / COUNTER_REGIME / WITH_REGIME etc. The
                     code already flags noise candles as coin flips; this
                     shows whether that flag is right.
      by_regime    — trend-following logic should do better in UPTREND /
                     DOWNTREND than in SIDEWAYS. If it doesn't, the regime
                     detector is not working.
      by_asset     — separates 'the strategy is weak' from 'two exotic OTC
                     pairs are dragging the average down'.
      by_hour      — session quality effects.

    `break_even` is included on every bucket because raw accuracy is
    misleading for binary options: at an 85% payout, 50% accuracy is not
    break-even, it is a steady loss.
    """
    cutoff = int(time.time()) - days * 86400
    where  = ["ctime >= ?", "result IN ('correct','wrong')"]
    params: list = [cutoff]
    if asset:
        where.append("asset=?"); params.append(asset)
    wsql = " WHERE " + " AND ".join(where)

    def _rate(rows):
        out = []
        for key, n, r in rows:
            if not n:
                continue
            out.append({
                "key": key if key is not None else "(none)",
                "n": n,
                "correct": r,
                "accuracy": round(100.0 * r / n, 2),
            })
        return sorted(out, key=lambda d: -d["n"])

    with _lock:
        con = _connect()
        try:
            return _diagnosis_query(con, wsql, params, cutoff, days, asset)
        finally:
            con.close()


def _wilson(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion, in percent.

    A bare accuracy number is not decidable against the payout break-even:
    54.07% on n=3037 and 54.07% on n=30 mean completely different things.
    Wilson is used rather than the normal approximation because it stays
    correct for small n and for rates near 0 or 100 -- exactly the buckets
    (STRONG, NOISE_CANDLE) where a naive interval would mislead most.
    """
    if n <= 0:
        return (0.0, 100.0)
    p = correct / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (round(max(0.0, centre - half) * 100, 2),
            round(min(1.0, centre + half) * 100, 2))


# Tags derived from the OUTCOME candle. They describe what happened, not what
# was knowable when the signal fired, so they can never be used as an entry
# filter -- MAJORITY_WRONG is the extreme case: it is tagged from the
# right/wrong vote split, so "it is wrong when the majority was wrong" is a
# tautology, not an edge.
_POST_HOC_TAGS = {"NOISE_CANDLE", "BIG_MOVE", "LATE_FLIP", "MAJORITY_WRONG"}


def _verdict(correct: int, n: int) -> str:
    """Honest three-way call against the payout break-even.

    The old version returned "PROFITABLE" whenever the point estimate cleared
    54.05%. On the first live sample that fired at 54.07% -- a 0.02pp margin
    on a +/-1.8pp interval -- which is indistinguishable from break-even and
    exactly the kind of number that talks someone into risking real money.
    A claim of profit now requires the entire interval to clear the bar.
    """
    lo, hi = _wilson(correct, n)
    if lo > 54.05:
        return f"PROFITABLE — 95% CI [{lo}%, {hi}%] is entirely above break-even"
    if hi < 54.05:
        return f"LOSING — 95% CI [{lo}%, {hi}%] is entirely below break-even"
    return (f"UNPROVEN — 95% CI [{lo}%, {hi}%] straddles the {54.05}% "
            f"break-even; no evidence of an edge either way. Need more data.")


def _diagnosis_query(con, wsql, params, cutoff, days, asset) -> dict:
    def _rate(rows, post_hoc: bool = False):
        out = []
        for key, n, r in rows:
            if not n:
                continue
            r = r or 0
            lo, hi = _wilson(r, n)
            row = {
                "key": key if key is not None else "(none)",
                "n": n,
                "correct": r,
                "accuracy": round(100.0 * r / n, 2),
                "ci95": [lo, hi],
                # The only question that matters for a binary payout: is the
                # WHOLE interval above break-even? If not, this bucket has not
                # been shown to make money, however good the point estimate.
                # Whole-interval test. Deliberately overwritten to False for
                # post-hoc buckets below: NOISE_CANDLE printed 68% with a
                # "beats break-even" star while being computed from the
                # outcome candle, which is the most dangerous thing this
                # report could show -- a green light on an untradeable slice.
                "beats_breakeven": lo > 54.05,
            }
            if post_hoc or (isinstance(key, str) and key in _POST_HOC_TAGS):
                row["post_hoc"] = True
                row["beats_breakeven"] = False   # never tradeable, whatever the rate
                row["warning"] = ("derived from the outcome candle -- "
                                  "NOT usable as an entry filter")
            out.append(row)
        return sorted(out, key=lambda d: -d["n"])

    if True:
        def q(expr):
            return con.execute(
                f"SELECT {expr} AS k, COUNT(*), SUM(result='correct') "
                f"FROM signal_log{wsql} GROUP BY k", params).fetchall()

        total_n, total_r = con.execute(
            f"SELECT COUNT(*), SUM(result='correct') "
            f"FROM signal_log{wsql}", params).fetchone()
        draws = con.execute(
            "SELECT COUNT(*) FROM signal_log"
            f"{wsql} AND result='draw'", params).fetchone()[0]

        # by_strength reads the FROZEN signal-time column. by_strength_live
        # reads the runconf-mutated one and is reported separately, flagged,
        # so its ~100%/0% split is never mistaken for a tradeable filter.
        # Rows written before the lookahead fix have a `strength` that was
        # mutated by the outcome candle's own ticks. Averaging them in made
        # MEDIUM read 79.98% while the clean rows sat at 53% -- the exact
        # mirage this report exists to prevent. strength_live IS NULL marks
        # them, so exclude them here rather than reporting a blended number.
        _clean = (f"{wsql} AND strength_live IS NOT NULL" if wsql
                  else " WHERE strength_live IS NOT NULL")
        by_strength = _rate(con.execute(
            f"SELECT strength, COUNT(*), COALESCE(SUM(result='correct'),0) "
            f"FROM signal_log{_clean} AND strength IS NOT NULL "
            f"GROUP BY strength", params).fetchall())
        by_strength_live = _rate(q("strength_live"), post_hoc=True)
        by_regime   = _rate(q("regime"))
        by_zone     = _rate(q("zone"))
        by_asset    = _rate(q("asset"))
        by_signal   = _rate(q("signal"))
        by_hour     = _rate(q("CAST(strftime('%H', ctime, 'unixepoch') AS INTEGER)"))

        # Tags are a comma-joined list, so they need one LIKE pass each.
        tag_rows = []
        for tag in ("NOISE_CANDLE", "BIG_MOVE", "WITH_REGIME", "COUNTER_REGIME",
                    "LATE_FLIP", "MAJORITY_WRONG"):
            n, r = con.execute(
                f"SELECT COUNT(*), SUM(result='correct') FROM signal_log"
                f"{wsql} AND tags LIKE ?", params + [f"%{tag}%"]).fetchone()
            if n:
                tag_rows.append((tag, n, r or 0))
        by_tag = _rate(tag_rows)

        # Effective sample size. The 15 OTC pairs are broker-synthesised and
        # move together, so N rows is NOT N independent observations: 3037
        # rows over 15 pairs and ~200 minutes is closer to 200 independent
        # draws. Reporting distinct candle timestamps alongside the row count
        # keeps that honest -- every interval below is computed on rows and is
        # therefore OPTIMISTIC by roughly sqrt(rows/timestamps).
        distinct_ct, distinct_assets = con.execute(
            f"SELECT COUNT(DISTINCT ctime), COUNT(DISTINCT asset) "
            f"FROM signal_log{wsql}", params).fetchone()

        # Rows written before the lookahead split have strength_live IS NULL
        # and a `strength` that was mutated by the running-candle ticks. They
        # cannot be repaired -- the clean value was never stored -- so they
        # must not be silently averaged in with post-fix rows. NULL is the
        # marker, no extra column needed.
        pre_fix = con.execute(
            f"SELECT COUNT(*) FROM signal_log{wsql} AND strength_live IS NULL"
            if wsql else
            "SELECT COUNT(*) FROM signal_log WHERE strength_live IS NULL",
            params).fetchone()[0]

    overall = round(100.0 * (total_r or 0) / total_n, 2) if total_n else None
    return {
        "days": days,
        "asset": asset,
        "graded": total_n,
        "draws": draws,
        "overall_accuracy": overall,
        # 85% is Quotex's typical payout; 100/(100+85) = 54.05%.
        "break_even_at_85pct_payout": 54.05,
        "overall_ci95": list(_wilson(total_r or 0, total_n)) if total_n else None,
        "verdict": _verdict(total_r or 0, total_n) if total_n else None,
        "effective_sample": {
            "rows": total_n,
            "distinct_timestamps": distinct_ct,
            "distinct_assets": distinct_assets,
            "pre_lookahead_fix_rows": pre_fix,
            "note": ("Correlated pairs mean the independent sample size is "
                     "nearer distinct_timestamps than rows; treat every "
                     "interval here as optimistic."),
            "warning": (
                f"{pre_fix} of {total_n} rows predate the strength lookahead "
                f"fix; their `strength` column is outcome-contaminated and "
                f"by_strength is unusable until they age out of the window."
                if pre_fix else None),
        },
        "by_strength": by_strength,
        "by_strength_live": by_strength_live,
        "by_tag": by_tag,
        "by_regime": by_regime,
        "by_zone": by_zone,
        "by_signal": by_signal,
        "by_asset": by_asset,
        "by_hour": by_hour,
    }


def _wr_bucket(correct: int, wrong: int, draws: int, min_n: int,
               break_even: float) -> dict:
    """THE single win-rate bucket builder — used by every per-pair /
    per-direction / per-window surface so no two tabs can ever disagree.

    rate   = correct / (correct + wrong); draws are broker refunds,
    excluded from n but reported. Verdict status comes from the WHOLE
    95% Wilson interval against the payout break-even (never the point
    estimate — a 54.07% point estimate on a ±1.8pp interval is noise):
      proven_win  — the whole interval clears break-even
      proven_loss — the whole interval sits below it
      unproven    — the interval straddles it (the normal state)
      thin        — under min_n graded signals; no claim either way
    """
    n = correct + wrong
    lo, hi = _wilson(correct, n)
    if n == 0:
        return {"n": 0, "correct": 0, "wrong": 0, "draws": draws,
                "rate": None, "ci95": None, "status": "none"}
    if n < min_n:
        status = "thin"
    elif lo > break_even:
        status = "proven_win"
    elif hi < break_even:
        status = "proven_loss"
    else:
        status = "unproven"
    return {"n": n, "correct": correct, "wrong": wrong, "draws": draws,
            "rate": round(100.0 * correct / n, 1), "ci95": [lo, hi],
            "status": status}


def _payout_break_even(payout: int | None) -> float:
    """Break-even accuracy for a binary payout: 100/(100+payout).
    payout=85 → 54.05%. Falls back to 85% (Quotex's typical) when unknown."""
    p = payout if payout and payout > 0 else 85
    return round(100.0 * 100.0 / (100.0 + p), 2)


def direction_winrate(days: int = 7, period: int = 60,
                      payout: int | None = None) -> dict:
    """Per-pair CALL vs PUT win rates — the user's explicit requirement:

    "Call ও put কোনো সিগন্যাল গুলো কেমন win রেট দিচ্ছে। সেই গুলো আলাদা
    আলাদা করে নিজের মতো করে দেখতে পারবো।"

    One row per pair with the CALL bucket, the PUT bucket and the combined
    bucket, each with n / correct / wrong / rate / 95% Wilson interval and
    an honest status verdict. `direction_bias` summarises which side has
    been stronger (minimum-sample guarded).
    """
    MIN_N = 20           # unified across ALL surfaces (was 20 vs 100 vs 10)
    BREAK_EVEN = _payout_break_even(payout)
    cutoff = int(time.time()) - days * 86400

    with _lock:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT asset, signal, "
                "       COALESCE(SUM(result='correct'),0), "
                "       COALESCE(SUM(result='wrong'),0), "
                "       COALESCE(SUM(result='draw'),0), "
                "       COALESCE(SUM(result='no_data'),0), "
                "       COALESCE(SUM(result='pending'),0) "
                "FROM signal_log "
                "WHERE ctime >= ? AND period = ? "
                "      AND signal IN ('CALL','PUT') "
                "GROUP BY asset, signal", (cutoff, period)).fetchall()
        finally:
            con.close()

    by_pair: dict[str, dict[str, list]] = {}
    for asset, signal, correct, wrong, draws, no_data, pending in rows:
        slot = by_pair.setdefault(asset, {
            "CALL": [0, 0, 0], "PUT": [0, 0, 0],
            "no_data": 0, "pending": 0})
        if signal not in ("CALL", "PUT"):
            continue
        slot[signal][0] += correct
        slot[signal][1] += wrong
        slot[signal][2] += draws
        slot["no_data"] += no_data
        slot["pending"] += pending

    display_names = {}
    try:
        from strategies import get_profile
        for asset in by_pair:
            try:
                display_names[asset] = get_profile(asset).display
            except Exception:
                display_names[asset] = asset
    except Exception:
        pass

    pairs = []
    for asset, slot in by_pair.items():
        call = _wr_bucket(*slot["CALL"], MIN_N, BREAK_EVEN)
        put = _wr_bucket(*slot["PUT"], MIN_N, BREAK_EVEN)
        allt = _wr_bucket(slot["CALL"][0] + slot["PUT"][0],
                          slot["CALL"][1] + slot["PUT"][1],
                          slot["CALL"][2] + slot["PUT"][2],
                          MIN_N, BREAK_EVEN)
        # direction bias — needs a meaningful sample on BOTH sides before
        # claiming one side is better, otherwise it is coin-flip noise.
        bias = "none"
        if call["n"] >= MIN_N and put["n"] >= MIN_N:
            if call["rate"] - put["rate"] >= 3.0:
                bias = "call"
            elif put["rate"] - call["rate"] >= 3.0:
                bias = "put"
        pairs.append({
            "asset": asset,
            "display": display_names.get(asset, asset),
            "call": call, "put": put, "all": allt,
            "direction_bias": bias,
            "no_data": slot["no_data"], "pending": slot["pending"],
        })

    pairs.sort(key=lambda r: -(r["all"]["rate"] if r["all"]["n"] else 0))

    # Overall buckets across all pairs
    tc = sum(p["call"]["correct"] for p in pairs)
    tw = sum(p["call"]["wrong"] for p in pairs)
    td = sum(p["call"]["draws"] for p in pairs)
    pc = sum(p["put"]["correct"] for p in pairs)
    pw = sum(p["put"]["wrong"] for p in pairs)
    pd = sum(p["put"]["draws"] for p in pairs)
    return {
        "days": days,
        "period": period,
        "break_even": BREAK_EVEN,
        "min_n": MIN_N,
        "overall": {
            "call": _wr_bucket(tc, tw, td, MIN_N, BREAK_EVEN),
            "put": _wr_bucket(pc, pw, pd, MIN_N, BREAK_EVEN),
            "all": _wr_bucket(tc + pc, tw + pw, td + pd, MIN_N, BREAK_EVEN),
        },
        "pairs": pairs,
    }


def pair_winrate(days: int = 7, period: int = 60,
                 payout: int | None = None) -> dict:
    """Per-pair win rate for the frontend sidebar.

    Thin, consistent core: same buckets, same MIN_N, same break-even as
    direction_winrate — deliberately the SAME verdicts on both surfaces.
    `status` is the only field the UI should colour on.
    """
    MIN_N = 20
    BREAK_EVEN = _payout_break_even(payout)
    cutoff = int(time.time()) - days * 86400

    with _lock:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT asset, "
                "       COALESCE(SUM(result='correct'),0), "
                "       COALESCE(SUM(result='wrong'),0), "
                "       COALESCE(SUM(result='draw'),0), "
                "       COALESCE(SUM(result='no_data'),0), "
                "       COALESCE(SUM(result='pending'),0) "
                "FROM signal_log "
                "WHERE ctime >= ? AND period = ? "
                "GROUP BY asset", (cutoff, period)).fetchall()
        finally:
            con.close()

    out = []
    total_no_data = 0
    total_pending = 0
    for asset, correct, wrong, draws, no_data, pending in rows:
        total_no_data += no_data
        total_pending += pending
        b = _wr_bucket(correct, wrong, draws, MIN_N, BREAK_EVEN)
        if not b["n"]:
            continue
        b["asset"] = asset
        b["no_data"] = no_data
        b["pending"] = pending
        out.append(b)

    out.sort(key=lambda r: -(r["rate"] if r["rate"] is not None else -1))
    total_n = sum(r["n"] for r in out)
    total_c = sum(r["correct"] for r in out)
    return {
        "days": days,
        "period": period,
        "break_even": BREAK_EVEN,
        "min_n": MIN_N,
        "pairs": out,
        "no_data": total_no_data,
        "pending": total_pending,
        "overall": {
            "n": total_n,
            "accuracy": round(100.0 * total_c / total_n, 1) if total_n else None,
            "ci95": list(_wilson(total_c, total_n)) if total_n else None,
            "status": _wr_bucket(total_c, total_n - total_c, 0,
                                 MIN_N, BREAK_EVEN)["status"] if total_n else "none",
        },
    }
