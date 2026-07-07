#!/usr/bin/env python3
"""Shadow calibration logger for the weather model.

The weather book's bottleneck is CALIBRATION DATA: only ~5-8 bets settle per
day, so judging a model era (30-bet gate) takes weeks. But every scan already
computes a model probability for EVERY quoted temp market (~40/scan) - and
betting on none of them costs nothing. Log them all, settle them later:
5-10x the calibration data per day, free, with zero capital at risk.

Files (append-only; join on ticker offline or via report()):
- logs/weather_shadow.csv          predictions: ts, ticker, city, date, strike,
                                   hl, hrs, n_sources, model_p (RAW pre-shrink),
                                   mkt_bid, mkt_ask
- logs/weather_shadow_results.csv  outcomes: ticker, outcome (1=YES), ts

Dedupe: at most one prediction row per ticker per DEDUPE_HOURS (so the
earliest row per ticker is a clean fixed-lead sample; later rows add a
lead-time dimension). settle_daily() is called from the bot loop and runs at
most once per calendar day with a bounded number of API lookups.

Run `python3 weather_shadow.py --report` for raw-model-vs-actual calibration
buckets alongside what the market mid said - this is the data that decides
MODEL_WEIGHT empirically instead of by era-sized guesses.
"""
from __future__ import annotations
import os, json, csv, datetime
import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SHADOW = os.path.join("logs", "weather_shadow.csv")
RESULTS = os.path.join("logs", "weather_shadow_results.csv")
STATE = os.path.join("logs", "weather_shadow_state.json")
DEDUPE_HOURS = 3
MAX_LOOKUPS = 80
COLS = ["ts", "ticker", "city", "date", "strike", "hl", "hrs",
        "n_sources", "model_p", "mkt_bid", "mkt_ask"]


def _load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save_state(st):
    try:
        os.makedirs("logs", exist_ok=True)
        json.dump(st, open(STATE, "w"))
    except Exception:
        pass


def _iso_ok(v, cutoff):
    try:
        return datetime.datetime.fromisoformat(v) >= cutoff
    except Exception:
        return False


