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
