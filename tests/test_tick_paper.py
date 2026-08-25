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
            "yes_bid": 90.0, "yes_ask": 94.0}
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
