import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshibot.strategies.smart import SmartStrategy
from kalshibot.strategies.base import MarketSnapshot, Position


def snap(bid, ask, pos=None):
    return MarketSnapshot("X", yes_bid=bid, yes_ask=ask, no_bid=100 - ask,
                          no_ask=100 - bid, yes_bid_size=100, yes_ask_size=100,
                          position=pos or Position())


def test_smart_stop_loss_sells_at_bid():
    s = SmartStrategy({"lookback_cycles": 4, "stop_loss_cents": 8})
    held = Position(side="yes", count=1, avg_price_cents=50)
    out = s.decide(snap(40, 60, held))   # bid 40 <= 50-8
    assert out and out[0].action == "sell"
    assert out[0].order_type == "market" and out[0].price_cents == 40


def test_smart_take_profit_above_entry():
    s = SmartStrategy({"lookback_cycles": 4, "stop_loss_cents": 8})
    held = Position(side="yes", count=1, avg_price_cents=50)
    out = s.decide(snap(50, 70, held))   # no stop, no trend yet
    assert out and out[0].action == "sell"
    assert out[0].order_type == "limit" and out[0].price_cents > 50


def test_smart_enters_when_flat_and_spread_wide():
    s = SmartStrategy({"lookback_cycles": 4})
    out = s.decide(snap(40, 60))         # wide spread, no trend yet
    assert out and out[0].action == "buy" and out[0].side == "yes"
    assert out[0].price_cents == 41


def test_smart_skips_entry_when_price_falling():
    s = SmartStrategy({"lookback_cycles": 4, "max_down_trend_cents": 3})
    for bid, ask in [(55, 65), (50, 60), (45, 55)]:
        s.decide(snap(bid, ask))
    out = s.decide(snap(40, 60))         # trend now ~-10c -> skip
    assert out == []


def test_smart_momentum_entry_on_strong_rise():
    # tight spread (no maker capture) but a big up-move that clears spread+fees
    s = SmartStrategy({"lookback_cycles": 4, "momentum_entry_cents": 4})
    for bid, ask in [(44, 46), (47, 49), (50, 52)]:
        s.decide(snap(bid, ask))
    out = s.decide(snap(52, 54))         # mids 45,48,51,53 -> trend ~+8c
    assert out and out[0].action == "buy" and out[0].side == "yes"
    assert out[0].order_type == "market"        # crosses the spread (taker)
    assert out[0].price_cents == 54             # buys at the ask


def test_smart_momentum_skips_small_rise():
    # tight spread AND only a small up-move -> neither maker nor momentum fires
    s = SmartStrategy({"lookback_cycles": 4, "momentum_entry_cents": 4})
    for bid, ask in [(49, 51), (50, 52), (51, 53)]:
        s.decide(snap(bid, ask))
    out = s.decide(snap(52, 54))         # mids 50,51,52,53 -> trend ~+3c (< 4)
    assert out == []


def test_smart_max_hold_frees_capital():
    s = SmartStrategy({"lookback_cycles": 4, "stop_loss_cents": 8, "max_hold_cycles": 3})
    held = Position(side="yes", count=5, avg_price_cents=50)
    out = None
    for _ in range(5):                    # flat price: no stop, no take-profit fill
        out = s.decide(snap(50, 60, held))
    # after exceeding max_hold (3 cycles) it market-sells at the bid to free capital
    assert out and out[0].action == "sell"
    assert out[0].order_type == "market" and out[0].price_cents == 50


def _run():
    import traceback
    names = sorted(n for n in globals() if n.startswith("test_"))
    p = 0
    for n in names:
        try:
            globals()[n](); print("PASS " + n); p += 1
        except Exception:
            print("FAIL " + n); traceback.print_exc()
    print("%d/%d smart tests passed" % (p, len(names)))
    return 0 if p == len(names) else 1


if __name__ == "__main__":
    sys.exit(_run())
