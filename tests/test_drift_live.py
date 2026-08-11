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
    ms = [_mk(tk=f"KXHIGHNY-26JUL-N{i}", bid=95, ask=97, city=f"c{i}",
              strike=i) for i in range(7)]
    b2 = _bot(tmp_path, monkeypatch)
    b2.max_open_c = 100000
    b2.dry_balance_c = 100000
    b2.place(mkts=ms)
    assert sum(1 for x in b2.bets.values() if x["trig"] == "nickel") == 5


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
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 1)  # not what this tests
    monkeypatch.setattr(dl, "TAKER_FIRST", False)  # nor is execution
    b3 = _bot(tmp_path, monkeypatch)
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
    b = _bot(tmp_path, monkeypatch)
    b.day_pnl_c = -b.max_day_loss_c
    assert b.place(mkts=[_mk(bid=82, ask=85)]) == 0
    assert b.halted


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
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 1)  # measuring Kelly, not the floor
    b = _bot(tmp_path, monkeypatch)
    b.history = _proven_hist()                 # gate=scale + proven 85-89
    assert b._gate()[0] == "scale"
    assert b.place(mkts=[_mk(bid=87, ask=89)]) == 1
    # 8/11 earned sizing: proven lanes use the 8% Kelly ceiling (was 3%)
    assert next(iter(b.bets.values()))["count"] == 4   # half-Kelly, 8% cap
    monkeypatch.setattr(dl, "KELLY_PROVEN_N", 999)     # same lane, unproven
    b2 = _bot(tmp_path, monkeypatch)
    b2.history = _proven_hist()
    assert b2.place(mkts=[_mk(bid=87, ask=89)]) == 1
    assert next(iter(b2.bets.values()))["count"] == 2  # quarter-Kelly


def test_taker_falls_back_to_maker_when_toll_too_big(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "MIN_CONTRACTS", 1)  # not what this tests
    # regression (7/28): scale mode used to Kelly-size takers AT THE ASK,
    # where f*=0 by construction -> every high-mid tight-spread candidate
    # was dropped entirely (no maker join either). Now: edge measured at
    # the bid; if a taker lot doesn't fit, rest a maker join instead.
    monkeypatch.setattr(dl, "KELLY_PROVEN_N", 999)
    b = _bot(tmp_path, monkeypatch)
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
    assert dl.REST_MAX_H == 0.5          # cross at 30 min, not 45
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
    assert b.max_open_c == 12000                # 60%
    assert b.max_day_loss_c == 2000             # 10%
    # filled positions count toward NAV at cost
    b.bets = {"T1": dict(_lvl_bet(), count=10)}     # +$8.20 basis
    b._refresh_caps(b.balance_c())
    assert b.max_open_c == int(20820 * 0.60)
    # drawdown shrinks caps; floors keep probes viable
    b.bets = {}
    b.dry_balance_c = 3000
    b._refresh_caps(b.balance_c())
    assert b.max_bet_c == 200 and b.max_day_loss_c == 300
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
    assert dl.SELL_MIN_C == 97 and dl.NICKEL_SELL_MIN_C == 98
    assert dl.SELL_CAP_C == 99


def test_premium_offers_quote_all_inventory(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=82, ask=85)])         # level, taker fill at 85
    b.quote_offers()
    tk = next(iter(b.bets))
    off = b.offers[tk]
    # 8/11 ladder: 5 lots split - 3 at the 97 rung, 2 at 99
    assert off["count"] == b.bets[tk]["count"] == 5
    assert [(l["px"], l["count"]) for l in off["legs"]] == [(97, 3), (99, 2)]
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
    assert len(l3) == 1 and l3[0]["px"] == 97 and l3[0]["count"] == 5


def test_offer_fill_books_premium_and_recycles(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(bid=82, ask=85)])
    tk = next(iter(b.bets))
    r0, day0 = b.realized_c, b.day_pnl_c
    b.quote_offers()
    legs = b.offers[tk]["legs"]
    # both rungs lift: 3 @ 97 and 2 @ 99
    b._check_offers(set(), {legs[0]["oid"]: 3, legs[1]["oid"]: 2})
    assert tk not in b.bets and tk not in b.offers
    assert b.realized_c > r0 and b.day_pnl_c > day0
    assert b.history[-1]["exit_px"] == 99 and b.history[-2]["exit_px"] == 97
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
    # 2 of the 97-rung's 3 lift; the 99 leg keeps resting
    b._check_offers({legs[1]["oid"]}, {legs[0]["oid"]: 2})
    assert b.bets[tk]["count"] == 3
    assert len(b.offers[tk]["legs"]) == 1        # surviving 99 leg
    b.quote_offers()                             # resize: requote at 3
    off = b.offers[tk]
    assert off["count"] == 3
    assert [(l["px"], l["count"]) for l in off["legs"]] == [(97, 2), (99, 1)]
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
    assert b3.k_sold.get(tk) == 97 * 3 + 99 * 2


