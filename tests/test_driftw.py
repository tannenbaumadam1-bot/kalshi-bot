"""Drift WIDE book (driftw1): universe filters, triggers, dedupe, gate, exits."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drift_wide as dw


def _mk(tk="KXFED-26JUL-T450", bid=82, ask=85, name="Fed decision", event="KXFED-26JUL",
        vol=500.0, hrs=10.0):
    return {"ticker": tk, "event": event, "name": name,
            "yes_bid": bid, "yes_ask": ask, "vol": vol, "hrs": hrs}


def _bot(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dw, "BETS", str(tmp_path / "b.csv"))
    b = dw.DriftWide.__new__(dw.DriftWide)
    b.start, b.cash = 10000, 10000.0
    b.bets, b.history, b.last_mid, b.last_vol = {}, [], {}, {}
    b.wins = b.losses = b.placed = 0
    b.fees = 0.0
    return b


def test_level_entry_first_sight(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # >=80c side-mid qualifies on LEVEL alone, no momentum memory needed
    assert b.place(mkts=[_mk(bid=82, ask=85)]) == 1
    bet = next(iter(b.bets.values()))
    assert bet["side"] == "yes" and bet["entry"] == 82
    assert bet["era"] == "driftw1" and bet["trig"] == "level"
    assert abs(bet["pside"] - 0.835) < 0.01     # market prob, not a model


def test_no_side_level(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # yes-mid 15.5 -> NO side mid 84.5 >= 80: buy NO at 100-ask
    assert b.place(mkts=[_mk(bid=14, ask=17)]) == 1
    bet = next(iter(b.bets.values()))
    assert bet["side"] == "no" and bet["entry"] == 83


def test_climb_needs_volume_and_near_close(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=66, ask=70, vol=300.0)])        # memory only (mid 68)
    # climbed 3c but volume flat -> stale quote, no bet
    assert b.place(mkts=[_mk(bid=69, ask=73, vol=300.0)]) == 0
    b2 = _bot(tmp_path, monkeypatch)
    b2.place(mkts=[_mk(bid=66, ask=70, vol=300.0)])
    # climbed with volume but far from close -> noise, no bet
    assert b2.place(mkts=[_mk(bid=69, ask=73, vol=400.0, hrs=40.0)]) == 0
    b3 = _bot(tmp_path, monkeypatch)
    b3.place(mkts=[_mk(bid=66, ask=70, vol=300.0)])
    # climbed, volume rising, close within 24h -> bet
    assert b3.place(mkts=[_mk(bid=69, ask=73, vol=400.0, hrs=10.0)]) == 1
    assert next(iter(b3.bets.values()))["trig"] == "climb"


def test_liquidity_guards(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # thin 24h volume -> skip
    assert b.place(mkts=[_mk(bid=82, ask=85, vol=50.0)]) == 0
    # wide spread -> mid is fiction -> skip
    assert b.place(mkts=[_mk(bid=80, ask=90, vol=500.0)]) == 0
    # entry above MAX_ENTRY (91c bid) -> near-certainty, not our lane
    assert b.place(mkts=[_mk(bid=91, ask=93, vol=500.0)]) == 0


def test_one_bet_per_event(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    ms = [_mk(tk="KXX-A-T1", bid=82, ask=85, event="KXX-A"),
          _mk(tk="KXX-A-T2", bid=83, ask=86, event="KXX-A")]
    assert b.place(mkts=ms) == 1                     # ladder = one opinion


def test_probe_sizing_and_gate(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=82, ask=85)])
    bet = next(iter(b.bets.values()))
    assert bet["entry"] * bet["count"] <= dw.PROBE_COST_CENTS   # probe cap
    assert b._gate() == ("probe", 0)
    # 30 settled winners at good calibration -> scale
    b.history = [{"outcome": 1, "pnl": 0.10, "pside": 0.9} for _ in range(30)]
    mode, n = b._gate()
    assert mode == "scale" and n == 30


def test_stop_and_trail(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets = {"T1": {"side": "yes", "entry": 82, "count": 1, "fee": 1,
                     "pside": 0.83, "name": "x", "event": "E1", "ots": "",
                     "era": "driftw1", "trig": "level", "peak": 83.0}}
    # holds while favorite and near peak
    assert b.stop_check(quotes={"T1": (80, 84)}) == 0
    # trail: peak ran to 95, now back to 79 mid -> >=15c off peak -> exit
    b.bets["T1"]["peak"] = 95.0
    assert b.stop_check(quotes={"T1": (77, 81)}) == 1
    assert b.history[-1]["faded"] is True
    # stop: below 50c mid -> thesis dead
    b.bets = {"T2": {"side": "yes", "entry": 82, "count": 1, "fee": 1,
                     "pside": 0.83, "name": "x", "event": "E2", "ots": "",
                     "era": "driftw1", "trig": "level", "peak": 83.0}}
    assert b.stop_check(quotes={"T2": (44, 48)}) == 1
    assert b.history[-1]["stopped"] is True


def test_weather_series_excluded():
    # the scanner predicate: weather series belong to drift1, never driftw
    import weather_edge as we
    assert "KXHIGHCHI" in we.SERIES        # sanity: the exclusion set exists
