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

CUR_ERA = "v9-core"     # thresholds-only >=70% conf; everything else = legacy

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
                        "yes_ask": _cents(mk, "yes_ask"),
                        "last": _cents(mk, "last_price")}
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


def _freshest_balance_c(dv, cv):
    """8/6 (Adam: "the NAV on the tracker and on kalshi need to line up"):
    both live books snapshot the SAME account balance, but at different
    moments - and with the crypto book cycling every 3 min vs the drift
    book's 10, the hero NAV was gluing a stale dlive cash snapshot to
    fresh position marks. Every crypto fill in the gap window showed up
    twice (cash not yet debited + position counted), drifting the tracker
    off the Kalshi app by dollars around busy hours. Use whichever book's
    balance snapshot is newer."""
    db, cb = dv.get("balance_c"), cv.get("balance_c")
    if cb is not None and (cv.get("updated") or "") > (dv.get("updated") or ""):
        return cb
    return db


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
    # 8/10: the legacy paper-book KPI block retired - it has served
    # nothing but nulls since the paper books were archived 7/30. The
    # key stays (empty) so old JS null-guards keep working.
    out["kpi"] = {}
    out["shadow"] = _shadow_report()
    # weather step errors (written by weather_paper.step; absent = healthy)
    err_path = os.path.join("logs", "weather_err.txt")
    if os.path.exists(err_path):
        try:
            out["weather_err"] = open(err_path).read()[:1200]
        except Exception:
            pass
    # live trader state (real money when armed), if present
    # weather-live DRY rehearsal retired 7/30 with the paper books - its
    # stale state must not feed the header strip anymore
    for key, fname in (("dlive", "drift_live_state.json"),):
        lpath = os.path.join("logs", fname)
        if not os.path.exists(lpath):
            continue
        try:
            lv = json.load(open(lpath))
            out[key] = {"updated": lv.get("updated", ""),
                        "summary": lv.get("summary", {}) or {},
                        # 8/11 tie-out fix: the account NAV adds
                        # dv.recv_c, but this whitelist dropped it - the
                        # weather settlement receivable was invisible to
                        # the hero number (per-book NAV was fine)
                        "recv_c": lv.get("recv_c"),
                        "balance_c": lv.get("balance_c")}
        except Exception:
            continue
        if key != "dlive":
            continue
        # REAL-MONEY section detail: THE MIRROR - Kalshi's own positions and
        # resting orders (written verbatim by the executor each cycle) are
        # what renders; the bot's internal book is only a fallback.
        # 8/10 reconcile fix: a mirror row the book has ALREADY SETTLED
        # is cash in flight, not a position - its dollars live in recv_c
        # until the exchange credits them. Valuing it here double-counted
        # every just-settled winner for the few minutes Kalshi keeps
        # listing it after settlement.
        _wdone = {h.get("tk") for h in (lv.get("history") or [])}
        _wbets = lv.get("bets") or {}
        dop = ([dict(b) for b in (lv.get("k_positions") or [])
                if not ((b.get("ticker") in _wdone)
                        and (b.get("ticker") not in _wbets))]
               or [dict(b, ticker=tk) for tk, b in _wbets.items()])
        rest = ([dict(o) for o in (lv.get("k_resting") or [])]
                or [dict(o) for o in (lv.get("pending") or {}).values()])
        _WANT["tickers"] = (_WANT.get("tickers") or []) +             [b.get("ticker") for b in dop if b.get("ticker")]
        rp = dict(_PRICES["by_ticker"])
        du, dval = 0.0, 0.0
        for b in dop:
            px = rp.get(b.get("ticker") or "")
            if not px or not (px["yes_bid"] or px["yes_ask"] or px.get("last")):
                b["now"] = None
                b["upnl"] = None
                dval += b.get("entry", 0) * b.get("count", 0) / 100.0
                continue
            # Kalshi-app parity (Adam 7/30: dashboard NAV $103.22 vs the
            # app's Portfolio $104.86, cash identical to the penny): the
            # app values positions at LAST TRADED price; we marked at the
            # liquidation bid, undershooting by ~the spread on every
            # position. Mark at last (fallback mid, then bid) so the
            # tracker shows the app's number.
            last = px.get("last") or 0
            yb_, ya_ = px["yes_bid"], px["yes_ask"]
            if last > 0:
                ym = last
            elif yb_ and ya_:
                ym = (yb_ + ya_) / 2.0
            elif yb_:
                ym = yb_
            else:
                # ask-only book (close-to-settlement limbo): no honest
                # mark exists - hold at cost, show a dash (8/3 fix)
                b["now"] = None
                b["upnl"] = None
                dval += b.get("entry", 0) * b.get("count", 0) / 100.0
                continue
            mark = ym if b.get("side") == "yes" else (100 - ym)
            mark = int(round(max(0, min(100, mark))))
            b["now"] = mark
            b["value"] = round(mark * b.get("count", 0) / 100.0, 2)
            b["upnl"] = round((mark - b.get("entry", 0)) * b.get("count", 0) / 100.0, 2)
            du += b["upnl"]
            dval += b["value"]
        out[key]["open"] = dop
        out[key]["resting"] = rest
        out[key]["unrealized"] = round(du, 2)
        out[key]["history"] = list(reversed((lv.get("history") or [])[-15:]))
        out[key]["nickel"] = lv.get("nickel")
        if lv.get("balance_c") is not None:
            # Kalshi's balance still includes cash committed to resting buy
            # orders (verified live 7/23: bal $100.09 with $59.81 resting),
            # so NAV = balance + FILLED position value only.
            # + settlement receivable (8/10): wins we detected whose
            # cash credit hasn't landed yet - closes the NAV dip
            out[key]["marked_nav"] = round(lv["balance_c"] / 100.0 + dval
                                           + float(lv.get("recv_c") or 0)
                                           / 100.0, 2)
            # THE scoreboard (Adam 7/23: "kalshi should always be our source
            # of proof"): true P&L = Kalshi-derived NAV minus net deposits.
            baseline = float(os.environ.get("DRIFT_LIVE_BASELINE_D", "100.09"))
            out[key]["baseline"] = baseline
            out[key]["pnl_true"] = round(out[key]["marked_nav"] - baseline, 2)
    # 7/30: the paper books are retired, so the LIVE book owns the page
    # heartbeat now - never gate the REAL MONEY panel on a retired ledger
    if out.get("dlive"):
        out["running"] = True
        if not out.get("updated"):
            out["updated"] = out["dlive"].get("updated", "")
    # poly reward-farming book RETIRED 7/23 (ledger archived, not deleted)
    # drift1 paper book retired 7/25; driftw2-fin retired 7/30
    # driftc = LANE 2 AUDITION (7/31): crypto drift paper book, gate 100
    # driftc paper audition retired 8/3 (gate passed -> live book)
    # SPORTS PAPER BOOK (8/12) RETIRED 8/20 (Adam: "please shut down
    # this paperbook"). It reached 4 settled turns of a 200-turn gate in
    # 8 days: 2W/2L, -$3.04, offer side 0 lifted. The taker thesis
    # (Polymarket-anchored edge) never got a sample worth grading, and
    # the phantom book now reads the same sports surface from the maker
    # side at 100x the rate. Payload block removed so a dead panel can't
    # masquerade as a live book. State file preserved on the server;
    # lessons in SPORTS_AUTOPSY.md. To revive: restore this block +
    # PAPER_SPORTS=1 + un-hide #sportswrap.
    # MID-BAND PAPER BOOK RETIRED 8/19 (Adam: "cut things that are a
    # distraction"). The book stopped trading 8/14: it filled all 12
    # slots and then froze - no fair value to tell it when a thesis had
    # broken, so nothing could exit and the turn counter stuck at 4.
    # NOT a failed thesis: all 4 completed turns WON (+$2.18, +$0.55 per
    # turn, the buy-at-20-sell-at-40 trade). The lane comes back with a
    # brain when the weather nowcast passes its shadow exam; the frozen
    # state is filed as MIDBAND_AUTOPSY.md. State file preserved on the
    # server. To revive: restore this block + PAPER_MIDBAND=1.
    # CULTURE SCANNER (8/19): phase-0 telemetry block
    try:
        _cu = os.path.join("logs", "culture_state.json")
        if os.path.exists(_cu):
            out["culture"] = json.load(open(_cu))
    except Exception:
        pass
    # PHANTOM BOOK (8/20) RETIRED 8/25 - payload block removed so a dead
    # book cannot masquerade as a live one on the tracker (the same rule
    # applied to the sports book on 8/20). Final tape: match rate 9%,
    # spread -2.45c/pair after fees, adverse -5.1c, -$44.09 over 483
    # settles. State file preserved on the server; verdict in
    # PHANTOM_AUTOPSY.md. To revive: restore this block, un-hide
    # #phwrap, and set PAPER_PHANTOM=1.
    # TICK BOOK (8/25): 15-minute commodity windows, paper. The raw
    # proxy tape (thousands of price ticks) stays on the server; the
    # tracker gets the model, the calibration table and the verdict.
    try:
        _tk = os.path.join("logs", "tick_state.json")
        if os.path.exists(_tk):
            _td = json.load(open(_tk))
            _td.pop("ticks", None)
            _td.pop("fills", None)
            _td.pop("pos", None)
            _td.pop("proxy_err", None)
            out["tick"] = _td
    except Exception:
        pass
    for key, fname in (("clive", "crypto_live_state.json"),):
        fpath = os.path.join("logs", fname)
        if os.path.exists(fpath):
            try:
                out[key] = json.load(open(fpath))
            except Exception:
                pass
        if not out.get(key):
            continue
        # live marks on this book's positions (same background price cache)
        # 8/7 KALSHI-TRUTH VALUATION for the crypto book (Adam: recurring
        # tracker-vs-app NAV gaps): the internal book settles a position
        # the instant the outcome is knowable, but the EXCHANGE keeps
        # listing it until the payout posts minutes later. Valuing the
        # internal list left every just-settled position counted nowhere
        # (not a position, cash not yet credited) - a hole that opens
        # EVERY HOUR now that hourlies trade. So: value and render
        # Kalshi's own position list (the mirror), merged with internal
        # rows for entry/name context; internal list is only a fallback
        # when the mirror has not been written yet.
        _internal = {(b.get("ticker") or ""): b
                     for b in (out[key].get("open") or [])}
        _kp = out[key].get("k_positions")
        # 8/10 reconcile fix (found live: two just-settled winners showed
        # as entry-0 "positions" worth $5.55 WHILE their $6.00 payout also
        # sat in recv_c - a transient double count every settlement hour).
        # A mirror row the book already settled is cash in flight, not a
        # position: skip it; recv_c carries its value until the credit.
        _cdone = {h.get("tk") for h in (out[key].get("history") or [])}
        if _kp:
            dop = []
            for p in _kp:
                _tk = p.get("ticker") or ""
                if _tk in _cdone and _tk not in _internal:
                    continue
                row = dict(_internal.get(_tk,
                                         {"name": p.get("ticker", ""),
                                          "entry": 0, "ots": ""}))
                row.update({"ticker": p.get("ticker"),
                            "side": p.get("side"),
                            "count": p.get("count", 0)})
                dop.append(row)
            out[key]["open"] = dop
        else:
            dop = out[key].get("open") or []
        _WANT["tickers"] = (_WANT.get("tickers") or []) +             [b.get("ticker") for b in dop if b.get("ticker")]
        dprices = dict(_PRICES["by_ticker"])
        du, dpriced, dval = 0.0, 0, 0.0
        for b in dop:
            px = dprices.get(b.get("ticker") or "")
            # 8/3 phantom-loss fix (Adam's SOL screenshot: NO marked 0c,
            # -$0.92, then WON 2 min later): between close and settlement
            # a book often holds only a vestigial 100c ask - marking off
            # a one-sided ask zeroed the NO side. Honest mark = last
            # trade, else two-sided mid, else bid; ask-only/empty books
            # get NO mark (held at cost, shown as dash) until settlement.
            last = (px or {}).get("last") or 0
            yb_ = (px or {}).get("yes_bid") or 0
            ya_ = (px or {}).get("yes_ask") or 0
            if last > 0:
                ym = last
            elif yb_ and ya_:
                ym = (yb_ + ya_) / 2.0
            elif yb_:
                ym = yb_
            else:
                ym = None
            if ym is None:
                b["now"] = None
                b["upnl"] = None
                dval += b.get("entry", 0) * b.get("count", 0) / 100.0
                continue
            mark = ym if b.get("side") == "yes" else (100 - ym)
            mark = int(round(max(0, min(100, mark))))
            b["now"] = mark
            b["value"] = round(mark * b.get("count", 0) / 100.0, 2)
            b["upnl"] = round((mark - b.get("entry", 0)) * b.get("count", 0) / 100.0, 2)
            du += b["upnl"]
            dval += b["value"]
            dpriced += 1
        dsum = out[key].get("summary")
        if isinstance(dsum, dict):
            if dpriced:
                dsum["unrealized"] = round(du, 2)
            dsum["marked_nav"] = round(float(dsum.get("cash") or 0) + dval
                                       + float(out[key].get("recv_c") or 0)
                                       / 100.0, 2)
    # 8/3 TWO LIVE BOOKS: the hero is the ACCOUNT - balance + weather
    # positions + crypto positions, all marked. dlive's own block only
    # counted its (now weather-fenced) universe.
    try:
        dv, cv = out.get("dlive") or {}, out.get("clive") or {}
        if dv.get("balance_c") is not None:
            def _val(rows):
                return sum((b.get("value") if b.get("value") is not None
                            else (b.get("entry", 0) or 0)
                            * (b.get("count", 0) or 0) / 100.0)
                           for b in rows or [])
            acct = round(_freshest_balance_c(dv, cv) / 100.0
                         + _val(dv.get("open"))
                         + _val(cv.get("open"))
                         + (float(dv.get("recv_c") or 0)
                            + float(cv.get("recv_c") or 0)) / 100.0, 2)
            dv["marked_nav"] = acct
            dv["pnl_true"] = round(acct - float(dv.get("baseline") or 100.09), 2)
            # 8/3 P&L ATTRIBUTION (Adam: total AND by strategy): crypto
            # book P&L = its realized + marked unrealized; weather = the
            # remainder of the account total (exact by construction, the
            # two always sum to the hero number)
            c_pnl = float((cv.get("summary") or {}).get("realized") or 0)
            c_pnl += sum((b.get("upnl") or 0) for b in (cv.get("open") or []))
            dv["pnl_crypto"] = round(c_pnl, 2)
            dv["pnl_weather"] = round(dv["pnl_true"] - c_pnl, 2)
            # 8/14 CASH-IN TRUTH. Deposits are the only honest anchor.
            # realized_true is derived from the NAV identity
            #   NAV = deposits + realized + unrealized
            # which reconciles exactly (cash + open value = NAV), so it
            # cannot drift the way the settlement ledger has.
            try:
                _dep = float((dv.get("summary") or {}).get("deposits") or 0)
                _unreal = float(dv.get("unrealized") or 0)
                # 8/14: when live marks are missing, `unrealized` falls back
                # to 0.0 - which is INDISTINGUISHABLE from "genuinely flat".
                # Splitting realized from unrealized on a silent zero would
                # overstate realized_true by exactly the unmarked P&L, so
                # when any open position is unmarked we publish the flag and
                # withhold the split rather than print a confident wrong
                # number. (roi_on_cash is safe either way: NAV at cost basis
                # is conservative, not fabricated.)
                _open = dv.get("open") or []
                _stale = any(b.get("now") is None for b in _open)
                if _dep > 0:
                    dv["deposits"] = _dep
                    dv["roi_on_cash"] = round((acct - _dep) / _dep * 100.0, 2)
                    dv["marks_stale"] = bool(_stale)
                    dv["realized_true"] = (None if _stale
                                           else round(acct - _dep - _unreal, 2))
                    # k_realized has been provably wrong (said -81 while
                    # NAV proved +21). Never render it as truth again
                    # without this flag beside it.
                    _kr = (dv.get("summary") or {}).get("k_realized")
                    if _kr is not None and dv["realized_true"] is not None:
                        dv["k_realized_gap"] = round(
                            float(_kr) - dv["realized_true"], 2)
                        dv["k_realized_suspect"] = bool(
                            abs(dv["k_realized_gap"]) > 5.0)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    except Exception:
        pass
    # 8/3 PERFORMANCE BREAKOUT (Adam: weekly & monthly % by strategy):
    # built from each book's persistent daily-P&L ledger (never trimmed).
    # Closed periods are realized-only; the CURRENT period is everything
    # else (open positions marked live + any ledger drift), so each
    # column telescopes EXACTLY to the account scoreboard. Return % is
    # on period-start NAV (baseline + all prior periods), compounding.
    try:
        import datetime as _dt
        dv = out.get("dlive") or {}
        if dv.get("pnl_true") is not None:
            def _days(fname):
                try:
                    d = json.load(open(os.path.join("logs", fname)))
                    return d.get("pnl_days") or {}
                except Exception:
                    return {}
            wx_d = _days("drift_live_state.json")
            cr_d = _days("crypto_live_state.json")
            base = float(dv.get("baseline") or 100.09)
            today_s = _dt.date.today().isoformat()
            def _periods(kind):
                buckets, order = {}, []
                alldays = sorted(set(list(wx_d) + list(cr_d) + [today_s]))
                for ds in alldays:
                    try:
                        d = _dt.date.fromisoformat(ds)
                    except ValueError:
                        continue
                    if kind == "w":
                        mon = d - _dt.timedelta(days=d.weekday())
                        k = mon.isoformat()
                        lab = (mon.strftime("%b %-d") + " – "
                               + (mon + _dt.timedelta(days=6)).strftime("%b %-d"))
                    else:
                        k = ds[:7]
                        lab = d.strftime("%B %Y")
                    if k not in buckets:
                        buckets[k] = {"wx": 0.0, "cr": 0.0, "label": lab}
                        order.append(k)
                    buckets[k]["wx"] += float(wx_d.get(ds) or 0)
                    buckets[k]["cr"] += float(cr_d.get(ds) or 0)
                if not order:
                    return []
                cw = sum(buckets[k]["wx"] for k in order[:-1])
                cc = sum(buckets[k]["cr"] for k in order[:-1])
                buckets[order[-1]]["wx"] = float(dv.get("pnl_weather") or 0) - cw
                buckets[order[-1]]["cr"] = float(dv.get("pnl_crypto") or 0) - cc
                res, nav0 = [], base
                for i, k in enumerate(order):
                    b = buckets[k]
                    tot = b["wx"] + b["cr"]
                    res.append({"label": b["label"],
                                "live": i == len(order) - 1,
                                "wx": round(b["wx"], 2),
                                "cr": round(b["cr"], 2),
                                "tot": round(tot, 2),
                                "wx_pct": round(b["wx"] / nav0 * 100, 2) if nav0 else None,
                                "cr_pct": round(b["cr"] / nav0 * 100, 2) if nav0 else None,
                                "tot_pct": round(tot / nav0 * 100, 2) if nav0 else None,
                                "nav1": round(nav0 + tot, 2)})
                    nav0 += tot
                return res
            dv["perf"] = {"weekly": _periods("w"), "monthly": _periods("m")}
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
  <h1>Leonard the Bot</h1><span class=tag>LIVE</span>
  <span class=live id=live></span>
  <span class=upd id=upd><span class=dot id=dot></span>loading&hellip;</span>
