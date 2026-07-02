#!/usr/bin/env python3
"""WEATHER EDGE engine for Kalshi daily temperature markets.  (v4)

Thesis: out-forecast the crowd on daily high/low temperature thresholds and
bet ONLY where our probability beats the market price by more than fees.

v4 upgrades:
- EXACT settlement stations: each Kalshi series settles on one specific NWS
  station (verified 2026-07-02 from the live markets' rules_primary). We now
  forecast that station, not a city-ish coordinate. (Chicago = Midway, NOT
  O'Hare; Dallas = DFW; NYC = Central Park; etc.)
- Series-ticker whitelist instead of title matching: no more accidentally
  parsing hourly/Weather-Company/oddball markets, and we pick up Dallas +
  Las Vegas which title matching missed.
- ENSEMBLE forecasts: 31 GFS ensemble members from Open-Meteo. Probability =
  fraction of members clearing the strike (each smeared by a small
  observation-noise term), replacing the hand-tuned bell curve. Falls back to
  the deterministic forecast + sigma model if the ensemble API is down.
- Probability still shrunk 50/50 toward the market price (v3): calibration
  showed the raw model is overconfident, so any edge must survive shrinkage.

It is a HYPOTHESIS until proven: log every bet with our probability, then
check calibration (do our 70% bets win ~70%?).
"""
from __future__ import annotations
import math, re, sys, datetime
import requests
from kalshibot.fees import fee_cents

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

# Exact NWS settlement station per city (from each series' rulebook).
CITY_COORDS = {
    "atlanta": (33.630, -84.442),        # Hartsfield-Jackson (KATL)
    "austin": (30.183, -97.680),         # Austin-Bergstrom (KAUS)
    "boston": (42.361, -71.010),         # Logan (KBOS)
    "chicago": (41.786, -87.752),        # MIDWAY (KMDW) - rules say Midway, not O'Hare
    "dallas": (32.897, -97.038),         # DFW Intl (KDFW)
    "denver": (39.847, -104.656),        # Denver Intl (KDEN)
    "houston": (29.980, -95.360),        # Bush Intercontinental (KIAH)
    "las vegas": (36.072, -115.163),     # Harry Reid Intl (KLAS)
    "los angeles": (33.938, -118.389),   # LAX
    "miami": (25.788, -80.317),          # Miami Intl (KMIA)
    "minneapolis": (44.883, -93.229),    # MSP
    "new orleans": (29.993, -90.251),    # Louis Armstrong (KMSY)
    "new york": (40.779, -73.969),       # Central Park (KNYC)
    "oklahoma city": (35.393, -97.601),  # Will Rogers (KOKC)
    "philadelphia": (39.868, -75.231),   # PHL Intl
    "phoenix": (33.428, -112.004),       # Sky Harbor (KPHX)
    "san antonio": (29.534, -98.464),    # San Antonio Intl (KSAT)
    "san francisco": (37.620, -122.365), # SFO
    "seattle": (47.444, -122.314),       # SeaTac (KSEA)
    "washington": (38.848, -77.034),     # Reagan National (KDCA)
}

# series_ticker -> (city, is_low). Exact whitelist; anything else is skipped.
SERIES = {
    "KXHIGHAUS": ("austin", False),        "KXLOWTAUS": ("austin", True),
    "KXHIGHCHI": ("chicago", False),       "KXLOWTCHI": ("chicago", True),
    "KXHIGHDEN": ("denver", False),        "KXLOWTDEN": ("denver", True),
    "KXHIGHLAX": ("los angeles", False),   "KXLOWTLAX": ("los angeles", True),
    "KXHIGHMIA": ("miami", False),         "KXLOWTMIA": ("miami", True),
    "KXHIGHNY": ("new york", False),       "KXLOWTNYC": ("new york", True),
    "KXHIGHPHIL": ("philadelphia", False), "KXLOWTPHIL": ("philadelphia", True),
    "KXHIGHTATL": ("atlanta", False),      "KXLOWTATL": ("atlanta", True),
    "KXHIGHTBOS": ("boston", False),       "KXLOWTBOS": ("boston", True),
    "KXHIGHTDAL": ("dallas", False),       "KXLOWTDAL": ("dallas", True),
    "KXHIGHTDC": ("washington", False),    "KXLOWTDC": ("washington", True),
    "KXHIGHTHOU": ("houston", False),      "KXLOWTHOU": ("houston", True),
    "KXHIGHTLV": ("las vegas", False),     "KXLOWTLV": ("las vegas", True),
    "KXHIGHTMIN": ("minneapolis", False),  "KXLOWTMIN": ("minneapolis", True),
    "KXHIGHTNOLA": ("new orleans", False), "KXLOWTNOLA": ("new orleans", True),
    "KXHIGHTOKC": ("oklahoma city", False),"KXLOWTOKC": ("oklahoma city", True),
    "KXHIGHTPHX": ("phoenix", False),      "KXLOWTPHX": ("phoenix", True),
    "KXHIGHTSATX": ("san antonio", False), "KXLOWTSATX": ("san antonio", True),
    "KXHIGHTSEA": ("seattle", False),      "KXLOWTSEA": ("seattle", True),
    "KXHIGHTSFO": ("san francisco", False),"KXLOWTSFO": ("san francisco", True),
}

