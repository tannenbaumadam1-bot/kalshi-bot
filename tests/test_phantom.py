"""Phantom book (8/20): the paper market-making desk.

The whole value of this module is that its fills are HONEST. Paper
books lie by filling at our price instantly; this one only fills when a
real print says it would have. These tests exist to keep it honest.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phantom as ph


def _bot(tmp_path, monkeypatch):
    monkeypatch.setattr(ph, "STATE", str(tmp_path / "ph.json"))
    b = ph.PhantomBook()
    b.rec = None
    # tests post a quote then send it a print: mirror the live order by
    # treating the quote just posted as the resting one
    _q = b.quote

    def _quote_and_rest(mkts):
        n = _q(mkts)
        b.resting = b.quotes
        return n
    b.quote = _quote_and_rest
    return b


def _mk(tk="KXMLBGAME-T1", yb=45, ya=55, sp="mlb", ev="E1", vol=100):
    return {"tk": tk, "event": ev, "title": "team a beats team b",
            "sport": sp, "yb": yb, "ya": ya, "vol": vol, "oi": 10}


def _tr(tk="KXMLBGAME-T1", px=57, cnt=10, side="yes", tid=None):
    return {"trade_id": tid or f"t{time.time_ns()}", "ticker": tk,
            "yes_price": px, "count": cnt, "taker_side": side}


# ---------------- classification & schema ----------------

def test_sport_classifier_finds_adams_two_sports():
    assert ph.sport("KXMLBGAME-26AUG20", "Yankees vs Red Sox") == "mlb"
    assert ph.sport("KXATPMATCH-X", "Alcaraz to win") == "tennis"
    assert ph.sport("KXUSOPEN-X", "US Open winner") == "tennis"
    assert ph.sport("KXHIGHNY-26AUG20", "NYC high temp") is None


def test_dual_schema_prices():
    assert ph._cents({"yes_bid": 45}, "yes_bid") == 45
    assert ph._cents({"yes_bid_dollars": "0.45"}, "yes_bid") == 45
    assert ph._cents({}, "yes_bid") is None


def test_fee_follows_kalshi_schedule():
    # taker at 50c on 100 contracts: 0.07 x 100 x .25 = $1.75 = 175c
    assert ph.fee_c(50, 100, maker=False) == 175
    # maker is a quarter of that
    assert ph.fee_c(50, 100, maker=True) == 44
    # the tails are cheap - this is why the live book lives at 85-98c
    assert ph.fee_c(95, 100, maker=False) < 40


# ---------------- quoting policy ----------------

def test_quotes_inside_a_wide_market(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    q = b.quotes["KXMLBGAME-T1"]
    assert q["bid"] == 46 and q["ask"] == 54      # stepped inside by 1
    assert q["ask"] > q["bid"]                    # overround survives


def test_tight_markets_join_the_touch(tmp_path, monkeypatch):
    """8/20 (Adam: 'why don't we include it in the actual book'). We
    can't step INSIDE a 2c market, but we can join it - same book, one
    inventory, tagged so the ledger can still split wide from tight."""
    b = _bot(tmp_path, monkeypatch)
    assert b.quote([_mk(yb=49, ya=51)]) == 1
    q = b.quotes["KXMLBGAME-T1"]
    assert q["lane"] == "tight"
    assert q["bid"] == 49 and q["ask"] == 51      # joined, not crossed


def test_refuses_one_sided_and_extreme_books(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(tk="A", yb=0, ya=55), _mk(tk="B", yb=45, ya=0),
             _mk(tk="C", yb=2, ya=9), _mk(tk="D", yb=93, ya=99)])
    assert not b.quotes


def test_quote_cap_prefers_volume(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(ph, "MAX_QUOTES", 2)
    b.quote([_mk(tk=f"T{i}", vol=i) for i in range(6)])
    assert len(b.quotes) == 2
    assert "T5" in b.quotes and "T4" in b.quotes   # busiest books first


# ---------------- fill realism: THE point ----------------

def test_strict_fill_requires_trading_through_us(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])                  # our ask = 54
    b.check_fills([_tr(px=57, side="yes")])        # printed THROUGH 54
    assert b.stats["fills_strict"] == 1
    assert b.stats["fills_loose"] == 0
    assert b.inv["KXMLBGAME-T1"]["sn"] == 10


def test_loose_fill_when_print_is_at_our_price(tmp_path, monkeypatch):
    """At our price = queue-dependent. Counted, but never as strict."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=54, side="yes")])
    assert b.stats["fills_loose"] == 1 and b.stats["fills_strict"] == 0


def test_print_that_never_reaches_us_is_no_fill(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])                  # bid 46 / ask 54
    b.check_fills([_tr(px=50, side="yes"), _tr(px=50, side="no")])
    assert b.stats["fills_strict"] == b.stats["fills_loose"] == 0
    assert not b.inv


