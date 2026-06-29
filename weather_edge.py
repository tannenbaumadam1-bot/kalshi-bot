#!/usr/bin/env python3
"""WEATHER EDGE engine for Kalshi daily temperature markets.

Thesis: out-forecast the crowd on daily high/low temperature thresholds using
a free weather model (Open-Meteo), and bet ONLY where our probability beats
the market price by more than fees. Hold to settlement (pay the fee once).

It is a HYPOTHESIS until proven: run it, log the bets it would make and our
probability, then check calibration over weeks (do our 70% bets win ~70%?).

Runs on your machine / server (it fetches Open-Meteo + Kalshi at runtime).
"""
from __future__ import annotations
import math, re, sys, datetime
import requests
from kalshibot.fees import fee_cents

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

# Representative coords per city (airport/station-ish). Refine to the exact
# NWS settlement station later for max accuracy.
CITY_COORDS = {
    "new york": (40.78, -73.97), "nyc": (40.78, -73.97),
    "chicago": (41.96, -87.93), "miami": (25.79, -80.32),
    "austin": (30.18, -97.68), "los angeles": (33.94, -118.40),
    "denver": (39.85, -104.66), "philadelphia": (39.87, -75.23),
    "phoenix": (33.43, -112.01), "seattle": (47.44, -122.31),
    "san francisco": (37.62, -122.37), "minneapolis": (44.88, -93.22),
    "oklahoma city": (35.39, -97.60), "new orleans": (29.99, -90.25),
    "san antonio": (29.53, -98.47), "washington": (38.85, -77.03),
    "houston": (29.99, -95.36), "boston": (42.36, -71.01), "atlanta": (33.63, -84.44),
}


def norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def prob_at_least(forecast_temp, strike, sigma):
    """P(settled integer temp >= strike). Continuity: temp>=X ~ true>=X-0.5."""
    z = (strike - 0.5 - forecast_temp) / max(0.1, sigma)
    return max(0.0, min(1.0, 1 - norm_cdf(z)))


def sigma_for_lead(hours_to_close):
    """Forecast error (degF) grows with lead time. Calibrate from results later."""
    if hours_to_close <= 6:   return 1.8
    if hours_to_close <= 18:  return 2.8
    if hours_to_close <= 36:  return 3.6
    return 4.5


def fetch_forecast(lat, lon, date_str):
    """Open-Meteo daily max/min (F) for a given YYYY-MM-DD. Runtime fetch."""
    try:
        r = requests.get(OPEN_METEO, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit", "timezone": "America/New_York",
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
    """Pull near-term Kalshi daily temperature markets with strikes parsed."""
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
            title = ev.get("title") or ""
            tl = title.lower()
            is_temp = st.startswith(("KXTEMP", "KXHIGHT", "KXLOWT")) or "temperature" in tl
            if not is_temp:
                continue
            city = next((c for c in CITY_COORDS if c in tl), None)
            if not city:
                continue
            is_low = st.startswith("KXLOWT") or " low" in tl or "minimum" in tl
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
                    "title": title, "sub": mk.get("yes_sub_title", ""),
                })
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def scan(min_edge_cents=4, max_edge_cents=20, verbose=True):
    mkts = find_temp_markets()
    fcache = {}
    edges = []
    for mk in mkts:
        if mk["yes_bid"] <= 0 or mk["yes_ask"] <= 0:
            continue
        key = (mk["city"], mk["date"])
        if key not in fcache:
            lat, lon = CITY_COORDS[mk["city"]]
            fcache[key] = fetch_forecast(lat, lon, mk["date"])
        fc = fcache[key]
        ftemp = fc["min"] if mk["is_low"] else fc["max"]
        if ftemp is None:
            continue
        sigma = sigma_for_lead(mk["hrs"])
        fair = prob_at_least(ftemp, mk["strike"], sigma)   # P(temp >= strike)
        yes_ask, no_ask = mk["yes_ask"], 100 - mk["yes_bid"]
        ev_yes = fair * 100 - yes_ask - fee_cents(yes_ask, 1, taker=True)
        ev_no = (1 - fair) * 100 - no_ask - fee_cents(no_ask, 1, taker=True)
        raw_yes = fair * 100 - yes_ask              # disagreement vs market
        raw_no = (1 - fair) * 100 - no_ask
        if ev_yes >= ev_no:
            side, ev, raw = "YES", ev_yes, raw_yes
        else:
            side, ev, raw = "NO", ev_no, raw_no
        # must beat fees, but skip "too good to be true" gaps - those are
        # almost always a data mismatch (wrong station/timing), not alpha.
        if ev < min_edge_cents or raw > max_edge_cents:
            continue
        edges.append((ev, side, mk, fair, ftemp))
    edges.sort(key=lambda e: -e[0])
    if verbose:
        print(f"Scanned {len(mkts)} temp markets across {len(fcache)} city-days.")
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
        print("weather math self-test PASSED")
    else:
        scan()
