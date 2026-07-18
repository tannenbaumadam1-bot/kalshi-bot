#!/usr/bin/env python3
"""Polymarket READ-ONLY client - market data, order books, reward configs.

No keys, no orders, nothing that can spend money. Pulls:
  - rewarded markets + their reward config (daily pool, min size, max spread)
    from the public Gamma API
  - live order books from the public CLOB API
  - a helper to estimate the COMPETING qualifying liquidity near mid (the
    liquidity we'd have to share the reward pool with)

This is step 1 of a possible Polymarket expansion; a paper simulator
(poly_paper.py) uses it to estimate reward-farming P&L BEFORE any real USDC.
"""
from __future__ import annotations
import json
import requests

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def reward_markets(limit=200, min_pool=1.0):
    """Active markets that currently pay LP rewards, richest-volume first."""
    try:
        ms = requests.get(GAMMA, params={
            "active": "true", "closed": "false", "limit": limit,
            "order": "volume24hr", "ascending": "false"}, timeout=20).json()
    except Exception:
        return []
    out = []
    for m in ms:
        pool = 0.0
        for r in (m.get("clobRewards") or []):
            pool = max(pool, _f(r.get("rewardsDailyRate")))
        if pool < min_pool:
            continue
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            toks = []
        bb, ba = _f(m.get("bestBid")), _f(m.get("bestAsk"))
        out.append({
            "q": (m.get("question") or "")[:60],
            "condition_id": m.get("conditionId"),
            "tokens": toks,
            "pool_daily": pool,                       # USDC/day (VALIDATE units vs first payout)
            "min_size": _f(m.get("rewardsMinSize")),  # min qualifying order size (shares)
            "max_spread_c": _f(m.get("rewardsMaxSpread")),  # cents from mid to qualify
            "best_bid": bb, "best_ask": ba,
            "mid": round((bb + ba) / 2.0, 4) if (bb and ba) else None,
            "spread_c": round(_f(m.get("spread")) * 100, 2),
            "vol24": _f(m.get("volume24hr")),
        })
    return out


def book(token_id):
    """(bids, asks) as lists of (price, size); empty on failure."""
    try:
        b = requests.get(CLOB + "/book", params={"token_id": token_id}, timeout=15).json()
        bids = [(_f(x.get("price")), _f(x.get("size"))) for x in b.get("bids", []) or []]
        asks = [(_f(x.get("price")), _f(x.get("size"))) for x in b.get("asks", []) or []]
        return bids, asks
    except Exception:
        return [], []


def qualifying_liquidity(bids, asks, mid, max_spread_c):
    """Competing size (shares) resting within the reward band of the midpoint -
    the liquidity we'd split the reward pool with."""
    if mid is None:
        return 0.0
    band = max_spread_c / 100.0
    liq = sum(s for p, s in bids if 0 <= (mid - p) <= band)
    liq += sum(s for p, s in asks if 0 <= (p - mid) <= band)
    return liq


def market_competition(mkt):
    """Total competing qualifying liquidity (shares) across the market's tokens."""
    total = 0.0
    for tok in mkt.get("tokens", []):
        bids, asks = book(tok)
        total += qualifying_liquidity(bids, asks, mkt.get("mid"), mkt.get("max_spread_c", 1.5))
    return total


def _demo():
    ms = reward_markets()
    print("Rewarded Polymarket markets (top by volume): %d\n" % len(ms))
    print("%-46s %9s %8s %7s %8s" % ("market", "pool/day", "minSize", "maxSpr", "spread"))
    for m in ms[:12]:
        print("%-46s %9.0f %8.0f %6.1fc %6.2fc" % (
            m["q"][:46], m["pool_daily"], m["min_size"], m["max_spread_c"], m["spread_c"]))
    if ms:
        m = ms[0]
        comp = market_competition(m)
        print("\nTop market '%s':" % m["q"][:40])
        print("  reward pool ~%.0f/day, competing qualifying liquidity ~%.0f shares near mid %.2f"
              % (m["pool_daily"], comp, m["mid"] or 0))
        print("  -> $500 of our liquidity would be ~%.2f%% of the qualifying pool"
              % (100 * (500 / (m["mid"] or 0.5)) / max(1.0, comp + (500 / (m["mid"] or 0.5)))))


if __name__ == "__main__":
    _demo()
