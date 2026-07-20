"""Momentum drift book (drift1): trigger, caps, sizing, settle, salvage exit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drift_paper as dp
import weather_paper as wp


def _mk(tk="KXHIGHNY-26JUL21-T86", bid=66, ask=70, city="new york",
        is_low=False, strike=87, kind="ge", cap=None, date="2026-07-21"):
    return {"ticker": tk, "city": city, "is_low": is_low, "strike": strike,
            "kind": kind, "cap": cap, "yes_bid": bid, "yes_ask": ask,
            "date": date, "hrs": 10.0, "title": "", "sub": "",
            "bid_size": 50.0, "ask_size": 50.0}


def _bot(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dp, "BETS", str(tmp_path / "b.csv"))
    b = dp.DriftPaper.__new__(dp.DriftPaper)
    b.start, b.cash = 10000, 10000.0
    b.bets, b.history, b.last_mid = {}, [], {}
    b.wins = b.losses = b.placed = 0
    b.fees = 0.0
    return b


def test_needs_two_scans_and_a_climb(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # first sight: high but no momentum memory -> no bet, memory recorded
    assert b.place(mkts=[_mk(bid=66, ask=70)]) == 0
    assert b.last_mid and not b.bets
    # second scan: climbed 3c -> bet YES at the bid (maker)
    assert b.place(mkts=[_mk(bid=69, ask=73)]) == 1
    bet = next(iter(b.bets.values()))
    assert bet["side"] == "yes" and bet["entry"] == 69
    assert bet["era"] == "drift1"
    assert abs(bet["pside"] - 0.71) < 0.01     # market prob, not a model


def test_flat_or_falling_never_triggers(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=66, ask=70)])
    assert b.place(mkts=[_mk(bid=66, ask=70)]) == 0     # flat
    assert b.place(mkts=[_mk(bid=62, ask=66)]) == 0     # falling favorite
    assert not b.bets


def test_no_side_momentum(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # yes-mid falling = NO side climbing
    b.place(mkts=[_mk(bid=28, ask=32)])                 # no-side mid 70
    assert b.place(mkts=[_mk(bid=24, ask=28)]) == 1     # no-mid 74, climbed 4
    bet = next(iter(b.bets.values()))
    assert bet["side"] == "no" and bet["entry"] == 100 - 28


def test_probe_stakes_and_event_cap(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    m1 = _mk(tk="A-T86", strike=87)
    m2 = _mk(tk="A-T88", strike=89)                     # same city-day event
    b.place(mkts=[m1, m2])
    up1 = dict(m1, yes_bid=69, yes_ask=73)
    up2 = dict(m2, yes_bid=69, yes_ask=73)
    assert b.place(mkts=[up1, up2]) == 1                # 1 bet per event only
    bet = next(iter(b.bets.values()))
    assert bet["count"] == 1          # probe: one contract when entry > 60c


def test_mid_zone_ignored(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=48, ask=52)])                 # 50c = no favorite
    assert b.place(mkts=[_mk(bid=52, ask=56)]) == 0
    assert not b.bets


def test_settle_math(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets = {"TK1": {"side": "yes", "entry": 70, "count": 1, "fee": 1,
                      "pside": 0.72, "city": "boston", "strike": 80,
                      "kind": "ge", "cap": None, "hl": "hi",
                      "date": "2026-07-21", "ots": "2026-07-21T10:00:00",
                      "era": "drift1"}}
    monkeypatch.setattr(dp, "fetch_result", lambda tk: "yes")
    b.settle()
    assert b.wins == 1 and not b.bets
    assert abs(b.history[-1]["pnl"] - 0.29) < 0.01      # 100-70-1 fee = +29c


# ---- salvage exit (weather book) ----

def _wbot():
    w = wp.WeatherPaper.__new__(wp.WeatherPaper)
    w.cash = 10000.0
    w.realized = 0.0
    w.fees = 0.0
    w.cooldown = {}
    w.history = []
    w.bets = {"KXHIGHDEN-26JUL21-T95": {
        "side": "yes", "entry": 35, "count": 3, "fee": 2, "pside": 0.52,
        "city": "denver", "strike": 95, "kind": "ge", "cap": None, "hl": "hi",
        "date": "2026-07-21", "ots": "2026-07-21T10:00:00", "era": "v7-obs"}}
    return w


def test_salvage_sells_immediately_when_model_agrees(monkeypatch):
    w = _wbot()
    # market crashed to 15c bid; model also says ~8% -> salvage on FIRST check
    monkeypatch.setattr(wp.WeatherPaper, "_reprice",
                        lambda self, *a, **k: (0.08, 0.35))
    w._quote = lambda tk: (15, 19)
    w.exit_check()
    assert len(w.bets) == 0
    h = w.history[-1]
    assert h["exited"] is True and h.get("salvaged") is True
    assert h["exit_px"] == 15


def test_no_salvage_when_model_still_believes(monkeypatch):
    w = _wbot()
    # cheap price but model says 45% vs market ~17% -> HOLD (underpriced)
    monkeypatch.setattr(wp.WeatherPaper, "_reprice",
                        lambda self, *a, **k: (0.45, 0.35))
    w._quote = lambda tk: (15, 19)
    w.exit_check()
    w.exit_check()
    assert len(w.bets) == 1


def test_salvage_threshold_respected(monkeypatch):
    w = _wbot()
    # 25c bid > SALVAGE_C 20 and model agrees it's weak -> normal path only
    # (needs 2 confirms), so after ONE check the position is still open
    monkeypatch.setattr(wp.WeatherPaper, "_reprice",
                        lambda self, *a, **k: (0.10, 0.35))
    w._quote = lambda tk: (25, 29)
    w.exit_check()
    assert len(w.bets) == 1


# ---- momentum stop (Adam 7/21: thesis dead below 50c -> cut the loss) ----

def _stop_bot(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets = {"TK1": {"side": "yes", "entry": 70, "count": 1, "fee": 1,
                      "pside": 0.72, "city": "boston", "strike": 80,
                      "kind": "ge", "cap": None, "hl": "hi",
                      "date": "2026-07-21", "ots": "2026-07-21T10:00:00",
                      "era": "drift1"}}
    return b


def test_stop_cuts_loss_below_50(tmp_path, monkeypatch):
    b = _stop_bot(tmp_path, monkeypatch)
    cash0 = b.cash
    assert b.stop_check(quotes={"TK1": (44, 48)}) == 1   # mid 46 < 50
    assert not b.bets
    h = b.history[-1]
    assert h["stopped"] is True and h["outcome"] is None
    assert h["exit_px"] == 44
    # loss capped ~ entry-bid+fees, NOT the full 70c ride to zero
    assert -0.30 < h["pnl"] < -0.20
    assert b.cash > cash0                                # salvage cash recovered


def test_stop_holds_while_still_favorite(tmp_path, monkeypatch):
    b = _stop_bot(tmp_path, monkeypatch)
    assert b.stop_check(quotes={"TK1": (53, 57)}) == 0   # mid 55 >= 50 -> hold
    assert len(b.bets) == 1


def test_stop_works_for_no_side(tmp_path, monkeypatch):
    b = _stop_bot(tmp_path, monkeypatch)
    b.bets["TK1"].update({"side": "no", "entry": 70})
    # yes mid 62 -> our NO side mid 38 < 50 -> stop, sell NO at 100-ask
    assert b.stop_check(quotes={"TK1": (60, 64)}) == 1
    assert b.history[-1]["exit_px"] == 100 - 64


def test_stopped_rows_excluded_from_gate(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.history = [{"outcome": None, "stopped": True, "pnl": -0.25,
                  "pside": 0.7}] * 40
    mode, n = b._gate()
    assert n == 0 and mode == "probe"
