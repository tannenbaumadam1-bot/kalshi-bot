#!/usr/bin/env python3
"""Report card - a plain-English summary of how the demo bot is doing.

Reads the bot's diary (logs/trades.csv) and checks your live demo balance,
then prints an easy-to-read summary. Run it any time:  python report.py
"""

from __future__ import annotations

import csv
import os
from collections import Counter

from kalshibot.config import load_config
from kalshibot.client import KalshiClient, KalshiError

LOG = "logs/trades.csv"
START_CENTS = 10000  # demo accounts start with $100.00


def _read_log():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="") as f:
        return list(csv.DictReader(f))


def _fees_cents(rows):
    total = 0
    for r in rows:
        try:
            total += int(r.get("est_fee_cents") or 0)
        except (TypeError, ValueError):
            pass
    return total


def main() -> int:
    rows = _read_log()
    sent = [r for r in rows if r.get("event") == "order_sent"]
    dry = [r for r in rows if r.get("event") == "dry_run_order"]
    blocked = [r for r in rows if r.get("event") == "blocked"]
    errors = [r for r in rows if r.get("event") == "error"]
    days = sorted({(r.get("timestamp") or "")[:10] for r in rows if r.get("timestamp")})

    print("=" * 50)
    print("            KALSHI BOT - REPORT CARD")
    print("=" * 50)

    # ----- live money picture -----
    total_now = None
    is_live = False
    try:
        cfg = load_config()
        is_live = cfg.is_live
        cl = KalshiClient(cfg.key_id, cfg.private_key_path, cfg.base_url)
        bal = cl._request("GET", "/portfolio/balance")
        cash = int(bal.get("balance", 0) or 0)
        in_play = int(bal.get("portfolio_value", 0) or 0)
        total_now = cash + in_play
        positions = [p for p in cl.get_positions()
                     if _pos_count(p) != 0]

        env = "REAL MONEY" if is_live else "demo (fake money)"
        print(f"\nAccount:            {env}")
        print(f"Cash on hand:       ${cash/100:,.2f}")
        print(f"Tied up in bets:    ${in_play/100:,.2f}")
        print(f"TOTAL worth now:    ${total_now/100:,.2f}")
        if not is_live:
            print(f"Started with:       ${START_CENTS/100:,.2f}")
            diff = total_now - START_CENTS
            word = "UP" if diff >= 0 else "DOWN"
            print(f"Net change:         {word} ${abs(diff)/100:,.2f}")
        print(f"Open bets right now:{len(positions):>4}")
    except KalshiError as e:
        print(f"\n(Couldn't reach Kalshi to read your balance: {e})")
    except (FileNotFoundError, ValueError) as e:
        print(f"\n(Config problem: {e})")

    # ----- what the bot did -----
    print("\n--- What the bot has done (from its diary) ---")
    if not rows:
        print("Nothing yet - the bot hasn't run, or hasn't traded.")
    else:
        span = f"{days[0]} to {days[-1]}" if days else "n/a"
        print(f"Days active:        {len(days)}  ({span})")
        print(f"Real orders placed: {len(sent)}")
        print(f"Practice picks:     {len(dry)}   (dry-run, nothing sent)")
        print(f"Skipped (too risky):{len(blocked):>4}")
        if errors:
            print(f"Errors:             {len(errors)}")
        print(f"Fees paid (est.):   ${_fees_cents(sent)/100:,.2f}")

    # ----- plain-English verdict -----
    print("\n--- Plain-English verdict ---")
    if total_now is None:
        print("Couldn't read your balance, so no scorecard yet.")
    elif len(sent) == 0 and len(dry) == 0:
        print("The bot hasn't placed anything yet. Let it run a while.")
    elif len(sent) == 0:
        print("Still in practice mode (dry run). When you run "
              "4_trade_on_demo.bat, it will place real demo orders.")
    else:
        diff = total_now - START_CENTS
        if diff > 0:
            print(f"So far so good - up ${diff/100:,.2f} on fake money.")
            print("Keep letting it run; one good stretch isn't proof yet.")
        elif diff == 0:
            print("Break-even so far. Needs more time to tell.")
        else:
            print(f"Down ${abs(diff)/100:,.2f} on fake money so far.")
            print("This is exactly why we test on demo first. If it keeps "
                  "slipping, we adjust the strategy before any real money.")
    print("=" * 50)
    return 0


def _pos_count(p):
    raw = p.get("position", p.get("position_fp", 0))
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
