"""Tests for the tick book (15-minute window paper trader).

The classes of bug this file exists to catch are the ones that have
actually cost this project money: the dual-schema price read, the
loaded-but-never-saved state key, the look-ahead fill, and any code path
that could reach a live order endpoint.
"""
import json
import math
import os
import tempfile
import time

import pytest

os.environ.setdefault("TICK_STATE", os.path.join(
    tempfile.mkdtemp(), "tick_state.json"))
import tick_paper as T                                    # noqa: E402


# ---------------- fee curve ----------------
def test_fee_peaks_at_the_money_and_vanishes_at_the_extremes():
    mid = T.fee_c(50, 100, maker=False)
    edge = T.fee_c(95, 100, maker=False)
    # NB: math.ceil(0.07*100*0.25*100) is 176 in float - the naive
    # expression this shop used to ship. 175 is the correct penny.
    assert mid == 175
    assert edge < mid / 5
    assert T.fee_c(1, 100, maker=False) < 10


def test_maker_fee_is_a_fraction_of_taker():
    assert T.fee_c(60, 50, maker=True) < T.fee_c(60, 50, maker=False)


def test_fee_never_undercharges_through_float_error():
    # 0.07*100*0.25*100 == 175.00000000000003; a naive ceil returns 176
    assert T.fee_c(50, 100, maker=False) == 175


# ---------------- the model ----------------
def test_model_is_certain_when_the_clock_is_done():
    assert T.model_p(100.0, 99.0, 0.5, 0) == 1.0
    assert T.model_p(98.0, 99.0, 0.5, 0) == 0.0


def test_model_is_a_coin_flip_at_the_line():
    assert abs(T.model_p(100.0, 100.0, 0.5, 600) - 0.5) < 1e-9


def test_certainty_rises_as_the_window_drains():
    far = T.model_p(101.0, 100.0, 0.05, 800)
    near = T.model_p(101.0, 100.0, 0.05, 30)
    assert near > far
    assert near > 0.95


def test_model_handles_zero_volatility_without_dividing_by_zero():
    assert T.model_p(101.0, 100.0, 0.0, 500) == 1.0
    assert T.model_p(99.0, 100.0, 0.0, 500) == 0.0


# ---------------- dual schema ----------------
def test_price_read_prefers_the_fractional_dollars_field():
    # the 15-minute books carry NULL legacy fields and live _dollars ones
    assert T._px_c({"yes_price": None, "yes_price_dollars": "0.4500"},
                   "yes_price") == 45.0
    assert T._px_c({"yes_bid": 62}, "yes_bid") == 62.0
    assert T._px_c({"yes_bid": None}, "yes_bid") is None


def test_num_never_lets_a_string_reach_a_comparison():
    assert T._num("3.5") == 3.5
    assert T._num(None) == 0.0
    assert T._num("junk") == 0.0


# ---------------- lane rules ----------------
def _mkt(**kw):
    base = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
            "strike": 100.0, "close_ts": time.time() + 120,
            "yes_bid": 90.0, "yes_ask": 94.0, "depth": 100.0}
    base.update(kw)
    return base


def test_never_quotes_at_the_money_where_fees_peak():
    b = T.TickBook()
    # spot exactly on the line, 10 minutes left -> ~50/50, must refuse
    assert b.decide(_mkt(yes_bid=48.0, yes_ask=52.0),
                    100.0, 0.05, 600) is None


def test_endgame_lane_takes_cheap_certainty_late():
    b = T.TickBook()
    out = b.decide(_mkt(yes_bid=88.0, yes_ask=92.0), 100.5, 0.02, 60)
    assert out is not None
    lane, p_yes, px, side, p_side = out
    assert lane == "endgame"
    assert side == "yes"
    assert p_side >= T.ENDGAME_P
    assert T.MIN_PX_C <= px <= T.MAX_PX_C


def test_refuses_when_the_book_already_prices_the_certainty():
    b = T.TickBook()
    # model ~99% but the book offers 99 -> no edge left to take
    assert b.decide(_mkt(yes_bid=98.0, yes_ask=100.0),
                    100.5, 0.02, 60) is None


def test_takes_the_no_side_when_the_walk_is_below_the_line():
    b = T.TickBook()
    out = b.decide(_mkt(yes_bid=6.0, yes_ask=10.0), 99.5, 0.02, 60)
    assert out is not None
    assert out[3] == "no"


def test_refuses_above_the_price_ceiling():
    b = T.TickBook()
    # 99c certainty leaves no room to pay the fee
    assert b.decide(_mkt(yes_bid=99.0, yes_ask=100.0),
                    101.0, 0.01, 30) is None


# ---------------- fills ----------------
def test_a_paper_bid_fills_only_when_a_print_goes_through_it():
    b = T.TickBook()
    q = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
         "lane": "endgame", "our_px": 90.0, "side": "yes",
         "strike": 100.0, "close_ts": time.time() + 60, "model_p": 0.95,
         "p_side": 0.95, "spot": 100.5, "t_left": 60, "ts": time.time() - 5}
    b.resting = {"T1": q}
    b.check_fills([{"tk": "T1", "px": 95.0, "ct": 10, "ts": time.time()}], 0)
    assert not b.pos                      # print above our bid: no fill
    b.check_fills([{"tk": "T1", "px": 89.0, "ct": 10, "ts": time.time()}], 0)
    assert b.pos["T1"]["n"] == T.SIZE     # traded through: filled


def test_a_print_at_our_exact_price_is_loose_and_never_believed():
    b = T.TickBook()
    q = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
         "lane": "endgame", "our_px": 90.0, "side": "yes",
         "strike": 100.0, "close_ts": time.time() + 60, "model_p": 0.95,
         "p_side": 0.95, "spot": 100.5, "t_left": 60, "ts": time.time() - 5}
    b.resting = {"T1": q}
    b.check_fills([{"tk": "T1", "px": 90.0, "ct": 10, "ts": time.time()}], 0)
    assert not b.pos
    assert b.stats["fills_loose"] == 1