def test_taker_side_decides_which_of_our_orders_fills(tmp_path,
                                                      monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no")])         # hit bids, through 46
    r = b.inv["KXMLBGAME-T1"]
    assert r["bn"] == 10 and r["sn"] == 0
    assert b.stats["fills_bid"] == 1


def test_fill_size_capped_by_print_and_by_our_size(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=57, cnt=3, tid="a")])    # small print
    assert b.inv["KXMLBGAME-T1"]["sn"] == 3
    b.check_fills([_tr(px=57, cnt=999, tid="b")])  # huge print
    assert b.inv["KXMLBGAME-T1"]["sn"] == ph.SIZE  # capped at our size


def test_a_trade_is_only_scored_once(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    t = _tr(px=57, cnt=2, tid="dup")
    b.check_fills([t])
    b.check_fills([t])
    assert b.stats["fills_strict"] == 1


# ---------------- the KPI: match rate ----------------

def test_matched_pair_locks_the_spread(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])                  # 46 / 54 -> 8c wide
    b.check_fills([_tr(px=44, side="no", cnt=10, tid="b1"),
                   _tr(px=57, side="yes", cnt=10, tid="a1")])
    bk = b.book([_mk(yb=45, ya=55)])
    assert bk["pairs"] == 10
    # gross 10 x 8c = 80c, minus maker fees both legs, still solidly +
    assert 40 < bk["locked_c"] <= 80
    assert bk["unmatched"] == 0


def test_one_sided_flow_leaves_directional_risk(tmp_path, monkeypatch):
    """Retail flow is one-sided by nature - this is the failure mode
    the whole experiment exists to measure."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=57, side="yes", cnt=10)])   # only sells fill
    bk = b.book([_mk(yb=45, ya=55)])
    assert bk["pairs"] == 0
    assert bk["unmatched"] == 10
    assert bk["cluster_n"] == 1


def test_match_rate_is_published(tmp_path, monkeypatch):
    """Event-weighted rate lives on as match_events; match_rate itself
    is contract-weighted (see test_match_rate_is_contract_weighted)."""
    b = _bot(tmp_path, monkeypatch)
    b.stats["fills_bid"], b.stats["fills_ask"] = 3, 1
    monkeypatch.setattr(b, "fetch_markets", lambda full=True: ([], False))
    monkeypatch.setattr(b, "fetch_trades", lambda s: [])
    st = b.step()
    assert st["match_events"] == 0.5        # 2*min(3,1)/(3+1)
    assert st["match_rate"] is None         # no contracts filled yet


def test_unmatched_inventory_marks_against_us(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=10)])   # we bought at 46
    bk = b.book([_mk(yb=20, ya=30)])                 # mid collapsed to 25
    assert bk["unreal_c"] < 0


# ---------------- adverse selection ----------------

def test_adverse_scored_after_the_clock(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=10)])   # bought, mid was 50
    b.score_adverse([_mk(yb=35, ya=45)])             # mid now 40
    assert b.fills[-1]["adv_f"] is None              # too soon
    b.fills[-1]["ts"] = time.time() - ph.ADV_FAST_S - 1
    b.score_adverse([_mk(yb=35, ya=45)])
    assert b.fills[-1]["adv_f"] == -10.0             # ran us over
    s = b._adverse_summary()
    assert s["fast"]["n"] == 1 and s["fast"]["avg"] == -10.0


def test_adverse_sign_flips_for_sells(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=57, side="yes", cnt=10)])  # we sold at 54
    b.fills[-1]["ts"] = time.time() - ph.ADV_FAST_S - 1
    b.score_adverse([_mk(yb=35, ya=45)])             # mid fell: good
    assert b.fills[-1]["adv_f"] == 10.0


# ---------------- safety & plumbing ----------------

def test_it_cannot_trade():
    """No client, no key, no order path. Phase 0 means phase 0."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "phantom.py")).read()
    for forbidden in ("create_order", "/portfolio/orders", "KalshiClient",
                      "place_order", "private_key"):
        assert forbidden not in src