</div>
<div id=rmwrap style="display:none;border:1.5px solid var(--amb);border-radius:12px;padding:16px 18px;margin:18px 0 4px;background:linear-gradient(180deg,rgba(232,180,76,.05),transparent)">
<h2 style="margin:0 0 12px">Real money &middot; THE scoreboard <span id=rmmode style="text-transform:none;letter-spacing:0"></span></h2>
<div class=hero style="margin:4px 0 12px">
  <div class=nav><div class=k>Account NAV &middot; Kalshi is the source of truth</div><div class=v id=rmpnl>&ndash;</div>
    <div class=d id=rmpnld></div></div>
</div>
<div class=grid id=rmtiles></div>
<div id=perfwrap style="display:none;margin-top:16px;border-top:1px solid rgba(125,144,173,.25);padding-top:14px">
<h2 style="margin:0 0 10px">Performance &middot; returns by period <span style="text-transform:none;letter-spacing:0;font-size:11px;color:var(--mut)">&middot; % on period-start NAV &middot; open period marked live</span></h2>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px">
<div><div class=t style="margin-bottom:6px">Weekly</div>
<table><thead><tr><th>Week</th><th class=num>Weather</th><th class=num>Crypto</th><th class=num>Total</th><th class=num>NAV end</th></tr></thead>
<tbody id=perfw></tbody></table></div>
<div><div class=t style="margin-bottom:6px">Monthly</div>
<table><thead><tr><th>Month</th><th class=num>Weather</th><th class=num>Crypto</th><th class=num>Total</th><th class=num>NAV end</th></tr></thead>
<tbody id=perfm></tbody></table></div>
</div></div>
<h2 style="margin:18px 0 10px;border-top:1px solid rgba(125,144,173,.25);padding-top:14px">Book 1 &middot; Weather <span style="text-transform:none;letter-spacing:0">(era dlive1 &middot; two-sided book: taker entries + dip bids + 97/99 offers &middot; 100% of NAV)</span></h2>
<div class=grid id=wxtiles></div>
<details style="margin-top:10px"><summary style="cursor:pointer;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.12em">Diagnostics &amp; capital routing</summary>
<div class=grid id=wxdiag style="margin-top:10px"></div>
<div style="margin-top:12px"><div class=t style="margin-bottom:6px">Bucket routing (trigger &times; entry band &middot; negative at n&ge;8 auto-blocked)</div>
<table><thead><tr><th>Bucket</th><th class=num>N</th><th class=num>Record</th><th class=num>Net</th><th>Routing</th></tr></thead>
<tbody id=rmbuckets></tbody></table></div>
</details>
<div style="margin-top:12px"><div class=t style="margin-bottom:6px">Weather positions &amp; resting orders (marked live)</div>
<table><thead><tr><th>Market</th><th>Side</th><th>Status</th><th class=num>Mkt prob</th>
<th class=num>Entry</th><th class=num>Now</th><th class=num>Qty</th><th class=num>Value</th><th class=num>uP&amp;L</th></tr></thead>
<tbody id=rmopen></tbody></table></div>
<div style="margin-top:12px"><div class=t style="margin-bottom:6px">Weather results (settled &amp; exits)</div>
<table><thead><tr><th>Closed</th><th>Market</th><th>Side</th><th class=num>Entry</th>
<th class=num>Exit/Settle</th><th class=num>Qty</th><th>Result</th><th class=num>P&amp;L</th></tr></thead>
<tbody id=rmreal></tbody></table></div>
</div>
<div id=clivewrap style="display:none;border:1.5px solid rgba(96,165,250,.55);border-radius:12px;padding:14px 18px;margin:14px 0 4px;background:linear-gradient(180deg,rgba(96,165,250,.05),transparent)">
<h2 style="margin:0 0 10px">Book 2 &middot; Crypto drift <span id=clmode style="text-transform:none;letter-spacing:0"></span></h2>
<div class=grid id=cltiles></div>
<div style="margin-top:12px"><div class=t style="margin-bottom:6px">Crypto positions (marked live)</div>
<table><thead><tr><th>Market</th><th>Side</th><th class=num>Mkt prob</th>
<th class=num>Entry</th><th class=num>Now</th><th class=num>Qty</th><th class=num>Value</th><th class=num>uP&amp;L</th></tr></thead>
<tbody id=cltbl></tbody></table></div>
<div id=clrealwrap style="margin-top:12px;display:none"><div class=t style="margin-bottom:6px">Crypto results (latest)</div>
<table><thead><tr><th>Closed</th><th>Market</th><th>Side</th><th class=num>Entry</th>
<th class=num>Exit/Settle</th><th class=num>Qty</th><th>Result</th><th class=num>P&amp;L</th></tr></thead>
<tbody id=clreal></tbody></table></div>
</div>
<!-- Book 3 (sports, taker) panel retired 8/20 (Adam: "please shut
     down this paperbook"). 4/200 gate in 8 days, 2W/2L, -$3.04,
     0 lifted. The phantom book reads the same surface from the
     maker side. Revive: restore this panel + the payload block +
     PAPER_SPORTS=1. Lessons: SPORTS_AUTOPSY.md -->
<!-- Book 4 (mid-band) panel retired 8/19 - see payload note -->
<!-- Book 4 (phantom) panel RETIRED 8/25 (Adam: "please totally get
     rid of this book from the bot and the tracker"). Died on its own
     three KPIs: match rate 9% by contract (1,608 of 1,772 filled
     contracts never paired), spread -2.45c/pair AFTER fees (gross
     3.24c vs 5.68c of fee - the thesis inverted), adverse -5.1c.
     -$44.09 over 483 settles. Ledger preserved on the server;
     verdict in PHANTOM_AUTOPSY.md. Revive: restore this panel + the
     payload block + PAPER_PHANTOM=1. -->
<div id=tkwrap style="display:none;border:1.5px solid rgba(56,189,248,.45);border-radius:12px;padding:14px 18px;margin:14px 0 4px;background:linear-gradient(180deg,rgba(56,189,248,.05),transparent)">
<h2 style="margin:0 0 10px">Book 5 &middot; Tick <span id=tkmode style="text-transform:none;letter-spacing:0"></span></h2>
<div class=grid id=tktiles></div>
<div style="margin-top:12px"><div class=t style="margin-bottom:6px">Live 15-minute windows &mdash; the model vs the book</div>
<table><thead><tr><th>Market</th><th class=num>Strike</th><th class=num>Spot (Pyth)</th>
<th class=num>Distance</th><th class=num>Time left</th><th class=num>Book</th>
<th class=num>Model</th><th class=num>Edge</th><th>Quoting</th></tr></thead>
<tbody id=tkwin></tbody></table></div>
<div style="margin-top:12px"><div class=t style="margin-bottom:6px">Calibration &mdash; THE deliverable. When the model says 90%, do 90% of them win?</div>
<table><thead><tr><th>Model said</th><th class=num>Windows</th><th class=num>Actually won</th><th>Verdict</th></tr></thead>
<tbody id=tkcal></tbody></table>
<div class=mut style="font-size:11px;margin-top:6px">A model that is right about its own confidence is a business; one that is overconfident is a slow way to lose. Nothing here trades real money &mdash; this table decides whether anything ever does.</div></div>
<div style="margin-top:12px"><div class=t style="margin-bottom:6px">Settled windows</div>
<table><thead><tr><th>Closed</th><th>Market</th><th>Lane</th><th>Side</th>
<th class=num>Paid</th><th class=num>Model said</th><th>Result</th><th class=num>P&amp;L</th></tr></thead>
<tbody id=tkset></tbody></table></div>
</div>
<!-- PAPER SECTIONS RETIRED 7/30 (Adam: 'get rid of the other two paper
     strategies for now') - hidden, not deleted; ledgers archived on the
     server as *_retired.json. Set display:block + revive the books in
     paper.py (PAPER_WX_RETIRED=0 / PAPER_DRIFTW_RETIRED=0) to bring back. -->
<div id=paperwrap style="display:none">
<div id=combined style="margin:14px 0 2px;"></div>
<h2>Paper R&amp;D books <span style="text-transform:none;letter-spacing:0">(simulations &mdash; the proving ground for future live books, NOT the scoreboard &middot; paper fills are optimistic: instant at our price, no adverse selection)</span></h2>
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
  <div class=panel><div class=t>Current model &middot; v9 core <span style="text-transform:none;letter-spacing:0">(thresholds only, &ge;70% conf &mdash; the proven bucket)</span></div><table><tbody id=eracur></tbody></table></div>
  <div class=panel><div class=t>Legacy <span style="text-transform:none;letter-spacing:0">(v2&ndash;v8 &mdash; incl. the band experiment)</span></div><table><tbody id=eraleg></tbody></table></div>
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
<h2>Momentum drift &middot; WIDE <span style="text-transform:none;letter-spacing:0">(paper &mdash; certainty rules on commodities &amp; financial-close markets &middot; era driftw2-fin)</span></h2>
<div class=grid id=driftw></div>
<div style="margin-top:10px"><div class=t style="margin-bottom:6px">Open positions (marked live)</div>
<table><thead><tr><th>Market</th><th>Side</th><th class=num>Mkt prob</th>
<th class=num>Trigger</th><th class=num>Entry</th><th class=num>Now</th><th class=num>Qty</th><th class=num>Fee</th><th class=num>Value</th><th class=num>uP&amp;L</th></tr></thead>
<tbody id=driftwtbl></tbody></table></div>
<div style="margin-top:14px"><div class=t style="margin-bottom:6px">Realized trades (settled &amp; stopped)</div>
<table><thead><tr><th>Closed</th><th>Market</th><th>Side</th><th class=num>Mkt prob</th>
<th class=num>Entry</th><th class=num>Exit/Settle</th><th class=num>Qty</th><th class=num>Fee</th><th>Result</th><th class=num>P&amp;L</th></tr></thead>
<tbody id=driftwreal></tbody></table></div>
<div class=foot id=foot></div>
</div><!-- /paperwrap -->
</div>
<script>
const $=id=>document.getElementById(id);
const F=x=>'$'+Number(x||0).toFixed(2);
const M=x=>{const n=Number(x||0);return (n>=0?'+':'-')+'$'+Math.abs(n).toFixed(2);};
const C=x=>Number(x||0)>=0?'pos':'neg';
const NA='<span class=mut>&ndash;</span>';
const P=x=>{const n=Number(x||0);return (n>=0?'+':'-')+Math.abs(n).toFixed(2)+'%';};
const feeC=f=>(f==null)?NA:(Number(f).toFixed(0)+'&cent;');
function mkt(b){const kk=b.kind||'ge';
  const st=(kk==='band')?(b.strike+'&ndash;'+(b.cap!=null?b.cap:'?')+'&deg;'):((kk==='le')?'&le;'+b.strike+'&deg;':'&ge;'+b.strike+'&deg;');
  return '<td><span class=mkt>'+(b.city||'')+' '+st+' '+((b.hl==='lo')?'low':'high')+'</span></td>';}
function side(s){s=(s||'').toLowerCase();return '<td><span class="chip '+(s==='yes'?'yes':'no')+'">'+s.toUpperCase()+'</span></td>';}
function era(b){const cur=(b.era==='v9-core');
  return '<td><span class="chip '+(cur?'era':'leg')+'">'+(cur?'v9':'legacy')+'</span></td>';}
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
    const DW=d.driftw||null;
    const bookNav=B=>{if(!B)return null;const s=B.summary||{};
      const stake=(B.open||[]).reduce((a,b)=>a+(b.entry||0)*(b.count||0)/100,0);
      return s.marked_nav!=null?Number(s.marked_nav):(Number(s.cash||0)+stake);};
    const dwSum=DW?(DW.summary||{}):{};
    const dwBank=bookNav(DW), dwStart=DW?Number(dwSum.start||0):0;
    const cards=[
      stratCard('Weather edge &middot; v9-core','forecast','era',F(wNav),
        '<span class="'+C((k.era_current||{}).net)+'">'+M((k.era_current||{}).net||0)+'</span>',
        '&middot; v9: '+((k.era_current||{}).wins||0)+'W/'+((k.era_current||{}).losses||0)+'L'
        +((k.era_current||{}).expectancy!=null?' &middot; '+M((k.era_current||{}).expectancy)+'/bet':'')
        +' <span class=mut>(bank incl. legacy '+M((k.era_legacy||{}).net||0)+')</span>',
        wSettled>=30?'v9 gate: n\u226530 met':'v9 probing '+wSettled+'/30','leg'),
      stratCard('Drift WIDE &middot; driftw2-fin','momentum','yes',
        DW?F(dwBank):NA,
        DW?'<span class="'+C(dwBank-dwStart)+'">'+M(dwBank-dwStart)+'</span>':NA,
        DW?('&middot; '+(dwSum.wins||0)+'W/'+(dwSum.losses||0)+'L &middot; '+(dwSum.open||0)+' open &middot; commodities + financials'):'&middot; starting',
        DW?((dwSum.gate==='scale'?'gate: passed':'probing '+(dwSum.gate_n||0)+'/30')):'starting','leg'),
    ];
    $('strat').innerHTML=cards.join('');
    let tStart=wStart, tNav=wNav, nb=1;
    if(DW){tStart+=dwStart;tNav+=dwBank;nb++;}
    $('combined').innerHTML='<div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;">'
      +'<span style="font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.12em;">Combined paper NAV</span>'
      +'<span style="font-size:33px;font-weight:800;letter-spacing:-1px;">'+F(tNav)+'</span>'
      +'<span class="'+C(tNav-tStart)+'" style="font-size:15px;">'+M(tNav-tStart)+'</span>'
      +'<span class=mut style="font-size:12px;">across '+nb+' paper strategies &middot; started '+F(tStart)+'</span></div>';
  }
  const ageMin=(Date.now()-new Date(d.updated).getTime())/60000;
  $('upd').innerHTML='<span class="dot'+(ageMin>30?' stale':'')+'"></span>'
    +(ageMin>30?'STALE &middot; ':'')+'updated '+(d.updated?d.updated.replace('T',' ').slice(0,16):'-');
  if(d.dlive){const L=d.dlive,S=L.summary||{};
    $('rmwrap').style.display='block';
    const isLive=(S.mode==='LIVE');
    $('rmmode').innerHTML='<span class=chip style="background:'+(isLive?'rgba(244,105,95,.15);color:var(--red)':'rgba(125,144,173,.13);color:var(--mut)')+'">'+(S.mode||'?')+'</span>'
      +(S.halted?' <span class=chip style="background:rgba(244,105,95,.25);color:var(--red)">DAY HALTED</span>':'')
      +' <span class=mut style="font-size:11px">era dlive1 &middot; full paper brain (nickel + pyramid)'
      +(S.caps?' &middot; caps '+F(S.caps.bet)+'/bet &middot; '+F(S.caps.open)+' open &middot; '+F(S.caps.halt)+' daily halt'+(S.caps.dyn?' (% of NAV, live)':''):'')
      +'</span>';
    const bal=(L.balance_c!=null)?L.balance_c/100:null;
    if(L.pnl_true!=null&&L.marked_nav!=null){
      const base=Number(L.deposits||L.baseline||100.09);
      const retPct=base>0?(L.pnl_true/base*100):null;
      $('rmpnl').innerHTML=F(L.marked_nav)
        +' <span class="'+C(L.pnl_true)+'" style="font-size:22px">'+(retPct!=null?P(retPct):'')+'</span>'
        +' <span class="'+C(L.pnl_true)+'" style="font-size:15px;font-weight:600">'+M(L.pnl_true)+'</span>';
      $('rmpnld').innerHTML='<span class=mut>total return on '+F(base)+' deposited'
        +((L.pnl_weather!=null&&L.pnl_crypto!=null&&base>0)?(' &middot; weather <span class="'+C(L.pnl_weather)+'">'+M(L.pnl_weather)+' ('+P(L.pnl_weather/base*100)+')</span> + crypto <span class="'+C(L.pnl_crypto)+'">'+M(L.pnl_crypto)+' ('+P(L.pnl_crypto/base*100)+')</span>'):'')
        +' &middot; the exchange is proof</span>';
    }
    // EVERY number below is derived from Kalshi's own records (settlements,
    // positions, balance). The bot's internal diary is never displayed.
    const todayTrue=(L.marked_nav!=null&&S.day_nav0!=null)?(L.marked_nav-S.day_nav0):null;
    // ACCOUNT row: the five numbers that matter, nothing else
    const cS=((d.clive||{}).summary)||{};
    const base2=Number(L.deposits||L.baseline||100.09);
    const todayPct=(todayTrue!=null&&S.day_nav0>0)?(todayTrue/S.day_nav0*100):null;
    $('rmtiles').innerHTML=[
      tile('Weather book P&L',(L.pnl_weather!=null)?('<span class="'+C(L.pnl_weather)+'">'+M(L.pnl_weather)+'</span>'):NA,'era dlive1'+((L.pnl_weather!=null&&base2>0)?' &middot; '+P(L.pnl_weather/base2*100)+' of deposits':'')),
      tile('Crypto book P&L',(L.pnl_crypto!=null)?('<span class="'+C(L.pnl_crypto)+'">'+M(L.pnl_crypto)+'</span>'):NA,'era clive1'+(cS.paused?' &middot; <span style="color:var(--amb)">PAUSED</span> (winding down)':'')+((L.pnl_crypto!=null&&base2>0)?' &middot; '+P(L.pnl_crypto/base2*100)+' of deposits':'')),
      tile('Account balance',bal!=null?F(bal):NA,'cash, live from Kalshi'),
      tile("Today's return",(todayTrue!=null)?('<span class="'+C(todayTrue)+'">'+(todayPct!=null?P(todayPct):M(todayTrue))+'</span>'):'<span class=mut>anchoring&hellip;</span>',(todayTrue!=null)?('<span class="'+C(todayTrue)+'">'+M(todayTrue)+'</span> &middot; NAV vs day start '+F(S.day_nav0)):'account, NAV vs day start'),
      tile('Fees (total)',F((S.k_fees||0)+(cS.fees||0)),'both books, lifetime'),
      // 8/10 truth check: internal book ledgers vs the exchange's NAV.
      // A growing gap means a bookkeeping bug - shown loudly, never hidden.
      (function(){const g=(L.pnl_true!=null)?(L.pnl_true-((S.net||0)+(S.unrealized||0)+(cS.realized||0)+(cS.unrealized||0))):null;
        return tile('Books vs NAV',(g!=null)?('<span class="'+C(g)+'">'+M(g)+'</span>'):NA,'internal ledgers vs exchange NAV &middot; investigate if it grows');})()
    ].join('');
    // PERFORMANCE - weekly & monthly % returns by book, from the
    // never-trimmed daily ledgers; % beside $, live period chipped
    if(L.perf&&((L.perf.weekly||[]).length||(L.perf.monthly||[]).length)){
      $('perfwrap').style.display='block';
      const pc=(v,p)=>'<td class=num><span class="'+C(v)+'">'+(p!=null?P(p):M(v))+'</span><br><span class=mut style="font-size:10px">'+M(v)+'</span></td>';
      const rows=a=>a.map(r=>'<tr'+(r.live?' style="background:rgba(232,180,76,.05)"':'')+'><td>'+r.label
        +(r.live?' <span class=chip style="background:rgba(232,180,76,.18);color:var(--amb);font-size:9px">LIVE</span>':'')
        +'</td>'+pc(r.wx,r.wx_pct)+pc(r.cr,r.cr_pct)+pc(r.tot,r.tot_pct)
        +'<td class=num>'+F(r.nav1)+'</td></tr>').join('');
      $('perfw').innerHTML=rows(L.perf.weekly||[]);
      $('perfm').innerHTML=rows(L.perf.monthly||[]);
    }
    // BOOK 1 - WEATHER: status at a glance
    $('wxtiles').innerHTML=[
      tile('Record',(S.has_kalshi_truth&&S.k_wins!=null)?((S.k_wins||0)+'W / '+(S.k_losses||0)+'L'):'<span class=mut>syncing&hellip;</span>','Kalshi settlements + our sales, lifetime &middot; '+((S.k_open!=null?S.k_open:S.open)||0)+' open &middot; '+((S.k_resting_n!=null?S.k_resting_n:S.resting)||0)+' resting'),
      (S.caps?tile('Sizing',F(S.caps.bet)+'/bet','gate '+(S.gate==='scale'?'PASSED':'probe')+' &middot; halt -'+F(S.caps.halt)+' &middot; entries &ge;'+(S.caps.floor||80)+'&cent;'):''),
      (L.nickel?tile('Nickel lane',(L.nickel.wins||0)+'W / '+(L.nickel.losses!=null?L.nickel.losses:((L.nickel.n||0)-(L.nickel.wins||0)))+'L &middot; <span class="'+C(L.nickel.net)+'">'+M(L.nickel.net||0)+'</span>',(L.nickel.open||0)+'/'+(L.nickel.max_open||5)+' lanes &middot; settle-or-stop'):''),
      (S.dips&&S.dips.on?tile('Bid side',((S.dips.fills||0)?((S.dips.fills||0)+' dips caught &middot; '+F(S.dips.fill_cost||0)+' wholesale'):'<span class=mut>fishing&hellip;</span>'),(S.dips.resting||0)+' bids resting ('+F(S.dips.cost||0)+') &middot; '+(S.dips.discount||4)+'&cent; under market &middot; floor 80&cent;'):''),
      (S.quotes&&S.quotes.on?tile('Offer side',((S.quotes.sold||0)?((S.quotes.sold||0)+' lifted &middot; <span class="'+C(S.quotes.sold_net)+'">'+M(S.quotes.sold_net||0)+'</span>'):'<span class=mut>quoting&hellip;</span>'),(S.quotes.resting||0)+' resting &middot; sells at &ge;'+(S.quotes.min||97)+'&cent; ('+(S.quotes.nickel_min||98)+'&cent; nickels) &middot; two-sided book'):''),
      tile('Mirror sync',(S.sync_diffs==null)?NA:(S.sync_diffs===0?'<span class=pos>1:1 WITH KALSHI</span>':'<span class=neg>'+S.sync_diffs+' DIFFS</span>'),'book vs exchange, every cycle')
    ].join('');
    // diagnostics drawer: the instruments, out of the way until wanted
    $('wxdiag').innerHTML=[
      tile('Unrealized (weather)',(L.unrealized!=null)?'<span class="'+C(L.unrealized)+'">'+M(L.unrealized)+'</span>':NA,'open positions vs cost'),
      (function(){const E=S.exec||{};const pm=E.placed_maker||0,fm=E.filled_maker||0;
        return tile('Execution',pm?Math.round(100*fm/pm)+'% maker fill':'&ndash;',
          'takers '+(E.filled_taker||0)+'/'+(E.placed_taker||0)+' &middot; bucket-blocked '+(E.bucket_blocked||0));})(),
      tile('Exit autopsy',(S.autopsy_n_settled?('exits '+((S.autopsy_saved||0)>=0?'saved ':'cost ')+'<span class="'+C(S.autopsy_saved)+'">'+M(S.autopsy_saved||0)+'</span>'):'<span class=mut>grading&hellip;</span>'),
        (S.autopsy_would_won||0)+' of '+(S.autopsy_n_settled||0)+' graded would have won'),
      tile('Missed fills',(function(){const ms=S.miss_since||{};
        return (S.miss_settled?('recoverable '+'<span class="'+C(-(ms.recoverable||0))+'">'+M(Math.abs(ms.recoverable||0))+'</span>'+' <span class=mut style="font-size:12px">since fix</span>'):'<span class=mut>grading&hellip;</span>');})(),
        (function(){const ms=S.miss_since||{},mp=S.miss_pre||{};
        return (ms.n||0)+' since taker-first ('+(ms.would_won||0)+' would-won at unreachable prices, '+M(ms.cost||0)+') &middot; pre-fix history: '+(mp.n||0)+' misses '+M(mp.cost||0)+' (closed 8/10)';})())
    ].join('');
    $('rmbuckets').innerHTML=((S.buckets||[]).map(b=>'<tr><td>'+b.bucket+'</td>'
      +'<td class=num>'+b.n+'</td><td class=num>'+b.wins+'W/'+(b.n-b.wins)+'L</td>'
      +'<td class=num><span class="'+C(b.net)+'">'+M(b.net)+'</span></td>'
      +'<td>'+(b.blocked?'<span class=chip style="background:rgba(244,105,95,.15);color:var(--red)">BLOCKED</span>':(b.proven?'<span class=chip style="background:rgba(47,208,140,.25);color:var(--grn)">&frac12; KELLY</span>':'<span class=chip style="background:rgba(47,208,140,.13);color:var(--grn)">OPEN</span>'))+'</td></tr>').join(''))
      ||'<tr><td colspan=5 class=empty>Accumulating live evidence per bucket&hellip;</td></tr>';
    const rows=[];
    (L.open||[]).forEach(b=>rows.push('<tr>'+mkt(b)+side(b.side)
      +'<td><span class=chip style="background:rgba(47,208,140,.13);color:var(--grn)">'+((b.trig==='nickel')?'NICKEL':'FILLED')+'</span></td>'
      +'<td class=num>'+Math.round((b.pside||0)*100)+'%</td>'
      +'<td class=num>'+b.entry+'&cent;</td>'
      +'<td class=num>'+(b.now!=null?b.now+'&cent;':'&ndash;')+'</td>'
      +'<td class=num>'+b.count+(b.adds?' <span class=mut>(+'+b.adds+')</span>':'')+'</td>'
      +'<td class=num>'+(b.value!=null?F(b.value):'&ndash;')+'</td>'
      +'<td class=num>'+(b.upnl!=null?('<span class="'+C(b.upnl)+'">'+M(b.upnl)+'</span>'):'&ndash;')+'</td></tr>'));
    (L.resting||[]).forEach(o=>rows.push('<tr>'+mkt(o)+side(o.side)
      +'<td><span class=chip style="background:rgba(232,180,76,.13);color:var(--amb)">'+((o.trig==='nickel')?'NICKEL REST':'RESTING')+'</span></td>'
      +'<td class=num>'+Math.round((o.pside||0)*100)+'%</td>'
      +'<td class=num>'+o.entry+'&cent;</td><td class=num>&ndash;</td>'
      +'<td class=num>'+o.count+'</td>'
      +'<td class=num>'+F(o.entry*o.count/100)+'</td><td class=num>&ndash;</td></tr>'));
    $('rmopen').innerHTML=rows.join('')||'<tr><td colspan=9 class=empty>No live positions or resting orders yet.</td></tr>';
    const rl=[];
    // 8/12 (Adam): result = P&L SIGN, full stop. Outcome-based labels
    // called a +$0.06 premium sale "LOST" because the settlement went
    // to the buyer. Money decides; sold rows get their own chip.
    (L.history||[]).forEach(b=>{const won=Number(b.pnl)>0;
      rl.push('<tr><td class=mut>'+((b.ts||'').slice(5,16).replace('T',' '))+'</td>'+mkt(b)+side(b.side)
      +'<td class=num>'+b.entry+'&cent;</td>'
      +'<td class=num>'+(b.exit_px!=null?b.exit_px+'&cent;':(won?'100&cent;':'0&cent;'))+'</td>'
      +'<td class=num>'+b.count+'</td>'
      +'<td>'+(b.sold?('<span class=chip style="background:rgba(47,208,140,.13);color:var(--grn)">SOLD '+(won?'+':'')+'</span>'):(b.stopped?'<span class=chip style="background:rgba(232,180,76,.13);color:var(--amb)">STOP</span>':(b.faded?'<span class=chip style="background:rgba(180,120,230,.15);color:#b478e6">FADE</span>':('<span class="'+(won?'won':'lost')+'">'+(won?'WON':'LOST')+'</span>'))))+'</td>'
      +'<td class=num><span class="'+C(b.pnl)+'">'+M(b.pnl)+'</span></td></tr>');});
    $('rmreal').innerHTML=rl.join('')||'<tr><td colspan=8 class=empty>Nothing realized yet &mdash; first settlements land tomorrow morning.</td></tr>';
  }
  // 8/10 (Adam): crypto book PAUSED and off the tracker - the panel
  // hides while paused; the ledgers, NAV accounting and the hero
  // attribution stay (the record is the record, only the display goes)
  if(d.clive&&d.clive.summary&&!d.clive.summary.paused){const K2=d.clive,S3=K2.summary||{};
    document.getElementById('clivewrap').style.display='block';
    const cLive=(S3.mode==='LIVE');
    $('clmode').innerHTML='<span class=chip style="background:'+(cLive?'rgba(244,105,95,.15);color:var(--red)':'rgba(125,144,173,.13);color:var(--mut)')+'">'+(S3.mode||'?')+'</span>'
      +(S3.halted?' <span class=chip style="background:rgba(244,105,95,.25);color:var(--red)">DAY HALTED</span>':'')
      +' <span class=mut style="font-size:11px">era clive1 &middot; taker-first &middot; '+Math.round((S3.alloc||0.5)*100)+'% of NAV &middot; band 80-88&cent; &middot; ladder-arb lane &middot; no stop, no trail</span>';
    const cc=S3.caps||{};
    $('cltiles').innerHTML=[
      tile('Book bankroll',F(S3.bank||0),'50% of NAV, compounds every cycle &middot; '+((S3.kelly||0.25)>=0.5?'&frac12;-Kelly (PROVEN, n='+(S3.kelly_n||0)+')':'&frac14;-Kelly ('+(S3.kelly_n||0)+'/'+(S3.kelly_gate||100)+' to upgrade)')+' &middot; '+F(cc.bet||0)+'/bet'),
      tile('Realized (after fees)',(S3.realized!=null)?('<span class="'+C(S3.realized)+'">'+M(S3.realized)+'</span>'):'&ndash;','fees '+F(S3.fees||0)+' &middot; settle/stop ledger'),
      tile('Record',(S3.wins||0)+'W / '+(S3.losses||0)+'L',(S3.open||0)+' open &middot; '+(S3.resting||0)+' resting &middot; '+(S3.placed||0)+' placed'),
      (S3.arb&&S3.arb.on?tile('Ladder arb',((S3.arb.pairs||0)?((S3.arb.pairs||0)+' pairs &middot; <span class="'+C(S3.arb.pnl)+'">'+M(S3.arb.pnl||0)+'</span>'):'<span class=mut>scanning&hellip;</span>'),(S3.arb.open_pairs||0)+' open &middot; best gap '+(S3.arb.best_gap_c!=null?S3.arb.best_gap_c+'&cent;':'&ndash;')+' &middot; needs '+(S3.arb.min_net_c||1)+'&cent;/ct net &middot; arithmetic, not opinions'):''),
      (S3.hi?tile('93-96&cent; probe',S3.hi.blocked?'<span class=neg>BLOCKED</span> &middot; '+(S3.hi.w||0)+'W / '+(S3.hi.l||0)+'L':(((S3.hi.w||0)+(S3.hi.l||0))?((S3.hi.w||0)+'W / '+(S3.hi.l||0)+'L &middot; <span class="'+C(S3.hi.pnl)+'">'+M(S3.hi.pnl||0)+'</span>'):'<span class=mut>no settles yet</span>'),(S3.hi.open||0)+' open &middot; ladder '+Math.round((S3.hi.pct||0.06)*100)+'%/bet (8% @'+(S3.hi.n1||10)+', 10% @'+(S3.hi.n2||20)+' net+)'):''),
      tile("Today's P&L",(S3.day_pnl!=null)?('<span class="'+C(S3.day_pnl)+'">'+M(S3.day_pnl)+'</span>'):'&ndash;','halts at -'+F(cc.halt||0)),
      tile('Mirror sync',(S3.sync_diffs==null)?'<span class=mut>syncing&hellip;</span>':(S3.sync_diffs===0?'<span class=pos>1:1 WITH KALSHI</span>':'<span class=neg>'+S3.sync_diffs+' DIFFS</span>'),'crypto book vs exchange positions (crypto universe)')
    ].join('');
    $('cltbl').innerHTML=((K2.open||[]).map(b=>'<tr><td>'+(b.name||b.ticker||'')+'</td>'
      +'<td><span class=chip style="background:'+(b.side==='yes'?'rgba(47,208,140,.13);color:var(--grn)':'rgba(244,105,95,.13);color:var(--red)')+'">'+String(b.side||'').toUpperCase()+'</span></td>'
      +'<td class=num>'+Math.round((b.pside||0)*100)+'%</td>'
      +'<td class=num>'+b.entry+'&cent;</td>'
      +'<td class=num>'+(b.now!=null?b.now+'&cent;':'&ndash;')+'</td>'
      +'<td class=num>'+b.count+'</td>'
      +'<td class=num>'+(b.value!=null?F(b.value):F((b.entry||0)*(b.count||0)/100))+'</td>'
      +'<td class=num>'+(b.upnl!=null?('<span class="'+C(b.upnl)+'">'+M(b.upnl)+'</span>'):'&ndash;')+'</td></tr>').join(''))
      ||'<tr><td colspan=8 class=empty>Scanning crypto hourlies for 80-92&cent; favorites&hellip;</td></tr>';
    const crl=(K2.settled||[]).slice(0,10);
    if(crl.length){document.getElementById('clrealwrap').style.display='block';
      $('clreal').innerHTML=crl.map(h=>'<tr><td>'+String(h.ts||'').slice(5,16).replace('T',' ')+'</td>'
        +'<td>'+(h.name||'')+'</td>'
        +'<td>'+String(h.side||'').toUpperCase()+'</td>'
        +'<td class=num>'+h.entry+'&cent;</td>'
        +'<td class=num>'+(h.exit_px!=null?h.exit_px+'&cent;':(h.outcome===1?'100&cent;':'0&cent;'))+'</td>'
        +'<td class=num>'+h.count+'</td>'
        +'<td>'+(h.outcome===1?'<span class=pos>WON</span>':(h.outcome===0?'<span class=neg>LOST</span>':'STOP'))+'</td>'
        +'<td class=num><span class="'+C(h.pnl)+'">'+M(h.pnl)+'</span></td></tr>').join('');}}
  if(d.tick){const T=d.tick,TR=T.rules||{};
    $('tkwrap').style.display='block';
    $('tkmode').innerHTML='<span class=chip style="background:rgba(56,189,248,.16);color:#7dd3fc">PAPER &middot; NO MONEY</span>'
      +' <span class=mut style="font-size:11px">era '+(T.era||'tick1')+' &middot; 15-minute gold/silver/WTI windows &middot; distance-vs-clock model on a Pyth proxy &middot; fills only when a REAL print trades through us</span>';
    const cl=T.clock||{},lanes=T.by_lane||{},RF=T.refuse||{};
    $('tktiles').innerHTML=[
      tile('TOTAL P&L','<span class="'+C(T.total)+'">'+M(T.total||0)+'</span>','banked '+M(T.realized||0)+' from '+(T.settled_n||0)+' settled &middot; open '+M(T.open_pnl||0)+' &middot; using '+M(T.capital||0)+' of '+M(T.capital_max||100)),
      tile('Evidence clock',(cl.n||0)+' / '+(cl.goal||200),(cl.verdict_due?'<span class=neg>VERDICT DUE</span>':'settled windows before this lane is judged')+' &middot; '+(T.wins||0)+'W / '+(T.losses||0)+'L'),
      tile('Paper fills',(T.fills_strict||0)+' strict &middot; '+(T.fills_loose||0)+' loose','strict = a real print traded THROUGH our resting bid &middot; '+(T.trades_seen||0)+' prints seen &middot; '+(T.cycles||0)+' cycles'),
      tile('Lanes',Object.keys(lanes).length?Object.keys(lanes).map(function(k){return k+' '+M(lanes[k].pnl)}).join(' &middot; '):'&ndash;','endgame = certainty the clock already delivered &middot; tail = the longshot-bias harvest'),
      tile('Why we stood aside',(RF.no_edge||0)+' no edge &middot; '+(RF.band_skip||0)+' out of band','+'+(RF.no_vol||0)+' warming up &middot; '+(RF.proxy_dead||0)+' dead proxy &middot; '+(RF.capped||0)+' capped &mdash; a quiet book here means the market was efficient, which is itself the finding'),
      tile('In and out',(T.exits||0)+' early exits','sold back to the book before settlement rather than riding a binary &middot; the live weather ledger says lifts +$88.82 vs settles &minus;$91.05'),
      tile('Instrument zero-error',Object.keys(T.basis||{}).map(function(k){return k.replace('KX','').replace('15M','')+' '+(T.basis[k]!=null?(T.basis[k]>0?'+':'')+T.basis[k].toFixed(4):'<span class=mut>learning</span>')}).join(' &middot; '),'our proxy MINUS Kalshi&rsquo;s settlement feed, measured free at every window open and subtracted before any distance is computed. WTI read &minus;0.075 against windows that only travel ~0.05 &mdash; the error was bigger than the signal. Samples: '+Object.keys(T.basis_n||{}).map(function(k){return k.replace('KX','').replace('15M','')+' '+T.basis_n[k]}).join('/')),
      tile('True arbitrage',(T.arb_seen||0)+' crossed books','yes ask + no ask &lt; 100 after BOTH fees &mdash; locked profit with no view. Counted, not assumed: on a liquid book this should be ~zero'),
      (function(){var P=T.pair||{};var r=(P.rate!=null)?(P.rate*100).toFixed(0)+'%':'&ndash;';var b=(P.breakeven!=null)?(P.breakeven*100).toFixed(0)+'%':'&ndash;';
       return tile('Legged pair completion','<span class="'+(P.pays?'pos':'neg')+'">'+r+'</span> <span class=mut style="font-size:12px">need '+b+'</span>','buy YES low + NO low = 100 &minus; the window&rsquo;s range, so the lock IS the range. But the leg that fills ALONE fills because price ran away and stayed away &mdash; which is that leg losing. '+(P.both||0)+' both &middot; '+(P.one||0)+' one-legged &middot; n='+(P.n||0)+' &middot; lock +'+(P.lock_c||0)+'&cent; vs risk &minus;'+(P.risk_c||0)+'&cent;. Measured, not traded: backtest was negative at every level.');})()
    ].join('');
    $('tkwin').innerHTML=(T.windows||[]).map(function(w){
      var edge=(w.model_p!=null&&w.yes_bid!=null)?(w.model_p*100-((w.yes_bid+w.yes_ask)/2)):null;
      return '<tr><td>'+(w.label||'')+'</td>'
      +'<td class=num>'+(w.strike!=null?w.strike:'&ndash;')+'</td>'
      +'<td class=num>'+(w.spot!=null?w.spot:'&ndash;')+'</td>'
      +'<td class=num><span class="'+C(w.d)+'">'+(w.d!=null?(w.d>0?'+':'')+w.d:'&ndash;')+'</span></td>'
      +'<td class=num>'+(w.t_left!=null?w.t_left+'s':'&ndash;')+'</td>'
      +'<td class=num>'+(w.yes_bid!=null?w.yes_bid+'/'+w.yes_ask:'&ndash;')+'</td>'
      +'<td class=num>'+(w.model_p!=null?(w.model_p*100).toFixed(1)+'%':'<span class=mut>warming</span>')+'</td>'
      +'<td class=num>'+(edge!=null?(edge>0?'+':'')+edge.toFixed(1)+'&cent;':'&ndash;')+'</td>'
      +'<td>'+(w.dead?'<span class=neg>feed dead</span>':(w.quoted?'<span class=pos>quoting</span>':'<span class=mut>&mdash;</span>'))+'</td></tr>';}).join('')
      ||'<tr><td colspan=9 class=empty>No open window right now&hellip;</td></tr>';
    $('tkcal').innerHTML=(T.calibration||[]).map(function(c){
      var said=parseFloat(c.bucket)+5,dev=(c.hit!=null)?(c.hit-said):null;
      return '<tr><td>'+c.bucket+'</td><td class=num>'+c.n+'</td>'
      +'<td class=num>'+(c.hit!=null?c.hit.toFixed(0)+'%':'&ndash;')+'</td>'
      +'<td>'+((dev==null||c.n<10)?'<span class=mut>too few</span>':(Math.abs(dev)<=8?'<span class=pos>honest</span>':'<span class=neg>'+(dev<0?'OVERconfident':'UNDERconfident')+'</span>'))+'</td></tr>';}).join('')
      ||'<tr><td colspan=4 class=empty>No settled windows yet &mdash; the table fills as the clock runs&hellip;</td></tr>';
    $('tkset').innerHTML=(T.settled||[]).map(function(s){
      return '<tr><td class=mut>'+String(s.ts||'').replace('T',' ').slice(5,16)+'</td>'
      +'<td>'+(s.label||'')+'</td><td>'+(s.lane||'')+'</td>'
      +'<td>'+String(s.side||'').toUpperCase()+'</td>'
      +'<td class=num>'+(s.px!=null?s.px+'&cent;':'&ndash;')+(s.exit_px!=null?' &rarr; '+s.exit_px+'&cent;':'')+'</td>'
      +'<td class=num>'+(s.model_p!=null?(s.model_p*100).toFixed(0)+'%':'&ndash;')+'</td>'
      +'<td>'+(s.won?'<span class=pos>WON</span>':'<span class=neg>LOST</span>')+'</td>'
      +'<td class=num><span class="'+C(s.pnl)+'">'+M(s.pnl)+'</span></td></tr>';}).join('')
      ||'<tr><td colspan=8 class=empty>Nothing settled yet&hellip;</td></tr>';}
  // Book 4 (phantom) renderer retired 8/25 - see the panel note above.
  if(d.driftw){const D=d.driftw,dsm=D.summary||{};
    const wmkt=b=>'<td><span class=mkt>'+((b.name||b.ticker||'')+'').replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</span></td>';
    $('driftw').innerHTML=[
      tile('Bank (paper)',F(dsm.cash||0),'started '+F(dsm.start||0)),
      tile('Record',(dsm.wins||0)+'W / '+(dsm.losses||0)+'L',(dsm.open||0)+' open'),
      tile('Realized P&L',(dsm.realized!=null)?'<span class="'+C(dsm.realized)+'">'+M(dsm.realized)+'</span>':NA,''),
      tile('Unrealized (marked)',(dsm.unrealized!=null)?'<span class="'+C(dsm.unrealized)+'">'+M(dsm.unrealized)+'</span>':NA,
        (dsm.marked_nav!=null)?('marked NAV '+F(dsm.marked_nav)):''),
      tile('Gate',(dsm.gate||'probe')+' '+(dsm.gate_n||0)+'/30','same contract: +EV and calib &le;5pts'),
      tile('Universe','commodities + financials','&le;48h to close &middot; vol&ge;200 &middot; spread&le;6&cent; &middot; no nickel/pyramid yet'),
      tile('Trigger','≥80¢ level · 65–80¢ climb','vol-confirmed · close&le;24h climbs · stop <50¢ · trail 15¢'),
    ].join('');
    const dr=[];
    (D.open||[]).forEach(b=>dr.push('<tr>'+wmkt(b)+side(b.side)
      +'<td class=num>'+Math.round((b.pside||0)*100)+'%</td>'
      +'<td class=num>'+(b.trig==='level'?'<span class=mut>level</span>':'<span class=mut>climb</span>')+'</td>'
      +'<td class=num>'+b.entry+'&cent;</td>'
      +'<td class=num>'+(b.now!=null?b.now+'&cent;':'&ndash;')+'</td>'
      +'<td class=num>'+b.count+'</td>'
      +'<td class=num>'+feeC(b.fee)+'</td>'
      +'<td class=num>'+(b.value!=null?F(b.value):'&ndash;')+'</td>'
      +'<td class=num>'+(b.upnl!=null?('<span class="'+C(b.upnl)+'">'+M(b.upnl)+'</span>'):'&ndash;')+'</td></tr>'));
    $('driftwtbl').innerHTML=dr.join('')||'<tr><td colspan=10 class=empty>No open positions — first scan pending.</td></tr>';
    const rl=[];
    (D.settled||[]).slice(0,20).forEach(b=>{const won=Number(b.pnl)>0;
      rl.push('<tr><td class=mut>'+((b.ts||'').slice(5,16).replace('T',' '))+'</td>'+wmkt(b)+side(b.side)
      +'<td class=num>'+Math.round((b.pside||0)*100)+'%</td>'
      +'<td class=num>'+b.entry+'&cent;</td>'
      +'<td class=num>'+(b.exit_px!=null?b.exit_px+'&cent;':(won?'100&cent;':'0&cent;'))+'</td>'
      +'<td class=num>'+b.count+'</td>'
      +'<td class=num>'+feeC(b.fee)+'</td>'
      +'<td>'+(b.stopped?'<span class=chip style="background:rgba(232,180,76,.13);color:var(--amb)">STOP</span>':(b.faded?'<span class=chip style="background:rgba(180,120,230,.15);color:#b478e6">FADE</span>':('<span class="'+(won?'won':'lost')+'">'+(won?'WON':'LOST')+'</span>')))+'</td>'
      +'<td class=num><span class="'+C(b.pnl)+'">'+M(b.pnl)+'</span></td></tr>');});
    $('driftwreal').innerHTML=rl.join('')||'<tr><td colspan=10 class=empty>No realized trades yet.</td></tr>';
  } else { $('driftw').innerHTML='<div class=tile><div class=k>Drift wide</div><div class=v>&ndash;</div><div class=s>starting&hellip;</div></div>';
    $('driftwtbl').innerHTML='<tr><td colspan=10 class=empty>No state yet.</td></tr>';
    $('driftwreal').innerHTML='<tr><td colspan=10 class=empty>No state yet.</td></tr>'; }
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
