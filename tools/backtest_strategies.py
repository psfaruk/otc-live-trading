"""
Per-pair backtest framework (v2 — honest validation).

IMPORTANT — what changed and why (2026-08 signal-accuracy overhaul):

The old backtest generated SYNTHETIC TRENDING data for TREND pairs and
then reported 60-82% accuracy — a circular validation. The generator
baked in exactly the structure the strategies assume, so the backtest
measured "does the engine detect the trends we invented for it" instead
of "will signals win on the real Quotex OTC feed".

The default generator is now CALIBRATED to the statistical signature
measured on the live feed (see analyze_eoc.py's recorded live stats):
  - Color-continuation probability (p_cont) per archetype. The live
    measurements put color-following at 47.3-48.3% on OTC pegs
    (marubozu n=859, spinning-top n=237, doji n=232), i.e. p_cont ~=
    0.45-0.48 — momentum FOLLOWING is an anti-signal there. TREND
    archetype pairs keep a hot 0.54 continuation bias.
  - A slowly wandering mean anchor + mean-reversion pull for
    RANGE_BOUNCE / MEAN_REVERT archetypes.
  - Trap wicks (spike + snap back) at the profile's trap sensitivity.

On this feed, a parrot (follow-last-color) engine scores exactly 1-p_cont
(46-55% by archetype, most BELOW 50%) — matching the live diagnosis that
predictions are "wrong". A context-aware engine must FOLLOW regime
structure and FADE colorless chop to clear the payout breakeven
(52.08% at 92% payout, 55.56% at 80%).

The report also attributes accuracy per DECISION PATH (pattern-vote vs
tiebreak regime vs tiebreak color) so regressions localize immediately.

Usage:
  python tools/backtest_strategies.py [--asset EURUSD] [--n 2000]
  python tools/backtest_strategies.py --all
  python tools/backtest_strategies.py --gen legacy   # old circular generator

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


def _gen_calibrated(seed: int, n: int, base_price: float,
                    profile: PairProfile, seed_offset: int = 0
                    ) -> list[dict]:
    """Feed generator calibrated to the MEASURED live-feed signature.

    Per-candle close direction is sampled with persistence p_cont
    (archetype-keyed), then a converging tick walk builds realistic wick
    anatomy around the chosen close. Trap-wick spikes perturb high/low
    only. A wandering anchor + pull gives range pairs their oscillation.

    Why p_cont matters: with p_cont < 0.5 the ONLY way to beat 50% is to
    fade the last candle in chop and follow it when real regime structure
    exists — exactly the behavior the live measurements demand.
    """
    rng = random.Random((seed ^ (seed_offset * 0x9E3779B9)) & 0xFFFFFFFF)
    p_cont_map = {"TREND": 0.54, "MIXED": 0.50,
                  "RANGE_BOUNCE": 0.47, "MEAN_REVERT": 0.45}
    p_cont = p_cont_map.get(profile.archetype, 0.50)

    vol_map = {"TREND": 0.0018, "MEAN_REVERT": 0.0014,
               "RANGE_BOUNCE": 0.0010, "MIXED": 0.0015}
    vol = vol_map.get(profile.archetype, 0.0015) * base_price
    if profile.asset in ("USDZAR_otc", "USDMXN_otc", "USDCOP_otc", "BRLUSD_otc"):
        vol *= 2.0
    if profile.asset in ("EURGBP", "USDBDT_otc", "USDDZD_otc"):
        vol *= 0.6

    # OTC pairs get trap wicks at profile sensitivity; real majors fewer.
    trap_freq = (max(0.0, min(0.25, profile.trap_wick_sensitivity * 0.10))
                 if profile.asset.endswith("_otc") else 0.05)

    center = base_price
    last_dir = 1
    price = base_price
    t0 = 1_700_000_000 - n * 60
    candles: list[dict] = []
    for i in range(n):
        # Wander the anchor (slow drift of the "fair" level)
        center *= 1.0 + rng.gauss(0, vol * 0.02 / base_price)
        # Mean-reversion pull toward the anchor (stronger for range pairs)
        pull_k = {"RANGE_BOUNCE": 0.06, "MEAN_REVERT": 0.10,
                  "MIXED": 0.03, "TREND": 0.01}.get(profile.archetype, 0.03)
        pull = (center - price) * pull_k
        # Direction with measured persistence
        if rng.random() < p_cont:
            d = last_dir
        else:
            d = -last_dir
        # Body magnitude (mean-reverting pull shapes big-move decay)
        body = abs(rng.gauss(0, vol * 0.6)) + vol * 0.15
        target = price + pull + d * body
        # Converging tick walk -> realistic wicks + close near target
        ticks = [price]
        cur = price
        for _ in range(59):
            cur += (target - cur) * 0.10 + rng.gauss(0, vol / 6)
            ticks.append(cur)
        c = ticks[-1]
        # Trap wick: single-tick spike far from the path (high/low only)
        if rng.random() < trap_freq:
            sd = rng.choice([-1, 1])
            spike = vol * rng.uniform(2.5, 4.5)
            ticks[rng.randint(8, 50)] += sd * spike
        h = max(ticks)
        l = min(ticks)
        candles.append({"time": t0 + i * 60, "open": price,
                        "high": h, "low": l, "close": c})
        # next candle's continuation reference = this candle's color
        last_dir = 1 if c >= price else -1
        price = c
    return candles


def generate_pair_data(profile: PairProfile, n: int = 2000,
                       seed_offset: int = 0, mode: str = "calibrated"
                       ) -> list[dict]:
    """Generate synthetic OHLC data for this pair (see module docstring).
    mode: 'calibrated' (measured-signature feed) or 'legacy' (circular)."""
    base_prices = {
        "USDINR_otc": 83.0, "USDIDR_otc": 15800.0, "USDCOP_otc": 4100.0,
        "USDBDT_otc": 110.0, "USDMXN_otc": 19.5, "USDDZD_otc": 134.0,
        "USDPHP_otc": 58.0, "USDPKR_otc": 280.0, "USDZAR_otc": 18.5,
        "BRLUSD_otc": 5.5, "NZDUSD_otc": 0.60,
        "USDJPY": 150.0, "EURUSD": 1.08, "GBPUSD": 1.27,
        "AUDUSD": 0.66, "EURGBP": 0.84,
    }
    base_price = base_prices.get(profile.asset, 1.0)

    if mode == "calibrated":
        return _gen_calibrated(hash(profile.asset) & 0xFFFFFFFF,
                               n, base_price, profile,
                               seed_offset=seed_offset)

    # ── Legacy generators (kept for A/B; circular for TREND pairs) ──────
    vol_map = {
        "TREND": 0.0018,
        "MEAN_REVERT": 0.0014,
        "RANGE_BOUNCE": 0.0010,
        "MIXED": 0.0015,
    }
    vol = vol_map.get(profile.archetype, 0.0015)
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
    path_stats: dict          # decision path -> {n, correct, acc}
    archetype: str
    best_window_acc: float
    worst_window_acc: float


def _decision_path(result: dict) -> str:
    """Attribute a graded signal to the path that produced it.
    Parses the same TIEBREAK reason markers the engines emit."""
    for r in result.get("reasons", []):
        if r.startswith("TIEBREAK:"):
            if "indep_net leans" in r:
                return "TIEBREAK_INDEP"
            if "regime=" in r:
                return "TIEBREAK_REGIME"
            return "TIEBREAK_COLOR"
    if result.get("score", 0) != 0:
        return "PATTERN_VOTE"
    return "OTHER"


def backtest_pair(profile: PairProfile, n: int = 2000,
                  warmup: int = 41, mode: str = "calibrated",
                  seed_offset: int = 0) -> BacktestResult:
    """Run the strategy engine over synthetic data for this pair."""
    candles = generate_pair_data(profile, n=n, mode=mode,
                                 seed_offset=seed_offset)

    # Per-pattern stats
    pattern_stats: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "correct": 0, "wrong": 0, "acc": 0.0,
                 "avg_strength": 0.0, "directions": []})

    n_signals = n_neutral = n_call = n_put = 0
    n_correct = n_wrong = n_draw = 0
    cont = rev = 0
    strength_dist: Counter = Counter()
    strength_acc: dict = defaultdict(lambda: [0, 0])  # label -> [correct, wrong]
    path_stats: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "correct": 0, "wrong": 0, "acc": 0.0})

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

        # Decision-path attribution (which pipeline layer produced this)
        _path = _decision_path(result)
        path_stats[_path]["n"] += 1
        if correct:
            path_stats[_path]["correct"] += 1
        else:
            path_stats[_path]["wrong"] += 1

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

    # Finalize path stats
    for pstat in path_stats.values():
        if pstat["n"]:
            pstat["acc"] = round(pstat["correct"] / pstat["n"] * 100, 2)

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
        path_stats=dict(path_stats),
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
    ap.add_argument("--gen", choices=["calibrated", "legacy"],
                    default="calibrated",
                    help="Feed generator: 'calibrated' (default) matches the "
                         "measured live signature (per-archetype continuation "
                         "probability + trap wicks); 'legacy' is the old "
                         "archetype generator (circular for TREND pairs)")
    ap.add_argument("--payout", type=float, default=92.0,
                    help="Payout %% used for the breakeven line (default 92)")
    ap.add_argument("--seeds", type=int, default=1,
                    help="Number of independent feeds per pair to average "
                         "over (default 1). Use >=3 for stable numbers — "
                         "single-seed accuracy carries ~±1.8%% noise.")
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

    print(f"Backtesting {len(profiles)} pairs × {args.n} candles each "
          f"(generator: {args.gen})...")
    be = 100.0 / (1.0 + args.payout / 100.0)
    print(f"Breakeven accuracy at {args.payout:.0f}% payout: {be:.2f}%")
    print(f"{'asset':14s} {'arch':14s} {'signals':>8s} {'correct':>8s} "
          f"{'wrong':>6s} {'acc%':>7s} {'cont%':>7s} {'best_h%':>8s} {'worst_h%':>9s}")
    print("-" * 100)

    all_results: list[BacktestResult] = []
    for p in profiles:
        if args.seeds <= 1:
            r = backtest_pair(p, n=args.n, warmup=args.warmup, mode=args.gen)
        else:
            # Multi-seed: average correctness over independent feeds so the
            # per-pair accuracy is not dominated by one generator seed.
            from dataclasses import replace as _dc_replace
            r = None
            for s in range(args.seeds):
                rs = backtest_pair(p, n=args.n, warmup=args.warmup,
                                   mode=args.gen, seed_offset=s)
                if r is None:
                    r = rs
                else:
                    r.n_correct += rs.n_correct
                    r.n_wrong += rs.n_wrong
                    r.n_draw += rs.n_draw
                    r.n_call += rs.n_call
                    r.n_put += rs.n_put
                    r.n_neutral += rs.n_neutral
                    r.n_signals += rs.n_signals
                    r.continuation_rate += rs.continuation_rate
                    for k, v in rs.strength_acc.items():
                        sa = r.strength_acc.setdefault(k, [0, 0])
                        sa[0] += v[0]; sa[1] += v[1]
                    for k, v in rs.path_stats.items():
                        pst = r.path_stats.setdefault(k, {"n": 0, "correct": 0,
                                                          "wrong": 0, "acc": 0.0})
                        pst["n"] += v["n"]; pst["correct"] += v["correct"]
                        pst["wrong"] += v["wrong"]
                    for k, v in rs.pattern_stats.items():
                        ppst = r.pattern_stats.setdefault(k, {"n": 0, "correct": 0,
                                                              "wrong": 0, "acc": 0.0,
                                                              "avg_strength": 0.0})
                        ppst["n"] += v["n"]; ppst["correct"] += v["correct"]
                        ppst["wrong"] += v["wrong"]
            graded = r.n_correct + r.n_wrong
            r.accuracy = round(r.n_correct / max(1, graded) * 100, 2)
            cr = r.continuation_rate / args.seeds
            r.continuation_rate = round(cr, 2)
            for pst in r.path_stats.values():
                if pst["n"]:
                    pst["acc"] = round(pst["correct"] / pst["n"] * 100, 2)
            for ppst in r.pattern_stats.values():
                if ppst["n"]:
                    ppst["acc"] = round(ppst["correct"] / ppst["n"] * 100, 1)
                    ppst["avg_strength"] = round(
                        ppst["avg_strength"] / args.seeds, 3)
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
        "generator": args.gen,
        "payout_pct": args.payout,
        "breakeven_accuracy_pct": round(be, 2),
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
                "path_stats": r.path_stats,
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
        f.write(f"- Generator: {args.gen}\n")
        f.write(f"- Payout: {args.payout:.0f}% -> breakeven accuracy: "
                f"**{be:.2f}%** (below this, the pair bleeds money)\n")
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

        f.write("\n## Decision-Path Accuracy (which pipeline layer decided)\n\n")
        f.write("| Pair | Path | N | Correct | Accuracy |\n")
        f.write("|------|------|---|---------|----------|\n")
        for r in all_results:
            for pname, pstat in sorted(r.path_stats.items(),
                                       key=lambda x: -x[1]["n"]):
                f.write(f"| {r.asset} | {pname} | {pstat['n']} | "
                        f"{pstat['correct']} | {pstat['acc']:.2f}% |\n")

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
