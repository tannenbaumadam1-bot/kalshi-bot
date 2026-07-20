#!/usr/bin/env python3
"""Weather edge LIVE executor v2 - same brain as weather_paper, real orders.

Mirrors the v7/v8 paper discipline exactly: maker-only entries at the price
weather_edge.scan() chose, 30-85c price band, multi-strike (ge/le/band),
3-bet-per-event cap, probe stakes (<= 60c cost) until the LIVE book itself
passes the 30-bet calibration gate, forecast-based exits (never price stops),
12h re-entry cooldown, daily loss halt.

MODES (safety ladder - the mode is decided at startup, printed, and saved):
  DRY   - full pipeline, logs every would-be order, sends NOTHING.
          Default whenever the arm conditions are not all met.
  DEMO  - real orders to Kalshi's demo exchange (KALSHI_ENV=demo).
  LIVE  - real money. Requires ALL of:
            1. config_live.yaml api.key_id set (no PASTE placeholder)
               and the private key file present
            2. environment KALSHI_WEATHER_LIVE=1
            3. arm file logs/LIVE_ARMED exists  (or --yes-live + typed LIVE)

Hard caps (config_live.yaml risk.*, enforced before every order):
  max_position_dollars / max_open_dollars / max_daily_loss_dollars /
  min_cash_reserve_dollars.

Run:   python3 weather_live.py            (interactive)
       python3 weather_live.py --once     (single cycle, for tests/cron)
Service: deploy/kalshi-weather-live.service (disabled by default).
State -> logs/weather_live_state.json (dashboard picks it up)
Bets  -> logs/weather_live_bets.csv
"""
from __future__ import annotations
import os, sys, json, csv, time, datetime

import yaml

import weather_edge as we
import weather_paper as wp
from kalshibot.fees import fee_cents
from weather_paper import fetch_result

CONFIG = "config_live.yaml"
STATE = os.path.join("logs", "weather_live_state.json")
BETS = os.path.join("logs", "weather_live_bets.csv")
ARM_FILE = os.path.join("logs", "LIVE_ARMED")
LIVE_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

REST_MAX_H = float(os.environ.get("WX_LIVE_REST_MAX_H", "4"))
CYCLE_S = int(os.environ.get("WX_LIVE_CYCLE_S", "600"))
GATE_MIN_N = wp.GATE_MIN_N
GATE_MAX_GAP = wp.GATE_MAX_GAP
PROBE_COST_CENTS = wp.PROBE_COST_CENTS
ERA = "live1"


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today():
    return datetime.date.today().isoformat()