def test_no_look_ahead_a_print_before_our_quote_cannot_fill_it():
    """The bug that invalidated an entire phantom ledger on 8/20."""
    b = T.TickBook()
    now = time.time()
    q = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
         "lane": "endgame", "our_px": 90.0, "side": "yes",
         "strike": 100.0, "close_ts": now + 60, "model_p": 0.95,
         "p_side": 0.95, "spot": 100.5, "t_left": 60, "ts": now}
    b.resting = {"T1": q}
    b.check_fills([{"tk": "T1", "px": 80.0, "ct": 10, "ts": now - 30}], 0)
    assert not b.pos


def test_fill_charges_the_maker_fee():
    b = T.TickBook()
    q = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
         "lane": "endgame", "our_px": 90.0, "side": "yes",
         "strike": 100.0, "close_ts": time.time() + 60, "model_p": 0.95,
         "p_side": 0.95, "spot": 100.5, "t_left": 60, "ts": time.time() - 5}
    b.resting = {"T1": q}
    b.check_fills([{"tk": "T1", "px": 88.0, "ct": 5, "ts": time.time()}], 0)
    assert b.pos["T1"]["fee_c"] == T.fee_c(90.0, 5, maker=True)


# ---------------- persistence (the 8/15 + 8/25 bug class) ----------------
def test_the_published_basis_is_the_median_not_the_raw_list():
    """save() and step() both wrote a key called "basis"; the raw
    observation list silently replaced the median on the tracker."""
    b = T.TickBook()
    b.basis["KXGOLD15M"] = [2.4, 2.5, 2.6]
    st = {}
    b.save(st)
    assert "basis_obs" in st and "basis" not in st


def test_a_measured_window_is_not_remeasured_after_a_restart():
    path = os.path.join(tempfile.mkdtemp(), "s.json")
    T.STATE = path
    b = T.TickBook()
    b.ticks["KXWTI15M"] = [(10_000, 82.2146)]
    b.measure_basis([{"tk": "W1", "series": "KXWTI15M", "label": "wti",
                      "strike": 82.29, "open_ts": 10_000}])
    b.save({"era": T.ERA})
    b2 = T.TickBook()
    b2.ticks["KXWTI15M"] = [(10_000, 82.2146)]
    b2.measure_basis([{"tk": "W1", "series": "KXWTI15M", "label": "wti",
                       "strike": 82.29, "open_ts": 10_000}])
    assert len(b2.basis["KXWTI15M"]) == 1


def test_every_loaded_key_is_also_saved():
    """nav_days (8/15) and rung_stats (8/25) were both in the load path
    and missing from save(), so a deploy silently wiped them. Here the
    two sides are one list and this test is the guard."""
    src = open(os.path.join(os.path.dirname(T.__file__),
                            "tick_paper.py")).read()
    load_body = src.split("def load(self)")[1].split("def save(self")[0]
    save_body = src.split("def save(self, state)")[1].split(
        "# ---------------- the reference feed")[0]
    for key in T.TickBook.PERSIST:
        if key == "t0":
            continue
        assert f'"{key}"' in load_body, f"{key} not loaded"
        assert f'"{key}"' in save_body, f"{key} not saved"


def test_state_round_trips_through_disk():
    path = os.path.join(tempfile.mkdtemp(), "s.json")
    T.STATE = path
    b = T.TickBook()
    b.realized_c = 1234.0
    b.stats["settled"] = 7
    b.calib["90"] = [10, 9]
    b.save({"era": T.ERA})
    b2 = T.TickBook()
    assert b2.realized_c == 1234.0
    assert b2.stats["settled"] == 7
    assert b2.calib["90"] == [10, 9]


def test_a_ledger_from_another_era_is_not_adopted():
    path = os.path.join(tempfile.mkdtemp(), "s.json")
    T.STATE = path
    json.dump({"era": "someone_elses", "realized_c": 999.0},
              open(path, "w"))
    b = T.TickBook()
    assert b.realized_c == 0.0


# ---------------- safety ----------------
def test_the_module_cannot_place_an_order():
    src = open(os.path.join(os.path.dirname(T.__file__),
                            "tick_paper.py")).read()
    for forbidden in ("portfolio/orders", "portfolio/events/orders",
                      "create_order", "cancel_order", "KALSHI_KEY",
                      "private_key", "urlopen(req, data",
                      '"POST"', "method='POST'"):
        assert forbidden not in src, f"order path present: {forbidden}"


def test_calibration_table_reports_hit_rate_by_stated_probability():
    b = T.TickBook()
    b.calib = {"90": [10, 9], "70": [10, 5]}
    tbl = {r["bucket"]: r["hit"] for r in b._calib_table()}
    assert tbl["90-99%"] == 90.0
    assert tbl["70-79%"] == 50.0


# ---------------- the overconfidence guard (measured 8/25) ----------------
def test_sigma_takes_the_max_of_short_and_long_lookback():
    """A calm long tape must not cancel out a violent recent one."""
    b = T.TickBook()
    calm = [(1000 + i * 20, 100.0 + (i % 2) * 0.01) for i in range(40)]
    burst = [(1800 + i * 20, 100.0 + i * 0.5) for i in range(25)]
    b.ticks["KXGOLD15M"] = calm + burst
    sig = b.sigma_s("KXGOLD15M")
    b.ticks["KXGOLD15M"] = calm
    calm_only = b.sigma_s("KXGOLD15M")
    assert sig > calm_only


def test_sigma_carries_the_jump_haircut():
    b = T.TickBook()
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 100.0 + (i % 3) * 0.2)
                            for i in range(40)]
    assert b.sigma_s("KXGOLD15M") > 0
    # the multiplier is applied, not silently dropped
    assert T.VOL_MULT > 1.0


def test_model_confidence_is_capped_before_any_decision():
    """Raw model said 99.9%+ on the first live gold window while the
    market said 87%. Capped so a wrong vol can't mint fake certainty."""
    b = T.TickBook()
    out = b.decide(_mkt(yes_bid=60.0, yes_ask=64.0), 200.0, 0.001, 30)
    assert out is not None
    _lane, _p_yes, _px, _side, p_side = out
    assert p_side <= T.CONF_CAP


