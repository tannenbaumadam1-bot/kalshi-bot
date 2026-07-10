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
import os, json, math, re, sys, time, datetime
import requests
from kalshibot.fees import fee_cents
import weather_ensemble as wx
import weather_shadow as ws
import weather_nowcast as nc

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
# v5 (2026-07-03): cut 0.5 -> 0.35. All 70 settled bets showed actual win rate
# below prediction in EVERY bucket (e.g. pred 61% -> actual 0%); the market
# mid was closer to truth than the blended fair, so weight it more.
MODEL_WEIGHT = 0.35
# Small noise smeared around each ensemble member: station micro-climate,
# rounding, and the gap between model grid cell and the physical thermometer.
OBS_JITTER = 1.5

# v7: NOWCAST bets. Same-day markets are priced from station OBSERVATIONS
# (running max/min so far) + remaining-hours ensemble - hard data, so it gets
# a higher blend weight than a pure forecast.
NOWCAST_WEIGHT = float(os.environ.get("WX_NOWCAST_WEIGHT", "0.60"))
NOWCAST_MAX_HRS = float(os.environ.get("WX_NOWCAST_MAX_HRS", "26"))

# v7: MODEL_WEIGHT learned from shadow data (weather_shadow.fit_weight writes
# logs/learned_weight.json daily). Applied only once the sample is real;
# clamped so a fluke fit can never swing sizing to extremes.
LEARNED_W_PATH = os.path.join("logs", "learned_weight.json")
W_MIN_N = 150
W_CLAMP = (0.05, 0.60)
_wcache = {"ts": 0.0, "w": None}


def blend_weight():
    """Forecast-blend weight: learned from shadow calibration when n >= W_MIN_N,
    else the hand-set MODEL_WEIGHT. Reloads at most every 30 minutes."""
    now = time.time()
    if now - _wcache["ts"] > 1800:
        _wcache["ts"] = now
        _wcache["w"] = None
        try:
            d = json.load(open(LEARNED_W_PATH))
            if int(d.get("n", 0)) >= W_MIN_N and d.get("w_best") is not None:
                _wcache["w"] = max(W_CLAMP[0], min(W_CLAMP[1], float(d["w_best"])))
        except Exception:
            pass
    return _wcache["w"] if _wcache["w"] is not None else MODEL_WEIGHT


