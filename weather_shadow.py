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
LEARNED = os.path.join("logs", "learned_weight.json")
# v7: shadow rows logged BEFORE the ticker-date fix priced the WRONG day -
# they must not teach the blend weight. Only fit on rows after the deploy.
FIT_SINCE = os.environ.get("WX_FIT_SINCE", "2026-07-10T16:30")
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


def joined(since=None):
    """Earliest shadow row per settled ticker: [{'mp','out','mid'}, ...].
    since: ISO ts - skip prediction rows logged before it."""
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
            if since and (r.get("ts") or "") < since:
                continue
            if tk in res and tk not in seen:
                seen.add(tk)
                try:
                    rows.append({"mp": float(r["model_p"]), "out": res[tk],
                                 "mid": (float(r["mkt_bid"]) + float(r["mkt_ask"])) / 200.0})
                except Exception:
                    pass
    return rows


def fit_weight(rows=None):
    """Grid-search the Brier-minimizing blend weight w for
    fair = w*model + (1-w)*market_mid on the joined shadow data.
    This replaces per-era MODEL_WEIGHT guesses with measurement."""
    rows = joined(since=FIT_SINCE) if rows is None else rows
    n = len(rows)
    out = {"n": n, "since": FIT_SINCE,
           "ts": datetime.datetime.now().isoformat(timespec="seconds")}
    if not n:
        return out
    def brier(w):
        return sum((w * r["mp"] + (1 - w) * r["mid"] - r["out"]) ** 2
                   for r in rows) / n
    grid = [i / 100.0 for i in range(0, 101, 5)]
    best = min(grid, key=brier)
    out.update({"w_best": best, "brier_model": round(brier(1.0), 4),
                "brier_market": round(brier(0.0), 4),
                "brier_best": round(brier(best), 4)})
    return out


def fit_daily():
    """Write logs/learned_weight.json at most once per calendar day (loop-safe).
    weather_edge.blend_weight() picks it up when n is large enough."""
    st = _load_state()
    today = datetime.date.today().isoformat()
    if st.get("last_fit") == today:
        return None
    st["last_fit"] = today
    _save_state(st)
    d = fit_weight()
    try:
        os.makedirs("logs", exist_ok=True)
        json.dump(d, open(LEARNED, "w"))
    except Exception:
        pass
    return d


def report_data():
    """Machine-readable shadow calibration (for the dashboard /public feed)."""
    rows = joined()          # buckets show ALL history (context)
    buckets = []
    for lo, hi in [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)]:
        sel = [r for r in rows if lo <= r["mp"] < hi]
        if sel:
            buckets.append({
                "bucket": "%d-%d%%" % (lo * 100, min(100, hi * 100)), "n": len(sel),
                "model": round(100 * sum(r["mp"] for r in sel) / len(sel), 1),
                "actual": round(100 * sum(r["out"] for r in sel) / len(sel), 1),
                "mkt": round(100 * sum(r["mid"] for r in sel) / len(sel), 1)})
    # MARKET-price buckets: same joined rows keyed by what the MARKET said
    # early - the favorite-longshot evidence behind the drift book + salvage
    mkt_buckets = []
    for lo, hi in [(0, .1), (.1, .2), (.2, .35), (.35, .5),
                   (.5, .65), (.65, .8), (.8, .9), (.9, .95), (.95, 1.01)]:
        sel = [r for r in rows if lo <= r["mid"] < hi]
        if sel:
            mkt_buckets.append({
                "bucket": "%d-%d" % (lo * 100, min(100, hi * 100)),
                "n": len(sel),
                "mkt": round(100 * sum(r["mid"] for r in sel) / len(sel), 1),
                "actual": round(100 * sum(r["out"] for r in sel) / len(sel), 1)})
    # fit uses its own POST-CUTOFF join (buckets above show full history)
    return {"n": len(rows), "buckets": buckets, "mkt_buckets": mkt_buckets,
            "fit": fit_weight()}


def report():
    """Calibration buckets on the joined shadow data: RAW model prob vs
    actual outcome vs what the market mid believed. Earliest row per ticker."""
    d = report_data()
    if not d["n"]:
        print("no joined shadow data yet")
        return
    print("shadow calibration (RAW model prob vs outcome, %d markets):" % d["n"])
    for b in d["buckets"]:
        print("  %8s: n=%3d  model %5.1f%%  actual %5.1f%%  mkt-mid %5.1f%%"
              % (b["bucket"], b["n"], b["model"], b["actual"], b["mkt"]))
    f = d["fit"]
    if f.get("w_best") is not None:
        print("blend fit: best w=%.2f  brier(model)=%.4f  brier(market)=%.4f  brier(best)=%.4f"
              % (f["w_best"], f["brier_model"], f["brier_market"], f["brier_best"]))


if __name__ == "__main__":
    import sys
    if "--report" in sys.argv:
        report()
    elif "--fit-weight" in sys.argv:
        d = fit_weight()
        print(json.dumps(d, indent=2))
        if d.get("n"):
            json.dump(d, open(LEARNED, "w"))
            print("saved -> " + LEARNED)
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
        # fit_weight: market perfectly calibrated, model junk -> w_best == 0
        rows = ([{"mp": 0.9, "out": 0, "mid": 0.2}] * 40 +
                [{"mp": 0.1, "out": 1, "mid": 0.8}] * 40)
        f = fit_weight(rows)
        assert f["w_best"] == 0.0 and f["brier_market"] < f["brier_model"]
        # model perfectly right, market wrong -> w_best == 1
        rows = ([{"mp": 1.0, "out": 1, "mid": 0.5}] * 40 +
                [{"mp": 0.0, "out": 0, "mid": 0.5}] * 40)
        f = fit_weight(rows)
        assert f["w_best"] == 1.0 and f["brier_model"] < f["brier_market"]
        assert fit_weight([]) .get("w_best") is None
        print("weather_shadow self-test PASSED (dedupe, pending, settle, fit)")