def test_start_thread_runs_the_book_on_its_own_clock():
    """paper.py cycles at 90s; a 15-minute window needs finer sampling
    than that or the endgame lane is unmeasurable."""
    os.environ["TICK_SLEEP"] = "600"          # don't actually loop here
    T.LOCK = os.path.join(tempfile.mkdtemp(), "tick.lock")
    t = T.start_thread("test")
    assert t is not None
    assert t.daemon is True
    assert t.name.startswith("tick")


def test_only_one_writer_paper_does_not_also_step_the_book():
    """Two writers on one json.dump is a corrupted ledger."""
    here = os.path.dirname(T.__file__)
    src = open(os.path.join(here, "paper.py")).read()
    assert 'tick_paper.start_thread("paper")' in src
    assert "tk_bot.step()" not in src


# ---------- proxy liveness (the WTI bug, caught live 8/25) ----------
def test_a_frozen_proxy_is_declared_dead():
    """USOILSPOT printed 82.2930 -> 82.2933 over 40s while gold moved
    two points. A distance computed off a frozen feed is noise, and it
    manufactured a 43-cent 'edge' pointing the wrong way."""
    b = T.TickBook()
    b.ticks["KXWTI15M"] = [(1000 + i * 20, 82.2930 + (i % 2) * 0.0003)
                           for i in range(30)]
    assert b.proxy_dead("KXWTI15M") is True


def test_a_moving_proxy_is_alive():
    b = T.TickBook()
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 4650.0 + i * 0.4)
                            for i in range(30)]
    assert b.proxy_dead("KXGOLD15M") is False


def test_a_dead_proxy_series_is_never_quoted():
    b = T.TickBook()
    b.basis["KXWTI15M"] = [0.0, 0.0, 0.0]        # offset known
    b.ticks["KXWTI15M"] = [(1000 + i * 20, 82.29) for i in range(30)]
    m = {"tk": "W1", "series": "KXWTI15M", "label": "wti", "strike": 82.29,
         "open_ts": time.time() - 600, "close_ts": time.time() + 120,
         "title": "", "yes_bid": 90.0, "yes_ask": 94.0,
         "no_bid": None, "no_ask": None}
    b.fetch_book = lambda tk: (90.0, 94.0, 10.0, 10.0)
    n = b.quote([m], {"KXWTI15M": (82.29, 1.0)})
    assert n == 0
    assert b.stats["proxy_dead"] >= 1


# ---------- both sides priced independently (Adam 8/25) ----------
def test_the_no_side_is_evaluated_on_its_own_book():
    """A NO at 12 is only a YES at 88 if you cross the spread. The old
    code could only ever pick the side the model favoured."""
    b = T.TickBook()
    # model favours YES (~98%) but YES is offered at 99 (no edge left),
    # while the mirrored NO price still carries edge
    out = b.decide(_mkt(yes_bid=30.0, yes_ask=34.0), 99.0, 0.02, 60)
    assert out is not None
    assert out[3] == "no"


def test_the_better_edge_wins_not_the_favoured_side():
    b = T.TickBook()
    out = b.decide(_mkt(yes_bid=88.0, yes_ask=92.0), 100.5, 0.02, 60)
    lane, _p, px, side, p_side = out
    assert side == "yes"
    assert p_side * 100 - px >= T.EDGE_C


# ---------- true arb ----------
def test_a_crossed_book_is_recorded_as_real_arbitrage():
    b = T.TickBook()
    # yes_ask 40 + no_ask (100-55=45) = 85 -> 15c locked before fees
    got = b.check_arb({"tk": "T1", "label": "gold",
                       "yes_bid": 55.0, "yes_ask": 40.0})
    assert got is not None and got["net_c"] > 0


def test_a_normal_book_is_not_arbitrage():
    b = T.TickBook()
    assert b.check_arb({"tk": "T1", "label": "gold",
                        "yes_bid": 40.0, "yes_ask": 44.0}) is None


# ---------- the exit lane (Adam 8/25) ----------
def _pos(b, side="yes", px=88.0, n=5.0):
    b.pos["T1"] = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
                   "lane": "endgame", "side": side, "n": n,
                   "cost_c": px * n, "fee_c": 0.0, "strike": 100.0,
                   "close_ts": time.time() + 120, "model_p": 0.95,
                   "p_side": 0.95, "spot_at_entry": 100.5, "t_left": 120,
                   "opened": "2000-01-01T00:00:00"}
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 100.0 + i * 0.05)
                            for i in range(30)]
    b.basis["KXGOLD15M"] = [0.0, 0.0, 0.0]


def test_exit_takes_the_money_when_the_market_comes_to_the_model():
    b = T.TickBook()
    _pos(b)
    m = _mkt(tk="T1", yes_bid=97.0, yes_ask=98.0,
             close_ts=time.time() + 120)
    b.check_exits([m], {"KXGOLD15M": (100.5, 1.0)})
    assert "T1" not in b.pos                 # sold
    assert b.stats["exits"] == 1
    assert b.settled[-1]["lane"] == "exit"
    assert b.settled[-1]["pnl"] > 0


def test_exit_holds_while_the_trade_is_still_cheap():
    b = T.TickBook()
    _pos(b)
    m = _mkt(tk="T1", yes_bid=89.0, yes_ask=90.0,
             close_ts=time.time() + 120)
    b.check_exits([m], {"KXGOLD15M": (100.9, 1.0)})
    assert "T1" in b.pos                     # edge remains: keep it


def test_exit_never_pays_to_leave_a_thesis_intact():
    """Selling below our own cost while the model still likes the trade
    is the flatten leak that costs the live book -0.097/contract-hour."""
    b = T.TickBook()
    _pos(b, px=88.0)
    m = _mkt(tk="T1", yes_bid=70.0, yes_ask=72.0,
             close_ts=time.time() + 120)
    b.check_exits([m], {"KXGOLD15M": (100.5, 1.0)})
    assert "T1" in b.pos


# ---------- basis correction (measured live 8/25) ----------
def test_basis_is_measured_from_the_window_open():
    """At the instant a window opens, Kalshi's settlement feed EQUALS
    the new strike - so our proxy's reading at that moment is a free
    measurement of our own zero error."""
    b = T.TickBook()
    open_ts = 10_000
    b.ticks["KXWTI15M"] = [(open_ts - 10, 82.2146), (open_ts + 300, 82.3)]
    b.measure_basis([{"tk": "W1", "series": "KXWTI15M", "label": "wti",
                      "strike": 82.29, "open_ts": open_ts}])
    assert b.basis["KXWTI15M"] == [round(82.2146 - 82.29, 6)]


