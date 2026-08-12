"""Sports paper book (sports1): anchored entries, conservative offer
sim, settlement grading, go-live gate math."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sports_paper as sp


def _bot(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "STATE", str(tmp_path / "sp.json"))
    b = sp.SportsPaper()
    return b


def _mk(tk="KXMLBGAME-26AUG12NYARB-NYA", ev="KXMLBGAME-26AUG12NYARB",
        title="Yankees at Red Sox Winner?", team="New York Yankees",
        bid=76, ask=78, hrs=5.0):
    return {"ticker": tk, "event": ev, "title": title, "team": team,
            "yes_bid": bid, "yes_ask": ask, "hrs": hrs}


def _pm(question="Yankees vs. Red Sox", probs=None):
    probs = probs or {"New York Yankees": 0.84, "Boston Red Sox": 0.16}
    return [{"q": question, "toks": sorted(sp._tokens(question)),
             "probs": probs}]


def test_anchor_matching_and_entry(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index", lambda: _pm())
    # fair 84 vs ask 78: edge ~5.5c after fee -> BUY at the real ask
    assert b.place([_mk()]) == 1
    tk = next(iter(b.bets))
    pos = b.bets[tk]
    assert pos["entry"] == 78 and pos["count"] == sp.SIZE
    assert pos["fair"] == 0.84 and pos["edge"] >= sp.EDGE_MIN_C
    # rungs: max(97, 78+6)=97 low rung + 99 high rung
    assert [r[0] for r in pos["rungs"]] == [97, 99]
    # one opinion per game: same event refused
    assert b.place([_mk(tk="OTHER", team="New York Yankees")]) == 0


def test_thin_edge_and_no_anchor_refused(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index",
                        lambda: _pm(probs={"New York Yankees": 0.79,
                                           "Boston Red Sox": 0.21}))
    assert b.place([_mk(bid=76, ask=78)]) == 0     # edge < 3c after fees
    assert b.miss.get("thin_edge") == 1
    monkeypatch.setattr(b, "fetch_pm_index",
                        lambda: _pm(question="Dodgers vs. Padres",
                                    probs={"Dodgers": 0.9}))
    assert b.place([_mk()]) == 0                    # no confident match
    assert b.miss.get("no_anchor") == 1


def test_offer_sim_needs_bid_through(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index", lambda: _pm())
    b.place([_mk()])
    tk = next(iter(b.bets))
    # bid 90: below both rungs - nothing sells
    b.offer_check([_mk(bid=90, ask=92)])
    assert b.sold == 0
    # bid grinds to 97: the 97 rung lifts, 99 keeps waiting
    b.offer_check([_mk(bid=97, ask=99)])
    assert b.sold == 1 and b.bets[tk]["count"] == sp.SIZE - 3
    assert b.history[-1]["sold"] is True and b.history[-1]["pnl"] > 0
    # bid 99: the last rung lifts, position closed, all profit banked
    b.offer_check([_mk(bid=99, ask=100)])
    assert tk not in b.bets and b.sold == 2
    assert b.realized_c > 0


def test_settle_and_gate_math(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index", lambda: _pm())
    b.place([_mk()])
    tk = next(iter(b.bets))
    monkeypatch.setattr(sp, "fetch_result", lambda t: "yes")
    b.settle()
    assert tk not in b.bets
    assert b.gate["w"] == 1 and b.gate["ben"] > 0
    b.save()
    d = json.load(open(sp.STATE))
    g = d["summary"]["gate"]
    assert g["n"] == 1 and g["ready"] is False      # 1 of 200: not ready
    assert d["summary"]["rules"]["anchor"] == "polymarket"


def test_sold_rows_graded_vs_settlement(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index", lambda: _pm())
    b.place([_mk()])
    b.offer_check([_mk(bid=99, ask=100)])           # both rungs lift
    assert not b.bets and len(b.sold_log) == 2
    monkeypatch.setattr(sp, "fetch_result", lambda t: "yes")
    b.settle()
    assert all(r["res"] == "yes" and "kept" in r for r in b.sold_log)
