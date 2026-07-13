import csv
import datetime
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sharp_ev as se


def _ev(start, pinn_home=1.893, pinn_away=2.034):
    # default = fair home ~51.8% vs 50c bid -> ~1.7c blended edge (inside the
    # tradeable band [PROBE_MIN_EDGE_C, MAX_EDGE_C)); pass 1.60/2.60 for a
    # too-good-to-be-true ~8.8c "edge" (rejected by the ceiling)
    return {"commence_time": start.astimezone(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "home_team": "Pittsburgh Pirates", "away_team": "Atlanta Braves",
            "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Pittsburgh Pirates", "price": pinn_home},
                {"name": "Atlanta Braves", "price": pinn_away}]}]}]}


def _mk(start, **kw):
    mons = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    tk = "KXMLBGAME-26%s%02d%02d%02dATLPIT-PIT" % (
        mons[start.month-1], start.day, start.hour, start.minute)
    m = {"ticker": tk, "title": "Atlanta vs Pittsburgh Winner?",
         "yes_sub_title": "Pittsburgh", "yes_bid": 50, "yes_ask": 53,
         "_sport": "baseball_mlb"}
    m.update(kw)
    return m


def _bot():
    p = se.SharpEV.__new__(se.SharpEV)
    p.start = 10000; p.cash = 10000.0; p.bets = {}; p.pending = {}
    p.realized = 0.0; p.wins = p.losses = p.placed = p.canceled = 0
    p.fees = 0.0; p.history = []; p.last_fetch = ""; p.warned_no_key = False
    p.credits_remaining = None; p.next_starts = []; p.shadow_day = ""
    p.shadow_cache = {}; p._shadow_rows = []; p.last_scan = {}
    return p


def _fill(p, last=None):
    """Force-fill every pending order via a synthetic trade-through print."""
    quotes = {}
    for tk, o in p.pending.items():
        px = last if last is not None else (
            o["entry"] if o["side"] == "yes" else 100 - o["entry"])
        quotes[tk] = {"yes_bid": 0, "yes_ask": 0, "last_price": px}
    return p.check_fills(quotes=quotes)


def _backdate(p):
    past = (datetime.datetime.now(se.ET)
            - datetime.timedelta(hours=4)).isoformat(timespec="minutes")
    for b in p.bets.values():
        b["start"] = past


def test_devig_removes_vig():
    f = se.devig({"A": 1.91, "B": 1.91})
    assert abs(f["A"] - 0.5) < 1e-9 and abs(sum(f.values()) - 1) < 1e-9


def test_ticker_parse_with_and_without_time():
    dt, ht, code = se.parse_ticker("KXMLBGAME-26JUL091235ATLPIT-PIT")
    assert ht and code == "PIT" and dt.hour == 12
    d2, ht2, _ = se.parse_ticker("KXWNBAGAME-26JUL07CHIPHX-PHX")
    assert not ht2 and d2 == datetime.date(2026, 7, 7)


def test_team_disambiguator():
    assert se.team_matches("Los Angeles D", "Los Angeles Dodgers")
    assert not se.team_matches("Los Angeles D", "Los Angeles Angels")


def test_pipeline_rests_fills_and_settles():
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    p = _bot()
    cands = p.candidates([_ev(start)], [_mk(start)], now=now)
    assert len(cands) == 1
    assert se.PROBE_MIN_EDGE_C <= cands[0][3] < se.MAX_EDGE_C   # inside the band
    assert p.place(cands) == 1
    assert len(p.pending) == 1 and not p.bets and p.cash == 10000.0  # rests, no cash
    assert _fill(p) == 1
    assert len(p.bets) == 1 and not p.pending and p.cash < 10000.0
    b = list(p.bets.values())[0]
    assert b["entry"] * b["count"] <= se.PROBE_COST_CENTS   # probe stakes
    p.fetch_result = lambda tk: "yes"
    p.settle()
    assert p.wins == 0 and p.bets           # start-guard: game not started yet
    _backdate(p)
    p.settle()
    assert p.wins == 1 and p.realized > 0 and not p.bets


