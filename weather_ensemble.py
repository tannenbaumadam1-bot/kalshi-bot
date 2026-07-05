#!/usr/bin/env python3
"""Multi-model weather ENSEMBLE forecaster for Kalshi temperature markets.

The bot's biggest weakness was a SINGLE-source forecast that ran ~2x
overconfident. This module fuses MANY independent numerical weather models
into one calibrated probability, so the edge comes from a genuinely better
forecast than the market's - not from a tight bell curve around one model.

Sources (KEYLESS unless noted), each an independent model:
  - Open-Meteo multi-model: ECMWF-IFS, GFS, ICON, GEM, Meteo-France, JMA, UKMO
    (the world's major operational models) in a single request.
  - Open-Meteo GFS 31-member ensemble (within-model spread / tails).
  - NWS / api.weather.gov gridpoint forecast: the OFFICIAL NBM-based forecast
    for the exact settlement station (best-effort, keyless).
  - Optional keyed tiers, auto-enabled only if the env key is present:
    WeatherAPI (WEATHERAPI_KEY), Visual Crossing (VISUALCROSSING_KEY),
    Tomorrow.io (TOMORROW_KEY).

Combination: every model point-forecast + ensemble member is pooled into a
WEIGHTED kernel-density estimate of the daily max/min. Model DISAGREEMENT
widens the distribution (honest uncertainty); a lead-time kernel bandwidth,
inflated by a calibration factor, sets the residual per-model error. Then
P(settle >= strike) is read off that distribution.

Every source's prediction is logged (logs/weather_sources.csv) so per-source
reliability weights can be LEARNED over time (see weather_backtest.py).
"""
from __future__ import annotations
import os, csv, json, math, datetime
import requests

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
NWS_API = "https://api.weather.gov"
SRC_LOG = os.path.join("logs", "weather_sources.csv")

# Independent deterministic NWP models available keyless via Open-Meteo.
DET_MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless",
              "meteofrance_seamless", "jma_seamless", "ukmo_seamless"]

# Per-model residual error (degF) by lead time = kernel bandwidth. Inflated by
# WX_CAL_SPREAD to correct the observed overconfidence; env-tunable so it can be
# dialed with real calibration data without a code change.
CAL_SPREAD_MULT = float(os.environ.get("WX_CAL_SPREAD", "1.35"))
# Require this many INDEPENDENT sources before we trust the ensemble enough to
# bet; fewer than this and we skip (don't bet on thin data).
MIN_SOURCES = int(os.environ.get("WX_MIN_SOURCES", "3"))

# Per-model skill weights. Priors reflect broad, well-known model reputation
# (ECMWF strongest global model; JMA weaker over the US) and are deliberately
# GENTLE to avoid overfitting. If logs/model_weights.json exists (written by
# `weather_backtest.py --learn` from real error data), it multiplies these -
# that is the "learn which sources are most accurate over time" mechanism.
MODEL_PRIORS = {"ecmwf_ifs025": 1.5, "gfs_seamless": 1.2, "icon_seamless": 1.0,
                "gem_seamless": 0.9, "meteofrance_seamless": 1.0,
                "jma_seamless": 0.8, "ukmo_seamless": 1.0}
WEIGHTS_PATH = "model_weights.json"   # repo root so it deploys
_LEARNED = None


def _model_weight(m):
    global _LEARNED
    if _LEARNED is None:
        try:
            _LEARNED = json.load(open(WEIGHTS_PATH))
        except Exception:
            _LEARNED = {}
    if m in _LEARNED:
        return float(_LEARNED[m])            # data-driven skill weight (learned)
    return MODEL_PRIORS.get(m, 1.0)          # fallback prior when unlearned


def _norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _lead_kernel(hrs):
    if hrs <= 6:    base = 1.5
    elif hrs <= 18: base = 2.3
    elif hrs <= 36: base = 3.2
    else:           base = 4.0
    return base * CAL_SPREAD_MULT


def _get(url, params=None, headers=None, timeout=15):
    return requests.get(url, params=params, headers=headers or {}, timeout=timeout).json()


