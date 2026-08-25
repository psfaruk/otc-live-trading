"""
Per-pair synthetic backtest framework.

Generates synthetic 1-minute candle data that mimics each pair's
research-documented behavior (Trend/Cycle/Noise + Trap Wick),
then runs the strategy engine over it and reports per-strategy
accuracy + per-pair accuracy + continuation bias.

Usage:
  python tools/backtest_strategies.py [--asset EURUSD] [--n 2000]
  python tools/backtest_strategies.py --all

Output:
  <repo>/reports/backtest_report.json
  <repo>/reports/backtest_report.md

Override the directory with --out, or the BACKTEST_OUT env var.
"""
from __future__ import annotations
import argparse
import json
import math
import os
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from strategies import run_strategy, list_profiles
from strategies.pair_profiles import PairProfile


# ── Synthetic price generator per pair archetype ──────────────────────────────

def _gen_trending(seed: int, n: int, base_price: float, drift_pct: float,
                  volatility: float, trap_freq: float) -> list[dict]:
    """Generate candles with a strong trend component + occasional trap wicks."""
    rng = random.Random(seed)
    candles: list[dict] = []
    price = base_price
    t0 = int(time.time()) - n * 60
    drift_per_candle = base_price * drift_pct
    vol = base_price * volatility
    for i in range(n):
        # Periodic regime shifts within the trend
        if i % 80 == 0:
            regime_shift = rng.choice([-1, 1]) * 0.5
        else:
            regime_shift = 0
        o = price
        ticks = [o]
        cur = o
        for _ in range(60):
            cur += rng.gauss(drift_per_candle + regime_shift * drift_per_candle * 0.5,
                              vol / math.sqrt(60))
            ticks.append(cur)
        # Occasional trap wick: spike one tick far from real price then snap back
        if rng.random() < trap_freq:
            spike_dir = rng.choice([-1, 1])
            spike_size = vol * rng.uniform(2.5, 4.5)
            spike_idx = rng.randint(15, 45)
            ticks[spike_idx] = cur + spike_dir * spike_size
        c = ticks[-1]
        h = max(ticks)
        l = min(ticks)
        candles.append({"time": t0 + i * 60, "open": o, "high": h,
                        "low": l, "close": c})
        price = c
    return candles


def _gen_mean_reverting(seed: int, n: int, base_price: float,
                        volatility: float, range_pct: float,
                        trap_freq: float) -> list[dict]:
    """Generate candles that oscillate around a fixed mean (range-bound)."""
    rng = random.Random(seed)
    candles: list[dict] = []
    center = base_price
    range_size = base_price * range_pct
    vol = base_price * volatility
    t0 = int(time.time()) - n * 60
    price = base_price
    for i in range(n):
        # Pull back toward center
        pull = (center - price) * 0.15
        o = price
        ticks = [o]
        cur = o
        for _ in range(60):
            cur += rng.gauss(pull / 60, vol / math.sqrt(60))
            # Reflect off range edges
            if cur > center + range_size:
                cur = center + range_size - (cur - center - range_size) * 0.5
            elif cur < center - range_size:
                cur = center - range_size + (center - range_size - cur) * 0.5
            ticks.append(cur)
        # Trap wicks more frequent in mean-reverting pairs (Quotex behavior)
        if rng.random() < trap_freq:
            spike_dir = rng.choice([-1, 1])
            spike_size = vol * rng.uniform(2.0, 4.0)
            spike_idx = rng.randint(15, 45)
            ticks[spike_idx] = cur + spike_dir * spike_size
        c = ticks[-1]
        h = max(ticks)
        l = min(ticks)
        candles.append({"time": t0 + i * 60, "open": o, "high": h,
                        "low": l, "close": c})
        price = c
    return candles


