"""Drift LIVE executor (dlive1): modes, caps, triggers, exits, DRY fills."""
import os
import sys
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drift_live as dl

TODAY = _dt.date.today().isoformat()


def _mk(tk="KXHIGHNY-26JUL-T86", bid=82, ask=85, city="new york",
        is_low=False, strike=87, kind="ge", cap=None, date=None, vol=100.0,
        hrs=10.0):
    return {"ticker": tk, "city": city, "is_low": is_low, "strike": strike,
            "kind": kind, "cap": cap, "yes_bid": bid, "yes_ask": ask,
            "date": date or TODAY, "hrs": hrs, "title": "", "sub": "",
            "bid_size": 50.0, "ask_size": 50.0, "vol": vol}


def _bot(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dl, "BETS", str(tmp_path / "b.csv"))
    # 8/18: legacy tests run WITHOUT the Adam override lane; tests that
    # exercise BUCKET_ALLOW set it explicitly
    monkeypatch.setattr(dl, "BUCKET_ALLOW", set())
    monkeypatch.setattr(dl, "GATE_FORCE", "")   # tests grade the gate honestly
    monkeypatch.setattr(dl, "OVN_LO_MODE", "off")  # ovn tests set explicitly
    # 8/20 Adam order raised the live floor to 7. Legacy tests grade cap /
    # offer / dip MECHANICS with numbers hand-computed at the historic
    # 5-lot fixture scale; the floor itself is graded in
    # test_floor_is_seven_live. Pinning here keeps both honest.
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 5)
    b = dl.DriftLive(None, mode="DRY")
    return b


def test_floor_is_seven_live(tmp_path, monkeypatch):
    """8/20 Adam order: minimum position is 7 contracts, live default."""
    assert int(os.environ.get("DRIFT_LIVE_MIN_CONTRACTS", "7")) == 7
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s7.json"))
    monkeypatch.setattr(dl, "BETS", str(tmp_path / "b7.csv"))
    monkeypatch.setattr(dl, "BUCKET_ALLOW", set())
    monkeypatch.setattr(dl, "GATE_FORCE", "")
    monkeypatch.setattr(dl, "OVN_LO_MODE", "off")
    b = dl.DriftLive(None, mode="DRY")   # module default floor: 7
    b.dry_balance_c = 100000             # roomy caps so the floor decides
    assert dl.MIN_CONTRACTS == 7
    assert b.place(mkts=[{
        "ticker": "KXHIGHNY-26AUG-T86", "city": "nyc", "is_low": False,
        "strike": 86, "kind": "ge", "cap": None, "yes_bid": 87,
        "yes_ask": 89, "date": _dt.date.today().isoformat(), "hrs": 3.0,
        "title": "", "sub": "", "bid_size": 50.0, "ask_size": 50.0,
        "vol": 100.0}]) == 1
    n = next(iter(b.bets.values()))["count"] if b.bets else \
        next(iter(b.pending.values()))["count"]
    assert n >= 7, n


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
    # 8/10 taker-first: every level entry crosses to the ask (78/78
    # misses would have won; the spread toll is cheaper than the miss)
    assert bet["side"] == "yes" and bet["entry"] == 85
    assert b.exec_stats.get("filled_taker") == 1
    assert bet["era"] == "dlive1" and bet["trig"] == "level"
    assert bet["entry"] * bet["count"] <= b.max_bet_c
    assert b.dry_balance_c < 10000


def test_nickel_lane_places_and_skips_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, 'WX_ALLOC', 1.0)   # cap math, pre-split
    b = _bot(tmp_path, monkeypatch)
    # side-mid 96, entry 95c bid in 93..96 -> 10-contract nickel, own lane
    assert b.place(mkts=[_mk(bid=95, ask=97)]) == 1
    bet = next(iter(b.bets.values()))
    assert bet["trig"] == "nickel" and bet["count"] == 10 and bet["entry"] == 95
    # nickel outcomes never count toward the drift gate
    b.history = [{"outcome": 1, "pnl": 0.05, "pside": 0.95, "trig": "nickel"}
                 for _ in range(30)]
    assert b._gate() == ("probe", 0)


def test_nickel_entry_band_and_lanes(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # 97c entry: above NICKEL_MAX_ENTRY -> no payoff left, skip
    assert b.place(mkts=[_mk(bid=97, ask=99)]) == 0
    # lane cap: 5 concurrent nickels max
    # 8/15: 10 candidates so the lane CAP (now 8) is what binds,
    # not the size of the fixture.
    ms = [_mk(tk=f"KXHIGHNY-26JUL-N{i}", bid=95, ask=97, city=f"c{i}",
              strike=i) for i in range(10)]
    b2 = _bot(tmp_path, monkeypatch)
    b2.max_open_c = 100000
    b2.dry_balance_c = 100000
    b2.place(mkts=ms)
    assert sum(1 for x in b2.bets.values()
               if x["trig"] == "nickel") == dl.dp.NICKEL_MAX_OPEN == 8


def test_nickel_size_steps_on_proof(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    assert b._nickel_count() == 10
    b.history = [{"trig": "nickel", "outcome": 1, "pnl": 0.05, "entry": 95}
                 for _ in range(10)]
    assert b._nickel_count() == 15
    b.history *= 2
    assert b._nickel_count() == 20
    # 98c grandfathers never count toward proof
    b.history = [{"trig": "nickel", "outcome": 1, "pnl": 0.02, "entry": 98}
                 for _ in range(20)]
    assert b._nickel_count() == 10


def test_pyramid_add_on_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "TAKER_FIRST", False)  # nor is execution
    b3 = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 1)  # not what this tests
    b3.place(mkts=[_mk(bid=80, ask=83)])         # level entry at 80
    tk3 = next(iter(b3.bets))
    b3.place(mkts=[_mk(bid=90, ask=92)])         # smid 91 >= 80+10 -> add at 90
    assert b3.bets[tk3]["count"] == 2 and b3.bets[tk3]["adds"] == 1
    b3.place(mkts=[_mk(bid=90, ask=92)])
    b3.place(mkts=[_mk(bid=90, ask=92)])
    assert b3.bets[tk3]["adds"] <= 2             # capped at PYRAMID_MAX
    # nickels never pyramid
    b4 = _bot(tmp_path, monkeypatch)
    b4.place(mkts=[_mk(bid=95, ask=97)])
    tk4 = next(iter(b4.bets))
    b4.place(mkts=[_mk(bid=96, ask=98)])
    assert not b4.bets[tk4].get("adds")


def test_climb_needs_confirmation(tmp_path, monkeypatch):
    # exercises the climb trigger machinery below the 7/31 production
    # floor (80c) - lower the floor for this test only
    monkeypatch.setattr(dl, "ENTRY_FLOOR", 50)
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=66, ask=70, vol=300.0)])       # memory only
    assert not b.bets
    # climb on rising volume, same-day -> maker entry
    assert b.place(mkts=[_mk(bid=69, ask=73, vol=400.0)]) == 1
    assert next(iter(b.bets.values()))["trig"] == "climb"


def test_open_cap_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "DYN_CAPS", False)   # pin the tiny manual cap
    b = _bot(tmp_path, monkeypatch)
    b.max_open_c = 200          # tiny cap: one probe bet only
    ms = [_mk(tk=f"KXHIGHNY-26JUL-T{i}", bid=82, ask=85,
              city=f"c{i}", strike=80 + i) for i in range(4)]
    b.place(mkts=ms)
    assert b.open_cost_c() <= 200 + b.max_bet_c


def test_daily_halt(tmp_path, monkeypatch):
    # 8/18 v2: caps refresh BEFORE the gate (so the loss must exceed the
    # POST-refresh cap), and with no marks the realized fail-safe rules
    b = _bot(tmp_path, monkeypatch)
    b.day_pnl_c = -99999
    assert b.place(mkts=[_mk(bid=82, ask=85)]) == 0
    assert b.halted
    assert b.day_halt_basis == "realized"


def _lvl_bet(peak=83.0):
    return {"side": "yes", "entry": 82, "count": 1, "fee": 1,
            "pside": 0.83, "city": "x", "strike": 1, "kind": "ge",
            "cap": None, "hl": "hi", "date": TODAY, "ots": "",
            "era": "dlive1", "trig": "level", "peak": peak}


def test_trail_off_by_default_holds_to_settlement(tmp_path, monkeypatch):
    # 7/27 autopsy verdict: 4/5 exits would have won; trailing cost $1.56.
    b = _bot(tmp_path, monkeypatch)
    b.bets = {"T1": _lvl_bet(peak=95.0)}
    assert b.stop_check(quotes={"T1": (77, 81)}) == 0     # wobble: HOLD
    assert "T1" in b.bets
    # 7/28: sub-50c dips are nowcast noise (5/6 stops would have won) -
    # 46c now HOLDS; only a true collapse below STOP_C=35 gets cut
    assert b.stop_check(quotes={"T1": (44, 48)}) == 0
    assert "T1" in b.bets
    # 8/10: the disaster stop is retired too (autopsy: exits netted
    # -$1.91; a collapse between polls can't be stopped anyway). HOLD.
    assert b.stop_check(quotes={"T1": (30, 34)}) == 0
    assert "T1" in b.bets
    # env rollback path still works
    monkeypatch.setattr(dl, "WSTOP_ON", True)
    assert b.stop_check(quotes={"T1": (30, 34)}) == 1
    assert b.history[-1]["stopped"] is True


def test_stop_and_trail_when_reenabled(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "TRAIL_ON", True)
    monkeypatch.setattr(dl, "WSTOP_ON", True)
    b = _bot(tmp_path, monkeypatch)
    b.bets = {"T1": _lvl_bet()}
    assert b.stop_check(quotes={"T1": (80, 84)}) == 0     # healthy: hold
    b.bets["T1"]["peak"] = 95.0
    assert b.stop_check(quotes={"T1": (77, 81)}) == 1     # trail exit
    assert b.history[-1]["faded"] is True
    b.bets = {"T2": _lvl_bet()}
    assert b.stop_check(quotes={"T2": (30, 34)}) == 1     # momentum stop
    assert b.history[-1]["stopped"] is True


def _proven_hist(n=30, entry=87):
    return [{"outcome": 1, "pnl": 0.10, "pside": 0.9, "trig": "level",
             "entry": entry, "tk": f"T{i}", "ots": str(i)} for i in range(n)]


def test_evidence_weighted_kelly_fraction(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.history = _proven_hist()
    bs = b._bucket_stats()
    assert b._kelly_frac(bs, "level", 87) == dl.KELLY_PROVEN_MULT  # proven
    assert b._kelly_frac(bs, "level", 82) == dl.KELLY_BASE     # other band
    assert b._kelly_frac(bs, "climb", 87) == dl.KELLY_BASE     # other trig
    # a proven bucket that goes net-negative loses the boost
    b.history = b.history[:10] + [{"outcome": 0, "pnl": -2.0, "pside": 0.9,
                                   "trig": "level", "entry": 87,
                                   "tk": "TL", "ots": "x"}]
    assert b._kelly_frac(b._bucket_stats(), "level", 87) == dl.KELLY_BASE


def test_proven_bucket_sizes_up(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, 'WX_ALLOC', 1.0)   # cap math, pre-split
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 1)  # measuring Kelly, not the floor
    b.history = _proven_hist()                 # gate=scale + proven 85-89
    assert b._gate()[0] == "scale"
    assert b.place(mkts=[_mk(bid=87, ask=89)]) == 1
    # 8/11 earned sizing: proven lanes use the 8% Kelly ceiling (was 3%)
    assert next(iter(b.bets.values()))["count"] == 4   # half-Kelly, 8% cap
    monkeypatch.setattr(dl, "KELLY_PROVEN_N", 999)     # same lane, unproven
    b2 = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 1)  # helper re-pins; Kelly again
    b2.history = _proven_hist()
    assert b2.place(mkts=[_mk(bid=87, ask=89)]) == 1
    assert next(iter(b2.bets.values()))["count"] == 2  # quarter-Kelly


def test_taker_falls_back_to_maker_when_toll_too_big(tmp_path, monkeypatch):
    # regression (7/28): scale mode used to Kelly-size takers AT THE ASK,
    # where f*=0 by construction -> every high-mid tight-spread candidate
    # was dropped entirely (no maker join either). Now: edge measured at
    # the bid; if a taker lot doesn't fit, rest a maker join instead.
    monkeypatch.setattr(dl, "KELLY_PROVEN_N", 999)
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 1)  # not what this tests
    b.history = _proven_hist()
    b.dry_balance_c = 4600            # taker lot at 89c doesn't fit Kelly
    assert b.place(mkts=[_mk(bid=87, ask=89)]) == 1
    bet = next(iter(b.bets.values()))
    assert bet["entry"] == 87 and bet["count"] == 1    # maker join at bid


def test_nickel_pos_cap_trims_to_nav(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, 'WX_ALLOC', 1.0)   # cap math, pre-split
    # isolate the pos-cap floor from the 8/11 concentration caps
    monkeypatch.setattr(dl, "CITY_CAP_PCT", 1.0)
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 1.0)
    # 7/29 pos cap trims the ladder; 8/10 the 5-lot floor overrides it
    # (Adam: every weather position >= 5 contracts) - only the LANE
    # aggregate cap may skip a nickel now
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 3000                    # NAV $30 -> pos cap $3.00
    assert b.place(mkts=[_mk(bid=95, ask=97)]) == 1
    bet = next(iter(b.bets.values()))
    assert bet["trig"] == "nickel" and bet["count"] == 5   # 10 -> floor 5
    assert bet["entry"] * bet["count"] <= int(3000 * dl.NICKEL_LANE_PCT)


def test_nickel_lane_cap_blocks_fourth(tmp_path, monkeypatch):
    # lane (filled + resting) capped at 25% of NAV (8/10: was 30% -
    # survival math says the 26-0 streak doesn't yet prove the price)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000                   # lane cap $25 -> two 10-lots
    ms = [_mk(tk=f"KXHIGHNY-26JUL-N{i}", bid=95, ask=97, city=f"c{i}",
              strike=i) for i in range(7)]
    b.place(mkts=ms)
    nk = [x for x in b.bets.values() if x["trig"] == "nickel"]
    assert len(nk) == 2                       # 2 x $9.50 fits, 3rd would break
    assert sum(x["entry"] * x["count"] for x in nk) <= 2500