def test_pending_expires_unfilled_at_lockout():
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    p = _bot()
    cands = p.candidates([_ev(start)], [_mk(start)], now=now)
    assert p.place(cands) == 1
    tk = list(p.pending)[0]
    p.pending[tk]["expire"] = (now - datetime.timedelta(minutes=1)
                               ).isoformat(timespec="seconds")
    # quote that never trades through our bid -> cancel at lockout
    p.check_fills(quotes={tk: {"yes_bid": 51, "yes_ask": 53, "last_price": 52}})
    assert not p.pending and not p.bets and p.canceled == 1 and p.cash == 10000.0


def test_no_side_fill_logic():
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    ev = _ev(start)
    ev["bookmakers"][0]["markets"].append({"key": "totals", "outcomes": [
        {"name": "Over", "price": 1.850, "point": 8.5},
        {"name": "Under", "price": 2.086, "point": 8.5}]})  # fair over ~ 53%
    mons = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    tk = "KXMLBTOTAL-26%s%02d%02d%02dATLPIT-9" % (
        mons[start.month-1], start.day, start.hour, start.minute)
    mk = {"ticker": tk, "title": "Atlanta vs Pittsburgh: total runs",
          "yes_sub_title": "Over 8.5 runs scored", "floor_strike": 8.5,
          "strike_type": "greater", "yes_bid": 52, "yes_ask": 55,
          "_sport": "baseball_mlb", "_kind": "total"}
    p = _bot()
    cands = p.candidates([ev], [mk], now=now)
    # market says 53.5 mid, sharp says 53.0 -> NO (under) side has a band edge
    assert len(cands) == 1 and cands[0][2] == "no"
    assert se.PROBE_MIN_EDGE_C <= cands[0][3] < se.MAX_EDGE_C
    assert p.place(cands) == 1
    o = p.pending[tk]
    assert o["side"] == "no" and o["entry"] == 100 - 55
    # a print BELOW 100-entry does not fill a NO bid...
    p.check_fills(quotes={tk: {"yes_bid": 52, "yes_ask": 54, "last_price": 50}})
    assert p.pending and not p.bets
    # ...a print at/through 100-entry does
    p.check_fills(quotes={tk: {"yes_bid": 52, "yes_ask": 54, "last_price": 56}})
    assert not p.pending and len(p.bets) == 1
    _backdate(p)
    p.fetch_result = lambda tk: "no"
    p.settle()
    assert p.wins == 1 and p.realized > 0


def test_filters_reject_bad_candidates():
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    p = _bot()
    ev = _ev(start)
    assert p.candidates([ev], [_mk(start, yes_bid=8, yes_ask=11)], now=now) == []   # longshot
    assert p.candidates([ev], [_mk(start, yes_bid=40, yes_ask=55)], now=now) == []  # wide spread
    live = _ev(now - datetime.timedelta(minutes=5))
    assert p.candidates([live], [_mk(start)], now=now) == []                        # in-play
    far = _ev(now + datetime.timedelta(hours=48))
    assert p.candidates([far], [_mk(start)], now=now) == []                         # too early


def test_disagreeing_books_are_untrustworthy():
    now = datetime.datetime.now(se.ET)
    ev = _ev(now)
    ev["bookmakers"] = [
        {"key": "b%d" % i, "markets": [{"key": "h2h", "outcomes": [
            {"name": "Pittsburgh Pirates", "price": pr},
            {"name": "Atlanta Braves", "price": 5.0 - pr}]}]}
        for i, pr in enumerate([1.45, 2.2, 1.9])]
    assert se.fair_from_books(ev) == ({}, "")


def test_one_bet_per_game_incl_pending():
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    p = _bot()
    mk = _mk(start)
    p.pending[mk["ticker"].rsplit("-", 1)[0] + "-ATL"] = {"entry": 40, "count": 1}
    assert p.candidates([_ev(start)], [mk], now=now) == []


def test_integer_total_lines_skipped():
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    ev = _ev(start)
    ev["bookmakers"][0]["markets"].append({"key": "totals", "outcomes": [
        {"name": "Over", "price": 1.91, "point": 9.0},
        {"name": "Under", "price": 1.91, "point": 9.0}]})
    mk = {"ticker": "KXMLBTOTAL-26JUL091235ATLPIT-9", "title": "Atlanta vs Pittsburgh",
          "yes_sub_title": "Over 9 runs", "floor_strike": 9.0, "strike_type": "greater",
          "yes_bid": 40, "yes_ask": 42, "_sport": "baseball_mlb", "_kind": "total"}
    assert _bot().candidates([ev], [mk], now=now) == []   # push risk -> skip


