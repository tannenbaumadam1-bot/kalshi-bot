"""Logic tests - run with:  python -m pytest -q   (or python tests/test_logic.py)

These test the pure decision logic with no network calls, so you can
trust the math and the safety limits before ever connecting to Kalshi.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshibot.fees import fee_cents, min_profitable_spread_cents, round_trip_edge_cents
from kalshibot.risk import RiskManager
from kalshibot.config import RiskConfig
from kalshibot.engine import parse_orderbook
from kalshibot.strategies.base import MarketSnapshot, Position
from kalshibot.strategies.maker import MakerStrategy


# ---------- fees ----------
def test_fee_is_highest_at_50c():
    f50 = fee_cents(50, 100, taker=True)
    f10 = fee_cents(10, 100, taker=True)
    assert f50 > f10
    assert f50 == 175  # 0.07 * .5 * .5 * 100 = $1.75 = 175c


def test_maker_fee_cheaper_than_taker():
    assert fee_cents(50, 100, taker=False) < fee_cents(50, 100, taker=True)


def test_fee_rounds_up_on_tiny_orders():
    assert fee_cents(50, 1, taker=True) == 2  # 1.75c -> rounds up to 2c


def test_round_trip_needs_real_spread():
    assert round_trip_edge_cents(50, 50, 1, taker=True) < 0
    assert round_trip_edge_cents(40, 60, 1, taker=True) > 0


# ---------- risk manager ----------
def make_risk():
    cfg = RiskConfig(max_trades_per_day=20, max_position_dollars=2.00,
                     max_open_dollars=10.00, max_daily_loss_dollars=5.00,
                     min_cash_reserve_dollars=1.00)
    rm = RiskManager(cfg)
    rm.start_day(2000)
    return rm


def test_blocks_when_position_cap_exceeded():
    rm = make_risk()
    ok, reason = rm.approve_order(order_cost_cents=300, current_position_cents=0,
                                  open_exposure_cents=0, current_balance_cents=2000)
    assert not ok and "per-market" in reason


def test_blocks_below_cash_reserve():
    cfg = RiskConfig(max_position_dollars=2.00, max_open_dollars=10.00,
                     max_daily_loss_dollars=5.00, min_cash_reserve_dollars=1.00)
    rm = RiskManager(cfg)
    rm.start_day(200)
    ok, reason = rm.approve_order(order_cost_cents=150, current_position_cents=0,
                                  open_exposure_cents=0, current_balance_cents=200)
    assert not ok and "reserve" in reason


def test_blocks_after_daily_trade_cap():
    rm = make_risk()
    for _ in range(20):
        rm.record_trade()
    ok, reason = rm.can_trade_today(2000)
    assert not ok and "trade cap" in reason


def test_blocks_after_daily_loss_limit():
    rm = make_risk()
    ok, reason = rm.can_trade_today(1400)
    assert not ok and "loss limit" in reason


def test_allows_normal_small_order():
    rm = make_risk()
    ok, reason = rm.approve_order(order_cost_cents=50, current_position_cents=0,
                                  open_exposure_cents=0, current_balance_cents=2000)
    assert ok, reason


# ---------- orderbook parsing ----------
def test_parse_orderbook_best_levels():
    ob = {"yes": [[40, 100], [39, 50], [41, 30]], "no": [[55, 80], [54, 20]]}
    bb = parse_orderbook(ob)
    assert bb["yes_bid"] == 41
    assert bb["yes_ask"] == 100 - 55
    assert bb["yes_bid_size"] == 30


# ---------- maker strategy ----------
def test_maker_enters_only_on_wide_spread():
    strat = MakerStrategy({"edge_cents": 1, "order_size": 1})
    tight = MarketSnapshot("X", yes_bid=44, yes_ask=45, no_bid=55, no_ask=56,
                           yes_bid_size=100, yes_ask_size=100, position=Position())
    assert strat.decide(tight) == []
    wide = MarketSnapshot("X", yes_bid=40, yes_ask=60, no_bid=40, no_ask=60,
                          yes_bid_size=100, yes_ask_size=100, position=Position())
    intents = strat.decide(wide)
    assert len(intents) == 1
    assert intents[0].action == "buy" and intents[0].side == "yes"
    assert intents[0].price_cents == 41


def test_maker_exits_above_entry():
    strat = MakerStrategy({"edge_cents": 1, "order_size": 1})
    held = MarketSnapshot("X", yes_bid=50, yes_ask=70, no_bid=30, no_ask=50,
                          yes_bid_size=100, yes_ask_size=100,
                          position=Position(side="yes", count=1, avg_price_cents=50))
    intents = strat.decide(held)
    assert len(intents) == 1
    assert intents[0].action == "sell"
    assert intents[0].price_cents > 50


def _run_all():
    import traceback
    names = sorted(n for n in globals() if n.startswith("test_"))
    passed = 0
    for n in names:
        try:
            globals()[n]()
            print("PASS " + n)
            passed += 1
        except Exception:
            print("FAIL " + n)
            traceback.print_exc()
    print("\n%d/%d tests passed" % (passed, len(names)))
    return 0 if passed == len(names) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
