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
    # ask above the 88c ceiling (8/10): the hi band is structurally
    # retired - no real order, but the signal lands in the paper-shadow
    # book so the evidence keeps accumulating at zero cost
    assert b.place(mkts=[_mk(tk="T2", ev="E2", bid=92, ask=94)]) == 0
    assert "T2" not in b.bets
    assert b.shadow["T2"]["band"] == "hi" and b.shadow["T2"]["entry"] == 94
    # above the 96c old probe ceiling: refused outright, not even shadowed
    assert b.place(mkts=[_mk(tk="T2b", ev="E2b", bid=96, ask=98)]) == 0
    assert "T2b" not in b.shadow
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
    # 8/7: dedup is per (coin, settlement hour) - a strike ladder on one
    # coin/hour is still ONE opinion however many event tickers it spans
    ms = [_mk(tk="KXBTC-26AUG0317-B64000", ev="KXBTC-26AUG0317",
              bid=84, ask=86),
          _mk(tk="KXBTCD-26AUG0317-T63999", ev="KXBTCD-26AUG0317",
              bid=85, ask=87)]
    assert b.place(mkts=ms) == 1


def test_stop_35_no_trail(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "STOP_ON", True)
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
    monkeypatch.setattr(cl, "STOP_ON", True)
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


def test_hi_band_probe_ledger(tmp_path, monkeypatch):
    # 8/10: the hi band is structurally retired - its signals go to the
    # PAPER-SHADOW book, settle virtually into the gate ledger's s*
    # counters, and never touch the money ledgers
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    assert b.place(mkts=[_mk(tk="H1", ev="EH1", bid=93, ask=95)]) == 0
    assert b.shadow["H1"]["band"] == "hi"
    assert b.place(mkts=[_mk(tk="C1", ev="EC1", bid=84, ask=86)]) == 1
    assert b.bets["C1"]["band"] == "core"
    monkeypatch.setattr(cl, "fetch_result", lambda tk: "yes")
    b.shadow["H1"]["side"] = "yes"
    b.bets["C1"]["side"] = "yes"
    realized_before = b.realized_c
    b.settle()
    # shadow win lands ONLY in the gate ledger's shadow counters
    assert b.hi_g.get("sw") == 1 and b.hi_g.get("sl", 0) == 0
    assert b.hi_g.get("spnl", 0) > 0
    assert b.hi["w"] == 0                       # money ledger untouched
    assert "H1" not in b.shadow                 # resolved and cleared
    # the core win is real money and carries its breakeven for the gate
    assert b.core["w"] == 1 and b.core_g.get("ben", 0) > 0
    assert b.realized_c > realized_before
    # shadow + gate ledgers survive a save/load round-trip
    b.place(mkts=[_mk(tk="H2", ev="EH2", bid=93, ask=95)])
    b.save()
    b2 = cl.CryptoLive(None, mode="DRY")
    assert b2.hi_g.get("sw") == 1 and "H2" in b2.shadow


def test_hi_ladder_steps_on_evidence(tmp_path, monkeypatch):
    # 8/5 hi-band size ladder: base pct until 10 settled NET-POSITIVE
    # (8%), 10% at 20 - and straight back to base if net goes negative
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000                           # bank $100, NAV<$300
    b.refresh_bank(b.balance_c())
    assert b._hi_pct() == cl.BET_PCT_BOOST            # 0 settled: base 6%
    b.hi = {"w": 9, "l": 0, "pnl": 0.55}
    b.hi_g = dict({"w": 9, "l": 0, "pnl": 0.55})
    assert b._hi_pct() == cl.BET_PCT_BOOST            # 9 settled: not yet
    b.hi = {"w": 10, "l": 0, "pnl": 0.6}
    b.hi_g = dict({"w": 10, "l": 0, "pnl": 0.6})
    assert b._hi_pct() == cl.HI_PCT1                  # 10 net+: 8%
    assert b._hi_cap_c() == int(b.bank_c * cl.HI_PCT1)
    b.hi = {"w": 19, "l": 1, "pnl": 0.7}
    b.hi_g = dict({"w": 19, "l": 1, "pnl": 0.7})
    assert b._hi_pct() == cl.HI_PCT2                  # 20 net+: full 10%
    b.hi = {"w": 19, "l": 1, "pnl": -0.10}
    b.hi_g = dict({"w": 19, "l": 1, "pnl": -0.10})
    assert b._hi_pct() == cl.BET_PCT_BOOST            # net<=0: raise revoked
    b.hi = {"w": 20, "l": 0, "pnl": 0.0}
    b.hi_g = dict({"w": 20, "l": 0, "pnl": 0.0})
    assert b._hi_pct() == cl.BET_PCT_BOOST            # zero net is not proof
    # ladder never sizes BELOW base: at >=$300 NAV base is 3%, steps hold
    b.dry_balance_c = 40000
    b.refresh_bank(b.balance_c())
    b.hi = {"w": 10, "l": 0, "pnl": 0.6}
    b.hi_g = dict({"w": 10, "l": 0, "pnl": 0.6})
    assert b._hi_pct() == cl.HI_PCT1
    # core band is untouched by the hi ledger
    assert b._bet_pct() == cl.BET_PCT