def test_dollars_fields_normalized():
    assert se._cents({"yes_bid_dollars": "0.6800"}, "yes_bid") == 68
    assert se._cents({"yes_bid": 41}, "yes_bid") == 41
    assert se._cents({}, "yes_bid") == 0


def test_near_game_guard_blocks_offseason():
    now = datetime.datetime.now(se.ET)
    soon = _mk(now + datetime.timedelta(hours=3))
    far = _mk(now + datetime.timedelta(days=60))
    assert se.SharpEV._near_game([soon], now)
    assert not se.SharpEV._near_game([far], now)        # NFL-preseason pattern
    assert not se.SharpEV._near_game([dict(soon, yes_bid=0)], now)  # unquoted
    # date-only tickers (WNBA/NFL): today or tomorrow counts
    d = now + datetime.timedelta(days=1)
    mons = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    dmk = {"ticker": "KXWNBAGAME-26%s%02dCHIPHX-PHX" % (mons[d.month-1], d.day),
           "yes_bid": 40, "yes_ask": 43}
    assert se.SharpEV._near_game([dmk], now)


def test_adaptive_interval_and_pace():
    p = _bot()
    assert p._interval_h() == se.SCAN_HOURS              # unknown budget -> default
    p.last_scan = {"sports": [{"sport": "a"}, {"sport": "b"}]}
    p.credits_remaining = 1e9
    assert p._interval_h() == 1.0                        # huge budget -> clamp fast
    p.credits_remaining = se.CREDIT_RESERVE              # exhausted -> clamp slow
    assert p._interval_h() == 24.0
    p.credits_remaining = 0.0
    assert not p._pace_ok()                              # no slack -> no bursts
    p.credits_remaining = se.CREDITS_MO
    assert p._pace_ok()                                  # untouched budget -> slack


def test_burst_near_game_start():
    p = _bot()
    nowa = datetime.datetime.now(se.ET)
    p.next_starts = [(nowa + datetime.timedelta(hours=8)).isoformat(timespec="minutes")]
    assert not p._burst_near()
    p.next_starts = [(nowa + datetime.timedelta(minutes=45)).isoformat(timespec="minutes")]
    assert p._burst_near()
    p.next_starts = [(nowa + datetime.timedelta(minutes=5)).isoformat(timespec="minutes")]
    assert not p._burst_near()                           # inside lockout: pointless


def test_edge_ceiling_rejects_biggest_edges():
    """v3: edges >= MAX_EDGE_C are information, not opportunity (measured:
    2.0-2.5c realized 3W/14L; shadow 2-3c EV -23c/contract). Rejected in BOTH
    gate modes, but still shadow-logged."""
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    p = _bot()
    big = _ev(start, 1.60, 2.60)          # ~8.8c blended "edge"
    assert p.candidates([big], [_mk(start)], now=now) == []
    assert any(float(r[8]) >= se.MAX_EDGE_C for r in p._shadow_rows)
    p.history = [{"era": se.ERA, "outcome": 1, "pnl": 10, "pside": 0.5}] * 30
    assert p._gate()[0] == "scale"
    assert p.candidates([big], [_mk(start)], now=now) == []       # scale too
    # ...and the band itself still trades in scale mode (gate = sizing only)
    assert len(p.candidates([_ev(start)], [_mk(start)], now=now)) == 1


