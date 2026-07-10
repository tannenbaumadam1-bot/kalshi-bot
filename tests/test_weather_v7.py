import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_t = tempfile.mkdtemp()
import weather_paper as wp
wp.WSIM=os.path.join(_t,"s.json"); wp.WBETS=os.path.join(_t,"b.csv"); wp.WSTATE=os.path.join(_t,"st.json")
import weather_edge as we
import weather_nowcast as nc
import weather_shadow as wsh


def _fresh():
    w = wp.WeatherPaper.__new__(wp.WeatherPaper)
    w.start = 10000.0; w.cash = 10000.0; w.per_bet = 2.0
    w.bets = {}; w.realized = 0.0; w.wins = 0; w.losses = 0
    w.fees = 0.0; w.placed = 0; w.history = []; w.cooldown = {}
    return w


def _edge(entry=35, fair=0.48, city="denver", strike=95, is_low=False, tk=None):
    mk = {"ticker": tk or "T-%s-%s" % (city, strike), "city": city,
          "is_low": is_low, "strike": strike, "yes_bid": entry,
          "yes_ask": entry + 6, "entry_price": entry, "maker": True,
          "src": "forecast", "w": 0.35, "date": "2026-07-10"}
    return (5.0, "YES", mk, fair, 90.0)


def test_ticker_date_beats_close_time():
    # THE v7 bug fix: settlement day from the ticker, not UTC close time
    assert we.ticker_date("KXLOWTCHI-26JUL11-T68") == "2026-07-11"
    assert we.ticker_date("KXHIGHTPHX-26AUG01-T110") == "2026-08-01"
    assert we.ticker_date("KXHIGHNY-27JAN05-T40") == "2027-01-05"
    assert we.ticker_date("NOTATICKER") is None
    assert we.ticker_date("") is None


def test_cooldown_blocks_reentry(monkeypatch):
    import datetime
    w = _fresh()
    e = _edge(tk="T-cool")
    monkeypatch.setattr(we, "scan", lambda **kw: [e])
    w.cooldown["T-cool"] = datetime.datetime.now().isoformat(timespec="seconds")
    w.place()
    assert len(w.bets) == 0                       # inside cooldown -> blocked
    old = datetime.datetime.now() - datetime.timedelta(hours=wp.COOLDOWN_H + 1)
    w.cooldown["T-cool"] = old.isoformat(timespec="seconds")
    w.place()
    assert len(w.bets) == 1                       # cooldown expired -> allowed


def test_min_price_30_rejects_cheap_entries(monkeypatch):
    w = _fresh()
    monkeypatch.setattr(we, "scan", lambda **kw: [_edge(entry=20, fair=0.40, tk="T-cheap")])
    w.place()
    assert len(w.bets) == 0                       # 20c < MIN_PRICE 30 -> no bet
    monkeypatch.setattr(we, "scan", lambda **kw: [_edge(entry=30, fair=0.45, tk="T-ok")])
    w.place()
    assert len(w.bets) == 1


def test_lo_market_book_cap(monkeypatch):
    w = _fresh()
    # bankroll 10000c -> lo allowance = 0.5 * 0.30 * 10000 = 1500c
    los = [_edge(entry=50, fair=0.65, city="c%d" % i, strike=70, is_low=True,
                 tk="T-lo-%d" % i) for i in range(40)]
    monkeypatch.setattr(we, "scan", lambda **kw: los)
    w.place()
    lo_stake = sum(b["entry"] * b["count"] for b in w.bets.values() if b["hl"] == "lo")
    assert 0 < lo_stake <= wp.LO_BOOK_FRAC * wp.MAX_BOOK_FRAC * 10000
    # a HI bet must still be allowed after the lo cap binds
    monkeypatch.setattr(we, "scan", lambda **kw: [_edge(entry=50, fair=0.65, tk="T-hi")])
    w.place()
    assert "T-hi" in w.bets


def test_calibrate_maps_overconfident_history():
    # _calibrate: pside 0.60 bucket with 11/30 actual -> ~ (11+1)/(30+2) = 0.375
    w = _fresh()
    w.history = ([{"era": wp.ERA, "pnl": 1.0, "pside": 0.60, "outcome": 1}] * 11 +
                 [{"era": wp.ERA, "pnl": -0.4, "pside": 0.60, "outcome": 0}] * 19)
    p_cal = w._calibrate(0.60)
    assert 0.30 < p_cal < 0.45 < 0.60
    # too little data -> passthrough (probe stakes protect us there anyway)
    w.history = w.history[:5]
    assert w._calibrate(0.60) == 0.60


