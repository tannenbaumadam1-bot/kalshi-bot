"""Risk manager: the bot's seatbelts.

Every proposed order is checked against these limits before it can be
sent. If any limit is hit, the order is blocked (and for daily-loss /
trade-count limits, the whole bot stops for the day).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Optional, Tuple

from .config import RiskConfig


@dataclass
class RiskState:
    day: date = field(default_factory=date.today)
    trades_today: int = 0
    realized_pnl_cents: int = 0      # locked-in profit/loss today
    start_balance_cents: int = 0     # balance at start of the day

    def roll_day_if_needed(self) -> None:
        today = date.today()
        if today != self.day:
            self.day = today
            self.trades_today = 0
            self.realized_pnl_cents = 0
            self.start_balance_cents = 0


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.state = RiskState()

    # ----- helpers --------------------------------------------------
    def start_day(self, balance_cents: int) -> None:
        self.state.roll_day_if_needed()
        if self.state.start_balance_cents == 0:
            self.state.start_balance_cents = balance_cents

    def day_pnl_cents(self, current_balance_cents: int) -> int:
        """Mark-to-balance P&L for the day (cash basis)."""
        if self.state.start_balance_cents == 0:
            return 0
        return current_balance_cents - self.state.start_balance_cents

    # ----- the gate -------------------------------------------------
    def can_trade_today(self, current_balance_cents: int) -> Tuple[bool, str]:
        """Global checks that, if failed, halt trading for the rest of the day."""
        self.state.roll_day_if_needed()

        if self.state.trades_today >= self.cfg.max_trades_per_day:
            return False, (
                f"daily trade cap reached "
                f"({self.state.trades_today}/{self.cfg.max_trades_per_day})"
            )

        loss = -self.day_pnl_cents(current_balance_cents)
        if loss >= int(self.cfg.max_daily_loss_dollars * 100):
            return False, (
                f"daily loss limit hit (down ${loss/100:.2f}, "
                f"limit ${self.cfg.max_daily_loss_dollars:.2f})"
            )

        return True, "ok"

    def approve_order(
        self,
        *,
        order_cost_cents: int,
        current_position_cents: int,
        open_exposure_cents: int,
        current_balance_cents: int,
    ) -> Tuple[bool, str]:
        """Per-order checks. order_cost_cents is what this order would tie up."""
        ok, reason = self.can_trade_today(current_balance_cents)
        if not ok:
            return False, reason

        if (current_position_cents + order_cost_cents) > int(
            self.cfg.max_position_dollars * 100
        ):
            return False, (
                f"would exceed per-market cap "
                f"(${self.cfg.max_position_dollars:.2f})"
            )

        if (open_exposure_cents + order_cost_cents) > int(
            self.cfg.max_open_dollars * 100
        ):
            return False, (
                f"would exceed total open exposure cap "
                f"(${self.cfg.max_open_dollars:.2f})"
            )

        reserve = int(self.cfg.min_cash_reserve_dollars * 100)
        if (current_balance_cents - order_cost_cents) < reserve:
            return False, (
                f"would breach cash reserve floor "
                f"(${self.cfg.min_cash_reserve_dollars:.2f})"
            )

        return True, "ok"

    def record_trade(self) -> None:
        self.state.trades_today += 1