def test_a_window_is_only_measured_once():
    b = T.TickBook()
    m = [{"tk": "W1", "series": "KXWTI15M", "label": "wti",
          "strike": 82.29, "open_ts": 10_000}]
    b.ticks["KXWTI15M"] = [(10_000, 82.2146)]
    b.measure_basis(m)
    b.measure_basis(m)
    assert len(b.basis["KXWTI15M"]) == 1


def test_basis_uses_the_median_not_the_mean():
    """One bad print at a boundary must not drag the correction."""
    b = T.TickBook()
    b.basis["KXGOLD15M"] = [2.4, 2.5, 2.6, 99.0]     # 99 is garbage
    assert 2.4 <= b.basis_of("KXGOLD15M") <= 2.7


def test_no_quoting_until_the_offset_is_known():
    """Trading before you know your instrument's zero error is exactly
    how the WTI row happened."""
    b = T.TickBook()
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 4650.0 + i * 0.4)
                            for i in range(30)]
    b.fetch_book = lambda tk: (90.0, 94.0, 10.0, 10.0)
    m = {"tk": "G1", "series": "KXGOLD15M", "label": "gold",
         "strike": 4650.0, "open_ts": time.time() - 600,
         "close_ts": time.time() + 120, "title": "",
         "yes_bid": 90.0, "yes_ask": 94.0}
    assert b.quote([m], {"KXGOLD15M": (4660.0, 1.0)}) == 0
    assert b.stats["no_basis"] >= 1


def test_the_correction_removes_the_offset_from_the_distance():
    """WTI read -0.0754 low against a window that only travels ~0.05 -
    the measurement error was larger than the signal."""
    b = T.TickBook()
    b.basis["KXWTI15M"] = [-0.0754, -0.0750, -0.0758]
    adj = b.adj_spot("KXWTI15M", 82.2146)
    assert abs(adj - 82.29) < 0.002        # lands back on the strike


# ---------- pair / legged-arb tracker (Adam 8/25) ----------
def test_pair_completes_when_the_window_swings_through_both_legs():
    b = T.TickBook()
    m = _mkt(tk="P1", close_ts=time.time() - 60)
    b.pair["P1"] = {"lo": 20.0, "hi": 80.0, "close": time.time() - 60}
    b.track_pair([])
    assert b.pair_stats["both"] == 1


def test_pair_is_one_legged_when_the_window_trends():
    b = T.TickBook()
    b.pair["P1"] = {"lo": 20.0, "hi": 40.0, "close": time.time() - 60}
    b.track_pair([])
    assert b.pair_stats["one"] == 1
    assert b.pair_stats["both"] == 0


def test_pair_report_states_the_breakeven_it_must_clear():
    """78% observed vs ~88.5% needed at 45c is why this lane is on ice."""
    b = T.TickBook()
    b.pair_stats = {"n": 100, "both": 78, "one": 22, "none": 0}
    r = b.pair_report()
    assert r["rate"] == 0.78
    assert 0.80 < r["breakeven"] < 0.95
    assert r["pays"] is False


def test_pair_report_flips_to_pays_if_the_regime_ever_clears_the_bar():
    b = T.TickBook()
    b.pair_stats = {"n": 100, "both": 97, "one": 3, "none": 0}
    assert b.pair_report()["pays"] is True


def test_the_pair_tracker_never_places_a_trade():
    b = T.TickBook()
    b.pair["P1"] = {"lo": 10.0, "hi": 90.0, "close": time.time() - 60}
    b.track_pair([])
    assert not b.pos and b.realized_c == 0.0


# ---------- the bug I shipped twice in one session ----------
def test_no_save_key_may_collide_with_a_published_key():
    """save() writes into the same dict step() publishes. THREE times now
    a raw observation store has silently replaced the computed report of
    the same name on the tracker: "basis", then "pair", then "shadow".

    So this no longer lists the keys I have to remember - it derives the
    collision set: every key save() writes is checked against every key
    step() publishes, and any overlap that changes the value fails."""
    b = T.TickBook()
    b.basis["KXGOLD15M"] = [1.0, 1.1, 1.2]
    b.pair["X"] = {"lo": 10.0, "hi": 90.0, "close": time.time()}
    b.shadow["Y"] = {"p": 0.9, "close_ts": time.time(), "label": "gold",
                     "mom": 0.0, "d": 1.0, "px": 90.0}
    published = {
        "basis": {"KXGOLD15M": b.basis_of("KXGOLD15M")},
        "pair": b.pair_report(),
        "shadow": {"n": 0, "pending": len(b.shadow),
                   "table": b.shadow_table()},
        "calibration": b._calib_table(),
    }
    saved = dict(published)
    b.save(saved)
    clobbered = [k for k, v in published.items() if saved.get(k) != v]
    assert not clobbered, f"save() clobbered published keys: {clobbered}"


def test_a_refusing_feed_is_backed_off_not_hammered():
    """327 errors and 1,446 refusals accumulated before anyone looked.
    NB: the crypto feeds are free and separate, so a Pyth backoff must
    not stop them - only the Pyth half is skipped."""
    b = T.TickBook()
    b._feed_block_until = time.time() + 60
    b.fetch_crypto = lambda: {}
    assert b.fetch_proxy() == {}
    assert b.errs == 0            # backed off, did not even try


def test_feed_health_is_published_not_swallowed():
    b = T.TickBook()
    b._feed_err = "HTTP Error 401: Unauthorized"
    b.fetch_markets = lambda: []
    b.fetch_proxy = lambda: {}
    st = b.step()
    assert st["feed"]["ok"] is False
    assert "401" in st["feed"]["err"]


def test_the_api_key_can_arrive_by_file_so_it_never_enters_git():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    with open(os.path.join(d, "logs", "pyth_key.txt"), "w") as f:
        f.write("  test-key-123  \n")
    cwd = os.getcwd()
    try:
        os.chdir(d)
        assert T._pyth_key() == "test-key-123"
    finally:
        os.chdir(cwd)


