#!/usr/bin/env python3
"""Weather edge PAPER trader - its own isolated $100 ledger.

Each step: settle any temperature bets whose market has resolved (credit
100/0), then scan for new edges and place small paper bets, holding to
settlement. Every bet is logged with OUR probability and the eventual
outcome -> that's the calibration data that proves whether the edge is real.

Fully self-contained; the main bot imports and calls .step() inside a
try/except, so nothing here can disturb spread-capture trading.
"""
from __future__ import annotations
import os, json, csv, datetime
import requests
from kalshibot.fees import fee_cents
import weather_edge as we

WSIM = os.path.join("logs", "weather_sim.json")
WBETS = os.path.join("logs", "weather_bets.csv")
WSTATE = os.path.join("logs", "weather_state.json")

# Total-book exposure cap: open cost basis may never exceed this fraction of
# bankroll (cash + open stake). Keeps one bad day from being a blowout and
# leaves dry powder for tomorrow's edges. (Per-bet Kelly cap is 3%.)
MAX_BOOK_FRAC = 0.50


def fetch_result(ticker):
    """Settled outcome of a market: 'yes', 'no', or None if not settled yet."""
    try:
        d = requests.get(we.KALSHI + f"/markets/{ticker}", timeout=15).json()
        mk = d.get("market", d)
        res = (mk.get("result") or "").lower()
        return res if res in ("yes", "no") else None
    except Exception:
        return None