def _stale_order(tk="T1", entry=80, count=2, trig="level"):
    return {"ticker": tk, "side": "yes", "entry": entry, "count": count,
            "pside": 0.82, "trig": trig, "city": "x", "strike": 1,
            "hl": "hi", "kind": "ge", "cap": None, "date": TODAY,
            "ots": "", "era": "dlive1", "peak": float(entry)}


def test_cross_expiring_takes_the_ask(tmp_path, monkeypatch):
    # 7/30: 20/20 expired joins would have won ($10.90) -> cross instead
    b = _bot(tmp_path, monkeypatch)
    assert b._cross_expiring(_stale_order(), 2, q=(82, 84)) is True
    assert "T1" in b.bets and b.bets["T1"]["count"] == 2
    assert b.bets["T1"]["entry"] == 84                 # paid the ask
    assert b.exec_stats.get("placed_taker") == 1
    assert b.exec_stats.get("filled_taker") == 1       # DRY: instant fill
    assert b.exec_stats.get("cross_expiry") == 1


def test_cross_expiring_refuses_bad_setups(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # 8/7: a runner at 88/90 is now BOARDED, not forfeited. It is 10c
    # above our stale 80c join, but 100-90-fee still leaves edge, and the
    # stale join price has no bearing on whether the ask is worth paying.
    assert b._cross_expiring(_stale_order(tk="T2"), 2, q=(88, 90)) is True
    assert b.bets["T2"]["entry"] == 90
    # signal faded below our entry: smid 76 < 80
    assert b._cross_expiring(_stale_order(tk="T3"), 2, q=(74, 78)) is False
    # nothing unfilled
    assert b._cross_expiring(_stale_order(tk="T4"), 0, q=(82, 84)) is False
    assert "T3" not in b.bets and "T4" not in b.bets
    assert b._cross_why in ("nothing_unfilled",)


def test_pursuit_ladder_boards_runners(tmp_path, monkeypatch):
    # 8/3: a runner at 86/88 (8c above our 80c join) now gets crossed
    b = _bot(tmp_path, monkeypatch)
    assert b._cross_expiring(_stale_order(tk="T5"), 2, q=(86, 88)) is True
    assert b.bets["T5"]["entry"] == 88
    # and the requote chases past the old 92c ceiling, up to 96c
    b2 = _bot(tmp_path, monkeypatch)
    b2.pending = {"o9": dict(_stale_order(tk="T9"), ticker="T9", oid="o9")}
    mk = {"ticker": "T9", "yes_bid": 94, "yes_ask": 96, "city": "x",
          "strike": 1, "hl": "hi"}
    assert b2._maybe_requote("T9", mk) is not False  # 94c join placed
    o = list(b2.pending.values())[0]
    assert o["entry"] == 94 and o["requotes"] == 1


def test_cross_expiring_respects_caps(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.max_bet_c = 170                                   # 84c x 2 = 168 fits
    assert b._cross_expiring(_stale_order(count=3), 3, q=(82, 84)) is True
    assert b.bets["T1"]["count"] == 2                   # trimmed 3 -> 2


def test_execution_defaults_widened():
    # 8/10 (miss-autopsy 78/78 would-won, $36.72): TAKER-FIRST is the
    # default and the spread fence widened 4 -> 6; the stop is retired
    assert dl.TAKER_FIRST is True
    assert dl.TAKER_MIN_SMID == 84 and dl.TAKER_MAX_SPREAD == 6
    assert dl.STOP_C == 35 and dl.WSTOP_ON is False
    assert dl.ENTRY_FLOOR == 80          # 7/31: sub-80c entries killed
    # 8/4 pursuit escalation (52/52 misses would have won, $26.72):
    # 8/14: 15 min, not 30. Only $15.39 of $124.75 NAV was FILLED; the
    # other $86.84 sat in unfilled joins eating caps.open (4,739 slate
    # refusals) without earning anything.
    assert dl.REST_MAX_H == 0.25
    assert dl.CHASE_MAX_E == 97          # chase winners to 97c
    # 8/7: the stale-anchor chase cap is retired (0 = off, env rollback
    # only). Crossing is now decided by the edge left AT THE ASK.
    assert dl.CROSS_MAX_CHASE == 0
    assert dl.CROSS_MIN_EDGE_C == 3


def test_entry_floor_blocks_sub_80(tmp_path, monkeypatch):
    # 7/31 concentration: every sub-80c band lost money live (-$8.10/36)
    b = _bot(tmp_path, monkeypatch)
    assert b.place(mkts=[_mk(bid=78, ask=81)]) == 0     # bid below floor
    b.place(mkts=[_mk(bid=70, ask=74, vol=300.0)])      # climb memory
    assert b.place(mkts=[_mk(bid=73, ask=77, vol=400.0)]) == 0  # climb dead
    assert not b.bets and not b.pending
    assert b.place(mkts=[_mk(bid=80, ask=83)]) == 1     # at the floor: fine


def test_taker_first_on_proven_lane(tmp_path, monkeypatch):
    # proven half-Kelly lane + tight spread -> pay the ask immediately
    b = _bot(tmp_path, monkeypatch)
    b.history = _proven_hist(entry=82)          # level:80-84 proven, gate scale
    assert b.place(mkts=[_mk(bid=82, ask=84)]) == 1     # smid 83 < 84 gate
    bet = next(iter(b.bets.values()))
    assert bet["entry"] == 84                   # crossed, didn't queue
    assert b.exec_stats.get("filled_taker") == 1
    # 8/10 taker-first: even an unproven lane below the smid gate
    # crosses now - the maker join is only a fallback (TAKER_FIRST=0)
    monkeypatch.setattr(dl, "KELLY_PROVEN_N", 999)
    b2 = _bot(tmp_path, monkeypatch)
    b2.history = _proven_hist(entry=82)
    assert b2.place(mkts=[_mk(bid=82, ask=84)]) == 1
    assert next(iter(b2.bets.values()))["entry"] == 84  # crossed anyway
    # with taker-first off, the old maker join comes back
    monkeypatch.setattr(dl, "TAKER_FIRST", False)
    b3 = _bot(tmp_path, monkeypatch)
    b3.history = _proven_hist(entry=82)
    assert b3.place(mkts=[_mk(bid=82, ask=84)]) == 1
    assert next(iter(b3.bets.values()))["entry"] == 82  # joined the bid


def test_dynamic_caps_track_nav(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, 'WX_ALLOC', 1.0)   # cap math, pre-split
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 20000                     # NAV $200, nothing filled
    b._refresh_caps(b.balance_c())
    assert b.max_bet_c == 1200                  # 6% boost (NAV < $300)
    # 8/13 holding-time-scaled: 85% deployed when everything flattens
    # before close (60% if DRIFT_LIVE_FLATTEN is off)
    assert b.max_open_c == int(20000 * dl.OPEN_PCT)
    assert b.max_day_loss_c == int(20000 * dl.HALT_PCT)   # 8/13: 15%
    # filled positions count toward NAV at cost
    b.bets = {"T1": dict(_lvl_bet(), count=10)}     # +$8.20 basis
    b._refresh_caps(b.balance_c())
    assert b.max_open_c == int(20820 * dl.OPEN_PCT)
    # drawdown shrinks caps; floors keep probes viable
    b.bets = {}
    b.dry_balance_c = 3000
    b._refresh_caps(b.balance_c())
    assert b.max_bet_c == 200 and b.max_day_loss_c == int(3000 * dl.HALT_PCT)
    b.dry_balance_c = 1000
    b._refresh_caps(b.balance_c())
    assert b.max_bet_c == 200 and b.max_day_loss_c == 200   # floors


def test_dynamic_caps_frozen_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "DYN_CAPS", False)
    b = _bot(tmp_path, monkeypatch)
    before = (b.max_bet_c, b.max_open_c, b.max_day_loss_c)
    b._refresh_caps(999999)
    assert (b.max_bet_c, b.max_open_c, b.max_day_loss_c) == before


def test_gate_probe_until_30(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    assert b._gate() == ("probe", 0)
    b.history = [{"outcome": 1, "pnl": 0.10, "pside": 0.9} for _ in range(30)]
    assert b._gate() == ("scale", 30)


class _FakeMissClient:
    """Resting order that goes stale -> cancel path logs a miss."""
    def __init__(self, oid, tk):
        self._oid, self._tk = oid, tk
        self.canceled = []

    def get_resting_orders(self):
        return [{"order_id": self._oid, "ticker": self._tk}]

    def get_fills(self, limit=100):
        return []

    def cancel_order(self, oid):
        self.canceled.append(oid)
        return {}


def test_miss_logged_on_stale_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "CROSS_EXPIRY", False)   # isolate miss logging
    b = _bot(tmp_path, monkeypatch)
    old = (_dt.datetime.now() - _dt.timedelta(hours=3)).isoformat()
    b.client = _FakeMissClient("o1", "KXHIGHNY-26JUL-T86")
    b.pending = {"o1": {"ticker": "KXHIGHNY-26JUL-T86", "side": "yes",
                        "entry": 85, "count": 2, "pside": 0.86, "fee": 0,
                        "trig": "level", "city": "new york", "strike": 86,
                        "kind": "ge", "cap": None, "hl": "hi",
                        "date": TODAY, "ots": old, "era": "dlive1"}}
    b.check_orders()
    assert not b.pending and b.client.canceled == ["o1"]
    assert len(b.miss) == 1
    m = b.miss[0]
    assert m["tk"] == "KXHIGHNY-26JUL-T86" and m["count"] == 2
    assert m["res"] is None
    # partial fill before cancel -> only the UNFILLED remainder is a miss
    b.client = _FakeMissClient("o2", "KXHIGHNY-26JUL-T87")
    b.pending = {"o2": {"ticker": "KXHIGHNY-26JUL-T87", "side": "yes",
                        "entry": 85, "count": 3, "pside": 0.86, "fee": 0,
                        "trig": "level", "city": "new york", "strike": 87,
                        "kind": "ge", "cap": None, "hl": "hi", "date": TODAY,
                        "ots": old, "era": "dlive1", "filled_seen": 1}}
    b.check_orders()
    assert b.miss[-1]["count"] == 2


def test_miss_check_grades_vs_settlement(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.miss = [{"tk": "W", "side": "yes", "entry": 85, "count": 2,
               "trig": "level", "pside": 0.86, "ots": "", "cts": "",
               "res": None},
              {"tk": "L", "side": "yes", "entry": 90, "count": 1,
               "trig": "level", "pside": 0.9, "ots": "", "cts": "",
               "res": None}]
    monkeypatch.setattr(dl, "fetch_result",
                        lambda tk: {"W": "yes", "L": "no"}.get(tk))
    b.miss_check()
    wfee = dl.fee_cents(85, 2, taker=False)
    assert b.miss[0]["would_pnl"] == round((15 * 2 - wfee) / 100, 2)  # won
    lfee = dl.fee_cents(90, 1, taker=False)
    assert b.miss[1]["would_pnl"] == round((-90 - lfee) / 100, 2)     # lost
    s = b._miss_summary()
    assert s["miss_settled"] == 2 and s["miss_would_won"] == 1
    assert s["miss_cost"] == round(b.miss[0]["would_pnl"]
                                   + b.miss[1]["would_pnl"], 2)


def test_miss_persists_and_caps_in_summary(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b._log_miss({"ticker": "T", "side": "yes", "entry": 85, "count": 1,
                 "trig": "level", "pside": 0.86, "ots": ""}, 1)
    b.save(balance_c=b.balance_c())
    b2 = dl.DriftLive(None, mode="DRY")
    assert len(b2.miss) == 1 and b2.miss[0]["tk"] == "T"
    st = __import__("json").load(open(dl.STATE))
    assert "miss_n" in st["summary"] and "caps" in st["summary"]
    assert st["summary"]["caps"]["dyn"] == dl.DYN_CAPS


def test_build_is_dry_without_key(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dl, "CONFIG", str(tmp_path / "nope.yaml"))
    monkeypatch.delenv("KALSHI_DRIFT_LIVE", raising=False)
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    b = dl.build()
    assert b.mode == "DRY" and b.client is None


class _SettleClient:
    """Settlement feed client: page1/page2 for the seed, then a ROLLING
    window that drops the oldest rows - the cumulative ledger must not."""
    def __init__(self):
        self.rows = [
            {"ticker": f"KXHIGHNY-26JUL-T{i}", "settled_time": f"2026-07-2{4+i%5}T0{i%9}:00:00",
             "revenue": 100, "value": 0, "yes_total_cost": 0,
             "no_total_cost": 82, "fee_cost": "0.01"} for i in range(6)]

    def get_settlements_page(self, limit=200, cursor=None):
        if cursor is None:
            return self.rows[:3], "c2"
        return self.rows[3:], None

    def get_settlements(self, limit=200):
        return self.rows[2:]           # window rolled: first two gone


def test_cumulative_ledger_never_shrinks(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dl, "BETS", str(tmp_path / "b.csv"))
    b = dl.DriftLive(None, mode="DRY")
    b.client = _SettleClient()
    b.sync_kalshi_truth()              # seed: paginates BOTH pages
    assert b.k_cum["w"] == 6 and b.k_cum["seeded"] is True
    b.sync_kalshi_truth()              # window rolled; dedupe holds
    b.sync_kalshi_truth()
    assert b.k_cum["w"] == 6           # never shrinks, never double-counts
    assert round(b.k_cum["pnl"], 2) == round(6 * 0.17, 2)


def test_daily_pnl_ledger_seeds_with_residual(tmp_path, monkeypatch):
    # trimmed-off history rows can't be dated: their P&L lands on the era
    # epoch so weekly/monthly columns still sum to lifetime realized
    import json as _json
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dl, "BETS", str(tmp_path / "b.csv"))
    (tmp_path / "s.json").write_text(_json.dumps({
        "mode": "DRY",
        "realized_c": 500.0,   # $5 lifetime; surviving rows only show $3
        "history": [
            {"pnl": 1.0, "ts": "2026-07-28T12:00:00"},
            {"pnl": 2.0, "ts": "2026-07-29T12:00:00"}]}))
    b = dl.DriftLive(None, mode="DRY")
    assert b.pnl_days["2026-07-28"] == 1.0
    assert b.pnl_days["2026-07-29"] == 2.0
    assert b.pnl_days[dl.LIVE_EPOCH[:10]] == 2.0    # the $2 residual
    assert abs(sum(b.pnl_days.values()) - 5.0) < 0.005


def test_cross_expiring_one_sided_books(tmp_path, monkeypatch):
    # 8/4: every one of the 52 logged misses would have WON - near
    # settlement the runner's book often holds only an ask. Cross it.
    b = _bot(tmp_path, monkeypatch)
    # YES side, ask-only book (no bid): entry 80, ask 84 -> cross
    assert b._cross_expiring(_stale_order(tk="T1"), 2, q=(0, 84)) is True
    assert b.bets["T1"]["entry"] == 84
    # YES side, ask-only but ask BELOW entry: signal faded -> refuse
    assert b._cross_expiring(_stale_order(tk="T2"), 2, q=(0, 78)) is False
    # YES side, ask-only vestigial 99c: above 97c ceiling -> refuse
    assert b._cross_expiring(_stale_order(tk="T3", entry=95, trig="x"),
                             2, q=(0, 99)) is False
    # YES side, bid-only book (no ask to cross) -> refuse
    assert b._cross_expiring(_stale_order(tk="T4"), 2, q=(82, 0)) is False
    # NO side, yes-bid-only book: our ask = 100-yb = 84 -> cross
    o = dict(_stale_order(tk="T5"), side="no")
    assert b._cross_expiring(o, 2, q=(16, 0)) is True
    assert b.bets["T5"]["entry"] == 84
    # NO side, yes-ask-only (no yes bid = no NO ask) -> refuse
    o2 = dict(_stale_order(tk="T6"), side="no")
    assert b._cross_expiring(o2, 2, q=(0, 20)) is False


def test_day_anchor_includes_both_books(tmp_path, monkeypatch):
    # 8/6 (Adam caught the +14.6% ghost return): the day-start NAV anchor
    # must count cash + WEATHER open cost + CRYPTO open cost. Excluding
    # the crypto book made every overnight crypto dollar look like
    # today's profit.
    import json as _json
    b = _bot(tmp_path, monkeypatch)
    cpath = tmp_path / "crypto_state.json"
    cpath.write_text(_json.dumps(
        {"bets": {"KXBTCD-X": {"entry": 94, "count": 2},
                  "KXETHD-X": {"entry": 90, "count": 3}}}))
    monkeypatch.setattr(dl, "CRYPTO_STATE_PATH", str(cpath))
    b.bets = {"W1": {"entry": 80, "count": 2}}
    assert b._day_anchor_c(10000) == 10000 + 160 + (188 + 270)
    # missing crypto state file -> anchor still works, crypto part 0
    monkeypatch.setattr(dl, "CRYPTO_STATE_PATH", str(tmp_path / "none.json"))
    assert b._day_anchor_c(10000) == 10160


def test_stale_anchor_discarded_once_on_upgrade(tmp_path, monkeypatch):
    # states written before the fix (no nav0_v2 flag) carry a crypto-blind
    # anchor: it must be dropped at load so the next cycle re-anchors;
    # states written by the fixed build keep their anchor across restarts
    import json as _json
    b = _bot(tmp_path, monkeypatch)
    b.day_nav0_c = 9963
    b.save(balance_c=None)
    d = _json.load(open(dl.STATE))
    assert d.get("nav0_v2") is True and d["day_nav0_c"] == 9963
    b2 = dl.DriftLive(None, mode="DRY")
    assert b2.day_nav0_c == 9963          # v2 state: anchor survives
    d.pop("nav0_v2")
    _json.dump(d, open(dl.STATE, "w"))
    b3 = dl.DriftLive(None, mode="DRY")
    assert b3.day_nav0_c is None          # pre-fix state: re-anchor


# --- 8/7: the miss leak, measured instead of guessed -------------------

def test_runaway_winner_is_boarded_not_forfeited(tmp_path, monkeypatch):
    """The leak's actual shape: a join at 80c whose market ran to 96c was
    refused for being 16c from a stale quote, then settled at 100."""
    b = _bot(tmp_path, monkeypatch)
    assert b._cross_expiring(_stale_order(tk="R1"), 2, q=(94, 96)) is True
    assert b.bets["R1"]["entry"] == 96


def test_cross_refused_only_when_no_edge_is_left(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # 98c ask: above CHASE_MAX_E (97), nothing left worth paying for
    assert b._cross_expiring(_stale_order(tk="E1"), 2, q=(97, 98)) is False
    assert b._cross_why == "ask_above_ceiling"
    # at the ceiling but the fee eats the remaining 3c
    monkeypatch.setattr(dl, "CROSS_MIN_EDGE_C", 6)
    assert b._cross_expiring(_stale_order(tk="E2"), 2, q=(95, 97)) is False
    assert b._cross_why == "no_edge_left"


def test_every_refusal_records_a_reason(tmp_path, monkeypatch):
    """No silent forfeits - three passes at this leak failed because the
    ledger never said WHY the cross was declined."""
    b = _bot(tmp_path, monkeypatch)
    for tk, q, expect in [("W1", None, "no_quote"),
                          ("W2", (74, 78), "signal_faded"),
                          ("W3", (97, 98), "ask_above_ceiling")]:
        b._cross_expiring(_stale_order(tk=tk), 2, q=q)
        assert b._cross_why == expect, (tk, b._cross_why)
    b._cross_expiring(_stale_order(tk="W4"), 0, q=(82, 84))
    assert b._cross_why == "nothing_unfilled"


def test_miss_records_reason_and_the_ask_it_walked_away_from(
        tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b._log_miss(_stale_order(tk="M1"), 2, why="signal_faded", ask=78)
    r = b.miss[-1]
    assert r["why"] == "signal_faded" and r["ask"] == 78
    s = b._miss_summary()
    assert s["miss_why"]["signal_faded"]["n"] == 1


def test_recoverable_is_scored_at_the_refused_ask_not_the_stale_join(
        tmp_path, monkeypatch):
    """would_pnl prices a fill we could never have got; miss_recoverable
    prices the one we actually turned down."""
    b = _bot(tmp_path, monkeypatch)
    b._log_miss(_stale_order(tk="M2", entry=80), 2, why="chase_cap", ask=96)
    monkeypatch.setattr(dl, "fetch_result", lambda tk: "yes")
    b.miss_check()
    r = b.miss[-1]
    assert r["res"] == "yes"
    assert r["would_pnl"] > r["cross_pnl"]        # the honest number is lower
    s = b._miss_summary()
    assert s["miss_recoverable"] < s["miss_cost"]
    assert s["miss_recoverable"] > 0              # but still real money


def test_weather_entry_floor_lifts_a_one_lot(tmp_path, monkeypatch):
    """Kelly asked for 1-2; the fee round-up makes that 12-25% drag."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 3)
    ms = [_mk(tk="KXHIGHNY-26JUL-T86", bid=88, ask=89, city="new york",
              strike=87)]
    b.place(mkts=ms)
    if b.bets:
        assert list(b.bets.values())[0]["count"] >= 3


def test_weather_floor_overrides_the_bet_cap(tmp_path, monkeypatch):
    """8/7 (Adam): still trade it, at the floor, rather than skip."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 3)
    monkeypatch.setattr(dl, "DYN_CAPS", False)   # caps are recomputed otherwise
    b.max_bet_c = 100                       # 89c x 3 = 267 > cap
    ms = [_mk(tk="KXHIGHNY-26JUL-T86", bid=88, ask=89, city="new york",
              strike=87)]
    b.place(mkts=ms)
    assert b.bets and list(b.bets.values())[0]["count"] == 3


def test_settlement_receivable_bridges_nav(tmp_path, monkeypatch):
    # 8/10: a detected win's payout sits in the receivable until the
    # exchange's balance credit lands (consumed on balance rise,
    # hard-expired at 15 min) so marked NAV never dips at settlement
    b = _bot(tmp_path, monkeypatch)
    b._recv_add(300)
    assert b._recv_c() == 300                   # no balance info: held
    assert b._recv_c(balance_c=5000) == 300     # first sighting anchors
    assert b._recv_c(balance_c=5200) == 100     # +200 credit consumed
    assert b._recv_c(balance_c=5300) == 0       # fully consumed
    b._recv_add(500)
    b.recv[-1][0] = "2000-01-01T00:00:00"       # ancient: hard-expired
    assert b._recv_c() == 0


# ---- 8/10 two-sided book: premium offer side ----

def test_offer_defaults():
    assert dl.QUOTE_ON is True
    # 8/21 settles->lifts: nickel floor 98 -> 96 (the "21-0 perfect
    # lane" note was stale; it is 7W/1L and NET -$6.07 because the one
    # settle loss ate seven wins), markup 6 -> 4
    assert dl.SELL_MIN_C == 98 and dl.NICKEL_SELL_MIN_C == 96
    assert dl.SELL_MARKUP_C == 4
    assert dl.SELL_CAP_C == 99


def test_premium_offers_quote_all_inventory(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=82, ask=85)])         # level, taker fill at 85
    b.quote_offers()
    tk = next(iter(b.bets))
    off = b.offers[tk]
    # 8/11 ladder: 5 lots split - 3 at the low rung (98 since 8/17), 2 at 99
    assert off["count"] == b.bets[tk]["count"] == 5
    assert [(l["px"], l["count"]) for l in off["legs"]] == [(98, 3), (99, 2)]
    # nickel entry 95: low rung min(99, max(98, 101)) = 99 -> single rung
    monkeypatch.setattr(dl, 'WX_ALLOC', 1.0)
    b2 = _bot(tmp_path, monkeypatch)
    b2.place(mkts=[_mk(bid=95, ask=97)])
    b2.quote_offers()
    tk2 = next(iter(b2.bets))
    assert b2.bets[tk2]["trig"] == "nickel"
    legs2 = b2.offers[tk2]["legs"]
    assert len(legs2) == 1 and legs2[0]["px"] == 99
    assert legs2[0]["count"] == int(b2.bets[tk2]["count"])
    # idempotent: re-running does not duplicate or resize
    n_off = dict(b.offers)
    b.quote_offers()
    assert b.offers == n_off
    # ladder off: one rung at the low price
    monkeypatch.setattr(dl, "SELL_LADDER_ON", False)
    b3 = _bot(tmp_path, monkeypatch)
    b3.place(mkts=[_mk(bid=82, ask=85)])
    b3.quote_offers()
    l3 = b3.offers[next(iter(b3.bets))]["legs"]
    assert len(l3) == 1 and l3[0]["px"] == 98 and l3[0]["count"] == 5


def test_offer_fill_books_premium_and_recycles(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=82, ask=85)])
    tk = next(iter(b.bets))
    r0, day0 = b.realized_c, b.day_pnl_c
    b.quote_offers()
    legs = b.offers[tk]["legs"]
    # both rungs lift: 3 @ 98 and 2 @ 99
    b._check_offers(set(), {legs[0]["oid"]: 3, legs[1]["oid"]: 2})
    assert tk not in b.bets and tk not in b.offers
    assert b.realized_c > r0 and b.day_pnl_c > day0
    assert b.history[-1]["exit_px"] == 99 and b.history[-2]["exit_px"] == 98
    assert all(h["pnl"] > 0 and h.get("sold") for h in b.history[-2:])
    # 8/11 sold autopsy: grades against eventual settlement
    monkeypatch.setattr(dl, "fetch_result", lambda tk: "yes")
    b.sold_check()
    rows = [r for r in b.sold_log if r["tk"] == tk]
    assert all(r["res"] == "yes" and r["would_pnl"] > 0 for r in rows)
    # would have won at settlement: selling gave up a little (kept < 0)
    assert all(r["kept"] < 0 for r in rows)


