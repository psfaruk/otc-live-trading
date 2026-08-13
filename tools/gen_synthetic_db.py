"""
Generate a synthetic candle_micro.db for backtest verification.

Creates ~2000 candles per asset for 4 assets (EURUSD_otc, GBPUSD_otc,
USDJPY_otc, AUDUSD_otc) with realistic price action (random walk + trend
regimes + occasional reversals). Each candle gets ~60 downsampled ticks.
This lets us run tools/replay_eoc.py to verify the analyzer post-refactor.

The data is SYNTHETIC — accuracy numbers won't reflect real market edge.
The point is to verify:
  1. NEUTRAL count is 0 (the new always-emit requirement).
  2. CALL/PUT split is roughly balanced.
  3. Strength distribution has all 3 tiers (STRONG/MEDIUM/WEAK).
  4. No exceptions during analyze_eoc across 8000+ candles.
"""
import os
import sys
import json
import sqlite3
import random
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import db as _db  # use the same schema

DB_PATH = os.path.join(_ROOT, "candle_micro_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

# Initialize schema
_db.DB_PATH = DB_PATH
_db.init()

ASSETS = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDUSD_otc"]
PERIOD = 60
N_CANDLES = 2000
START_TIME = int(time.time()) - N_CANDLES * PERIOD

random.seed(42)

con = sqlite3.connect(DB_PATH)
try:
    for asset in ASSETS:
        # Each asset: random walk with periodic regime shifts
        price = 1.0 if "JPY" not in asset else 110.0
        regime = 0  # 0=side, 1=up, -1=down
        regime_left = 0
        for i in range(N_CANDLES):
            if regime_left <= 0:
                regime = random.choice([-1, 0, 0, 0, 1, 1])
                regime_left = random.randint(30, 80)
            regime_left -= 1

            drift = regime * 0.00005
            o = price
            # 60 ticks within the candle
            ticks = []
            cur = o
            for t in range(60):
                cur += random.gauss(drift, 0.0003)
                ticks.append(round(cur, 6))
            c = ticks[-1]
            h = max(ticks)
            l = min(ticks)
            ctime = START_TIME + i * PERIOD
            # buy_pct: fraction of ticks that were up-moves
            up = sum(1 for k in range(1, len(ticks)) if ticks[k] > ticks[k-1])
            dn = sum(1 for k in range(1, len(ticks)) if ticks[k] < ticks[k-1])
            bpct = (up / (up + dn) * 100) if (up + dn) else 50.0

            ticks_json = json.dumps(ticks)
            con.execute(
                """INSERT OR REPLACE INTO candle_micro
                   (asset, period, ctime, open, high, low, close,
                    buy_pct, pressure, tick_count, ticks, gap_pct, gap_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'NONE')""",
                (asset, PERIOD, ctime, o, h, l, c,
                 bpct, bpct, len(ticks), ticks_json)
            )
            price = c  # next candle opens where this one closed
    con.commit()
    print(f"Generated {N_CANDLES * len(ASSETS)} rows across {len(ASSETS)} assets -> {DB_PATH}")
finally:
    con.close()
