"""
SQLite store for closed-candle microstructure data.

Each row = one closed candle's summary:
  OHLC + buy/sell pressure + last-tick reaction + phase pattern + hold zone

This lets EOC analysis see what the *previous* candle's internal pressure
looked like — information that would otherwise be discarded when ticks.clear().
"""
import os
import sqlite3
import threading

# QX_DB_PATH lets a deployment point this at a persistent volume (e.g.
# Railway's ephemeral filesystem otherwise loses this file on every redeploy)
# — unset falls back to the local-dev default next to this file.
DB_PATH = os.environ.get("QX_DB_PATH") or os.path.join(
    os.path.dirname(__file__), "candle_micro.db")
_lock   = threading.Lock()

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
    strength    TEXT,              -- STRONG / MEDIUM / WEAK
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
    PRIMARY KEY (asset, period, ctime)
);
CREATE INDEX IF NOT EXISTS idx_signal_ctime
    ON signal_log (asset, period, ctime DESC);

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

-- Periodic snapshots of the STILL-FORMING candle (many rows per candle —
-- one per throttled tick-batch, not a single closed-candle summary like
-- candle_micro). Captures how price behaved against EXISTING key levels
-- (built from already-closed candle history) while the candle was still
-- open, so that reaction data isn't discarded when the candle finally
-- closes and only its final OHLC survives.
CREATE TABLE IF NOT EXISTS candle_running (
    asset        TEXT    NOT NULL,
    period       INTEGER NOT NULL,
    ctime        INTEGER NOT NULL,   -- open time of the still-forming candle
    snap_time    INTEGER NOT NULL,   -- wall-clock time of this snapshot
    open         REAL,
    high_so_far  REAL,
    low_so_far   REAL,
    last_price   REAL,
    tick_count   INTEGER,
    zone              TEXT,          -- SUPPORT/RESISTANCE/MID/WICK_WALL/GAP_ZONE/RANGE_EDGE
    zone_side         TEXT,
    zone_price        REAL,
    zone_touches      INTEGER,
    reaction_type     TEXT,          -- BOUNCE/REJECTION/SWEEP/BREAKOUT/ABSORPTION/EXHAUSTION/CONTINUATION/NONE
    reaction_quality  INTEGER,
    reaction_direction INTEGER,      -- +1/-1/0
    PRIMARY KEY (asset, period, ctime, snap_time)
);
CREATE INDEX IF NOT EXISTS idx_running_asset_ctime
    ON candle_running (asset, period, ctime DESC, snap_time DESC);
"""

# ── Public API ────────────────────────────────────────────────────────────────

def init() -> None:
    """Create tables if they don't exist + migrate signal_log columns."""
    with _lock:
        con = sqlite3.connect(DB_PATH)
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
                ("strength", "TEXT"), ("agree", "INTEGER"),
                ("right_codes", "TEXT"), ("wrong_codes", "TEXT"),
                ("reasons", "TEXT"), ("a_open", "REAL"), ("a_close", "REAL"),
                ("regime", "TEXT"), ("zone", "TEXT"),
                ("tags", "TEXT"), ("postmortem", "TEXT"),
                ("market", "TEXT"), ("reaction_type", "TEXT"),
                ("reaction_quality", "INTEGER"), ("setup_id", "TEXT"),
                ("trade_ok", "INTEGER"), ("trade_why", "TEXT"),
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
        con = sqlite3.connect(DB_PATH)
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
        con = sqlite3.connect(DB_PATH)
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
    import time
    cutoff = int(time.time()) - keep_days * 86400
    with _lock:
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("DELETE FROM candle_micro WHERE ctime < ?", (cutoff,))
            # candle_running gets many rows PER candle (throttled snapshots
            # through its whole life) — prune much more aggressively (1 day)
            # or it grows far faster than every other table.
            con.execute("DELETE FROM candle_running WHERE ctime < ?",
                        (int(time.time()) - 1 * 86400,))
            # Keep signal_log longer (30 days) so win-rate has history.
            con.execute("DELETE FROM signal_log WHERE ctime < ?",
                        (int(time.time()) - 30 * 86400,))
            # theory_votes mirrors signal_log rows — same 30-day retention
            # (was never pruned before: unbounded growth).
            con.execute("DELETE FROM theory_votes WHERE ctime < ?",
                        (int(time.time()) - 30 * 86400,))
            con.commit()
        finally:
            con.close()


# ── Signal logging / win-rate ──────────────────────────────────────────────────

