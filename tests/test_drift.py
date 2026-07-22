"""Momentum drift book (drift1): trigger, caps, sizing, settle, salvage exit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drift_paper as dp
import weather_paper as wp


import datetime as _dt

TODAY = _dt.date.today().isoformat()
TOMORROW = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()


def _mk(tk="KXHIGHNY-26JUL21-T86", bid=66, ask=70, city="new york",
        is_low=False, strike=87, kind="ge", cap=None, date=None, vol=100.0):
    return {"ticker": tk, "city": city, "is_low": is_low, "strike": strike,
            "kind": kind, "cap": cap, "yes_bid": bid, "yes_ask": ask,
            "date": date or TODAY, "hrs": 10.0, "title": "", "sub": "",
            "bid_size": 50.0, "ask_size": 50.0, "vol": vol}


def _bot(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dp, "BETS", str(tmp_path / "b.csv"))
    b = dp.DriftPaper.__new__(dp.DriftPaper)
    b.start, b.cash = 10000, 10000.0
    b.bets, b.history, b.last_mid, b.last_vol = {}, [], {}, {}
    b.wins = b.losses = b.placed = 0
    b.fees = 0.0
    return b


def test_needs_two_scans_and_a_climb(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # first sight: high but no momentum memory -> no bet, memory recorded
    assert b.place(mkts=[_mk(bid=66, ask=70)]) == 0
    assert b.last_mid and not b.bets
    # second scan: climbed 3c on rising volume -> bet YES at the bid (maker)
    assert b.place(mkts=[_mk(bid=69, ask=73, vol=160.0)]) == 1
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
    assert b.place(mkts=[_mk(bid=24, ask=28, vol=160.0)]) == 1   # no-mid 74, +4
    bet = next(iter(b.bets.values()))
    assert bet["side"] == "no" and bet["entry"] == 100 - 28


def test_probe_stakes_and_event_cap(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    m1 = _mk(tk="A-T86", strike=87)
    m2 = _mk(tk="A-T88", strike=89)                     # same city-day event
    b.place(mkts=[m1, m2])
    up1 = dict(m1, yes_bid=69, yes_ask=73, vol=160.0)
    up2 = dict(m2, yes_bid=69, yes_ask=73, vol=160.0)
    assert b.place(mkts=[up1, up2]) == 1                # 1 bet per event only
    bet = next(iter(b.bets.values()))
    # 7/21 jugular sizing: probe $1.50 -> 2 contracts at 69c
    assert bet["entry"] * bet["count"] <= dp.PROBE_COST_CENTS


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
    # 35c bid > SALVAGE_C 30 and model agrees it's weak -> normal path only
    # (needs 2 confirms), so after ONE check the position is still open
    monkeypatch.setattr(wp.WeatherPaper, "_reprice",
                        lambda self, *a, **k: (0.10, 0.35))
    w._quote = lambda tk: (35, 39)
    w.exit_check()
    assert len(w.bets) == 1


# ---- 80c level entries (7/21: certainty is underpriced, 18/18 evidence) ----

def test_level_entry_needs_no_climb(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # first sight at 84c mid: no momentum memory, but level >= 80 -> bet
    assert b.place(mkts=[_mk(bid=82, ask=86)]) == 1
    bet = next(iter(b.bets.values()))
    assert bet["trig"] == "level" and bet["side"] == "yes" and bet["entry"] == 82


def test_65_80_band_still_requires_climb(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # 72c mid, first sight -> below level bar, no climb memory -> no bet
    assert b.place(mkts=[_mk(bid=70, ask=74)]) == 0
    # climbs 3c on volume -> now the experiment fires, tagged as climb
    assert b.place(mkts=[_mk(bid=73, ask=77, vol=160.0)]) == 1
    assert next(iter(b.bets.values()))["trig"] == "climb"


def test_level_entry_respects_max_entry(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # 94c mid -> entry 92 > DRIFT_MAX_ENTRY 90 -> skip (nothing left to win)
    assert b.place(mkts=[_mk(bid=92, ask=96)]) == 0


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



# ---- momentum-trader upgrades (7/21: vol confirm, same-day, rank, pyramid, fade A/B) ----

def test_climb_needs_rising_volume(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk()])
    # 3c climb but volume flat -> stale quote, not momentum -> no bet
    assert b.place(mkts=[_mk(bid=69, ask=73, vol=100.0)]) == 0
    assert not b.bets


def test_climb_needs_same_day_market(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk(date=TOMORROW)])
    # tomorrow's market climbing on volume -> still no (info arrives settle-day)
    assert b.place(mkts=[_mk(bid=69, ask=73, vol=160.0, date=TOMORROW)]) == 0
    # but a LEVEL entry in tomorrow's market is fine (static edge, not momentum)
    assert b.place(mkts=[_mk(tk="T-lvl", bid=82, ask=86, date=TOMORROW,
                             city="boston")]) == 1
    assert next(iter(b.bets.values()))["trig"] == "level"


def test_candidates_ranked_by_strength(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "DRIFT_MAX_PER_DAY", 1)
    small = _mk(tk="T-small", city="boston")
    big = _mk(tk="T-big", city="denver")
    b.place(mkts=[small, big])
    # both climb on volume, but 'big' climbs harder -> it gets the only slot
    assert b.place(mkts=[dict(small, yes_bid=69, yes_ask=73, vol=160.0),
                         dict(big, yes_bid=72, yes_ask=76, vol=160.0)]) == 1
    assert "T-big" in b.bets and "T-small" not in b.bets


def test_pyramid_active_in_probe_and_capped(tmp_path, monkeypatch):
    # Adam 7/21: pyramiding runs during probe too (paper trading)
    b = _bot(tmp_path, monkeypatch)
    b.bets = {"TK1": {"side": "yes", "entry": 70, "count": 1, "fee": 1,
                      "pside": 0.72, "city": "boston", "strike": 80,
                      "kind": "ge", "cap": None, "hl": "hi", "date": TODAY,
                      "ots": TODAY + "T10:00:00", "era": "drift1", "adds": 0}}
    ran = _mk(tk="TK1", bid=83, ask=87)      # +13c past entry
    b.place(mkts=[ran])                       # probe -> pyramid fires anyway
    bet = b.bets["TK1"]
    assert bet["adds"] == 1 and bet["count"] == 2
    assert 70 < bet["entry"] <= 83            # weighted-average entry
    b.place(mkts=[ran]); b.place(mkts=[ran])
    assert b.bets["TK1"]["adds"] <= dp.PYRAMID_MAX


def test_pyramid_knob_can_relock_behind_gate(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "PYRAMID_PROBE", False)
    b.bets = {"TK1": {"side": "yes", "entry": 70, "count": 1, "fee": 1,
                      "pside": 0.72, "city": "boston", "strike": 80,
                      "kind": "ge", "cap": None, "hl": "hi", "date": TODAY,
                      "ots": TODAY + "T10:00:00", "era": "drift1", "adds": 0}}
    b.place(mkts=[_mk(tk="TK1", bid=83, ask=87)])
    assert b.bets["TK1"]["adds"] == 0         # probe + knob off -> no adds


def test_trailing_exit_applies_to_every_bet(tmp_path, monkeypatch):
    # Adam 7/22: trailing exit on ALL trades (no more A/B split)
    b = _bot(tmp_path, monkeypatch)
    base = {"entry": 70, "count": 1, "fee": 1, "pside": 0.72, "city": "boston",
            "strike": 80, "kind": "ge", "cap": None, "hl": "hi", "date": TODAY,
            "ots": TODAY + "T10:00:00", "era": "drift1"}
    b.bets = {"T1": dict(base, side="yes", peak=85.0),
              "T2": dict(base, side="yes", peak=75.0)}
    # side mid 68: T1 is 17c off its 85 peak -> trail-exit; T2 only 7c off -> hold
    q = {"T1": (66, 70), "T2": (66, 70)}
    assert b.stop_check(quotes=q) == 1
    assert "T1" not in b.bets and "T2" in b.bets
    h = b.history[-1]
    assert h["faded"] is True and h["stopped"] is False and h["outcome"] is None
    # exited near the peak run-up: this one actually LOCKED a small gain
    assert h["pnl"] < 0.05                      # entry 70, exit 66, fees


# ---- weather-book pyramiding (Adam 7/21: accentuate winners) ----

def _wedge(tk="KXHIGHDEN-26JUL21-T95", entry_price=50, side="YES", fair=0.75):
    mk = {"ticker": tk, "city": "denver", "is_low": False, "strike": 95,
          "kind": "ge", "cap": None, "yes_bid": entry_price,
          "yes_ask": entry_price + 4, "date": TODAY, "hrs": 8.0,
          "entry_price": entry_price, "maker": True, "src": "nowcast",
          "w": 0.35, "vol": 100.0}
    return (5.0, side, mk, fair, 90.0)


def _wpbot():
    w = wp.WeatherPaper.__new__(wp.WeatherPaper)
    w.cash = 10000.0
    w.fees = 0.0
    w.history = []
    w.bets = {"KXHIGHDEN-26JUL21-T95": {
        "side": "yes", "entry": 40, "count": 1, "fee": 1, "pside": 0.55,
        "city": "denver", "strike": 95, "kind": "ge", "cap": None, "hl": "hi",
        "date": TODAY, "ots": TODAY + "T10:00:00", "era": "v7-obs", "adds": 0}}
    return w


def test_weather_pyramid_adds_to_model_confirmed_runner():
    w = _wpbot()
    # price ran 40 -> 55 (+15 > +10) and the scan re-confirms the edge
    assert w._maybe_pyramid("KXHIGHDEN-26JUL21-T95", "YES",
                            _wedge(entry_price=55)[2], 0.75) is True
    b = w.bets["KXHIGHDEN-26JUL21-T95"]
    assert b["adds"] == 1 and b["count"] == 2
    assert 40 < b["entry"] <= 55              # weighted-average entry


def test_weather_pyramid_skips_small_run_and_flipped_side():
    w = _wpbot()
    # only +5 past entry -> not a runner
    assert w._maybe_pyramid("KXHIGHDEN-26JUL21-T95", "YES",
                            _wedge(entry_price=45)[2], 0.75) is False
    # model flipped to NO -> never add
    assert w._maybe_pyramid("KXHIGHDEN-26JUL21-T95", "NO",
                            _wedge(entry_price=55)[2], 0.38) is False
    assert w.bets["KXHIGHDEN-26JUL21-T95"]["count"] == 1


def test_weather_pyramid_respects_add_cap():
    w = _wpbot()
    w.bets["KXHIGHDEN-26JUL21-T95"]["adds"] = wp.WX_PYRAMID_MAX
    assert w._maybe_pyramid("KXHIGHDEN-26JUL21-T95", "YES",
                            _wedge(entry_price=55)[2], 0.75) is False


def test_live_dry_merges_pyramid_fill(tmp_path, monkeypatch):
    import weather_live as wl
    monkeypatch.setattr(wl, "STATE", str(tmp_path / "ls.json"))
    monkeypatch.setattr(wl, "BETS", str(tmp_path / "lb.csv"))
    b = wl.WeatherLive.__new__(wl.WeatherLive)
    b.client, b.mode = None, "DRY"
    b.max_bet_c, b.max_open_c, b.max_day_loss_c, b.reserve_c = 200, 1500, 300, 200
    b.bets = {"TK1": {"side": "yes", "entry": 40, "count": 1, "fee": 1,
                      "pside": 0.55, "city": "denver", "strike": 95,
                      "kind": "ge", "cap": None, "hl": "hi", "date": TODAY,
                      "ots": TODAY + "T10:00:00", "era": "live1", "adds": 0}}
    b.pending, b.cooldown, b.history = {}, {}, []
    b.realized_c = b.fees_c = b.day_pnl_c = 0.0
    b.wins = b.losses = b.placed = b.canceled = 0
    b.day, b.halted, b.dry_balance_c = wl.today(), False, 10000
    mk = _wedge(tk="TK1", entry_price=55)[2]
    assert b._maybe_pyramid_order("TK1", "YES", mk, 0.75, 10000) is True
    assert len(b.pending) == 1
    # DRY fill happens in place(); simulate the merge path directly
    oid, o = next(iter(b.pending.items()))
    b._merge_fill("TK1", o["entry"], o["count"], 0)
    bet = b.bets["TK1"]
    assert bet["adds"] == 1 and bet["count"] == 2 and 40 < bet["entry"] <= 55


# ---- weather band discipline (Adam 7/22: bands rode to zero, model vetoed exits) ----

def _bandbot():
    w = wp.WeatherPaper.__new__(wp.WeatherPaper)
    w.cash = 10000.0
    w.realized = 0.0
    w.fees = 0.0
    w.cooldown = {}
    w.history = []
    w.bets = {"KXHIGHNY-26JUL22-B79.5": {
        "side": "yes", "entry": 41, "count": 4, "fee": 3, "pside": 0.57,
        "city": "new york", "strike": 79, "kind": "band", "cap": 80, "hl": "hi",
        "date": TODAY, "ots": TODAY + "T10:00:00", "era": "v7-obs"}}
    return w


def test_band_hard_stop_ignores_model_belief(monkeypatch):
    w = _bandbot()
    # model STILL believes (52%) - exactly the failure mode from 7/22 - but the
    # band fell 41 -> mid 25 (>12c below entry): sell anyway, no veto
    monkeypatch.setattr(wp.WeatherPaper, "_reprice",
                        lambda self, *a, **k: (0.52, 0.35))
    w._quote = lambda tk: (23, 27)
    w.exit_check()
    assert len(w.bets) == 0
    h = w.history[-1]
    assert h["exited"] is True and h["outcome"] is None


def test_band_holds_above_stop_line(monkeypatch):
    w = _bandbot()
    # mid 32 = only 9c below entry 41 -> no hard stop; model believes -> hold
    monkeypatch.setattr(wp.WeatherPaper, "_reprice",
                        lambda self, *a, **k: (0.55, 0.35))
    w._quote = lambda tk: (30, 34)
    w.exit_check()
    w.exit_check()
    assert len(w.bets) == 1


def test_threshold_bets_keep_model_veto(monkeypatch):
    w = _bandbot()
    w.bets["KXHIGHNY-26JUL22-B79.5"]["kind"] = "ge"   # same drop, ge kind
    monkeypatch.setattr(wp.WeatherPaper, "_reprice",
                        lambda self, *a, **k: (0.52, 0.35))
    w._quote = lambda tk: (23, 27)
    w.exit_check()
    w.exit_check()
    assert len(w.bets) == 1      # model still believes -> ge bets may hold


# ---- nickel experiment (Adam 7/22: 10 contracts at >=95c, hold to settle) ----

def test_nickel_places_ten_contracts(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # first sight, no climb needed: mid 96 -> nickel entry at the 95c bid
    assert b.place(mkts=[_mk(bid=95, ask=97)]) == 1
    bet = next(iter(b.bets.values()))
    assert bet["trig"] == "nickel" and bet["count"] == dp.NICKEL_COUNT
    assert bet["entry"] == 95


def test_nickel_needs_upside_left(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    # 99c bid: nothing left to win after fees -> skip
    assert b.place(mkts=[_mk(bid=99, ask=100)]) == 0
    # 98c entry: breakeven 98% - rejected since 7/21 (max entry 96c)
    assert b.place(mkts=[_mk(tk="T98", bid=98, ask=99, city="boston")]) == 0


def test_nickel_prefers_cheapest_entry(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "NICKEL_MAX_OPEN", 1)
    rich = _mk(tk="T-rich", bid=96, ask=98, city="boston")   # entry 96, win 4c
    cheap = _mk(tk="T-cheap", bid=94, ask=96, city="denver") # entry 94, win 6c
    assert b.place(mkts=[rich, cheap]) == 1
    assert "T-cheap" in b.bets and "T-rich" not in b.bets


def test_nickel_concurrent_cap(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets = {f"N{i}": {"trig": "nickel", "entry": 95, "count": 10, "fee": 1,
                        "side": "yes", "pside": 0.96, "city": f"c{i}",
                        "strike": 80, "kind": "ge", "cap": None, "hl": "hi",
                        "date": TODAY, "ots": TODAY + "T10:00:00",
                        "era": "drift1"} for i in range(dp.NICKEL_MAX_OPEN)}
    assert b.place(mkts=[_mk(tk="N-new", bid=95, ask=97, city="boston")]) == 0


def test_nickel_excluded_from_drift_gate(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.history = [{"trig": "nickel", "outcome": 1, "pnl": 0.4, "pside": 0.96}] * 40
    mode, n = b._gate()
    assert n == 0 and mode == "probe"          # nickels never drive the gate
    st = b._nickel_stats()
    assert st["n"] == 40 and st["wins"] == 40 and st["net"] == 16.0



# ---- Soros #1 (7/21): weather book is thresholds-only now ----

def test_weather_bands_retired(monkeypatch):
    w = _wbot()
    w.bets = {}
    band_mk = {"ticker": "B1", "city": "denver", "is_low": False, "strike": 95,
               "kind": "band", "cap": 96, "yes_bid": 40, "yes_ask": 44,
               "date": TODAY, "hrs": 8.0, "entry_price": 40, "maker": True,
               "src": "nowcast", "w": 0.35, "vol": 100.0}
    monkeypatch.setattr(wp.we, "scan", lambda **kw: [(5.0, "YES", band_mk, 0.85, 90.0)])
    w.placed = 0
    w.place()
    assert len(w.bets) == 0            # band candidate skipped even at 85% conf


# ---- nickel scale-on-proof (Adam 7/22: press the nickel winner) ----

def test_nickel_size_steps_up_on_proof(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    assert b._nickel_count() == dp.NICKEL_COUNT          # base 10
    # 3 grandfathered 98c wins do NOT scale it (entry > 96c cap)
    b.history = [{"trig": "nickel", "outcome": 1, "pnl": 0.19, "entry": 98}] * 10
    assert b._nickel_count() == dp.NICKEL_COUNT
    # 10 settled <=96c nickels, net positive -> 15ct
    b.history = [{"trig": "nickel", "outcome": 1, "pnl": 0.5, "entry": 94}] * 10
    assert b._nickel_count() == dp.NICKEL_STEP1_CT
    # 20 -> 20ct
    b.history *= 2
    assert b._nickel_count() == dp.NICKEL_STEP2_CT
    # net NEGATIVE at any n -> back to base (proof revoked)
    b.history = ([{"trig": "nickel", "outcome": 1, "pnl": 0.4, "entry": 94}] * 15
                 + [{"trig": "nickel", "outcome": 0, "pnl": -9.4, "entry": 94}])
    assert b._nickel_count() == dp.NICKEL_COUNT


def test_nickel_lanes_widened_to_five(tmp_path, monkeypatch):
    assert dp.NICKEL_MAX_OPEN == 5