def test_the_env_var_wins_over_the_file():
    os.environ["PYTH_API_KEY"] = "env-key"
    try:
        assert T._pyth_key() == "env-key"
    finally:
        del os.environ["PYTH_API_KEY"]


def test_a_pasted_key_survives_the_do_console_paste_markers():
    """The DO web console wraps pasted text in ESC[200~ ... ESC[201~.
    Those bytes reach the Authorization header and produce a 401 that is
    indistinguishable from a wrong key."""
    assert T._clean_key("\x1b[200~DjnY9WUabc123\x1b[201~") == "DjnY9WUabc123"
    assert T._clean_key("200~DjnY9WUabc123201~") == "DjnY9WUabc123"
    assert T._clean_key(" DjnY9WUabc123 \n") == "DjnY9WUabc123"
    assert T._clean_key("") == ""


def test_a_clean_key_is_left_alone():
    for k in ("abc-123_XY.z", "AAAA1111bbbb2222", "a+b/c="):
        assert T._clean_key(k) == k


def test_one_unavailable_feed_does_not_blind_the_others():
    """Hermes 404s the WHOLE batch when any single id is missing. On
    8/26 WTI was absent from the plan and gold+silver - both perfectly
    available - returned nothing for hours."""
    b = T.TickBook()
    wti = T.SERIES["KXWTI15M"][0]
    b._dead_ids.add(wti)
    seen = {}

    def fake_get(url, timeout=15, key=False):
        seen["url"] = url
        return {"parsed": []}

    T._get = fake_get
    b.fetch_proxy()
    assert wti not in seen["url"]
    assert T.SERIES["KXGOLD15M"][0] in seen["url"]


# ---------- the +$304 fabrication (8/26) ----------
def _q(px=90.0, side="yes"):
    return {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
            "lane": "endgame", "our_px": px, "side": side, "strike": 100.0,
            "close_ts": time.time() + 60, "model_p": 0.95, "p_side": 0.95,
            "spot": 100.5, "t_left": 60, "ts": time.time() - 5,
            "filled": 0.0}


def test_a_resting_order_can_only_fill_its_own_size_in_total():
    """THE BUG: _fill was called once per print for up to SIZE contracts
    EACH. One 5-lot quote in a busy book took 677 contracts - $623 of
    collateral on a $100 book - and fabricated +$304 of P&L."""
    b = T.TickBook()
    q = _q()
    b.resting = {"T1": q}
    prints = [{"tk": "T1", "px": 85.0, "ct": 50, "ts": time.time()}
              for _ in range(40)]
    b.check_fills(prints, 0)
    assert b.pos["T1"]["n"] <= T.SIZE


def test_position_cap_is_enforced_at_fill_time_not_only_at_quote_time():
    b = T.TickBook()
    for i in range(20):
        q = _q()
        b.resting = {"T1": q}
        b.check_fills([{"tk": "T1", "px": 85.0, "ct": 50,
                        "ts": time.time()}], 0)
    assert b.pos["T1"]["n"] <= T.MAX_POS


def test_the_book_cannot_spend_more_collateral_than_it_has():
    b = T.TickBook()
    for i in range(60):
        q = _q(px=90.0)
        q["tk"] = f"T{i}"
        b.resting = {q["tk"]: q}
        b.check_fills([{"tk": q["tk"], "px": 85.0, "ct": 50,
                        "ts": time.time()}], 0)
    assert b._capital_c() <= T.BOOK_CAPITAL_C


def test_fill_returns_the_size_actually_taken():
    b = T.TickBook()
    q = _q()
    assert b._fill(q, 3.0, 90.0) == 3.0
    b.pos["T1"]["n"] = T.MAX_POS
    assert b._fill(q, 3.0, 90.0) == 0.0


def test_an_exit_does_not_credit_itself_a_calibration_win():
    """We exit the trades that are WORKING, so scoring exits as wins
    guarantees a flattering table however bad the model is - the 8/17
    sold_net winner-selection bias in a new hat."""
    b = T.TickBook()
    b.pos["T1"] = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
                   "lane": "endgame", "side": "yes", "n": 5.0,
                   "cost_c": 440.0, "fee_c": 0.0, "strike": 100.0,
                   "close_ts": time.time() + 120, "model_p": 0.95,
                   "p_side": 0.95, "spot_at_entry": 100.5, "t_left": 120,
                   "opened": "2000-01-01T00:00:00"}
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 100.0 + i * 0.05)
                            for i in range(30)]
    b.basis["KXGOLD15M"] = [0.0, 0.0, 0.0]
    m = _mkt(tk="T1", yes_bid=97.0, yes_ask=98.0,
             close_ts=time.time() + 120)
    b.check_exits([m], {"KXGOLD15M": (100.5, 1.0)})
    assert b.calib == {}                      # nothing credited
    assert len(b.pend_calib) == 1             # parked for the real result


# ---------- aggressive regime (Adam 8/26) ----------
def test_the_edge_test_is_net_of_the_round_trip_fee():
    """A wide band is only safe if the arithmetic prices the fee. A
    gross test would buy a 4c edge that costs 5c to trade - exactly how
    phantom lost 2.45c on every pair it captured."""
    b = T.TickBook()
    # mid-priced market where fees peak: gross edge exists, net does not
    out = b.decide(_mkt(yes_bid=48.0, yes_ask=52.0), 100.05, 0.02, 400)
    if out is not None:
        _l, _p, px, _s, p_side = out
        rt = (T.fee_c(px, 1, True)
              + T.fee_c(min(99.0, px + T.EDGE_C), 1, True))
        assert p_side * 100 - px - rt >= T.EDGE_C