def log_signal(asset: str, period: int, ctime: int, signal: str,
               score: int, confidence: float, codes: str,
               actual: str, result: str,
               strength: str | None = None, agree: int | None = None,
               right_codes: str = "", wrong_codes: str = "",
               reasons: str = "", a_open: float | None = None,
               a_close: float | None = None,
               regime: str | None = None, zone: str | None = None,
               tags: str = "", postmortem: str = "",
               votes: list[tuple[str, str, int, str]] | None = None,
               market: str | None = None, reaction_type: str | None = None,
               reaction_quality: int | None = None,
               setup_id: str | None = None,
               trade_ok: int | None = None,
               trade_why: str | None = None) -> None:
    """
    Record one resolved prediction with a full WHY report:
      codes        — theories that fired
      right_codes  — theories that called THIS candle correctly
      wrong_codes  — theories that were wrong THIS candle
      reasons      — the exact votes (JSON) so the decision can be replayed
      a_open/a_close — the actual outcome candle (to see the move size)
      regime/zone  — analyze_eoc's market context the prediction was made in
      tags         — comma flags explaining the outcome (NOISE_CANDLE, ...)
      postmortem   — one human-readable line: why this trade won or lost
      votes        — [(theory, CALL/PUT, mag, right/wrong), ...] → theory_votes
      market/reaction_type/reaction_quality/setup_id — reaction_engine's
        richer context+reaction read, and the trade-gate key (setup_id)
    """
    with _lock:
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("""
                INSERT OR REPLACE INTO signal_log
                (asset, period, ctime, signal, score, confidence,
                 codes, actual, result, strength, agree,
                 right_codes, wrong_codes, reasons, a_open, a_close,
                 regime, zone, tags, postmortem,
                 market, reaction_type, reaction_quality, setup_id,
                 trade_ok, trade_why)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (asset, period, ctime, signal, score, confidence,
                  codes, actual, result, strength, agree,
                  right_codes, wrong_codes, reasons, a_open, a_close,
                  regime, zone, tags, postmortem,
                  market, reaction_type, reaction_quality, setup_id,
                  trade_ok, trade_why))
            if votes:
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
    Recent per-theory accuracy — the feedback loop consumed by analyze_eoc.

    Reads right_codes / wrong_codes over the last `days` days (works for all
    historical rows; theory_votes refines this over time). Only theories with
    at least `min_n` resolved votes are returned, so tiny samples can never
    flip a theory's live weighting.
    Returns {theory: {"n": int, "rate": float}}.
    """
    import time as _time
    cutoff = int(_time.time()) - days * 86400
    where, params = ["ctime >= ?"], [cutoff]
    if asset:
        where.append("asset=?");  params.append(asset)
    if period:
        where.append("period=?"); params.append(period)
    wsql = " WHERE " + " AND ".join(where)

    with _lock:
        con = sqlite3.connect(DB_PATH)
        try:
            rows = con.execute(
                f"SELECT right_codes, wrong_codes FROM signal_log{wsql}",
                params).fetchall()
        finally:
            con.close()

    acc: dict[str, list[int]] = {}
    for right, wrong in rows:
        for code in (right or "").split(","):
            if code:
                acc.setdefault(code, [0, 0])[0] += 1
        for code in (wrong or "").split(","):
            if code:
                acc.setdefault(code, [0, 0])[1] += 1
    return {
        code: {"n": r + w, "rate": round(r / (r + w) * 100, 1)}
        for code, (r, w) in acc.items() if (r + w) >= min_n
    }


def get_signals(asset: str | None = None, period: int | None = None,
                limit: int = 50) -> list[dict]:
    """Most recent resolved signals with their full postmortem, newest first."""
    where, params = [], []
    if asset:
        where.append("asset=?");  params.append(asset)
    if period:
        where.append("period=?"); params.append(period)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        con = sqlite3.connect(DB_PATH)
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


def get_stats(asset: str | None = None, period: int | None = None) -> dict:
    """
    Overall + per-theory win-rate from signal_log.
    Filters by asset/period when given. Per-theory rate = how often the overall
    prediction was correct when that theory fired (a proxy for theory value).
    """
    where, params = [], []
    if asset:
        where.append("asset=?");  params.append(asset)
    if period:
        where.append("period=?"); params.append(period)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""

    with _lock:
        con = sqlite3.connect(DB_PATH)
        try:
            total, correct, wrong_n, draws = con.execute(
                f"SELECT COUNT(*), COALESCE(SUM(result='correct'),0), "
                f"COALESCE(SUM(result='wrong'),0), "
                f"COALESCE(SUM(result='draw'),0) "
                f"FROM signal_log{wsql}", params).fetchone()
            rows = con.execute(
                f"SELECT codes, result FROM signal_log{wsql}", params).fetchall()
            strength_rows = con.execute(
                f"SELECT strength, COALESCE(SUM(result='correct'),0), "
                f"COALESCE(SUM(result IN ('correct','wrong')),0) "
                f"FROM signal_log{wsql}"
                f"{' AND' if wsql else ' WHERE'} strength IS NOT NULL "
                f"GROUP BY strength", params).fetchall()
        finally:
            con.close()

    # Measured accuracy per strength label — the UI shows this next to the
    # badge so "STRONG" can't imply an edge the data doesn't support.
    by_strength = {
        s: {"n": n, "rate": round(w / n * 100, 1) if n else 0.0}
        for s, w, n in strength_rows if n
    }

    # Draws are broker refunds — excluded from every win-rate denominator.
    theory: dict[str, list[int]] = {}
    for codes, result in rows:
        if result == "draw":
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
        "total":      total or 0,
        "correct":    correct or 0,
        "wrong":      wrong_n or 0,
        "draws":      draws or 0,
        "rate":       round((correct or 0) / decided * 100, 1) if decided else 0.0,
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
        con = sqlite3.connect(DB_PATH)
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
