#!/usr/bin/env python3
"""PAPER TRADING on LIVE Kalshi market data.

What this is:
  * Reads REAL, live market prices from the public Kalshi endpoint
    (no API key, no account, no money, no identity needed).
  * Runs your real 'smart' strategy on those live markets.
  * SIMULATES fills and tracks a simulated P&L after fees.
  * Places ZERO real orders. It cannot move money - it never authenticates.

Why it matters:
  Demo has fake, thin liquidity so its P&L is meaningless. This watches the
  strategy against genuinely deep markets - the only honest way to judge
  whether it would actually make money, with nothing at risk.

Honesty about the fill model:
  * TAKER fills (momentum entries, market exits) fill at the live touch -
    realistic.
  * MAKER fills (resting spread-capture orders) are assumed to fill when the
    live price trades to our limit. That ignores queue position and adverse
    selection, so maker P&L here is an OPTIMISTIC upper bound, not a promise.

Performance:
  * A summary line prints every cycle (P&L, win/loss, fees).
  * Every cycle is also appended to logs/paper_pnl.csv (open it in Excel).

Run:  python paper.py [--config=config_balanced.yaml] [--cycles=N] [--start=100]
"""
from __future__ import annotations

import os
import sys
import json
import time
import datetime
import threading
import requests
try:
    import weather_paper
except Exception:
    weather_paper = None


def _serve_dashboard():
    """Serve the dashboard from INSIDE the bot process, so it stays up as long as
    the bot runs - independent of the flaky standalone kalshi-dashboard service.
    If that service already holds the port, we simply no-op."""
    try:
        import dashboard as _dash
    except Exception as e:
        print(f"  in-process dashboard unavailable: {e}")
        return
    _dash.HOST = os.environ.get("DASH_HOST", "0.0.0.0")
    _dash.PORT = int(os.environ.get("DASH_PORT", "8765"))
    _dash.TOKEN = os.environ.get("DASH_TOKEN", "")   # no token -> existing links still work

    def _run():
        try:
            srv = _dash.ThreadingHTTPServer((_dash.HOST, _dash.PORT), _dash.H)
        except OSError:
            return   # port already served by the standalone service - fine
        threading.Thread(target=_dash._price_loop, daemon=True).start()
        try:
            srv.serve_forever()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
    print(f"  dashboard served in-process on {_dash.HOST}:{_dash.PORT}")
try:
    import weather_live
except Exception:
    weather_live = None
try:
    import drift_paper
except Exception:
    drift_paper = None
try:
    import drift_wide
except Exception:
    drift_wide = None
try:
    import drift_live
except Exception:
    drift_live = None

from kalshibot.config import load_config
from kalshibot.fees import fee_cents
from kalshibot.strategies import build_strategy
from kalshibot.strategies.base import MarketSnapshot, Position

LIVE = "https://api.elections.kalshi.com/trade-api/v2"
LOG_PATH = os.path.join("logs", "paper_pnl.csv")
TRADES_PATH = os.path.join("logs", "paper_trades.csv")
STATE_PATH = os.path.join("logs", "paper_state.json")
SIM_PATH = os.path.join("logs", "paper_sim.json")   # persisted portfolio (survives restarts)
LOCK_PATH = os.path.join("logs", "paper.lock")     # single-instance guard
WATCHDOG_SEC = int(os.environ.get("PAPER_WATCHDOG_SEC", "1800"))  # hang guard; 0 disables
_HEARTBEAT = {"ts": time.time()}                   # last main-loop progress


def _f(v):
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _c(v):
    return int(round(_f(v) * 100))