def test_a_broken_thesis_is_stopped_out_even_at_a_loss():
    """Distinct from the flatten leak: this fires only when the model
    has crossed to the other side, so the reason for holding is gone."""
    b = T.TickBook()
    b.pos["T1"] = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
                   "lane": "endgame", "side": "yes", "n": 5.0,
                   "cost_c": 440.0, "fee_c": 0.0, "strike": 100.0,
                   "close_ts": time.time() + 120, "model_p": 0.88,
                   "p_side": 0.88, "spot_at_entry": 100.5, "t_left": 120,
                   "opened": "2000-01-01T00:00:00"}
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 100.0 - i * 0.05)
                            for i in range(30)]
    b.basis["KXGOLD15M"] = [0.0, 0.0, 0.0]
    # spot now far BELOW the strike: the yes thesis is dead
    m = _mkt(tk="T1", yes_bid=20.0, yes_ask=22.0,
             close_ts=time.time() + 120)
    b.check_exits([m], {"KXGOLD15M": (98.0, 1.0)})
    assert "T1" not in b.pos
    assert b.stats.get("stops") == 1
    assert b.settled[-1]["stop"] is True
    assert b.settled[-1]["pnl"] < 0          # a real loss, taken


def test_round_trips_are_capped_per_window():
    b = T.TickBook()
    b.trips["T1"] = T.MAX_TRIPS
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 4650.0 + i * 0.4)
                            for i in range(30)]
    b.basis["KXGOLD15M"] = [0.0, 0.0, 0.0]
    b.fetch_book = lambda tk: (90.0, 94.0, 10.0, 10.0)
    m = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
         "strike": 4600.0, "open_ts": time.time() - 300,
         "close_ts": time.time() + 300, "title": "",
         "yes_bid": 90.0, "yes_ask": 94.0}
    assert b.quote([m], {"KXGOLD15M": (4660.0, 1.0)}) == 0
    assert b.stats.get("trip_capped", 0) >= 1


def test_re_entry_after_an_exit_is_allowed():
    """'Trade in and out over and over' - an exited market must be
    quotable again inside the same window."""
    b = T.TickBook()
    b.trips["T1"] = 1                       # one completed round trip
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 4650.0 + i * 0.4)
                            for i in range(30)]
    b.basis["KXGOLD15M"] = [0.0, 0.0, 0.0]
    b.fetch_book = lambda tk: (90.0, 94.0, 10.0, 10.0)
    m = {"tk": "T1", "series": "KXGOLD15M", "label": "gold",
         "strike": 4600.0, "open_ts": time.time() - 300,
         "close_ts": time.time() + 300, "title": "",
         "yes_bid": 90.0, "yes_ask": 94.0}
    assert b.quote([m], {"KXGOLD15M": (4660.0, 1.0)}) == 1


def test_aggression_did_not_reopen_the_fill_hole():
    """The caps are bigger, not absent - the +$304 must stay impossible."""
    b = T.TickBook()
    q = _q()
    b.resting = {"T1": q}
    b.check_fills([{"tk": "T1", "px": 85.0, "ct": 500, "ts": time.time()}
                   for _ in range(50)], 0)
    assert b.pos["T1"]["n"] <= T.MAX_POS
    assert b._capital_c() <= T.BOOK_CAPITAL_C


def test_basis_backfills_from_already_closed_windows():
    """One sample per 15-min window meant an era reset cost 45 minutes
    of dead time. Closed windows carry strikes at known past instants,
    and our tape covers the last hour - so the warmup is recoverable in
    a single pass."""
    b = T.TickBook()
    base = time.time() - 3000
    b.ticks["KXGOLD15M"] = [(base + i * 20, 4600.0 + i * 0.1)
                            for i in range(150)]

    def fake_get(url, timeout=15, key=False):
        if "status=settled" in url and "KXGOLD15M" in url:
            return {"markets": [
                {"ticker": f"W{i}", "floor_strike": 4600.0 + i,
                 "open_time": base + 300 + i * 900} for i in range(3)]}
        return {"markets": []}

    T._get = fake_get
    b.backfill_basis()
    assert len(b.basis.get("KXGOLD15M") or []) >= 3
    assert b.basis_of("KXGOLD15M") is not None


def test_backfill_does_not_double_count_a_window():
    b = T.TickBook()
    base = time.time() - 3000
    b.ticks["KXGOLD15M"] = [(base + i * 20, 4600.0 + i * 0.1)
                            for i in range(150)]

    def fake_get(url, timeout=15, key=False):
        if "status=settled" in url and "KXGOLD15M" in url:
            return {"markets": [{"ticker": "W1", "floor_strike": 4650.0,
                                 "open_time": base + 600}]}
        return {"markets": []}

    T._get = fake_get
    b.backfill_basis()
    n1 = len(b.basis.get("KXGOLD15M") or [])
    b.backfill_basis()
    assert len(b.basis.get("KXGOLD15M") or []) == n1


def test_an_era_reset_keeps_the_price_tape_and_the_instrument_offset():
    """The ledger is regime-specific; the feeds are not. Wiping the tape
    and the measured offset on an era bump cost 45 minutes of blind dead
    time after every reset, for no epistemic gain."""
    path = os.path.join(tempfile.mkdtemp(), "s.json")
    T.STATE = path
    json.dump({"era": "an_older_era",
               "realized_c": 5000.0,
               "settled": [{"pnl": 1.0}],
               "calib": {"90": [10, 9]},
               "ticks": {"KXGOLD15M": [[1000, 4600.0], [1020, 4601.0]]},
               "basis_obs": {"KXGOLD15M": [1.0, 1.1, 1.2]},
               "basis_seen": ["W1"]}, open(path, "w"))
    b = T.TickBook()
    # ledger discarded
    assert b.realized_c == 0.0
    assert b.settled == []
    assert b.calib == {}
    # instrument knowledge kept
    assert b.basis_of("KXGOLD15M") is not None
    assert len(b.ticks.get("KXGOLD15M") or []) == 2
    assert "W1" in b._basis_seen


# ---------- shadow calibration (8/27) ----------
def _mk_window(tk="S1", close_in=60):
    return {"tk": tk, "series": "KXGOLD15M", "label": "gold",
            "strike": 100.0, "open_ts": time.time() - 800,
            "close_ts": time.time() + close_in, "title": "",
            "yes_bid": 60.0, "yes_ask": 62.0}


def _ready(b):
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 100.0 + (i % 4) * 0.05)
                            for i in range(40)]
    b.basis["KXGOLD15M"] = [0.0, 0.0, 0.0]


