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
    # tests stay offline: no live sharp feed unless a test injects one
    monkeypatch.setattr(b, "fetch_sharp_index", lambda: [])
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


def test_five_lot_floor(tmp_path, monkeypatch):
    # 8/12 (Adam): 5-contract minimum across ALL strategies, paper incl.
    assert sp.MIN_CONTRACTS == 5 and sp.SIZE >= 5
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index", lambda: _pm())
    b.place([_mk()])
    assert next(iter(b.bets.values()))["count"] >= 5


def test_dual_anchor_veto_and_blend(tmp_path, monkeypatch):
    # 8/12: two anchors agreeing = trade at the blend; disagreeing = veto
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index", lambda: _pm())
    sharp_agree = [{"q": "New York Yankees at Boston Red Sox",
                    "toks": sorted(sp._tokens(
                        "New York Yankees at Boston Red Sox")),
                    "probs": {"New York Yankees": 0.86,
                              "Boston Red Sox": 0.14}}]
    monkeypatch.setattr(b, "fetch_sharp_index", lambda: sharp_agree)
    assert b.place([_mk()]) == 1
    pos = next(iter(b.bets.values()))
    assert pos["anchors"] == 2
    assert pos["fair"] == round((0.84 + 0.86) / 2, 3)   # the blend
    # disagreement beyond 5c: refused, counted
    b2 = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b2, "fetch_pm_index", lambda: _pm())
    sharp_off = [dict(sharp_agree[0],
                      probs={"New York Yankees": 0.70,
                             "Boston Red Sox": 0.30})]
    monkeypatch.setattr(b2, "fetch_sharp_index", lambda: sharp_off)
    assert b2.place([_mk()]) == 0
    assert b2.miss.get("anchor_disagree") == 1
    # no sharp match at all: PM-only entry allowed but tagged
    b3 = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b3, "fetch_pm_index", lambda: _pm())
    assert b3.place([_mk()]) == 1
    assert next(iter(b3.bets.values()))["anchors"] == 1


# ---- 8/12: the launch bug - empty PM index (gamma has no 'category') ----

def test_anchor_yes_no_market_prices_the_team(tmp_path, monkeypatch):
    # PM prices many games per-team: "Will the New York Yankees win on
    # 2026-08-12?" with Yes/No outcomes. Yes IS the team's probability.
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index", lambda: _pm(
        question="Will the New York Yankees win on 2026-08-12?",
        probs={"Yes": 0.84, "No": 0.16}))
    assert b.place([_mk()]) == 1
    pos = b.bets[next(iter(b.bets))]
    assert pos["fair"] == 0.84


def test_pm_index_keeps_only_moneyline_games(tmp_path, monkeypatch):
    # gamma /markets rows have NO 'category' field (verified live 8/12 -
    # the old filter kept nothing and the book placed zero for a day).
    # The discriminator is sportsMarketType == "moneyline".
    b = _bot(tmp_path, monkeypatch)
    rows = [
        {"question": "Will the New York Yankees win on 2026-08-12?",
         "sportsMarketType": "moneyline", "volume": "50000",
         "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.84", "0.16"])},
        {"question": "Will the New York Yankees win the 2026 World Series?",
         "volume": "999999",                       # futures: no smt field
         "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.18", "0.82"])},
        {"question": "Yankees vs. Red Sox: winning margin 3+?",
         "sportsMarketType": "spreads", "volume": "50000",
         "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.4", "0.6"])},
        {"question": "Thin game", "sportsMarketType": "moneyline",
         "volume": "5",                            # under PM_MIN_VOL
         "outcomes": json.dumps(["Yes", "No"]),
         "outcomePrices": json.dumps(["0.5", "0.5"])},
    ]

    class _R:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    def _get(*a, **k):        # page 0 has the rows; page 2 is empty
        off = (k.get("params") or {}).get("offset", 0)
        return _R(rows if not off else [])

    monkeypatch.setattr(sp, "requests", type("M", (), {
        "get": staticmethod(_get)}))
    b._pm_cache = {"ts": None, "rows": []}
    out = b.fetch_pm_index()
    assert len(out) == 1
    assert out[0]["probs"] == {"Yes": 0.84, "No": 0.16}


def test_anchor_matches_pm_per_team_question_phrasing(tmp_path,
                                                      monkeypatch):
    # 55 no_anchor with a healthy index: PM prices games as "Will the
    # <team> win on <date>?" - the opponent never appears, so the old
    # >=2-shared-title-token gate could never match a "X vs Y" title.
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index", lambda: _pm(
        question="Will the New York Yankees win on 2026-08-12?",
        probs={"Yes": 0.84, "No": 0.16}))
    assert b.place([_mk(title="Yankees vs. Red Sox Winner?",
                        team="New York Yankees")]) == 1
    assert b.bets[next(iter(b.bets))]["fair"] == 0.84


def test_anchor_needs_every_team_token(tmp_path, monkeypatch):
    # the guard against loose matching: a partial name hit is not our
    # team (Sox: Boston vs Chicago White), so it must refuse
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(b, "fetch_pm_index", lambda: _pm(
        question="Will the Chicago White Sox win on 2026-08-12?",
        probs={"Yes": 0.84, "No": 0.16}))
    assert b.place([_mk(title="Red Sox vs. Yankees Winner?",
                        team="Boston Red Sox")]) == 0
    assert b.miss.get("no_anchor") == 1