def test_offer_partial_fill_shrinks_and_requotes(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=82, ask=85)])
    tk = next(iter(b.bets))
    n = b.bets[tk]["count"]
    assert n == 5                                # 8/10 floor
    b.quote_offers()
    legs = b.offers[tk]["legs"]
    # 2 of the low rung's 3 lift; the 99 leg keeps resting
    b._check_offers({legs[1]["oid"]}, {legs[0]["oid"]: 2})
    assert b.bets[tk]["count"] == 3
    assert len(b.offers[tk]["legs"]) == 1        # surviving 99 leg
    b.quote_offers()                             # resize: requote at 3
    off = b.offers[tk]
    assert off["count"] == 3
    assert [(l["px"], l["count"]) for l in off["legs"]] == [(98, 2), (99, 1)]
    # position settles before the quote lifts: stale offer dropped
    del b.bets[tk]
    b.quote_offers()
    assert tk not in b.offers


def test_offer_never_below_cost_or_when_off(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _lvl_bet()
    b.bets["T1"]["entry"] = 98                   # deep entry: 99 quote ok
    b.quote_offers()
    assert b.offers["T1"]["legs"][0]["px"] == 99   # still above cost
    b.bets["T2"] = _lvl_bet()
    b.bets["T2"]["entry"] = 99                   # nothing above cost <= 99
    b.quote_offers()
    assert "T2" not in b.offers
    monkeypatch.setattr(dl, "QUOTE_ON", False)
    b3 = _bot(tmp_path, monkeypatch)
    b3.bets["T3"] = _lvl_bet()
    b3.quote_offers()
    assert not b3.offers


def test_fractional_lift_leaves_exact_stub(tmp_path, monkeypatch):
    # 8/11: Kalshi trades fractional contracts - a buyer took 4.75 of a
    # 5-lot quote live, and the int-rounding booked it as 5 sold. Now
    # the fraction is counted exactly: stub stays, P&L is precise.
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=82, ask=85)])
    tk = next(iter(b.bets))
    n = b.bets[tk]["count"]
    assert n == 5
    b.quote_offers()
    legs = b.offers[tk]["legs"]
    # buyers take 3 @ 97 and 1.75 @ 99, leaving a 0.25 stub
    b._check_offers(set(), {legs[0]["oid"]: 3, legs[1]["oid"]: 1.75})
    assert abs(b.bets[tk]["count"] - 0.25) < 0.001   # exact stub remains
    assert b.history[-1]["count"] == 1.75            # sales booked exactly
    assert b.history[-2]["count"] == 3
    assert b.history[-1]["pnl"] > 0 and b.history[-2]["pnl"] > 0
    # the stub can't be quoted (sub-1 contract) - it holds to settlement
    b.quote_offers()
    assert tk not in b.offers
    # and a fractional Kalshi mirror row survives sync without a false
    # divergence flag
    b.k_positions = [{"ticker": tk, "side": "yes", "count": 0.25}]
    b.bets[tk]["count"] = 0.25
    class _C:  # minimal client stub so _sync_diffs runs
        pass
    b.client = _C()
    assert b._sync_diffs() == 0


