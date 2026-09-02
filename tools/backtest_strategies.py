"""
Deterministic confluence-council backtest.

python tools/backtest_strategies.py --all --seeds 2 --n 1500

═══════════════════════════════════════════════════════════════════════════
WHAT WAS WRONG WITH THE OLD HARNESS (and is fixed here):

  1. NON-REPRODUCIBLE — it seeded the generator with hash(asset). Python
     randomises string hashes per process, so EVERY RUN TESTED A DIFFERENT
     DATASET and no accuracy claim could be reproduced or diffed. Seeds now
     come from zlib.crc32(asset) + an explicit seed offset.
  2. CIRCULAR GENERATOR — p_cont was keyed off the archetype of the pair
     under test with TREND=0.54 baked in, then follow-style engines got
     credit for "finding" those trends. The continuation table here is a
     FIXED, PUBLISHED constant mirroring the live measurements
     (0.44-0.48 fade-side on the OTC pegs, ~0.53 on trenders) — it is
     structure the engine must LEARN from trailing data, not a handout.
  3. "EVERY CANDLE MUST SIGNAL" ASSERTION — the old harness crashed on
     NEUTRAL by design. The confluence council emits NEUTRAL (NO TRADE)
     whenever the voters do not agree; this harness verifies the OPPOSITE
     invariant: every emitted signal satisfies the council thresholds and
     every NEUTRAL candle is ungraded.
  4. CORRUPTED ATTRIBUTION — patterns were credited/debited with the
     ENGINE's final direction, so "pattern accuracy" was meaningless and
     the bad-detector feedback loop measured the wrong thing. Each VOTER
     is now graded on its OWN vote.
  5. INCOMPLETE AGGREGATION — strength/window histograms silently kept
     seed 0's values in multi-seed runs. Everything aggregates now.

GRADING SEMANTICS (matches live feed.py exactly):
  A signal computed from candles[0..i] predicts candle i+1 and is graded
  on candle i+1's own open→close direction. close==open is a draw
  (broker refund). NEUTRAL emits nothing and is never graded.

HONESTY NOTE: this is a synthetic feed. It validates CALIBRATION
(agreement-count and strength must be monotonic with accuracy) and
REGRESSION (re-runs must be byte-identical), not live profitability.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies import run_strategy, get_profile, list_profiles
from strategies.runner import MIN_AGREE, SCORE_FLOOR, WEIGHT_FLOOR

BREAKEVEN_92 = round(100.0 * 100.0 / 192.0, 2)   # 52.08 at 92% payout
BREAKEVEN_85 = round(100.0 * 100.0 / 185.0, 2)   # 54.05 at 85% payout

# FIXED continuation table (published, NOT read from the profile under
# test). Mirrors the live measurements in the repo's own research notes.
P_CONT_MAP = {
    "RANGE_BOUNCE": 0.46, "MEAN_REVERT": 0.44,
    "MIXED": 0.49, "TREND": 0.53,
}


def _wilson(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 100.0)
    p = correct / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (round(max(0.0, centre - half) * 100, 2),
            round(min(1.0, centre + half) * 100, 2))


def _gen_calibrated(seed: int, n: int, base_price: float, archetype: str,
                    vol_mult: float = 1.0, trap_freq: float = 0.10
                    ) -> list[dict]:
    """Synthetic 1-minute feed: per-candle direction sampled from the FIXED
    persistence table + converging tick walk (realistic wick anatomy) +
    wandering anchor + mean-reversion pull + engineered trap wicks."""
    rng = random.Random(seed & 0xFFFFFFFF)
    p_cont = P_CONT_MAP.get(archetype, 0.50)
    vol_map = {"TREND": 0.0018, "MEAN_REVERT": 0.0014,
               "RANGE_BOUNCE": 0.0010, "MIXED": 0.0015}
    vol = vol_map.get(archetype, 0.0015) * base_price * vol_mult
    pull_k = {"RANGE_BOUNCE": 0.06, "MEAN_REVERT": 0.10,
              "MIXED": 0.03, "TREND": 0.01}.get(archetype, 0.03)

    center = base_price
    last_dir = 1
    price = base_price
    t0 = 1_700_000_000 - n * 60
    candles: list[dict] = []
    for i in range(n):
        center *= 1.0 + rng.gauss(0, vol * 0.02 / base_price)
        pull = (center - price) * pull_k
        d = last_dir if rng.random() < p_cont else -last_dir
        body = abs(rng.gauss(0, vol * 0.6)) + vol * 0.15
        target = price + pull + d * body
        ticks = [price]
        cur = price
        for _ in range(59):
            cur += (target - cur) * 0.10 + rng.gauss(0, vol / 6)
            ticks.append(cur)
        c = ticks[-1]
        if rng.random() < trap_freq:
            sd = rng.choice([-1, 1])
            spike = vol * rng.uniform(2.5, 4.5)
            ticks[rng.randint(8, 50)] += sd * spike
        h = max(ticks)
        l = min(ticks)
        candles.append({"time": t0 + i * 60, "open": price,
                        "high": h, "low": l, "close": c})
        last_dir = 1 if c >= price else -1
        price = c
    return candles


BASE_PRICES = {
    "USDINR_otc": 83.0, "USDIDR_otc": 15800.0, "USDCOP_otc": 4100.0,
    "USDBDT_otc": 110.0, "USDMXN_otc": 19.5, "USDDZD_otc": 134.0,
    "USDPHP_otc": 58.0, "USDPKR_otc": 280.0, "USDZAR_otc": 18.5,
    "BRLUSD_otc": 5.5, "NZDUSD_otc": 0.61,
    "USDJPY": 150.0, "EURUSD": 1.085, "GBPUSD": 1.27, "AUDUSD": 0.655,
    "EURGBP": 0.855,
}
HIGH_VOL = {"USDZAR_otc", "USDMXN_otc", "USDCOP_otc", "BRLUSD_otc"}
LOW_VOL = {"EURGBP", "USDBDT_otc", "USDDZD_otc"}


def generate_pair_data(asset: str, n: int, seed: int) -> list[dict]:
    profile = get_profile(asset)
    base = BASE_PRICES.get(asset, 1.0)
    vol_mult = 2.0 if asset in HIGH_VOL else (0.6 if asset in LOW_VOL else 1.0)
    trap = (min(0.25, profile.trap_wick_sensitivity * 0.10)
            if asset.endswith("_otc") else 0.05)
    return _gen_calibrated(seed, n, base, profile.archetype, vol_mult, trap)


def _bucket_store():
    return {"n": 0, "correct": 0, "wrong": 0, "draw": 0}


def _bucket_add(b: dict, outcome: str):
    b["n"] += 1
    if outcome in ("correct", "wrong", "draw"):
        b[outcome] += 1


def _rate(b: dict) -> float | None:
    decided = b["correct"] + b["wrong"]
    return round(100.0 * b["correct"] / decided, 2) if decided else None


def run_backtest(asset: str, n: int, seed: int, quiet: bool = False) -> dict:
    """Run the council over one synthetic feed. Deterministic in
    (asset, n, seed)."""
    profile = get_profile(asset)
    candles = generate_pair_data(asset, n, seed)

    per_agree: dict[int, dict] = {}
    per_strength: dict[str, dict] = {}
    per_voter: dict[str, dict] = {}
    per_regime: dict[str, dict] = {}
    emitted = 0
    skipped_neutral = 0
    draws = 0
    warmup = 30

    for i in range(warmup, n - 1):
        window = candles[:i + 1]
        target = candles[i + 1]
        r = run_strategy(window, asset, period=60, ticks=None,
                         running_ticks=None)
        sig = r["signal"]
        if sig not in ("CALL", "PUT"):
            skipped_neutral += 1
            continue
        # ── INTEGRITY ASSERTIONS (confluence guarantees) ─────────────────
        assert r["agree"] >= MIN_AGREE, (
            f"{asset}@{i}: emitted with agree={r['agree']} < {MIN_AGREE}")
        assert abs(r.get("score", 0)) >= 1, (
            f"{asset}@{i}: emitted with noise-level score")
        conf = r.get("confluence") or {}
        assert conf.get("emitted") is True, (
            f"{asset}@{i}: signal disagrees with its own confluence block")

        move = target["close"] - target["open"]
        if abs(move) <= 1e-10:
            outcome = "draw"
        else:
            actual_up = move > 0
            outcome = ("correct"
                       if actual_up == (sig == "CALL") else "wrong")

        agree = r["agree"]
        per_agree.setdefault(agree, _bucket_store())
        _bucket_add(per_agree[agree], outcome)
        strength = r.get("strength") or "MEDIUM"
        per_strength.setdefault(strength, _bucket_store())
        _bucket_add(per_strength[strength], outcome)
        regime = (r.get("regime") or {}).get("trend") or "SIDEWAYS"
        per_regime.setdefault(regime, _bucket_store())
        _bucket_add(per_regime[regime], outcome)
        # VOTER ATTRIBUTION — each voter on its OWN vote (not the engine's)
        for v in r.get("voters") or []:
            if v.get("dir", 0) == 0:
                continue
            per_voter.setdefault(v["name"], _bucket_store())
            if outcome == "draw":
                _bucket_add(per_voter[v["name"]], "draw")
            else:
                v_ok = (v["dir"] > 0) == (move > 0)
                _bucket_add(per_voter[v["name"]],
                            "correct" if v_ok else "wrong")

        emitted += 1
        if outcome == "draw":
            draws += 1

    decided = emitted - draws
    correct = sum(b["correct"] for b in per_agree.values())
    lo, hi = _wilson(correct, decided)
    return {
        "asset": asset,
        "archetype": profile.archetype,
        "seed": seed,
        "candles": n,
        "decided_candles": n - 1 - warmup,
        "emitted": emitted,
        "neutral_share": round(100.0 * skipped_neutral /
                               max(1, n - 1 - warmup), 1),
        "correct": correct,
        "wrong": decided - correct,
        "draws": draws,
        "rate": round(100.0 * correct / decided, 2) if decided else None,
        "ci95": [lo, hi],
        "emission_share": round(100.0 * emitted /
                                max(1, n - 1 - warmup), 1),
        "per_agree": {str(k): {"n": b["n"], "correct": b["correct"],
                               "rate": _rate(b)}
                      for k, b in sorted(per_agree.items())},
        "per_strength": {k: {"n": b["n"], "correct": b["correct"],
                             "rate": _rate(b)}
                         for k, b in per_strength.items()},
        "per_voter": {k: {"n": b["n"], "correct": b["correct"],
                          "rate": _rate(b)}
                      for k, b in sorted(per_voter.items())},
        "per_regime": {k: {"n": b["n"], "correct": b["correct"],
                           "rate": _rate(b)}
                       for k, b in sorted(per_regime.items())},
    }


def _merge_acc(dst: dict, src: dict, key_field: str = "asset"):
    """Aggregate run dicts across seeds/pairs — every histogram merges."""
    for k in ("emitted", "correct", "wrong", "draws", "decided_candles"):
        dst[k] = dst.get(k, 0) + src.get(k, 0)
    for field in ("per_agree", "per_strength", "per_voter", "per_regime"):
        for bucket, stats in (src.get(field) or {}).items():
            acc = dst.setdefault(field, {}).setdefault(
                bucket, {"n": 0, "correct": 0})
            acc["n"] += stats["n"]
            acc["correct"] += stats["correct"]
            acc["rate"] = (round(100.0 * acc["correct"] / acc["n"], 2)
                           if acc["n"] else None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--seed-offset", type=int, default=0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.asset:
        assets = [args.asset]
    elif args.all:
        assets = [p.asset for p in list_profiles()]
    else:
        print("specify --asset ASSET or --all")
        return

    out_dir = args.out or os.environ.get(
        "BACKTEST_OUT",
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "reports"))
    os.makedirs(out_dir, exist_ok=True)

    all_runs = []
    print(f"{'PAIR':<12} {'ARCH':<13} {'EMIT%':>6} {'NEUT%':>6} "
          f"{'N':>5} {'RATE':>7} {'CI95':>17} {'STRONG':>7} {'MED':>7}")
    for asset in assets:
        merged = {"emitted": 0, "correct": 0, "wrong": 0, "draws": 0,
                  "decided_candles": 0}
        for s in range(args.seed_offset, args.seed_offset + args.seeds):
            seed = zlib.crc32(f"{asset}:{s}".encode()) & 0xFFFFFFFF
            r = run_backtest(asset, args.n, seed)
            r["seed"] = s
            all_runs.append(r)
            _merge_acc(merged, r)
        dec = merged["correct"] + merged["wrong"]
        rate = round(100.0 * merged["correct"] / dec, 2) if dec else None
        lo, hi = _wilson(merged["correct"], dec)
        st = (merged.get("per_strength") or {}).get("STRONG") or {}
        md = (merged.get("per_strength") or {}).get("MEDIUM") or {}
        emit_share = round(100.0 * merged["emitted"] /
                           max(1, merged["decided_candles"]), 1)
        neut = round(100.0 - emit_share, 1)
        print(f"{asset:<12} {get_profile(asset).archetype:<13} "
              f"{emit_share:>6.1f} {neut:>6.1f} {dec:>5} "
              f"{rate if rate is not None else '-':>7} "
              f"[{lo:>6.2f},{hi:>6.2f}] "
              f"{(st.get('rate') if st.get('rate') is not None else '-'):>7} "
              f"{(md.get('rate') if md.get('rate') is not None else '-'):>7}")

    # ── Portfolio-level aggregates ──────────────────────────────────────
    port = {"emitted": 0, "correct": 0, "wrong": 0, "draws": 0,
            "decided_candles": 0}
    for r in all_runs:
        _merge_acc(port, r)
    p_dec = port["correct"] + port["wrong"]
    p_rate = round(100.0 * port["correct"] / p_dec, 2) if p_dec else None
    p_lo, p_hi = _wilson(port["correct"], p_dec)

    def _acc(field):
        out = {}
        for bucket, st in (port.get(field) or {}).items():
            out[bucket] = {"n": st["n"],
                           "rate": (round(100.0 * st["correct"] / st["n"], 2)
                                    if st["n"] else None)}
        return out

    by_agree = _acc("per_agree")
    by_strength = _acc("per_strength")
    by_voter = _acc("per_voter")

    # Calibration checks — the reason the council exists
    agree_seq = [by_agree[k]["rate"] for k in sorted(by_agree, key=int)
                 if by_agree[k]["rate"] is not None and by_agree[k]["n"] >= 30]
    agree_monotonic = all(
        agree_seq[i + 1] >= agree_seq[i] - 1.0
        for i in range(len(agree_seq) - 1)) if len(agree_seq) >= 2 else None

    report = {
        "generated_at": int(time.time()),
        "engine": "confluence-council",
        "deterministic": True,
        "params": {"n": args.n, "seeds": args.seeds,
                   "min_agree": MIN_AGREE, "score_floor": SCORE_FLOOR,
                   "weight_floor": WEIGHT_FLOOR},
        "breakeven": {"payout_92": BREAKEVEN_92, "payout_85": BREAKEVEN_85},
        "portfolio": {
            "decided": p_dec,
            "emitted": port["emitted"],
            "emission_share": round(100.0 * port["emitted"] /
                                    max(1, port["decided_candles"]), 1),
            "rate": p_rate,
            "ci95": [p_lo, p_hi],
            "beats_breakeven_92": (p_lo > BREAKEVEN_92) if p_dec else False,
        },
        "calibration": {
            "by_agree": by_agree,
            "agree_monotonic": agree_monotonic,
            "by_strength": by_strength,
            "by_voter": by_voter,
        },
        "runs": all_runs,
    }

    out_path = os.path.join(out_dir, "backtest_report.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)
    md_path = os.path.join(out_dir, "backtest_report.md")
    with open(md_path, "w") as f:
        f.write(f"# Confluence-council backtest\n\n")
        f.write(f"- candles/pair: {args.n}, seeds: {args.seeds}, "
                f"deterministic (crc32 seeding)\n")
        f.write(f"- portfolio: **{p_rate}%** over {p_dec} decided signals "
                f"(95% CI [{p_lo}%, {p_hi}%])\n")
        f.write(f"- emission share: {report['portfolio']['emission_share']}% "
                f"of candles (the rest are NO TRADE)\n")
        f.write(f"- break-even at 92% payout: {BREAKEVEN_92}% | "
                f"at 85%: {BREAKEVEN_85}%\n\n")
        f.write("## Calibration by agreement count\n\n")
        f.write("| agree | n | rate |\n|---|---|---|\n")
        for k in sorted(by_agree, key=int):
            f.write(f"| {k} | {by_agree[k]['n']} | {by_agree[k]['rate']}% |\n")
        f.write("\n## By strength\n\n| strength | n | rate |\n|---|---|---|\n")
        for k, v in by_strength.items():
            f.write(f"| {k} | {v['n']} | {v['rate']}% |\n")
        f.write("\n## Voter accuracy (own vote, emitted candles)\n\n")
        f.write("| voter | n | rate |\n|---|---|---|\n")
        for k, v in by_voter.items():
            f.write(f"| {k} | {v['n']} | {v['rate']}% |\n")

    print(f"\nportfolio: {p_rate}% over {p_dec} decided "
          f"(CI95 [{p_lo}, {p_hi}]) | emission "
          f"{report['portfolio']['emission_share']}% of candles")
    print(f"calibration: agree-monotonic={agree_monotonic} | "
          f"by_agree={by_agree}")
    print(f"reports: {out_path} + {md_path}")


if __name__ == "__main__":
    main()