def test_state_round_trips(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=57, side="yes", cnt=10)])
    b.save({"era": ph.ERA})
    b2 = _bot(tmp_path, monkeypatch)
    assert b2.inv["KXMLBGAME-T1"]["sn"] == 10


def test_era_change_discards_stale_state(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.inv["X"] = {"bn": 1, "bc": 1, "sn": 0, "sc": 0}
    b.save({"era": "somethingelse"})
    b2 = _bot(tmp_path, monkeypatch)
    assert not b2.inv


def test_step_survives_a_dead_exchange(tmp_path, monkeypatch):
    """Never break the paper loop - the tape matters more than any
    single cycle."""
    b = _bot(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("kalshi down")
    monkeypatch.setattr(ph.requests, "get", boom)
    st = b.step()
    assert st["scanned"] == 0 and st["quoted"] == 0
    assert st["era"] == ph.ERA


def test_count_fp_string_is_the_real_field(tmp_path, monkeypatch):
    """Kalshi sends count_fp as a string ('55.00'); reading 'count'
    would have meant zero fills forever - a silent empty experiment."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([{"trade_id": "x", "ticker": "KXMLBGAME-T1",
                    "yes_price_dollars": "0.5700", "count_fp": "6.00",
                    "taker_side": "yes"}])
    assert b.inv["KXMLBGAME-T1"]["sn"] == 6
    assert b.stats["fills_strict"] == 1


def test_block_trades_are_flow_not_fills(tmp_path, monkeypatch):
    """Negotiated off-book: it never touched our resting order."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([{"trade_id": "b1", "ticker": "KXMLBGAME-T1",
                    "yes_price": 57, "count_fp": "500",
                    "taker_side": "yes", "is_block_trade": True}])
    assert not b.inv
    assert b.flow.get("blocks") == 1


def test_flow_bucketed_by_the_spread_it_printed_in(tmp_path, monkeypatch):
    """The decisive early question: do the WIDE books have customers?"""
    b = _bot(tmp_path, monkeypatch)
    tight = _mk(tk="TIGHT", yb=49, ya=51)
    wide = _mk(tk="WIDE", yb=45, ya=55)
    b.quote([tight, wide])
    b.score_flow([{"ticker": "TIGHT", "count_fp": "5"},
                  {"ticker": "TIGHT", "count_fp": "5"},
                  {"ticker": "WIDE", "count_fp": "5"}], [tight, wide])
    assert b.flow["by_spread"]["1-3"] == 2
    assert b.flow["by_spread"]["8-14"] == 1
    assert b.flow["prints"] == 3 and b.flow["contracts"] == 15
    assert b.flow["in_ours"] == 3          # both lanes are quoted now


def test_fill_counters_survive_a_restart(tmp_path, monkeypatch):
    """Caught live 8/20: inventory persisted but counters didn't, so
    match_rate read 0.0 with 40 contracts paired."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=5, tid="b1"),
                   _tr(px=57, side="yes", cnt=5, tid="a1")])
    b.save({"era": ph.ERA})
    b2 = _bot(tmp_path, monkeypatch)
    assert b2.stats["fills_bid"] == 1 and b2.stats["fills_ask"] == 1
    assert b2.stats["fills_strict"] == 2


def test_wide_books_get_a_competitive_quote_not_a_silly_one(
        tmp_path, monkeypatch):
    """8/20 audit: a 25c book was producing a 23c-wide 'quote' that only
    informed flow would ever cross."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=27, ya=52)])                  # 25c wide
    q = b.quotes["KXMLBGAME-T1"]
    assert q["ask"] - q["bid"] <= ph.MAX_WIDTH_C
    assert q["bid"] > 27 and q["ask"] < 52        # inside their market
    assert b.stats["tightened"] == 1


