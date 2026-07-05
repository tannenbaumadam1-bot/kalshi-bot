#!/usr/bin/env python3
"""Honest compound-returns model: Polymarket reward-farming vs Kalshi weather edge.

NOT a promise - a transparent model with stated assumptions, so the two paths
can be compared on the same axes. Reality will deviate (variance, dilution,
program changes). Run: python3 compound_model.py

Key structural facts baked in (researched 2026-07):
  * Polymarket LP rewards: ~$5M/mo pool; disciplined makers report ~40-120% APY
    on deployed capital AFTER adverse selection (promotional sources - treat the
    high end with skepticism). Rewards DILUTE as your capital grows (fixed pool
    per market, more competition), so effective APY falls with size.
  * Kalshi weather edge: a per-bet edge (IF real - currently UNPROVEN and at the
    calibration gate) compounds fast, BUT weather markets are THIN: the whole
    book absorbs only a few hundred dollars before you run out of edges / move
    price. So it's HIGH-% but LOW-CAPACITY: great for $100, useless for $5k.
"""
from __future__ import annotations

def grow_polymarket(capital, base_apy, days=365):
    """Day-by-day compounding; effective APY shrinks as bankroll grows (dilution)."""
    bank = capital
    path = {}
    for d in range(1, days + 1):
        haircut = 1.0 / (1.0 + bank / 7000.0)      # dilution as size grows
        apy_eff = base_apy * haircut
        r_daily = (1 + apy_eff) ** (1 / 365.0) - 1
        bank *= (1 + r_daily)
        if d in (1, 7, 30, 90, 365):
            path[d] = bank
    return path


def grow_weather(capital, edge, days=365, book_frac=0.30, cap_dollars=400.0):
    """Weather edge compounding with a hard CAPACITY ceiling (thin markets).
    Only min(bankroll, cap) is deployable; the rest sits idle earning nothing."""
    bank = capital
    path = {}
    for d in range(1, days + 1):
        deployable = min(bank, cap_dollars)
        daily_ret = (deployable / bank) * book_frac * edge   # EV/day on total bankroll
        bank *= (1 + daily_ret)
        if d in (1, 7, 30, 90, 365):
            path[d] = bank
    return path


def pct(a, b):
    return 100 * (a - b) / b


def table(title, rows, capitals):
    print("\n" + title)
    print("%-10s %10s %10s %10s %10s %12s" % ("capital", "day 1", "week", "month", "quarter", "1 year"))
    for cap in capitals:
        p = rows[cap]
        print("%-10s %10.2f %10.2f %10.2f %10.2f %12.2f  (%+.0f%%)" %
              ("$%d" % cap, p[1], p[7], p[30], p[90], p[365], pct(p[365], cap)))


def main():
    capitals = [100, 500, 1000, 5000]

    print("=" * 74)
    print("POLYMARKET REWARD FARMING  (net APY after adverse selection; dilutes with size)")
    print("=" * 74)
    for label, apy in [("Pessimistic (10% APY)", 0.10),
                       ("Base (35% APY)", 0.35),
                       ("Optimistic (80% APY)", 0.80)]:
        rows = {c: grow_polymarket(c, apy) for c in capitals}
        table(label, rows, capitals)

    print("\n" + "=" * 74)
    print("KALSHI WEATHER EDGE  (IF the edge is real - UNPROVEN; ~$400 capacity ceiling)")
    print("=" * 74)
    for label, edge in [("Weak edge (2%/bet)", 0.02),
                        ("Base edge (3%/bet)", 0.03),
                        ("Strong edge (5%/bet)", 0.05)]:
        rows = {c: grow_weather(c, edge) for c in capitals}
        table(label, rows, capitals)

    print("\n" + "=" * 74)
    print("SIDE-BY-SIDE: 1-year value by starting capital (base scenarios)")
    print("=" * 74)
    print("%-10s %22s %22s" % ("capital", "Polymarket (35% APY)", "Weather (3% edge, capped)"))
    for c in capitals:
        pm = grow_polymarket(c, 0.35)[365]
        wx = grow_weather(c, 0.03)[365]
        print("%-10s %18.0f %6s %18.0f" % ("$%d" % c, pm, "", wx))
    print("\nRead: weather's % is huge at $100 (it blows past its own ceiling fast) but")
    print("stalls - at $5k almost all capital sits idle. Reward farming scales with")
    print("capital but at a lower, diluting rate. Neither is proven for YOU yet.")


if __name__ == "__main__":
    main()
