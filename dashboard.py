#!/usr/bin/env python3
"""Institutional-grade live dashboard for the Kalshi WEATHER paper book.

Single-file: serves an auto-refreshing page with NAV, P&L attribution,
performance / risk / execution KPIs, equity curve, daily P&L, strategy-era
breakdown (current model vs legacy), calibration table, and marked-to-market
open positions. Reads logs/weather_state.json; live marks from Kalshi's
public market data (cached 60s). No keys, nothing sensitive.

Public mode (cloud):
    DASH_HOST=0.0.0.0 DASH_PORT=8765 DASH_TOKEN=somesecret python3 dashboard.py
"""

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

try:
    import requests
except Exception:       # dashboard still works without live prices
    requests = None

WEATHER_PATH = os.path.join("logs", "weather_state.json")
HOST = os.environ.get("DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8765"))
TOKEN = os.environ.get("DASH_TOKEN", "")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

CUR_ERA = "v7-obs"      # bets from the current model; everything else = legacy

try:
    import weather_shadow as _wsh
except Exception:
    _wsh = None

_SHADOW_CACHE = {"ts": 0.0, "data": None}


def _shadow_report():
    """Shadow calibration for /public: raw model vs market vs outcomes, plus
    the Brier-fit blend weight. Local CSV parse only; cached 10 min."""
    now = time.time()
    if now - _SHADOW_CACHE["ts"] > 600:
        _SHADOW_CACHE["ts"] = now
        try:
            _SHADOW_CACHE["data"] = _wsh.report_data() if _wsh else None
        except Exception:
            _SHADOW_CACHE["data"] = None
    return _SHADOW_CACHE["data"]


_PRICES = {"ts": 0.0, "by_ticker": {}}
_PRICES_LOCK = threading.Lock()
_WANT = {"tickers": []}   # open tickers a background thread keeps marks fresh for


def _price_loop():
    """Refresh marks OFF the request path so a slow Kalshi call never hangs a
    page load (the #1 cause of the dashboard looking 'down')."""
    while True:
        try:
            ts = list(_WANT["tickers"])
            if ts:
                fetch_prices(ts)
        except Exception:
            pass
        time.sleep(30)


def _safe_data():
    try:
        return build_data()
    except Exception as e:
        return {"running": False, "error": str(e)[:200]}


def _cents(mk, key):
    v = mk.get(key)
    if isinstance(v, (int, float)) and v > 0:
        return int(round(float(v)))
    v = mk.get(key + "_dollars")
    try:
        return int(round(float(v) * 100)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def fetch_prices(tickers):
    """Current yes_bid/yes_ask (cents) per ticker; cached 60s. Never raises."""
    if not tickers or requests is None:
        return {}
    with _PRICES_LOCK:
        fresh = (time.time() - _PRICES["ts"] < 60
                 and all(t in _PRICES["by_ticker"] for t in tickers))
        if fresh:
            return _PRICES["by_ticker"]
        out = dict(_PRICES["by_ticker"])
        try:
            for i in range(0, len(tickers), 40):
                batch = tickers[i:i + 40]
                d = requests.get(KALSHI + "/markets",
                                 params={"tickers": ",".join(batch), "limit": len(batch)},
                                 timeout=10).json()
                for mk in d.get("markets", []) or []:
                    out[mk.get("ticker", "")] = {
                        "yes_bid": _cents(mk, "yes_bid"),
                        "yes_ask": _cents(mk, "yes_ask")}
            _PRICES["ts"] = time.time()
            _PRICES["by_ticker"] = out
        except Exception:
            pass
        return out


def _era_stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0, "wins": 0, "losses": 0, "net": 0.0,
                "expectancy": None, "pred": None, "actual": None}
    wins = sum(1 for b in rows if b.get("outcome") == 1)
    net = sum(float(b.get("pnl", 0) or 0) for b in rows)
    pred = sum(float(b.get("pside", 0) or 0) for b in rows) / n
    return {"n": n, "wins": wins, "losses": n - wins, "net": round(net, 2),
            "expectancy": round(net / n, 2), "pred": round(100 * pred, 1),
            "actual": round(100 * wins / n, 1)}


def compute_kpis(out):
    """All book analytics, computed server-side from state + marks."""
    s = out.get("summary") or {}
    settled = out.get("settled") or []
    open_bets = out.get("open") or []
    start = float(s.get("start", 0) or 0)
    banked = float(s.get("total", 0) or 0)
    unreal = s.get("unrealized")
    k = {"window_n": len(settled)}

    pnls = [float(b.get("pnl", 0) or 0) for b in settled]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    k["profit_factor"] = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
    k["avg_win"] = round(sum(wins) / len(wins), 2) if wins else None
    k["avg_loss"] = round(sum(losses) / len(losses), 2) if losses else None
    k["expectancy"] = round(sum(pnls) / len(pnls), 2) if pnls else None
    k["best"] = round(max(pnls), 2) if pnls else None
    k["worst"] = round(min(pnls), 2) if pnls else None

    # max drawdown on the banked curve (window)
    peak, dd = 0.0, 0.0
    for v in out.get("curve") or []:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    k["max_dd"] = round(dd, 2)

    # returns
    k["return_pct"] = round(100 * banked / start, 1) if start else None
    if start and unreal is not None:
        k["marked_return_pct"] = round(100 * (banked + float(unreal)) / start, 1)
        k["nav"] = round(start + banked + float(unreal), 2)
    else:
        k["marked_return_pct"] = k["return_pct"]
        k["nav"] = round(start + banked, 2)

    # daily P&L (banked, window), newest last, cap 14 days
    daily = {}
    for b in settled:
        d = (b.get("ts") or "")[:10]
        if d:
            daily[d] = daily.get(d, 0.0) + float(b.get("pnl", 0) or 0)
    k["daily"] = [[d, round(v, 2)] for d, v in sorted(daily.items())][-14:]
    today = time.strftime("%Y-%m-%d", time.localtime())
    k["today"] = today
    k["today_pnl"] = round(sum(float(b.get("pnl", 0) or 0) for b in settled
                               if (b.get("ts") or "")[:10] == today), 2)

    # strategy eras
    cur = [b for b in settled if b.get("era") == CUR_ERA]
    legacy = [b for b in settled if b.get("era") != CUR_ERA]
    k["era_current"] = _era_stats(cur)
    k["era_legacy"] = _era_stats(legacy)
    k["era_current"]["open"] = sum(1 for b in open_bets if b.get("era") == CUR_ERA)

    # calibration buckets (window): predicted vs realized
    buckets = [(0.0, 0.30, "<30%"), (0.30, 0.50, "30-50%"),
               (0.50, 0.70, "50-70%"), (0.70, 1.01, ">70%")]
    cal = []
    for lo, hi, label in buckets:
        rows = [b for b in settled
                if b.get("outcome") in (0, 1) and lo <= float(b.get("pside", 0) or 0) < hi]
        if rows:
            pred = 100 * sum(float(b.get("pside", 0) or 0) for b in rows) / len(rows)
            act = 100 * sum(1 for b in rows if b.get("outcome") == 1) / len(rows)
            cal.append({"bucket": label, "n": len(rows),
                        "pred": round(pred), "act": round(act),
                        "delta": round(act - pred)})
        else:
            cal.append({"bucket": label, "n": 0, "pred": None, "act": None, "delta": None})
    k["calibration"] = cal

    # risk
    stakes = [(b.get("entry", 0) * b.get("count", 0) / 100.0, b) for b in open_bets]
    tot_stake = sum(x for x, _ in stakes)
    k["exposure"] = round(tot_stake, 2)
    nav = k["nav"] or (start + banked)
    k["exposure_pct"] = round(100 * tot_stake / nav, 1) if nav else None
    if stakes:
        mx, mb = max(stakes, key=lambda t: t[0])
        k["largest_pos"] = round(mx, 2)
        k["largest_pos_name"] = "%s %s%s" % (mb.get("city", ""), mb.get("strike", ""),
                                             " lo" if mb.get("hl") == "lo" else " hi")
    else:
        k["largest_pos"] = None
        k["largest_pos_name"] = ""

    # execution
    fees = float(s.get("fees", 0) or 0)
    k["fees"] = round(fees, 2)
    nset = int(s.get("settled", 0) or 0)
    k["fee_per_bet"] = round(fees / max(1, int(s.get("placed", 0) or 1)), 2)
    k["fee_drag_pct"] = round(100 * fees / start, 1) if start else None
    return k


