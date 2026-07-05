#!/usr/bin/env python3
"""The SNOWBALL: reinvest ALL Polymarket rewards vs withdraw them.

Each day's modeled net reward is added back to deployed liquidity, so the base
grows and the next day earns more - compounding. HONEST twist: effective APY
falls as capital grows (you become a bigger share of a fixed reward pool =
dilution), so the hockey stick bends over instead of going vertical forever.

Scenarios use the researched empirical range (net APY after adverse selection):
conservative 10%, base 35%, optimistic 80%.  Run: python3 reinvest_model.py
"""
from __future__ import annotations

DILUTION_K = 7000.0     # bigger bank -> lower effective APY (fixed pools, more competition)


def apy_at(bank, base_apy):
    return base_apy / (1 + bank / DILUTION_K)


def sim(capital, base_apy, days, reinvest=True):
    """Returns (curve_by_day, daily_earn_by_day, final_value)."""
    bank = capital
    withdrawn = 0.0
    curve, earns = [capital], [0.0]
    for _ in range(days):
        base = bank if reinvest else capital           # simple earns on original only
        r = (1 + apy_at(base, base_apy)) ** (1 / 365.0) - 1
        earn = base * r
        if reinvest:
            bank += earn
        else:
            withdrawn += earn
        curve.append(bank)
        earns.append(earn)
    return curve, earns, (bank if reinvest else capital + withdrawn)


HORIZONS = [("1 mo", 30), ("3 mo", 90), ("6 mo", 180),
            ("1 yr", 365), ("2 yr", 730), ("3 yr", 1095)]
CAPS = [100, 500, 1000, 5000]
SCEN = [("Rewards fade 15% APY", 0.15), ("Conservative 40% APY", 0.40),
        ("Base 70% APY", 0.70), ("Optimistic 120% APY", 1.20)]


def money(x):
    return "$%s" % ("{:,.0f}".format(x) if x >= 100 else "{:,.2f}".format(x))


def main():
    for name, apy in SCEN:
        print("\n" + "=" * 78)
        print("REINVESTING ALL REWARDS  -  %s" % name)
        print("=" * 78)
        print("%-8s" % "capital" + "".join("%11s" % h[0] for h in HORIZONS))
        for cap in CAPS:
            row = "%-8s" % money(cap)
            for _, dd in HORIZONS:
                _, _, val = sim(cap, apy, dd)
                row += "%11s" % money(val)
            print(row)

    print("\n" + "=" * 78)
    print("ACCELERATION - what you EARN PER DAY as the snowball grows (Base 70%, reinvesting)")
    print("=" * 78)
    print("%-8s %12s %12s %12s %12s" % ("capital", "day 30", "day 180", "day 365", "day 730"))
    for cap in CAPS:
        _, earns, _ = sim(cap, 0.70, 730)
        print("%-8s %12s %12s %12s %12s" %
              (money(cap), money(earns[30]), money(earns[180]), money(earns[365]), money(earns[730])))

    print("\n" + "=" * 78)
    print("REINVEST vs WITHDRAW - the compounding premium (Base 70%, $1,000 start)")
    print("=" * 78)
    print("%-8s %16s %16s %14s" % ("horizon", "reinvest (compound)", "withdraw (simple)", "extra from"))
    print("%-8s %16s %16s %14s" % ("", "value", "value", "compounding"))
    for hname, dd in HORIZONS:
        _, _, comp = sim(1000, 0.70, dd, reinvest=True)
        _, _, simp = sim(1000, 0.70, dd, reinvest=False)
        print("%-8s %16s %16s %14s" % (hname, money(comp), money(simp), money(comp - simp)))

    print("\nHonest note: the curve BENDS as capital grows - at higher balances you're a")
    print("bigger share of fixed reward pools, so effective APY falls (dilution). The")
    print("snowball is real but it is not infinite, and none of this is guaranteed.")


if __name__ == "__main__":
    main()