def _gen_mixed(seed: int, n: int, base_price: float, volatility: float,
               trap_freq: float) -> list[dict]:
    """Generate candles with mixed regime: trend phases + range phases."""
    rng = random.Random(seed)
    candles: list[dict] = []
    price = base_price
    vol = base_price * volatility
    t0 = int(time.time()) - n * 60
    regime = "trend"
    regime_left = 0
    drift = 0
    for i in range(n):
        if regime_left <= 0:
            regime = rng.choice(["trend", "trend", "range", "range"])
            regime_left = rng.randint(30, 80)
            drift = rng.choice([-1, 1]) * base_price * 0.0002 if regime == "trend" else 0
        regime_left -= 1
        o = price
        ticks = [o]
        cur = o
        pull = (price - base_price) * 0.05 if regime == "range" else 0
        for _ in range(60):
            cur += rng.gauss((drift - pull) / 60, vol / math.sqrt(60))
            ticks.append(cur)
        if rng.random() < trap_freq:
            spike_dir = rng.choice([-1, 1])
            spike_size = vol * rng.uniform(2.5, 4.5)
            spike_idx = rng.randint(15, 45)
            ticks[spike_idx] = cur + spike_dir * spike_size
        c = ticks[-1]
        h = max(ticks)
        l = min(ticks)
        candles.append({"time": t0 + i * 60, "open": o, "high": h,
                        "low": l, "close": c})
        price = c
    return candles


def generate_pair_data(profile: PairProfile, n: int = 2000,
                       seed_offset: int = 0) -> list[dict]:
    """Generate synthetic OHLC data mimicking this pair's archetype."""
    # Base price per pair (realistic for forex)
    base_prices = {
        "USDINR_otc": 83.0, "USDIDR_otc": 15800.0, "USDCOP_otc": 4100.0,
        "USDBDT_otc": 110.0, "USDMXN_otc": 19.5, "USDDZD_otc": 134.0,
        "USDPHP_otc": 58.0, "USDPKR_otc": 280.0, "USDZAR_otc": 18.5,
        "BRLUSD_otc": 5.5, "NZDUSD_otc": 0.60,
        "USDJPY": 150.0, "EURUSD": 1.08, "GBPUSD": 1.27,
        "AUDUSD": 0.66, "EURGBP": 0.84,
    }
    base_price = base_prices.get(profile.asset, 1.0)

    # Volatility per pair archetype (matches research)
    vol_map = {
        "TREND": 0.0018,
        "MEAN_REVERT": 0.0014,
        "RANGE_BOUNCE": 0.0010,
        "MIXED": 0.0015,
    }
    vol = vol_map.get(profile.archetype, 0.0015)
    # Scale for higher-vol pairs (USDZAR, USDMXN, USDCOP, BRLUSD)
    if profile.asset in ("USDZAR_otc", "USDMXN_otc", "USDCOP_otc", "BRLUSD_otc"):
        vol *= 2.0
    if profile.asset in ("EURGBP", "USDBDT_otc", "USDDZD_otc"):
        vol *= 0.6

    trap_freq = max(0.0, min(0.25, profile.trap_wick_sensitivity * 0.10))

    seed = hash(profile.asset) & 0xFFFFFFFF ^ seed_offset
    if profile.archetype == "TREND":
        # Each trend phase drifts ~0.5% per 100 candles
        return _gen_trending(seed, n, base_price, 0.00005, vol, trap_freq)
    elif profile.archetype == "MEAN_REVERT":
        return _gen_mean_reverting(seed, n, base_price, vol, 0.005, trap_freq)
    elif profile.archetype == "RANGE_BOUNCE":
        return _gen_mean_reverting(seed, n, base_price, vol * 0.8, 0.004, trap_freq)
    else:  # MIXED
        return _gen_mixed(seed, n, base_price, vol, trap_freq)


# ── Backtest runner ────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    asset: str
    n_candles: int
    n_signals: int
    n_neutral: int
    n_call: int
    n_put: int
    n_correct: int
    n_wrong: int
    n_draw: int
    accuracy: float
    continuation_rate: float   # how often signal matches last candle color
    strength_dist: dict
    strength_acc: dict
    pattern_stats: dict       # pattern_name -> {n, correct, wrong, acc, avg_strength}
    archetype: str
    best_window_acc: float
    worst_window_acc: float