def build_data():
    out = {"running": False, "updated": "", "summary": {}, "open": [], "settled": []}
    if os.path.exists(WEATHER_PATH):
        try:
            w = json.load(open(WEATHER_PATH))
            out["running"] = True
            out["updated"] = w.get("updated", "")
            out["summary"] = w.get("summary", {}) or {}
            out["open"] = w.get("open", []) or []
            out["settled"] = w.get("settled", []) or []
            out["depth"] = w.get("depth")
        except Exception:
            pass
    # live marks on open positions
    tickers = [b.get("ticker") for b in out["open"] if b.get("ticker")]
    _WANT["tickers"] = tickers
    prices = dict(_PRICES["by_ticker"])
    unreal, priced = 0.0, 0
    for b in out["open"]:
        px = prices.get(b.get("ticker") or "")
        if not px or not (px["yes_bid"] or px["yes_ask"]):
            b["now"] = None
            b["upnl"] = None
            continue
        mark = px["yes_bid"] if b.get("side") == "yes" else (100 - px["yes_ask"])
        mark = max(0, min(100, mark))
        b["now"] = mark
        b["value"] = round(mark * b.get("count", 0) / 100.0, 2)
        b["upnl"] = round((mark - b.get("entry", 0)) * b.get("count", 0) / 100.0, 2)
        unreal += b["upnl"]
        priced += 1
    if out["summary"] and priced:
        out["summary"]["unrealized"] = round(unreal, 2)
    # banked P&L curve, oldest -> newest
    curve, run = [], 0.0
    for b in reversed(out["settled"]):
        run += float(b.get("pnl", 0) or 0)
        curve.append(round(run, 2))
    out["curve"] = curve
    out["kpi"] = compute_kpis(out)
    out["shadow"] = _shadow_report()
    # weather step errors (written by weather_paper.step; absent = healthy)
    err_path = os.path.join("logs", "weather_err.txt")
    if os.path.exists(err_path):
        try:
            out["weather_err"] = open(err_path).read()[:1200]
        except Exception:
            pass
    # live trader state (real money), if present
    live_path = os.path.join("logs", "weather_live_state.json")
    if os.path.exists(live_path):
        try:
            lv = json.load(open(live_path))
            out["live"] = {"updated": lv.get("updated", ""),
                           "summary": lv.get("summary", {}) or {},
                           "balance_c": lv.get("balance_c")}
        except Exception:
            pass
    poly_path = os.path.join("logs", "poly_state.json")
    if os.path.exists(poly_path):
        try:
            out["poly"] = json.load(open(poly_path))
        except Exception:
            pass
    drift_path = os.path.join("logs", "drift_state.json")
    if os.path.exists(drift_path):
        try:
            out["drift"] = json.load(open(drift_path))
        except Exception:
            pass
    if out.get("drift"):
        # live marks on drift positions (same background price cache)
        dop = out["drift"].get("open") or []
        _WANT["tickers"] = (_WANT.get("tickers") or []) +             [b.get("ticker") for b in dop if b.get("ticker")]
        dprices = dict(_PRICES["by_ticker"])
        du, dpriced, dval = 0.0, 0, 0.0
        for b in dop:
            px = dprices.get(b.get("ticker") or "")
            if not px or not (px["yes_bid"] or px["yes_ask"]):
                b["now"] = None
                b["upnl"] = None
                dval += b.get("entry", 0) * b.get("count", 0) / 100.0
                continue
            mark = px["yes_bid"] if b.get("side") == "yes" else (100 - px["yes_ask"])
            mark = max(0, min(100, mark))
            b["now"] = mark
            b["value"] = round(mark * b.get("count", 0) / 100.0, 2)
            b["upnl"] = round((mark - b.get("entry", 0)) * b.get("count", 0) / 100.0, 2)
            du += b["upnl"]
            dval += b["value"]
            dpriced += 1
        dsum = out["drift"].get("summary")
        if isinstance(dsum, dict):
            if dpriced:
                dsum["unrealized"] = round(du, 2)
            dsum["marked_nav"] = round(float(dsum.get("cash") or 0) + dval, 2)
    sev_path = os.path.join("logs", "sharpev_state.json")
    if os.path.exists(sev_path):
        try:
            out["sharpev"] = json.load(open(sev_path))
        except Exception:
            pass
    return out


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Leonard the Bot</title>
<style>
:root{--bg:#0a0f1a;--panel:#0f1624;--panel2:#0c1220;--ink:#e6ecf7;--mut:#7d90ad;
--line:#1c2739;--grn:#2fd08c;--red:#f4695f;--amb:#e8b44c;--acc:#5b8def}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
font-variant-numeric:tabular-nums}
.wrap{max-width:1120px;margin:0 auto;padding:20px 18px 64px}
.hdr{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;border-bottom:1px solid var(--line);padding-bottom:12px}
.hdr h1{font-size:15px;letter-spacing:.14em;text-transform:uppercase;margin:0;font-weight:700}
.hdr .tag{font-size:10px;letter-spacing:.1em;color:var(--amb);border:1px solid var(--amb);
border-radius:4px;padding:1px 6px;text-transform:uppercase}
.hdr .upd{margin-left:auto;color:var(--mut);font-size:12px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--grn);margin-right:5px}
.dot.stale{background:var(--red)}
.live{color:var(--acc);font-size:12px}
.hero{display:flex;flex-wrap:wrap;gap:34px;align-items:flex-end;margin:20px 0 6px}
.nav .k{color:var(--mut);font-size:11px;letter-spacing:.12em;text-transform:uppercase}
.nav .v{font-size:42px;font-weight:800;letter-spacing:-1px;line-height:1.05}
.nav .d{font-size:13px;margin-top:2px}
.hmet{min-width:110px}
.hmet .k{color:var(--mut);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase}
.hmet .v{font-size:19px;font-weight:700;margin-top:2px}
.pos{color:var(--grn)}.neg{color:var(--red)}.mut{color:var(--mut)}
h2{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--mut);
margin:30px 0 10px;display:flex;align-items:center;gap:10px}
h2:after{content:"";flex:1;height:1px;background:var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:10px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile .k{color:var(--mut);font-size:10px;letter-spacing:.08em;text-transform:uppercase}
.tile .v{font-size:19px;font-weight:700;margin-top:3px}
.tile .s{color:var(--mut);font-size:11px;margin-top:1px}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:760px){.charts{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.panel .t{color:var(--mut);font-size:10px;letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px}
svg{display:block;width:100%}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);
border-radius:8px;overflow:hidden;font-size:13px}
th,td{text-align:left;padding:8px 11px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.07em}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right}
.chip{display:inline-block;padding:0 6px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.04em}
.chip.yes{background:rgba(47,208,140,.13);color:var(--grn)}
.chip.no{background:rgba(244,105,95,.13);color:var(--red)}
.chip.era{background:rgba(91,141,239,.13);color:var(--acc)}
.chip.leg{background:rgba(125,144,173,.13);color:var(--mut)}
.won{color:var(--grn);font-weight:700}.lost{color:var(--red);font-weight:700}
.empty{color:var(--mut);text-align:center;padding:16px}
.mkt{font-weight:600}
.foot{color:var(--mut);font-size:11.5px;margin-top:36px;border-top:1px solid var(--line);padding-top:12px;line-height:1.6}
.eras{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:700px){.eras{grid-template-columns:1fr}}
.eras table{font-size:12.5px}
</style></head><body><div class=wrap>
<div class=hdr>
  <h1>Leonard the Bot &middot; Paper</h1><span class=tag>4 strategies</span>
  <span class=live id=live></span>
  <span class=upd id=upd><span class=dot id=dot></span>loading&hellip;</span>
