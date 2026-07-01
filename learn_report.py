#!/usr/bin/env python3
"""Learning report: turns settled weather bets into evidence and tweaks.

Reads logs/weather_bets.csv (paper) and logs/weather_live_bets.csv (live),
and prints:
  1. Calibration - our predicted probability vs the realized win rate,
     in buckets. THE key table: if 60% predictions win ~60%, the edge is
     real; if they win ~50%, we have no edge and live trading should stop.
  2. P&L breakdown by city, by hi/lo, by side - finds where the edge
     lives and where it leaks.
  3. Fee drag and expectancy per bet.
  4. Data-driven suggestions (only when sample size is big enough).

Run:  python learn_report.py
"""
import csv, os, sys
from collections import defaultdict

FILES = [("PAPER", os.path.join("logs", "weather_bets.csv")),
         ("LIVE", os.path.join("logs", "weather_live_bets.csv"))]


def load(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            if r.get("event") != "SETTLE":
                continue
            try:
                rows.append({"city": r["city"], "hl": r["hl"], "side": r["side"],
                             "p": float(r["our_prob_side"]),
                             "entry": float(r["entry_c"]),
                             "count": float(r["count"]),
                             "won": int(r["outcome"]),
                             "pnl": float(r["pnl_$"])})
            except (ValueError, KeyError):
                continue
    return rows


def calibration(rows):
    buckets = defaultdict(lambda: [0, 0, 0.0])   # bucket -> [n, wins, pnl]
    for r in rows:
        b = min(int(r["p"] * 10), 9)
        buckets[b][0] += 1
        buckets[b][1] += r["won"]
        buckets[b][2] += r["pnl"]
    print("  our prob   bets   won   actual   pnl$")
    for b in sorted(buckets):
        n, w, pnl = buckets[b]
        print(f"  {b*10:3d}-{b*10+9:2d}%   {n:4d}  {w:4d}   {w/n*100:5.1f}%  {pnl:+7.2f}")


def by_key(rows, key):
    agg = defaultdict(lambda: [0, 0, 0.0])
    for r in rows:
        agg[r[key]][0] += 1
        agg[r[key]][1] += r["won"]
        agg[r[key]][2] += r["pnl"]
    for k in sorted(agg, key=lambda k: agg[k][2]):
        n, w, pnl = agg[k]
        print(f"  {str(k):>12}: {n:3d} bets, {w/n*100:4.0f}% won, {pnl:+7.2f}$")


ERA_CUT = "2026-07-01"   # disciplined filters + market-price guard fully live


def main():
    for label, path in FILES:
        rows = load(path)
        print(f"\n===== {label} ({len(rows)} settled bets) =====")
        if not rows:
            print("  no settled bets yet")
            continue
        total = sum(r["pnl"] for r in rows)
        wins = sum(r["won"] for r in rows)
        exp_p = sum(r["p"] for r in rows) / len(rows)
        print(f"  Net P&L: {total:+.2f}$ | {wins}W/{len(rows)-wins}L "
              f"({wins/len(rows)*100:.0f}% actual vs {exp_p*100:.0f}% predicted)")
        print("\n  -- calibration (the edge test) --")
        calibration(rows)
        print("\n  -- by city --")
        by_key(rows, "city")
        print("\n  -- by hi/lo --")
        by_key(rows, "hl")
        print("\n  -- by side --")
        by_key(rows, "side")
        # suggestions
        print("\n  -- suggestions --")
        if len(rows) < 30:
            print(f"  Sample too small ({len(rows)} bets) for tuning decisions.")
            print("  Rule of thumb: 30+ to spot gross miscalibration, 100+ to tune.")
        else:
            gap = wins / len(rows) - exp_p
            if gap < -0.10:
                print(f"  Predicted {exp_p*100:.0f}% but won {wins/len(rows)*100:.0f}% "
                      "- model is OVERCONFIDENT. Widen sigma_for_lead or raise min_edge.")
            elif gap > 0.05:
                print("  Winning more than predicted - edge is real; consider "
                      "sizing up (raise Kelly cap) before loosening filters.")
            else:
                print("  Roughly calibrated. Judge by net P&L after fees.")


if __name__ == "__main__":
    sys.exit(main())