def test_match_rate_is_contract_weighted(tmp_path, monkeypatch):
    """One 2-lot fill + one 20-lot fill is not a 100% matched book."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=2, tid="b1"),
                   _tr(px=57, side="yes", cnt=10, tid="a1")])
    monkeypatch.setattr(b, "fetch_markets",
                        lambda full=True: ([_mk(yb=45, ya=55)], 1))
    monkeypatch.setattr(b, "fetch_trades", lambda s: [])
    st = b.step()
    assert st["match_events"] == 1.0        # one fill each side
    assert st["match_rate"] == 0.333       # only 4 paired of 12 total
    assert st["contracts"] == 12


def test_net_reports_the_residual_against_the_spread(tmp_path, monkeypatch):
    """Locked spread alone flatters the book; net is the truth."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=10, tid="b1")])
    monkeypatch.setattr(b, "fetch_markets",
                        lambda full=True: ([_mk(yb=20, ya=30)], 1))
    monkeypatch.setattr(b, "fetch_trades", lambda s: [])
    st = b.step()
    assert st["net"] == round(st["locked"] + st["unreal"], 2)
    assert st["net"] < 0


def test_event_label_reads_like_a_game_not_a_ticker():
    """Adam 8/20: 'it doesn't show the real event'."""
    assert ph.event_label(
        "KXMLBTOTAL-26AUG201410SEAMIL") == "SEA vs MIL - total runs"
    assert ph.event_label("KXWTAMATCH-26AUG21GAUKOS") == "GAU vs KOS - match"
    assert ph.event_label(None) == "-"
    assert ph.event_label("garbage")          # never raises


def test_per_market_pnl_is_published(tmp_path, monkeypatch):
    """Adam 8/20: 'why doesn't it show p/l for each event'."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(tk="KXMLBTOTAL-26AUG201410SEAMIL", yb=45, ya=55,
                 ev="KXMLBTOTAL-26AUG201410SEAMIL")])
    b.check_fills([_tr(tk="KXMLBTOTAL-26AUG201410SEAMIL", px=44,
                       side="no", cnt=10)])
    bk = b.book([_mk(tk="KXMLBTOTAL-26AUG201410SEAMIL", yb=20, ya=30)])
    r = bk["positions"][0]
    assert r["net"] == 10 and r["pnl"] < 0
    assert r["event"] == "SEA vs MIL - total runs"
    c = bk["clusters"][0]
    assert c["strikes"] == 1 and c["pnl"] == r["pnl"]


def test_clusters_count_strikes_on_one_game(tmp_path, monkeypatch):
    """Six run-total strikes on one game is ONE bet in six costumes."""
    b = _bot(tmp_path, monkeypatch)
    ev = "KXMLBTOTAL-26AUG201410SEAMIL"
    mks = [_mk(tk=f"{ev}-{n}", yb=45, ya=55, ev=ev) for n in (7, 8, 9)]
    b.quote(mks)
    for m in mks:
        b.check_fills([_tr(tk=m["tk"], px=44, side="no", cnt=10)])
    bk = b.book(mks)
    assert bk["clusters"][0]["strikes"] == 3
    assert bk["clusters"][0]["net"] == 30
    assert bk["cluster_n"] == 1


def test_inventory_cap_stops_the_martingale(tmp_path, monkeypatch):
    """8/20 audit #2: quotes re-post every cycle, so one market piled up
    40 lots on one side. A maker caps inventory; a gambler doesn't."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(ph, "MAX_POS", 20)
    monkeypatch.setattr(ph, "SKEW_MAX_C", 0)   # isolate the CAP
    for i in range(8):                       # 8 cycles of one-sided flow
        b.quote([_mk(yb=45, ya=55)])
        b.check_fills([_tr(px=44, side="no", cnt=10, tid=f"c{i}")])
    r = b.inv["KXMLBGAME-T1"]
    assert r["bn"] - r["sn"] <= 20
    assert b.stats.get("pos_capped", 0) > 0