def test_scale_mode_sizes_on_calibrated_prob(monkeypatch):
    # wiring: in scale mode place() must Kelly-size on _calibrate(p), so a bet
    # whose RAW Kelly is positive but CALIBRATED Kelly is negative gets skipped
    w = _fresh()
    monkeypatch.setattr(w, "_gate", lambda: ("scale", 30))
    monkeypatch.setattr(w, "_calibrate", lambda p: p * 0.6)   # harsh empirical shrink
    monkeypatch.setattr(we, "scan", lambda **kw: [_edge(entry=45, fair=0.60, tk="T-k")])
    w.place()
    # raw f* = .60-.40/(55/45) = +0.27 ; calibrated p=.36 -> f* = .36-.64/1.222 < 0
    assert "T-k" not in w.bets
    # and a genuinely strong calibrated edge still gets sized > probe
    monkeypatch.setattr(w, "_calibrate", lambda p: p)
    monkeypatch.setattr(we, "scan", lambda **kw: [_edge(entry=35, fair=0.60, tk="T-big")])
    w.place()
    assert "T-big" in w.bets
    b = w.bets["T-big"]
    assert b["entry"] * b["count"] > wp.PROBE_COST_CENTS   # Kelly-sized, not probe


def test_nowcast_locked_outcomes():
    # high already past strike -> P ~ 1 regardless of remaining hours
    assert nc.final_prob(96.0, [85.0, 87.0], 92, is_low=False) > 0.98
    # low already below strike -> P(low >= strike) ~ 0
    assert nc.final_prob(69.0, [80.0], 72, is_low=True) < 0.05
    # remaining-hours risk keeps it honest in between
    p = nc.final_prob(88.0, [90.0, 93.0], 92, is_low=False)
    assert 0.2 < p < 0.9


def test_nowcast_state_pricing():
    st = {"run_max": 91.0, "run_min": 70.0, "n_obs": 20,
          "rem_max": [89.0, 93.0, 95.0], "rem_min": [72.0, 74.0, 71.0]}
    hi = nc.prob_from_state(st, 92, is_low=False)   # 2/3 members push over
    assert 0.3 < hi < 0.9
    lo = nc.prob_from_state(st, 70, is_low=True)    # run_min 70 vs strike 70
    assert 0.2 < lo < 0.8
    assert nc.prob_from_state(None, 92, False) is None


def test_fit_weight_and_blend_loading():
    # market calibrated, model junk -> learned w = 0, clamped to W_CLAMP floor
    rows = ([{"mp": 0.9, "out": 0, "mid": 0.2}] * 100 +
            [{"mp": 0.1, "out": 1, "mid": 0.8}] * 100)
    f = wsh.fit_weight(rows)
    assert f["w_best"] == 0.0 and f["n"] == 200
    path = os.path.join(_t, "lw.json")
    json.dump(f, open(path, "w"))
    old, we.LEARNED_W_PATH = we.LEARNED_W_PATH, path
    we._wcache["ts"] = 0.0
    try:
        assert we.blend_weight() == we.W_CLAMP[0]       # clamped, not 0
    finally:
        we.LEARNED_W_PATH = old; we._wcache["ts"] = 0.0
    # small n -> ignored, fall back to MODEL_WEIGHT
    json.dump({"n": 10, "w_best": 0.0}, open(path, "w"))
    old, we.LEARNED_W_PATH = we.LEARNED_W_PATH, path
    we._wcache["ts"] = 0.0
    try:
        assert we.blend_weight() == we.MODEL_WEIGHT
    finally:
        we.LEARNED_W_PATH = old; we._wcache["ts"] = 0.0


def test_shadow_report_data_shape():
    rows = [{"mp": 0.5, "out": 1, "mid": 0.5}] * 10
    f = wsh.fit_weight(rows)
    assert set(f) >= {"n", "w_best", "brier_model", "brier_market", "brier_best"}
    assert f["brier_best"] <= f["brier_model"] and f["brier_best"] <= f["brier_market"]


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
    print("%d/%d v7 tests passed" % (p, len(names)))
    return 0 if p == len(names) else 1


if __name__ == "__main__":
    sys.exit(_run())


def test_quote_returns_tuple_on_success_and_failure(monkeypatch):
    # regression: a tail-rebuild once dropped the success return -> None
    # unpack crash in exit_check on every live step. Exercise the REAL method.
    import weather_paper as wpm
    w = _fresh()
    class _R:
        def json(self):
            return {"market": {"yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4400"}}
    monkeypatch.setattr(wpm.requests, "get", lambda *a, **k: _R())
    assert w._quote("T-x") == (40, 44)
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(wpm.requests, "get", _boom)
    assert w._quote("T-x") == (None, None)