# ---- sources ---------------------------------------------------------------
def fetch_openmeteo_models(lat, lon, date_str):
    """dict model -> {'max':F,'min':F} for each independent NWP model."""
    out = {}
    try:
        d = _get(OPEN_METEO, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit", "timezone": "auto",
            "start_date": date_str, "end_date": date_str,
            "models": ",".join(DET_MODELS),
        }, timeout=20).get("daily", {})
        for m in DET_MODELS:
            mx = d.get("temperature_2m_max_" + m)
            mn = d.get("temperature_2m_min_" + m)
            vmax = mx[0] if isinstance(mx, list) and mx else None
            vmin = mn[0] if isinstance(mn, list) and mn else None
            if vmax is not None or vmin is not None:
                out[m] = {"max": vmax, "min": vmin}
    except Exception:
        pass
    return out


def fetch_gfs_ensemble(lat, lon, date_str):
    """31-member GFS ensemble daily max/min (F) for the station-local day."""
    try:
        h = _get(ENSEMBLE_API, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m", "models": "gfs_seamless",
            "temperature_unit": "fahrenheit", "timezone": "auto",
            "start_date": date_str, "end_date": date_str,
        }, timeout=25).get("hourly", {})
        maxs, mins = [], []
        for k in [k for k in h if k.startswith("temperature_2m")]:
            vals = [v for v in (h.get(k) or []) if v is not None]
            if len(vals) >= 18:
                maxs.append(max(vals)); mins.append(min(vals))
        if len(maxs) >= 10:
            return {"max": maxs, "min": mins}
    except Exception:
        pass
    return None


def fetch_nws(lat, lon, date_str):
    """Official NWS/NBM forecast for the settlement station (best-effort)."""
    hdr = {"User-Agent": "kalshi-weather-paper-bot (research; contact via app)"}
    try:
        p = _get("%s/points/%.4f,%.4f" % (NWS_API, lat, lon), headers=hdr, timeout=12)
        url = p["properties"]["forecast"]
        periods = _get(url, headers=hdr, timeout=12)["properties"]["periods"]
        hi = lo = None
        for per in periods:
            if (per.get("startTime", "")[:10]) != date_str:
                continue
            t = per.get("temperature")
            if t is None:
                continue
            if per.get("isDaytime"):
                hi = float(t)
            else:
                lo = float(t) if lo is None else lo
        return {"max": hi, "min": lo}
    except Exception:
        return {"max": None, "min": None}


def _fetch_keyed(lat, lon, date_str):
    """Optional keyed providers - only run if their env key is set."""
    out = {}
    wa = os.environ.get("WEATHERAPI_KEY")
    if wa:
        try:
            d = _get("https://api.weatherapi.com/v1/forecast.json", params={
                "key": wa, "q": "%.4f,%.4f" % (lat, lon), "dt": date_str}, timeout=12)
            day = d["forecast"]["forecastday"][0]["day"]
            out["weatherapi"] = {"max": float(day["maxtemp_f"]), "min": float(day["mintemp_f"])}
        except Exception:
            pass
    vc = os.environ.get("VISUALCROSSING_KEY")
    if vc:
        try:
            u = ("https://weather.visualcrossing.com/VisualCrossingWebServices/rest/"
                 "services/timeline/%.4f,%.4f/%s" % (lat, lon, date_str))
            d = _get(u, params={"key": vc, "unitGroup": "us", "include": "days"}, timeout=12)
            day = d["days"][0]
            out["visualcrossing"] = {"max": float(day["tempmax"]), "min": float(day["tempmin"])}
        except Exception:
            pass
    tm = os.environ.get("TOMORROW_KEY")
    if tm:
        try:
            d = _get("https://api.tomorrow.io/v4/weather/forecast", params={
                "location": "%.4f,%.4f" % (lat, lon), "timesteps": "1d",
                "units": "imperial", "apikey": tm}, timeout=12)
            vals = d["timelines"]["daily"][0]["values"]
            out["tomorrow"] = {"max": float(vals["temperatureMax"]),
                               "min": float(vals["temperatureMin"])}
        except Exception:
            pass
    return out


# ---- distribution ----------------------------------------------------------
class Dist:
    """Weighted kernel-density estimate of a daily temperature."""
    def __init__(self, samples, kernel):
        self.samples = [(float(t), float(w)) for t, w in samples if t is not None]
        self.kernel = kernel

    def ok(self):
        return len(self.samples) > 0

    @property
    def center(self):
        if not self.samples:
            return None
        sw = sum(w for _, w in self.samples)
        return sum(t * w for t, w in self.samples) / sw

    def spread(self):
        c = self.center
        if c is None:
            return None
        sw = sum(w for _, w in self.samples)
        var = sum(w * (t - c) ** 2 for t, w in self.samples) / sw
        return math.sqrt(var)

    def prob_at_least(self, strike):
        """P(settled integer temp >= strike), continuity-corrected."""
        if not self.samples:
            return None
        h = max(0.5, self.kernel)
        num = sum(w * (1 - _norm_cdf((strike - 0.5 - t) / h)) for t, w in self.samples)
        den = sum(w for _, w in self.samples)
        return max(0.0, min(1.0, num / den))