def test_cap_never_blocks_a_fill_that_reduces_risk(tmp_path, monkeypatch):
    """At the cap we must still be able to trade OUT - refusing the
    other side would freeze us long exactly when we want to pair off."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(ph, "MAX_POS", 20)
    for i in range(4):
        b.quote([_mk(yb=45, ya=55)])
        b.check_fills([_tr(px=44, side="no", cnt=10, tid=f"b{i}")])
    n0 = b.inv["KXMLBGAME-T1"]["bn"] - b.inv["KXMLBGAME-T1"]["sn"]
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=57, side="yes", cnt=10, tid="sell")])
    assert b.inv["KXMLBGAME-T1"]["sn"] == 10          # the pairing filled
    assert (b.inv["KXMLBGAME-T1"]["bn"]
            - b.inv["KXMLBGAME-T1"]["sn"]) < n0


def test_cluster_cap_limits_one_game(tmp_path, monkeypatch):
    """Six strikes on one game is one bet - cap the GAME, not the row."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(ph, "MAX_CLUSTER_POS", 30)
    ev = "KXMLBTOTAL-26AUG201410SEAMIL"
    mks = [_mk(tk=f"{ev}-{n}", yb=45, ya=55, ev=ev) for n in range(6)]
    for i in range(3):
        b.quote(mks)
        for m in mks:
            b.check_fills([_tr(tk=m["tk"], px=44, side="no", cnt=10,
                               tid=f"{m['tk']}-{i}")])
    total = sum(abs(v["bn"] - v["sn"]) for v in b.inv.values())
    assert total <= 30 + ph.SIZE          # cap plus the fill in flight


def test_pnl_splits_spread_from_directional_luck(tmp_path, monkeypatch):
    """A headline that mixes them reports a coin flip as a strategy."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=10, tid="b1"),
                   _tr(px=57, side="yes", cnt=5, tid="a1")])
    monkeypatch.setattr(b, "fetch_markets",
                        lambda full=True: ([_mk(yb=95, ya=99)], 1))
    monkeypatch.setattr(b, "fetch_trades", lambda s: [])
    st = b.step()
    assert round(st["spread_pnl"] + st["risk_pnl"], 2) == st["net"]
    assert st["risk_pnl"] > st["spread_pnl"]      # luck dominates
    assert st["per_pair_c"] is not None


# ---------------- 8/20 build: skew, staleness, lanes ----------------

def test_inventory_skew_moves_the_line(tmp_path, monkeypatch):
    """The structural fix for one-sided inventory: long -> shade BOTH
    quotes down, so selling gets easier and adding gets harder. This is
    a book moving its line."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(ph, "HIT_COOLDOWN_S", 0)  # isolate the skew
    b.quote([_mk(yb=45, ya=55)])
    flat = dict(b.quotes["KXMLBGAME-T1"])
    b.check_fills([_tr(px=44, side="no", cnt=10)])   # now long 10
    b.quote([_mk(yb=45, ya=55)])
    long_q = b.quotes["KXMLBGAME-T1"]
    assert long_q["skew"] > 0
    assert long_q["bid"] < flat["bid"]       # harder to buy more
    assert long_q["ask"] < flat["ask"]       # easier to sell out


