"""Strategy plugins. Add new strategies here and register them below."""

from __future__ import annotations

from typing import Any, Dict

from .base import MarketSnapshot, OrderIntent, Position, Strategy
from .maker import MakerStrategy
from .momentum import MomentumStrategy
from .smart import SmartStrategy
from .watch import WatchStrategy

_REGISTRY = {
    "maker": MakerStrategy,
    "momentum": MomentumStrategy,
    "smart": SmartStrategy,
    "watch": WatchStrategy,
}


def build_strategy(name: str, params: Dict[str, Any]) -> Strategy:
    name = (name or "smart").lower()
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {', '.join(_REGISTRY)}"
        )
    return _REGISTRY[name](params.get(name, {}) or {})


__all__ = [
    "MarketSnapshot", "OrderIntent", "Position", "Strategy",
    "MakerStrategy", "MomentumStrategy", "SmartStrategy", "WatchStrategy",
    "build_strategy",
]