def fetch_live(max_pages: int = 8):
    """Live non-MVE markets with quotes, keyed by ticker."""
    out = {}
    cursor = None
    for _ in range(max_pages):
        params = {"limit": 200, "status": "open", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        try:
            d = requests.get(LIVE + "/events", params=params, timeout=15).json()
        except Exception:
            break
        for ev in d.get("events", []) or []:
            if "MVE" in (ev.get("series_ticker") or ""):
                continue
            ev_title = ev.get("title") or ""
            for mk in ev.get("markets", []) or []:
                yb, ya = _c(mk.get("yes_bid_dollars")), _c(mk.get("yes_ask_dollars"))
                if yb <= 0 or ya <= 0:
                    continue
                base = mk.get("title") or ev_title or mk["ticker"]
                sub = mk.get("yes_sub_title") or ""
                name = (base + " - " + sub) if sub and sub.lower() not in base.lower() else base
                out[mk["ticker"]] = {
                    "yes_bid": yb, "yes_ask": ya,
                    "yes_bid_size": int(_f(mk.get("yes_bid_size_fp"))),
                    "yes_ask_size": int(_f(mk.get("yes_ask_size_fp"))),
                    "volume": _f(mk.get("volume_fp")),
                    "vol24": _f(mk.get("volume_24h_fp")),
                    "close": mk.get("close_time") or "",
                    "last_price": _c(mk.get("last_price_dollars")),
                    "name": name,
                }
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def _days_to_close(close_str, now):
    if not close_str:
        return None
    try:
        cdt = datetime.datetime.strptime(close_str, "%Y-%m-%dT%H:%M:%SZ")
        cdt = cdt.replace(tzinfo=datetime.timezone.utc)
        return (cdt - now).total_seconds() / 86400.0
    except Exception:
        return None


def select(book, m):
    """Pick tradeable markets, prioritising RECENTLY ACTIVE and SOONER-RESOLVING
    ones (spread capture needs markets that actually churn, not multi-year bets)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    maxdays = getattr(m, "max_days_to_resolve", 0) or 0
    minrec = getattr(m, "min_recent_volume", 0) or 0
    out = []
    for t, q in book.items():
        yb, ya = q["yes_bid"], q["yes_ask"]
        if not (m.min_price_cents <= yb <= m.max_price_cents):
            continue
        if not (m.min_spread_cents <= (ya - yb) <= m.max_spread_cents):
            continue
        if q["volume"] < m.min_volume:
            continue
        if q.get("vol24", 0) < minrec:                 # must be trading recently
            continue
        if q["yes_bid_size"] < m.min_book_depth or q["yes_ask_size"] < m.min_book_depth:
            continue
        if maxdays > 0:                                # skip far-off resolutions
            d = _days_to_close(q.get("close", ""), now)
            if d is not None and d > maxdays:
                continue
        out.append(t)
    def _score(t):
        q = book[t]
        d = _days_to_close(q.get("close", ""), now)
        d = 365.0 if d is None else max(0.0, d)
        # recent activity, tilted toward markets that resolve sooner
        return q.get("vol24", 0) * (180.0 / (d + 180.0))
    out.sort(key=_score, reverse=True)   # active AND sooner-resolving first
    return out[: m.scan_top_n]


class Sim:
    """Simulated portfolio with full win/loss + fee accounting.

    Positions store avg cost *including* the buy fee per contract, so that
    realized P&L on a sell already nets every fee on both legs.
    """
    def __init__(self, start_cents):
        self.start = start_cents
        self.cash = float(start_cents)
        self.pos = {}            # ticker -> [count, avg_cost_per_contract_float]
        self.resting = {}        # ticker -> dict(action, price, count)
        self.fees = 0.0
        self.fills = 0
        self.realized = 0.0      # net cents from closed round-trips
        self.round_trips = 0
        self.wins = 0
        self.losses = 0
        self.prev_vol = {}     # ticker -> volume last cycle (to spot new trades)
        self.names = {}        # ticker -> human-readable market name

    def _log_trade(self, ticker, action, count, price, fee, taker, entry=None, pnl=None):
        import csv as _csv
        os.makedirs("logs", exist_ok=True)
        new = not os.path.exists(TRADES_PATH)
        with open(TRADES_PATH, "a", newline="") as f:
            w = _csv.writer(f)
            if new:
                w.writerow(["timestamp", "ticker", "action", "type", "count",
                            "price_c", "fee_c", "entry_c", "trade_pnl_$", "name"])
            w.writerow([datetime.datetime.now().isoformat(timespec="seconds"),
                        ticker, action, "taker" if taker else "maker", count,
                        price, round(fee, 2),
                        "" if entry is None else int(round(entry)),
                        "" if pnl is None else round(pnl / 100, 2),
                        self.names.get(ticker, ticker)])

    def to_dict(self):
        return {"start": self.start, "cash": self.cash, "pos": self.pos,
                "resting": self.resting, "realized": self.realized,
                "round_trips": self.round_trips, "wins": self.wins,
                "losses": self.losses, "fees": self.fees, "fills": self.fills,
                "names": self.names}

    def save(self, path):
        try:
            os.makedirs("logs", exist_ok=True)
            with open(path, "w") as f:
                json.dump(self.to_dict(), f)
        except Exception:
            pass

    def load(self, path):
        if not os.path.exists(path):
            return False
        try:
            d = json.load(open(path))
        except Exception:
            return False
        self.start = d.get("start", self.start)
        self.cash = d.get("cash", self.cash)
        self.pos = {k: list(v) for k, v in d.get("pos", {}).items()}
        self.resting = d.get("resting", {})
        self.realized = d.get("realized", 0.0)
        self.round_trips = d.get("round_trips", 0)
        self.wins = d.get("wins", 0)
        self.losses = d.get("losses", 0)
        self.fees = d.get("fees", 0.0)
        self.fills = d.get("fills", 0)
        self.names = d.get("names", {})
        return True

    def fill_buy(self, t, price, count, taker):
        f = fee_cents(price, count, taker=taker)
        self.fees += f
        cost_per = price + f / max(1, count)     # bake buy fee into cost basis
        self.cash -= price * count + f
        if t in self.pos:
            c0, a0 = self.pos[t]
            nc = c0 + count
            self.pos[t] = [nc, (a0 * c0 + cost_per * count) / nc]
        else:
            self.pos[t] = [count, cost_per]
        self.fills += 1
        self._log_trade(t, "BUY", count, price, f, taker, entry=price)

    def fill_sell(self, t, price, count, taker):
        f = fee_cents(price, count, taker=taker)
        self.fees += f
        self.cash += price * count - f
        entry = None
        net = None
        if t in self.pos:
            c0, a0 = self.pos[t]
            entry = a0
            net = (price * count - f) - a0 * count   # nets buy+sell fees
            self.realized += net
            self.round_trips += 1
            if net >= 0:
                self.wins += 1
            else:
                self.losses += 1
            left = c0 - count
            if left <= 0:
                del self.pos[t]
            else:
                self.pos[t] = [left, a0]
        self.fills += 1
        self._log_trade(t, "SELL", count, price, f, taker, entry=entry, pnl=net)

    def check_resting_fills(self, book):
        """Fill a resting order when the market actually trades at/through our
        price. We detect a real trade via a volume increase since last cycle,
        and use last_price (where it traded) plus the quote to bound the move.
        Falls back to the strict quote-cross condition too."""
        for t in list(self.resting.keys()):
            o = self.resting[t]
            q = book.get(t)
            if not q:
                continue
            p = o["price"]
            last = q.get("last_price", 0)
            traded = q["volume"] > self.prev_vol.get(t, q["volume"])
            low = min(q["yes_bid"], last) if last > 0 else q["yes_bid"]
            high = max(q["yes_ask"], last) if last > 0 else q["yes_ask"]
            if o["action"] == "buy":
                if q["yes_ask"] <= p or (traded and low <= p):
                    self.fill_buy(t, p, o["count"], taker=False)
                    del self.resting[t]
            else:  # sell
                if q["yes_bid"] >= p or (traded and high >= p):
                    self.fill_sell(t, p, o["count"], taker=False)
                    del self.resting[t]

    def position_for(self, t):
        if t in self.pos:
            c, a = self.pos[t]
            return Position(side="yes", count=c, avg_price_cents=int(round(a)))
        return Position()

    def unrealized(self, book):
        u = 0.0
        for t, (c, a) in self.pos.items():
            bid = book.get(t, {}).get("yes_bid", a)
            u += c * (bid - a)
        return u

    def equity(self, book):
        mtm = sum(c * book.get(t, {}).get("yes_bid", a) for t, (c, a) in self.pos.items())
        return self.cash + mtm


def log_row(cycle, cands, sim, book):
    new = not os.path.exists(LOG_PATH)
    os.makedirs("logs", exist_ok=True)
    total = sim.equity(book) - sim.start
    with open(LOG_PATH, "a") as f:
        if new:
            f.write("timestamp,cycle,candidates,fills,open_positions,round_trips,"
                    "wins,losses,realized_$,unrealized_$,total_pnl_$,fees_$\n")
        f.write("%s,%d,%d,%d,%d,%d,%d,%d,%.2f,%.2f,%.2f,%.2f\n" % (
            datetime.datetime.now().isoformat(timespec="seconds"), cycle, cands,
            sim.fills, len(sim.pos), sim.round_trips, sim.wins, sim.losses,
            sim.realized / 100, sim.unrealized(book) / 100, total / 100, sim.fees / 100))


def print_summary(sim, book):
    total = sim.equity(book) - sim.start
    wr = (100 * sim.wins / sim.round_trips) if sim.round_trips else 0
    avg = (sim.realized / sim.round_trips / 100) if sim.round_trips else 0
    print("\n===== PAPER PERFORMANCE =====")
    print(f"  Total P&L      : ${total/100:+.2f}   (started ${sim.start/100:.2f})")
    print(f"  Realized       : ${sim.realized/100:+.2f} over {sim.round_trips} round-trips")
    print(f"  Win rate       : {sim.wins}W / {sim.losses}L ({wr:.0f}%), avg ${avg:+.2f}/trade")
    print(f"  Unrealized     : ${sim.unrealized(book)/100:+.2f} ({len(sim.pos)} open)")
    print(f"  Fees paid       : ${sim.fees/100:.2f}")
    print(f"  Full history   : {LOG_PATH}")
    print("  (Simulation only - no orders were ever placed.)")


def _pid_alive(pid):
    """Cross-platform 'is this PID a live process?' check.
    NOTE: never use os.kill(pid, 0) on Windows - it TERMINATES the process."""
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x100000
        ERROR_ACCESS_DENIED = 5
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(SYNCHRONIZE, 0, pid)
        if h:
            k32.CloseHandle(h)
            return True
        return k32.GetLastError() == ERROR_ACCESS_DENIED
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _lock_fresh():
    """True only if another LIVE process holds the lock.
    A lock whose owning PID is dead is stale regardless of file mtime
    (fixes the 2026-07-02/03 outages where a stale-but-recent lock
    kept the bot crash-looping for hours)."""
    if not os.path.exists(LOCK_PATH):
        return False
    pid = None
    try:
        with open(LOCK_PATH) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pass
    if pid is not None and pid != os.getpid():
        if _pid_alive(pid):
            return True
        try:
            os.remove(LOCK_PATH)  # dead owner -> clean up and proceed
        except OSError:
            pass
        return False
    # unreadable/no PID: fall back to the old mtime freshness rule
    try:
        return (time.time() - os.path.getmtime(LOCK_PATH)) < 180
    except OSError:
        return False


def touch_lock():
    _HEARTBEAT["ts"] = time.time()
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _watchdog_tripped(last_beat, now, limit):
    """Pure decision: has the main loop been silent past the limit?"""
    return limit > 0 and (now - last_beat) > limit


def _watchdog():
    """Daemon thread: if the main loop stops heartbeating (hung network call,
    deadlock - the 2026-07-07 overnight wedge), force-exit so systemd
    Restart=always brings up a fresh process. The PID-aware lock ignores the
    dead owner, so restart is clean."""
    while True:
        time.sleep(60)
        if _watchdog_tripped(_HEARTBEAT["ts"], time.time(), WATCHDOG_SEC):
            print(f"WATCHDOG: no heartbeat for >{WATCHDOG_SEC}s - forcing restart")
            try:
                os.remove(LOCK_PATH)
            except OSError:
                pass
            os._exit(86)   # skip finally/atexit; systemd restarts us


def write_state(sim, book, enabled=True):
    """Publish the bot's CURRENT holdings + resting orders so the dashboard
    can show exactly what markets it is in and what it is waiting on."""
    pos = []
    for t, (c, a) in sim.pos.items():
        bid = book.get(t, {}).get("yes_bid", int(round(a)))
        pos.append({"ticker": t, "name": sim.names.get(t, t), "count": c,
                    "avg": round(a, 1), "bid": bid,
                    "unreal": round(c * (bid - a) / 100.0, 2)})
    rest = []
    for t, o in sim.resting.items():
        rest.append({"ticker": t, "name": sim.names.get(t, t), "action": o["action"],
                     "price": o["price"], "count": o["count"]})
    data = {"updated": datetime.datetime.now().isoformat(timespec="seconds"),
            "spread_enabled": enabled,
            "start": round(sim.start / 100.0, 2),
            "equity": round(sim.equity(book) / 100.0, 2),
            "cash": round(sim.cash / 100.0, 2),
            "realized": round(sim.realized / 100.0, 2),
            "unrealized": round(sim.unrealized(book) / 100.0, 2),
            "total": round((sim.equity(book) - sim.start) / 100.0, 2),
            "wins": sim.wins, "losses": sim.losses,
            "round_trips": sim.round_trips, "fees": round(sim.fees / 100.0, 2),
            "open": len(sim.pos),
            "positions": sorted(pos, key=lambda x: -abs(x["unreal"])),
            "resting": sorted(rest, key=lambda x: x["ticker"])}
    try:
        os.makedirs("logs", exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def main():
    args = sys.argv[1:]
    cfg_path = "config_balanced.yaml"
    cycles = 0
    start_dollars = 100.0
    reset = False
    for a in args:
        if a.startswith("--config="):
            cfg_path = a.split("=", 1)[1]
        elif a.startswith("--cycles="):
            cycles = int(a.split("=", 1)[1])
        elif a.startswith("--start="):
            start_dollars = float(a.split("=", 1)[1])
        elif a == "--reset":
            reset = True

    if reset:
        for p in (SIM_PATH, STATE_PATH, LOG_PATH, TRADES_PATH, LOCK_PATH):
            try:
                os.remove(p)
            except OSError:
                pass
        print("RESET: cleared previous portfolio + logs. Fresh start.\n")

    if _lock_fresh():
        print("Another paper bot is ALREADY running (lock file is fresh).")
        print("Close that window first so they don't fight over the log files.")
        print(f"If you're certain none is running, delete {LOCK_PATH} and retry.")
        return 1
    touch_lock()
    if WATCHDOG_SEC > 0:
        threading.Thread(target=_watchdog, daemon=True).start()

    cfg = load_config(cfg_path)
    m = cfg.markets
    spread_on = bool(cfg.raw.get("spread_capture", True))
    strat = build_strategy("smart", cfg.strategy_params)
    sim = Sim(int(start_dollars * 100))
    wp = None
    if weather_paper is not None:
        try:
            wp = weather_paper.WeatherPaper()
        except Exception:
            wp = None
    wl_dry = None
    if weather_live is not None:
        try:
            wl_dry = weather_live.WeatherLive(None, mode="DRY")
        except Exception:
            wl_dry = None
    drift_bot = None
    if drift_paper is not None:
        try:
            drift_bot = drift_paper.DriftPaper()
        except Exception:
            drift_bot = None
    dw_bot = None
    if drift_wide is not None:
        try:
            dw_bot = drift_wide.DriftWide()
        except Exception:
            dw_bot = None
    dl_dry = None
    if drift_live is not None:
        try:
            dl_dry = drift_live.DriftLive(None, mode="DRY")
        except Exception:
            dl_dry = None
    # retired strategies (funding 7/18, sports/sharp 7/21): purge orphaned books
    for _fs in ("funding_state.json", "sharpev_state.json", "sharpev_sim.json"):
        try:
            _fp = os.path.join("logs", _fs)
            if os.path.exists(_fp):
                os.remove(_fp)
                print(f"removed orphaned logs/{_fs} (strategy retired)")
        except Exception:
            pass
    # poly reward-farming retired 7/23 (Adam) - ARCHIVE the ledger, never
    # delete it (tracker stays cumulative): dashboard stops reading it, the
    # 18-day history stays on disk for the record.
    try:
        _pp = os.path.join("logs", "poly_state.json")
        if os.path.exists(_pp):
            os.replace(_pp, os.path.join("logs", "poly_state_archived.json"))
            print("archived logs/poly_state.json -> poly_state_archived.json (book retired)")
    except Exception:
        pass
    _serve_dashboard()
    if sim.load(SIM_PATH):
        print(f"Resumed previous session: ${sim.cash/100:.2f} cash, "
              f"{len(sim.pos)} positions held, {len(sim.resting)} resting orders, "
              f"{sim.round_trips} trades so far.")

    print("PAPER TRADING on LIVE data | no key, no money, no orders sent")
    print(f"config={cfg_path} | start=${start_dollars:.2f} | "
          f"{'infinite' if cycles == 0 else cycles} cycles | Ctrl+C to stop")
    print(f"performance is also saved to {LOG_PATH}\n")

    n = 0
    book = {}
    try:
        while cycles == 0 or n < cycles:
            n += 1
            if spread_on:
                book = fetch_live()
                sim.names.update({t: q.get('name', t) for t, q in book.items()})
                sim.check_resting_fills(book)
                # re-price: drop unfilled orders older than the stale window
                # so they get re-placed at current prices (keeps tracker fresh)
                _stale = max(1, cfg.engine.cancel_stale_after_s // max(1, cfg.engine.cycle_seconds))
                for _t in [t for t, o in sim.resting.items() if n - o.get('placed', n) >= _stale]:
                    del sim.resting[_t]
                cands = select(book, m)
                watch = set(cands) | set(sim.pos) | set(sim.resting)

                for t in watch:
                    q = book.get(t)
                    if not q:
                        continue
                    snap = MarketSnapshot(
                        ticker=t, yes_bid=q["yes_bid"], yes_ask=q["yes_ask"],
                        no_bid=100 - q["yes_ask"], no_ask=100 - q["yes_bid"],
                        yes_bid_size=q["yes_bid_size"], yes_ask_size=q["yes_ask_size"],
                        position=sim.position_for(t),
                    )
                    for it in strat.decide(snap):
                        if it.action == "buy":
                            if t in sim.pos or t in sim.resting:
                                continue
                            pct = getattr(cfg.risk, "position_pct", 0) or 0
                            eq_d = sim.equity(book) / 100.0          # current equity in $
                            if pct > 0:
                                target_d = max(0.50, eq_d * pct)     # grows/shrinks with equity
                                target_d = min(target_d, eq_d * 0.10)  # never >10% of equity in one bet
                            else:
                                target_d = cfg.risk.target_position_dollars
                            sz = max(1, int(target_d * 100) // max(1, it.price_cents))
                            if sim.cash < it.price_cents * sz:
                                continue
                            if it.order_type == "market":
                                sim.fill_buy(t, q["yes_ask"], sz, taker=True)
                                print(f"  +BUY (mom)  {t[:34]} x{sz} @ {q['yes_ask']}c")
                            else:
                                sim.resting[t] = {"action": "buy", "price": it.price_cents, "count": sz, "placed": n}
                        elif it.action == "sell":
                            held = sim.pos.get(t)
                            if not held:
                                continue
                            if it.order_type == "market":
                                sim.fill_sell(t, q["yes_bid"], held[0], taker=True)
                                print(f"  -SELL(exit) {t[:34]} x{held[0]} @ {q['yes_bid']}c")
                            else:
                                sim.resting[t] = {"action": "sell", "price": it.price_cents, "count": held[0], "placed": n}

                total = sim.equity(book) - sim.start
                print(f"[cycle {n}] candidates {len(cands)} | round-trips {sim.round_trips} "
                      f"({sim.wins}W/{sim.losses}L) | resting {len(sim.resting)} | open {len(sim.pos)} | "
                      f"fees ${sim.fees/100:.2f} | PAPER P&L ${total/100:+.2f}")
                log_row(n, len(cands), sim, book)
                sim.prev_vol = {t: q["volume"] for t, q in book.items()}
                write_state(sim, book)
                sim.save(SIM_PATH)
            else:
                print(f"[cycle {n}] spread-capture DISABLED (weather-only mode)")
                write_state(sim, {}, enabled=False)
            touch_lock()
            if wp is not None and n % 20 == 1:
                try:
                    wp.step()
                    s = wp.summary()
                    print(f"  WEATHER: P&L ${s['realized']:+.2f} | open {s['open_bets']} | "
                          f"settled {s['wins']}W/{s['losses']}L | placed {s['placed']}")
                except Exception as e:
                    print(f"  weather step skipped: {e}")
            if dw_bot is not None and n % 20 == 7:
                try:
                    nw = dw_bot.step()
                    ws2 = dw_bot.summary()
                    if nw or ws2["open"]:
                        print(f"  DRIFTW(paper): {nw} placed | bank ${ws2['cash']:.2f} | "
                              f"{ws2['wins']}W/{ws2['losses']}L | open {ws2['open']} | "
                              f"gate {ws2['gate']} {ws2['gate_n']}/30")
                except Exception as e:
                    print(f"  drift-wide step skipped: {e}")
            if drift_bot is not None and n % 20 == 11:
                try:
                    nd = drift_bot.step()
                    ds = drift_bot.summary()
                    if nd or ds["open"]:
                        print(f"  DRIFT(paper): {nd} placed | bank ${ds['cash']:.2f} | "
                              f"{ds['wins']}W/{ds['losses']}L | open {ds['open']} | "
                              f"gate {ds['gate']} {ds['gate_n']}/30")
                except Exception as e:
                    print(f"  drift step skipped: {e}")
            if (dl_dry is not None and n % 20 == 17
                    and not os.path.exists(drift_live.ARM_FILE)
                    and os.environ.get("KALSHI_DRIFT_LIVE", "") != "1"):
                # drift go-live DRY REHEARSAL: same brain, would-be orders
                # only. Auto-defers the moment the real live service is armed.
                try:
                    dl_dry.step()
                except Exception as e:
                    print(f"  drift-live dry step skipped: {e}")
            if (wl_dry is not None and n % 20 == 3
                    and not os.path.exists(weather_live.ARM_FILE)
                    and os.environ.get("KALSHI_WEATHER_LIVE", "") != "1"):
                # go-live DRY REHEARSAL: same brain, would-be orders only.
                # Auto-defers the moment the real live service is armed.
                try:
                    wl_dry.step()
                    ws_ = wl_dry.save  # state written inside step()
                except Exception as e:
                    print(f"  weather-live dry step skipped: {e}")
            if cycles == 0 or n < cycles:
                time.sleep(cfg.engine.cycle_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
    print_summary(sim, book)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
