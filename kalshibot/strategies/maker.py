"""Conservative maker / spread-capture strategy (the default).

Idea, in plain English:
  * If we hold nothing in a market and its bid-ask spread is wide
    enough to beat fees, place a resting BUY order for YES just above
    the current best bid (a "maker" order, which pays the low maker
    fee). We are trying to buy a contract cheaply.
  * If we already hold YES, place a resting SELL order a few cents
    above what we paid - enough to clear fees on both legs and book a
    small profit.

This never shorts and never chases price, so the worst case in a single
market is bounded by the per-market risk cap. It is intentionally simple
and easy to read; treat it as a starting point, not a money printer.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..fees import fee_cents, min_profitable_spread_cents
from .base import MarketSnapshot, OrderIntent, Strategy


class MakerStrategy(Strategy):
    name = "maker"

    def __init__(self, params: Dict[str, Any]):
        super().__init__(params)
        self.edge_cents = int(params.get("edge_cents", 1))
        self.order_size = int(params.get("order_size", 1))

    def decide(self, snap: MarketSnapshot) -> List[OrderIntent]:
        pos = snap.position

        # --- holding YES: try to exit at a small, fee-aware profit ---
        if not pos.is_flat and pos.side == "yes":
            # price that clears entry fee + exit fee + 1c profit
            costs = (
                fee_cents(pos.avg_price_cents, pos.count, taker=False)
                + fee_cents(pos.avg_price_cents + 2, pos.count, taker=False)
            )
            target = pos.avg_price_cents + max(2, (costs // max(1, pos.count)) + 1)
            target = min(99, target)
            # Only post the exit if it is at or above the current bid side,
            # so it rests as a maker order rather than crossing.
            target = max(target, snap.yes_bid + self.edge_cents)
            return [
                OrderIntent(
                    ticker=snap.ticker,
                    action="sell",
                    side="yes",
                    count=pos.count,
                    price_cents=target,
                    reason=f"exit yes @ {target}c (entry {pos.avg_price_cents}c)",
                )
            ]

        # --- flat: only enter when the spread can pay for itself -------
        if pos.is_flat:
            # need a real two-sided book
            if snap.yes_bid <= 0 or snap.yes_ask <= 0:
                return []
            needed = min_profitable_spread_cents(snap.yes_bid, taker=False)
            if snap.yes_spread < needed:
                return []
            entry = snap.yes_bid + self.edge_cents
            if entry >= snap.yes_ask:  # don't cross the spread (stay a maker)
                return []
            return [
                OrderIntent(
                    ticker=snap.ticker,
                    action="buy",
                    side="yes",
                    count=self.order_size,
                    price_cents=entry,
                    reason=(
                        f"enter yes @ {entry}c "
                        f"(spread {snap.yes_spread}c >= needed {needed}c)"
                    ),
                )
            ]

        return []
