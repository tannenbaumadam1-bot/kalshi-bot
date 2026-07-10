#!/usr/bin/env python3
"""Observation-anchored NOWCAST for same-day Kalshi temperature markets.

The 24-48h forecast game is a fight against an efficient market that prices
the same public models we read. The information edge is INTRADAY: once the
settlement station has been reporting for a few hours, the day's max/min is
partially OBSERVED. P(final >= strike) is then a mix of hard data (the
running extreme so far) and a small remaining-hours forecast - much sharper
than any pure forecast, and often ahead of a slow market.

Sources (keyless):
  - api.weather.gov station observations (the SAME station the market
    settles on) -> running max/min for the local climate day so far.
  - Open-Meteo GFS ensemble hourly members -> distribution of the REMAINING
    hours of the day.

Math: for each ensemble member, final_extreme = max(run, member_remaining)
(min for lows); P(settle >= strike) = mean over members of the smeared
indicator. Observation jitter is small (obs vs CLI rounding), so once the
extreme is locked in the probability saturates - as it should.
"""
from __future__ import annotations
import datetime, math, time
import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:          # pragma: no cover
    ZoneInfo = None

NWS_API = "https://api.weather.gov"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Settlement station (ICAO) per city - same stations weather_edge forecasts.
STATIONS = {
    "atlanta": "KATL", "austin": "KAUS", "boston": "KBOS", "chicago": "KMDW",
    "dallas": "KDFW", "denver": "KDEN", "houston": "KIAH", "las vegas": "KLAS",
    "los angeles": "KLAX", "miami": "KMIA", "minneapolis": "KMSP",
    "new orleans": "KMSY", "new york": "KNYC", "oklahoma city": "KOKC",
    "philadelphia": "KPHL", "phoenix": "KPHX", "san antonio": "KSAT",
    "san francisco": "KSFO", "seattle": "KSEA", "washington": "KDCA",
}
TZS = {
    "atlanta": "America/New_York", "austin": "America/Chicago",
    "boston": "America/New_York", "chicago": "America/Chicago",
    "dallas": "America/Chicago", "denver": "America/Denver",
    "houston": "America/Chicago", "las vegas": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles", "miami": "America/New_York",
    "minneapolis": "America/Chicago", "new orleans": "America/Chicago",
    "new york": "America/New_York", "oklahoma city": "America/Chicago",
    "philadelphia": "America/New_York", "phoenix": "America/Phoenix",
    "san antonio": "America/Chicago", "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "washington": "America/New_York",
}

MIN_OBS = 4          # need this many valid station obs before we trust "run"
OBS_JITTER = 0.8     # degF: obs-vs-CLI rounding / sensor noise
_HDR = {"User-Agent": "kalshi-weather-paper-bot (research; contact via app)"}


def _norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def city_now(city, now_utc=None):
    """Current datetime in the station's timezone (None if tz unavailable)."""
    tz = TZS.get(city)
    if not tz or ZoneInfo is None:
        return None
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc)
    return now_utc.astimezone(ZoneInfo(tz))


def fetch_obs(city, date_str, now_local=None):
    """Running (run_max_F, run_min_F, n_obs) at the settlement station for the
    LOCAL day date_str so far. Returns None if unavailable/not local-today."""
    sid = STATIONS.get(city)
    nl = now_local or city_now(city)
    if not sid or nl is None or nl.strftime("%Y-%m-%d") != date_str:
        return None
    midnight = nl.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        d = requests.get(
            "%s/stations/%s/observations" % (NWS_API, sid),
            params={"start": midnight.isoformat(timespec="seconds"), "limit": 200},
            headers=_HDR, timeout=15).json()
        temps = []
        for ft in d.get("features", []) or []:
            pr = ft.get("properties", {}) or {}
            v = (pr.get("temperature") or {}).get("value")
            ts = pr.get("timestamp", "")
            if v is None or not ts:
                continue
            try:
                t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if t.astimezone(nl.tzinfo).strftime("%Y-%m-%d") != date_str:
                    continue
            except Exception:
                continue
            temps.append(v * 9.0 / 5.0 + 32.0)
        if len(temps) >= MIN_OBS:
            return {"run_max": max(temps), "run_min": min(temps), "n_obs": len(temps)}
    except Exception:
        pass
    return None


