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
    assert b._bet_cap_c() == 300                      # 6% boost of $50
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
    assert b.max_bet_c == 300                         # 6% boost of $50
    assert b.max_open_c == 3000                       # 60% of $50
    assert b.max_day_loss_c == 500                    # 10% of $50

def test_kelly_ladder_compounds_on_evidence(tmp_path, monkeypatch):
    # gate lowered 100 -> 25 on 8/4 (Adam override at 34 settled 33W/1L);
    # the net-positive guard and auto-downgrade are the invariants
    b = _bot(tmp_path, monkeypatch)
    assert b._kelly() == cl.KELLY                     # unproven: quarter
    b.wins, b.losses, b.realized_c = 20, 4, 500.0
    assert b._kelly() == cl.KELLY                     # n=24: not yet
    b.wins = 21
    assert b._kelly() == cl.KELLY_PROVEN              # 25 settled, net>0
    b.realized_c = -1.0
    assert b._kelly() == cl.KELLY                     # net<0: auto-revert


def test_bank_compounds_with_account_nav(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch, peer_cost=0)
    b.dry_balance_c = 10000
    b.refresh_bank(b.balance_c())
    cap1 = b._bet_cap_c()
    b.dry_balance_c = 14000                           # account grew 40%
    b.refresh_bank(b.balance_c())
    assert b.bank_c == 7000 and b._bet_cap_c() > cap1  # caps grew with it


def test_daily_pnl_ledger_never_trims(tmp_path, monkeypatch):
    # 8/3: weekly/monthly perf derives from pnl_days, which must survive
    # the history[-120:] trim - one float per date, forever
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    b.place(mkts=[_mk()])
    tk = "KXBTCD-26AUG0317-T64000"
    monkeypatch.setattr(cl.dw.DriftWide, "_quotes",
                        lambda self, tks: {tk: (30, 33)})
    assert b.stop_check() == 1
    d = cl.today()
    assert d in b.pnl_days
    assert abs(b.pnl_days[d] - b.history[-1]["pnl"]) < 0.005
    # backfill: a reloaded book with history but no ledger seeds itself
    b.save()
    b2 = cl.CryptoLive(None, mode="DRY")
    b2.pnl_days = {}
    b2.load()
    assert d in b2.pnl_days and abs(b2.pnl_days[d] - b.pnl_days[d]) < 0.005


def test_bet_pct_boost_reverts_at_300_nav(tmp_path, monkeypatch):
    # 8/4 Adam: 6%/bet while account NAV < $300, standard 3% after.
    # The handoff is a step DOWN in bet dollars - that is the design.
    b = _bot(tmp_path, monkeypatch, peer_cost=0)
    b.dry_balance_c = 29900                           # $299: boosted
    b.refresh_bank(b.balance_c())
    assert b._bet_pct() == cl.BET_PCT_BOOST
    assert b._bet_cap_c() == int(29900 * 0.5 * 0.06)  # ~$8.97
    b.dry_balance_c = 30000                           # $300: reverts
    b.refresh_bank(b.balance_c())
    assert b._bet_pct() == cl.BET_PCT
    assert b._bet_cap_c() == int(30000 * 0.5 * 0.03)  # $4.50
    # weather book steps at the same account-NAV threshold
    import drift_live as dl2
    monkeypatch.setattr(dl2, "STATE", str(tmp_path / "w.json"))
    monkeypatch.setattr(dl2, "BETS", str(tmp_path / "w.csv"))
    monkeypatch.setattr(dl2, "CRYPTO_STATE_PATH", str(tmp_path / "none.json"))
    w = dl2.DriftLive(None, mode="DRY")
    w.dry_balance_c = 29900
    w._refresh_caps(w.balance_c())
    assert w.max_bet_c == int(29900 * 0.5 * 0.06)
    w.dry_balance_c = 30000
    w._refresh_caps(w.balance_c())
    assert w.max_bet_c == int(30000 * 0.5 * 0.03)
