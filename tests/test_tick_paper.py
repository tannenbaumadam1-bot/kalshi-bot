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
    t = T.start_thread()
    assert t.daemon is True
    assert t.name == "tick"


def test_only_one_writer_paper_does_not_also_step_the_book():
    """Two writers on one json.dump is a corrupted ledger."""
    here = os.path.dirname(T.__file__)
    src = open(os.path.join(here, "paper.py")).read()
    assert "tick_paper.start_thread()" in src
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
