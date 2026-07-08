import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import funding_arb as fa
fa.FSTATE = os.path.join(tempfile.mkdtemp(), "f.json")


def _fund():
    return [
        {"asset": "BTC", "funding_hr": 0.0000125, "mark": 60000.0, "apy": 0.0000125*24*365},  # ~11% keep
        {"asset": "MEME", "funding_hr": 0.02, "mark": 0.001, "apy": 0.02*24*365},              # micro meme -> drop
        {"asset": "WILD", "funding_hr": 0.05, "mark": 5.0, "apy": 0.05*24*365},                # ~438% > MAX_APY -> drop
        {"asset": "ZRO", "funding_hr": 0.0000633, "mark": 0.93, "apy": 0.0000633*24*365}]       # ~55% keep


def test_opportunities_filter():
    ops = fa.opportunities(_fund())
    names = {o["asset"] for o in ops}
    assert "BTC" in names and "ZRO" in names
    assert "MEME" not in names            # micro-cap meme excluded
    assert "WILD" not in names            # extreme funding (blowup risk) excluded
    # side is set correctly (positive funding -> short perp)
    btc = next(o for o in ops if o["asset"] == "BTC")
    assert btc["side"].startswith("short perp")


def test_allocate_caps():
    p = fa.FundingPaper.__new__(fa.FundingPaper)
    p.start=100.0; p.cash=100.0; p.days=0; p.earned=0.0; p.last_date=""; p.positions=[]; p.history=[]
    picks = p._allocate(fa.opportunities(_fund()))
    assert len(picks) <= fa.MAX_ASSETS
    for pk in picks:
        assert pk["alloc"] <= fa.MAX_PER_ASSET * p.cash + 1e-9
        assert pk["net"] >= 0


def test_step_accrues_and_compounds():
    p = fa.FundingPaper.__new__(fa.FundingPaper)
    p.start=100.0; p.cash=100.0; p.days=0; p.earned=0.0; p.last_date=""; p.positions=[]; p.history=[]
    net, picks = p.step(funding=_fund(), force=True)
    assert net > 0 and p.cash > 100.0 and p.days == 1 and picks


def test_gate_probe_then_scale():
    p = fa.FundingPaper.__new__(fa.FundingPaper)
    p.start=100.0; p.cash=100.0; p.days=0; p.earned=0.0; p.last_date=""; p.positions=[]
    p.history = []
    assert p.gate()[0] == "probe"
    p.days = 25
    p.history = [{"net": 0.05} for _ in range(25)]     # 25 profitable days
    assert p.gate()[0] == "scale"
    p.history = [{"net": -0.05} for _ in range(25)]    # losing streak -> stay probe
    assert p.gate()[0] == "probe"


def test_persistence_enrich():
    ops = fa.opportunities(_fund())
    hist = {"BTC": 0.00002, "ZRO": -0.0000633}        # ZRO EWMA flips sign -> drop
    en = fa.enrich_persistence(ops, hist_fn=lambda a: hist.get(a))
    names = {o["asset"] for o in en}
    assert "ZRO" not in names                          # sign-unstable carry skipped
    btc = next(o for o in en if o["asset"] == "BTC")
    assert abs(btc["apy"] - 0.00002 * 24 * 365) < 1e-9 # EWMA replaces snapshot APY
    assert btc["funding_hr_ewma"] == 0.00002
    # identity fallback when history unavailable
    en2 = fa.enrich_persistence(ops, hist_fn=lambda a: None)
    assert {o["asset"] for o in en2} == {o["asset"] for o in ops}


def test_hysteresis_keep_and_evict():
    p = fa.FundingPaper.__new__(fa.FundingPaper)
    p.start=100.0; p.cash=100.0; p.days=1; p.earned=0.0; p.last_date=""; p.history=[]
    p.positions = [{"asset": "BTC"}]                   # incumbent, ~11% APY
    ops = fa.opportunities(_fund())                    # BTC + ZRO qualify
    # small-edge challenger does NOT evict (ZRO ~55% but seats are open -> fills free)
    roster = p._roster(ops)
    assert {o["asset"] for o in roster} == {"BTC", "ZRO"}   # open seat filled, no eviction
    # force a full book: MAX_ASSETS=1 -> ZRO must beat BTC by SWITCH_EDGE to evict
    old = fa.MAX_ASSETS
    fa.MAX_ASSETS = 1
    try:
        roster = p._roster(ops)
        assert roster[0]["asset"] == "ZRO"             # 55% > 11% + 10pts -> evicted
        near = [dict(o) for o in ops]
        for o in near:
            if o["asset"] == "ZRO":
                o["apy"] = 0.115                       # only 0.5pt better -> keep incumbent
        roster = p._roster(near)
        assert roster[0]["asset"] == "BTC"
    finally:
        fa.MAX_ASSETS = old


def test_weighted_alloc_and_rotation_cost():
    p = fa.FundingPaper.__new__(fa.FundingPaper)
    p.start=100.0; p.cash=100.0; p.days=1; p.earned=0.0; p.last_date=""; p.history=[]
    p.positions = [{"asset": "BTC"}]
    old_cap = fa.MAX_PER_ASSET
    fa.MAX_PER_ASSET = 0.80                            # uncap so weighting is visible
    try:
        picks = p._allocate(fa.opportunities(_fund()))
        by = {pk["asset"]: pk for pk in picks}
        assert by["ZRO"]["alloc"] > by["BTC"]["alloc"] # higher APY -> more capital
        for pk in picks:
            assert pk["alloc"] <= fa.MAX_PER_ASSET * p.cash + 1e-9
    finally:
        fa.MAX_PER_ASSET = old_cap
    assert by["BTC"]["rot_cost"] == 0                  # incumbent free
    assert by["ZRO"]["rot_cost"] > 0                   # new leg after day 0 pays entry
    assert abs(by["ZRO"]["rot_cost"] - round(fa.SWITCH_COST * by["ZRO"]["alloc"], 4)) < 1e-6


def _run():
    import traceback
    names = sorted(n for n in globals() if n.startswith("test_"))
    ok = 0
    for n in names:
        try:
            globals()[n](); print("PASS " + n); ok += 1
        except Exception:
            print("FAIL " + n); traceback.print_exc()
    print("%d/%d funding tests passed" % (ok, len(names)))
    return 0 if ok == len(names) else 1


if __name__ == "__main__":
    sys.exit(_run())