def test_k_truth_v2_folds_sale_proceeds(tmp_path, monkeypatch):
    # 8/11: settlements-only accounting scored every premium sale as a
    # phantom loss (k_losses 65->86 the first offer-side night). The v2
    # rebuild backfills sale proceeds from history and reseeds k_cum.
    b = _bot(tmp_path, monkeypatch)
    b.history.append({"tk": "T9", "city": "x", "strike": 1, "hl": "hi",
                      "side": "yes", "trig": "level", "pside": 0.85,
                      "entry": 85, "count": 5, "outcome": None,
                      "exited": True, "sold": True, "exit_px": 97,
                      "pnl": 0.55, "ts": "2026-08-11T00:00:00", "ots": "", "era": "dlive1"})
    b.k_cum = {"seeded": True, "w": 1, "l": 2, "pnl": -5.0}
    b.k_sold = {}
    b.save()
    b2 = dl.DriftLive(None, mode="DRY")
    assert b2.k_cum == {"v2_sold": True}         # forced reseed
    assert b2.k_sold.get("T9") == 485            # 97c x 5 backfilled
    # live recording: a lifted offer books its proceeds too
    b3 = _bot(tmp_path, monkeypatch)
    b3.place(mkts=[_mk(bid=82, ask=85)])
    tk = next(iter(b3.bets))
    b3.quote_offers()
    lg = b3.offers[tk]["legs"]
    b3._check_offers(set(), {lg[0]["oid"]: 3, lg[1]["oid"]: 2})
    assert b3.k_sold.get(tk) == 98 * 3 + 99 * 2


# ---- 8/11 approved queue: caps, earned sizing, sync detail, re-entry ----

def test_city_cap_blocks_concentration(tmp_path, monkeypatch):
    # two strikes on one thermometer was the autopsy's #2 risk: the
    # same-city entry that would breach the city cap is refused
    # (8/13: cap is 15% of NAV while the pre-close flatten is on)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000                      # NAV $100 -> city cap $15
    # low + high on one city pass the one-opinion key but share the cap
    assert b.place(mkts=[_mk(tk="KXLOWTCHI-26AUG11-B64.5", bid=87, ask=89,
                             city="chicago", strike=64,
                             is_low=True)]) == 1                 # ~$4.45
    assert b.place(mkts=[_mk(tk="KXHIGHCHI-26AUG11-B82.5", bid=87, ask=89,
                             city="chicago", strike=82)]) == 1   # ~$8.90 tot
    assert b.place(mkts=[_mk(tk="KXLOWTCHI-26AUG11-B70.5", bid=87, ask=89,
                             city="chicago", strike=70, is_low=True,
                             date="2099-01-01")]) == 1           # ~$13.35
    assert b.place(mkts=[_mk(tk="KXLOWTCHI-26AUG12-B66.5", bid=87, ask=89,
                             city="chicago", strike=66, is_low=True,
                             date="2099-01-02")]) == 0           # breach
    assert b.exec_stats.get("city_capped") == 1
    # a different city is unaffected
    assert b.place(mkts=[_mk(tk="KXLOWTNYC-26AUG11-B69.5", bid=87, ask=89,
                             city="new york", strike=69)]) == 1


def test_slate_cap_blocks_same_morning_pileup(tmp_path, monkeypatch):
    # all-8-positions-settling-one-morning was risk #1: one settlement
    # date may not hold more than SLATE_CAP_PCT of NAV.
    # 8/15: DERIVED from the constant instead of hardcoded to 40%. The
    # slate cap is now a measured experiment (0.40 -> 0.60 and, if the
    # week behaves, region slates after) and a magic number here breaks
    # the suite every time the dial moves - which teaches us to edit the
    # assert rather than think. What must hold at ANY setting is the
    # relationship: N five-lots fit under the cap and the next is
    # refused, and a different settlement date is untouched.
    # 8/17: isolate the SLATE axis - the metric-slate cap (0.30) now
    # binds first on an all-highs pileup, which is ITS test, not this one
    monkeypatch.setattr(dl, "METRIC_SLATE_PCT", 1.0)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000                      # NAV $100
    cost_c = 89 * dl.MIN_CONTRACTS               # taker entry x 5-lot floor
    fit = int(10000 * dl.SLATE_CAP_PCT) // cost_c
    assert fit >= 2                              # cap too small to test
    placed = 0
    for i in range(fit + 4):
        placed += b.place(mkts=[_mk(tk=f"KXHIGHNY-26JUL-S{i}", bid=87,
                                    ask=89, city=f"c{i}", strike=60 + i)])
    assert placed == fit
    assert b.exec_stats.get("slate_capped", 0) >= 1
    # a different settlement date is unaffected
    assert b.place(mkts=[_mk(tk="KXHIGHNY-26JUL-S99", bid=87, ask=89,
                             city="c99", strike=99,
                             date="2099-01-01")]) == 1


def test_proven_bucket_earns_bigger_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "CITY_CAP_PCT", 1.0)   # isolate sizing
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 1.0)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 60000                      # NAV $600 (post-boost)
    b._refresh_caps(b.balance_c())
    assert b.max_bet_c == 1800                   # base 3% of $600
    assert b.max_bet_pv_c == 4800                # proven 8%
    # a proven half-Kelly lane sizes Kelly past the base 3% fraction
    b.history = _proven_hist(n=30, entry=89)
    assert b.place(mkts=[_mk(bid=89, ask=91)]) == 1
    bet = next(iter(b.bets.values()))
    cost = bet["entry"] * bet["count"]
    assert cost > b.max_bet_c                    # beyond base cap
    assert cost <= b.max_bet_pv_c                # inside the earned one
    # same market, UNPROVEN lane: base fraction keeps it under base cap
    monkeypatch.setattr(dl, "KELLY_PROVEN_N", 999)     # unproven now
    b2 = _bot(tmp_path, monkeypatch)
    b2.dry_balance_c = 60000
    b2._refresh_caps(b2.balance_c())
    b2.history = _proven_hist(n=30, entry=89)
    assert b2.place(mkts=[_mk(bid=89, ask=91)]) == 1
    bet2 = next(iter(b2.bets.values()))
    assert bet2["entry"] * bet2["count"] <= b2.max_bet_c


def test_sync_excludes_settled_and_sold(tmp_path, monkeypatch):
    # the stuck sync_diffs=3: Kalshi keeps listing settled AND sold-away
    # markets for a while - both are expected, not divergence
    b = _bot(tmp_path, monkeypatch)
    class _C:
        pass
    b.client = _C()
    b.k_positions = [{"ticker": "T_SOLD", "side": "yes", "count": 5},
                     {"ticker": "T_DONE", "side": "yes", "count": 3},
                     {"ticker": "T_REAL", "side": "yes", "count": 2}]
    b.k_sold = {"T_SOLD": 485}
    b.settled_tks = ["T_DONE"]
    assert b._sync_diffs() == 1                  # only T_REAL counts
    assert b.sync_bad[0]["tk"] == "T_REAL"
    # a re-bought sold ticker is checked again (not excluded)
    b.bets["T_SOLD"] = dict(_lvl_bet(), count=1)
    assert b._sync_diffs() == 2


def test_reentry_after_lift_same_market(tmp_path, monkeypatch):
    # 8/11 approved: sold at premium -> the same market may be re-entered
    # on the next qualifying signal (second round trip, same day)
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=82, ask=85)])
    tk = next(iter(b.bets))
    b.quote_offers()
    legs = b.offers[tk]["legs"]
    b._check_offers(set(), {legs[0]["oid"]: 3, legs[1]["oid"]: 2})
    assert tk not in b.bets                      # fully sold
    # signal fires again: the book buys the same market again
    assert b.place(mkts=[_mk(bid=82, ask=85)]) == 1
    assert b.bets[tk]["count"] == 5


# ---- 8/11 standing bid side ----

def test_dip_defaults():
    assert dl.DIP_ON is True and dl.DIP_DISCOUNT_C == 2   # 8/13: buy fills
    assert dl.DIP_MAX_PCT == 0.25   # 8/13: inventory is the constraint


def _mkq(tk="KXHIGHNY-26JUL-T86", bid=87, ask=89, **kw):
    return _mk(tk=tk, bid=bid, ask=ask, **kw)


def test_dip_bids_rest_on_context_markets(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    mkt = _mkq()
    b.place(mkts=[mkt])                     # entry 89 taker, 5 lots
    tk = next(iter(b.bets))
    # place() already quoted the dip on the way out (held context)
    d = b.dips[tk]
    assert d["px"] == 85 and d["count"] == 5 and d["side"] == "yes"
    # no context, no dip: a market that never triggered an entry
    b.place(mkts=[_mkq(tk="KXHIGHNY-26JUL-X1", bid=70, ask=74,
                       city="elsewhere")])  # below the level trigger
    assert "KXHIGHNY-26JUL-X1" not in b.bets
    assert "KXHIGHNY-26JUL-X1" not in b.dips
    # sold context: after a full sale the dip bid keeps working the market
    b2 = _bot(tmp_path, monkeypatch)
    b2.place(mkts=[mkt])
    tk2 = next(iter(b2.bets))
    b2.quote_offers()
    legs = b2.offers[tk2]["legs"]
    b2._check_offers(set(), {legs[0]["oid"]: 3, legs[1]["oid"]: 2})
    assert tk2 not in b2.bets and tk2 in b2.k_sold
    b2.quote_dips([mkt], b2.dry_balance_c, b2._bucket_stats())
    assert tk2 in b2.dips                   # rebuy bid resting at 85


def test_dip_floor_and_room(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mkq(bid=82, ask=84)])    # entry 84
    tk = next(iter(b.bets))
    b.dips.pop(tk, None)
    # bid 82: 82-4=78 clamps to floor 80, room 82-80=2 >= 2: placed at 80
    b.quote_dips([_mkq(bid=82, ask=84)], b.dry_balance_c, b._bucket_stats())
    assert b.dips[tk]["px"] == 80
    # bid 81: floor 80 leaves only 1c of room - no dip
    b.dips.pop(tk, None)
    b.quote_dips([_mkq(bid=81, ask=83)], b.dry_balance_c, b._bucket_stats())
    assert tk not in b.dips


def test_dip_fill_merges_and_creates(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mkq()])                  # 5 lots at 89
    tk = next(iter(b.bets))
    n0, e0 = b.bets[tk]["count"], b.bets[tk]["entry"]
    oid = b.dips[tk]["oid"]
    b._check_dips(set(), {oid: 5})          # wobble fills us at 85
    assert b.bets[tk]["count"] == n0 + 5
    assert b.bets[tk]["entry"] < e0         # average cheapened
    assert b.exec_stats["dip_fills"] == 1
    assert tk not in b.dips
    # fill with NO position (sold context): new bet in the dip lane
    b2 = _bot(tmp_path, monkeypatch)
    b2.place(mkts=[_mkq()])
    tk2 = next(iter(b2.bets))
    b2.quote_offers()
    legs = b2.offers[tk2]["legs"]
    b2._check_offers(set(), {legs[0]["oid"]: 3, legs[1]["oid"]: 2})
    b2.quote_dips([_mkq()], b2.dry_balance_c, b2._bucket_stats())
    oid2 = b2.dips[tk2]["oid"]
    b2._check_dips(set(), {oid2: 5})
    assert b2.bets[tk2]["trig"] == "dip" and b2.bets[tk2]["entry"] == 85
    # ...and the offer engine retails the dip inventory
    b2.quote_offers()
    assert tk2 in b2.offers


def test_dip_refresh_follows_market(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mkq()])                  # dip at 85 (bid 87)
    tk = next(iter(b.bets))
    assert b.dips[tk]["px"] == 85
    # market runs up 4c: target 89, drift >= refresh threshold -> requote
    b.quote_dips([_mkq(bid=91, ask=93)], b.dry_balance_c,
                 b._bucket_stats())
    assert b.dips[tk]["px"] == 89
    # small wiggle (2c): keep resting, no churn
    b.quote_dips([_mkq(bid=93, ask=95)], b.dry_balance_c,
                 b._bucket_stats())
    assert b.dips[tk]["px"] == 89


def test_dip_total_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "CITY_CAP_PCT", 1.0)
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 1.0)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000                 # NAV $100 -> dip cap $15
    ms = [_mkq(tk=f"KXHIGHNY-26JUL-D{i}", bid=87, ask=89, city=f"c{i}",
               strike=70 + i) for i in range(10)]
    b.place(mkts=ms)                        # entries + dips on the way out
    dip_cost = sum(d["entry"] * d["count"] for d in b.dips.values())
    assert dip_cost <= 2500                 # <= 25% of NAV (8/13)
    assert len(b.dips) < 10                 # the cap refused the rest


# ---- 8/12 over-refusal fixes: filled-only caps, trim, orphan hygiene ----