def _log_sources(city, date_str, sources):
    try:
        os.makedirs("logs", exist_ok=True)
        new = not os.path.exists(SRC_LOG)
        with open(SRC_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "city", "date", "source", "max_f", "min_f"])
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            for name, v in sources.items():
                w.writerow([ts, city, date_str, name, v.get("max"), v.get("min")])
    except Exception:
        pass


def forecast(city, date_str, lat, lon, hrs=24, log=True):
    """Fuse all available sources into calibrated max/min distributions."""
    kernel = _lead_kernel(hrs)
    det = fetch_openmeteo_models(lat, lon, date_str)
    ens = fetch_gfs_ensemble(lat, lon, date_str)
    nws = fetch_nws(lat, lon, date_str)
    keyed = _fetch_keyed(lat, lon, date_str)

    smax, smin, sources = [], [], {}
    for m, v in det.items():
        w = _model_weight(m)                     # skill-weighted (priors x learned)
        smax.append((v.get("max"), w)); smin.append((v.get("min"), w))
        sources[m] = v
    if nws.get("max") is not None or nws.get("min") is not None:
        smax.append((nws.get("max"), 1.5))     # official station forecast: weight up
        smin.append((nws.get("min"), 1.5))
        sources["nws"] = nws
    for name, v in keyed.items():
        smax.append((v.get("max"), 1.0)); smin.append((v.get("min"), 1.0))
        sources[name] = v
    if ens:
        wt = 2.0 / max(1, len(ens["max"]))      # whole ensemble ~= 2 sources of tail info
        for t in ens["max"]:
            smax.append((t, wt))
        for t in ens["min"]:
            smin.append((t, wt))
        sources["gfs_ens"] = {"max": round(sum(ens["max"]) / len(ens["max"]), 1),
                              "min": round(sum(ens["min"]) / len(ens["min"]), 1)}
    if log:
        _log_sources(city, date_str, sources)
    return {"max": Dist(smax, kernel), "min": Dist(smin, kernel),
            "n_sources": len(sources), "sources": sources}


def prob(city, date_str, lat, lon, strike, is_low, hrs=24, log=True):
    """(probability, center_temp, n_sources) for one Kalshi threshold."""
    fc = forecast(city, date_str, lat, lon, hrs, log=log)
    d = fc["min"] if is_low else fc["max"]
    if not d.ok():
        return None, None, 0
    return d.prob_at_least(strike), d.center, fc["n_sources"]


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        # a spread of models around 100F; P(>=101) should be well under P(>=99)
        d = Dist([(98, 1), (100, 1), (102, 1), (101, 1), (99, 1)], _lead_kernel(24))
        assert d.center is not None and 99.5 <= d.center <= 100.5
        assert d.prob_at_least(90) > 0.95 and d.prob_at_least(112) < 0.05
        assert d.prob_at_least(101) < d.prob_at_least(99)     # monotonic
        # disagreement widens the distribution -> less confident than a tight one
        wide = Dist([(90, 1), (110, 1)], _lead_kernel(24))
        tight = Dist([(100, 1), (100, 1)], _lead_kernel(24))
        assert wide.prob_at_least(105) > tight.prob_at_least(105)  # tail fatter when models disagree
        assert Dist([], _lead_kernel(24)).prob_at_least(100) is None
        print("weather_ensemble self-test PASSED")
    else:
        # live demo on a couple of cities
        for city, (la, lo) in [("phoenix", (33.428, -112.004)),
                               ("chicago", (41.786, -87.752))]:
            today = datetime.date.today().isoformat()
            fc = forecast(city, today, la, lo, 24, log=False)
            print("%-9s  n=%d  high~%.1fF spread %.1f  low~%.1fF" % (
                city, fc["n_sources"], fc["max"].center or -99,
                fc["max"].spread() or 0, fc["min"].center or -99))
