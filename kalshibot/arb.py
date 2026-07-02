"""Logical-arbitrage detection (READ-ONLY) - pure functions over markets.

For a mutually-exclusive, exhaustive set of markets (one event, exactly one
outcome resolves YES), the YES prices should sum to ~$1.00. When they don't:
  * sum of YES asks < $1  -> buy every YES cheap = profit ("underround")
  * sum of YES bids > $1  -> sell every YES dear = profit ("overround")
Net figures include estimated taker fees. These are CANDIDATES to verify, not
guaranteed money - grouping by event does not prove mutual exclusivity.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from .fees import fee_cents


def _cents(v) -> int:
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return 0


def find_arbs(markets: List[Dict], min_markets: int = 2,
              min_net_cents: int = 1) -> Dict[str, list]:
    events = defaultdict(list)
    for mk in markets:
        ev = mk.get("event_ticker") or mk.get("event") or "?"
        events[ev].append((_cents(mk.get("yes_bid_dollars")),
                           _cents(mk.get("yes_ask_dollars"))))

    under, over = [], []
    for ev, legs in events.items():
        if len(legs) < min_markets:
            continue
        asks = [a for _, a in legs]
        bids = [b for b, _ in legs]
        if all(a > 0 for a in asks):
            cost = sum(asks)
            fees = sum(fee_cents(a, 1, taker=True) for a in asks)
            net = (100 - cost) - fees
            if net >= min_net_cents:
                under.append((net, ev, len(legs), cost, fees))
        if all(b > 0 for b in bids):
            proceeds = sum(bids)
            fees = sum(fee_cents(b, 1, taker=True) for b in bids)
            net = (proceeds - 100) - fees
            if net >= min_net_cents:
                over.append((net, ev, len(legs), proceeds, fees))

    under.sort(reverse=True)
    over.sort(reverse=True)
    return {"under": under, "over": over}


def plan_basket_buy(legs, qty: int = 1, min_net_cents: int = 1):
    """Decide whether to fire a buy-all-YES arb on a VERIFIED one-winner set.

    legs: list of (ticker, ask_cents, ask_size). Returns the net profit in
    cents if every leg is fillable at qty and the basket clears fees with at
    least min_net_cents edge; otherwise None. Caller must have already
    confirmed the event is mutually_exclusive (exactly one leg resolves YES).
    """
    if not legs:
        return None
    for _, ask, size in legs:
        if ask <= 0 or size < qty:
            return None   # a leg can't be bought / not enough depth -> abort
    cost = sum(ask for _, ask, _ in legs) * qty
    fees = sum(fee_cents(ask, qty, taker=True) for _, ask, _ in legs)
    net = 100 * qty - cost - fees   # exactly one leg pays 100c per contract
    return net if net >= min_net_cents else None


def plan_basket_sell(legs, qty: int = 1, min_net_cents: int = 1):
    """Overround: SELL YES on every leg of a verified one-winner set.
    legs: list of (ticker, bid_cents, bid_size). You receive the bids now;
    at resolution exactly one YES wins so you pay 100c total. Profit if the
    bids sum to > 100c after fees. Returns net cents or None.
    """
    if not legs:
        return None
    for _, bid, size in legs:
        if bid <= 0 or size < qty:
            return None
    proceeds = sum(bid for _, bid, _ in legs) * qty
    fees = sum(fee_cents(bid, qty, taker=True) for _, bid, _ in legs)
    net = proceeds - 100 * qty - fees
    return net if net >= min_net_cents else None


def reconcile_fills(fills):
    """Given how many contracts actually filled on each leg, work out how many
    COMPLETE baskets we hold and which excess to flatten.

    fills: dict ticker -> filled_qty.
    Returns (m, excess) where m = complete baskets (min fill across legs) and
    excess = {ticker: qty_to_flatten} for everything above m. If any leg filled
    0, m is 0 and every filled leg is flattened (-> back to flat, no naked bet).
    """
    if not fills:
        return 0, {}
    m = min(fills.values())
    excess = {t: f - m for t, f in fills.items() if f - m > 0}
    return m, excess
