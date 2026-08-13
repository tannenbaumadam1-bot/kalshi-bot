"""Mid-band paper book (midband1): entries, convergence exits, flatten,
turn grading. Paper-only - no venue calls in tests."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import midband_paper as mb

TODAY = "2026-08-13"


def _bot(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "STATE", str(tmp_path / "mb.json"))
    b = mb.MidbandPaper()
    return b


def _mk(tk="KXHIGHTNY-26AUG13-B85.5", bid=30, ask=32, city="new york",
        strike=85, kind="band", cap=86, is_low=False, hrs=8.0):
    return {"ticker": tk, "city": city, "is_low": is_low, "strike": strike,
            "kind": kind, "cap": cap, "yes_bid": bid, "yes_ask": ask,
            "date": TODAY, "hrs": hrs, "title": "", "sub": "",
            "bid_size": 50.0, "ask_size": 50.0, "vol": 100.0}


def _model(b, monkeypatch, p):
    monkeypatch.setattr(b, "band_prob", lambda mk, cache: p)


def test_entry_needs_band_edge_and_price_window(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    _model(b, monkeypatch, 0.45)              # model 45c vs 32c ask
    assert b.place([_mk()]) == 1
    pos = b.bets["KXHIGHTNY-26AUG13-B85.5"]
    assert pos["entry"] == 32 and pos["count"] == mb.SIZE
    assert pos["fair"] == 0.45 and pos["edge"] >= mb.EDGE_MIN_C
    # too rich for the lane: an 88c favorite is the LIVE book's trade
    b2 = _bot(tmp_path, monkeypatch)
    _model(b2, monkeypatch, 0.99)
    assert b2.place([_mk(tk="T2", bid=86, ask=88)]) == 0
    # no edge over the ask -> refused, and counted
    b3 = _bot(tmp_path, monkeypatch)
    _model(b3, monkeypatch, 0.33)
    assert b3.place([_mk(tk="T3")]) == 0
    assert b3.miss["thin_edge"] == 1


def test_one_opinion_per_city_and_date(tmp_path, monkeypatch):
    # adjacent-band stacking is what made the 8/12 Miami tail - this
    # book must not flatter itself with it
    b = _bot(tmp_path, monkeypatch)
    _model(b, monkeypatch, 0.45)
    assert b.place([_mk(tk="A", strike=85), _mk(tk="B", strike=87)]) == 1
    assert len(b.bets) == 1


def test_convergence_exit_books_a_turn(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    _model(b, monkeypatch, 0.45)
    b.place([_mk()])
    tk = next(iter(b.bets))
    # +40% target on a 32c entry = 45c bid
    b.exits([_mk(bid=44, ask=46)])
    assert tk in b.bets                        # not there yet
    b.exits([_mk(bid=45, ask=47)])
    assert tk not in b.bets
    assert b.turns["n"] == 1 and b.turns["wins"] == 1
    assert b.history[-1]["why"] == "target" and b.history[-1]["exit"] == 45
    assert b.realized_c > 0


def test_flatten_before_close_even_at_a_loss(tmp_path, monkeypatch):
    # never held to settlement: the whole point of the velocity build
    b = _bot(tmp_path, monkeypatch)
    _model(b, monkeypatch, 0.45)
    b.place([_mk()])
    tk = next(iter(b.bets))
    b.exits([_mk(bid=21, ask=23, hrs=0.5)])
    assert tk not in b.bets
    assert b.history[-1]["why"] == "flatten"
    assert b.turns["n"] == 1 and b.turns["wins"] == 0
    assert b.realized_c < 0                    # losses are booked honestly


def test_late_entries_refused(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    _model(b, monkeypatch, 0.60)
    assert b.place([_mk(hrs=0.5)]) == 0
    assert b.miss["too_late"] == 1


def test_summary_grades_turns_not_settlements(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.turns = {"n": 4, "net_c": 120.0, "wins": 1}
    b.save()
    s = json.load(open(mb.STATE))["summary"]
    assert s["turns"] == 4 and s["wins"] == 1 and s["losses"] == 3
    assert s["win_rate"] == 0.25               # mid-band loses often...
    assert s["per_turn"] == 0.3                # ...and still pays
    assert s["gate"]["need"] == mb.GATE_N and s["gate"]["ready"] is False
    assert s["mode"] == "PAPER"