def test_skew_reverses_when_we_are_short(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(ph, "HIT_COOLDOWN_S", 0)  # isolate the skew
    b.quote([_mk(yb=45, ya=55)])
    flat = dict(b.quotes["KXMLBGAME-T1"])
    b.check_fills([_tr(px=57, side="yes", cnt=10)])  # now short 10
    b.quote([_mk(yb=45, ya=55)])
    q = b.quotes["KXMLBGAME-T1"]
    assert q["skew"] < 0
    assert q["bid"] > flat["bid"] and q["ask"] > flat["ask"]


def test_quotes_never_cross_or_go_marketable(tmp_path, monkeypatch):
    """Skew and widening must never post an order that trades instantly
    - that would make us a taker, paying 4x the fee."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(ph, "SKEW_MAX_C", 20)
    b.quote([_mk(yb=49, ya=51)])
    b.check_fills([_tr(px=48, side="no", cnt=10)])
    b.quote([_mk(yb=49, ya=51)])
    q = b.quotes["KXMLBGAME-T1"]
    assert q["ask"] > q["bid"]
    assert q["ask"] >= 50 and q["bid"] <= 50     # inside/at their touch


def test_stale_quote_is_not_filled(tmp_path, monkeypatch):
    """If the market has jumped past where we quoted, a real maker has
    already cancelled - only someone informed is still lifting us."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])                  # mid 50, ask 54
    b.check_fills([_tr(px=50 + ph.STALE_C + 5, side="yes", cnt=10)])
    assert not b.inv
    assert b.stats["stale_skipped"] == 1


def test_we_back_off_after_being_hit(tmp_path, monkeypatch):
    """A fill is information. Re-arming at the same price into the same
    flow is how a maker donates."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(ph, "SKEW_MAX_C", 0)      # isolate the widening
    b.quote([_mk(yb=45, ya=55)])
    before = dict(b.quotes["KXMLBGAME-T1"])
    b.check_fills([_tr(px=44, side="no", cnt=10)])
    b.quote([_mk(yb=45, ya=55)])
    after = b.quotes["KXMLBGAME-T1"]
    assert after["ask"] - after["bid"] > before["ask"] - before["bid"]
    assert b.stats["widened"] == 1


def test_lanes_are_tagged_through_to_the_ledger(tmp_path, monkeypatch):
    """One book, one inventory - but the tape must still be able to say
    which WIDTH is the business."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(tk="W", yb=45, ya=55), _mk(tk="T", yb=49, ya=51)])
    assert b.quotes["W"]["lane"] == "wide"
    assert b.quotes["T"]["lane"] == "tight"
    b.check_fills([_tr(tk="W", px=57, side="yes", cnt=10, tid="w1"),
                   _tr(tk="T", px=49, side="no", cnt=10, tid="t1")])
    assert b.inv["W"]["lane"] == "wide"
    assert b.inv["T"]["lane"] == "tight"
    assert {f["lane"] for f in b.fills} == {"wide", "tight"}


def test_lane_report_splits_wide_from_tight(tmp_path, monkeypatch):
    """The decision this build exists to inform: is the business the
    wide books nobody trades, or the penny books everybody does?"""
    b = _bot(tmp_path, monkeypatch)
    mks = [_mk(tk="W", yb=45, ya=55), _mk(tk="T", yb=49, ya=51)]
    b.quote(mks)
    b.check_fills([_tr(tk="W", px=57, side="yes", cnt=10, tid="w1"),
                   _tr(tk="W", px=44, side="no", cnt=10, tid="w2"),
                   _tr(tk="T", px=49, side="no", cnt=10, tid="t1")])
    bk = b.book(mks)
    rep = b._lane_report(bk["lanes"])
    assert rep["wide"]["pairs"] == 10 and rep["wide"]["match"] == 1.0
    assert rep["tight"]["pairs"] == 0
    assert rep["tight"]["unmatched"] == 10
    assert rep["wide"]["per_pair_c"] is not None


# ---------------- settlement + the running total ----------------

class _Resp:
    def __init__(self, res):
        self._res = res

    def json(self):
        return {"market": {"result": self._res}}


def test_settlement_banks_a_finished_market(tmp_path, monkeypatch):
    """Without this a running total is a lie: when a game ends the
    market leaves the scan and its P&L silently evaporates."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=10)])     # long 10 @ 46
    monkeypatch.setattr(ph.requests, "get", lambda *a, **k: _Resp("yes"))
    assert b.settle_check([]) == 1                     # market gone
    assert "KXMLBGAME-T1" not in b.inv
    # bought 10 at 46c, settles at 100c: +540c minus fees
    assert 500 < b.realized_c <= 540
    assert b.settled[-1]["result"] == "yes"


def test_settlement_takes_the_loss_too(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=10)])
    monkeypatch.setattr(ph.requests, "get", lambda *a, **k: _Resp("no"))
    b.settle_check([])
    assert b.realized_c < -400          # paid 460c, got nothing


def test_a_paired_market_settles_to_its_spread(tmp_path, monkeypatch):
    """Fully matched inventory is indifferent to the outcome - that IS
    the thesis, and settlement must prove it."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])                       # 46 / 54
    b.check_fills([_tr(px=44, side="no", cnt=10, tid="b1"),
                   _tr(px=57, side="yes", cnt=10, tid="a1")])
    monkeypatch.setattr(ph.requests, "get", lambda *a, **k: _Resp("yes"))
    b.settle_check([])
    yes_pnl = b.realized_c
    b2 = _bot(tmp_path, monkeypatch)
    b2.realized_c = 0.0
    b2.inv = {}
    b2.quote([_mk(yb=45, ya=55)])
    b2.check_fills([_tr(px=44, side="no", cnt=10, tid="b2"),
                    _tr(px=57, side="yes", cnt=10, tid="a2")])
    monkeypatch.setattr(ph.requests, "get", lambda *a, **k: _Resp("no"))
    b2.settle_check([])
    assert round(yes_pnl, 6) == round(b2.realized_c, 6)   # outcome-blind
    assert yes_pnl > 0                                    # the spread


def test_total_is_realized_plus_open(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.realized_c = 250.0
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=10)])
    monkeypatch.setattr(b, "fetch_markets",
                        lambda full=True: ([_mk(yb=45, ya=55)], 1))
    monkeypatch.setattr(b, "fetch_trades", lambda s: [])
    monkeypatch.setattr(b, "settle_check", lambda m: 0)
    st = b.step()
    assert st["realized"] == 2.5
    assert round(st["realized"] + st["open_pnl"], 2) == st["total"]
    assert round(st["spread_pnl"] + st["risk_pnl"], 2) == st["open_pnl"]