def fetch_remaining_members(lat, lon, date_str, now_local):
    """Per-ensemble-member (remaining_max, remaining_min) over the REST of the
    local day. Empty lists if no hours remain (extremes fully observed)."""
    try:
        d = requests.get(ENSEMBLE_API, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m", "models": "gfs_seamless",
            "temperature_unit": "fahrenheit", "timezone": "auto",
            "start_date": date_str, "end_date": date_str,
        }, timeout=25).json()
        h = d.get("hourly", {})
        times = h.get("time", []) or []
        cut = now_local.strftime("%Y-%m-%dT%H:00")
        idx = [i for i, t in enumerate(times) if t > cut]
        rmax, rmin = [], []
        for k in [k for k in h if k.startswith("temperature_2m")]:
            arr = h.get(k) or []
            vals = [arr[i] for i in idx if i < len(arr) and arr[i] is not None]
            if vals:
                rmax.append(max(vals)); rmin.append(min(vals))
        return rmax, rmin
    except Exception:
        return [], []


def final_prob(run, members, strike, is_low, jitter=OBS_JITTER):
    """P(settled integer extreme >= strike). run = observed extreme so far;
    members = per-member remaining-hours extreme (may be empty = day done)."""
    if run is None:
        return None
    finals = ([min(run, m) for m in members] if is_low
              else [max(run, m) for m in members]) or [run]
    j = max(0.1, jitter)
    p = sum(1 - _norm_cdf((strike - 0.5 - f) / j) for f in finals) / len(finals)
    return max(0.0, min(1.0, p))


_DS_CACHE = {}
DS_TTL = 600      # one obs+ensemble fetch per city-day per 10 min


def day_state(city, date_str, lat, lon, now_local=None):
    """Everything needed to price every strike of one city-day, one fetch set.
    Returns {'run_max','run_min','n_obs','rem_max':[..],'rem_min':[..]} or None.
    Cached DS_TTL seconds so exit_check + scan share one fetch per city-day."""
    key = (city, date_str)
    hit = _DS_CACHE.get(key)
    if hit and time.time() - hit[0] < DS_TTL:
        return hit[1]
    nl = now_local or city_now(city)
    obs = fetch_obs(city, date_str, now_local=nl)
    if not obs:
        st = None
    else:
        rmax, rmin = fetch_remaining_members(lat, lon, date_str, nl)
        st = {"run_max": obs["run_max"], "run_min": obs["run_min"],
              "n_obs": obs["n_obs"], "rem_max": rmax, "rem_min": rmin}
    _DS_CACHE[key] = (time.time(), st)
    return st


def prob_from_state(st, strike, is_low, jitter=OBS_JITTER):
    if not st:
        return None
    if is_low:
        return final_prob(st["run_min"], st["rem_min"], strike, True, jitter)
    return final_prob(st["run_max"], st["rem_max"], strike, False, jitter)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        # high already blew through the strike -> near-certain YES
        assert final_prob(95.0, [88.0, 90.0], 92, is_low=False) > 0.98
        # high locked well below strike, no remaining heat -> near-zero
        assert final_prob(84.0, [83.0, 82.0], 92, is_low=False) < 0.02
        # remaining hours may still push above: between the extremes
        p = final_prob(88.0, [89.0, 93.0, 95.0], 92, is_low=False)
        assert 0.3 < p < 0.9
        # LOW: morning min 70 already below the 72 strike -> "low >= 72" ~ 0
        assert final_prob(70.0, [75.0], 72, is_low=True) < 0.05
        # LOW: min so far 75, evening members may dip below 72
        p = final_prob(75.0, [70.0, 74.0, 76.0], 72, is_low=True)
        assert 0.2 < p < 0.8
        # no remaining hours -> pure obs
        assert final_prob(95.0, [], 92, is_low=False) > 0.98
        assert final_prob(None, [90.0], 92, False) is None
        # every station city has a timezone
        for c in STATIONS:
            assert c in TZS, c
        print("weather_nowcast self-test PASSED")
    else:
        for c in ["phoenix", "chicago"]:
            nl = city_now(c)
            st = day_state(c, nl.strftime("%Y-%m-%d"),
                           *{"phoenix": (33.428, -112.004),
                             "chicago": (41.786, -87.752)}[c])
            print(c, st and {k: (round(v, 1) if isinstance(v, float) else
                                 (len(v) if isinstance(v, list) else v))
                             for k, v in st.items()})