</div>
<div id=combined style="margin:14px 0 2px;"></div>
<h2>Strategy portfolio <span style="text-transform:none;letter-spacing:0">(4 uncorrelated paper books)</span></h2>
<div id=strat style="display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:12px;"></div>
<h2>Weather book <span style="text-transform:none;letter-spacing:0">(forecast edge &mdash; calibration-gated)</span></h2>
<div class=hero>
  <div class=nav><div class=k>Marked equity (NAV)</div><div class=v id=nav>&ndash;</div>
    <div class=d id=navd></div></div>
  <div class=hmet><div class=k>Banked P&amp;L</div><div class=v id=banked>&ndash;</div></div>
  <div class=hmet><div class=k>Today's P&amp;L</div><div class=v id=today>&ndash;</div></div>
  <div class=hmet><div class=k>Unrealized</div><div class=v id=unrl>&ndash;</div></div>
  <div class=hmet><div class=k>Cash</div><div class=v id=cash>&ndash;</div></div>
  <div class=hmet><div class=k>At stake</div><div class=v id=stake>&ndash;</div></div>
</div>
<h2>Performance</h2><div class=grid id=perf></div>
<h2>Risk &amp; Execution</h2><div class=grid id=risk></div>
<h2>Book history</h2>
<div class=charts>
  <div class=panel><div class=t>Banked equity curve <span id=curven></span></div><svg id=eq viewBox="0 0 520 150" preserveAspectRatio=none></svg></div>
  <div class=panel><div class=t>Daily banked P&amp;L</div><svg id=daily viewBox="0 0 520 150" preserveAspectRatio=none></svg></div>
</div>
<h2>Strategy attribution</h2>
<div class=eras>
  <div class=panel><div class=t>Current model &middot; v7 obs-nowcast (calibration-gated)</div><table><tbody id=eracur></tbody></table></div>
  <div class=panel><div class=t>Legacy (v2&ndash;v6 &mdash; wrong-day forecasts)</div><table><tbody id=eraleg></tbody></table></div>
</div>
<h2>Model calibration <span style="text-transform:none;letter-spacing:0">(predicted vs realized win rate &mdash; the go-live gate &middot; sub-50% buckets RETIRED 7/18, shadow-only)</span></h2>
<table><thead><tr><th>Confidence bucket</th><th class=num>Bets</th><th class=num>Predicted</th>
<th class=num>Realized</th><th class=num>Gap</th></tr></thead><tbody id=calib></tbody></table>
<h2>Market-price calibration <span style="text-transform:none;letter-spacing:0">(what the market said vs what happened &mdash; the drift + salvage evidence)</span></h2>
<table><thead><tr><th>Market price</th><th class=num>Markets</th><th class=num>Mkt implied</th>
<th class=num>Actually won</th><th class=num>Bias</th></tr></thead><tbody id=mktcal></tbody></table>
<h2>Open positions (marked to market)</h2>
<table><thead><tr><th>Market</th><th>Side</th><th>Model</th><th class=num>Our prob</th>
<th class=num>Entry</th><th class=num>Mark</th><th class=num>Qty</th>
<th class=num>Cost</th><th class=num>Fee</th><th class=num>Value</th><th class=num>Unrl P&amp;L</th></tr></thead>
<tbody id=open></tbody></table>
<h2>Settled (15 most recent &mdash; current model) <span style="text-transform:none;letter-spacing:0" id=legnote></span></h2>
<table><thead><tr><th>Market</th><th>Side</th><th>Model</th><th class=num>Our prob</th>
<th class=num>Entry</th><th class=num>Qty</th><th class=num>Fee</th><th>Result</th><th class=num>P&amp;L</th></tr></thead>
<tbody id=settled></tbody></table>
<h2>Polymarket reward farming <span style="text-transform:none;letter-spacing:0">(paper &mdash; modeled, reinvesting)</span></h2>
<div class=grid id=poly></div>
<div style="margin-top:10px"><table><thead><tr><th>Date</th><th>Market / activity</th>
<th class=num>Alloc</th><th class=num>Net</th><th class=num>Bank after</th></tr></thead>
<tbody id=polytbl></tbody></table></div>
<h2>Momentum drift <span style="text-transform:none;letter-spacing:0">(paper &mdash; buy the climbing favorite at maker, no model, ride to settlement)</span></h2>
<div class=grid id=drift></div>
<div style="margin-top:10px"><table><thead><tr><th>Market</th><th>Side</th><th class=num>Mkt prob</th>
<th class=num>From&rarr;At</th><th class=num>Entry</th><th class=num>Now</th><th class=num>Qty</th><th>Result</th><th class=num>P&amp;L</th></tr></thead>
<tbody id=drifttbl></tbody></table></div>
<h2>Sharp +EV sports <span style="text-transform:none;letter-spacing:0">(paper &mdash; sharp-book fair value vs Kalshi price, maker-only, gated)</span></h2>
<div class=grid id=sev></div>
<h2>Sharp strategy attribution</h2>
<div class=eras>
  <div class=panel><div class=t>Current strategy &middot; v3 thin-band <span style="text-transform:none;letter-spacing:0">(1.5&ndash;2&cent; edges only, stale-odds gated &mdash; from Jul 13)</span></div><table><tbody id=sevcur></tbody></table></div>
  <div class=panel><div class=t>Fade book &middot; fade1 <span style="text-transform:none;letter-spacing:0">(bet WITH Kalshi against 2&ndash;5&cent; "edges" &mdash; promoted Jul 18, own gate)</span></div><table><tbody id=sevfade></tbody></table></div>
  <div class=panel><div class=t>Legacy &middot; v1 wide-edge <span style="text-transform:none;letter-spacing:0">(big "edges" were toxic &mdash; retired Jul 13)</span></div><table><tbody id=sevleg></tbody></table></div>
