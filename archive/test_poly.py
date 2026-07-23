import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import poly_client as pc
import poly_paper as pp
pp.PSTATE = os.path.join(tempfile.mkdtemp(), "poly.json")


def test_qualifying_liquidity_band():
    mid = 0.50
    bids = [(0.495, 100), (0.48, 50), (0.40, 999)]   # 0.495 in 1.5c band, 0.48 in, 0.40 out
    asks = [(0.505, 100), (0.52, 50), (0.60, 999)]
    liq = pc.qualifying_liquidity(bids, asks, mid, max_spread_c=1.5)
    assert abs(liq - (100 + 100)) < 1e-6   # only the two within 1.5c of mid count


def test_est_net_daily_capped_and_scales():
    m = {"mid": 0.5, "pool_daily": 10000.0}
    small = pp.est_net_daily(200, m, competing_shares=680000)
    big = pp.est_net_daily(800, m, competing_shares=680000)
    assert 0 < small < big                              # more capital -> more reward
    assert big <= pp.DAILY_NET_CAP * 800 + 1e-9         # never exceeds the sanity cap


def test_pick_respects_caps_and_affordability():
    p = pp.PolyPaper.__new__(pp.PolyPaper)
    p.start=1000.0; p.cash=1000.0; p.days=0; p.earned=0.0; p.history=[]
    markets = [
        {"q":"cheap-min","mid":0.5,"pool_daily":8000.0,"min_size":100,"max_spread_c":1.5},   # $50 min -> ok
        {"q":"pricey-min","mid":0.5,"pool_daily":9000.0,"min_size":1000,"max_spread_c":1.5}]  # $500 min > 25% cap -> skip
    picks = p._pick(markets, comp_fn=lambda m: 500000.0)
    names = [m["q"] for m,_,_,_ in picks]
    assert "cheap-min" in names and "pricey-min" not in names
    for _m, alloc, _c, _n in picks:
        assert alloc <= pp.MAX_PER_MKT_FRAC * p.cash + 1e-9   # per-market cap respected


def test_pick_limits_market_count():
    p = pp.PolyPaper.__new__(pp.PolyPaper)
    p.start=100000.0; p.cash=100000.0; p.days=0; p.earned=0.0; p.history=[]
    markets = [{"q":"m%d"%i,"mid":0.5,"pool_daily":8000.0,"min_size":10,"max_spread_c":1.5}
               for i in range(20)]
    picks = p._pick(markets, comp_fn=lambda m: 400000.0)
    assert len(picks) <= pp.MAX_MARKETS


def test_step_accrues_and_compounds():
    p = pp.PolyPaper.__new__(pp.PolyPaper)
    p.start=100.0; p.cash=100.0; p.days=0; p.earned=0.0; p.history=[]
    markets = [{"q":"m","mid":0.5,"pool_daily":8000.0,"min_size":20,"max_spread_c":1.5}]
    net, picks = p.step(markets=markets, comp_fn=lambda m: 300000.0)
    assert net > 0 and p.cash > 100.0 and p.days == 1 and p.earned == net


def _run():
    import traceback
    names = sorted(n for n in globals() if n.startswith("test_"))
    ok = 0
    for n in names:
        try:
            globals()[n](); print("PASS " + n); ok += 1
        except Exception:
            print("FAIL " + n); traceback.print_exc()
    print("%d/%d poly tests passed" % (ok, len(names)))
    return 0 if ok == len(names) else 1


if __name__ == "__main__":
    sys.exit(_run())
