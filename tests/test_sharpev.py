import datetime
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sharp_ev as se


def _ev(start, pinn_home=1.60, pinn_away=2.60):
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
    p.start = 10000; p.cash = 10000.0; p.bets = {}; p.realized = 0.0
    p.wins = p.losses = p.placed = 0; p.fees = 0.0; p.history = []
    p.last_fetch = ""; p.warned_no_key = False
    return p


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


def test_pipeline_places_probe_and_settles():
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    p = _bot()
    cands = p.candidates([_ev(start)], [_mk(start)], now=now)
    assert len(cands) == 1 and cands[0][3] >= se.MIN_EDGE_C
    assert p.place(cands) == 1
    b = list(p.bets.values())[0]
    assert b["entry"] * b["count"] <= se.PROBE_COST_CENTS   # probe stakes
    p.fetch_result = lambda tk: "yes"
    p.settle()
    assert p.wins == 1 and p.realized > 0 and not p.bets


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


def test_one_bet_per_game():
    now = datetime.datetime.now(se.ET)
    start = now + datetime.timedelta(hours=3)
    p = _bot()
    mk = _mk(start)
    p.bets[mk["ticker"].rsplit("-", 1)[0] + "-ATL"] = {"entry": 40, "count": 1}
    assert p.candidates([_ev(start)], [mk], now=now) == []
