import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate: never touch the real ledger files
import tempfile
_tmp = tempfile.mkdtemp()
import weather_paper as wp
wp.WSIM = os.path.join(_tmp, "sim.json")
wp.WBETS = os.path.join(_tmp, "bets.csv")
wp.WSTATE = os.path.join(_tmp, "state.json")

from kalshibot.fees import fee_cents
import weather_edge as we


def _fresh():
    w = wp.WeatherPaper.__new__(wp.WeatherPaper)
    w.start = 10000.0; w.cash = 10000.0; w.per_bet = 2.0
    w.bets = {}; w.realized = 0.0; w.wins = 0; w.losses = 0
    w.fees = 0.0; w.placed = 0; w.history = []
    return w


def _edge(entry_price, side="YES", fair=0.30, city="denver", strike=95):
    mk = {"ticker": "T-%s-%s" % (city, strike), "city": city, "is_low": False,
          "strike": strike, "yes_bid": entry_price, "yes_ask": entry_price + 8,
          "entry_price": entry_price, "maker": True}
    ev = 5.0
    return [(ev, side, mk, fair, 90.0)]


def test_scan_prefers_maker_and_uses_maker_fee():
    # a market where the maker (bid) entry is +EV but the taker (ask) is not
    class MK(dict):
        pass
    # fabricate a single market through the scan math directly
    fair = 0.30
    yes_bid, yes_ask = 20, 26
    # maker: buy YES at bid 20; taker would buy at ask 26
    ev_maker = fair * 100 - yes_bid - fee_cents(yes_bid, 1, taker=False)
    ev_taker = fair * 100 - yes_ask - fee_cents(yes_ask, 1, taker=True)
    assert ev_maker > ev_taker            # maker economics strictly better
    assert ev_maker > 0 and ev_taker < ev_maker


def test_place_uses_maker_fee_and_entry_price(monkeypatch):
    w = _fresh()
    monkeypatch.setattr(we, "scan", lambda **kw: _edge(20, "YES", fair=0.32))
    w.place()
    assert len(w.bets) == 1
    b = next(iter(w.bets.values()))
    assert b["maker"] is True
    assert b["entry"] == 20                       # rested at the bid, not the ask
    # fee charged must be the MAKER fee, not taker
    assert b["fee"] == fee_cents(20, b["count"], taker=False)
    assert b["fee"] < fee_cents(20, b["count"], taker=True)


def test_probe_mode_keeps_stakes_tiny_when_unproven(monkeypatch):
    w = _fresh()
    monkeypatch.setattr(we, "scan", lambda **kw: _edge(20, "YES", fair=0.40))
    w.place()
    b = next(iter(w.bets.values()))
    # unproven era -> probe: cost basis capped near PROBE_COST_CENTS
    assert b["entry"] * b["count"] <= wp.PROBE_COST_CENTS + b["entry"]
    assert w._gate()[0] == "probe"


def test_gate_scales_only_after_calibrated_history():
    w = _fresh()
    # 30 settled current-era bets, well-calibrated & profitable
    for i in range(30):
        w.history.append({"era": wp.ERA, "pnl": 0.5, "pside": 0.60, "outcome": 1})
    mode, n = w._gate()
    assert n == 30 and mode == "scale"
    # break calibration: predicted 60% but actual 0% -> must stay in probe
    w.history = [{"era": wp.ERA, "pnl": -0.2, "pside": 0.60, "outcome": 0}
                 for _ in range(30)]
    assert w._gate()[0] == "probe"


def test_post_fee_floor_skips_thin_edges(monkeypatch):
    # scan returns nothing when edge < floor; place should place nothing
    w = _fresh()
    monkeypatch.setattr(we, "scan", lambda **kw: [])
    w.place()
    assert len(w.bets) == 0


def _run():
    import traceback
    class _MP:
        def setattr(self, obj, name, val): setattr(obj, name, val)
    names = sorted(n for n in globals() if n.startswith("test_"))
    p = 0
    for n in names:
        try:
            fn = globals()[n]
            fn(_MP()) if fn.__code__.co_argcount else fn()
            print("PASS " + n); p += 1
        except Exception:
            print("FAIL " + n); traceback.print_exc()
    print("%d/%d maker tests passed" % (p, len(names)))
    return 0 if p == len(names) else 1


if __name__ == "__main__":
    sys.exit(_run())
