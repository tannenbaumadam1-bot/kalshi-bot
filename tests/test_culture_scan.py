"""8/19 culture scanner (phase 0): classify, match, record - no trading."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import culture_scan as cu


def test_family_classification():
    assert cu.classify("KXSPOTIFYSONGSOUTSIDE", "How many streams will Cardi B's outside have") == "spotify"
    assert cu.classify("KXRTTHEBEAR", "The Bear RT") == "rt"
    assert cu.classify("KXMCPOKEMONLEGENDSZA", "Metacritic score") == "metacritic"
    assert cu.classify("KXTOP10BILLBOARDSPOTSTEDDYSSWIMS", "spots") == "billboard"
    assert cu.classify("KXGRAMBHA", "Best Historical Album") == "award"
    assert cu.classify("KXPS5PROPRICE", "PS5 price cut") == "other"


def test_kworb_matching_tokens():
    s = cu.CultureScan()
    rows = [{"artist": "Cardi B", "title": "Outside",
             "streams": 3200000, "d1": 100, "d7": 2000, "total": 999,
             "toks": sorted(cu._tokens("Cardi B Outside"))},
            {"artist": "Katy Perry", "title": "The One That Got Away",
             "streams": 1, "d1": 1, "d7": 1, "total": 1,
             "toks": sorted(cu._tokens("Katy Perry The One That Got Away"))}]
    row, score = s.match_kworb(
        "How many streams will Cardi B's Outside have on spotify", rows)
    assert row is not None and row["artist"] == "Cardi B"
    row2, _ = s.match_kworb("Completely Unrelated Market", rows)
    assert row2 is None


def test_step_writes_state_and_tape(tmp_path, monkeypatch):
    import recorder as rc
    monkeypatch.setattr(cu, "STATE", str(tmp_path / "culture_state.json"))
    monkeypatch.setattr(rc, "DIR", str(tmp_path / "ticks"))
    monkeypatch.setattr(rc, "ON", True)
    s = cu.CultureScan()
    s.rec = rc.Recorder()
    # stub the two network fetches
    monkeypatch.setattr(s, "fetch_markets", lambda: ([
        {"tk": "KXSPOTIFYSONGSOUTSIDE", "event": "E1",
         "title": "How many streams will Cardi B's Outside have",
         "fam": "spotify", "yb": 40, "ya": 46, "vol": 120, "oi": 60,
         "close": "2026-08-22"},
        {"tk": "KXRTX", "event": "E2", "title": "Movie RT score",
         "fam": "rt", "yb": None, "ya": 80, "vol": 5, "oi": 2,
         "close": "2026-09-01"}], True))
    monkeypatch.setattr(s, "fetch_kworb", lambda: [
        {"artist": "Cardi B", "title": "Outside", "streams": 3200000,
         "d1": 100, "d7": 2000, "total": 999,
         "toks": sorted(cu._tokens("Cardi B Outside"))}])
    st = s.step()
    assert st["scanned"] == 2 and st["liquid"] == 1
    assert st["matched"] == 1 and st["kworb_rows"] == 1
    assert st["families"] == {"spotify": 1, "rt": 1}
    assert st["examples"][0]["counter"] == "Outside"
    saved = json.load(open(tmp_path / "culture_state.json"))
    assert saved["scanned"] == 2
    tape = list(open(next((tmp_path / "ticks").glob("*.jsonl"))))
    assert json.loads(tape[0])["kind"] == "culture"


def test_step_survives_dead_networks(tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "STATE", str(tmp_path / "c.json"))
    s = cu.CultureScan()
    s.rec = None
    monkeypatch.setattr(s, "fetch_markets", lambda: ([], False))
    monkeypatch.setattr(s, "fetch_kworb", lambda: [])
    st = s.step()
    assert st["scanned"] == 0 and st["matched"] == 0