</div>
<div style="margin-top:10px"><table><thead><tr><th>Start</th><th>Game</th><th>Our team</th>
<th class=num>Fair</th><th class=num>Entry</th><th class=num>Edge</th><th class=num>Qty</th>
<th>Result</th><th class=num>P&amp;L</th></tr></thead><tbody id=sevtbl></tbody></table></div>
<div class=foot id=foot></div>
</div>
<script>
const $=id=>document.getElementById(id);
const F=x=>'$'+Number(x||0).toFixed(2);
const M=x=>{const n=Number(x||0);return (n>=0?'+':'-')+'$'+Math.abs(n).toFixed(2);};
const C=x=>Number(x||0)>=0?'pos':'neg';
const NA='<span class=mut>&ndash;</span>';
const feeC=f=>(f==null)?NA:(Number(f).toFixed(0)+'&cent;');
function mkt(b){const kk=b.kind||'ge';
  const st=(kk==='band')?(b.strike+'&ndash;'+(b.cap!=null?b.cap:'?')+'&deg;'):((kk==='le')?'&le;'+b.strike+'&deg;':'&ge;'+b.strike+'&deg;');
  return '<td><span class=mkt>'+(b.city||'')+' '+st+' '+((b.hl==='lo')?'low':'high')+'</span></td>';}
function side(s){s=(s||'').toLowerCase();return '<td><span class="chip '+(s==='yes'?'yes':'no')+'">'+s.toUpperCase()+'</span></td>';}
function era(b){const cur=(b.era==='v7-obs');
  return '<td><span class="chip '+(cur?'era':'leg')+'">'+(cur?'v7':'legacy')+'</span></td>';}