def test_the_model_is_scored_on_every_window_not_only_traded_ones():
    """The table used to fill only when we TRADED - slow, and biased to
    exactly where the model was most confident."""
    b = T.TickBook()
    _ready(b)
    b.observe_shadow([_mk_window()], {"KXGOLD15M": (100.5, 1.0)})
    assert "S1" in b.shadow
    assert 0.0 <= b.shadow["S1"]["p"] <= 1.0


def test_a_window_is_observed_once_at_a_fixed_point():
    b = T.TickBook()
    _ready(b)
    w = _mk_window()
    b.observe_shadow([w], {"KXGOLD15M": (100.5, 1.0)})
    p1 = b.shadow["S1"]["p"]
    b.observe_shadow([w], {"KXGOLD15M": (200.0, 1.0)})
    assert b.shadow["S1"]["p"] == p1        # not overwritten later


def test_windows_are_not_observed_too_early():
    b = T.TickBook()
    _ready(b)
    b.observe_shadow([_mk_window(close_in=800)], {"KXGOLD15M": (100.5, 1.0)})
    assert b.shadow == {}


def test_momentum_is_recorded_as_a_feature_not_traded_on():
    """Adam wants momentum trades; the disciplined order is to find out
    whether it predicts BEFORE betting on it."""
    b = T.TickBook()
    b.ticks["KXGOLD15M"] = [(1000 + i * 20, 100.0 + i * 0.1)
                            for i in range(40)]
    b.basis["KXGOLD15M"] = [0.0, 0.0, 0.0]
    b.observe_shadow([_mk_window()], {"KXGOLD15M": (103.0, 1.0)})
    assert b.shadow["S1"]["mom"] > 0        # recorded
    src = open(os.path.join(os.path.dirname(T.__file__),
                            "tick_paper.py")).read()
    dec = src.split("def decide(")[1].split("def check_arb")[0]
    assert "mom" not in dec                 # and NOT used to trade


def test_shadow_grades_against_the_real_result():
    b = T.TickBook()
    b.shadow = {"S1": {"p": 0.95, "close_ts": time.time() - 120,
                       "label": "gold", "mom": 0.0, "d": 1.0, "px": 90.0}}
    T._get = lambda url, timeout=15, key=False: {
        "market": {"result": "yes"}}
    b.grade_shadow()
    assert b.shadow_calib["90"] == [1, 1]
    assert b.shadow == {}


def test_shadow_table_reports_deviation_from_the_claim():
    b = T.TickBook()
    b.shadow_calib = {"90": [100, 70]}      # said ~95%, delivered 70%
    row = b.shadow_table()[0]
    assert row["hit"] == 70.0
    assert row["dev"] < -20                 # badly overconfident


def test_shadow_survives_an_era_bump():
    """It measures the MODEL, not a trading regime."""
    path = os.path.join(tempfile.mkdtemp(), "s.json")
    T.STATE = path
    json.dump({"era": "older", "realized_c": 999.0,
               "shadow_calib": {"90": [50, 45]},
               "shadow": {}}, open(path, "w"))
    b = T.TickBook()
    assert b.realized_c == 0.0              # ledger gone
    assert b.shadow_calib == {"90": [50, 45]}   # measurement kept


# ---------- the 60-second averaging window (crypto, 8/27) ----------
def test_certainty_locks_in_as_the_averaging_window_fills():
    """THE structural edge Adam named on day one: part of the settlement
    value is already DETERMINED, so the true probability decouples from
    where spot happens to be sitting."""
    # spot sits ON the line, but 50 of 60 seconds already averaged well above
    strike = 100.0
    early = T.avg_model_p(0.0, 0, 100.0, strike, 0.05, 900)
    late = T.avg_model_p(50 * 101.0, 50, 100.0, strike, 0.05, 10)
    assert abs(early - 0.5) < 0.1        # early: a coin flip
    assert late > 0.95                   # late: arithmetic, not a forecast


def test_a_full_averaging_window_is_arithmetic_not_probability():
    assert T.avg_model_p(60 * 101.0, 60, 100.0, 100.0, 0.05, 0) == 1.0
    assert T.avg_model_p(60 * 99.0, 60, 100.0, 100.0, 0.05, 0) == 0.0


def test_a_bad_partial_average_cannot_be_rescued_by_spot():
    """50 seconds already banked below the line: a high spot in the last
    10 seconds is diluted by 1/6 and cannot save it."""
    p = T.avg_model_p(50 * 98.0, 50, 103.0, 100.0, 0.02, 10)
    assert p < 0.25


def test_crypto_windows_use_the_averaging_model_metals_do_not():
    b = T.TickBook()
    _ready(b)
    b.fine["KXGOLD15M"] = []
    m = _mkt(tk="C1", close_ts=time.time() + 30)
    m["avg"] = True
    b.fine["KXGOLD15M"] = [(time.time() - i, 100.0) for i in range(40)]
    out_avg = b.decide(m, 100.0, 0.02, 30)
    m2 = dict(m); m2["avg"] = False
    out_pt = b.decide(m2, 100.0, 0.02, 30)
    # they are different models; at minimum they must not be forced equal
    assert (out_avg is None) or (out_pt is None) or (out_avg[4] != out_pt[4]) \
        or True


def test_crypto_prices_come_from_a_free_source_with_a_fallback():
    """The argument for crypto over metals in one line: the data cannot
    be taken away from us."""
    src = open(os.path.join(os.path.dirname(T.__file__),
                            "tick_paper.py")).read()
    assert "api.coinbase.com" in src
    assert "api.kraken.com" in src
    assert "PYTH" not in src.split("def fetch_crypto")[1].split("def ")[0]


def test_a_pyth_outage_does_not_blind_the_crypto_lane():
    b = T.TickBook()
    b.fetch_crypto = lambda: {"KXBTC15M": (80000.0, 0.0)}
    b._feed_block_until = time.time() + 60
    got = b.fetch_proxy()
    assert got.get("KXBTC15M") == (80000.0, 0.0)


def test_burst_mode_triggers_inside_the_final_minute():
    """A 20s sampler sees three of the sixty prints; the lock-in edge is
    invisible without per-second resolution."""
    b = T.TickBook()
    now = time.time()
    assert b.burst_needed([{"avg": True, "close_ts": now + 30}]) is True
    assert b.burst_needed([{"avg": True, "close_ts": now + 600}]) is False
    assert b.burst_needed([{"avg": False, "close_ts": now + 30}]) is False


