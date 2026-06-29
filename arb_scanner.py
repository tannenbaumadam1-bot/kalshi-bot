#!/usr/bin/env python3
"""Standalone arb scanner (READ-ONLY). Same logic the bot uses each cycle.
Run anytime:  python arb_scanner.py
"""
from __future__ import annotations

from kalshibot.config import load_config
from kalshibot.client import KalshiClient
from kalshibot.arb import find_arbs


def main():
    cfg = load_config()
    cl = KalshiClient(cfg.key_id, cfg.private_key_path, cfg.base_url)
    markets = []
    cursor = None
    for _ in range(20):
        d = cl.get_markets(limit=200, status="open", cursor=cursor)
        markets += d.get("markets", []) or []
        cursor = d.get("cursor")
        if not cursor:
            break
    res = find_arbs(markets)
    print("=" * 58)
    print("   KALSHI LOGICAL-ARB SCANNER (read-only, places nothing)")
    print("=" * 58)
    print(f"Scanned {len(markets)} open markets.\n")
    print(f"UNDERROUND (buy every YES < $1): {len(res['under'])} candidate(s)")
    for net, ev, n, cost, fees in res["under"][:10]:
        print(f"  +{net}c net | {ev} | {n} legs | buy-all costs {cost}c, fees ~{fees}c")
    print(f"\nOVERROUND (sell every YES > $1): {len(res['over'])} candidate(s)")
    for net, ev, n, proc, fees in res["over"][:10]:
        print(f"  +{net}c net | {ev} | {n} legs | sell-all gets {proc}c, fees ~{fees}c")
    if not res["under"] and not res["over"]:
        print("\n(no logical-arb candidates right now - demo books are thin)")
    print("\nNOTE: candidates only. Verify the event is a true one-winner set")
    print("and every leg has depth before trusting one.")


if __name__ == "__main__":
    main()
