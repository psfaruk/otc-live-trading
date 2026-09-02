"""Gate grid-search: record every emitted signal, then evaluate candidate
council thresholds offline to pick the honesty/quantity trade-off."""
import os, sys, zlib, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategies import get_profile, list_profiles
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
from backtest_strategies import generate_pair_data, _wilson, BREAKEVEN_92

from strategies.runner import run_strategy
from strategies import runner as R

def collect(assets, n, seeds):
    recs = []
    for asset in assets:
        for s in range(seeds):
            seed = zlib.crc32(f"{asset}:{s}".encode()) & 0xFFFFFFFF
            candles = generate_pair_data(asset, n, seed)
            for i in range(30, n - 1):
                r = run_strategy(candles[:i + 1], asset, period=60)
                if r["signal"] not in ("CALL", "PUT"):
                    continue
                t = candles[i + 1]
                move = t["close"] - t["open"]
                if abs(move) <= 1e-10:
                    continue
                voters = {v["name"]: v["dir"] for v in r.get("voters", [])}
                recs.append({
                    "asset": asset, "agree": r["agree"],
                    "aw": r["agree_weight"], "net": r["score"],
                    "strength": r["strength"],
                    "stat_agrees": voters.get("STAT") == (1 if r["signal"] == "CALL" else -1),
                    "win": 1 if (move > 0) == (r["signal"] == "CALL") else 0,
                })
    return recs

def evaluate(recs, min_agree, min_aw, min_net, need_stat):
    sel = [r for r in recs if r["agree"] >= min_agree and r["aw"] >= min_aw
           and abs(r["net"]) >= min_net and (r["stat_agrees"] or not need_stat)]
    n = len(sel)
    if n < 50:
        return None
    w = sum(r["win"] for r in sel)
    lo, hi = _wilson(w, n)
    return {"n": n, "rate": round(100 * w / n, 2), "ci": [lo, hi]}

if __name__ == "__main__":
    assets = [p.asset for p in list_profiles()]
    print("collecting...")
    recs = collect(assets, 1500, 2)
    print(f"{len(recs)} emitted signals recorded")
    print(f"{'MIN_AGREE':>9} {'MIN_AW':>7} {'MIN_NET':>8} {'STAT':>5} | {'N':>6} {'RATE':>7} {'CI':>18} {'BE92':>5}")
    for min_agree in (3, 4):
        for min_aw in (4.0, 5.0, 6.0, 7.0):
            for min_net in (3.0, 4.0, 5.0):
                for need_stat in (False, True):
                    r = evaluate(recs, min_agree, min_aw, min_net, need_stat)
                    if r:
                        beat = "PASS" if r["ci"][0] > BREAKEVEN_92 else ("pt>" if r["rate"] > BREAKEVEN_92 else "-")
                        print(f"{min_agree:>9} {min_aw:>7} {min_net:>8} {str(need_stat):>5} | "
                              f"{r['n']:>6} {r['rate']:>7} [{r['ci'][0]:>6},{r['ci'][1]:>6}] {beat:>5}")