def backtest_pair(profile: PairProfile, n: int = 2000,
                  warmup: int = 41) -> BacktestResult:
    """Run the strategy engine over synthetic data for this pair."""
    candles = generate_pair_data(profile, n=n)

    # Per-pattern stats
    pattern_stats: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "correct": 0, "wrong": 0, "acc": 0.0,
                 "avg_strength": 0.0, "directions": []})

    n_signals = n_neutral = n_call = n_put = 0
    n_correct = n_wrong = n_draw = 0
    cont = rev = 0
    strength_dist: Counter = Counter()
    strength_acc: dict = defaultdict(lambda: [0, 0])  # label -> [correct, wrong]

    # Track best/worst window accuracy
    win_correct: dict[int, list[int]] = defaultdict(lambda: [0, 0])  # hour -> [c, w]

    for i in range(warmup, len(candles) - 1):
        window = candles[:i+1]
        # Build a partial tick list from this candle's high-low-close
        # (no real tick history in synthetic mode)
        result = run_strategy(window, asset=profile.asset, period=60,
                              ticks=None, running_ticks=None)

        sig = result["signal"]
        n_signals += 1
        strength_dist[result["strength"]] += 1
        if sig == "NEUTRAL":
            n_neutral += 1
            continue
        if sig == "CALL":
            n_call += 1
        else:
            n_put += 1

        # Compare with NEXT candle's direction
        next_c = candles[i + 1]
        if next_c["close"] == next_c["open"]:
            n_draw += 1
            continue
        actual_up = next_c["close"] > next_c["open"]
        pred_up = sig == "CALL"
        correct = pred_up == actual_up
        if correct:
            n_correct += 1
            strength_acc[result["strength"]][0] += 1
        else:
            n_wrong += 1
            strength_acc[result["strength"]][1] += 1

        # Continuation rate
        last_bull = window[-1]["close"] >= window[-1]["open"]
        if pred_up == last_bull:
            cont += 1
        else:
            rev += 1

        # Per-pattern attribution
        for pname in result.get("patterns_fired", []):
            ps = pattern_stats[pname]
            ps["n"] += 1
            ps["directions"].append(pred_up)
            if correct:
                ps["correct"] += 1
            else:
                ps["wrong"] += 1
            ps["avg_strength"] += result.get("confidence", 0)

        # Window accuracy
        hour_utc = (window[-1]["time"] // 3600) % 24
        if correct:
            win_correct[hour_utc][0] += 1
        else:
            win_correct[hour_utc][1] += 1

    graded = n_correct + n_wrong
    accuracy = (n_correct / graded * 100) if graded else 0.0
    cont_rate = (cont / (cont + rev) * 100) if (cont + rev) else 0.0

    # ── Hard guarantee: every closed candle MUST emit CALL or PUT ─────────
    # The user's explicit requirement is "প্রত্যেক পেয়ার এ প্রত্যেক ক্যান্ডেল এ
    # সিগন্যাল আসতে হবে" — every candle on every pair must produce a signal.
    # The strategy engine's tiebreak (strategies/runner.py:515-530) and the
    # legacy analyze_eoc tiebreak both guarantee this on any non-empty candle
    # window. If n_neutral > 0 here, the guarantee is broken — surface it
    # loudly instead of silently shipping a regression.
    if n_neutral > 0:
        raise AssertionError(
            f"INVARIANT VIOLATION on {profile.asset}: {n_neutral} of "
            f"{n_signals} closed candles returned NEUTRAL instead of "
            f"CALL/PUT. The always-emit tiebreak is broken — investigate "
            f"strategies/runner.py:515-530 before deploying."
        )

    # Finalize pattern stats
    for ps in pattern_stats.values():
        if ps["n"]:
            ps["acc"] = round(ps["correct"] / ps["n"] * 100, 1)
            ps["avg_strength"] = round(ps["avg_strength"] / ps["n"], 3)
        del ps["directions"]   # don't serialize

    # Best/worst hour
    best_w = max(win_correct.items(), key=lambda x: x[1][0] / max(1, sum(x[1])))
    worst_w = min(win_correct.items(), key=lambda x: x[1][0] / max(1, sum(x[1])))
    best_acc = best_w[1][0] / max(1, sum(best_w[1])) * 100
    worst_acc = worst_w[1][0] / max(1, sum(worst_w[1])) * 100

    return BacktestResult(
        asset=profile.asset, n_candles=len(candles),
        n_signals=n_signals, n_neutral=n_neutral,
        n_call=n_call, n_put=n_put,
        n_correct=n_correct, n_wrong=n_wrong, n_draw=n_draw,
        accuracy=round(accuracy, 2),
        continuation_rate=round(cont_rate, 2),
        strength_dist=dict(strength_dist),
        strength_acc={k: v for k, v in strength_acc.items()},
        pattern_stats=dict(pattern_stats),
        archetype=profile.archetype,
        best_window_acc=round(best_acc, 2),
        worst_window_acc=round(worst_acc, 2),
    )


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--asset", default=None,
                    help="Run only this asset (default: all)")
    ap.add_argument("--all", action="store_true",
                    help="Run all profiles")
    ap.add_argument("--n", type=int, default=2000,
                    help="Number of candles per pair")
    ap.add_argument("--warmup", type=int, default=41,
                    help="Warmup candles before analysis starts")
    ap.add_argument("--out", default=None,
                    help="Output directory (default: <repo>/reports, or "
                         "$BACKTEST_OUT)")
    args = ap.parse_args()

    # Was hardcoded to one developer's laptop path (/home/z/my-project/
    # download) — same class of bug as the old hardcoded Windows QX_ROOT.
    # It happened to work locally and blew up (or silently wrote to a junk
    # tree) anywhere else, Railway included. Resolve relative to the repo
    # so the tool works from any checkout and any working directory.
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = (args.out
               or os.environ.get("BACKTEST_OUT", "").strip()
               or os.path.join(_repo_root, "reports"))
    os.makedirs(out_dir, exist_ok=True)

    profiles = list_profiles()
    if args.asset:
        profiles = [p for p in profiles if p.asset == args.asset]
    elif not args.all and not args.asset:
        # Default: run all
        pass

    print(f"Backtesting {len(profiles)} pairs × {args.n} candles each...")
    print(f"{'asset':14s} {'arch':14s} {'signals':>8s} {'correct':>8s} "
          f"{'wrong':>6s} {'acc%':>7s} {'cont%':>7s} {'best_h%':>8s} {'worst_h%':>9s}")
    print("-" * 100)

    all_results: list[BacktestResult] = []
    for p in profiles:
        r = backtest_pair(p, n=args.n, warmup=args.warmup)
        all_results.append(r)
        print(f"{r.asset:14s} {r.archetype:14s} {r.n_signals:8d} "
              f"{r.n_correct:8d} {r.n_wrong:6d} {r.accuracy:6.2f}% "
              f"{r.continuation_rate:6.2f}% {r.best_window_acc:7.2f}% "
              f"{r.worst_window_acc:8.2f}%")

    # Aggregate stats
    total_correct = sum(r.n_correct for r in all_results)
    total_wrong = sum(r.n_wrong for r in all_results)
    overall_acc = total_correct / max(1, total_correct + total_wrong) * 100

    # Per-pattern aggregate
    pattern_agg: dict[str, dict] = defaultdict(lambda: {"n": 0, "correct": 0, "wrong": 0})
    for r in all_results:
        for pname, ps in r.pattern_stats.items():
            pattern_agg[pname]["n"] += ps["n"]
            pattern_agg[pname]["correct"] += ps["correct"]
            pattern_agg[pname]["wrong"] += ps["wrong"]

    # Save JSON
    out_json = {
        "generated_at": int(time.time()),
        "n_candles_per_pair": args.n,
        "overall_accuracy": round(overall_acc, 2),
        "total_correct": total_correct,
        "total_wrong": total_wrong,
        "per_pair": [
            {
                "asset": r.asset, "archetype": r.archetype,
                "n_candles": r.n_candles, "n_signals": r.n_signals,
                "n_neutral": r.n_neutral, "n_call": r.n_call, "n_put": r.n_put,
                "n_correct": r.n_correct, "n_wrong": r.n_wrong, "n_draw": r.n_draw,
                "accuracy": r.accuracy,
                "continuation_rate": r.continuation_rate,
                "strength_dist": r.strength_dist,
                "strength_acc": r.strength_acc,
                "pattern_stats": r.pattern_stats,
                "best_window_acc": r.best_window_acc,
                "worst_window_acc": r.worst_window_acc,
            } for r in all_results
        ],
        "pattern_aggregate": {
            k: {**v, "acc": round(v["correct"] / max(1, v["n"]) * 100, 2)}
            for k, v in sorted(pattern_agg.items(),
                                key=lambda x: -x[1]["n"])
        },
    }

    json_path = os.path.join(out_dir, "backtest_report.json")
    with open(json_path, "w") as f:
        json.dump(out_json, f, indent=2)
    print(f"\nJSON report saved to: {json_path}")

    # Save Markdown
    md_path = os.path.join(out_dir, "backtest_report.md")
    with open(md_path, "w") as f:
        f.write("# Strategy Backtest Report\n\n")
        f.write(f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")
        f.write(f"- Candles per pair: {args.n}\n")
        f.write(f"- Overall accuracy: **{overall_acc:.2f}%**\n")
        f.write(f"- Total correct: {total_correct}, Total wrong: {total_wrong}\n\n")

        f.write("## Per-Pair Results\n\n")
        f.write("| Pair | Archetype | Signals | Correct | Wrong | Accuracy | Cont% | Best Hour% | Worst Hour% |\n")
        f.write("|------|-----------|---------|---------|-------|----------|-------|------------|-------------|\n")
        for r in all_results:
            f.write(f"| {r.asset} | {r.archetype} | {r.n_signals} | "
                    f"{r.n_correct} | {r.n_wrong} | **{r.accuracy:.2f}%** | "
                    f"{r.continuation_rate:.2f}% | {r.best_window_acc:.2f}% | "
                    f"{r.worst_window_acc:.2f}% |\n")

        f.write("\n## Per-Pattern Aggregate\n\n")
        f.write("| Pattern | N | Correct | Wrong | Accuracy |\n")
        f.write("|---------|---|---------|-------|----------|\n")
        for pname, ps in sorted(pattern_agg.items(),
                                key=lambda x: -x[1]["n"]):
            acc = ps["correct"] / max(1, ps["n"]) * 100
            f.write(f"| {pname} | {ps['n']} | {ps['correct']} | "
                    f"{ps['wrong']} | **{acc:.2f}%** |\n")

        f.write("\n## Per-Pair Per-Pattern Breakdown\n\n")
        for r in all_results:
            if not r.pattern_stats:
                continue
            f.write(f"### {r.asset} ({r.archetype})\n\n")
            f.write("| Pattern | N | Correct | Wrong | Accuracy |\n")
            f.write("|---------|---|---------|-------|----------|\n")
            for pname, ps in sorted(r.pattern_stats.items(),
                                     key=lambda x: -x[1]["n"]):
                f.write(f"| {pname} | {ps['n']} | {ps['correct']} | "
                        f"{ps['wrong']} | {ps['acc']:.2f}% |\n")
            f.write("\n")
    print(f"Markdown report saved to: {md_path}")

    return out_json


if __name__ == "__main__":
    main()
