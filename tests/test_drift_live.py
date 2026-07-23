"""Drift LIVE executor (dlive1): modes, caps, triggers, exits, DRY fills."""
import os
import sys
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drift_live as dl

TODAY = _dt.date.today().isoformat()


def _mk(tk="KXHIGHNY-26JUL-T86", bid=82, ask=85, city="new york",
        is_low=False, strike=87, kind="ge", cap=None, date=None, vol=100.0):
    return {"ticker": tk, "city": city, "is_low": is_low, "strike": strike,
            "kind": kind, "cap": cap, "yes_bid": bid, "yes_ask": ask,
            "date": date or TODAY, "hrs": 10.0, "title": "", "sub": "",
            "bid_size": 50.0, "ask_size": 50.0, "vol": vol}


def _bot(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dl, "BETS", str(tmp_path / "b.csv"))
    b = dl.DriftLive(None, mode="DRY")
    return b


def test_dry_default_and_caps(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    assert b.client is None and b.mode == "DRY"
    assert b.max_bet_c >= 100 and b.max_open_c > b.max_bet_c
    assert b.dry_balance_c == 10000


def test_level_entry_dry_fills(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    n = b.place(mkts=[_mk(bid=82, ask=85)])
    assert n == 1
    # DRY: resting order promoted to a position instantly
    assert not b.pending and len(b.bets) == 1
    bet = next(iter(b.bets.values()))
    assert bet["side"] == "yes" and bet["entry"] == 82
    assert bet["era"] == "dlive1" and bet["trig"] == "level"
    assert bet["entry"] * bet["count"] <= b.max_bet_c
    assert b.dry_balance_c < 10000


def test_no_nickel_lane_in_live(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # side-mid 96 (nickel zone in paper) -> live v1 skips entirely
    assert b.place(mkts=[_mk(bid=95, ask=97)]) == 0
    assert not b.bets


def test_climb_needs_confirmation(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=66, ask=70, vol=300.0)])       # memory only
    assert not b.bets
    # climb on rising volume, same-day -> maker entry
    assert b.place(mkts=[_mk(bid=69, ask=73, vol=400.0)]) == 1
    assert next(iter(b.bets.values()))["trig"] == "climb"


def test_open_cap_blocks(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.max_open_c = 200          # tiny cap: one probe bet only
    ms = [_mk(tk=f"KXHIGHNY-26JUL-T{i}", bid=82, ask=85,
              city=f"c{i}", strike=80 + i) for i in range(4)]
    b.place(mkts=ms)
    assert b.open_cost_c() <= 200 + b.max_bet_c


def test_daily_halt(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.day_pnl_c = -b.max_day_loss_c
    assert b.place(mkts=[_mk(bid=82, ask=85)]) == 0
    assert b.halted


def test_stop_and_trail_dry(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets = {"T1": {"side": "yes", "entry": 82, "count": 1, "fee": 1,
                     "pside": 0.83, "city": "x", "strike": 1, "kind": "ge",
                     "cap": None, "hl": "hi", "date": TODAY, "ots": "",
                     "era": "dlive1", "trig": "level", "peak": 83.0}}
    assert b.stop_check(quotes={"T1": (80, 84)}) == 0     # healthy: hold
    b.bets["T1"]["peak"] = 95.0
    assert b.stop_check(quotes={"T1": (77, 81)}) == 1     # trail exit
    assert b.history[-1]["faded"] is True
    b.bets = {"T2": {"side": "yes", "entry": 82, "count": 1, "fee": 1,
                     "pside": 0.83, "city": "x", "strike": 1, "kind": "ge",
                     "cap": None, "hl": "hi", "date": TODAY, "ots": "",
                     "era": "dlive1", "trig": "level", "peak": 83.0}}
    assert b.stop_check(quotes={"T2": (44, 48)}) == 1     # momentum stop
    assert b.history[-1]["stopped"] is True


def test_gate_probe_until_30(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    assert b._gate() == ("probe", 0)
    b.history = [{"outcome": 1, "pnl": 0.10, "pside": 0.9} for _ in range(30)]
    assert b._gate() == ("scale", 30)


def test_build_is_dry_without_key(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dl, "CONFIG", str(tmp_path / "nope.yaml"))
    monkeypatch.delenv("KALSHI_DRIFT_LIVE", raising=False)
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    b = dl.build()
    assert b.mode == "DRY" and b.client is None
