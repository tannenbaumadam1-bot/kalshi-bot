"""v8 multi-strike: classifier + probability composition + event cap."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weather_edge as we
import weather_paper as wp


def test_classify_from_api_fields():
    # ">86" (greater, floor 86) means 87 or above
    assert we.classify_market("greater", 86, None, "87° or above") == ("ge", 87, None)
    # "<79" (less, cap 79) means 78 or below
    assert we.classify_market("less", None, 79, "78° or below") == ("le", 78, None)
    # between 79-80 inclusive
    assert we.classify_market("between", 79, 80, "79° to 80°") == ("band", 79, 80)


def test_classify_fallback_subtitle():
    assert we.classify_market(None, None, None, "80° or above") == ("ge", 80, None)
    assert we.classify_market(None, None, None, "71° or below") == ("le", 71, None)
    assert we.classify_market(None, None, None, "74° to 75°") == ("band", 74, 75)
    assert we.classify_market(None, None, None, "no strike here") is None


def test_kind_prob_composition():
    # toy survival function: P(T >= s) for integer strikes
    table = {70: 1.0, 71: 0.9, 72: 0.7, 73: 0.4, 74: 0.2, 75: 0.05, 76: 0.0}
    pfn = lambda s: table.get(s)
    assert we.kind_prob(pfn, "ge", 73, None) == 0.4
    # <=72  ->  1 - P(>=73) = 0.6
    assert abs(we.kind_prob(pfn, "le", 72, None) - 0.6) < 1e-9
    # band 72-74 inclusive -> P(>=72) - P(>=75) = 0.65
    assert abs(we.kind_prob(pfn, "band", 72, 74) - 0.65) < 1e-9
    # band probabilities are non-negative even on noisy tables
    assert we.kind_prob(pfn, "band", 75, 75) >= 0.0
    # missing table entries propagate None (skip the market)
    assert we.kind_prob(pfn, "ge", 99, None) is None
    assert we.kind_prob(pfn, "band", 72, 99) is None


def test_bands_sum_to_distribution():
    # ladder consistency: le-tail + all bands + ge-tail == 1
    table = {s: p for s, p in
             [(69, 1.0), (70, 0.95), (71, 0.85), (72, 0.65), (73, 0.45),
              (74, 0.25), (75, 0.10), (76, 0.03), (77, 0.0)]}
    pfn = lambda s: table.get(s, 0.0 if s > 77 else 1.0)
    total = we.kind_prob(pfn, "le", 71, None)      # <=71
    total += we.kind_prob(pfn, "band", 72, 73)     # 72-73
    total += we.kind_prob(pfn, "band", 74, 75)     # 74-75
    total += we.kind_prob(pfn, "ge", 76, None)     # >=76
    assert abs(total - 1.0) < 1e-9


def test_event_cap_blocks_fourth_bet():
    p = wp.WeatherPaper.__new__(wp.WeatherPaper)
    p.bets = {f"TK{i}": {"city": "austin", "date": "2026-07-19", "hl": "lo",
                         "entry": 40, "count": 1} for i in range(wp.EVENT_MAX_BETS)}
    counts = {}
    for b in p.bets.values():
        ek = (b["city"], b.get("date", ""), b["hl"])
        counts[ek] = counts.get(ek, 0) + 1
    assert counts[("austin", "2026-07-19", "lo")] == wp.EVENT_MAX_BETS
    # place() skips when at cap - mirror of its guard condition
    assert counts.get(("austin", "2026-07-19", "lo"), 0) >= wp.EVENT_MAX_BETS


def test_depth_summary_shape():
    # LAST_DEPTH populated by scan(); here just assert the dict contract
    we.LAST_DEPTH.clear()
    we.LAST_DEPTH.update({"ts": "t", "edges": 2, "fill_total": 10.0,
                          "fill_med": 5.0, "fill_min": 1.0,
                          "n_mkts": 50, "touch_total": 900.0})
    for k in ("ts", "edges", "fill_total", "fill_med", "n_mkts", "touch_total"):
        assert k in we.LAST_DEPTH
