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
    # ask above the 92c core ceiling: now the 93-96c PROBE band (8/4),
    # tagged and ledgered separately
    assert b.place(mkts=[_mk(tk="T2", ev="E2", bid=92, ask=94)]) == 1
    assert b.bets["T2"]["band"] == "hi"
    # above the 96c probe ceiling: refused - no payoff left
    assert b.place(mkts=[_mk(tk="T2b", ev="E2b", bid=96, ask=98)]) == 0
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


def test_hi_band_probe_ledger(tmp_path, monkeypatch):
    # 8/4 crypto nickel probe: 93-96c entries keep their own W/L ledger
    # (weather-nickel playbook) so the lane earns promotion on evidence
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    assert b.place(mkts=[_mk(tk="H1", ev="EH1", bid=93, ask=95)]) == 1
    assert b.bets["H1"]["band"] == "hi"
    assert b.place(mkts=[_mk(tk="C1", ev="EC1", bid=84, ask=86)]) == 1
    assert b.bets["C1"]["band"] == "core"
    # hi-band win folds into the probe ledger; core win does not
    import weather_paper as wp
    monkeypatch.setattr(cl, "fetch_result", lambda tk: "yes")
    b.bets["H1"]["side"] = "yes"
    b.bets["C1"]["side"] = "yes"
    b.settle()
    assert b.hi["w"] == 1 and b.hi["l"] == 0
    assert b.hi["pnl"] > 0
    assert b.history[-1]["band"] in ("hi", "core")
    # probe ledger survives a save/load round-trip
    b.save()
    b2 = cl.CryptoLive(None, mode="DRY")
    assert b2.hi["w"] == 1


def test_hi_ladder_steps_on_evidence(tmp_path, monkeypatch):
    # 8/5 hi-band size ladder: base pct until 10 settled NET-POSITIVE
    # (8%), 10% at 20 - and straight back to base if net goes negative
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000                           # bank $100, NAV<$300
    b.refresh_bank(b.balance_c())
    assert b._hi_pct() == cl.BET_PCT_BOOST            # 0 settled: base 6%
    b.hi = {"w": 9, "l": 0, "pnl": 0.55}
    assert b._hi_pct() == cl.BET_PCT_BOOST            # 9 settled: not yet
    b.hi = {"w": 10, "l": 0, "pnl": 0.6}
    assert b._hi_pct() == cl.HI_PCT1                  # 10 net+: 8%
    assert b._hi_cap_c() == int(b.bank_c * cl.HI_PCT1)
    b.hi = {"w": 19, "l": 1, "pnl": 0.7}
    assert b._hi_pct() == cl.HI_PCT2                  # 20 net+: full 10%
    b.hi = {"w": 19, "l": 1, "pnl": -0.10}
    assert b._hi_pct() == cl.BET_PCT_BOOST            # net<=0: raise revoked
    b.hi = {"w": 20, "l": 0, "pnl": 0.0}
    assert b._hi_pct() == cl.BET_PCT_BOOST            # zero net is not proof
    # ladder never sizes BELOW base: at >=$300 NAV base is 3%, steps hold
    b.dry_balance_c = 40000
    b.refresh_bank(b.balance_c())
    b.hi = {"w": 10, "l": 0, "pnl": 0.6}
    assert b._hi_pct() == cl.HI_PCT1
    # core band is untouched by the hi ledger
    assert b._bet_pct() == cl.BET_PCT


def test_hi_block_closes_lane_when_proven_negative(tmp_path, monkeypatch):
    # 8/5: >=8 settled and net<0 = proven negative -> no NEW hi entries,
    # core entries unaffected (weather bucket-routing rule)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    b.hi = {"w": 3, "l": 5, "pnl": -1.20}
    assert b._hi_blocked()
    assert b.place(mkts=[_mk(tk="HB1", ev="EHB1", bid=93, ask=95)]) == 0
    assert b.place(mkts=[_mk(tk="CB1", ev="ECB1", bid=84, ask=86)]) == 1
    # not blocked while n<8 even if temporarily negative (still learning)
    b.hi = {"w": 2, "l": 3, "pnl": -0.60}
    assert not b._hi_blocked()
    assert b.place(mkts=[_mk(tk="HB2", ev="EHB2", bid=93, ask=95)]) == 1
    # ladder state is published for the tracker
    b.save()
    d = json.load(open(cl.STATE))
    hi = d["summary"]["hi"]
    assert hi["pct"] == b._hi_pct() and hi["blocked"] is False
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
             "yes_bid_dollars": "0.90", "yes_ask_dollars": "0.91",
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
    assert m["yes_bid"] == 90 and m["yes_ask"] == 91
    assert m["event"] == "KXBTCD-26AUG0614" and 0 < m["hrs"] < 1
    # and the young zero-volume hourly TRADES end to end via place(None)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    monkeypatch.setattr(cl, "fetch_crypto_mkts", lambda: mkts)
    assert b.place() == 1
    assert b.bets["KXBTCD-26AUG0614-T64000"]["band"] == "core"


# --- 8/7 truth fix: stop-outs are realized outcomes -------------------

def _stopped(monkeypatch, bid=12, ask=16):
    """Force every quote to a collapsed book so stop_check fires."""
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
    assert b._core_blocked() is True
    b.core = {"w": 2, "l": 6, "pnl": 1.0, "bf": 1}
    assert b._core_blocked() is False           # negative net required
    b.core = {"w": 0, "l": 4, "pnl": -3.0, "bf": 1}
    assert b._core_blocked() is False           # n < CORE_BLOCK_N


def test_blocked_core_band_places_nothing(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000
    b.core = {"w": 2, "l": 6, "pnl": -3.0, "bf": 1}
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
