#!/usr/bin/env python3
"""Kalshi Bot - command line entry point.

Usage:
  python run.py check            Test your connection and show your balance
  python run.py once             Run ONE cycle as a DRY RUN (places no orders)
  python run.py once --execute   Run one cycle and actually place orders
  python run.py run              Loop forever as a DRY RUN
  python run.py run --execute    Loop forever and actually place orders

Options:
  --config=FILE   Use an alternate config file (default: config.yaml)

Safety:
  * Without --execute, the bot NEVER sends an order; it only prints and
    logs what it *would* do. Always start here.
  * --execute against a 'demo' config trades fake money (safe).
  * --execute against a 'live' config trades REAL money and additionally
    requires the --i-understand-live flag.
"""

from __future__ import annotations

import sys

from kalshibot.client import KalshiClient, KalshiError
from kalshibot.config import load_config
from kalshibot.engine import Engine, parse_orderbook


def cmd_check(cfg, client) -> int:
    print(f"Environment: {cfg.environment}  ({cfg.base_url})")
    try:
        balance = client.get_balance_cents()
    except KalshiError as e:
        print(f"\nConnection FAILED: {e}")
        return 1
    print(f"Connected. Balance: ${balance/100:.2f}")

    try:
        data = client.get_markets(limit=5, status="open")
        markets = data.get("markets", [])
        print(f"\nSample open markets ({len(markets)} shown):")
        for mk in markets:
            print(f"  {mk.get('ticker','?'):<28} "
                  f"yes_bid={mk.get('yes_bid','?')}c "
                  f"yes_ask={mk.get('yes_ask','?')}c "
                  f"vol={mk.get('volume','?')}")
    except KalshiError as e:
        print(f"Could not list markets: {e}")
        return 1
    print("\nLooks good. Next: python run.py once   (dry run, places nothing)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    command = args[0]
    execute = "--execute" in args
    confirm_live = "--i-understand-live" in args

    # Optional alternate config:  --config=somefile.yaml
    config_path = "config.yaml"
    for a in args:
        if a.startswith("--config="):
            config_path = a.split("=", 1)[1]

    try:
        cfg = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Config problem: {e}")
        return 1

    # Live-trading safety gate
    if execute and cfg.is_live and not confirm_live:
        print("REFUSING to place real-money orders.\n"
              "Your config has environment: live and you passed --execute.\n"
              "If you really mean it, add --i-understand-live as well.\n"
              "Strongly recommended: prove the bot on demo first.")
        return 1

    try:
        client = KalshiClient(cfg.key_id, cfg.private_key_path, cfg.base_url)
    except KalshiError as e:
        print(f"Could not start client: {e}")
        return 1

    if command == "check":
        return cmd_check(cfg, client)

    if command in ("once", "run"):
        engine = Engine(cfg, client, dry_run=not execute)
        mode = "LIVE EXECUTION" if execute else "DRY RUN (no orders sent)"
        env = "REAL MONEY" if cfg.is_live else "demo / fake money"
        print(f"Mode: {mode} | {env}\n")
        if command == "once":
            engine.run_cycle()
        else:
            engine.run_forever()
        return 0

    print(f"Unknown command '{command}'. Run 'python run.py --help'.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