function prob(p){return '<td class=num>'+Math.round((Number(p)||0)*100)+'%</td>';}
function tile(k,v,s){return '<div class=tile><div class=k>'+k+'</div><div class=v>'+v+'</div>'+(s?'<div class=s>'+s+'</div>':'')+'</div>';}
function stratCard(name,kind,kindcls,bank,pnl,sub,status,statuscls){
  return '<div style="background:var(--panel);border:.5px solid var(--line);border-radius:12px;padding:14px 16px;">'
    +'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">'
    +'<span style="font-weight:500;font-size:13.5px;">'+name+'</span>'
    +'<span class="chip '+kindcls+'">'+kind+'</span></div>'
    +'<div style="font-size:25px;font-weight:700;margin-top:8px;letter-spacing:-.5px;">'+bank+'</div>'
    +'<div style="font-size:12px;margin-top:2px;">'+pnl+' <span class=mut>'+sub+'</span></div>'
    +'<div style="margin-top:9px;"><span class="chip '+statuscls+'">'+status+'</span></div></div>';
}
function drawCurve(el,curve){
  if(!curve||curve.length<2){el.innerHTML='';return;}
  const W=520,H=150,pl=34,pr=8,pt=10,pb=16;
  const mn=Math.min(0,...curve),mx=Math.max(0,...curve),rng=(mx-mn)||1;
  const X=i=>pl+(W-pl-pr)*i/(curve.length-1);
  const Y=v=>pt+(H-pt-pb)*(1-(v-mn)/rng);
  let g='';
  [mx,0,mn].forEach(v=>{const y=Y(v).toFixed(1);
    g+='<line x1="'+pl+'" y1="'+y+'" x2="'+(W-pr)+'" y2="'+y+'" stroke="#1c2739" stroke-width="1"/>'
      +'<text x="'+(pl-5)+'" y="'+(+y+3.5)+'" fill="#7d90ad" font-size="9" text-anchor="end">'+v.toFixed(0)+'</text>';});
  const pts=curve.map((v,i)=>X(i).toFixed(1)+','+Y(v).toFixed(1)).join(' ');
  const col=curve[curve.length-1]>=0?'#2fd08c':'#f4695f';
  g+='<polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="1.8"/>';
  const lx=X(curve.length-1),ly=Y(curve[curve.length-1]);
  g+='<circle cx="'+lx.toFixed(1)+'" cy="'+ly.toFixed(1)+'" r="2.6" fill="'+col+'"/>';
  el.innerHTML=g;
}
function drawDaily(el,daily){
  if(!daily||!daily.length){el.innerHTML='';return;}
  const W=520,H=150,pl=34,pr=8,pt=10,pb=22;
  const vals=daily.map(d=>d[1]);
  const mx=Math.max(1e-9,...vals.map(Math.abs));
  const zero=pt+(H-pt-pb)/2, half=(H-pt-pb)/2;
  const bw=Math.min(34,(W-pl-pr)/daily.length-6);
  let g='<line x1="'+pl+'" y1="'+zero+'" x2="'+(W-pr)+'" y2="'+zero+'" stroke="#1c2739"/>';
  daily.forEach((d,i)=>{
    const x=pl+(W-pl-pr)*(i+.5)/daily.length-bw/2;
    const h=half*Math.abs(d[1])/mx;
    const y=d[1]>=0?zero-h:zero;
    const col=d[1]>=0?'#2fd08c':'#f4695f';
    const lab=(d[1]>=0?'+':'\u2212')+Math.abs(d[1]).toFixed(2);
    let ly=d[1]>=0?y-4:zero+h+9;
    if(d[1]>=0&&ly<pt+7)ly=y+9; if(d[1]<0&&ly>H-pb+7)ly=H-pb+7;
    g+='<rect x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+Math.max(1,h).toFixed(1)+'" rx="2" fill="'+col+'" opacity=".85"/>'
      +'<text x="'+(x+bw/2).toFixed(1)+'" y="'+ly.toFixed(1)+'" fill="'+col+'" font-size="8" text-anchor="middle">'+lab+'</text>'
      +'<text x="'+(x+bw/2).toFixed(1)+'" y="'+(H-8)+'" fill="#7d90ad" font-size="8.5" text-anchor="middle">'+d[0].slice(5)+'</text>';});
  el.innerHTML=g;
}
function actRows(st,legs,unit){
  if(!st)return '';
  const rows=[];
  const day=st.last_date||'today';
  (legs||[]).forEach(p=>rows.push('<tr><td class=mut>'+day+'</td><td><span class=mkt>'+p.name+'</span> <span class="chip yes">OPEN</span></td>'
    +'<td class=num>'+F(p.alloc)+'</td><td class=num><span class="'+C(p.net)+'">'+M(p.net)+'</span><span class=mut>/day</span></td><td class=num>&ndash;</td></tr>'));
  const H=(st.history||[]).slice(-15).reverse();
  H.forEach(h=>{const n=(h.markets!=null?h.markets:h.assets)||0;
    rows.push('<tr><td class=mut>'+(h.ts||'').slice(0,10)+'</td><td class=mut>daily accrual &middot; '+n+' '+unit+'</td>'
    +'<td class=num>&ndash;</td><td class=num><span class="'+C(h.net)+'">'+M(h.net)+'</span></td><td class=num>'+F(h.cash)+'</td></tr>');});
  return rows.slice(0,15+(legs||[]).length).join('');
}
function eraRows(e){
  const rows=[['Settled bets',e.n+(e.open!=null?'  ('+e.open+' open)':'')],
   ['Record',e.n?e.wins+'W / '+e.losses+'L':'&ndash;'],
   ['Net P&L',e.n?'<span class="'+C(e.net)+'">'+M(e.net)+'</span>':'&ndash;'],
   ['Expectancy / bet',e.expectancy!=null?'<span class="'+C(e.expectancy)+'">'+M(e.expectancy)+'</span>':'&ndash;'],
   ['Predicted vs realized',e.pred!=null?e.pred+'% vs '+e.actual+'%':'&ndash;']];
  return rows.map(r=>'<tr><td class=mut>'+r[0]+'</td><td class=num>'+r[1]+'</td></tr>').join('');
}
async function load(){
  const tk=new URLSearchParams(location.search).get('token')||'';
  let d;try{d=await(await fetch('/data?token='+encodeURIComponent(tk),{cache:'no-store'})).json();}
  catch(e){$('upd').textContent='cannot reach bot';return;}
  if(d.auth===false){ // stale/missing token: fall back to the tokenless public feed
    try{d=await(await fetch('/public',{cache:'no-store'})).json();}
    catch(e){$('upd').textContent='cannot reach bot';return;}
  }
  if(d.auth===false){$('upd').textContent='bad token';return;}
  if(!d.running){$('upd').textContent='waiting for first state...';return;}
  const s=d.summary||{},k=d.kpi||{};
  {
    const wStart=Number(s.start||0);
    const wNav=(k.nav!=null?Number(k.nav):(wStart+Number(s.total||0)));
    const wSettled=Number((k.era_current&&k.era_current.n)||0);
    const P=d.poly||null, E=d.sharpev||null, DR=d.drift||null;
    const drSum=DR?(DR.summary||{}):{};
    const drOpenStake=DR?(DR.open||[]).reduce((a,b)=>a+(b.entry||0)*(b.count||0)/100,0):0;
    const drBank=DR?(drSum.marked_nav!=null?Number(drSum.marked_nav):(Number(drSum.cash||0)+drOpenStake)):null, drStart=DR?Number(drSum.start||0):0;
    const eSum=E?(E.summary||{}):{};
    const eOpenStake=E?(E.open||[]).reduce((a,b)=>a+(b.entry||0)*(b.count||0)/100,0):0;
    const eBank=E?(Number(eSum.cash||0)+eOpenStake):null, eStart=E?Number(eSum.start||0):0;
    const pBank=P?Number(P.cash||P.start||0):null, pStart=P?Number(P.start||0):0;
    const cards=[
      stratCard('Weather edge &middot; v7-obs','forecast','era',F(wNav),
        '<span class="'+C((k.era_current||{}).net)+'">'+M((k.era_current||{}).net||0)+'</span>',
        '&middot; v7: '+((k.era_current||{}).wins||0)+'W/'+((k.era_current||{}).losses||0)+'L'
        +((k.era_current||{}).expectancy!=null?' &middot; '+M((k.era_current||{}).expectancy)+'/bet':'')
        +' <span class=mut>(bank incl. legacy '+M((k.era_legacy||{}).net||0)+')</span>',
        wSettled>=30?'v7 gate: n\u226530 met':'v7 probing '+wSettled+'/30','leg'),
      stratCard('Polymarket rewards','liquidity','yes',
        P?F(pBank):NA, P?'<span class="'+C(P.earned)+'">'+M(P.earned)+'</span>':NA,
        P?('&middot; '+(P.days||0)+'d &middot; APY ~'+(P.apy_annualized!=null?P.apy_annualized:'&ndash;')+'%'):'&middot; starting',
        'reinvesting','era'),
      stratCard('Momentum drift &middot; drift1','momentum','yes',
        DR?F(drBank):NA,
        DR?'<span class="'+C(drBank-drStart)+'">'+M(drBank-drStart)+'</span>':NA,
        DR?('&middot; '+(drSum.wins||0)+'W/'+(drSum.losses||0)+'L &middot; '+(drSum.open||0)+' open &middot; buy strength, no model'):'&middot; starting',
        DR?((drSum.gate==='scale'?'gate: passed':'probing '+(drSum.gate_n||0)+'/30')):'starting','leg'),
      stratCard('Sharp +EV &middot; v3 band','anchor','era',
        E?F(eBank):NA,
        E?'<span class="'+C((E.era_current||{}).net)+'">'+M((E.era_current||{}).net||0)+'</span>':NA,
        E?('&middot; v3: '+((E.era_current||{}).wins||0)+'W/'+((E.era_current||{}).losses||0)+'L'
          +((E.era_current||{}).expectancy!=null?' &middot; '+M((E.era_current||{}).expectancy)+'/bet':'')
          +' <span class=mut>(bank incl. legacy '+M((E.era_legacy||{}).net||0)+')</span>'):'&middot; starting',
        E?((eSum.gate==='scale'?'v3 gate: passed':'v3 probing '+(eSum.gate_n||0)+'/30')):'starting','leg'),
    ];
    $('strat').innerHTML=cards.join('');
    let tStart=wStart, tNav=wNav, nb=1;
    if(P){tStart+=pStart;tNav+=pBank;nb++;} if(DR){tStart+=drStart;tNav+=drBank;nb++;}
    if(E){tStart+=eStart;tNav+=eBank;nb++;}
    $('combined').innerHTML='<div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;">'
      +'<span style="font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.12em;">Combined paper NAV</span>'
      +'<span style="font-size:33px;font-weight:800;letter-spacing:-1px;">'+F(tNav)+'</span>'
      +'<span class="'+C(tNav-tStart)+'" style="font-size:15px;">'+M(tNav-tStart)+'</span>'
      +'<span class=mut style="font-size:12px;">across '+nb+' paper strategies &middot; started '+F(tStart)+'</span></div>';
  }
  const ageMin=(Date.now()-new Date(d.updated).getTime())/60000;
  $('upd').innerHTML='<span class="dot'+(ageMin>30?' stale':'')+'"></span>'
    +(ageMin>30?'STALE &middot; ':'')+'updated '+(d.updated?d.updated.replace('T',' ').slice(0,16):'-');
  if(d.live&&d.live.summary){const L=d.live.summary;
    $('live').textContent=(L.mode||'LIVE')+' '+M(L.net||0)+' ('+(L.wins||0)+'W/'+(L.losses||0)+'L)'
      +(L.resting!=null?' \u00b7 '+L.resting+' resting':'')
      +(L.day_pnl!=null?' \u00b7 day '+M(L.day_pnl):'')
      +(L.halted?' \u00b7 HALTED':'')
      +(d.live.balance_c!=null?' \u00b7 bal $'+(d.live.balance_c/100).toFixed(2):'');}
  const start=Number(s.start||0),banked=Number(s.total||0);
  const unrl=(s.unrealized==null)?null:Number(s.unrealized);
  const nav=k.nav!=null?k.nav:start+banked;
  $('nav').textContent=F(nav);
  const chg=nav-start;
  $('navd').innerHTML='<span class="'+C(chg)+'">'+M(chg)+' ('+(start?((100*chg/start).toFixed(1)):'0')+'%)</span> <span class=mut>vs $'+start.toFixed(0)+' inception</span>';
  $('banked').innerHTML='<span class="'+C(banked)+'">'+M(banked)+'</span>';
  $('today').innerHTML=(k.today_pnl==null)?NA:'<span class="'+C(k.today_pnl)+'">'+M(k.today_pnl)+'</span>';
  $('unrl').innerHTML=unrl==null?NA:'<span class="'+C(unrl)+'">'+M(unrl)+'</span>';
  $('cash').textContent=F(s.cash);
  $('stake').textContent=F(s.open_exposure);
  const wr=(s.settled||0)>0?s.win_rate+'%':null;
  $('perf').innerHTML=[
    tile('Return (banked)',k.return_pct!=null?'<span class="'+C(k.return_pct)+'">'+k.return_pct+'%</span>':NA,'marked '+(k.marked_return_pct!=null?k.marked_return_pct+'%':'&ndash;')),
    tile('Win rate',wr||NA,(s.wins||0)+'W / '+(s.losses||0)+'L all-time'),
    tile('Profit factor',k.profit_factor!=null?k.profit_factor:NA,'gross win / gross loss'),
    tile('Expectancy',k.expectancy!=null?'<span class="'+C(k.expectancy)+'">'+M(k.expectancy)+'</span>':NA,'per settled bet'),
    tile('Avg win',k.avg_win!=null?'<span class=pos>'+M(k.avg_win)+'</span>':NA,''),
    tile('Avg loss',k.avg_loss!=null?'<span class=neg>'+M(k.avg_loss)+'</span>':NA,''),
    tile('Best / worst',(k.best!=null?M(k.best):'&ndash;')+' <span class=mut>/</span> '+(k.worst!=null?M(k.worst):'&ndash;'),'single bet'),
    tile('Max drawdown',k.max_dd!=null?'<span class=neg>-$'+Number(k.max_dd).toFixed(2)+'</span>':NA,'banked curve'),
  ].join('');
  $('risk').innerHTML=[
    tile('Exposure',F(k.exposure),k.exposure_pct!=null?k.exposure_pct+'% of NAV':''),
    tile('Open positions',(s.open_bets||0),''),
    tile('Largest position',k.largest_pos!=null?F(k.largest_pos):NA,k.largest_pos_name||''),
    tile('Total placed',(s.placed||0),'since inception'),
    tile('Fees paid',F(k.fees),k.fee_drag_pct!=null?k.fee_drag_pct+'% of inception NAV':''),
    tile('Fee / bet placed',k.fee_per_bet!=null?F(k.fee_per_bet):NA,''),
    (d.depth?tile('Depth at our entries','$'+Number(d.depth.fill_total||0).toFixed(0),
      (d.depth.edges||0)+' edges \u00b7 med $'+Number(d.depth.fill_med||0).toFixed(0)+'/strike at touch'):''),
    (d.depth?tile('Touch liquidity scanned','$'+Number(d.depth.touch_total||0).toFixed(0),
      (d.depth.n_mkts||0)+' strikes incl. bands \u00b7 measured, not modeled'):''),
  ].join('');
  $('curven').textContent='(last '+(k.window_n||0)+' settled)';
  drawCurve($('eq'),d.curve);
  drawDaily($('daily'),k.daily);
  $('eracur').innerHTML=eraRows(k.era_current||{});
  $('eraleg').innerHTML=eraRows(k.era_legacy||{});
  $('mktcal').innerHTML=((d.shadow&&d.shadow.mkt_buckets)||[]).map(c=>{
    const bias=(c.actual!=null&&c.mkt!=null)?Math.round((c.actual-c.mkt)*10)/10:null;
    return '<tr><td>'+c.bucket+'\u00a2</td><td class=num>'+c.n+'</td>'
      +'<td class=num>'+c.mkt+'%</td><td class=num>'+c.actual+'%</td>'
      +'<td class=num>'+(bias==null?'&ndash;':'<span class="'+(bias>=0?'pos':'neg')+'">'+(bias>0?'+':'')+bias+' pts</span>')+'</td></tr>';
  }).join('')||'<tr><td colspan=5 class=empty>Accumulating shadow outcomes&hellip;</td></tr>';
  $('calib').innerHTML=(k.calibration||[]).map(c=>{
    const ok=c.delta==null?null:Math.abs(c.delta)<=10;
    return '<tr><td>'+c.bucket+'</td><td class=num>'+c.n+'</td>'
      +'<td class=num>'+(c.pred!=null?c.pred+'%':'&ndash;')+'</td>'
      +'<td class=num>'+(c.act!=null?c.act+'%':'&ndash;')+'</td>'
      +'<td class=num>'+(c.delta==null?'&ndash;':'<span class="'+(ok?'pos':'neg')+'">'+(c.delta>0?'+':'')+c.delta+' pts</span>')+'</td></tr>';
  }).join('');
  $('open').innerHTML=(d.open||[]).map(b=>{
    const h=(b.now!=null);
    return '<tr>'+mkt(b)+side(b.side)+era(b)+prob(b.pside)
    +'<td class=num>'+b.entry+'&cent;</td>'
    +'<td class=num>'+(h?b.now+'&cent;':'&ndash;')+'</td>'
    +'<td class=num>'+b.count+'</td>'
    +'<td class=num>'+F(b.entry*b.count/100)+'</td>'
    +'<td class=num>'+feeC(b.fee)+'</td>'
    +'<td class=num>'+(h?F(b.value):'&ndash;')+'</td>'
    +'<td class=num>'+(h?'<span class="'+C(b.upnl)+'">'+M(b.upnl)+'</span>':'&ndash;')+'</td></tr>';
  }).join('')||'<tr><td colspan=11 class=empty>No open positions &mdash; waiting for a disciplined edge.</td></tr>';
  {
    const all=d.settled||[];
    const cur=all.filter(b=>b.era==='v7-obs');
    const nleg=all.length-cur.length;
    $('legnote').textContent=nleg>0?'('+nleg+' older legacy-model bets hidden \u2014 still counted in totals)':'';
    $('settled').innerHTML=cur.slice(0,15).map(b=>{
      const won=Number(b.outcome)===1;
      return '<tr>'+mkt(b)+side(b.side)+era(b)+prob(b.pside)
      +'<td class=num>'+b.entry+'&cent;</td><td class=num>'+b.count+'</td>'
      +'<td class=num>'+feeC(b.fee)+'</td>'
      +'<td>'+(b.exited?('<span class=chip style="background:rgba(232,180,76,.13);color:var(--amb)">'+(b.salvaged?'SALV':'EXIT')+'</span>'):'<span class="'+(won?'won':'lost')+'">'+(won?'WON':'LOST')+'</span>')+'</td>'
      +'<td class=num><span class="'+C(b.pnl)+'">'+M(b.pnl)+'</span></td></tr>';
    }).join('')||'<tr><td colspan=9 class=empty>No current-model bets settled yet \u2014 the open v6-ens positions settle daily.</td></tr>';
  }
  if(d.poly){const P=d.poly;const H=P.history||[];const last=H.length?H[H.length-1]:null;
    $('poly').innerHTML=[
      tile('Bank (paper)',F(P.cash||P.start||0),'started '+F(P.start||0)),
      tile('Rewards earned','<span class=pos>'+M(P.earned||0)+'</span>',(P.days||0)+' paper days'),
      tile('Annualized (modeled)','~'+(P.apy_annualized!=null?P.apy_annualized:'&ndash;')+'%','net, reinvested'),
      tile('Last-day reward',last?M(last.net):NA,last?(last.markets+' markets')+'':''),
      tile('Reinvest','ON','rewards &rarr; more liquidity'),
    ].join('');
  } else { $('poly').innerHTML='<div class=tile><div class=k>Polymarket</div><div class=v>&ndash;</div><div class=s>paper sim starting&hellip;</div></div>'; }
  $('polytbl').innerHTML=actRows(d.poly,(d.poly&&d.poly.positions||[]).map(p=>({name:p.q,alloc:p.alloc,net:p.net})),'markets')
    ||'<tr><td colspan=5 class=empty>No activity yet \u2014 allocations post once per day.</td></tr>';
  if(d.drift){const D=d.drift,dsm=D.summary||{};
    $('drift').innerHTML=[
      tile('Bank (paper)',F(dsm.cash||0),'started '+F(dsm.start||0)),
      tile('Record',(dsm.wins||0)+'W / '+(dsm.losses||0)+'L',(dsm.open||0)+' open'),
      tile('Realized P&L',(dsm.realized!=null)?'<span class="'+C(dsm.realized)+'">'+M(dsm.realized)+'</span>':NA,''),
      tile('Unrealized (marked)',(dsm.unrealized!=null)?'<span class="'+C(dsm.unrealized)+'">'+M(dsm.unrealized)+'</span>':NA,
        (dsm.marked_nav!=null)?('marked NAV '+F(dsm.marked_nav)):''),
      tile('Gate',(dsm.gate||'probe')+' '+(dsm.gate_n||0)+'/30','pside = market prob \u2192 gate measures the drift premium'),
      tile('Trigger','\u226565\u00a2 & climbing','maker join \u00b7 1/event \u00b7 no exits'),
    ].join('');
    const dr=[];
    (D.open||[]).forEach(b=>dr.push('<tr>'+mkt(b)+side(b.side)
      +'<td class=num>'+Math.round((b.pside||0)*100)+'%</td>'
      +'<td class=num>'+(b.from_mid!=null?Math.round(b.from_mid):'\u2013')+'\u2192'+(b.at_mid!=null?Math.round(b.at_mid):'\u2013')+'\u00a2</td>'
      +'<td class=num>'+b.entry+'&cent;</td>'
      +'<td class=num>'+(b.now!=null?b.now+'&cent;':'&ndash;')+'</td>'
      +'<td class=num>'+b.count+'</td>'
      +'<td><span class=chip style="background:rgba(91,141,239,.13);color:var(--acc)">OPEN</span></td>'
      +'<td class=num>'+(b.upnl!=null?('<span class="'+C(b.upnl)+'">'+M(b.upnl)+'</span>'):'&ndash;')+'</td></tr>'));
    (D.settled||[]).slice(0,10).forEach(b=>{const won=Number(b.outcome)===1;
      dr.push('<tr>'+mkt(b)+side(b.side)
      +'<td class=num>'+Math.round((b.pside||0)*100)+'%</td><td class=num>&ndash;</td>'
      +'<td class=num>'+b.entry+'&cent;</td><td class=num>&ndash;</td><td class=num>'+b.count+'</td>'
      +'<td><span class="'+(won?'won':'lost')+'">'+(won?'WON':'LOST')+'</span></td>'
      +'<td class=num><span class="'+C(b.pnl)+'">'+M(b.pnl)+'</span></td></tr>');});
    $('drifttbl').innerHTML=dr.join('')||'<tr><td colspan=9 class=empty>Waiting for a climbing favorite \u2014 needs two scans of the same market to see momentum.</td></tr>';
  } else { $('drift').innerHTML='<div class=tile><div class=k>Momentum drift</div><div class=v>&ndash;</div><div class=s>starting&hellip;</div></div>';
    $('drifttbl').innerHTML='<tr><td colspan=9 class=empty>No state yet.</td></tr>'; }
  if(d.sharpev){const S=d.sharpev,ss=S.summary||{};
    $('sev').innerHTML=[
      tile('Bank (paper)',F(ss.cash),'started '+F(ss.start)),
      tile('Realized P&L','<span class="'+C(ss.realized)+'">'+M(ss.realized)+'</span>',(ss.wins||0)+'W / '+(ss.losses||0)+'L'),
      tile('Open bets',(ss.open_bets||0),(ss.pending||0)+' resting \u00b7 '+(ss.canceled||0)+' expired'),
      tile('Calibration gate',(ss.gate||'probe')+' '+(ss.gate_n||0)+'/30','v3 bets only \u00b7 sizing is earned'),
      tile('v3 expectancy / bet',(S.era_current&&S.era_current.expectancy!=null)?'<span class="'+C(S.era_current.expectancy)+'">'+M(S.era_current.expectancy)+'</span>':NA,
        (S.era_current&&S.era_current.pred!=null)?('pred '+S.era_current.pred+'% vs act '+S.era_current.actual+'%'):'no v3 settles yet'),
      tile('Fade expectancy / bet',(S.era_fade&&S.era_fade.expectancy!=null)?'<span class="'+C(S.era_fade.expectancy)+'">'+M(S.era_fade.expectancy)+'</span>':NA,
        (S.era_fade&&S.era_fade.n)?(S.era_fade.n+' settled \u00b7 '+(S.era_fade.open||0)+' open'):'no fade settles yet'),
      tile('Placed',(ss.placed||0),(S.last_scan&&S.last_scan.credits!=null)?(S.last_scan.credits+' odds credits left'):'since inception'),
      tile('Last scan',(S.last_scan&&S.last_scan.ts)?S.last_scan.ts.replace('T',' ').slice(5,16):'\u2013',
        (S.last_scan&&S.last_scan.ts)?((S.last_scan.evaluated||0)+' edges eval \u00b7 best '
          +(S.last_scan.best_edge!=null?S.last_scan.best_edge+'\u00a2':'\u2013')
          +(S.last_scan.bar!=null?' vs '+S.last_scan.bar+(S.last_scan.ceil!=null?'\u2013'+S.last_scan.ceil:'')+'\u00a2 band':'')):'no scan yet'),
    ].join('');
    $('sevcur').innerHTML=eraRows(S.era_current||{});
    const FD=S.era_fade||{};
    $('sevfade').innerHTML=eraRows(FD)+(FD.gate!=null?('<tr><td class=mut>Gate</td><td class=num>'+FD.gate+' '+(FD.gate_n||0)+'/30</td></tr>'):'');
    $('sevleg').innerHTML=eraRows(S.era_legacy||{});
    const sevEra=b=>b.era_v?(b.era_v==='ev3-band'?' <span class="chip era" style="margin-left:5px">v3 new</span>':(b.era_v==='fade1'?' <span class="chip" style="margin-left:5px;background:rgba(180,120,230,.15);color:#b478e6">fade</span>':' <span class="chip leg" style="margin-left:5px">v1 old</span>')):'';
    const rows=[];
    (S.pending||[]).forEach(b=>rows.push('<tr><td class=mut>'+((b.start||'').slice(5,16).replace('T',' '))+'</td><td><span class=mkt>'+(b.game||'')+'</span></td><td>'+(b.team||'')+sevEra(b)+'</td>'
      +'<td class=num>'+Math.round((b.pside||0)*100)+'%</td><td class=num>'+b.entry+'&cent;</td><td class=num>+'+(b.edge||0)+'&cent;</td><td class=num>'+b.count+'</td>'
      +'<td><span class=chip style="background:rgba(230,180,60,.13);color:#e6b43c">RESTING</span></td><td class=num>&ndash;</td></tr>'));
    (S.open||[]).forEach(b=>rows.push('<tr><td class=mut>'+((b.start||'').slice(5,16).replace('T',' '))+'</td><td><span class=mkt>'+(b.game||'')+'</span></td><td>'+(b.team||'')+sevEra(b)+'</td>'
      +'<td class=num>'+Math.round((b.pside||0)*100)+'%</td><td class=num>'+b.entry+'&cent;</td><td class=num>+'+(b.edge||0)+'&cent;</td><td class=num>'+b.count+'</td>'
      +'<td><span class=chip style="background:rgba(91,141,239,.13);color:var(--acc)">OPEN</span></td><td class=num>&ndash;</td></tr>'));
    (S.settled||[]).slice(0,10).forEach(b=>{const won=Number(b.outcome)===1;
      rows.push('<tr><td class=mut>'+((b.ts||'').slice(5,16).replace('T',' '))+'</td><td><span class=mkt>'+(b.game||'')+'</span></td><td>'+(b.team||'')+sevEra(b)+'</td>'
      +'<td class=num>'+Math.round((b.pside||0)*100)+'%</td><td class=num>'+b.entry+'&cent;</td><td class=num>+'+(b.edge||0)+'&cent;</td><td class=num>'+b.count+'</td>'
      +'<td><span class="'+(won?'won':'lost')+'">'+(won?'WON':'LOST')+'</span></td><td class=num><span class="'+C(b.pnl)+'">'+M(b.pnl)+'</span></td></tr>');});
    if(S.shadow&&S.shadow.n){const bs=(S.shadow.buckets||[]).map(b=>b.edge+'\u00a2: n='+b.n+' act '+b.act+'% (fair '+b.fair+'%) EV '+(b.ev_c>0?'+':'')+b.ev_c+'\u00a2').join(' \u00b7 ');
      rows.push('<tr><td colspan=9 class=mut>Shadow anchor calibration \u2014 '+S.shadow.n+' settled edges: '+bs+'</td></tr>');}
    $('sevtbl').innerHTML=rows.join('')||'<tr><td colspan=9 class=empty>No qualifying edges yet'+((S.last_scan&&S.last_scan.ts)?' \u2014 scanning; Kalshi is tracking the sharp books inside the edge bar.':' \u2014 waiting for first scan.')+'</td></tr>';
  } else { $('sev').innerHTML='<div class=tile><div class=k>Sharp +EV</div><div class=v>&ndash;</div><div class=s>starting&hellip;</div></div>';
    $('sevtbl').innerHTML='<tr><td colspan=9 class=empty>No state yet.</td></tr>'; }
  $('foot').innerHTML='Paper account &mdash; no real money at risk. NAV = cash + open positions at current market bid (marks refresh ~60s). '
    +'Banked P&amp;L = settled bets only; positions are held to settlement. Performance and calibration KPIs computed on the last '
    +(k.window_n||0)+' settled bets; win rate and totals are all-time. Judge the edge on the v7 era only &mdash; legacy bets predate the current model. Auto-refreshes every 20s.';
}
load();setInterval(load,20000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/public"):
            body = json.dumps(_safe_data()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/data"):
            if TOKEN:
                from urllib.parse import urlparse, parse_qs
                given = parse_qs(urlparse(self.path).query).get("token", [""])[0]
                if given != TOKEN:
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"auth":false}')
                    return
            body = json.dumps(_safe_data()).encode()
            ctype = "application/json"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    url = f"http://127.0.0.1:{PORT}"
    try:
        srv = ThreadingHTTPServer((HOST, PORT), H)
    except OSError as e:
        print(f"Could not start dashboard on {url}: {e}")
        print("If it says 'address already in use', a dashboard is already")
        print("running - just open the address in your browser.")
        return 1
    shown = url + (f"/?token={TOKEN}" if TOKEN else "")
    print(f"Dashboard running at {shown}")
    if HOST == "127.0.0.1":
        print("Opening your browser... (keep this window open; Ctrl+C to stop)")
        threading.Timer(1.0, lambda: webbrowser.open(shown)).start()
    else:
        print("(Public mode - open the address above from any device.)")
    threading.Thread(target=_price_loop, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