class WeatherPaper:
    def __init__(self, start_cents=10000, per_bet_dollars=2.0):
        self.start = start_cents
        self.cash = float(start_cents)
        self.per_bet = per_bet_dollars
        self.bets = {}          # ticker -> bet dict
        self.realized = 0.0
        self.wins = 0
        self.losses = 0
        self.fees = 0.0
        self.placed = 0
        self.history = []   # recent settled bets (with outcomes)
        self.load()

    # ---- persistence ----
    def to_dict(self):
        return {"start": self.start, "cash": self.cash, "bets": self.bets,
                "realized": self.realized, "wins": self.wins, "losses": self.losses,
                "fees": self.fees, "placed": self.placed,
                "history": self.history[-100:]}

    def save(self):
        try:
            os.makedirs("logs", exist_ok=True)
            with open(WSIM, "w") as f:
                json.dump(self.to_dict(), f)
            st = {"updated": datetime.datetime.now().isoformat(timespec="seconds"),
                  "summary": self.summary(),
                  "open": [{"ticker": tk, "city": b["city"], "strike": b["strike"],
                            "hl": b["hl"], "side": b["side"], "entry": b["entry"],
                            "count": b["count"], "pside": round(b["pside"], 2),
                            "ots": b.get("ots", ""), "era": b.get("era", "v2")}
                           for tk, b in self.bets.items()],
                  "settled": list(reversed(self.history[-100:]))}
            with open(WSTATE, "w") as f:
                json.dump(st, f)
        except Exception:
            pass

    def load(self):
        if not os.path.exists(WSIM):
            return
        try:
            d = json.load(open(WSIM))
            self.start = d.get("start", self.start)
            self.cash = d.get("cash", self.cash)
            self.bets = d.get("bets", {})
            self.realized = d.get("realized", 0.0)
            self.wins = d.get("wins", 0)
            self.losses = d.get("losses", 0)
            self.fees = d.get("fees", 0.0)
            self.placed = d.get("placed", 0)
            self.history = d.get("history", [])
        except Exception:
            pass

    def _log(self, row):
        try:
            new = not os.path.exists(WBETS)
            os.makedirs("logs", exist_ok=True)
            with open(WBETS, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "event", "city", "strike", "hl", "side",
                                "our_prob_side", "entry_c", "count", "outcome", "pnl_$"])
                w.writerow(row)
        except Exception:
            pass

    # ---- core ----
    def settle(self):
        for tk, b in list(self.bets.items()):
            res = fetch_result(tk)
            if res is None:
                continue
            won = (res == b["side"])
            payout = 100 if won else 0
            net = (payout - b["entry"]) * b["count"] - b.get("fee", 0)
            self.cash += payout * b["count"]
            self.realized += net
            self.wins += int(won)
            self.losses += int(not won)
            self.history.append({"city": b["city"], "strike": b["strike"], "hl": b["hl"],
                                 "side": b["side"], "pside": round(b["pside"], 3),
                                 "entry": b["entry"], "count": b["count"],
                                 "outcome": (1 if won else 0), "pnl": round(net / 100.0, 2),
                                 "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                 "ots": b.get("ots", ""), "era": b.get("era", "v2")})
            self.history = self.history[-100:]
            self._log([datetime.datetime.now().isoformat(timespec="seconds"), "SETTLE",
                       b["city"], b["strike"], b["hl"], b["side"], round(b["pside"], 3),
                       b["entry"], b["count"], (1 if won else 0), round(net / 100, 2)])
            del self.bets[tk]

    def place(self):
        edges = we.scan(min_edge_cents=4, max_edge_cents=20, verbose=False)
        # bankroll = cash + cost basis of open bets (so sizing scales with equity)
        open_stake = sum(b["entry"] * b["count"] for b in self.bets.values())
        bankroll = self.cash + open_stake
        for ev, side, mk, fair, ftemp in edges:
            tk = mk["ticker"]
            if tk in self.bets:
                continue
            s = "yes" if side == "YES" else "no"
            price = mk["yes_ask"] if s == "yes" else (100 - mk["yes_bid"])
            if price <= 0 or price >= 100:
                continue
            # Kelly-fraction sizing, quarter-Kelly, hard-capped at 3% of bankroll
            p = fair if s == "yes" else (1 - fair)
            b_odds = (100 - price) / price
            f_star = p - (1 - p) / b_odds
            if f_star <= 0:
                continue
            frac = min(0.25 * f_star, 0.03)
            size = int((frac * bankroll) // price)
            if size < 1:
                continue
            fee = fee_cents(price, size, taker=True)
            cost = price * size + fee
            # total-book cap: never let open cost basis exceed MAX_BOOK_FRAC
            # of bankroll (edges are sorted best-first, so the best fit first)
            if open_stake + price * size > MAX_BOOK_FRAC * bankroll:
                continue
            if self.cash - cost < 100:        # keep a $1 reserve
                continue
            self.cash -= cost
            open_stake += price * size
            self.fees += fee
            pside = fair if s == "yes" else (1 - fair)
            self.bets[tk] = {"side": s, "entry": price, "count": size, "fee": fee,
                             "pside": pside, "city": mk["city"], "strike": mk["strike"],
                             "hl": ("lo" if mk["is_low"] else "hi"),
                             "ots": datetime.datetime.now().isoformat(timespec="seconds"),
                             "era": "v4-ens",
                             "mkt_bid": mk["yes_bid"], "mkt_ask": mk["yes_ask"]}
            self.placed += 1
            self._log([datetime.datetime.now().isoformat(timespec="seconds"), "OPEN",
                       mk["city"], mk["strike"], ("lo" if mk["is_low"] else "hi"), s,
                       round(pside, 3), price, size, "", ""])

    def step(self):
        self.settle()
        self.place()
        self.save()

    def summary(self):
        rt = self.wins + self.losses
        wr = round(100 * self.wins / rt) if rt else 0
        at_stake = sum(b["entry"] * b["count"] for b in self.bets.values()) / 100.0
        return {"start": round(self.start / 100, 2), "cash": round(self.cash / 100, 2),
                "open_bets": len(self.bets), "open_exposure": round(at_stake, 2),
                "settled": rt, "wins": self.wins, "losses": self.losses, "win_rate": wr,
                "realized": round(self.realized / 100, 2),
                "total": round(self.realized / 100, 2),   # banked P&L (open held to settle)
                "fees": round(self.fees / 100, 2), "placed": self.placed}