class WeatherLive:
    """Live executor. client=None -> DRY mode with a simulated $100 balance."""

    def __init__(self, client=None, mode="DRY"):
        cfg = {}
        try:
            cfg = yaml.safe_load(open(CONFIG)) or {}
        except Exception:
            pass
        r = cfg.get("risk", {}) if isinstance(cfg, dict) else {}
        self.max_bet_c = int(float(r.get("max_position_dollars", 2.0)) * 100)
        self.max_open_c = int(float(r.get("max_open_dollars", 15.0)) * 100)
        self.max_day_loss_c = int(float(r.get("max_daily_loss_dollars", 3.0)) * 100)
        self.reserve_c = int(float(r.get("min_cash_reserve_dollars", 2.0)) * 100)
        self.client = client
        self.mode = mode
        self.bets = {}        # ticker -> filled position
        self.pending = {}     # order_id -> resting order intent
        self.cooldown = {}
        self.realized_c = 0.0
        self.fees_c = 0.0
        self.wins = 0
        self.losses = 0
        self.placed = 0
        self.canceled = 0
        self.day = today()
        self.day_pnl_c = 0.0
        self.halted = False
        self.history = []
        self.dry_balance_c = 10000
        self.load()

    # ---- persistence ----
    def load(self):
        if os.path.exists(STATE):
            try:
                d = json.load(open(STATE))
                if d.get("mode") != self.mode:
                    return          # fresh book on any mode change (DRY->LIVE etc.)
                for k in ("bets", "pending", "cooldown", "realized_c", "fees_c",
                          "wins", "losses", "placed", "canceled", "day",
                          "day_pnl_c", "history", "dry_balance_c"):
                    if k in d:
                        setattr(self, k, d[k])
            except Exception:
                pass

    def save(self, balance_c=None):
        os.makedirs("logs", exist_ok=True)
        mode_gate, gate_n = self._gate()
        d = {"updated": now(), "mode": self.mode,
             "balance_c": balance_c,
             "bets": self.bets, "pending": self.pending,
             "cooldown": self.cooldown,
             "realized_c": self.realized_c, "fees_c": self.fees_c,
             "wins": self.wins, "losses": self.losses,
             "placed": self.placed, "canceled": self.canceled,
             "day": self.day, "day_pnl_c": self.day_pnl_c,
             "dry_balance_c": self.dry_balance_c,
             "history": self.history[-200:],
             "summary": {
                 "mode": self.mode,
                 "net": round(self.realized_c / 100, 2),
                 "wins": self.wins, "losses": self.losses,
                 "open": len(self.bets), "resting": len(self.pending),
                 "placed": self.placed, "canceled": self.canceled,
                 "fees": round(self.fees_c / 100, 2),
                 "day_pnl": round(self.day_pnl_c / 100, 2),
                 "halted": self.halted,
                 "gate": mode_gate, "gate_n": gate_n}}
        with open(STATE, "w") as f:
            json.dump(d, f)

    def _log(self, row):
        os.makedirs("logs", exist_ok=True)
        new = not os.path.exists(BETS)
        with open(BETS, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "event", "mode", "city", "strike", "hl",
                            "side", "our_prob_side", "entry_c", "count",
                            "outcome", "pnl_$", "order_id"])
            w.writerow(row)

    # ---- shared gate math (same contract as the paper book) ----
    def _gate(self):
        cur = [h for h in self.history if h.get("outcome") in (0, 1)][-60:]
        n = len(cur)
        if n < GATE_MIN_N:
            return "probe", n
        expectancy = sum(h["pnl"] for h in cur) / n
        pred = sum(h["pside"] for h in cur) / n
        act = sum(h["outcome"] for h in cur) / n
        if expectancy > 0 and (pred - act) <= GATE_MAX_GAP:
            return "scale", n
        return "probe", n

    def _cooled(self, tk):
        ts = self.cooldown.get(tk)
        if not ts:
            return False
        try:
            t0 = datetime.datetime.fromisoformat(ts)
            if (datetime.datetime.now() - t0).total_seconds() < wp.COOLDOWN_H * 3600:
                return True
        except Exception:
            pass
        self.cooldown.pop(tk, None)
        return False

    def _roll_day(self):
        if today() != self.day:
            self.day = today()
            self.day_pnl_c = 0.0
            self.halted = False

    def open_cost_c(self):
        oc = sum(b["entry"] * b["count"] + b.get("fee", 0)
                 for b in self.bets.values())
        oc += sum(o["entry"] * o["count"] for o in self.pending.values())
        return oc

    def balance_c(self):
        if self.client is None:
            return self.dry_balance_c
        return self.client.get_balance_cents()

    # ---- resting order lifecycle ----
    def check_orders(self):
        """Promote filled resting orders to positions; cancel stale ones."""
        if not self.pending:
            return
        resting_ids = set()
        if self.client is not None:
            try:
                resting_ids = {o.get("order_id") for o in self.client.get_resting_orders()}
            except Exception:
                return                      # can't verify -> touch nothing
        nowdt = datetime.datetime.now()
        for oid, o in list(self.pending.items()):
            tk = o["ticker"]
            if self.client is not None and oid not in resting_ids:
                # gone from the resting book -> filled (or canceled server-side)
                filled = 0
                try:
                    for f in self.client.get_fills(limit=100):
                        if f.get("order_id") == oid:
                            filled += int(f.get("count", 0))
                except Exception:
                    filled = o["count"]     # assume full fill; settle() reconciles
                if filled > 0:
                    fee = fee_cents(o["entry"], filled, taker=False)
                    self.fees_c += fee
                    if tk in self.bets and o.get("is_add"):
                        self._merge_fill(tk, o["entry"], filled, fee)
                    else:
                        self.bets[tk] = {**{k: o[k] for k in
                                            ("side", "entry", "city", "strike", "kind",
                                             "cap", "hl", "pside", "date", "src")},
                                         "count": filled, "fee": fee, "oid": oid,
                                         "ots": o.get("ots", now()), "era": ERA}
                    self._log([now(), "FILL", self.mode, o["city"], o["strike"],
                               o["hl"], o["side"], round(o["pside"], 3),
                               o["entry"], filled, "", "", oid])
                else:
                    self.canceled += 1
                del self.pending[oid]
                continue
            # stale: cancel after REST_MAX_H
            try:
                age_h = (nowdt - datetime.datetime.fromisoformat(o["ots"])).total_seconds() / 3600
            except Exception:
                age_h = 0
            if age_h > REST_MAX_H:
                if self.client is not None:
                    try:
                        self.client.cancel_order(oid)
                    except Exception:
                        continue
                self.canceled += 1
                self._log([now(), "CANCEL", self.mode, o["city"], o["strike"],
                           o["hl"], o["side"], round(o["pside"], 3),
                           o["entry"], o["count"], "", "", oid])
                del self.pending[oid]

    # ---- settle / exit ----
    def settle(self):
        for tk, b in list(self.bets.items()):
            res = fetch_result(tk)
            if res is None:
                continue
            won = (res == b["side"])
            payout = 100 if won else 0
            net = (payout - b["entry"]) * b["count"] - b.get("fee", 0)
            self.realized_c += net
            self.day_pnl_c += net
            if self.client is None:
                self.dry_balance_c += payout * b["count"]
            self.wins += int(won)
            self.losses += int(not won)
            self.history.append({"city": b["city"], "strike": b["strike"],
                                 "kind": b.get("kind", "ge"), "cap": b.get("cap"),
                                 "hl": b["hl"], "side": b["side"],
                                 "pside": round(b["pside"], 3), "entry": b["entry"],
                                 "count": b["count"], "outcome": 1 if won else 0,
                                 "pnl": round(net / 100, 2), "ts": now(),
                                 "ots": b.get("ots", ""), "era": ERA,
                                 "src": b.get("src", "")})
            self._log([now(), "SETTLE", self.mode, b["city"], b["strike"], b["hl"],
                       b["side"], round(b["pside"], 3), b["entry"], b["count"],
                       1 if won else 0, round(net / 100, 2), b.get("oid", "")])
            del self.bets[tk]

    def exit_check(self, margin_c=2):
        """Forecast-based stop (same rule as paper): sell only when the market
        pays more than our UPDATED fair value; never a naive price stop."""
        for tk, b in list(self.bets.items()):
            city, strike, is_low = b["city"], b["strike"], b["hl"] == "lo"
            date = b.get("date", "")
            if not date or city not in we.CITY_COORDS:
                continue
            lat, lon = we.CITY_COORDS[city]
            p_yes, wgt = wp.WeatherPaper._reprice(
                self, city, date, lat, lon, strike, is_low,
                b.get("kind", "ge"), b.get("cap"))
            if p_yes is None:
                continue
            p_new = p_yes if b["side"] == "yes" else (1 - p_yes)
            yb, ya = wp.WeatherPaper._quote(self, tk)
            if yb is None:
                continue
            bid = yb if b["side"] == "yes" else (100 - ya)
            ask = ya if b["side"] == "yes" else (100 - yb)
            if bid <= 0 or bid >= b["entry"]:
                b["exit_streak"] = 0
                continue
            mid_p = max(0.0, min(1.0, (bid + ask) / 200.0))
            exit_fee_per = fee_cents(bid, 1, taker=True)
            hold_ev = (wgt * p_new + (1 - wgt) * mid_p) * 100
            salvage = (bid <= wp.SALVAGE_C and p_new <= mid_p + 0.05)
            if salvage or bid - exit_fee_per > hold_ev + margin_c:
                b["exit_streak"] = int(b.get("exit_streak", 0)) + 1
                if not salvage and b["exit_streak"] < wp.EXIT_CONFIRMS:
                    continue
                cnt = b["count"]
                if self.client is not None:
                    try:
                        self.client.create_order(tk, action="sell", side=b["side"],
                                                 count=cnt, price_cents=bid)
                    except Exception:
                        continue
                exit_fee = fee_cents(bid, cnt, taker=True)
                net = (bid - b["entry"]) * cnt - b.get("fee", 0) - exit_fee
                self.realized_c += net
                self.day_pnl_c += net
                self.fees_c += exit_fee
                if self.client is None:
                    self.dry_balance_c += bid * cnt - exit_fee
                self.history.append({"city": city, "strike": strike,
                                     "kind": b.get("kind", "ge"), "cap": b.get("cap"),
                                     "hl": b["hl"], "side": b["side"],
                                     "pside": round(b["pside"], 3),
                                     "entry": b["entry"], "count": cnt,
                                     "outcome": None, "exited": True,
                                     "salvaged": bool(salvage),
                                     "pnl": round(net / 100, 2),
                                     "exit_px": bid, "ts": now(),
                                     "ots": b.get("ots", ""), "era": ERA})
                self._log([now(), "EXIT", self.mode, city, strike, b["hl"],
                           b["side"], round(p_new, 3), bid, cnt, "",
                           round(net / 100, 2), b.get("oid", "")])
                self.cooldown[tk] = now()
                del self.bets[tk]
            else:
                b["exit_streak"] = 0

    # ---- placement (maker resting orders, paper-identical filters) ----
    def place(self):
        if self.day_pnl_c <= -self.max_day_loss_c:
            self.halted = True
            return
        try:
            balance_c = self.balance_c()
        except Exception:
            return
        edges = we.scan(min_edge_cents=4, max_edge_cents=20, verbose=False)
        gate_mode, _n = self._gate()
        bankroll = balance_c + self.open_cost_c()
        ev_counts = {}
        for b in list(self.bets.values()) + list(self.pending.values()):
            ek = (b["city"], b.get("date", ""), b["hl"])
            ev_counts[ek] = ev_counts.get(ek, 0) + 1
        for ev, side, mk, fair, ftemp in edges:
            tk = mk["ticker"]
            if any(o["ticker"] == tk for o in self.pending.values()):
                continue
            if tk in self.bets:
                # ticker re-qualified in the scan -> maybe add to a runner
                self._maybe_pyramid_order(tk, side, mk, fair, balance_c)
                continue
            if self._cooled(tk):
                continue
            ekey = (mk["city"], mk.get("date", ""), "lo" if mk["is_low"] else "hi")
            if ev_counts.get(ekey, 0) >= wp.EVENT_MAX_BETS:
                continue
            s = "yes" if side == "YES" else "no"
            maker = bool(mk.get("maker", False))
            price = int(mk.get("entry_price",
                               mk["yes_ask"] if s == "yes" else (100 - mk["yes_bid"])))
            if price < wp.MIN_PRICE or price > wp.MAX_PRICE:
                continue
            p = fair if s == "yes" else (1 - fair)
            if p < wp.MIN_PSIDE:
                continue          # v8: sub-50% confidence measured -EV in 3 eras
            b_odds = (100 - price) / price
            f_star = p - (1 - p) / b_odds
            if f_star <= 0:
                continue
            if gate_mode == "probe":
                size = max(1, PROBE_COST_CENTS // price)
            else:
                frac = min(0.25 * f_star, wp.PER_BET_CAP)
                size = max(1, int((frac * bankroll) // price))
            while size > 1 and price * size > self.max_bet_c:
                size -= 1
            if price * size > self.max_bet_c:
                continue
            if self.open_cost_c() + price * size > self.max_open_c:
                continue
            if balance_c - price * size < self.reserve_c:
                continue
            oid = f"dry-{self.placed + 1}"
            if self.client is not None:
                try:
                    resp = self.client.create_order(tk, action="buy", side=s,
                                                    count=size, price_cents=price)
                    oid = ((resp.get("order") or {}).get("order_id")
                           or resp.get("order_id") or oid)
                except Exception as e:
                    print(f"  order failed {tk}: {e}")
                    continue
            balance_c -= price * size
            pside = fair if s == "yes" else (1 - fair)
            if self.client is None:
                self.dry_balance_c -= price * size
            self.pending[oid] = {
                "ticker": tk, "side": s, "entry": price, "count": size,
                "pside": pside, "city": mk["city"], "strike": mk["strike"],
                "kind": mk.get("kind", "ge"), "cap": mk.get("cap"),
                "hl": ("lo" if mk["is_low"] else "hi"),
                "date": mk.get("date", ""), "src": mk.get("src", "forecast"),
                "maker": maker, "ots": now()}
            ev_counts[ekey] = ev_counts.get(ekey, 0) + 1
            self.placed += 1
            self._log([now(), "REST", self.mode, mk["city"], mk["strike"],
                       ("lo" if mk["is_low"] else "hi"), s, round(pside, 3),
                       price, size, "", "", oid])
            print(f"  {self.mode} ORDER {tk}: {s.upper()} {size}x @ {price}c maker "
                  f"(p={pside:.2f})")
        # DRY mode: resting orders "fill" instantly at maker price (upper bound,
        # same optimistic assumption the paper book makes)
        if self.client is None:
            for oid, o in list(self.pending.items()):
                fee = fee_cents(o["entry"], o["count"], taker=False)
                self.fees_c += fee
                tk0 = o["ticker"]
                if tk0 in self.bets and o.get("is_add"):
                    self._merge_fill(tk0, o["entry"], o["count"], fee)
                else:
                    self.bets[tk0] = {**{k: o[k] for k in
                                         ("side", "entry", "count", "city",
                                          "strike", "kind", "cap", "hl",
                                          "pside", "date", "src")},
                                      "fee": fee, "oid": oid,
                                      "ots": o["ots"], "era": ERA}
                del self.pending[oid]

    def _merge_fill(self, tk, price, count, fee):
        """Fold a pyramid add-on fill into the existing position."""
        b = self.bets[tk]
        tot = b["count"] + count
        b["entry"] = round((b["entry"] * b["count"] + price * count) / tot, 1)
        b["count"] = tot
        b["fee"] = b.get("fee", 0) + fee
        b["adds"] = int(b.get("adds", 0)) + 1

    def _maybe_pyramid_order(self, tk, side, mk, fair, balance_c):
        """Rest a probe-size ADD order on a winner the model still believes
        in (same rules as the paper book: +WX_PYRAMID_UP_C past avg entry,
        re-qualified edge, same side, capped adds)."""
        if not wp.WX_PYRAMID:
            return False
        b = self.bets[tk]
        s = "yes" if side == "YES" else "no"
        if s != b["side"] or int(b.get("adds", 0)) >= wp.WX_PYRAMID_MAX:
            return False
        price = int(mk.get("entry_price",
                           mk["yes_ask"] if s == "yes" else (100 - mk["yes_bid"])))
        if price < b["entry"] + wp.WX_PYRAMID_UP_C:
            return False
        if price < wp.MIN_PRICE or price > wp.MAX_PRICE:
            return False
        p = fair if s == "yes" else (1 - fair)
        if p < wp.MIN_PSIDE:
            return False
        size = max(1, PROBE_COST_CENTS // price)
        if price * size > self.max_bet_c:
            return False
        if self.open_cost_c() + price * size > self.max_open_c:
            return False
        if balance_c - price * size < self.reserve_c:
            return False
        oid = f"dry-add-{self.placed + 1}"
        if self.client is not None:
            try:
                resp = self.client.create_order(tk, action="buy", side=s,
                                                count=size, price_cents=price)
                oid = ((resp.get("order") or {}).get("order_id")
                       or resp.get("order_id") or oid)
            except Exception:
                return False
        if self.client is None:
            self.dry_balance_c -= price * size
        self.pending[oid] = {
            "ticker": tk, "side": s, "entry": price, "count": size,
            "pside": round(p, 3), "city": b["city"], "strike": b["strike"],
            "kind": b.get("kind", "ge"), "cap": b.get("cap"), "hl": b["hl"],
            "date": b.get("date", ""), "src": b.get("src", "forecast"),
            "maker": True, "is_add": True, "ots": now()}
        self.placed += 1
        self._log([now(), "PYRAMID", self.mode, b["city"], b["strike"], b["hl"],
                   s, round(p, 3), price, size, "", "", oid])
        return True

    def step(self):
        self._roll_day()
        self.check_orders()
        self.settle()
        self.exit_check()
        self.place()
        try:
            bal = self.balance_c()
        except Exception:
            bal = None
        self.save(balance_c=bal)


def build():
    """Decide mode from config/env/arm-file and construct the trader."""
    cfg = {}
    try:
        cfg = yaml.safe_load(open(CONFIG)) or {}
    except Exception:
        pass
    api = cfg.get("api", {}) if isinstance(cfg, dict) else {}
    key_id = str(api.get("key_id", "") or "")
    key_path = str(api.get("private_key_path", "kalshi-live.key") or "")
    demo = os.environ.get("KALSHI_ENV", "").lower() == "demo"
    if demo:
        key_id = os.environ.get("KALSHI_DEMO_KEY_ID", key_id)
        key_path = os.environ.get("KALSHI_DEMO_KEY_PATH", "kalshi-demo.key")
    have_key = key_id and "PASTE" not in key_id and os.path.exists(key_path)
    armed = (os.environ.get("KALSHI_WEATHER_LIVE", "") == "1"
             and os.path.exists(ARM_FILE))
    if demo and have_key:
        from kalshibot.client import KalshiClient
        return WeatherLive(KalshiClient(key_id, key_path, DEMO_BASE), mode="DEMO")
    if have_key and armed:
        from kalshibot.client import KalshiClient
        return WeatherLive(KalshiClient(key_id, key_path, LIVE_BASE), mode="LIVE")
    return WeatherLive(None, mode="DRY")


def main():
    wl = build()
    if wl.mode == "LIVE" and "--yes-live" not in sys.argv and sys.stdin.isatty():
        if input("Type LIVE (all caps) to trade REAL money: ") != "LIVE":
            print("Cancelled.")
            return 0
    print(f"[{now()}] weather executor started in {wl.mode} mode "
          f"(caps: ${wl.max_bet_c/100:.2f}/bet, ${wl.max_open_c/100:.2f} open, "
          f"${wl.max_day_loss_c/100:.2f} daily halt; rest<= {REST_MAX_H}h)")
    if "--once" in sys.argv:
        wl.step()
        return 0
    while True:
        try:
            wl.step()
        except KeyboardInterrupt:
            print("stopped.")
            return 0
        except Exception as e:
            print(f"[{now()}] cycle error: {e}")
        time.sleep(CYCLE_S)


if __name__ == "__main__":
    raise SystemExit(main())