# ---- 8/11 approved queue: caps, earned sizing, sync detail, re-entry ----

def test_city_cap_blocks_concentration(tmp_path, monkeypatch):
    # two strikes on one thermometer was the autopsy's #2 risk: the
    # second same-city entry that would breach 10% of NAV is refused
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000                      # NAV $100 -> city cap $10
    # low + high on one city pass the one-opinion key but share the cap
    assert b.place(mkts=[_mk(tk="KXLOWTCHI-26AUG11-B64.5", bid=87, ask=89,
                             city="chicago", strike=64,
                             is_low=True)]) == 1                 # ~$4.45
    assert b.place(mkts=[_mk(tk="KXHIGHCHI-26AUG11-B82.5", bid=87, ask=89,
                             city="chicago", strike=82)]) == 1   # ~$8.90 tot
    assert b.place(mkts=[_mk(tk="KXLOWTCHI-26AUG12-B66.5", bid=87, ask=89,
                             city="chicago", strike=66, is_low=True,
                             date="2099-01-02")]) == 0           # breach
    assert b.exec_stats.get("city_capped") == 1
    # a different city is unaffected
    assert b.place(mkts=[_mk(tk="KXLOWTNYC-26AUG11-B69.5", bid=87, ask=89,
                             city="new york", strike=69)]) == 1


def test_slate_cap_blocks_same_morning_pileup(tmp_path, monkeypatch):
    # all-8-positions-settling-one-morning was risk #1: one settlement
    # date may not hold more than 40% of NAV
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000                      # NAV $100 -> slate cap $40
    placed = 0
    for i in range(12):
        placed += b.place(mkts=[_mk(tk=f"KXHIGHNY-26JUL-S{i}", bid=87,
                                    ask=89, city=f"c{i}", strike=60 + i)])
    # ~$4.45 each: 8 fit under $40, the 9th breaches
    assert placed == 8
    assert b.exec_stats.get("slate_capped", 0) >= 1
    # a different settlement date is unaffected
    assert b.place(mkts=[_mk(tk="KXHIGHNY-26JUL-S99", bid=87, ask=89,
                             city="c99", strike=99,
                             date="2099-01-01")]) == 1


def test_proven_bucket_earns_bigger_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "CITY_CAP_PCT", 1.0)   # isolate sizing
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 1.0)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 40000                      # NAV $400 (post-boost)
    b._refresh_caps(b.balance_c())
    assert b.max_bet_c == 1200                   # base 3% of $400
    assert b.max_bet_pv_c == 3200                # proven 8%
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
    b2.dry_balance_c = 40000
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
    assert dl.DIP_ON is True and dl.DIP_DISCOUNT_C == 4
    assert dl.DIP_MAX_PCT == 0.15


def _mkq(tk="KXHIGHNY-26JUL-T86", bid=87, ask=89, **kw):
    return _mk(tk=tk, bid=bid, ask=ask, **kw)


def test_dip_bids_rest_on_context_markets(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    mkt = _mkq()
    b.place(mkts=[mkt])                     # entry 89 taker, 5 lots
    tk = next(iter(b.bets))
    # place() already quoted the dip on the way out (held context)
    d = b.dips[tk]
    assert d["px"] == 83 and d["count"] == 5 and d["side"] == "yes"
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
    assert tk2 in b2.dips                   # rebuy bid resting at 83


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
    b._check_dips(set(), {oid: 5})          # wobble fills us at 83
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
    assert b2.bets[tk2]["trig"] == "dip" and b2.bets[tk2]["entry"] == 83
    # ...and the offer engine retails the dip inventory
    b2.quote_offers()
    assert tk2 in b2.offers


def test_dip_refresh_follows_market(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mkq()])                  # dip at 83 (bid 87)
    tk = next(iter(b.bets))
    assert b.dips[tk]["px"] == 83
    # market runs up 4c: target 87, drift >= refresh threshold -> requote
    b.quote_dips([_mkq(bid=91, ask=93)], b.dry_balance_c,
                 b._bucket_stats())
    assert b.dips[tk]["px"] == 87
    # small wiggle (2c): keep resting, no churn
    b.quote_dips([_mkq(bid=93, ask=95)], b.dry_balance_c,
                 b._bucket_stats())
    assert b.dips[tk]["px"] == 87


def test_dip_total_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "CITY_CAP_PCT", 1.0)
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 1.0)
    b = _bot(tmp_path, monkeypatch)
    b.dry_balance_c = 10000                 # NAV $100 -> dip cap $15
    ms = [_mkq(tk=f"KXHIGHNY-26JUL-D{i}", bid=87, ask=89, city=f"c{i}",
               strike=70 + i) for i in range(6)]
    b.place(mkts=ms)                        # entries + dips on the way out
    dip_cost = sum(d["entry"] * d["count"] for d in b.dips.values())
    assert dip_cost <= 1500                 # <= 15% of NAV
    assert len(b.dips) < 6                  # the cap refused the rest
