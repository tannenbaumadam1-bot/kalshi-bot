"""Smart strategy: fee-aware spread capture, gated by momentum, with a stop-loss,
PLUS a disciplined momentum entry.

This is the higher-quality default. In plain terms:

  ENTRY (when flat), in priority order:
    A) SPREAD CAPTURE (maker) - in markets whose spread beats fees, and only
       if the price is NOT falling hard, post a resting buy 1c above the bid.
       This EARNS the spread and pays the lower maker fee.
    B) MOMENTUM (taker) - if there is no maker entry but the price is rising
       hard, buy at the ask to ride the move - but ONLY when the size of the
       up-move is bigger than the spread + round-trip fees we'd pay to cross.
       If the move can't clear that cost, we skip it. This is what keeps
       momentum from just bleeding to fees.

  EXIT (when holding YES), in priority order:
    1. STOP-LOSS - if the bid dropped stop_loss_cents below entry, sell now.
    2. MOMENTUM CUT - if the price is sliding hard, exit at the bid.
    3. TAKE-PROFIT - otherwise rest a sell above entry, fee-aware.

Still a heuristic, not a proven edge - but it manages downside, earns the
spread where it can, and only chases momentum when the move pays for itself.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Deque, Dict, List

from ..fees import fee_cents, min_profitable_spread_cents
from .base import MarketSnapshot, OrderIntent, Strategy


class SmartStrategy(Strategy):
    name = "smart"

    def __init__(self, params: Dict[str, Any]):
        super().__init__(params)
        self.lookback = int(params.get("lookback_cycles", 4))
        self.order_size = int(params.get("order_size", 1))
        self.edge_cents = int(params.get("edge_cents", 1))
        self.stop_loss_cents = int(params.get("stop_loss_cents", 8))
        # don't enter (maker) if the price fell more than this over the window
        self.max_down_trend_cents = int(params.get("max_down_trend_cents", 3))
        # exit a holding if the price slides at least this much
        self.exit_down_trend_cents = int(params.get("exit_down_trend_cents", 5))
        # minimum up-move over the window to even consider a momentum (taker) entry
        self.momentum_entry_cents = int(params.get("momentum_entry_cents", 4))
        # only take momentum entries in this price band (room to run, not a
        # near-certain market where there's nothing left to gain)
        self.momentum_min_price = int(params.get("momentum_min_price", 10))
        self.momentum_max_price = int(params.get("momentum_max_price", 90))
        # free capital from positions that go nowhere for too long (0 = off)
        self.max_hold_cycles = int(params.get("max_hold_cycles", 0))
        self._history: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.lookback)
        )
        self._held: Dict[str, int] = defaultdict(int)

    def _trend(self, ticker: str, mid: float):
        hist = self._history[ticker]
        hist.append(mid)
        if len(hist) < self.lookback:
            return None  # not enough data yet
        return hist[-1] - hist[0]

    def decide(self, snap: MarketSnapshot) -> List[OrderIntent]:
        trend = self._trend(snap.ticker, snap.yes_mid)
        pos = snap.position

        # ---------------- holding YES: manage the exit ----------------
        if not pos.is_flat and pos.side == "yes":
            self._held[snap.ticker] += 1
            # 1) stop-loss (market sell at the bid)
            if snap.yes_bid > 0 and snap.yes_bid <= pos.avg_price_cents - self.stop_loss_cents:
                return [OrderIntent(
                    ticker=snap.ticker, action="sell", side="yes",
                    count=pos.count, price_cents=snap.yes_bid,
                    order_type="market",
                    reason=f"STOP-LOSS bid {snap.yes_bid}c vs entry {pos.avg_price_cents}c",
                )]
            # 2) momentum cut
            if trend is not None and trend <= -self.exit_down_trend_cents and snap.yes_bid > 0:
                return [OrderIntent(
                    ticker=snap.ticker, action="sell", side="yes",
                    count=pos.count, price_cents=snap.yes_bid,
                    order_type="market",
                    reason=f"momentum cut ({trend:.1f}c slide)",
                )]
            # 2.5) max-hold: position went nowhere too long -> free the capital
            if (self.max_hold_cycles and self._held[snap.ticker] > self.max_hold_cycles
                    and snap.yes_bid > 0):
                return [OrderIntent(
                    ticker=snap.ticker, action="sell", side="yes",
                    count=pos.count, price_cents=snap.yes_bid,
                    order_type="market",
                    reason=f"max-hold {self._held[snap.ticker]} cycles - freeing capital",
                )]
            # 3) take-profit (rest a sell above entry, fee-aware)
            costs = (fee_cents(pos.avg_price_cents, pos.count, taker=False)
                     + fee_cents(pos.avg_price_cents + 2, pos.count, taker=False))
            target = pos.avg_price_cents + max(2, (costs // max(1, pos.count)) + 1)
            target = min(99, max(target, snap.yes_bid + self.edge_cents))
            return [OrderIntent(
                ticker=snap.ticker, action="sell", side="yes",
                count=pos.count, price_cents=target,
                reason=f"take-profit @ {target}c (entry {pos.avg_price_cents}c)",
            )]

        # ---------------- flat: consider an entry --------------------
        if pos.is_flat:
            self._held[snap.ticker] = 0
            if snap.yes_bid <= 0 or snap.yes_ask <= 0:
                return []
            falling_hard = (trend is not None and trend < -self.max_down_trend_cents)

            # A) spread-capture (maker) entry - the primary, fee-earning path
            needed = min_profitable_spread_cents(snap.yes_bid, taker=False)
            if snap.yes_spread >= needed and not falling_hard:
                entry = snap.yes_bid + self.edge_cents
                if entry < snap.yes_ask:  # stay a maker, don't cross
                    note = "flat" if trend is None else f"trend {trend:+.1f}c"
                    return [OrderIntent(
                        ticker=snap.ticker, action="buy", side="yes",
                        count=self.order_size, price_cents=entry,
                        reason=f"enter yes @ {entry}c (spread {snap.yes_spread}c, {note})",
                    )]

            # B) momentum (taker) entry - only if the up-move pays for crossing
            if trend is not None and trend >= self.momentum_entry_cents:
                rt_fee = fee_cents(snap.yes_ask, 1, taker=True) * 2  # both legs, per contract
                cost_to_beat = snap.yes_spread + rt_fee + 1
                in_band = self.momentum_min_price <= snap.yes_ask <= self.momentum_max_price
                if trend >= cost_to_beat and in_band:
                    return [OrderIntent(
                        ticker=snap.ticker, action="buy", side="yes",
                        count=self.order_size, price_cents=snap.yes_ask,
                        order_type="market",
                        reason=(f"momentum entry +{trend:.1f}c (clears {snap.yes_spread}c "
                                f"spread + {rt_fee}c fees)"),
                    )]

        return []
