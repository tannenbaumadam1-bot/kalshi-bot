#!/usr/bin/env python3
"""Weather edge LIVE trader - real money, hard caps, same brain as paper.

Uses the exact same edge logic as weather_paper.py (weather_edge.scan with
the disciplined filters: no too-good-to-be-true edges, 20-80% band only,
quarter-Kelly sizing) but places REAL limit orders through the Kalshi API.

Safety model (all enforced every cycle, before any order):
  * refuses to start unless config_live.yaml has a real key id + key file
  * per-bet cost cap        (risk.max_position_dollars, default $2)
  * total open exposure cap (risk.max_open_dollars,     default $15)
  * daily loss halt         (risk.max_daily_loss_dollars, default $3)
  * cash reserve            (risk.min_cash_reserve_dollars)
  * never adds to an existing position in the same market

Run:  python weather_live.py            (asks you to type LIVE first)
      python weather_live.py --yes-live (skip prompt; for the .bat)
State -> logs/weather_live_state.json (dashboard picks it up)
Bets  -> logs/weather_live_bets.csv
"""
from __future__ import annotations
import os, sys, json, csv, time, datetime

import yaml

from kalshibot.client import KalshiClient
from kalshibot.fees import fee_cents
import weather_edge as we
from weather_paper import fetch_result

CONFIG = "config_live.yaml"
STATE = os.path.join("logs", "weather_live_state.json")
BETS = os.path.join("logs", "weather_live_bets.csv")
LIVE_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today():
    return datetime.date.today().isoformat()


