"""Core types every strategy speaks in.

A strategy receives a MarketSnapshot (current prices + your position in
that market) and returns a list of OrderIntents. It never talks to the
API or moves money itself - the engine does that, after the risk
manager approves each intent. This keeps strategies simple and safe to
experiment with.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Position:
    """Your current holding in one market."""
    side: Optional[str] = None      # "yes", "no", or None if flat
    count: int = 0                  # contracts held
    avg_price_cents: int = 0        # average entry price

    @property
    def is_flat(self) -> bool:
        return self.count == 0


@dataclass
class MarketSnapshot:
    ticker: str
    yes_bid: int          # best price someone will BUY yes at (cents)
    yes_ask: int          # best price you can BUY yes at (cents)
    no_bid: int           # best price someone will BUY no at (cents)
    no_ask: int           # best price you can BUY no at (cents)
    yes_bid_size: int
    yes_ask_size: int
    position: Position

    @property
    def yes_spread(self) -> int:
        return self.yes_ask - self.yes_bid

    @property
    def yes_mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2.0


@dataclass
class OrderIntent:
    """A request to place one order. The engine validates + sends it."""
    ticker: str
    action: str           # "buy" or "sell"
    side: str             # "yes" or "no"
    count: int
    price_cents: int      # limit price on the chosen side
    reason: str = ""      # human-readable explanation, written to the journal
    order_type: str = "limit"
    arb: bool = False     # part of an arb basket (skip dollar-resizing)


class Strategy(ABC):
    name = "base"

    def __init__(self, params: Dict[str, Any]):
        self.params = params or {}

    @abstractmethod
    def decide(self, snap: MarketSnapshot) -> List[OrderIntent]:
        """Return zero or more order intents for this market."""
        raise NotImplementedError