# Blend our probability with the market's implied probability (v3):
# calibration says the raw model is overconfident; edges must survive shrinkage.
MODEL_WEIGHT = 0.5
# Small noise smeared around each ensemble member: station micro-climate,
# rounding, and the gap between model grid cell and the physical thermometer.
OBS_JITTER = 1.5


def norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def prob_at_least(forecast_temp, strike, sigma):
    """P(settled integer temp >= strike). Continuity: temp>=X ~ true>=X-0.5."""
    z = (strike - 0.5 - forecast_temp) / max(0.1, sigma)
    return max(0.0, min(1.0, 1 - norm_cdf(z)))


def prob_from_members(temps, strike, jitter=OBS_JITTER):
    """P(settled temp >= strike) from ensemble members, each smeared by jitter."""
    if not temps:
        return None
    p = sum(1 - norm_cdf((strike - 0.5 - t) / max(0.1, jitter)) for t in temps)
    return max(0.0, min(1.0, p / len(temps)))


def sigma_for_lead(hours_to_close):
    """Deterministic-fallback forecast error (degF); grows with lead time."""
    if hours_to_close <= 6:   return 1.8
    if hours_to_close <= 18:  return 2.8
    if hours_to_close <= 36:  return 3.6
    return 4.5


def fetch_ensemble(lat, lon, date_str):
    """Per-member daily max/min (F) for the station's LOCAL day.
    Returns {"max": [..], "min": [..]} (31 GFS members) or None on failure."""
    try:
        r = requests.get(ENSEMBLE_API, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m", "models": "gfs_seamless",
            "temperature_unit": "fahrenheit", "timezone": "auto",
            "start_date": date_str, "end_date": date_str,
        }, timeout=25)
        h = r.json().get("hourly", {})
        keys = [k for k in h if k.startswith("temperature_2m")]
        maxs, mins = [], []
        for k in keys:
            vals = [v for v in (h.get(k) or []) if v is not None]
            if len(vals) >= 18:            # need most of the day's hours
                maxs.append(max(vals))
                mins.append(min(vals))
        if len(maxs) >= 10:                # need a real ensemble, not scraps
            return {"max": maxs, "min": mins}
    except Exception:
        pass
    return None


def fetch_forecast(lat, lon, date_str):
    """Deterministic fallback: Open-Meteo daily max/min (F), station-local day."""
    try:
        r = requests.get(OPEN_METEO, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit", "timezone": "auto",
            "start_date": date_str, "end_date": date_str,
        }, timeout=15)
        d = r.json().get("daily", {})
        return {"max": d.get("temperature_2m_max", [None])[0],
                "min": d.get("temperature_2m_min", [None])[0]}
    except Exception:
        return {"max": None, "min": None}


def _f(v):
    try: return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError): return 0.0


def _c(v):
    return int(round(_f(v) * 100))