class WeatherLive:
    def __init__(self):
        cfg = yaml.safe_load(open(CONFIG))
        if str(cfg.get("environment", "")).lower() != "live":
            raise SystemExit("config_live.yaml is not environment: live")
        api = cfg.get("api", {})
        key_id = str(api.get("key_id", ""))
        key_path = str(api.get("private_key_path", "kalshi-live.key"))
        if "PASTE" in key_id or not key_id:
            raise SystemExit("No live key id in config_live.yaml - not starting. "
                             "(This is the safety lock.)")
        if not os.path.exists(key_path):
            raise SystemExit(f"Missing {key_path} - not starting.")
        r = cfg.get("risk", {})
        self.max_bet_c = int(float(r.get("max_position_dollars", 2.0)) * 100)
        self.max_open_c = int(float(r.get("max_open_dollars", 15.0)) * 100)
        self.max_day_loss_c = int(float(r.get("max_daily_loss_dollars", 3.0)) * 100)
        self.reserve_c = int(float(r.get("min_cash_reserve_dollars", 2.0)) * 100)
        self.client = KalshiClient(key_id, key_path, LIVE_BASE)
        self.bets = {}
        self.realized_c = 0.0
        self.fees_c = 0.0
        self.wins = 0
        self.losses = 0
        self.placed = 0
        self.day = today()
        self.day_pnl_c = 0.0
        self.history = []
        self.load()

    # ---- persistence ----
    def load(self):
        if os.path.exists(STATE):
            try:
                d = json.load(open(STATE))
                for k in ("bets", "realized_c", "fees_c", "wins", "losses",
                          "placed", "day", "day_pnl_c", "history"):
                    if k in d:
                        setattr(self, k, d[k])
            except Exception:
                pass

    def save(self, balance_c=None):
        os.makedirs("logs", exist_ok=True)
        d = {"updated": now(), "mode": "LIVE",
             "balance_c": balance_c,
             "bets": self.bets, "realized_c": self.realized_c,
             "fees_c": self.fees_c, "wins": self.wins, "losses": self.losses,
             "placed": self.placed, "day": self.day, "day_pnl_c": self.day_pnl_c,
             "history": self.history[-200:],
             "summary": {
                 "net": round(self.realized_c / 100, 2),
                 "wins": self.wins, "losses": self.losses,
                 "open": len(self.bets), "placed": self.placed,
                 "fees": round(self.fees_c / 100, 2),
                 "day_pnl": round(self.day_pnl_c / 100, 2)}}
        with open(STATE, "w") as f:
            json.dump(d, f)

    def _log(self, row):
        os.makedirs("logs", exist_ok=True)
        new = not os.path.exists(BETS)
        with open(BETS, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "event", "city", "strike", "hl", "side",
                            "our_prob_side", "entry_c", "count", "outcome", "pnl_$",
                            "order_id"])
            w.writerow(row)

    # ---- core ----
    def _roll_day(self):
        if today() != self.day:
            self.day = today()
            self.day_pnl_c = 0.0

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
            self.wins += int(won)
            self.losses += int(not won)
            self.history.append({"city": b["city"], "strike": b["strike"],
                                 "hl": b["hl"], "side": b["side"],
                                 "pside": round(b["pside"], 3), "entry": b["entry"],
                                 "count": b["count"], "outcome": 1 if won else 0,
                                 "pnl": round(net / 100, 2), "ts": now()})
            self._log([now(), "SETTLE", b["city"], b["strike"], b["hl"], b["side"],
                       round(b["pside"], 3), b["entry"], b["count"],
                       1 if won else 0, round(net / 100, 2), b.get("oid", "")])
            print(f"  SETTLED {tk}: {'WON' if won else 'LOST'} {net/100:+.2f}$")
            del self.bets[tk]

    def open_cost_c(self):
        return sum(b["entry"] * b["count"] + b.get("fee", 0)
                   for b in self.bets.values())

    def place(self):
        if self.day_pnl_c <= -self.max_day_loss_c:
            print(f"  daily loss halt ({self.day_pnl_c/100:+.2f}$) - no new bets today")
            return
        try:
            balance_c = self.client.get_balance_cents()
        except Exception as e:
            print(f"  balance check failed ({e}) - skipping placement")
            return
        edges = we.scan(min_edge_cents=4, max_edge_cents=20, verbose=False)
        bankroll = balance_c + self.open_cost_c()
        for ev, side, mk, fair, ftemp in edges:
            tk = mk["ticker"]
            if tk in self.bets:
                continue
            s = "yes" if side == "YES" else "no"
            price = mk["yes_ask"] if s == "yes" else (100 - mk["yes_bid"])
            if price <= 0 or price >= 100:
                continue
            p = fair if s == "yes" else (1 - fair)
            b_odds = (100 - price) / price
            f_star = p - (1 - p) / b_odds
            if f_star <= 0:
                continue
            frac = min(0.25 * f_star, 0.03)
            size = int((frac * bankroll) // price)
            if size < 1:
                continue
            # hard per-bet cap
            while size > 1 and price * size + fee_cents(price, size) > self.max_bet_c:
                size -= 1
            fee = fee_cents(price, size)
            cost = price * size + fee
            if cost > self.max_bet_c:
                continue
            if self.open_cost_c() + cost > self.max_open_c:
                continue
            if balance_c - cost < self.reserve_c:
                continue
            try:
                resp = self.client.create_order(tk, action="buy", side=s,
                                                count=size, price_cents=price)
                oid = (resp.get("order", {}) or {}).get("order_id", "")
            except Exception as e:
                print(f"  order failed {tk}: {e}")
                continue
            balance_c -= cost
            self.fees_c += fee
            pside = fair if s == "yes" else (1 - fair)
            self.bets[tk] = {"side": s, "entry": price, "count": size, "fee": fee,
                             "pside": pside, "city": mk["city"], "strike": mk["strike"],
                             "hl": ("lo" if mk["is_low"] else "hi"), "oid": oid}
            self.placed += 1
            self._log([now(), "OPEN", mk["city"], mk["strike"],
                       ("lo" if mk["is_low"] else "hi"), s, round(pside, 3),
                       price, size, "", "", oid])
            print(f"  LIVE BET {tk}: {s.upper()} {size}x @ {price}c "
                  f"(our p={pside:.2f}, cost ${cost/100:.2f})")

    def step(self):
        self._roll_day()
        self.settle()
        self.place()
        try:
            bal = self.client.get_balance_cents()
        except Exception:
            bal = None
        self.save(balance_c=bal)


def main():
    if "--yes-live" not in sys.argv:
        conf = input("Type LIVE (all caps) to trade REAL money: ")
        if conf != "LIVE":
            print("Cancelled.")
            return 0
    wl = WeatherLive()
    print(f"[{now()}] weather LIVE trader started "
          f"(caps: ${wl.max_bet_c/100:.2f}/bet, ${wl.max_open_c/100:.2f} open, "
          f"${wl.max_day_loss_c/100:.2f} daily loss halt)")
    while True:
        try:
            wl.step()
        except KeyboardInterrupt:
            print("stopped.")
            return 0
        except Exception as e:
            print(f"[{now()}] cycle error: {e}")
        time.sleep(900)   # 15 min between cycles; settlement is daily anyway


if __name__ == "__main__":
    raise SystemExit(main())
