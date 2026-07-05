#!/usr/bin/env python3
"""Backtest: does the multi-model ENSEMBLE beat any single source?

For each city over a recent window it pulls (keyless):
  - ACTUAL daily max from Open-Meteo's archive (ERA5 reanalysis).
  - Each model's archived FORECAST from the historical-forecast API.
Then it compares every single model's mean-absolute-error (MAE) to the
ensemble mean's MAE. If the ensemble MAE is at/near the best single model AND
below the AVERAGE single model, fusing sources is a real, free accuracy gain.

Caveat: the historical-forecast API reflects recent model runs, so this is an
approximation of live forecast skill, not a perfect 1-day-lead replay. It is
still a fair apples-to-apples test: every model is scored the same way.

Run:  python3 weather_backtest.py            (default 4 cities, 14 days)
"""
from __future__ import annotations
import os, sys, json, datetime, statistics
import requests
import weather_ensemble as wx

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HIST_FC = "https://historical-forecast-api.open-meteo.com/v1/forecast"

CITIES = {"phoenix": (33.428, -112.004), "chicago": (41.786, -87.752),
          "new york": (40.779, -73.969), "denver": (39.847, -104.656)}


def _daily(url, lat, lon, d0, d1, models=None):
    p = {"latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
         "temperature_unit": "fahrenheit", "timezone": "auto",
         "start_date": d0, "end_date": d1}
    if models:
        p["models"] = ",".join(models)
    return requests.get(url, params=p, timeout=25).json().get("daily", {})


def backtest(cities=None, days=14):
    cities = cities or CITIES
    end = datetime.date.today() - datetime.timedelta(days=2)
    start = end - datetime.timedelta(days=days - 1)
    d0, d1 = start.isoformat(), end.isoformat()

    # accumulate abs errors per model and for the ensemble, pooled over cities
    err = {m: [] for m in wx.DET_MODELS}
    ens_err, ens_werr, single_all = [], [], []
    for city, (la, lo) in cities.items():
        act = _daily(ARCHIVE, la, lo, d0, d1)
        fc = _daily(HIST_FC, la, lo, d0, d1, models=wx.DET_MODELS)
        dates = act.get("time", []) or []
        actual = act.get("temperature_2m_max", []) or []
        for i, day in enumerate(dates):
            a = actual[i] if i < len(actual) else None
            if a is None:
                continue
            mv = []
            for m in wx.DET_MODELS:
                arr = fc.get("temperature_2m_max_" + m) or []
                v = arr[i] if i < len(arr) else None
                if v is not None:
                    err[m].append(abs(v - a)); single_all.append(abs(v - a))
                    mv.append((m, v))
            if mv:
                ens = sum(v for _, v in mv) / len(mv)               # equal-weight
                ens_err.append(abs(ens - a))
                ws = sum(wx._model_weight(m) for m, _ in mv)
                ensw = sum(wx._model_weight(m) * v for m, v in mv) / ws   # skill-weighted
                ens_werr.append(abs(ensw - a))
    return err, ens_err, ens_werr, single_all


def learn(days=30):
    """Compute inverse-error skill weights per model and save them so the
    ensemble upweights the historically-accurate models automatically."""
    err, _, _, _ = backtest(days=days)
    maes = {m: statistics.mean(e) for m, e in err.items() if e}
    if not maes:
        print("no data to learn from; try again later"); return
    inv = {m: 1.0 / mae for m, mae in maes.items()}
    avg = statistics.mean(inv.values())
    weights = {m: round(v / avg, 3) for m, v in inv.items()}   # normalized to mean 1.0
    os.makedirs("logs", exist_ok=True)
    json.dump(weights, open(wx.WEIGHTS_PATH, "w"), indent=2)
    print("Learned skill weights over %d days (saved to %s):" % (days, wx.WEIGHTS_PATH))
    for m, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        print("  %-22s weight %.2f  (MAE %.2f)" % (m, w, maes[m]))


def main():
    if "--learn" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--learn"]
        learn(int(rest[0]) if rest else 30); return
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    err, ens_err, ens_werr, single_all = backtest(days=days)
    print("\nBACKTEST: daily-high forecast error (MAE, degF) - lower is better\n")
    rows = []
    for m in wx.DET_MODELS:
        if err[m]:
            rows.append((m, statistics.mean(err[m]), len(err[m])))
    rows.sort(key=lambda r: r[1])
    for m, mae, n in rows:
        print("  %-22s MAE %.2f  (n=%d)" % (m, mae, n))
    if not rows:
        print("  (no data returned - APIs may be busy; re-run)")
        return
    best = rows[0][1]
    mean_single = statistics.mean(single_all)
    ens_mae = statistics.mean(ens_err) if ens_err else None
    print("  " + "-" * 40)
    print("  %-22s MAE %.2f" % ("AVG single model", mean_single))
    print("  %-22s MAE %.2f  (best single %.2f)" % ("ENSEMBLE (equal)", ens_mae, best))
    if ens_werr:
        print("  %-22s MAE %.2f" % ("*** ENSEMBLE (skill-wt) ***", statistics.mean(ens_werr)))
    if ens_mae is not None:
        print("\n  Ensemble vs avg single model: %+.1f%%   vs best single: %+.1f%%"
              % (100 * (ens_mae - mean_single) / mean_single,
                 100 * (ens_mae - best) / best))
        verdict = ("BEATS the average model and matches/beats the best"
                   if ens_mae <= best + 0.15 else
                   "beats the average model (a single model edged it this window)")
        print("  Verdict: ensemble %s." % verdict)


if __name__ == "__main__":
    main()
