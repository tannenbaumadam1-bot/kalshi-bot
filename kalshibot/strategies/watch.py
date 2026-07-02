"""Watch mode: a DEMO-ONLY strategy that forces visible fills.

It crosses the spread on purpose - buys at the current ask (fills right
away), then sells at the bid to close - so you can watch a full
buy -> hold -> sell cycle happen on screen and on your Kalshi Portfolio.

This is NOT a money-making strategy. Paying the spread loses a little each
round trip; it exists only to prove the machinery works end to end. The
daily-loss limit in config.yaml caps how much it can bleed.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import MarketSnapshot, OrderIntent, Strategy


class WatchStrategy(Strategy):
    name = "watch"

    def __init__(self, params: Dict[str, Any]):
        super().__init__(params)
        self.order_size = int(params.get("order_size", 1))

    def decide(self, snap: MarketSnapshot) -> List[OrderIntent]:
        pos = snap.position
        # holding YES -> close it by selling into the bid (fills now)
        if not pos.is_flat and pos.side == "yes":
            if snap.yes_bid <= 0:
                return []
            return [OrderIntent(
                ticker=snap.ticker, action="sell", side="yes",
                count=pos.count, price_cents=snap.yes_bid,
                order_type="market",
                reason=f"watch: close at bid {snap.yes_bid}c",
            )]
        # flat -> buy YES at the ask (fills now)
        if pos.is_flat:
            if snap.yes_ask <= 0:
                return []
            return [OrderIntent(
                ticker=snap.ticker, action="buy", side="yes",
                count=self.order_size, price_cents=snap.yes_ask,
                order_type="market",
                reason=f"watch: take ask {snap.yes_ask}c",
            )]
        return []