def test_realized_survives_a_restart(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.realized_c = 777.0
    b.settled = [{"tk": "X", "pnl": 7.77}]
    b.save({"era": ph.ERA})
    b2 = _bot(tmp_path, monkeypatch)
    assert b2.realized_c == 777.0 and b2.settled[0]["tk"] == "X"


def test_a_print_cannot_hit_a_quote_posted_after_it(tmp_path, monkeypatch):
    """The look-ahead bug that inflated phantom1: quotes were posted
    now and filled against 15 minutes of PAST prints."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    old = {"trade_id": "old", "ticker": "KXMLBGAME-T1",
           "yes_price": 57, "count_fp": "10", "taker_side": "yes",
           "created_time": "2020-01-01T00:00:00Z"}
    b.check_fills([old])
    assert not b.inv
    assert b.stats["pre_quote"] == 1


def test_capital_is_reported_next_to_the_pnl(tmp_path, monkeypatch):
    """A P&L with no denominator is a boast, not a number."""
    b = _bot(tmp_path, monkeypatch)
    b.quote([_mk(yb=45, ya=55)])
    b.check_fills([_tr(px=44, side="no", cnt=10)])
    bk = b.book([_mk(yb=45, ya=55)])
    assert bk["cap_c"] == 460          # 10 bought at 46c


def test_fast_scan_skips_the_rotation(tmp_path, monkeypatch):
    """8/20: the fast path re-prices only the series carrying flow, so
    quotes refresh every cycle instead of every third."""
    b = _bot(tmp_path, monkeypatch)
    b._series = [f"KXMLBEXTRA{i}" for i in range(50)]
    b._ser_ts = time.time()
    b.hot = {"KXMLBHOT"}
    fast, full = b.targets(full=False), b.targets(full=True)
    assert set(fast) == set(ph.CORE_SERIES) | {"KXMLBHOT"}
    assert len(full) > len(fast)


def test_fast_scan_never_settles(tmp_path, monkeypatch):
    """'Not scanned' must never be mistaken for 'finished'."""
    b = _bot(tmp_path, monkeypatch)
    b.inv["HELD"] = {"bn": 10, "bc": 460, "sn": 0, "sc": 0, "fee": 4,
                     "event": "E", "title": "t", "lane": "wide"}
    calls = []
    monkeypatch.setattr(b, "settle_check", lambda m: calls.append(1))
    monkeypatch.setattr(b, "fetch_markets", lambda full=True: ([], 0))
    monkeypatch.setattr(b, "fetch_trades", lambda s: [])
    b.step(full=False)
    assert not calls
    b.step(full=True)
    assert len(calls) == 1
