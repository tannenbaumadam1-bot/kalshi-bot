import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kalshibot.arb import find_arbs, plan_basket_buy


def test_underround_detected():
    mks = [{'event_ticker': 'E', 'yes_bid_dollars': '0.38', 'yes_ask_dollars': '0.40'},
           {'event_ticker': 'E', 'yes_bid_dollars': '0.43', 'yes_ask_dollars': '0.45'}]
    assert len(find_arbs(mks)['under']) == 1


def test_no_arb_when_near_one():
    mks = [{'event_ticker': 'X', 'yes_bid_dollars': '0.50', 'yes_ask_dollars': '0.51'},
           {'event_ticker': 'X', 'yes_bid_dollars': '0.48', 'yes_ask_dollars': '0.50'}]
    r = find_arbs(mks)
    assert r['under'] == [] and r['over'] == []


def test_overround_detected():
    mks = [{'event_ticker': 'O', 'yes_bid_dollars': '0.60', 'yes_ask_dollars': '0.62'},
           {'event_ticker': 'O', 'yes_bid_dollars': '0.55', 'yes_ask_dollars': '0.57'}]
    assert len(find_arbs(mks)['over']) == 1


def test_singletons_ignored():
    mks = [{'event_ticker': 'S', 'yes_bid_dollars': '0.10', 'yes_ask_dollars': '0.12'}]
    r = find_arbs(mks)
    assert r['under'] == [] and r['over'] == []


def test_plan_fires_on_profitable_fillable_basket():
    legs = [("A", 40, 50), ("B", 45, 50)]
    assert plan_basket_buy(legs, qty=1, min_net_cents=2) is not None


def test_plan_aborts_if_a_leg_lacks_depth():
    legs = [("A", 40, 50), ("B", 45, 0)]
    assert plan_basket_buy(legs, qty=1) is None


def test_plan_aborts_if_not_profitable():
    legs = [("A", 50, 50), ("B", 52, 50)]
    assert plan_basket_buy(legs, qty=1) is None


def test_sell_basket_fires_when_bids_over_one():
    from kalshibot.arb import plan_basket_sell
    legs = [("A", 60, 50), ("B", 55, 50)]   # bids sum 115 > 100 -> arb
    assert plan_basket_sell(legs, qty=1, min_net_cents=2) is not None

def test_sell_basket_aborts_when_bids_under_one():
    from kalshibot.arb import plan_basket_sell
    legs = [("A", 50, 50), ("B", 48, 50)]   # 98 < 100 -> no arb
    assert plan_basket_sell(legs, qty=1) is None

def test_reconcile_perfect_fill():
    from kalshibot.arb import reconcile_fills
    m, excess = reconcile_fills({"A": 1, "B": 1, "C": 1})
    assert m == 1 and excess == {}

def test_reconcile_one_leg_missed_flattens_rest():
    from kalshibot.arb import reconcile_fills
    m, excess = reconcile_fills({"A": 1, "B": 0, "C": 1})
    assert m == 0 and excess == {"A": 1, "C": 1}

def test_reconcile_partial_holds_min():
    from kalshibot.arb import reconcile_fills
    m, excess = reconcile_fills({"A": 3, "B": 2, "C": 3})
    assert m == 2 and excess == {"A": 1, "C": 1}


def _run():
    import traceback
    names = sorted(n for n in globals() if n.startswith('test_'))
    p = 0
    for n in names:
        try:
            globals()[n](); print('PASS ' + n); p += 1
        except Exception:
            print('FAIL ' + n); traceback.print_exc()
    print('%d/%d arb tests passed' % (p, len(names)))
    return 0 if p == len(names) else 1


if __name__ == '__main__':
    sys.exit(_run())