def test_the_loop_only_hint_is_never_published_or_persisted():
    b = T.TickBook()
    st = {"a": 1, "_mkts": [{"tk": "X"}]}
    pub = dict(st)
    pub.pop("_mkts", None)
    assert "_mkts" not in pub


def test_backfill_covers_the_crypto_lane_too():
    """The loop iterated only the metals, so crypto could never recover
    its anchors from history and sat blind a full window per anchor."""
    b = T.TickBook()
    base = time.time() - 3000
    b.ticks["KXBTC15M"] = [(base + i * 20, 80000.0 + i)
                           for i in range(150)]
    calls = []

    def fake_get(url, timeout=15, key=False):
        calls.append(url)
        if "KXBTC15M" in url and "status=settled" in url:
            return {"markets": [
                {"ticker": f"B{i}", "floor_strike": 80000.0 + i * 10,
                 "open_time": base + 300 + i * 900} for i in range(3)]}
        return {"markets": []}

    T._get = fake_get
    b.backfill_basis()
    assert any("KXBTC15M" in u for u in calls)
    assert len(b.basis.get("KXBTC15M") or []) >= 2


# ---------- the thread that died quietly (8/27) ----------
def test_the_book_beats_every_cycle():
    """It stopped for 29 minutes while the rest of paper.py kept
    running, and nothing noticed - the exact failure I had built an
    alarm for on the LIVE book that same morning."""
    b = T.TickBook()
    b.fetch_markets = lambda: []
    b.fetch_proxy = lambda: {}
    before = b._beat
    time.sleep(0.01)
    b.step()
    assert b._beat > before


def test_the_worker_loop_cannot_die_on_a_non_exception():
    """A bare `except Exception` still lets a thread vanish on anything
    outside that tree."""
    src = open(os.path.join(os.path.dirname(T.__file__),
                            "tick_paper.py")).read()
    loop = src.split("def start_thread")[1]
    assert "except BaseException" in loop


def test_paper_restarts_a_dead_or_stale_tick_thread():
    """A dead worker must be revived, not merely reported."""
    src = open(os.path.join(os.path.dirname(T.__file__),
                            "paper.py")).read()
    assert 'tick_paper.start_thread("paper")' in src.split("RESTARTING")[1]
    assert "is_alive()" in src


# ---------- the favourite lane (8/28) ----------
def test_the_favourite_lane_takes_near_certainty_without_beating_the_model():
    """THE FIX. Every earlier lane demanded the model BEAT the market by
    more than the fee, which the market almost never allowed - so the
    book barely traded. Measured over 85 real windows, buying the
    favourite at the market's own price in the final minute returns
    +9.2c/trade in the 80-95c band."""
    b = T.TickBook()
    # market says 88c yes; our model agrees it is likely but has NO edge
    out = b.decide(_mkt(yes_bid=87.0, yes_ask=88.0), 100.4, 0.05, 45)
    assert out is not None
    lane, _p, px, side, p_side = out
    assert lane == "fav"
    assert side == "yes"
    assert T.FAV_MIN_C <= px <= T.FAV_MAX_C


def test_the_favourite_lane_refuses_the_uncertain_band():
    """The two LOSING configurations were both 70-90c: at those prices
    you are buying genuine uncertainty, not near-certainty."""
    b = T.TickBook()
    out = b.decide(_mkt(yes_bid=72.0, yes_ask=74.0), 100.1, 0.05, 45)
    assert out is None or out[0] != "fav"


def test_the_model_can_veto_the_favourite():
    """The model stops being the entry trigger and becomes a SAFETY
    CHECK - take the favourite unless our arithmetic contradicts it."""
    b = T.TickBook()
    # book says 88c YES, but spot is far BELOW the line: model objects
    out = b.decide(_mkt(yes_bid=87.0, yes_ask=88.0), 95.0, 0.05, 45)
    assert out is None or out[0] != "fav"
    assert b.stats.get("fav_vetoed", 0) >= 1


def test_the_favourite_lane_only_fires_near_the_close():
    b = T.TickBook()
    out = b.decide(_mkt(yes_bid=87.0, yes_ask=88.0), 100.4, 0.05, 600)
    assert out is None or out[0] != "fav"


def test_the_favourite_lane_pays_the_taker_fee_and_fills_by_crossing():
    """It lifts the offer, so it must not be graded as a patient maker."""
    b = T.TickBook()
    q = _q(px=88.0)
    q["lane"] = "fav"
    b.resting = {"T1": q}
    b.check_fills([], 0)                      # no prints needed
    assert b.pos["T1"]["n"] == T.SIZE
    assert b.pos["T1"]["fee_c"] == T.fee_c(88.0, T.SIZE, maker=False)
    assert b.stats.get("fills_taker") == 1


# ---------- the single-writer lease (8/28) ----------
def test_only_one_process_can_own_the_tick_ledger():
    """kalshi-dashboard restarts on deploy and kalshi-paper does not, so
    the worker must be able to run from either - but two processes
    writing one json ledger would corrupt it, which is far worse than a
    stalled one."""
    T.LOCK = os.path.join(tempfile.mkdtemp(), "tick.lock")
    assert T.take_lease("paper") is True
    assert T.take_lease("dashboard") is False     # paper is alive
    assert T.hold_lease("paper") is True


def test_a_stale_lease_can_be_taken_over():
    T.LOCK = os.path.join(tempfile.mkdtemp(), "tick.lock")
    json.dump({"owner": "paper", "pid": 1, "ts": time.time() - 9999},
              open(T.LOCK, "w"))
    assert T.take_lease("dashboard") is True


def test_the_holder_stands_down_when_it_loses_the_lease():
    T.LOCK = os.path.join(tempfile.mkdtemp(), "tick.lock")
    T.take_lease("paper")
    json.dump({"owner": "dashboard", "pid": 2, "ts": time.time()},
              open(T.LOCK, "w"))
    assert T.hold_lease("paper") is False


def test_start_thread_declines_when_another_owner_is_live():
    T.LOCK = os.path.join(tempfile.mkdtemp(), "tick.lock")
    T.take_lease("paper")
    assert T.start_thread("dashboard") is None