def find_temp_markets(max_days=2):
    """Near-term Kalshi daily temperature markets (whitelisted series only)."""
    out, cursor, pages = [], None, 0
    now = datetime.datetime.now(datetime.timezone.utc)
    while pages < 45:
        p = {"limit": 200, "status": "open", "with_nested_markets": "true"}
        if cursor: p["cursor"] = cursor
        try:
            d = requests.get(KALSHI + "/events", params=p, timeout=20).json()
        except Exception:
            break
        pages += 1
        for ev in d.get("events", []) or []:
            st = ev.get("series_ticker", "") or ""
            if st not in SERIES:
                continue
            city, is_low = SERIES[st]
            for mk in ev.get("markets", []) or []:
                ct = mk.get("close_time", "")
                try:
                    close = datetime.datetime.strptime(ct, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                except Exception:
                    continue
                hrs = (close - now).total_seconds() / 3600
                if hrs < -2 or hrs > max_days * 24:
                    continue
                m = re.search(r"(-?\d+)\s*°?\s*(or above|or higher|\+)", (mk.get("yes_sub_title") or ""), re.I)
                if not m:
                    continue
                out.append({
                    "ticker": mk["ticker"], "city": city, "is_low": is_low,
                    "strike": int(m.group(1)),
                    "yes_bid": _c(mk.get("yes_bid_dollars")), "yes_ask": _c(mk.get("yes_ask_dollars")),
                    "date": close.astimezone().strftime("%Y-%m-%d"), "hrs": hrs,
                    "title": ev.get("title", ""), "sub": mk.get("yes_sub_title", ""),
                })
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def scan(min_edge_cents=4, max_edge_cents=20, verbose=True):
    mkts = find_temp_markets()
    ens_cache, det_cache = {}, {}
    edges = []
    n_ens = 0
    for mk in mkts:
        if mk["yes_bid"] <= 0 or mk["yes_ask"] <= 0:
            continue
        key = (mk["city"], mk["date"])
        lat, lon = CITY_COORDS[mk["city"]]
        if key not in ens_cache:
            ens_cache[key] = fetch_ensemble(lat, lon, mk["date"])
        ens = ens_cache[key]
        if ens is not None:
            temps = ens["min"] if mk["is_low"] else ens["max"]
            model = prob_from_members(temps, mk["strike"])
            ftemp = sum(temps) / len(temps)
            n_ens += 1
        else:
            # ensemble API down -> deterministic forecast + sigma model
            if key not in det_cache:
                det_cache[key] = fetch_forecast(lat, lon, mk["date"])
            fc = det_cache[key]
            ftemp = fc["min"] if mk["is_low"] else fc["max"]
            if ftemp is None:
                continue
            model = prob_at_least(ftemp, mk["strike"], sigma_for_lead(mk["hrs"]))
        if model is None:
            continue
        # stay out of the tails: extreme strikes are where any model is least
        # reliable; only bet genuinely-uncertain strikes.
        if model < 0.20 or model > 0.80:
            continue
        yes_ask, no_ask = mk["yes_ask"], 100 - mk["yes_bid"]
        # the MARKET must also see a real contest (15-85c). A 10x disagreement
        # on a tail is our data being wrong, not free money.
        if not (15 <= mk["yes_ask"] <= 85 or 15 <= mk["yes_bid"] <= 85):
            continue
        # shrink toward the market's implied probability (mid of bid/ask)
        mkt_prob = ((mk["yes_bid"] + mk["yes_ask"]) / 2.0) / 100.0
        fair = MODEL_WEIGHT * model + (1 - MODEL_WEIGHT) * mkt_prob
        ev_yes = fair * 100 - yes_ask - fee_cents(yes_ask, 1, taker=True)
        ev_no = (1 - fair) * 100 - no_ask - fee_cents(no_ask, 1, taker=True)
        raw_yes = fair * 100 - yes_ask              # disagreement vs market
        raw_no = (1 - fair) * 100 - no_ask
        if ev_yes >= ev_no:
            side, ev, raw = "YES", ev_yes, raw_yes
        else:
            side, ev, raw = "NO", ev_no, raw_no
        # must beat fees, but skip "too good to be true" gaps
        if ev < min_edge_cents or raw > max_edge_cents:
            continue
        edges.append((ev, side, mk, fair, ftemp))
    edges.sort(key=lambda e: -e[0])
    if verbose:
        print(f"Scanned {len(mkts)} temp markets across {len(ens_cache)} city-days "
              f"({n_ens} priced off the 31-member ensemble).")
        print(f"Found {len(edges)} bets with edge >= {min_edge_cents}c:\n")
        for ev, side, mk, fair, ftemp in edges[:25]:
            print(f"  +{ev:4.1f}c  {side:3} {mk['city']:>13} {mk['strike']}{'(lo)' if mk['is_low'] else '(hi)'}"
                  f"  forecast {ftemp:.0f}F  ourP {fair*100:4.1f}%  mkt {mk['yes_bid']}-{mk['yes_ask']}c  ({mk['hrs']:.0f}h)")
        if not edges:
            print("  (no edges right now - market agrees with the forecast)")
    return edges


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert abs(prob_at_least(72, 72, 3.5) - 0.557) < 0.02
        assert abs(prob_at_least(72, 75, 3.5) - 0.238) < 0.02
        assert abs(prob_at_least(72, 68, 3.5) - 0.901) < 0.02
        assert prob_at_least(90, 70, 3) > 0.99 and prob_at_least(50, 70, 3) < 0.01
        # ensemble math: all members at 72 vs strike 72 -> P(T>=71.5), z=-1/3
        assert abs(prob_from_members([72.0] * 31, 72) - norm_cdf(1 / 3.0)) < 0.01
        # members symmetric around the 71.5 continuity threshold -> 50%
        assert abs(prob_from_members([69.5, 70.5, 71.5, 72.5, 73.5], 72) - 0.5) < 0.01
        assert prob_from_members([80] * 20, 72) > 0.99
        assert prob_from_members([60] * 20, 72) < 0.01
        assert prob_from_members([], 72) is None
        # every whitelisted series maps to a station we have coords for
        for _st, (_city, _lo) in SERIES.items():
            assert _city in CITY_COORDS, _city
        print("weather math self-test PASSED (v4 ensemble)")
    else:
        scan()