def test_hi_block_closes_lane_when_proven_negative(tmp_path, monkeypatch):
    # 8/10 Wilson gate: a lane closes when even the OPTIMISTIC read of
    # its win rate (Wilson upper bound, z=1) sits below its own
    # fee-adjusted breakeven - or when a big sample is $-negative.
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    # legacy ledger (no breakeven data): old pnl<0-at-n>=8 rule holds
    b.core_g = {"w": 3, "l": 5, "pnl": -1.20}
    assert b._core_blocked()
    assert b.place(mkts=[_mk(tk="CB1", ev="ECB1", bid=84, ask=86)]) == 0
    assert "CB1" in b.shadow                    # blocked lane shadows
    # the g3 injustice, replayed: 9-1 slightly $-negative at avg
    # breakeven 87% - Wilson UB ~0.92 clears it, so the lane STAYS OPEN
    b.core_g = {"w": 9, "l": 1, "pnl": -0.22, "ben": 8.7}
    assert not b._core_blocked()
    assert b.place(mkts=[_mk(tk="CB2", ev="ECB2", bid=84, ask=86)]) == 1
    # pure Wilson block: UB ~0.67 far under an 86% breakeven, n<40
    b.core_g = {"w": 10, "l": 8, "pnl": -2.0, "ben": 15.5}
    assert b._core_blocked()
    # dollar backstop: the real g3 hi lane - 161-9 looks fine to Wilson
    # (UB ~0.962 vs be 0.957) but a 170-bet sample that is still
    # $-negative blocks regardless
    b.hi_g = {"w": 161, "l": 9, "pnl": -4.69, "ben": 162.69}
    assert b._hi_blocked()
    # gate state is published for the tracker
    b.core_g = {"w": 2, "l": 3, "pnl": -0.60, "ben": 4.3}
    b.save()
    d = json.load(open(cl.STATE))
    hi = d["summary"]["hi"]
    assert hi["blocked"] is True and d["summary"]["core"]["blocked"] is False
    assert hi["n1"] == cl.HI_STEP1_N and hi["n2"] == cl.HI_STEP2_N


def test_hourly_markets_pass_without_vol24(tmp_path, monkeypatch):
    # 8/6: Kalshi lists hourlies only ~60min before close -> vol24 ~0.
    # Within HOURLY_H of close the volume floor drops away; the spread
    # gate stays. Far-from-close markets still need the 500 floor.
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    # young hourly, zero volume, tight book, 47 min to close: TRADES
    assert b.place(mkts=[_mk(tk="HR1", ev="EHR1", bid=84, ask=86,
                             vol=0.0, hrs=0.78)]) == 1
    # same zero volume but 5h out (a daily going stale): still refused
    assert b.place(mkts=[_mk(tk="HR2", ev="EHR2", bid=84, ask=86,
                             vol=0.0, hrs=5.0)]) == 0
    # near close does NOT relax the spread gate
    assert b.place(mkts=[_mk(tk="HR3", ev="EHR3", bid=80, ask=86,
                             vol=0.0, hrs=0.5)]) == 0


