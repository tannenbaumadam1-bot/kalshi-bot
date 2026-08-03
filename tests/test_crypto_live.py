"""LANE 2 LIVE (clive1): taker-first crypto executor - allocation math,
entry band, stop, universe fence, DRY fills."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crypto_live as cl
import drift_live as dl


def _mk(tk="KXBTCD-26AUG0317-T64000", ev="KXBTCD-26AUG0317", bid=84, ask=86,
        vol=9999.0, hrs=1.0):
    return {"ticker": tk, "event": ev, "name": "btc " + tk, "yes_bid": bid,
            "yes_ask": ask, "vol": vol, "hrs": hrs}


def _bot(tmp_path, monkeypatch, peer_cost=0.0):
    monkeypatch.setattr(cl, "STATE", str(tmp_path / "c.json"))
    monkeypatch.setattr(cl, "BETS", str(tmp_path / "c.csv"))
    peer = tmp_path / "peer.json"
    peer.write_text(json.dumps(
        {"bets": {"W1": {"entry": peer_cost, "count": 1}}}))
    monkeypatch.setattr(cl, "PEER_STATE", str(peer))
    return cl.CryptoLive(None, mode="DRY")


def test_fifty_fifty_allocation(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch, peer_cost=2000)   # weather holds $20
    b.dry_balance_c = 8000                            # cash $80
    b.refresh_bank(b.balance_c())
    assert b.bank_c == 5000                           # (80+20)/2 = $50
    assert b._bet_cap_c() == 150                      # 3% of $50, floor $1.50
    assert b._open_cap_c() == 3000                    # 60%
    assert b._halt_c() == 500                         # 10%


def test_taker_entry_band_and_dry_fill(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000                           # bank $100 -> cap $3
    # 84/86: taker at the ASK, instant DRY fill, fee charged
    assert b.place(mkts=[_mk()]) == 1
    bet = b.bets["KXBTCD-26AUG0317-T64000"]
    assert bet["entry"] == 86 and bet["era"] == "clive1"
    assert b.fees_c > 0
    # ask above the 92c band ceiling: refused
    assert b.place(mkts=[_mk(tk="T2", ev="E2", bid=92, ask=94)]) == 0
    # ask below the 80c floor (mid 20-zone NO side is fine, but sub-80 no)
    assert b.place(mkts=[_mk(tk="T3", ev="E3", bid=74, ask=76)]) == 0
    # spread wider than 4c: SKIPPED, never joined (taker-first mandate)
    assert b.place(mkts=[_mk(tk="T4", ev="E4", bid=82, ask=88)]) == 0
    # NO side: mid 15 -> side no, entry ask = 100-bid
    assert b.place(mkts=[_mk(tk="T5", ev="E5", bid=13, ask=16)]) == 1
    assert b.bets["T5"]["side"] == "no" and b.bets["T5"]["entry"] == 87


def test_15_minute_series_excluded(tmp_path, monkeypatch):
    # 8/3: audition measured 15-min at ~zero edge; live trade lost
    assert cl._is_15m("KXETH15M-26AUG0314-T1890") is True
    assert cl._is_15m("KXNEARD-1", "NEAR price up in next 15 mins?") is True
    assert cl._is_15m("KXBTCD-26AUG0317-T64000", "Bitcoin price") is False
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    assert b.place(mkts=[_mk(tk="KXSOL15M-1", ev="E9")]) == 0   # excluded
    assert b.place(mkts=[_mk()]) == 1                           # hourly fine


def test_one_bet_per_event(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    ms = [_mk(tk="A1", ev="EV1", bid=84, ask=86),
          _mk(tk="A2", ev="EV1", bid=85, ask=87)]
    assert b.place(mkts=ms) == 1                      # ladder = one opinion


def test_stop_35_no_trail(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    b.place(mkts=[_mk()])
    tk = "KXBTCD-26AUG0317-T64000"
    b.bets[tk]["peak"] = 95.0
    monkeypatch.setattr(cl.dw.DriftWide, "_quotes",
                        lambda self, tks: {tk: (60, 62)})
    assert b.stop_check() == 0                        # deep fade: HOLD
    monkeypatch.setattr(cl.dw.DriftWide, "_quotes",
                        lambda self, tks: {tk: (30, 33)})
    assert b.stop_check() == 1                        # collapse: STOP
    assert b.history[-1]["stopped"] is True


def test_weather_book_fenced_from_crypto(tmp_path, monkeypatch):
    # the weather executor must never adopt / mirror crypto tickers
    assert dl._is_wx("KXHIGHNY-26AUG03-T81") is True
    assert dl._is_wx("KXBTCD-26AUG0317-T64000") is False
    assert dl._is_wx("KXETH-26AUG0317-B1890") is False
    assert dl._is_wx("") is False


def test_weather_caps_use_alloc_and_peer(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dl, "BETS", str(tmp_path / "b.csv"))
    cst = tmp_path / "cl.json"
    cst.write_text(json.dumps({"bets": {"C1": {"entry": 2000, "count": 1}}}))
    monkeypatch.setattr(dl, "CRYPTO_STATE_PATH", str(cst))
    b = dl.DriftLive(None, mode="DRY")
    b.dry_balance_c = 8000                            # cash $80 + crypto $20
    b._refresh_caps(b.balance_c())
    # account NAV $100, weather alloc 50% -> caps on $50
    assert b.max_bet_c == 200                         # 3% of 50 = 1.50 -> floor $2
    assert b.max_open_c == 3000                       # 60% of $50
    assert b.max_day_loss_c == 500                    # 10% of $50