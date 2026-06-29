"""Load and validate configuration from config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict

import yaml

DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
LIVE_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


@dataclass
class RiskConfig:
    max_trades_per_day: int = 20
    max_position_dollars: float = 2.00
    max_open_dollars: float = 10.00
    max_daily_loss_dollars: float = 5.00
    min_cash_reserve_dollars: float = 1.00
    target_position_dollars: float = 0.0
    position_pct: float = 0.0      # if >0, each position = this fraction of CURRENT equity (compounding)


@dataclass
class MarketConfig:
    scan_top_n: int = 40
    min_book_depth: int = 20
    min_spread_cents: int = 3
    min_price_cents: int = 15
    max_price_cents: int = 85
    min_volume: float = 0.0
    max_spread_cents: int = 100
    scan_pages: int = 10
    max_days_to_resolve: int = 0     # 0 = no limit; else skip markets resolving further out
    min_recent_volume: float = 0.0   # require this much 24h volume (recent activity)


@dataclass
class ArbConfig:
    trade: bool = False        # actually place arb orders (False = report only)
    qty_per_leg: int = 1
    min_net_cents: int = 2     # require at least this net edge after fees
    max_legs: int = 10         # skip giant baskets


@dataclass
class EngineConfig:
    cycle_seconds: int = 60
    cancel_stale_after_s: int = 300


@dataclass
class Config:
    environment: str
    key_id: str
    private_key_path: str
    strategy: str
    strategy_params: Dict[str, Any]
    risk: RiskConfig
    markets: MarketConfig
    engine: EngineConfig
    arb: ArbConfig
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.environment.lower() == "live"

    @property
    def base_url(self) -> str:
        return LIVE_BASE_URL if self.is_live else DEMO_BASE_URL


def load_config(path: str = "config.yaml") -> Config:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Could not find '{path}'. Copy 'config.example.yaml' to "
            f"'config.yaml' and fill in your API key id and key file path."
        )

    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}

    env = str(data.get("environment", "demo")).lower()
    if env not in ("demo", "live"):
        raise ValueError(f"environment must be 'demo' or 'live', got '{env}'")

    api = data.get("api", {})
    key_id = api.get("key_id", "")
    private_key_path = api.get("private_key_path", "")

    if not key_id or key_id.startswith("PASTE"):
        raise ValueError(
            "api.key_id is not set in config.yaml. Paste the API Key ID "
            "you got from Kalshi."
        )
    if not private_key_path:
        raise ValueError("api.private_key_path is not set in config.yaml.")

    cfg = Config(
        environment=env,
        key_id=key_id,
        private_key_path=private_key_path,
        strategy=str(data.get("strategy", "maker")),
        strategy_params=data.get("strategy_params", {}) or {},
        risk=RiskConfig(**(data.get("risk", {}) or {})),
        markets=MarketConfig(**(data.get("markets", {}) or {})),
        engine=EngineConfig(**(data.get("engine", {}) or {})),
        arb=ArbConfig(**(data.get("arb", {}) or {})),
        raw=data,
    )
    return cfg