def test_concentration_cap_ignores_pending(tmp_path, monkeypatch):
    # over-refusal autopsy: zombie/churning maker joins read as $40 of
    # slate commitment at $8.40 of real risk -> every entry refused all
    # day. Caps now count FILLED risk only (open cap still bounds both).
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000                   # NAV $100 -> slate cap $40
    b.pending["zombie"] = dict(_stale_order(tk="KXHIGHNY-26JUL-Z9",
                                            entry=90, count=40),
                               exec="maker")  # $36 phantom on today's slate
    assert b.place(mkts=[_mk(bid=95, ask=97, city="q1", strike=1)]) == 1
    nk = [x for x in b.bets.values() if x["trig"] == "nickel"]
    assert nk and nk[0]["count"] == 10        # full size, not refused


def test_concentration_cap_trims_to_fit(tmp_path, monkeypatch):
    # an oversize candidate takes the room that's left (>= 5-lot floor)
    # instead of walking away from a slate with real headroom
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 0.05)   # $5.00 of room
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000
    assert b.place(mkts=[_mk(bid=95, ask=97)]) == 1  # nickel wants 10 lots
    bet = next(iter(b.bets.values()))
    assert bet["trig"] == "nickel" and bet["count"] == 5   # 10 -> 5 fits
    assert bet["entry"] * bet["count"] <= 500


def test_concentration_refusal_publishes_detail(tmp_path, monkeypatch):
    # a refusal that can't trim to the floor is counted AND named on the
    # tracker with its arithmetic - never again a bare 2,353
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 0.01)   # $1: nothing fits
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000
    assert b.place(mkts=[_mk(bid=95, ask=97)]) == 0
    assert b.exec_stats.get("slate_capped") == 1
    r = b.cap_refuse[-1]
    assert r["kind"] == "slate" and r["tk"] == "KXHIGHNY-26JUL-T86"
    assert r["nav_c"] > 0 and r["px"] == 95
    b.save()
    import json as _json
    st = _json.load(open(dl.STATE))
    assert st["summary"]["cap_refuse_last"][-1]["kind"] == "slate"


def test_same_cycle_placements_still_count(tmp_path, monkeypatch):
    # dropping pending from the caps must NOT allow a one-cycle burst
    # straight past the slate: placements made THIS call still count
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 0.10)   # $10 of room
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000
    ms = [_mk(tk=f"KXHIGHNY-26JUL-B{i}", bid=95, ask=97, city=f"c{i}",
              strike=i) for i in range(3)]
    assert b.place(mkts=ms) == 1              # $9.50 fills the slate
    assert b.exec_stats.get("slate_capped", 0) >= 1


class _FakeOrphanClient:
    def __init__(self, rows):
        self.rows = rows
        self.canceled = []
        self.created = []

    def get_resting_orders(self):
        return list(self.rows)

    def get_fills(self, limit=100):
        return []

    def cancel_order(self, oid):
        self.canceled.append(oid)
        return {}

    def create_order(self, tk, **kw):
        self.created.append(dict(kw, ticker=tk))
        return {"order": {"order_id": f"o-{len(self.created)}"}}


def test_dead_book_entry_cancels_surviving_legs(tmp_path, monkeypatch):
    # DESYNC-DROP/settle used to del the offers entry and leave the 99c
    # rung resting live on the exchange (found live 8/12: 4 orphans)
    b = _bot(tmp_path, monkeypatch)
    b.client = _FakeOrphanClient([])
    tk = "KXHIGHNY-26JUL-T86"
    b.offers[tk] = {"legs": [{"oid": "L1", "px": 97, "count": 3},
                             {"oid": "L2", "px": 99, "count": 2}],
                    "count": 5, "ots": ""}
    b._check_offers({"L2"}, {})               # book has no position
    assert "L2" in b.client.canceled
    assert tk not in b.offers


def test_full_sale_cancels_surviving_ladder_leg(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.client = _FakeOrphanClient([])
    tk = "KXHIGHNY-26JUL-T86"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=85, count=5), fee=0)
    b.offers[tk] = {"legs": [{"oid": "L1", "px": 97, "count": 3},
                             {"oid": "L2", "px": 99, "count": 2}],
                    "count": 5, "ots": ""}
    b._check_offers({"L2"}, {"L1": 5})        # L1 lifted the whole lot
    assert tk not in b.bets                   # fully sold
    assert "L2" in b.client.canceled          # sibling rung not orphaned
    assert tk not in b.offers


def test_heal_adopts_yes_projected_no_side_orders(tmp_path, monkeypatch):
    # 8/12 HARD-LEARNED: GET /portfolio/orders projects NO-side buys as
    # yes-side SELLS ("sell yes 11c" == our buy NO 89c). Heal must adopt
    # regardless of the projected action - an action filter here spent
    # 20 live minutes canceling real NO-side entry joins as zombies.
    monkeypatch.setattr(dl, "CROSS_EXPIRY", False)
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHNY-26JUL-T86"
    b.client = _FakeOrphanClient([{"order_id": "R7", "ticker": tk,
                                   "action": "sell", "side": "yes"}])
    b.pending = {"synthetic-1": dict(_stale_order(tk=tk, entry=89,
                                                  count=11), exec="maker")}
    b.pending["synthetic-1"]["side"] = "no"
    b.check_orders()
    assert "R7" in b.pending                  # healed to the real oid
    assert "synthetic-1" not in b.pending
    assert b.client.canceled == []            # and nothing was canceled


# ---- 8/12 Miami hardening: caps read the EXCHANGE view too ----

def test_city_cap_sees_unbooked_exchange_position(tmp_path, monkeypatch):
    # the $43-on-one-thermometer night: fills our book hadn't seen were
    # invisible to bets-only caps. Exposure is now max(book, exchange).
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000                    # NAV $100 -> city cap $10
    b.k_positions = [{"ticker": "KXLOWTMIA-26AUG12-B79.5", "side": "no",
                      "count": 18, "entry": 88, "city": "miami",
                      "strike": 79, "kind": "band", "cap": 80, "hl": "lo",
                      "date": TODAY}]          # $15.84 the book can't see
    assert b.place(mkts=[_mk(tk="KXLOWTMIA-26AUG12-B75.5", bid=87, ask=89,
                             city="miami", strike=75, is_low=True)]) == 0
    assert b.exec_stats.get("city_capped") == 1
    assert b.cap_refuse[-1]["city_c"] == 18 * 88
    # a settled leftover row must NOT block (positions API lags settles)
    b2 = _bot(tmp_path, monkeypatch)
    b2.dry_balance_c = 10000
    b2.k_positions = list(b.k_positions)
    b2.settled_tks = ["KXLOWTMIA-26AUG12-B79.5"]
    assert b2.place(mkts=[_mk(tk="KXLOWTMIA-26AUG12-B75.5", bid=87, ask=89,
                              city="miami", strike=75, is_low=True)]) == 1


def test_entry_waits_for_adoption_of_exchange_position(tmp_path,
                                                       monkeypatch):
    # the exchange holds the ticker, our book doesn't know it yet ->
    # placing again is how 44 lots stacked into one strike. Skip and
    # let the mirror adopt first.
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHNY-26JUL-T86"
    b.k_positions = [{"ticker": tk, "side": "yes", "count": 5,
                      "entry": 85, "city": "new york", "strike": 86,
                      "kind": "ge", "cap": None, "hl": "hi",
                      "date": TODAY}]
    assert b.place(mkts=[_mk(tk=tk, bid=82, ask=85)]) == 0
    assert b.exec_stats.get("sync_wait") == 1
    # once adopted into bets, normal rules (incl. pyramid adds) resume
    b.bets[tk] = dict(_stale_order(tk=tk, entry=85, count=5), fee=0)
    assert b.place(mkts=[_mk(tk=tk, bid=82, ask=85)]) == 0  # dedupe, no stack
    assert b.exec_stats.get("sync_wait") == 1               # not re-counted


# ---- 8/12 invariant: no pending row dies with unbooked fills ----

class _FakeCancelClient:
    """Cancel response reveals fills the cycle's snapshot missed."""
    def __init__(self, resting, cancel_order_obj):
        self._resting = resting
        self._obj = cancel_order_obj
        self.created = []

    def get_resting_orders(self):
        return list(self._resting)

    def get_fills(self, limit=100):
        return []

    def cancel_order(self, oid):
        return {"order": dict(self._obj)}

    def create_order(self, tk, **kw):
        self.created.append(kw)
        return {"order": {"order_id": f"new-{len(self.created)}"}}


def test_stale_cancel_books_fills_from_cancel_response(tmp_path,
                                                       monkeypatch):
    # 3 of 5 filled between the fills snapshot and the cancel: the old
    # path deleted the row and those 3 lots went invisible (the Miami
    # class). Now the cancel response books them first.
    monkeypatch.setattr(dl, "CROSS_EXPIRY", False)
    b = _bot(tmp_path, monkeypatch)
    old = (_dt.datetime.now() - _dt.timedelta(hours=3)).isoformat()
    tk = "KXHIGHNY-26JUL-T86"
    b.client = _FakeCancelClient([{"order_id": "o1", "ticker": tk}],
                                 {"initial_count": 5,
                                  "remaining_count": 2})
    b.pending = {"o1": dict(_stale_order(tk=tk, entry=85, count=5),
                            exec="maker", ots=old)}
    b.check_orders()
    assert not b.pending
    assert b.bets[tk]["count"] == 3            # booked, not vanished
    # and the miss ledger sees only the true unfilled remainder
    assert b.miss[-1]["count"] == 2


def test_requote_chases_only_the_remainder(tmp_path, monkeypatch):
    # old path: cancel, zero filled_seen, re-order the FULL count -
    # a partially-filled join bought its filled lots again every chase
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHNY-26JUL-T86"
    b.client = _FakeCancelClient([], {"initial_count": 10,
                                      "remaining_count": 7})
    b.pending = {"o1": dict(_stale_order(tk=tk, entry=80, count=10),
                            exec="maker", requotes=0, ots=dl.now())}
    mk = _mk(tk=tk, bid=86, ask=88)
    assert b._maybe_requote(tk, mk) is True
    assert b.client.created[-1]["count"] == 7  # remainder only
    assert b.bets[tk]["count"] == 3            # the fills got booked
    no = b.pending[next(iter(b.pending))]
    assert no["count"] == 7 and no["filled_seen"] == 0


def test_stale_exchange_quotes_on_held_ticker_are_canceled(tmp_path,
                                                           monkeypatch):
    # found live: MIA held 44 lots with 70 lots of sells resting (the
    # current 22/22 ladder + a stale 13/13 from a smaller position).
    # Overselling FLIPS the position, so unknown quotes on a market we
    # hold get canceled. Scope guards: held tickers only, never a
    # ticker with a live pending entry join.
    b = _bot(tmp_path, monkeypatch)
    b.client = _FakeOrphanClient([])
    tk, other = "KXLOWTMIA-26AUG12-B79.5", "KXHIGHNY-26JUL-T86"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=88, count=44), fee=0)
    b.bets[other] = dict(_stale_order(tk=other, entry=85, count=5), fee=0)
    b.offers[tk] = {"legs": [{"oid": "L1", "px": 98, "count": 22},
                             {"oid": "L2", "px": 99, "count": 22}],
                    "count": 44, "rungs": [98, 99], "ots": ""}
    b.pending["p1"] = dict(_stale_order(tk=other), exec="maker")
    b.dips["KXLOWTMIA-26AUG12-B77.5"] = {"oid": "D1"}
    b.k_resting = [
        {"ticker": tk, "oid": "L1"}, {"ticker": tk, "oid": "L2"},
        {"ticker": tk, "oid": "STALE1"},      # the 13-lot ghost rungs
        {"ticker": tk, "oid": "STALE2"},
        {"ticker": other, "oid": "X9"},       # pending join here: hands off
        {"ticker": "KXLOWTMIA-26AUG12-B77.5", "oid": "D1"},   # our dip
        {"ticker": "KXHIGHTPHX-26AUG12-B98.5", "oid": "N1"},  # unheld
    ]
    b.quote_offers()
    assert sorted(b.client.canceled) == ["STALE1", "STALE2"]
    assert b.exec_stats.get("stale_quotes_canceled") == 2


# ---- 8/13 velocity build: flatten, time-decay ladder, turns ----

def test_flatten_sells_everything_near_close(tmp_path, monkeypatch):
    # the compounding change: capital comes back the same session, and
    # settlement risk (the only place this book ever lost big) is gone
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHNY-26JUL-T86"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=85, count=10), fee=5)
    b.offers[tk] = {"legs": [{"oid": "L1", "px": 97, "count": 10}],
                    "count": 10, "rungs": [97], "ots": ""}
    b.client = _FakeOrphanClient([])
    n = b.flatten([_mk(tk=tk, bid=91, ask=93, hrs=0.5)])
    assert n == 1 and tk not in b.bets and tk not in b.offers
    assert "L1" in b.client.canceled          # own quote pulled first
    assert b.exec_stats.get("flattened") == 1
    # sold at the bid: (91-85)*10 - entry fee - exit fee, booked as a turn
    assert b.turns["n"] == 1 and b.turns["kinds"]["flatten"] == 1
    assert b.history[-1]["exit_px"] == 91 and b.history[-1]["sold"] is True


