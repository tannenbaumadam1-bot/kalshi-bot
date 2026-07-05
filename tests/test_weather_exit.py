import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_t = tempfile.mkdtemp()
import weather_paper as wp
wp.WSIM=os.path.join(_t,"s.json"); wp.WBETS=os.path.join(_t,"b.csv"); wp.WSTATE=os.path.join(_t,"st.json")
import weather_ensemble as wx


def _bot_with_bet(entry=30, count=3):
    w = wp.WeatherPaper.__new__(wp.WeatherPaper)
    w.start=10000.0; w.cash=10000.0; w.per_bet=2.0; w.realized=0.0
    w.wins=0; w.losses=0; w.fees=0.0; w.placed=0; w.history=[]
    w.bets = {"KXHIGHDEN-26JUL05-T95": {
        "side":"yes","entry":entry,"count":count,"fee":1,"pside":0.33,
        "city":"denver","strike":95,"hl":"hi","date":"2026-07-05","era":wp.ERA}}
    return w


def test_cuts_loss_when_model_abandons(monkeypatch):
    w = _bot_with_bet(entry=30, count=3)
    monkeypatch.setattr(wx, "prob", lambda *a, **k: (0.05, 88.0, 9))   # model now says ~5%
    w._quote = lambda tk: (10, 14)                                     # market bid 10c (underwater)
    w.exit_check()
    assert len(w.bets) == 0                       # sold: market 10c > our 5c fair
    h = w.history[-1]
    assert h["exited"] is True and h["outcome"] is None
    assert h["pnl"] < 0                            # realized a (small) loss, saved vs 0 at settle


def test_holds_underpriced_when_model_still_believes(monkeypatch):
    w = _bot_with_bet(entry=30, count=3)
    monkeypatch.setattr(wx, "prob", lambda *a, **k: (0.40, 94.0, 9))   # model still ~40%
    w._quote = lambda tk: (20, 24)                # price fell to 20c but we think 40c
    w.exit_check()
    assert len(w.bets) == 1                        # HOLD: a naive stop would wrongly sell


def test_does_not_skim_winners_in_probe(monkeypatch):
    w = _bot_with_bet(entry=30, count=3)
    monkeypatch.setattr(wx, "prob", lambda *a, **k: (0.05, 80.0, 9))
    w._quote = lambda tk: (40, 44)                # bid 40 >= entry 30 -> not underwater
    w.exit_check()
    assert len(w.bets) == 1                        # only cuts losses; winners ride to settle


def test_holds_when_forecast_unavailable(monkeypatch):
    w = _bot_with_bet()
    monkeypatch.setattr(wx, "prob", lambda *a, **k: (None, None, 0))   # sources down
    w._quote = lambda tk: (5, 9)
    w.exit_check()
    assert len(w.bets) == 1                        # never sell blind


def test_exits_excluded_from_gate():
    w = _bot_with_bet()
    w.bets = {}
    # 30 EXITED rows (outcome None) must NOT satisfy the 30-settled gate
    w.history = [{"era": wp.ERA, "pnl": 0.5, "pside": 0.6, "outcome": None, "exited": True}
                 for _ in range(30)]
    assert w._gate() == ("probe", 0)
    # 30 real settled rows DO count
    w.history = [{"era": wp.ERA, "pnl": 0.3, "pside": 0.6, "outcome": 1} for _ in range(30)]
    assert w._gate()[0] == "scale"


def _run():
    import traceback
    class _MP:
        def setattr(self, o, n, v): setattr(o, n, v)
    names = sorted(n for n in globals() if n.startswith("test_"))
    p = 0
    for n in names:
        try:
            fn = globals()[n]; fn(_MP()) if fn.__code__.co_argcount else fn()
            print("PASS " + n); p += 1
        except Exception:
            print("FAIL " + n); traceback.print_exc()
    print("%d/%d exit tests passed" % (p, len(names)))
    return 0 if p == len(names) else 1


if __name__ == "__main__":
    sys.exit(_run())