def test_stale_odds_rejected_fresh_accepted():
    """v3: a candidate anchored to an old book line is a lag artifact -> skip.
    Odds age is logged into the shadow row (col 11); missing last_update is
    treated as fresh (back-compat with fixtures/old feeds)."""
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    utcnow = datetime.datetime.now(datetime.timezone.utc)
    p = _bot()
    stale = _ev(start)
    stale["bookmakers"][0]["markets"][0]["last_update"] = (
        utcnow - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert p.candidates([stale], [_mk(start)], now=now) == []
    assert any(r[11] != "" and float(r[11]) > se.MAX_ODDS_AGE_MIN
               for r in p._shadow_rows)
    fresh = _ev(start)
    fresh["bookmakers"][0]["markets"][0]["last_update"] = (
        utcnow - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert len(p.candidates([fresh], [_mk(start)], now=now)) == 1


def test_rest_time_cap():
    """v3: a resting order expires MAX_REST_H after placement even when the
    game lockout is much later (long rests measured net-negative)."""
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=20)
    p = _bot()
    cands = p.candidates([_ev(start)], [_mk(start)], now=now)
    assert p.place(cands) == 1
    o = list(p.pending.values())[0]
    expd = datetime.datetime.fromisoformat(o["expire"])
    assert expd <= now + datetime.timedelta(hours=se.MAX_REST_H, minutes=1)


def test_revalidate_pending_kills_stale_orders():
    """v3: on each scan the fresh fair for every resting order is captured
    (candidates() -> _pending_eval) and revalidate_pending() cancels orders
    whose edge collapsed - no more free options resting in the book."""
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    p = _bot()
    mk = _mk(start)
    cands = p.candidates([_ev(start)], [mk], now=now)
    assert p.place(cands) == 1
    tk = list(p.pending)[0]
    # next scan sees the same market: captured for revalidation, not re-bet
    assert p.candidates([_ev(start)], [mk], now=now) == []
    assert tk in p._pending_eval
    # fair unchanged -> order survives
    assert p.revalidate_pending() == 0 and tk in p.pending
    # sharp line collapses to 49% -> edge < CANCEL_EDGE_C -> canceled
    p._pending_eval = {tk: (0.49, mk["yes_bid"], mk["yes_ask"])}
    assert p.revalidate_pending() == 1
    assert not p.pending and p.canceled == 1 and p.cash == 10000.0


def test_shadow_report_buckets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("logs", exist_ok=True)
    with open(se.SSHADOWR, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "sport", "kind", "ticker", "side", "fair", "entry_c",
                    "mid_c", "edge_c", "src", "start", "outcome"])
        for i in range(6):   # 2-3c bucket: 4/6 winners at entry 40
            w.writerow(["t%d" % i, "mlb", "ml", "T%d" % i, "yes", 0.45, 40,
                        41, 2.5, "pinnacle", "s", 1 if i < 4 else 0])
        for i in range(4):   # <0 bucket: 1/4 winners
            w.writerow(["u%d" % i, "mlb", "ml", "U%d" % i, "no", 0.40, 45,
                        46, -3.0, "median4", "s", 1 if i < 1 else 0])
    rep = _bot().shadow_report()
    assert rep["n"] == 10
    by = {b["edge"]: b for b in rep["buckets"]}
    assert by["2-3"]["n"] == 6 and abs(by["2-3"]["act"] - 66.7) < 0.1
    assert by["2-3"]["ev_c"] > 0 and by["<0"]["ev_c"] < 0


def test_shadow_report_handles_odds_age_column(tmp_path, monkeypatch):
    """v3 rows are 13-wide (odds_age_m before outcome); report reads outcome
    from the LAST column so old 12-wide and new 13-wide rows mix safely."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("logs", exist_ok=True)
    with open(se.SSHADOWR, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "sport", "kind", "ticker", "side", "fair", "entry_c",
                    "mid_c", "edge_c", "src", "start", "odds_age_m", "outcome"])
        for i in range(3):   # old-style 12-wide row (no age)
            w.writerow(["t%d" % i, "mlb", "ml", "T%d" % i, "yes", 0.5, 45,
                        46, 1.7, "pinnacle", "s", 1])
        for i in range(3):   # new-style 13-wide row (with age)
            w.writerow(["n%d" % i, "mlb", "ml", "N%d" % i, "yes", 0.5, 45,
                        46, 1.7, "pinnacle", "s", 4.2, 0])
    rep = _bot().shadow_report()
    assert rep["n"] == 6
    by = {b["edge"]: b for b in rep["buckets"]}
    assert by["1-2"]["n"] == 6 and abs(by["1-2"]["act"] - 50.0) < 0.1