def test_direct_series_fetch_parses_and_filters(tmp_path, monkeypatch):
    # 8/6: crypto universe comes from per-series /markets calls (the
    # global sweep missed short-lived hourlies). Parse + hrs filter.
    calls = []

    class _Resp:
        def __init__(self, mks):
            self._m = mks

        def json(self):
            return {"markets": self._m}

    def fake_get(url, params=None, timeout=None):
        calls.append(params["series_ticker"])
        if params["series_ticker"] != "KXBTCD":
            return _Resp([])
        import datetime as _dt
        nowdt = _dt.datetime.now(_dt.timezone.utc)
        soon = (nowdt + _dt.timedelta(minutes=40)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        far = (nowdt + _dt.timedelta(hours=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return _Resp([
            {"ticker": "KXBTCD-26AUG0614-T64000", "event_ticker":
             "KXBTCD-26AUG0614", "title": "Bitcoin price",
             "yes_sub_title": "$64,000 or above", "close_time": soon,
             "yes_bid_dollars": "0.84", "yes_ask_dollars": "0.86",
             "volume_24h_fp": "0"},
            {"ticker": "KXBTCD-26AUG0817-T64000", "event_ticker":
             "KXBTCD-26AUG0817", "title": "too far out",
             "close_time": far, "yes_bid_dollars": "0.85",
             "yes_ask_dollars": "0.86", "volume_24h_fp": "9999"},
            {"ticker": "KXBTCD-BAD", "title": "bad close",
             "close_time": "nope", "yes_bid_dollars": "0.85",
             "yes_ask_dollars": "0.86"},
        ])

    monkeypatch.setattr(cl.requests, "get", fake_get)
    mkts = cl.fetch_crypto_mkts()
    assert calls == cl.CRYPTO_SERIES          # every series asked, by name
    assert [m["ticker"] for m in mkts] == ["KXBTCD-26AUG0614-T64000"]
    m = mkts[0]
    assert m["yes_bid"] == 84 and m["yes_ask"] == 86
    assert m["event"] == "KXBTCD-26AUG0614" and 0 < m["hrs"] < 1
    # and the young zero-volume hourly TRADES end to end via place(None)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: mkts)
    assert b.place() == 1
    assert b.bets["KXBTCD-26AUG0614-T64000"]["band"] == "core"


# --- 8/7 truth fix: stop-outs are realized outcomes -------------------

def _stopped(monkeypatch, bid=12, ask=16):
    """Force every quote to a collapsed book so stop_check fires.
    8/7: the stop is retired by default, so these tests opt back in."""
    monkeypatch.setattr(cl, "STOP_ON", True)
    monkeypatch.setattr(cl.dw.DriftWide, "_quotes",
                        lambda self, tks: {t: (bid, ask) for t in tks})

    class _N:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(cl.dcfg, "_cfg", lambda: _N())


def test_stop_counts_as_a_loss(tmp_path, monkeypatch):
    """The old stop path never touched wins/losses - the headline record
    read 125-1 while six positions had been stopped out for -$7.33."""
    b = _bot(tmp_path, monkeypatch)
    _stopped(monkeypatch)
    b.bets = {"KXT": {"side": "yes", "count": 3, "entry": 88, "pside": .88,
                      "fee": 0, "band": "core", "ots": "x", "name": "t"}}
    assert b.stop_check() == 1
    assert (b.wins, b.losses, b.stops) == (0, 1, 1)
    assert b.core["l"] == 1 and b.core["pnl"] < 0


def test_stop_routes_to_its_own_band(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    _stopped(monkeypatch)
    b.bets = {"KXH": {"side": "yes", "count": 3, "entry": 95, "pside": .95,
                      "fee": 0, "band": "hi", "ots": "x", "name": "h"}}
    b.stop_check()
    assert b.hi["l"] == 1 and b.core["l"] == 0 and b.losses == 1


def test_profitable_exit_counts_as_a_win(tmp_path, monkeypatch):
    """Realized outcomes count by P&L sign, not by exit reason."""
    b = _bot(tmp_path, monkeypatch)
    _stopped(monkeypatch, bid=30, ask=32)
    b.bets = {"KXP": {"side": "yes", "count": 3, "entry": 10, "pside": .30,
                      "fee": 0, "band": "core", "ots": "x", "name": "p"}}
    b.stop_check()
    assert (b.wins, b.losses) == (1, 0) and b.core["w"] == 1


def test_core_ledger_backfills_once_and_survives_restarts(tmp_path,
                                                          monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.wins, b.losses = 10, 1
    b.history = [
        {"pnl": 0.5, "band": "core"},
        {"pnl": -0.7, "band": "core", "stopped": True},
        {"pnl": -1.2, "band": "core", "stopped": True},
        {"pnl": 0.3, "band": "hi"},
        {"pnl": None, "band": "core"},          # unsettled: ignored
    ]
    b.core = {"w": 0, "l": 0, "pnl": 0.0}       # pre-fix state
    b.stops = 0
    b.load()
    assert b.core["w"] == 1 and b.core["l"] == 2
    assert b.core["pnl"] == -1.4                # hi row excluded
    assert b.stops == 2
    assert b.losses == 3                        # 1 + the 2 uncounted stops
    for _ in range(3):                          # restart loop
        b.save(balance_c=1)
        b = cl.CryptoLive(None, mode="DRY")
        assert (b.losses, b.stops) == (3, 2), "backfill double-counted"


def test_core_band_gate_closes_a_proven_negative_lane(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.core = {"w": 2, "l": 6, "pnl": -3.0, "bf": 1}
    b.core_g = dict({"w": 2, "l": 6, "pnl": -3.0, "bf": 1})
    assert b._core_blocked() is True
    b.core = {"w": 2, "l": 6, "pnl": 1.0, "bf": 1}
    b.core_g = dict({"w": 2, "l": 6, "pnl": 1.0, "bf": 1})
    assert b._core_blocked() is False           # negative net required
    b.core = {"w": 0, "l": 4, "pnl": -3.0, "bf": 1}
    b.core_g = dict({"w": 0, "l": 4, "pnl": -3.0, "bf": 1})
    assert b._core_blocked() is False           # n < CORE_BLOCK_N


def test_blocked_core_band_places_nothing(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    b.core = {"w": 2, "l": 6, "pnl": -3.0, "bf": 1}
    b.core_g = dict({"w": 2, "l": 6, "pnl": -3.0, "bf": 1})
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: [_mk()])
    assert b.place() == 0 and not b.bets


# --- 8/7: daily order cap retired + ET trading day ---------------------

def test_trading_day_rolls_at_midnight_et_not_utc(monkeypatch):
    """00:00-04:00 UTC is still the PREVIOUS ET day. Under the old UTC
    boundary the counter refilled at 8pm ET and the evening hourlies ate
    the whole budget before the US daytime markets ever opened."""
    import datetime as _dt

    class _FakeDT(_dt.datetime):
        _now = None

        @classmethod
        def now(cls, tz=None):
            return cls._now.astimezone(tz) if tz else cls._now

    monkeypatch.setattr(cl.datetime, "datetime", _FakeDT)

    # 01:30 UTC on Aug 8 == 21:30 ET on Aug 7 -> still Aug 7
    _FakeDT._now = _dt.datetime(2026, 8, 8, 1, 30, tzinfo=_dt.timezone.utc)
    assert cl.today() == "2026-08-07"
    # 05:30 UTC on Aug 8 == 01:30 ET on Aug 8 -> now it rolls
    _FakeDT._now = _dt.datetime(2026, 8, 8, 5, 30, tzinfo=_dt.timezone.utc)
    assert cl.today() == "2026-08-08"
    # midday ET is unambiguous either way
    _FakeDT._now = _dt.datetime(2026, 8, 7, 16, 0, tzinfo=_dt.timezone.utc)
    assert cl.today() == "2026-08-07"


def test_no_daily_cap_so_a_busy_morning_cannot_blind_the_afternoon(
        tmp_path, monkeypatch):
    """39 bets already opened today used to leave 1 slot for the rest of
    the day. With MAX_PER_DAY=0 the day's history is irrelevant."""
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 50000
    b.history = [{"ots": cl.today() + "T01:00:00", "pnl": 0.1}
                 for _ in range(39)]
    monkeypatch.setattr(cl, "MAX_PER_DAY", 0)
    monkeypatch.setattr(cl, "MAX_PER_CYCLE", 15)
    mkts = [_mk(tk=f"KXBTCD-26AUG07{h:02d}-T64000",
                ev=f"KXBTCD-26AUG07{h:02d}") for h in range(10, 16)]
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: mkts)
    assert b._placed_today() == 39
    assert b.place() == len(mkts)          # every distinct event taken


def test_per_cycle_ceiling_still_stops_a_runaway(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 500000
    monkeypatch.setattr(cl, "MAX_PER_DAY", 0)
    monkeypatch.setattr(cl, "MAX_PER_CYCLE", 5)
    mkts = [_mk(tk=f"KXBTCD-26AUG07{h:02d}-T64000",
                ev=f"KXBTCD-26AUG07{h:02d}") for h in range(0, 40)]
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: mkts)
    assert b.place() == 5


def test_explicit_daily_cap_is_still_honoured_if_set(tmp_path, monkeypatch):
    """The env override has to keep working - it's the rollback path."""
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 50000
    monkeypatch.setattr(cl, "MAX_PER_DAY", 3)
    b.history = [{"ots": cl.today() + "T01:00:00", "pnl": 0.1}]
    mkts = [_mk(tk=f"KXBTCD-26AUG07{h:02d}-T64000",
                ev=f"KXBTCD-26AUG07{h:02d}") for h in range(10, 16)]
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: mkts)
    assert b.place() == 2                  # 3 - 1 already opened today


# --- 8/7: crypto miss autopsy (the book had none) ---------------------

def test_expired_crypto_order_is_logged_as_a_miss(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.pending = {"o1": {"ticker": "KXBTCD-X", "side": "yes", "entry": 88,
                        "count": 3, "pside": .88, "band": "core",
                        "filled_seen": 0,
                        "ots": "2020-01-01T00:00:00"}}      # ancient
    b.check_orders()
    assert b.miss and b.miss[-1]["why"] == "rest_expired"
    assert b.miss[-1]["count"] == 3 and not b.pending


def test_crypto_miss_is_graded_against_settlement(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b._log_miss({"ticker": "KXBTCD-Y", "side": "yes", "entry": 90,
                 "count": 2, "pside": .9, "band": "hi"}, 2, "rest_expired")
    monkeypatch.setattr(cl, "fetch_result", lambda tk: "yes")
    b.miss_check()
    r = b.miss[-1]
    assert r["res"] == "yes" and r["would_pnl"] > 0
    s = b._miss_summary()
    assert s["miss_n"] == 1 and s["miss_would_won"] == 1
    assert s["miss_why"]["rest_expired"]["n"] == 1


def test_crypto_miss_ledger_survives_a_restart(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b._log_miss({"ticker": "KXBTCD-Z", "side": "no", "entry": 84,
                 "count": 1, "pside": .84, "band": "core"}, 1, "order_vanished")
    b.save(balance_c=1)
    b2 = cl.CryptoLive(None, mode="DRY")
    assert len(b2.miss) == 1 and b2.miss[0]["why"] == "order_vanished"


# --- 8/7: the doubling bug, the retired stop, the gate era ------------

def test_band_and_threshold_markets_share_one_underlying_key():
    """The 8/7 loss: KXETH (bands) and KXETHD (thresholds) are separate
    EVENTS but the same coin at the same instant."""
    assert (cl.underlying_key("KXETH-26AUG0713-B1907")
            == cl.underlying_key("KXETHD-26AUG0713-T1909.99")
            == ("ETH", "26AUG0713"))
    assert (cl.underlying_key("KXSOLE-26AUG0713-B74.375")
            == cl.underlying_key("KXSOLD-26AUG0713-T73.7499"))
    assert (cl.underlying_key("KXDOGE-26AUG0713-B0.072")
            == cl.underlying_key("KXDOGED-26AUG0713-T0.0699999"))
    # different HOURS on one coin stay independent
    assert (cl.underlying_key("KXXRPD-26AUG0713-T1.0199")
            != cl.underlying_key("KXXRPD-26AUG0717-T1.0199"))
    # an unknown series never silently merges with a known one
    assert cl.underlying_key("KXNEWCOIN-26AUG0713-T1")[0] != "BTC"


def test_only_one_bet_per_coin_hour(tmp_path, monkeypatch):
    """Replays the exact 8/7 pairs: the book must take ONE of each."""
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 50000
    mkts = [_mk(tk="KXETH-26AUG0713-B1907", ev="KXETH-26AUG0713"),
            _mk(tk="KXETHD-26AUG0713-T1909.99", ev="KXETHD-26AUG0713"),
            _mk(tk="KXXRP-26AUG0713-B1.02", ev="KXXRP-26AUG0713"),
            _mk(tk="KXXRPD-26AUG0713-T1.0199", ev="KXXRPD-26AUG0713"),
            _mk(tk="KXXRPD-26AUG0717-T1.0199", ev="KXXRPD-26AUG0717")]
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: mkts)
    assert b.place() == 3               # ETH 1pm, XRP 1pm, XRP 5pm
    held = list(b.bets) + [o["ticker"] for o in b.pending.values()]
    keys = {cl.underlying_key(t) for t in held}
    assert keys == {("ETH", "26AUG0713"), ("XRP", "26AUG0713"),
                    ("XRP", "26AUG0717")}


def test_existing_position_blocks_the_other_market_on_that_coin(
        tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 50000
    b.bets = {"KXETH-26AUG0713-B1907": {"side": "no", "entry": 96,
                                        "count": 3, "pside": .96,
                                        "band": "hi", "fee": 0}}
    monkeypatch.setattr(cl, "fetch_crypto_mkts",
                        lambda: [_mk(tk="KXETHD-26AUG0713-T1909.99",
                                     ev="KXETHD-26AUG0713")])
    assert b.place() == 0


def test_stop_is_retired_by_default(tmp_path, monkeypatch):
    """96c -> 1c gave no protection; 5pm legs were cut with hours left."""
    b = _bot(tmp_path, monkeypatch)
    b.bets = {"KXT": {"side": "yes", "count": 3, "entry": 96, "pside": .96,
                      "fee": 0, "band": "hi", "ots": "x", "name": "t"}}
    monkeypatch.setattr(cl.dw.DriftWide, "_quotes",
                        lambda self, tks: {t: (1, 3) for t in tks})
    assert b.stop_check() == 0          # collapsed book, still held
    assert "KXT" in b.bets and b.losses == 0


def test_gate_era_rearms_lanes_without_touching_the_lifetime_record(
        tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.hi = {"w": 46, "l": 3, "pnl": -4.31}
    b.hi_g = {"w": 46, "l": 3, "pnl": -4.31}
    b.core = {"w": 74, "l": 10, "pnl": -0.03, "bf": 1}
    b.core_g = {"w": 74, "l": 10, "pnl": -0.03}
    b.gate_era = "g1-old"
    b.halted = True
    assert b._hi_blocked() and b._core_blocked()
    b.save(balance_c=1)
    b2 = cl.CryptoLive(None, mode="DRY")          # reload = era bump
    assert b2.gate_era == cl.GATE_ERA
    assert not b2._hi_blocked() and not b2._core_blocked()
    assert not b2.halted                          # book reopened
    assert b2.hi == {"w": 46, "l": 3, "pnl": -4.31}   # lifetime intact
    assert b2.core["w"] == 74 and b2.core["pnl"] == -0.03


def test_realized_outcome_books_to_lifetime_and_gate_ledgers(
        tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b._lane_add("hi", -290, False)
    assert b.hi["l"] == 1 and b.hi_g["l"] == 1
    assert b.hi["pnl"] == b.hi_g["pnl"] == -2.9
    b._lane_add("core", 30, True)
    assert b.core["w"] == 1 and b.core_g["w"] == 1


def test_bank_refreshes_before_the_halt_is_evaluated(tmp_path, monkeypatch):
    """The deadlock: on a fresh process bank_c is 0, so _halt_c() fell
    back to its $2 floor, the book halted on a trivial daily loss, and
    place() returned BEFORE refresh_bank - so bank stayed 0 and the halt
    could never lift for the rest of the day."""
    b = _bot(tmp_path, monkeypatch, peer_cost=2000)
    b.dry_balance_c = 20000
    b.bank_c = 0
    b.day_pnl_c = -300.0                      # -$3: over the $2 floor
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: [_mk()])
    assert b.place() == 1                     # bank refreshed -> not halted
    assert b.bank_c > 0 and not b.halted
    assert b._halt_c() > 200                  # real cap, not the floor


def test_halt_still_fires_on_a_real_daily_loss(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch, peer_cost=2000)
    b.dry_balance_c = 20000
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: [_mk()])
    b.place()                                 # establishes bank
    b.day_pnl_c = -(b._halt_c() + 1)
    assert b.place() == 0 and b.halted


def test_era_change_rebases_the_day_budget_without_erasing_day_pnl(
        tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.day_pnl_c = -944.0                      # today's real -$9.44
    b.gate_era = "g1-old"
    b.halted = True
    b.save(balance_c=1)
    b2 = cl.CryptoLive(None, mode="DRY")
    assert b2.day_pnl_c == -944.0             # true day ledger preserved
    assert b2.halt_base_c == -944.0           # budget rebased from here
    assert not b2.halted                      # book reopened


def test_day_roll_clears_the_rebase(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.halt_base_c, b.day_pnl_c, b.day = -900.0, -900.0, "1999-01-01"
    b._roll_day()
    assert b.halt_base_c == 0.0 and b.day_pnl_c == 0.0 and not b.halted


# --- 8/7: fee-rounding floor (min 3 contracts) ------------------------

def test_min_contracts_floor_lifts_a_one_lot(tmp_path, monkeypatch):
    """Kelly asked for 1; the fee round-up makes that 25% drag at 96c."""
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    monkeypatch.setattr(cl, "MIN_CONTRACTS", 3)
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: [_mk(bid=84, ask=86)])
    # quarter-Kelly on this signal sizes to exactly 1 lot at 86c
    assert b.place() == 1
    assert list(b.bets.values())[0]["count"] >= 3


def test_floor_overrides_the_per_bet_cap(tmp_path, monkeypatch):
    """8/7 (Adam): keep trading everything it traded before, just at >= 3
    lots. A signal that no longer fits the per-bet cap is STILL TAKEN at
    the floor rather than skipped."""
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    monkeypatch.setattr(cl, "MIN_CONTRACTS", 3)
    monkeypatch.setattr(cl.CryptoLive, "_bet_cap_c", lambda self: 150)  # 1 lot
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: [_mk(bid=84, ask=86)])
    assert b.place() == 1
    assert list(b.bets.values())[0]["count"] == 3


def test_sizes_above_the_floor_still_trim_to_the_cap(tmp_path, monkeypatch):
    """Only the floor is exempt - Kelly sizes above it still respect it."""
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 2000000
    monkeypatch.setattr(cl, "MIN_CONTRACTS", 3)
    monkeypatch.setattr(cl.CryptoLive, "_bet_cap_c", lambda self: 860)  # 10 lots
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: [_mk(bid=84, ask=86)])
    b.place()
    n = list(b.bets.values())[0]["count"]
    assert 3 <= n <= 10 and 86 * n <= 860


def test_kelly_regains_control_above_the_floor(tmp_path, monkeypatch):
    """Once the bankroll is big enough that Kelly asks for >= the floor,
    max() stops binding and sizing is Kelly's again."""
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 2000000
    monkeypatch.setattr(cl, "MIN_CONTRACTS", 3)
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: [_mk(bid=84, ask=86)])
    b.place()
    assert list(b.bets.values())[0]["count"] > 3


def test_three_contracts_costs_the_same_fee_as_one_at_96c():
    """The whole reason for the floor."""
    from kalshibot.fees import fee_cents
    assert fee_cents(96, 1, True) == fee_cents(96, 3, True) == 1
    assert (100 - 96) * 3 - fee_cents(96, 3, True) == 11   # vs 3c on a 1-lot


# --- 8/7: sync_diffs must never go stale -----------------------------

class _FakeClient:
    def __init__(self, positions): self._p = positions
    def get_positions(self): return self._p
    def get_resting_orders(self): return []
    def get_fills(self, **k): return []
    def get_balance_cents(self): return 100000


def _pos(tk, n): return {"ticker": tk, "position_fp": str(n)}


def test_sync_diffs_reports_zero_when_books_agree(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.client = _FakeClient([_pos("KXBTCD-X", -3)])       # negative = NO side
    b.bets = {"KXBTCD-X": {"side": "no", "count": 3, "entry": 90, "fee": 0}}
    b.mirror()
    assert b._sync_diffs() == 0 and b.sync_bad == []


def test_sync_diffs_names_a_real_divergence(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.client = _FakeClient([_pos("KXBTCD-X", -3), _pos("KXETHD-Y", 2)])
    b.bets = {"KXBTCD-X": {"side": "no", "count": 5, "entry": 90, "fee": 0}}
    b.mirror()
    assert b._sync_diffs() == 2                  # wrong count + one missing
    names = {r["tk"] for r in b.sync_bad}
    assert names == {"KXBTCD-X", "KXETHD-Y"}
    row = next(r for r in b.sync_bad if r["tk"] == "KXBTCD-X")
    assert row["kalshi"] == ("no", 3) and row["book"] == ("no", 5)


def test_sync_diffs_cannot_freeze_when_a_bet_is_malformed(tmp_path,
                                                          monkeypatch):
    """The actual 8/7 bug: mirror() assigned k_positions BEFORE the diff
    count and swallowed every exception, so one bad row froze the metric
    at a stale value while positions kept refreshing."""
    b = _bot(tmp_path, monkeypatch)
    b.client = _FakeClient([_pos("KXBTCD-X", -3)])
    b.sync_diffs = 3                                   # stale value
    b.bets = {"KXBTCD-X": {"side": "no", "count": None, "entry": 90}}
    b.mirror()
    assert b._sync_diffs() == 1                        # counted, not frozen
    b.bets = {"KXBTCD-X": {"side": "no", "count": 3, "entry": 90, "fee": 0}}
    b.mirror()
    assert b._sync_diffs() == 0                        # and it clears


def test_sync_diffs_is_computed_at_save_not_carried(tmp_path, monkeypatch):
    """It must reflect the positions the payload actually publishes."""
    import json as _json
    b = _bot(tmp_path, monkeypatch)
    b.client = _FakeClient([_pos("KXBTCD-X", -3)])
    b.sync_diffs = 99                                  # poisoned
    b.bets = {"KXBTCD-X": {"side": "no", "count": 3, "entry": 90, "fee": 0}}
    b.mirror()
    b.save(balance_c=1000)
    d = _json.load(open(cl.STATE))
    assert d["summary"]["sync_diffs"] == 0
    assert d["summary"]["sync_bad"] == []


# --- 8/7: settled positions must never come back ---------------------

def test_settled_position_is_not_resurrected_by_a_late_fill(tmp_path,
                                                            monkeypatch):
    """The 8/7 bug: a market settled at 19:02 was back in the open book
    at 19:26. A leftover pending order promoted to a fill AFTER settle()
    had deleted the bet and booked the P&L."""
    b = _bot(tmp_path, monkeypatch)
    b.history = [{"tk": "KXDOGED-X", "pnl": 0.11, "outcome": 1,
                  "ts": "2026-08-07T19:02:00"}]
    o = {"ticker": "KXDOGED-X", "side": "no", "entry": 94, "count": 2,
         "pside": .94, "name": "doge", "band": "core", "ots": "x"}
    b._promote("oid1", o, 2)
    assert "KXDOGED-X" not in b.bets          # refused
    assert b.realized_c == 0                  # and nothing double-booked


def test_promote_still_tops_up_a_live_position(tmp_path, monkeypatch):
    """The guard must only block RESURRECTION, not normal partial fills."""
    b = _bot(tmp_path, monkeypatch)
    b.bets = {"KXBTCD-X": {"side": "no", "entry": 90, "count": 2, "fee": 1,
                           "pside": .9, "name": "btc", "band": "core"}}
    o = {"ticker": "KXBTCD-X", "side": "no", "entry": 90, "count": 2,
         "pside": .9, "name": "btc", "band": "core", "ots": "x"}
    b._promote("oid2", o, 2)
    assert b.bets["KXBTCD-X"]["count"] == 4


def test_sweep_removes_ghosts_without_booking_pnl(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.history = [{"tk": "KXXRP-X", "pnl": 0.15, "outcome": 1, "ts": "t"}]
    b.bets = {"KXXRP-X": {"side": "no", "entry": 84, "count": 1, "fee": 1,
                          "pside": .84, "name": "xrp", "band": "core"},
              "KXETH-LIVE": {"side": "no", "entry": 90, "count": 3, "fee": 1,
                             "pside": .9, "name": "eth", "band": "core"}}
    r0 = b.realized_c
    assert b._sweep_phantoms() == 1
    assert "KXXRP-X" not in b.bets and "KXETH-LIVE" in b.bets
    assert b.realized_c == r0                 # no P&L moved


def test_sync_diffs_ignores_kalshi_settlement_lag(tmp_path, monkeypatch):
    """Kalshi keeps reporting a market for minutes after it settles."""
    b = _bot(tmp_path, monkeypatch)
    b.client = _FakeClient([_pos("KXDOGED-X", -2), _pos("KXBTCD-Y", -3)])
    b.history = [{"tk": "KXDOGED-X", "pnl": 0.11, "outcome": 1, "ts": "t"}]
    b.bets = {"KXBTCD-Y": {"side": "no", "count": 3, "entry": 90, "fee": 0}}
    b.mirror()
    assert b._sync_diffs() == 0 and b.sync_bad == []


# ---- 8/10 ladder-coherence arb lane ----

def _arb_mkts(bid_lo=84, ask_lo=86, bid_hi=90, ask_hi=92):
    # two THRESHOLD strikes on the same coin+hour; a violation exists
    # whenever bid_hi > ask_lo (P(>65k) priced above P(>64k))
    return [_mk(tk="KXBTCD-26AUG0317-T64000", ev="KXBTCD-26AUG0317",
                bid=bid_lo, ask=ask_lo),
            _mk(tk="KXBTCD-26AUG0317-T65000", ev="KXBTCD-26AUG0317",
                bid=bid_hi, ask=ask_hi)]


def test_t_strike_parser():
    assert cl._t_strike("KXBTCD-26AUG1017-T66499.99") == 66499.99
    assert cl._t_strike("KXBTC-26AUG1017-B64950") is None
    assert cl._t_strike("") is None


def test_arb_pair_places_on_violation(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    # gap = 90 - 86 = 4c/contract; on 3 lots that clears both taker
    # fees with >= 1c/contract to spare -> pair fires
    assert b.place(mkts=_arb_mkts()) == 0     # directional count is 0
    assert len(b.arb_pairs) == 1
    pair = next(iter(b.arb_pairs.values()))
    y = b.bets["KXBTCD-26AUG0317-T64000"]
    n_ = b.bets["KXBTCD-26AUG0317-T65000"]
    assert y["side"] == "yes" and y["entry"] == 86 and y["band"] == "arb"
    assert n_["side"] == "no" and n_["entry"] == 10 and n_["band"] == "arb"
    assert pair["n"] == cl.ARB_CONTRACTS
    # locked profit: gap*n - both fees, at least 1c/contract
    fees = (cl.fee_cents(86, 3, taker=True)
            + cl.fee_cents(10, 3, taker=True))
    assert pair["net_c"] == 4 * 3 - fees and pair["net_c"] >= 3
    # the coin+hour is now ONE opinion: no directional bet may join it
    assert b.place(mkts=_arb_mkts()) == 0 and len(b.bets) == 2
    # reconcile marks the double-filled pair as on
    b.arb_reconcile()
    assert b.arb_pairs and next(iter(b.arb_pairs.values()))["status"] == "on"


def test_arb_no_violation_just_records_gap(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    # coherent ladder: higher strike cheaper, no trade
    b.place(mkts=_arb_mkts(bid_lo=90, ask_lo=92, bid_hi=84, ask_hi=86))
    assert not b.arb_pairs
    assert b.arb["best_gap_c"] == 84 - 92          # visibility: alive
    # tiny violation that fees eat: still no trade
    b2 = _bot(tmp_path, monkeypatch)
    b2.dry_balance_c = 20000
    b2.place(mkts=_arb_mkts(bid_lo=84, ask_lo=86, bid_hi=87, ask_hi=89))
    assert not b2.arb_pairs and b2.arb["best_gap_c"] == 1


def test_arb_settles_into_own_ledger(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    b.place(mkts=_arb_mkts())
    core_before = dict(b.core)
    # settle BETWEEN the strikes: both legs win (payout 200c/contract)
    res = {"KXBTCD-26AUG0317-T64000": "yes",
           "KXBTCD-26AUG0317-T65000": "no"}
    monkeypatch.setattr(cl, "fetch_result", lambda tk: res.get(tk))
    b.settle()
    b.arb_reconcile()
    assert b.arb["w"] == 2 and b.arb["l"] == 0 and b.arb["pnl"] > 0
    assert b.core == core_before          # forecast gates untouched
    assert not b.arb_pairs                # pair cleaned up
    # state round-trips
    b.save()
    b2 = cl.CryptoLive(None, mode="DRY")
    assert b2.arb["w"] == 2 and b2.arb["pairs"] == 1


def test_arb_orphan_unwinds_immediately(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    b.place(mkts=_arb_mkts())
    # simulate the NO leg dying unfilled: remove it from the book
    del b.bets["KXBTCD-26AUG0317-T65000"]
    pid = next(iter(b.arb_pairs))
    b.arb_pairs[pid]["status"] = "pending"      # never went two-sided
    monkeypatch.setattr(cl.dw.DriftWide, "_quotes",
                        lambda self, tks: {t: (85, 87) for t in tks})
    b.arb_reconcile()
    assert "KXBTCD-26AUG0317-T64000" not in b.bets   # sold at the bid
    assert not b.arb_pairs
    assert b.arb["scratches"] == 1
    assert b.history[-1]["band"] == "arb"


def test_arb_respects_pair_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "ARB_MAX_PAIRS", 1)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 50000
    two_events = _arb_mkts() + [
        _mk(tk="KXETHD-26AUG0317-T1900", ev="KXETHD-26AUG0317",
            bid=84, ask=86),
        _mk(tk="KXETHD-26AUG0317-T1950", ev="KXETHD-26AUG0317",
            bid=90, ask=92)]
    b.place(mkts=two_events)
    assert len(b.arb_pairs) == 1                # cap holds
