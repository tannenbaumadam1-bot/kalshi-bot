"""Example signal strategy: simple price momentum.

This is a deliberately simple ILLUSTRATION of how a signal-driven
strategy plugs into the same framework. It tracks the recent mid-price
of each market and, if YES has been rising over the lookback window,
buys a small amount of YES (expecting the move to continue); when it
already holds YES and momentum fades, it exits.

Momentum on its own is NOT a proven edge on Kalshi - this exists so you
can see how to wire real signals (sports feeds, crypto prices, model
outputs) in later. Keep it on demo.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Deque, Dict, List

from .base import MarketSnapshot, OrderIntent, Strategy


class MomentumStrategy(Strategy):
    name = "momentum"

    def __init__(self, params: Dict[str, Any]):
        super().__init__(params)
        self.lookback = int(params.get("lookback_cycles", 5))
        self.order_size = int(params.get("order_size", 1))
        self.min_move_cents = int(params.get("min_move_cents", 2))
        self._history: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.lookback)
        )

    def decide(self, snap: MarketSnapshot) -> List[OrderIntent]:
        hist = self._history[snap.ticker]
        hist.append(snap.yes_mid)

        if len(hist) < self.lookback:
            return []  # not enough data yet

        move = hist[-1] - hist[0]
        pos = snap.position

        # rising and flat -> buy a little YES at the ask (taker)
        if pos.is_flat and move >= self.min_move_cents:
            if snap.yes_ask <= 0:
                return []
            return [
                OrderIntent(
                    ticker=snap.ticker,
                    action="buy",
                    side="yes",
                    count=self.order_size,
                    price_cents=snap.yes_ask,
                    reason=f"momentum up {move:.1f}c over {self.lookback} cycles",
                )
            ]

        # holding YES and momentum gone -> exit at the bid
        if not pos.is_flat and pos.side == "yes" and move <= 0:
            if snap.yes_bid <= 0:
                return []
            return [
                OrderIntent(
                    ticker=snap.ticker,
                    action="sell",
                    side="yes",
                    count=pos.count,
                    price_cents=snap.yes_bid,
                    reason=f"momentum faded ({move:.1f}c), exit",
                )
            ]

        return []