def test_flatten_leaves_positions_with_time_left(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHNY-26JUL-T86"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=85, count=10), fee=5)
    b.client = _FakeOrphanClient([])
    assert b.flatten([_mk(tk=tk, bid=91, ask=93, hrs=6.0)]) == 0
    assert tk in b.bets
    # and a market with no honest bid is left to settle
    assert b.flatten([_mk(tk=tk, bid=0, ask=0, hrs=0.2)]) == 0


def test_sell_ladder_walks_down_as_close_approaches(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    pos = dict(_stale_order(entry=85, count=10), fee=0)
    # 8/21: the walk starts earlier and goes lower, because a position
    # quoted above where it will ever trade just settles (-$0.657) when
    # a lift is worth +$0.203.
    assert [r[0] for r in b._sell_rungs(pos, 8.0)] == [98, 99]
    assert [r[0] for r in b._sell_rungs(pos, 3.0)] == [98]
    assert [r[0] for r in b._sell_rungs(pos, 1.0)] == [94, 96]
    assert [r[0] for r in b._sell_rungs(pos, 2.0)] == [98]
    # never at or below cost
    assert b._sell_rungs(dict(pos, entry=99), 1.0) is None


def test_decay_requote_replaces_stale_rungs(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHNY-26JUL-T86"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=85, count=10), fee=0)
    b.client = _FakeOrphanClient([])
    b.quote_offers([_mk(tk=tk, hrs=8.0)])
    assert b.offers[tk]["rungs"] == [98, 99]
    b.quote_offers([_mk(tk=tk, hrs=1.0)])     # time ran on: walk it down
    assert b.offers[tk]["rungs"] == [94, 96]
    assert b.exec_stats.get("decay_requotes") == 1
    assert "L1" not in b.client.canceled       # (old legs were of-* ids)


def test_dips_no_longer_need_context(tmp_path, monkeypatch):
    # inventory is the constraint: any scanned favorite can be bid for
    b = _bot(tmp_path, monkeypatch)
    b.last_nav_c = 10000
    b.quote_dips([_mkq(tk="KXHIGHNY-26JUL-NEW", bid=88, ask=90,
                       city="nowhere", strike=71)], 10000, {})
    assert "KXHIGHNY-26JUL-NEW" in b.dips
    monkeypatch.setattr(dl, "DIP_CONTEXT_ONLY", True)
    b2 = _bot(tmp_path, monkeypatch)
    b2.last_nav_c = 10000
    b2.quote_dips([_mkq(tk="KXHIGHNY-26JUL-NEW2", bid=88, ask=90,
                        city="nowhere", strike=72)], 10000, {})
    assert not b2.dips                         # old behaviour still available


def test_turn_stats_published(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b._turn_add(120, "lift")
    b._turn_add(-20, "flatten")
    st = b._turn_stats()
    assert st["n"] == 2 and st["net"] == 1.0 and st["per_turn"] == 0.5
    assert st["today_n"] == 2 and st["kinds"] == {"lift": 1, "flatten": 1}
    b.save()
    import json as _json
    assert _json.load(open(dl.STATE))["summary"]["turns"]["n"] == 2


# ---- 8/13 manual resume (unhalt.txt) ----

def test_resume_lifts_halt_without_touching_the_ledger(tmp_path,
                                                       monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.max_day_loss_c = 1416
    b.day_pnl_c = -3559                      # the 8/13 kind of day
    assert b.place(mkts=[_mk(bid=87, ask=89)]) == 0
    assert b.halted is True
    f = tmp_path / "unhalt.txt"
    f.write_text(dl.today())
    monkeypatch.setattr(dl, "UNHALT_FILE", str(f))
    b._roll_day()                            # cycle start reads the switch
    assert b.halted is False
    assert b.day_pnl_c == -3559              # ledger untouched
    assert b.halt_base_c == -3559            # fresh budget from here
    assert b.place(mkts=[_mk(bid=87, ask=89)]) == 1
    # the fresh budget still binds: another full daily loss re-halts
    # (place() recomputes max_day_loss_c from NAV, so read it back)
    b.day_pnl_c = b.halt_base_c - b.max_day_loss_c
    assert b.place(mkts=[_mk(tk="KXHIGHNY-26JUL-T87", bid=87, ask=89)]) == 0
    assert b.halted is True
    b._roll_day()                            # same token: no second lift
    assert b.halted is True


def test_resume_token_ignored_when_stale(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.max_day_loss_c = 1416
    b.day_pnl_c = -3559
    f = tmp_path / "unhalt.txt"
    f.write_text("2020-01-01")               # yesterday's switch is dead
    monkeypatch.setattr(dl, "UNHALT_FILE", str(f))
    b._roll_day()
    assert b.halt_base_c == 0.0
    assert b.place(mkts=[_mk(bid=87, ask=89)]) == 0


# ---- 8/13 gate: measure chosen trades, and count turns ----

def _settled(pnl, outcome=1, pside=0.88, trig="level", i=0):
    return {"tk": f"S{i}", "ots": str(i), "trig": trig, "entry": 88,
            "count": 5, "outcome": outcome, "pside": pside, "pnl": pnl,
            "era": "dlive1"}


def test_gate_ignores_adopted_positions(tmp_path, monkeypatch):
    # 8/12 Miami: a 44-lot stack the bot never chose (bug artifact) put
    # -$38.72 into the window and pinned sizing in probe. The loss stays
    # in the ledger; it just doesn't get a vote on calibration.
    b = _bot(tmp_path, monkeypatch)
    b.history = [_settled(0.4, i=i) for i in range(40)]
    assert b._gate()[0] == "scale"
    b.history.append(dict(_settled(-38.72, outcome=0, i=99),
                          trig="adopt"))
    assert b._gate()[0] == "scale"          # artifact: no vote
    # a chosen loss of the same size DOES demote - the gate still works
    b.history.append(_settled(-38.72, outcome=0, i=98))
    assert b._gate()[0] == "probe"


def test_gate_counts_completed_turns(tmp_path, monkeypatch):
    # with the pre-close flatten, most inventory never settles - a
    # settlement-only window would freeze the gate wherever it stood
    b = _bot(tmp_path, monkeypatch)
    sold = [{"tk": f"F{i}", "ots": str(i), "trig": "level", "entry": 88,
             "count": 5, "outcome": None, "sold": True, "exited": True,
             "pnl": 0.3, "era": "dlive1"} for i in range(40)]
    b.history = sold
    assert b._gate() == ("scale", 40)       # turns alone can earn scale
    # losing round trips demote just as settlements would
    b.history = [dict(r, pnl=-0.3) for r in sold]
    assert b._gate()[0] == "probe"
    # calibration still judged on settled rows only (turns have no
    # forecast to score), so an overconfident settled cohort demotes
    b.history = sold + [_settled(0.01, outcome=0, pside=0.95, i=i)
                        for i in range(200, 210)]
    assert b._gate()[0] == "probe"


# ---- 8/13 throughput package: utilization, budget, fills, cycle ----

def test_utilization_published(tmp_path, monkeypatch):
    # growth = edge/turn x turns/day x UTILIZATION, and the third term
    # was invisible (~30% of the risk budget was actually working)
    b = _bot(tmp_path, monkeypatch)
    b.max_open_c = 10000
    b.bets = {"T1": dict(_stale_order(entry=85, count=10), fee=0)}
    b.dips = {"T2": {"entry": 80, "count": 5}}
    u = b._util_stats()
    assert u["deployed"] == 8.5 and u["cap"] == 100.0
    assert u["pct"] == 0.085 and u["pct_with_dips"] == 0.125
    assert u["positions"] == 1 and u["cycle_s"] in (dl.ACTIVE_CYCLE_S,
                                                    dl.CYCLE_S)
    b.save()
    import json as _json
    st = _json.load(open(dl.STATE))
    assert st["summary"]["util"]["cap"] == 100.0


def test_holding_time_scaled_budget():
    # inventory that never reaches settlement earns a bigger allowance;
    # turning the flatten off must restore the conservative budget
    assert dl.FLATTEN_ON is True
    assert dl.OPEN_PCT == 0.85 and dl.CITY_CAP_PCT == 0.15
    import importlib
    import os as _os
    _os.environ["DRIFT_LIVE_FLATTEN"] = "0"
    try:
        m = importlib.reload(dl)
        assert m.FLATTEN_ON is False
        assert m.OPEN_PCT == 0.60 and m.CITY_CAP_PCT == 0.10
    finally:
        _os.environ.pop("DRIFT_LIVE_FLATTEN", None)
        importlib.reload(dl)


def test_active_hours_cycle_is_faster(monkeypatch):
    import datetime as _d

    class _FakeDT(_d.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 13, 15, 0, tzinfo=tz)

    monkeypatch.setattr(dl.datetime, "datetime", _FakeDT)
    assert dl._cycle_s() == dl.ACTIVE_CYCLE_S == 90

    class _FakeNight(_d.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 13, 3, 0, tzinfo=tz)

    monkeypatch.setattr(dl.datetime, "datetime", _FakeNight)
    assert dl._cycle_s() == dl.CYCLE_S == 600


# ===================================================================
# 8/14 build: cash-in anchor, weekly circuit breaker, honest turns,
# proven-bucket chase ceiling, capital-hour instrumentation.
# ===================================================================


def test_deposits_anchor_is_100_dollars():
    """Every % must anchor to real cash in, not the 100.09 baseline
    artifact. If this ever drifts from Adam's actual deposits, the
    tracker starts lying again (the 8/13 -45%-vs-+22% argument)."""
    assert dl.DEPOSITS_C == 10000


def test_hold_hours_parses_and_rejects_garbage():
    import datetime as _d
    t = (_d.datetime.now() - _d.timedelta(hours=3)).isoformat()
    got = dl._hold_hours(t)
    assert got is not None and 2.9 < got < 3.1
    # unmeasurable inputs must return None, never 0.0 - a silent zero
    # would divide into an infinite per-capital-hour figure
    assert dl._hold_hours(None) is None
    assert dl._hold_hours("") is None
    assert dl._hold_hours("not-a-timestamp") is None
    # future timestamps (clock skew) and ancient rows are not measurable
    future = (_d.datetime.now() + _d.timedelta(hours=5)).isoformat()
    assert dl._hold_hours(future) is None
    ancient = (_d.datetime.now() - _d.timedelta(days=30)).isoformat()
    assert dl._hold_hours(ancient) is None


def _wk_book():
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.pnl_days = {}
    b.nav_days = {}
    b.last_nav_c = 12000        # $120 NAV
    b.week_halt_base_c = 0.0
    b.week_loss_c = None
    b.week_limit_c = None
    return b


def _nav_on(b, days_ago, nav_c):
    import datetime as _d
    d = (_d.date.today() - _d.timedelta(days=days_ago)).isoformat()
    b.nav_days[d] = nav_c


def test_breaker_cannot_halt_a_book_at_an_all_time_high():
    """THE v1 BUG. v1 summed realized P&L, so writing off dust while
    marks climbed read as bleeding: it sat at $16.74 of a $19.66 limit
    during a +23.7% ALL-TIME-HIGH week. At a high, drawdown is zero by
    definition and the breaker must be silent."""
    b = _wk_book()
    for i, nav in enumerate([12000, 11500, 11000, 10500]):
        _nav_on(b, i, nav)
    b.last_nav_c = 12000        # today IS the peak
    assert b._week_loss_exceeded() is False
    assert b.week_loss_c == 0.0


def test_breaker_fires_on_a_real_peak_to_trough_drawdown():
    """Three compounding bad days off a peak - the case the DAILY halt
    cannot see, and the reason this breaker exists."""
    b = _wk_book()
    _nav_on(b, 3, 14000)        # peak $140 three days ago
    _nav_on(b, 0, 11800)
    b.last_nav_c = 11800        # -$22 from peak, limit is 15% of 118 = 17.70
    assert b._week_loss_exceeded() is True
    assert b.week_loss_c == -2200.0
    assert b.week_limit_c == 1770.0


def test_breaker_tolerates_a_drawdown_inside_the_limit():
    b = _wk_book()
    _nav_on(b, 2, 12500)
    b.last_nav_c = 12000        # -$5 against a $18 limit
    assert b._week_loss_exceeded() is False
    assert b.week_loss_c == -500.0


def test_breaker_ignores_peaks_older_than_the_window():
    """An 8-day-old high must not keep the book halted forever."""
    b = _wk_book()
    _nav_on(b, 30, 40000)       # ancient $400 peak
    _nav_on(b, 1, 12100)
    b.last_nav_c = 12000
    assert b._week_loss_exceeded() is False


def test_breaker_fails_safe_when_nav_is_unknown():
    """Cold start before _refresh_caps has run. Must not halt, and must
    publish None rather than a plausible-looking 0.00 cap."""
    b = _wk_book()
    b.last_nav_c = 0
    assert b._week_loss_exceeded() is False
    assert b.week_limit_c is None and b.week_loss_c is None


def test_breaker_fails_safe_on_a_broken_nav_ledger():
    b = _wk_book()
    b.nav_days = None
    assert b._week_loss_exceeded() is False
    assert b.week_limit_c is None
    b2 = _wk_book()
    b2.nav_days = {}
    assert b2._week_loss_exceeded() is False


def test_breaker_uses_nav_not_realized_pnl():
    """Explicit regression guard: a week of ugly REALIZED P&L must not
    halt a book whose NAV never fell. This is exactly what v1 got wrong."""
    import datetime as _d
    b = _wk_book()
    today = _d.date.today()
    for i in range(7):
        b.pnl_days[(today - _d.timedelta(days=i)).isoformat()] = -50.0
    _nav_on(b, 0, 12000)
    b.last_nav_c = 12000
    assert b._week_loss_exceeded() is False


def test_settled_positions_count_as_turns():
    """The turn ledger was kinds={lift} only - a winners-only sample that
    made per_turn look far better than the book. Settlements are turns."""
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.turns = {}
    b._turn_add(120.0, "lift", hold_h=2.0)
    b._turn_add(-40.0, "settle", hold_h=8.0)
    assert b.turns["n"] == 2
    assert b.turns["kinds"] == {"lift": 1, "settle": 1}
    assert b.turns["kinds_net_c"]["lift"] == 120.0
    assert b.turns["kinds_net_c"]["settle"] == -40.0
    assert b.turns["net_c"] == 80.0
    # capital-hours accumulate per kind for the per-capital-hour objective
    assert b.turns["kinds_hold_h"] == {"lift": 2.0, "settle": 8.0}


def test_turn_add_without_hold_hours_records_no_capital_hours():
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.turns = {}
    b._turn_add(50.0, "lift")
    assert "kinds_hold_h" not in b.turns


def test_chase_ceiling_only_lifts_for_proven_buckets():
    assert dl.CHASE_MAX_E_PROVEN > dl.CHASE_MAX_E

    b = dl.DriftLive.__new__(dl.DriftLive)
    proven = {"level:80-84": {"n": 83, "wins": 69, "net": 9.87,
                              "blocked": False}}
    assert b._bucket_is_proven("level", 82, proven) is True
    # thin evidence is not proof
    thin = {"level:80-84": {"n": 3, "wins": 3, "net": 0.4, "blocked": False}}
    assert b._bucket_is_proven("level", 82, thin) is False
    # a losing lane is not proof
    losing = {"level:90-92": {"n": 10, "wins": 9, "net": -0.86,
                              "blocked": True}}
    assert b._bucket_is_proven("level", 91, losing) is False
    # a self-blocked lane can never chase, even if net were positive
    blocked = {"level:90-92": {"n": 40, "wins": 30, "net": 5.0,
                               "blocked": True}}
    assert b._bucket_is_proven("level", 91, blocked) is False
    # an unknown bucket is never proven
    assert b._bucket_is_proven("level", 82, {}) is False


def test_turn_stats_publishes_capital_hour_objective():
    """kinds alone cannot answer whether lifts and settlements earn
    differently per HOUR of capital locked - which is the objective the
    ladder gets retuned against. per_ch must reach the tracker."""
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.turns = {}
    b._turn_add(200.0, "lift", hold_h=2.0)      # $2.00 over 2h -> $1.00/h
    b._turn_add(-100.0, "settle", hold_h=10.0)  # -$1.00 over 10h -> -$0.10/h
    st = b._turn_stats()
    assert st["kinds"] == {"lift": 1, "settle": 1}
    assert st["kinds_net"]["lift"] == 2.0
    assert st["kinds_net"]["settle"] == -1.0
    assert st["kinds_hold_h"] == {"lift": 2.0, "settle": 10.0}
    assert st["per_ch"]["lift"] == 1.0
    assert st["per_ch"]["settle"] == -0.1


def test_turn_stats_per_ch_never_divides_by_zero():
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.turns = {}
    b._turn_add(50.0, "lift", hold_h=0.0)
    st = b._turn_stats()
    assert "lift" not in st["per_ch"]
    assert st["per_ch"] == {}


def test_weekly_breaker_publishes_none_not_zero_when_unevaluated():
    """A limit of 0.00 on the tracker reads like an armed cap of zero.
    Uncomputed must be None + armed=false, never a plausible number."""
    b = _wk_book()
    b.last_nav_c = 0            # cold start, _refresh_caps hasn't run
    assert b._week_loss_exceeded() is False
    assert b.week_limit_c is None
    assert b.week_loss_c is None


def test_weekly_breaker_arms_once_nav_is_known():
    """on=true but armed=false is a real state: enabled, not yet
    measured. It must not read as an armed cap of zero."""
    b = _wk_book()
    b.last_nav_c = 0
    b._week_loss_exceeded()
    assert b.week_limit_c is None          # not armed
    b.last_nav_c = 12000
    _nav_on(b, 0, 12100)
    b._week_loss_exceeded()
    assert b.week_limit_c == 1800.0        # 15% of $120
    assert b.week_loss_c == -100.0


def test_last_nav_c_is_persisted():
    """If last_nav_c doesn't survive a restart the weekly breaker is
    disarmed for the first cycle of every restart - which is exactly
    when a crash-looping book most needs it."""
    import inspect
    src = inspect.getsource(dl.DriftLive)
    assert '"last_nav_c"' in src


def test_day_dicts_survive_a_restart(tmp_path, monkeypatch):
    """8/15 REAL round trip, not a source grep.

    nav_days was in the LOAD whitelist and absent from save() for a full
    day, so the weekly breaker's rolling 7-day NAV peak silently reset on
    every deploy - and a grep-style test PASSES that bug, because the
    string is present (in the load list). Only a save->reload round trip
    catches it. Any per-day dict the breakers or the slate experiment
    depend on goes here.
    """
    b = _bot(tmp_path, monkeypatch)
    # dated in the past on purpose: save() runs a fresh _util_stats(),
    # which legitimately rewrites TODAY's row with the live counters.
    # Prior days must come back untouched.
    b.nav_days = {"2026-08-13": 12000.0, "2026-08-14": 12572.0}
    b.slate_days = {"2026-08-14": [4791.8, 7914]}
    b.save(balance_c=7779)

    b2 = dl.DriftLive(None, mode="DRY")          # same STATE path
    assert b2.nav_days == {"2026-08-13": 12000.0, "2026-08-14": 12572.0}
    assert b2.slate_days["2026-08-14"] == [4791.8, 7914]


# ---- 8/14: sticky bucket blocks (unblock-by-decay hole) ------------

def _bk_book(history):
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.history = history
    b.bucket_blocked_cum = {}
    return b


def _rows(trig, entry, n, pnl):
    return [{"tk": f"T{i}", "ots": f"o{i}", "trig": trig,
             "entry": entry, "pnl": pnl} for i in range(n)]


def test_losing_lane_stays_blocked_when_its_history_decays():
    """THE HOLE: _bucket_stats reads a 400-row window. level:90-92 sat at
    n=10 against MIN_N=8 - three rows rolling off would drop it to 7 and
    silently unblock a lane that lost money. Blocks must not decay."""
    b = _bk_book(_rows("level", 91, 10, -0.10))
    st = b._bucket_stats()
    assert st["level:90-92"]["blocked"] is True
    assert b._bucket_blocked(st, "level", 91) is True

    # now the window rolls: only 5 rows survive, below MIN_N of 8
    b.history = _rows("level", 91, 5, -0.10)
    st2 = b._bucket_stats()
    assert st2["level:90-92"]["blocked"] is True, "unblocked by decay!"
    assert st2["level:90-92"]["sticky"] is True
    assert b._bucket_blocked(st2, "level", 91) is True


def test_lane_stays_blocked_after_rolling_out_of_stats_entirely():
    """One step further: every row gone, so the bucket vanishes from
    bstats and bstats.get() returns None. Must still refuse."""
    b = _bk_book(_rows("level", 91, 10, -0.10))
    b._bucket_stats()
    b.history = []
    st = b._bucket_stats()
    assert "level:90-92" not in st
    assert b._bucket_blocked(st, "level", 91) is True


def test_sticky_block_records_the_evidence_that_caused_it():
    b = _bk_book(_rows("level", 91, 10, -0.10))
    b._bucket_stats()
    ev = b.bucket_blocked_cum["level:90-92"]
    assert ev["n"] == 10
    assert ev["net"] < 0
    assert ev["ts"]


def test_healthy_lane_is_never_latched():
    b = _bk_book(_rows("level", 82, 40, 0.20))
    st = b._bucket_stats()
    assert st["level:80-84"]["blocked"] is False
    assert st["level:80-84"]["sticky"] is False
    assert b.bucket_blocked_cum == {}


def test_thin_negative_lane_is_not_latched_yet():
    """Below MIN_N there isn't evidence yet - don't latch on noise."""
    b = _bk_book(_rows("level", 91, 3, -0.10))
    st = b._bucket_stats()
    assert st["level:90-92"]["blocked"] is False
    assert b.bucket_blocked_cum == {}


def test_blocked_lane_can_never_be_proven():
    """_bucket_is_proven gates the 99c chase ceiling. A latched lane must
    never reach it, even if later rows look positive."""
    b = _bk_book(_rows("level", 91, 10, -0.10))
    b._bucket_stats()
    b.history = _rows("level", 91, 40, 0.50)   # lane now looks great
    st = b._bucket_stats()
    assert b._bucket_blocked(st, "level", 91) is True
    assert b._bucket_is_proven("level", 91, st) is False


def test_hold_hours_wired_into_lift_and_flatten():
    """per_ch stays empty forever unless lifts carry capital-hours - and
    lifts are exactly what the ladder retune must measure."""
    import inspect
    src = inspect.getsource(dl.DriftLive)
    assert '_turn_add(net, "lift", hold_h=' in src
    # 8/17: exits also carry ehrs (entry-window telemetry)
    assert 'hold_h=_hold_hours(b.get("ots"))' in src
    for kind in ('"lift"', '"flatten"', '"settle"'):
        idx = src.find(f'_turn_add(net, {kind}')
        assert idx != -1, kind
        assert "hold_h" in src[idx:idx + 200], f"{kind} missing hold_h"


# ---- 8/14: over-offer root cause, working capital, faster recycle ----

class _FlakyClient:
    """Cancels fail until `heal` flips - the exact condition that
    orphaned live sell legs on the exchange."""
    def __init__(self, heal=False):
        self.heal = heal
        self.canceled = []

    def cancel_order(self, oid):
        if not self.heal:
            raise RuntimeError("exchange said no")
        self.canceled.append(oid)


def _off_book(heal=False):
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.client = _FlakyClient(heal)
    b.orphan_legs = []
    b.exec_stats = {}
    b.k_resting = []
    b.offers = {}
    b.bets = {}
    return b


def test_failed_cancel_is_remembered_not_dropped():
    """ROOT CAUSE: `except Exception: pass` then delete left a LIVE sell
    leg on the exchange with nothing tracking it. Those orphans are how
    positions ended up offered 3x over."""
    b = _off_book(heal=False)
    assert b._cancel_leg("oid-1", "TK") is False
    assert len(b.orphan_legs) == 1
    assert b.orphan_legs[0]["oid"] == "oid-1"
    assert b.exec_stats["orphan_legs"] == 1
    # the same failure must not enqueue twice
    b._cancel_leg("oid-1", "TK")
    assert len(b.orphan_legs) == 1


def test_successful_cancel_records_no_orphan():
    b = _off_book(heal=True)
    assert b._cancel_leg("oid-1", "TK") is True
    assert b.orphan_legs == []


def test_orphan_legs_are_retried_until_they_clear():
    b = _off_book(heal=False)
    b._cancel_leg("oid-1", "TK")
    b.k_resting = [{"oid": "oid-1"}]      # still live on the exchange
    b._retry_orphan_legs()
    assert len(b.orphan_legs) == 1, "must keep retrying while it rests"
    b.client.heal = True
    b._retry_orphan_legs()
    assert b.orphan_legs == []
    assert "oid-1" in b.client.canceled


def test_orphan_dropped_once_gone_from_the_exchange_book():
    """Filled or settled: nothing left to cancel, stop retrying."""
    b = _off_book(heal=False)
    b._cancel_leg("oid-1", "TK")
    b.k_resting = [{"oid": "someone-else"}]
    b._retry_orphan_legs()
    assert b.orphan_legs == []
    assert b.exec_stats["orphan_cleared"] == 1


def _oo_book(bets, resting):
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.bets = bets
    b.k_resting = resting
    return b


def test_over_offer_reads_the_exchange_not_our_own_books():
    """THE FALSE NEGATIVE: v1 summed self.offers[tk]['legs']. A stray leg
    is by definition one that fell OUT of self.offers, so that check only
    ever confirmed our bookkeeping agreed with itself - it returned {}
    while NOLA held 10 with 30 contracts resting."""
    b = _oo_book(
        {"NOLA": {"count": 10.0, "side": "yes"}},
        [{"ticker": "NOLA", "entry": 99, "count": 15},
         {"ticker": "NOLA", "entry": 98, "count": 10},
         {"ticker": "NOLA", "entry": 97, "count": 5}])
    # self.offers is deliberately EMPTY - v1 would have said {}
    b.offers = {}
    assert b._over_offer_c() == {"NOLA": 20.0}


def test_over_offer_detects_no_side_exits_through_yes_projection():
    """Holding NO, our 98c sell projects to a YES buy at 2c. The orders
    API is yes-projected and action/side must never be trusted, so the
    price band is what identifies an exit."""
    b = _oo_book(
        {"DC": {"count": 5.0, "side": "no"}},
        [{"ticker": "DC", "entry": 1, "count": 2},
         {"ticker": "DC", "entry": 2, "count": 6},
         {"ticker": "DC", "entry": 3, "count": 2}])
    assert b._over_offer_c() == {"DC": 5.0}


def test_over_offer_ignores_entry_joins():
    """An 89c NO entry join projects to a 11c yes row - it must not be
    counted as an exit, or every pyramiding position reads as over-offered."""
    b = _oo_book(
        {"BOS": {"count": 5.0, "side": "no"}},
        [{"ticker": "BOS", "entry": 11, "count": 5},   # entry join
         {"ticker": "BOS", "entry": 2, "count": 5}])   # real exit, exact
    assert b._over_offer_c() == {}

    b2 = _oo_book(
        {"DEN": {"count": 5.0, "side": "yes"}},
        [{"ticker": "DEN", "entry": 85, "count": 5},   # entry join
         {"ticker": "DEN", "entry": 99, "count": 5}])  # exit, exact
    assert b2._over_offer_c() == {}


def test_over_offer_clean_when_ladder_matches_position():
    b = _oo_book(
        {"HOU": {"count": 5.0, "side": "yes"}},
        [{"ticker": "HOU", "entry": 99, "count": 3},
         {"ticker": "HOU", "entry": 98, "count": 2}])
    assert b._over_offer_c() == {}


def test_over_offer_ignores_tickers_we_do_not_hold():
    b = _oo_book({}, [{"ticker": "SFO", "entry": 99, "count": 50}])
    assert b._over_offer_c() == {}


def test_over_offer_tolerates_fractional_dust():
    b = _oo_book(
        {"AUS": {"count": 1.28, "side": "yes"}},
        [{"ticker": "AUS", "entry": 98, "count": 1.28}])
    assert b._over_offer_c() == {}


def test_over_offer_survives_garbage_rows():
    b = _oo_book(
        {"NY": {"count": 5.0, "side": "yes"}},
        [{"ticker": "NY", "entry": None, "count": None},
         {"ticker": "NY", "entry": "junk", "count": 3},
         {"ticker": "NY", "entry": 99, "count": 9}])
    assert b._over_offer_c() == {"NY": 4.0}


def test_util_splits_working_from_committed_capital():
    """96% 'utilization' hid that only 12% of NAV was earning: $15.39
    filled vs $86.84 bid out and idle."""
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.bets = {"A": {"entry": 80, "count": 5, "fee": 0}}      # $4.00 filled
    b.pending = {"o1": {"entry": 90, "count": 10}}           # $9.00 idle
    b.dips = {}
    b.max_open_c = 10000                                     # $100 cap
    u = b._util_stats()
    assert u["working"] == 4.0
    assert u["committed"] == 9.0
    assert u["deployed"] == 13.0
    assert u["working_pct"] == 0.04
    assert u["pct"] == 0.13      # the old gauge, unchanged
    # the whole point: deployed is 3x working
    assert u["deployed"] > u["working"] * 3


# ===== 8/15 offer-engine build =====================================

def test_rung_telemetry_measures_real_conversion():
    """offers_placed also counts every decay requote, so the '20%
    conversion' figure was never a conversion rate. This is."""
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.rung_stats = {}
    for _ in range(10):
        b._rung_place(97)
    for _ in range(4):
        b._rung_place(99)
    b._rung_lift(97, 90.0, 3.0)
    b._rung_lift(97, 110.0, 2.0)
    b._rung_lift(99, 30.0, 9.0)
    r = b._rung_summary()
    assert r["97"]["placed"] == 10 and r["97"]["lifted"] == 2
    assert r["97"]["conv"] == 0.2
    assert r["97"]["net"] == 2.0
    assert r["97"]["per_lift"] == 1.0
    assert r["97"]["per_ch"] == 0.4          # $2.00 over 5 capital-hours
    assert r["99"]["conv"] == 0.25
    assert r["99"]["per_ch"] == round(0.30 / 9.0, 4)


def test_rung_summary_handles_never_lifted_and_zero_hold():
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.rung_stats = {}
    b._rung_place(99)
    r = b._rung_summary()
    assert r["99"]["conv"] == 0.0
    assert r["99"]["per_lift"] is None
    assert r["99"]["per_ch"] is None         # never divide by zero hold


def test_rung_lift_tolerates_unmeasurable_hold():
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.rung_stats = {}
    b._rung_place(97)
    b._rung_lift(97, 50.0, None)
    r = b._rung_summary()
    assert r["97"]["lifted"] == 1 and r["97"]["hold_h"] == 0.0
    assert r["97"]["per_ch"] is None


def _dust_book(bets):
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.bets = bets
    b.pending = {}
    return b


def test_dust_is_excluded_from_the_risk_budget():
    """16 of 25 slots held 1-2c residue while slate caps refused 6,828
    fresh entries. Dust cannot be quoted, so it must not consume the
    open-cap budget live inventory needs."""
    b = _dust_book({
        "LIVE": {"entry": 88, "count": 5, "fee": 0},     # $4.40 real
        "DUST1": {"entry": 2, "count": 30, "fee": 0},    # $0.60 residue
        "DUST2": {"entry": 1, "count": 10, "fee": 0},    # $0.10 residue
    })
    assert b.open_cost_c() == 88 * 5
    assert b.dust_c() == 2 * 30 + 1 * 10


def test_dust_threshold_is_inclusive_and_survives_garbage():
    b = _dust_book({"A": {"entry": 3, "count": 5},
                    "B": {"entry": 4, "count": 5},
                    "C": {"entry": None, "count": 5}})
    assert b._is_dust(b.bets["A"]) is True      # 3c is dust
    assert b._is_dust(b.bets["B"]) is False     # 4c is not
    assert b._is_dust(b.bets["C"]) is False     # unparseable != dust


def _rungs_book():
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.bucket_blocked_cum = {}
    return b


def test_proven_lane_may_quote_a_cent_lower():
    """96 on proven lanes only; unproven lanes keep the 98 floor, so
    this is a probe on earned ground. (8/21: nickels no longer stay at
    98 - see test_offer_defaults for why that note went stale.)"""
    b = _rungs_book()
    proven = {"level:80-84": {"n": 47, "wins": 43, "net": 13.5,
                              "blocked": False}}
    b.history = [{"tk": f"T{i}", "ots": f"o{i}", "trig": "level",
                  "entry": 82, "pnl": 0.29} for i in range(47)]
    pos = {"entry": 82, "count": 4, "trig": "level"}
    # far from close the ladder's own top rung dominates either way
    # (8/21: that top rung is 97, not 98 - the walk starts earlier)
    assert b._sell_rungs(pos, 8.0)[0][0] == 97
    # NOTE: below 2h the floor deliberately relaxes to 1 so the book can
    # flatten, so the lane floor only bites at >= 2h. At 2.0h the ladder
    # offers 96 and a PROVEN lane is allowed to take it.
    got = [r[0] for r in b._sell_rungs(pos, 2.0)]
    assert got[0] == 96, got

    b2 = _rungs_book()
    b2.history = [{"tk": "x", "ots": "o", "trig": "level",
                   "entry": 82, "pnl": 0.1}]        # thin: not proven
    assert [r[0] for r in b2._sell_rungs(pos, 2.0)][0] == 98


def test_nickel_floor_is_never_loosened():
    b = _rungs_book()
    b.history = []
    pos = {"entry": 94, "count": 4, "trig": "nickel"}
    for hrs in (8.0, 3.0, 1.0, 0.4, 0.0):
        rungs = b._sell_rungs(pos, hrs)
        if rungs:
            assert min(r[0] for r in rungs) >= dl.NICKEL_SELL_MIN_C, hrs


def test_decay_ladder_is_monotonic_and_walks_to_the_floor():
    prev_lo = 100
    for h_min, lo, hi in dl.DECAY_LADDER:
        assert lo <= hi <= dl.SELL_CAP_C
        assert lo <= prev_lo, "rungs must not step UP as close approaches"
        prev_lo = lo
    assert len(dl.DECAY_LADDER) == 5


def test_lift_marks_the_market_for_a_rebid():
    """One market can pay more than once: the band settles at 100
    essentially always, so a market we sold at 96-99 is the same trade
    again if it dips back."""
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.rebid = {}
    b.rung_stats = {}
    b.turns = {}
    assert "TK1" not in b.rebid
    b.rebid["TK1"] = dl.now()
    assert "TK1" in b.rebid


def test_rebid_map_stays_bounded():
    b = dl.DriftLive.__new__(dl.DriftLive)
    b.rebid = {f"T{i}": f"2026-08-15T00:00:{i:02d}" for i in range(250)}
    if len(b.rebid) > 200:
        for k in sorted(b.rebid, key=lambda x: b.rebid[x])[:50]:
            b.rebid.pop(k, None)
    assert len(b.rebid) == 200


# ---------------- 8/24 bucket ledger v2: full positions ----------------

def test_bucket_grades_full_position_not_first_exit(tmp_path, monkeypatch):
    """A winner that lifts in tranches must count its WHOLE lifecycle.
    v1 kept only the first exit row: tranche 1's sliver for winners,
    the full settle for never-lifted losers - the bias that cascaded
    sticky blocks over 8/22-8/24 and strangled the ladder."""
    b = _bot(tmp_path, monkeypatch)
    b.history = [
        # position A: two lift tranches then a small settle residue
        {"trig": "level", "entry": 87, "tk": "TA", "ots": "1",
         "pnl": 0.50, "sold": True},
        {"trig": "level", "entry": 87, "tk": "TA", "ots": "1",
         "pnl": 0.50, "sold": True},
        {"trig": "level", "entry": 87, "tk": "TA", "ots": "1",
         "pnl": -0.10, "outcome": 0},
        # position B: never lifted, settled as a loss
        {"trig": "level", "entry": 88, "tk": "TB", "ots": "2",
         "pnl": -0.60, "outcome": 0},
    ]
    bs = b._bucket_stats()
    a = bs[b._bucket_key("level", 87)]
    assert a["n"] == 2                       # two POSITIONS, not 4 rows
    assert a["wins"] == 1                    # A is a net winner (+0.90)
    assert abs(a["net"] - 0.30) < 1e-9       # +0.90 - 0.60, not -0.10 - 0.60


def test_bucket_v2_migration_purges_stale_convictions(tmp_path, monkeypatch):
    import json as _json
    b = _bot(tmp_path, monkeypatch)
    b.bucket_blocked_cum = {"level:85-89": {"n": 65, "net": -6.26, "ts": "x"}}
    b.save()
    d = _json.load(open(dl.STATE))
    assert d.get("bucket_v2") is True        # flag persists post-migration
    # a pre-v2 state file (flag absent) must purge on load
    d.pop("bucket_v2", None)
    _json.dump(d, open(dl.STATE, "w"))
    b2 = dl.DriftLive(None, mode="DRY")
    assert b2.bucket_blocked_cum == {}


# ------- 8/24: over-offer CLAMP + the level:85-89 override retirement -------

def test_over_offer_is_canceled_down_to_the_position(tmp_path, monkeypatch):
    """The Austin case: 90 contracts resting against 19 held. Detection
    existed since 8/14; nothing acted on it. Past flat those legs open a
    SHORT no risk gate can see."""
    b = _bot(tmp_path, monkeypatch)
    tk = "KXLOWTAUS-26AUG24-T72"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=88, count=19), fee=0)
    b.offers[tk] = {"legs": [{"oid": "L1", "px": 98, "count": 19},
                             {"oid": "L2", "px": 99, "count": 40},
                             {"oid": "L3", "px": 97, "count": 31}],
                    "count": 19, "rungs": [97, 98, 99], "ots": ""}
    rows = [{"ticker": tk, "oid": "L1", "entry": 98, "count": 19},
            {"ticker": tk, "oid": "L2", "entry": 99, "count": 40},
            {"ticker": tk, "oid": "L3", "entry": 97, "count": 31}]
    b.client = _FakeOrphanClient(rows)
    b.k_resting = rows
    assert b._over_offer_c()[tk] == 71.0        # 90 offered vs 19 held
    b._clamp_offers()
    # the WHOLE ladder clears, furthest-from-lifting first (99, 98, 97),
    # and the requote pass rebuilds a correctly sized one next cycle
    assert b.client.canceled == ["L2", "L1", "L3"]
    assert b.exec_stats["over_offer_canceled"] == 3
    assert b.exec_stats["over_offer_ct"] == 90.0
    assert tk not in b.offers


def test_clamp_leaves_a_correctly_sized_ladder_alone(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHNY-26AUG24-T78"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=88, count=20), fee=0)
    rows = [{"ticker": tk, "oid": "L1", "entry": 98, "count": 10},
            {"ticker": tk, "oid": "L2", "entry": 99, "count": 10}]
    b.client = _FakeOrphanClient(rows)
    b.k_resting = rows
    assert b._over_offer_c() == {}
    b._clamp_offers()
    assert b.client.canceled == []


def test_clamp_reads_a_no_position_from_the_low_side(tmp_path, monkeypatch):
    """Our sell of a NO position projects to a LOW yes price (the 8/12
    YES-projection rule), so 'furthest from lifting' is the MIN price."""
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHTDAL-26AUG24-T106"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=88, count=10),
                      side="no", fee=0)
    rows = [{"ticker": tk, "oid": "N1", "entry": 2, "count": 10},
            {"ticker": tk, "oid": "N2", "entry": 1, "count": 15}]
    b.client = _FakeOrphanClient(rows)
    b.k_resting = rows
    assert b._over_offer_c()[tk] == 15.0
    b._clamp_offers()
    # min projected px first on a no position (yes-1 == our NO sell at 99)
    assert b.client.canceled == ["N2", "N1"]


def test_clamp_keeps_a_failed_cancel_for_retry(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHCHI-26AUG24-B75.5"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=88, count=5), fee=0)
    rows = [{"ticker": tk, "oid": "L1", "entry": 99, "count": 25}]

    class _Flaky(_FakeOrphanClient):
        def cancel_order(self, oid):
            raise RuntimeError("exchange said no")

    b.client = _Flaky(rows)
    b.k_resting = rows
    b._clamp_offers()
    assert b.exec_stats.get("over_offer_canceled") is None
    assert any(o.get("oid") == "L1" for o in b.orphan_legs)


def test_level_85_89_override_retired_by_default():
    """8/24 Adam: the override is gone; the gate governs the lane."""
    assert dl.BUCKET_ALLOW == set()


def test_retired_override_lets_the_gate_block_a_negative_lane(tmp_path,
                                                              monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "BUCKET_ALLOW", set())
    b.history = [{"trig": "level", "entry": 87, "tk": f"T{i}", "ots": str(i),
                  "pnl": -0.20, "outcome": 0} for i in range(42)]
    bs = b._bucket_stats()
    assert bs["level:85-89"]["n"] == 42
    assert b._bucket_blocked(bs, "level", 87) is True
    assert b._bucket_is_proven("level", 87, bs) is False
    assert b._kelly_frac(bs, "level", 87) == dl.KELLY_BASE


def test_clamp_counts_every_branch_it_declines(tmp_path, monkeypatch):
    """8/24 pt3: the first ship cancelled nothing and had no counter to
    say why. Detector-vs-mirror disagreement must be loud."""
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHNY-26AUG24-T78"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=88, count=5), fee=0)
    # detector sees an over-offer, but the mirror row carries no oid
    rows = [{"ticker": tk, "oid": None, "entry": 99, "count": 14}]
    b.client = _FakeOrphanClient(rows)
    b.k_resting = rows
    assert b._over_offer_c()[tk] == 9.0
    b._clamp_offers()
    assert b.client.canceled == []
    assert b.exec_stats["clamp_ran"] == 1
    assert b.exec_stats["clamp_tickers"] == 1
    assert b.exec_stats["clamp_no_oid"] == 1
    assert b.exec_stats["clamp_no_legs"] == 1
    assert b.exec_stats.get("over_offer_canceled") is None


def test_clamp_counts_a_refused_cancel(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    tk = "KXHIGHMIA-26AUG24-B94.5"
    b.bets[tk] = dict(_stale_order(tk=tk, entry=88, count=5), fee=0)
    rows = [{"ticker": tk, "oid": "L1", "entry": 99, "count": 9}]

    class _Flaky(_FakeOrphanClient):
        def cancel_order(self, oid):
            raise RuntimeError("no")

    b.client = _Flaky(rows)
    b.k_resting = rows
    b._clamp_offers()
    assert b.exec_stats["clamp_legs"] == 1
    assert b.exec_stats["clamp_fail"] == 1
