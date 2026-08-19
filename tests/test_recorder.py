"""8/19 tape recorder: record everything; never break trading."""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder as rc
import drift_live as dl

TODAY = datetime.date.today().isoformat()


def _rec(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "DIR", str(tmp_path / "ticks"))
    monkeypatch.setattr(rc, "ON", True)
    return rc.Recorder()


def test_cycle_writes_one_compact_jsonl_line(tmp_path, monkeypatch):
    r = _rec(tmp_path, monkeypatch)
    mkts = [{"ticker": "T1", "yes_bid": 85, "yes_ask": 88,
             "bid_size": 50.0, "ask_size": 40.0, "vol": 120.0,
             "hrs": 9.5}]
    bets = {"T1": {"side": "no", "entry": 88, "count": 5, "mk_px": 12}}
    r.cycle(mkts, bets, 3, 2, 1, {"m": 12200, "c": 13000})
    f = tmp_path / "ticks" / f"{TODAY}.jsonl"
    rows = [json.loads(x) for x in open(f)]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "cycle"
    assert row["mkts"] == [["T1", 85, 88, 50.0, 40.0, 120.0, 9.5]]
    assert row["bets"] == [["T1", "no", 88, 5, 12]]
    assert row["nav"]["m"] == 12200
    assert r.lines_today == 1 and r.err_today == 0


def test_appends_across_cycles_and_counts(tmp_path, monkeypatch):
    r = _rec(tmp_path, monkeypatch)
    for _ in range(4):
        r.cycle([], {}, 0, 0, 0, {})
    f = tmp_path / "ticks" / f"{TODAY}.jsonl"
    assert len(open(f).readlines()) == 4
    assert r.stats()["lines"] == 4


def test_event_lines_and_unserializable_payloads_never_raise(
        tmp_path, monkeypatch):
    r = _rec(tmp_path, monkeypatch)
    r.event("cut", tk="T1", px=36)
    r.write({"bad": object()})          # default=str: still serializes
    r.cycle(None, None, 0, 0, 0, None)  # nulls everywhere: fine
    f = tmp_path / "ticks" / f"{TODAY}.jsonl"
    assert len(open(f).readlines()) == 3
    assert r.err_today == 0


def test_retention_sweeps_old_tape(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "DIR", str(tmp_path / "ticks"))
    monkeypatch.setattr(rc, "ON", True)
    monkeypatch.setattr(rc, "RETAIN_DAYS", 10)
    os.makedirs(tmp_path / "ticks")
    old = (datetime.date.today()
           - datetime.timedelta(days=30)).isoformat()
    open(tmp_path / "ticks" / f"{old}.jsonl", "w").write("x\n")
    r = rc.Recorder()
    r.cycle([], {}, 0, 0, 0, {})
    assert not os.path.exists(tmp_path / "ticks" / f"{old}.jsonl")
    assert os.path.exists(tmp_path / "ticks" / f"{TODAY}.jsonl")


def test_off_switch_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "DIR", str(tmp_path / "ticks"))
    monkeypatch.setattr(rc, "ON", False)
    r = rc.Recorder()
    r.cycle([], {}, 0, 0, 0, {})
    r.event("x")
    assert not os.path.exists(tmp_path / "ticks")
    assert r.stats()["on"] is False


def test_executor_publishes_recorder_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dl, "BETS", str(tmp_path / "b.csv"))
    monkeypatch.setattr(dl, "BUCKET_ALLOW", set())
    monkeypatch.setattr(dl, "GATE_FORCE", "")
    b = dl.DriftLive(None, mode="DRY")
    assert b.rec is not None            # recorder rides along
    assert "on" in b.rec.stats()
