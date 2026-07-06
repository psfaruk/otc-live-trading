"""
Offline EOC replay harness — runs analyze_eoc over the candle history stored
in candle_micro.db and reports the signal statistics that matter for bias
auditing. Exists because every past analyzer audit rebuilt this ad hoc.

What it measures per (asset, period):
  - signal count, NEUTRAL share
  - continuation share: directional signals pointing the same way as the
    just-closed candle (the "parrot" metric — live signal_log measured 87%
    before the 2026-07 de-biasing work)
  - accuracy vs the next candle (the feed is near-random; expect ~50%)
  - strength distribution, |score| percentiles (for MAX_SCORE calibration)
  - per-theory net-vote accuracy (same _parse_votes attribution as live
    _grade_and_log)

Fidelity caveats vs live:
  - candle_micro only has rows for candles that saw >=10 ticks, and
    cleanup() prunes rows older than 7 days — segments reset on any ctime
    gap and the skip count is reported.
  - ticks are the stored <=240-point downsample, not the raw ~500-tick
    deque live RUN/TRAP saw (their up/down ratios shift slightly).
  - ZONE_LOSS_GUARD is per-stream live state and is NOT applied here —
    this measures the raw analyzer, pre-guard.

Usage:
  .venv/Scripts/python.exe tools/replay_eoc.py [--asset EURUSD_otc]
      [--period 60] [--warmup 41] [--db path/to/candle_micro.db]
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from analyze_eoc import analyze_eoc, _parse_votes   # noqa: E402
import db as _db                                     # noqa: E402


def _load_rows(con, asset: str, period: int):
    return con.execute(
        """SELECT ctime, open, high, low, close, ticks
           FROM candle_micro WHERE asset=? AND period=?
           ORDER BY ctime""", (asset, period)).fetchall()


def replay(db_path: str, asset_filter: str | None, period_filter: int | None,
           warmup: int) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    groups = con.execute(
        "SELECT DISTINCT asset, period FROM candle_micro ORDER BY asset").fetchall()

    stats = {
        "signals": 0, "neutral": 0, "call": 0, "put": 0,
        "cont": 0, "rev": 0,          # directional signals vs just-closed color
        "correct": 0, "wrong": 0, "draw": 0,
        "strength": Counter(), "scores": [],
        "theory": defaultdict(lambda: [0, 0]),   # code -> [right, wrong]
        "skipped_gap_resets": 0, "rows_total": 0, "rows_analyzed": 0,
    }

    for asset, period in groups:
        if asset_filter and asset != asset_filter:
            continue
        if period_filter and period != period_filter:
            continue
        rows = _load_rows(con, asset, period)
        stats["rows_total"] += len(rows)

        window: list[dict] = []
        prev_ctime = None
        for idx, (ctime, o, h, l, c, ticks_json) in enumerate(rows):
            if prev_ctime is not None and ctime != prev_ctime + period:
                window = []                       # contiguity break — reset
                stats["skipped_gap_resets"] += 1
            prev_ctime = ctime
            window.append({"time": ctime, "open": o, "high": h,
                           "low": l, "close": c})
            if len(window) > 300:                 # live keeps a bounded history
                window.pop(0)
            if len(window) < warmup:
                continue
            # Need the NEXT contiguous candle to grade the prediction.
            if idx + 1 >= len(rows) or rows[idx + 1][0] != ctime + period:
                continue

            ticks = None
            if ticks_json:
                try:
                    ticks = json.loads(ticks_json)
                except Exception:
                    ticks = None

            # Same micro_history construction as feed._analyze_core.
            micro_hist = _db.get_micro_history(asset, period, n=5,
                                               before_ctime=ctime)
            result = analyze_eoc(window, ticks, micro_history=micro_hist,
                                 period=period)
            stats["rows_analyzed"] += 1

            sig = result["signal"]
            stats["signals"] += 1
            stats["scores"].append(abs(result.get("score", 0)))
            if sig == "NEUTRAL":
                stats["neutral"] += 1
                continue
            stats["call" if sig == "CALL" else "put"] += 1
            stats["strength"][result.get("strength", "?")] += 1

            closed_bull = c >= o
            if (sig == "CALL") == closed_bull:
                stats["cont"] += 1
            else:
                stats["rev"] += 1

            n_o, n_c = rows[idx + 1][1], rows[idx + 1][4]
            if n_c == n_o:
                stats["draw"] += 1
                continue
            actual_up = n_c > n_o
            correct = (sig == "CALL") == actual_up
            stats["correct" if correct else "wrong"] += 1

            # Per-theory attribution — same net-vote logic as _grade_and_log.
            for code, direction, _w in _parse_votes(result.get("reasons", [])):
                if direction == 0:
                    continue
                theory_correct = (direction > 0) == actual_up
                stats["theory"][code][0 if theory_correct else 1] += 1

    con.close()
    return stats


def report(s: dict) -> None:
    total = s["signals"]
    if not total:
        print("no signals produced — check filters/warmup")
        return
    directional = s["call"] + s["put"]
    graded = s["correct"] + s["wrong"]
    scores = sorted(s["scores"])

    def pct(v, base):
        return f"{v / base * 100:5.1f}%" if base else "   n/a"

    def pctile(p):
        return scores[min(int(len(scores) * p), len(scores) - 1)] if scores else 0

    print(f"rows: {s['rows_total']} total, {s['rows_analyzed']} analyzed, "
          f"{s['skipped_gap_resets']} gap resets")
    print(f"signals: {total}   NEUTRAL {s['neutral']} ({pct(s['neutral'], total)})   "
          f"CALL {s['call']}  PUT {s['put']}")
    if directional:
        print(f"continuation share (vs just-closed candle): "
              f"{pct(s['cont'], s['cont'] + s['rev'])}  "
              f"({s['cont']} cont / {s['rev']} rev)")
        print(f"accuracy: {pct(s['correct'], graded)} "
              f"({s['correct']}/{graded}, {s['draw']} draws excluded)")
        print(f"strength: {dict(s['strength'])}")
    print(f"|score|: p50={pctile(0.50)} p90={pctile(0.90)} "
          f"p99={pctile(0.99)} max={scores[-1] if scores else 0}")
    print(f"\n{'theory':10s} {'n':>6s} {'right':>6s} {'wrong':>6s} {'acc%':>6s}")
    for code, (r, w) in sorted(s["theory"].items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
        n = r + w
        print(f"{code:10s} {n:6d} {r:6d} {w:6d} {r / n * 100 if n else 0:6.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", default=os.path.join(_ROOT, "candle_micro.db"))
    ap.add_argument("--asset", default=None)
    ap.add_argument("--period", type=int, default=None)
    ap.add_argument("--warmup", type=int, default=41,
                    help="candles required before analyzing (key-levels lookback is 40)")
    args = ap.parse_args()
    # get_micro_history reads db.DB_PATH — point it at the same file we replay
    _db.DB_PATH = args.db
    report(replay(args.db, args.asset, args.period, args.warmup))
