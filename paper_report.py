#!/usr/bin/env python3
"""Read the paper-trading logs and print a performance scorecard PLUS the
full trade-by-trade history. Safe to run anytime - it only reads files.
"""
from __future__ import annotations

import csv
import os
import datetime

LOG_PATH = os.path.join("logs", "paper_pnl.csv")
TRADES_PATH = os.path.join("logs", "paper_trades.csv")
STATE_PATH = os.path.join("logs", "paper_state.json")


def _num(x, cast=float, default=0.0):
    try:
        return cast(x)
    except (TypeError, ValueError):
        return default


def scorecard():
    if not os.path.exists(LOG_PATH):
        print("No paper-trading log yet. Run 9_paper_live.bat first.")
        return False
    rows = []
    with open(LOG_PATH) as f:
        for r in csv.reader(f):
            if not r or r[0] == "timestamp":
                continue
            rows.append(r)
    if not rows:
        print("Log exists but has no data yet. Let the bot run longer.")
        return False

    last, first = rows[-1], rows[0]
    cycle = _num(last[1], int, 0); cands = _num(last[2], int, 0)
    open_pos = _num(last[4], int, 0); rt = _num(last[5], int, 0)
    wins = _num(last[6], int, 0); losses = _num(last[7], int, 0)
    realized = _num(last[8]); unreal = _num(last[9]); total = _num(last[10]); fees = _num(last[11])
    totals = [_num(r[10]) for r in rows]
    peak, trough = max(totals), min(totals)
    span = ""
    try:
        mins = (datetime.datetime.fromisoformat(last[0]) -
                datetime.datetime.fromisoformat(first[0])).total_seconds() / 60
        span = f"{mins/60:.1f} hours" if mins >= 90 else f"{mins:.0f} min"
    except Exception:
        pass
    wr = (100 * wins / rt) if rt else 0
    avg = (realized / rt) if rt else 0
    arrow = "UP" if total > 0 else ("DOWN" if total < 0 else "flat")
    start = 100.0
    try:
        import json as _json
        if os.path.exists(STATE_PATH):
            start = _json.load(open(STATE_PATH)).get("start", 100.0)
    except Exception:
        pass
    current = start + total

    print("=" * 60)
    print("   PAPER TRADING PERFORMANCE  (live data, no money)")
    print("=" * 60)
    print(f"  Running for : {span or 'a bit'}  ({cycle} cycles)")
    print(f"  Balance     : started ${start:.2f}  ->  now ${current:.2f}")
    print("")
    rlab = f"REALIZED   (banked from {rt} closed trades)"
    ulab = f"UNREALIZED (open, {open_pos} positions marked)"
    clab = "COMBINED   (realized + unrealized)"
    w = max(len(rlab), len(ulab), len(clab))
    print("  ---------------- GAINS BREAKDOWN ----------------")
    print(f"  {rlab:<{w}} : ${realized:+8.2f}")
    print(f"  {ulab:<{w}} : ${unreal:+8.2f}")
    print("  " + "-" * (w + 12))
    print(f"  {clab:<{w}} : ${total:+8.2f}   [{arrow}]")
    print("")
    print(f"  Win rate    : {wins}W / {losses}L ({wr:.0f}%)")
    print(f"  Avg / trade : ${avg:+.2f}      Fees paid: ${fees:.2f}")
    print(f"  Best / worst combined so far: ${peak:+.2f} / ${trough:+.2f}")
    print("=" * 60)
    return True


def all_trades():
    if not os.path.exists(TRADES_PATH):
        print("\nNo individual trades recorded yet.")
        print("(Restart 9_paper_live.bat to enable per-trade logging, then")
        print(" wait for orders to fill.)")
        return
    rows = []
    with open(TRADES_PATH) as f:
        for r in csv.reader(f):
            if not r or r[0] == "timestamp":
                continue
            rows.append(r)
    if not rows:
        print("\nNo trades filled yet - orders are still resting.")
        return

    print(f"\nEVERY TRADE  ({len(rows)} fills, newest at bottom)")
    print("-" * 72)
    print(f"  {'#':>3}  {'time':8}  {'act':4} {'type':5} {'contracts':>10}  "
          f"{'fee':>5}  {'P&L':>8}  ticker")
    print("-" * 72)
    running = 0.0
    for i, r in enumerate(rows, 1):
        # ts,ticker,action,type,count,price_c,fee_c,entry_c,trade_pnl_$
        ts = r[0][11:19] if len(r[0]) >= 19 else r[0]
        ticker, action, typ = r[1], r[2], r[3]
        count, price, fee = r[4], r[5], r[6]
        pnl = r[8] if len(r) > 8 else ""
        pnl_txt = ""
        if pnl not in ("", None):
            running += _num(pnl)
            pnl_txt = f"${_num(pnl):+.2f}"
        print(f"  {i:>3}  {ts:8}  {action:4} {typ:5} {count:>4} @ {price:>3}c  "
              f"{fee:>4}c  {pnl_txt:>8}  {ticker[:30]}")
    print("-" * 72)
    print(f"  Sum of realized trade P&L: ${running:+.2f}")
    print("  (Simulation on real prices. Nothing was ever placed.)")


def main():
    ok = scorecard()
    if ok:
        all_trades()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
