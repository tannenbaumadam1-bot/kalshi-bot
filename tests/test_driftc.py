"""LANE 2 audition (driftc1): crypto drift paper book - config, floor,
trail-off, stop, and config isolation from drift_wide."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drift_crypto as dc
import drift_wide as dw


def _mk(tk="KXBTCD-26AUG0117-T64000", ev="KXBTCD-26AUG0117", bid=82, ask=84,
        vol=9999.0, hrs=1.0):
    return {"ticker": tk, "event": ev, "name": "btc " + tk, "yes_bid": bid,
            "yes_ask": ask, "vol": vol, "hrs": hrs}


def _bot(tmp_path, monkeypatch):
    monkeypatch.setitem(dc._CFG, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setitem(dc._CFG, "BETS", str(tmp_path / "b.csv"))
    return dc.DriftCrypto()


def test_config_is_live_book_evidence(tmp_path, monkeypatch):
    assert dc._CFG["ERA"] == "driftc1"
    assert dc._CFG["CATEGORIES"] == {"Crypto"}
    assert dc._CFG["ENTRY_MIN_C"] == 80          # 7/31: sub-80c killed live
    assert dc._CFG["STOP_C"] == 35               # 7/28: 50c stops were leaks
    assert dc._CFG["FADE_DROP_C"] >= 900         # trail OFF
    assert dc._CFG["MAX_H"] <= 24                # convergence territory only
    assert dc._CFG["MAX_SPREAD_C"] <= 3
    assert dc.GATE_TARGET == 100


def test_config_never_leaks_into_drift_wide(tmp_path, monkeypatch):
    before = (dw.ERA, dw.STOP_C, dw.FADE_DROP_C, dw.ENTRY_MIN_C,
              dw.CATEGORIES, dw.STATE)
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk()])
    b.stop_check(quotes={})
    b.save()
    assert (dw.ERA, dw.STOP_C, dw.FADE_DROP_C, dw.ENTRY_MIN_C,
            dw.CATEGORIES, dw.STATE) == before   # restored after every call


def test_entry_floor_80(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # mid 80.5 but bid 79: below the entry floor -> refused
    assert b.place(mkts=[_mk(bid=79, ask=82)]) == 0
    # spread 4c > 3c cap -> refused
    assert b.place(mkts=[_mk(tk="T2", ev="E2", bid=82, ask=86)]) == 0
    # clean 82c level entry -> placed, fee charged
    assert b.place(mkts=[_mk(tk="T3", ev="E3", bid=82, ask=84)]) == 1
    bet = b.bets["T3"]
    assert bet["entry"] == 82 and bet["era"] == "driftc1"
    assert b.fees > 0                            # fee-inclusive from bet one


def test_trail_off_stop_35(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    assert b.place(mkts=[_mk(tk="T4", ev="E4", bid=82, ask=84)]) == 1
    b.bets["T4"]["peak"] = 95.0
    assert b.stop_check(quotes={"T4": (60, 62)}) == 0    # 35c-deep fade: HOLD
    assert b.stop_check(quotes={"T4": (44, 46)}) == 0    # sub-50 wobble: HOLD
    assert b.stop_check(quotes={"T4": (30, 33)}) == 1    # collapse: STOP
    assert b.history[-1]["stopped"] is True


def test_state_isolated_from_driftw(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(tk="T5", ev="E5", bid=82, ask=84)])
    b.save()
    st = str(tmp_path / "s.json")
    import json
    d = json.load(open(st))
    assert d["era"] == "driftc1"
    assert d["summary"]["gate"] == "probe"       # fresh book probes