_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def ticker_date(ticker):
    """Settlement DAY from the ticker itself (e.g. KXLOWTCHI-26JUL11-T68 ->
    2026-07-11). v7 CRITICAL FIX: these markets close ~midnight local = the
    NEXT calendar day in UTC, so deriving the day from close_time on a UTC
    server forecast the WRONG day (+1) for every bet in every prior era."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker or "")
    if not m or m.group(2) not in _MON:
        return None
    return "20%02d-%02d-%02d" % (int(m.group(1)), _MON[m.group(2)], int(m.group(3)))


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
                    # settlement day comes from the TICKER (unambiguous), not
                    # close_time (which is the next UTC day -> v2-v6 forecast
                    # the wrong day). Fallback keeps oddball tickers working.
                    "date": ticker_date(mk["ticker"]) or close.astimezone().strftime("%Y-%m-%d"),
                    "hrs": hrs,
                    "title": ev.get("title", ""), "sub": mk.get("yes_sub_title", ""),
                })
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def scan(min_edge_cents=4, max_edge_cents=20, verbose=True):
    mkts = find_temp_markets()
    fc_cache = {}
    nc_cache = {}
    edges = []
    shadow_rows = []
    src_tot = 0
    for mk in mkts:
        if mk["yes_bid"] <= 0 or mk["yes_ask"] <= 0:
            continue
        key = (mk["city"], mk["date"])
        lat, lon = CITY_COORDS[mk["city"]]
        if key not in fc_cache:
            # MULTI-MODEL ENSEMBLE: fuse ECMWF/GFS/ICON/GEM/MeteoFrance/JMA/UKMO
            # + GFS ensemble + NWS into one calibrated distribution.
            fc_cache[key] = wx.forecast(mk["city"], mk["date"], lat, lon, mk["hrs"])
        fc = fc_cache[key]
        dist = fc["min"] if mk["is_low"] else fc["max"]
        nsrc = fc["n_sources"]
        # need enough INDEPENDENT models to trust the ensemble; else skip
        if not dist.ok() or nsrc < wx.MIN_SOURCES:
            continue
        model = dist.prob_at_least(mk["strike"])
        ftemp = dist.center
        src_tot += nsrc
        if model is None:
            continue
        # SHADOW LOG: record the RAW model prob for EVERY evaluated market
        # (bet or not, tails included) - free calibration data at ~10x the
        # rate of settled bets. Joined to outcomes by weather_shadow.settle.
        # (Always the FORECAST prob, so learned MODEL_WEIGHT stays clean.)
        shadow_rows.append({
            "ticker": mk["ticker"], "city": mk["city"], "date": mk["date"],
            "strike": mk["strike"], "hl": "lo" if mk["is_low"] else "hi",
            "hrs": round(mk["hrs"], 1), "n_sources": nsrc,
            "model_p": round(model, 4),
            "mkt_bid": mk["yes_bid"], "mkt_ask": mk["yes_ask"]})
        # v7 NOWCAST: same-day markets are priced from station OBS (running
        # max/min) + remaining-hours ensemble - hard data beats a forecast.
        src, wgt = "forecast", blend_weight()
        if mk["hrs"] <= NOWCAST_MAX_HRS:
            if key not in nc_cache:
                try:
                    nc_cache[key] = nc.day_state(mk["city"], mk["date"], lat, lon)
                except Exception:
                    nc_cache[key] = None
            stt = nc_cache[key]
            if stt and stt.get("n_obs", 0) >= nc.MIN_OBS:
                p_now = nc.prob_from_state(stt, mk["strike"], mk["is_low"])
                if p_now is not None:
                    model, src, wgt = p_now, "nowcast", NOWCAST_WEIGHT
                    ftemp = stt["run_min"] if mk["is_low"] else stt["run_max"]
        # stay out of the tails: extreme strikes are where a FORECAST is least
        # reliable. A nowcast is grounded in observations, so its band is wide.
        lo_band, hi_band = (0.05, 0.95) if src == "nowcast" else (0.20, 0.80)
        if model < lo_band or model > hi_band:
            continue
        # the MARKET must also see a real contest (15-85c). A 10x disagreement
        # on a tail is our data being wrong, not free money.
        if not (15 <= mk["yes_ask"] <= 85 or 15 <= mk["yes_bid"] <= 85):
            continue
        # shrink toward the market's implied probability (mid of bid/ask):
        # calibration showed the raw model is ~2x overconfident, so we bet only
        # the residual disagreement that survives the blend.
        mkt_prob = ((mk["yes_bid"] + mk["yes_ask"]) / 2.0) / 100.0
        fair = wgt * model + (1 - wgt) * mkt_prob
        mk["src"] = src
        mk["w"] = wgt
        # MAKER entries: instead of crossing the spread (taker, ~7% fee), we
        # REST at the best bid to provide liquidity -> ~1/4 the fee AND a better
        # entry price. YES maker buys at yes_bid; NO maker buys at the no-bid
        # (= 100 - yes_ask). Held to settlement, so this is the only fee leg.
        yes_entry = mk["yes_bid"]
        no_entry = 100 - mk["yes_ask"]
        ev_yes = fair * 100 - yes_entry - fee_cents(yes_entry, 1, taker=False)
        ev_no = (1 - fair) * 100 - no_entry - fee_cents(no_entry, 1, taker=False)
        raw_yes = fair * 100 - yes_entry            # disagreement vs our entry
        raw_no = (1 - fair) * 100 - no_entry
        if ev_yes >= ev_no:
            side, ev, raw, entry = "YES", ev_yes, raw_yes, yes_entry
        else:
            side, ev, raw, entry = "NO", ev_no, raw_no, no_entry
        if entry < 1 or entry > 99:                 # need a real price to rest at
            continue
        # POST-FEE EDGE FLOOR: only bet when expected profit AFTER the (maker)
        # fee clears the bar; skip "too good to be true" gaps (data errors).
        if ev < min_edge_cents or raw > max_edge_cents:
            continue
        mk["entry_price"] = entry                   # maker price place() will use
        mk["maker"] = True
        edges.append((ev, side, mk, fair, ftemp))
    try:
        ws.log(shadow_rows)
    except Exception:
        pass
    edges.sort(key=lambda e: -e[0])
    if verbose:
        cd = len(fc_cache)
        avg = (src_tot / cd) if cd else 0
        print(f"Scanned {len(mkts)} temp markets across {cd} city-days "
              f"(multi-model ensemble, ~{avg:.1f} sources/city).")
        print(f"Found {len(edges)} bets with edge >= {min_edge_cents}c:\n")
        for ev, side, mk, fair, ftemp in edges[:25]:
            print(f"  +{ev:4.1f}c  {side:3} {mk['city']:>13} {mk['strike']}{'(lo)' if mk['is_low'] else '(hi)'}"
                  f"  {mk.get('src','forecast'):8} {ftemp:.0f}F  ourP {fair*100:4.1f}%  mkt {mk['yes_bid']}-{mk['yes_ask']}c  ({mk['hrs']:.0f}h)")
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
        # v7: settlement day parsed from the ticker, never the close time
        assert ticker_date("KXLOWTCHI-26JUL11-T68") == "2026-07-11"
        assert ticker_date("KXHIGHNY-26DEC03-T40") == "2026-12-03"
        assert ticker_date("garbage") is None
        # v7: blend_weight falls back to MODEL_WEIGHT with no learned file
        _wcache["ts"] = 0.0
        _tmp, LEARNED_W_PATH = LEARNED_W_PATH, "does_not_exist.json"
        assert blend_weight() == MODEL_WEIGHT
        LEARNED_W_PATH = _tmp
        print("weather math self-test PASSED (v7 nowcast)")
    else:
        scan()