def log(rows, now=None):
    """Append prediction rows (dicts keyed like COLS[1:]), deduped per ticker
    per DEDUPE_HOURS. Returns number of rows written."""
    if not rows:
        return 0
    now = now or datetime.datetime.now()
    st = _load_state()
    seen = st.get("seen", {})
    out = []
    for r in rows:
        tk = r.get("ticker")
        if not tk:
            continue
        last = seen.get(tk)
        if last and _iso_ok(last, now - datetime.timedelta(hours=DEDUPE_HOURS)):
            continue
        seen[tk] = now.isoformat(timespec="seconds")
        out.append(r)
    if out:
        try:
            os.makedirs(os.path.dirname(SHADOW) or ".", exist_ok=True)
            new = not os.path.exists(SHADOW)
            with open(SHADOW, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(COLS)
                ts = now.isoformat(timespec="seconds")
                for r in out:
                    w.writerow([ts] + [r.get(c, "") for c in COLS[1:]])
        except Exception:
            return 0
    cutoff = now - datetime.timedelta(days=6)
    st["seen"] = {k: v for k, v in seen.items() if _iso_ok(v, cutoff)}
    _save_state(st)
    return len(out)


def _uniq(path, col):
    out = set()
    try:
        with open(path) as f:
            for row in csv.DictReader(f):
                v = row.get(col)
                if v:
                    out.add(v)
    except Exception:
        pass
    return out


def pending(today=None):
    """Tickers logged, unresolved, whose market DATE has passed (sorted)."""
    today = today or datetime.date.today().isoformat()
    done = _uniq(RESULTS, "ticker")
    out = set()
    try:
        with open(SHADOW) as f:
            for row in csv.DictReader(f):
                tk, dt = row.get("ticker"), row.get("date") or ""
                if tk and tk not in done and dt and dt < today:
                    out.add(tk)
    except Exception:
        pass
    return sorted(out)


def fetch_result(tk):
    try:
        d = requests.get(KALSHI + "/markets/" + tk, timeout=15).json()
        res = ((d.get("market", d) or {}).get("result") or "").lower()
        return res if res in ("yes", "no") else None
    except Exception:
        return None


def settle(max_lookups=MAX_LOOKUPS):
    """Resolve pending tickers (bounded). Returns rows appended."""
    n = 0
    for tk in pending()[:max_lookups]:
        res = fetch_result(tk)
        if res is None:
            continue
        try:
            os.makedirs(os.path.dirname(RESULTS) or ".", exist_ok=True)
            new = not os.path.exists(RESULTS)
            with open(RESULTS, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ticker", "outcome", "ts"])
                w.writerow([tk, 1 if res == "yes" else 0,
                            datetime.datetime.now().isoformat(timespec="seconds")])
            n += 1
        except Exception:
            pass
    return n


def settle_daily():
    """Run settle() at most once per calendar day (bot-loop safe)."""
    st = _load_state()
    today = datetime.date.today().isoformat()
    if st.get("last_settle") == today:
        return 0
    st["last_settle"] = today
    _save_state(st)
    return settle()


def report():
    """Calibration buckets on the joined shadow data: RAW model prob vs
    actual outcome vs what the market mid believed. Earliest row per ticker."""
    res = {}
    if os.path.exists(RESULTS):
        for r in csv.DictReader(open(RESULTS)):
            try:
                res[r["ticker"]] = int(r["outcome"])
            except Exception:
                pass
    rows, seen = [], set()
    if os.path.exists(SHADOW):
        for r in csv.DictReader(open(SHADOW)):
            tk = r.get("ticker")
            if tk in res and tk not in seen:
                seen.add(tk)
                try:
                    rows.append((float(r["model_p"]), res[tk],
                                 float(r["mkt_bid"]), float(r["mkt_ask"])))
                except Exception:
                    pass
    if not rows:
        print("no joined shadow data yet")
        return
    print("shadow calibration (RAW model prob vs outcome, %d markets):" % len(rows))
    for lo, hi in [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)]:
        sel = [t for t in rows if lo <= t[0] < hi]
        if not sel:
            continue
        mp = sum(t[0] for t in sel) / len(sel)
        ao = sum(t[1] for t in sel) / len(sel)
        mm = sum((t[2] + t[3]) / 200.0 for t in sel) / len(sel)
        print("  %3.0f-%3.0f%%: n=%3d  model %5.1f%%  actual %5.1f%%  mkt-mid %5.1f%%"
              % (lo * 100, hi * 100, len(sel), mp * 100, ao * 100, mm * 100))


if __name__ == "__main__":
    import sys
    if "--report" in sys.argv:
        report()
    elif "--settle" in sys.argv:
        print("settled %d shadow tickers" % settle())
    elif "--selftest" in sys.argv:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            SHADOW = os.path.join(td, "s.csv")
            RESULTS = os.path.join(td, "r.csv")
            STATE = os.path.join(td, "st.json")
            now = datetime.datetime(2026, 7, 7, 12, 0)
            rows = [{"ticker": "T1", "city": "boston", "date": "2026-07-06",
                     "strike": 70, "hl": "hi", "hrs": 5, "n_sources": 4,
                     "model_p": 0.61, "mkt_bid": 40, "mkt_ask": 44}]
            assert log(rows, now=now) == 1
            assert log(rows, now=now + datetime.timedelta(hours=1)) == 0  # dedupe
            assert log(rows, now=now + datetime.timedelta(hours=4)) == 1  # re-log
            assert pending(today="2026-07-07") == ["T1"]
            fetch_result = lambda tk: "yes"        # noqa: E731
            globals()["fetch_result"] = fetch_result
            assert settle() == 1
            assert pending(today="2026-07-07") == []
        print("weather_shadow self-test PASSED (dedupe, pending, settle)")
