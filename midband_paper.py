"""Mid-band paper book (era midband1) - PAPER ONLY, zero dollars.

The idea came from a Polymarket trader (jjavi, verified 8/13: 1,859
predictions, $11.9K deployed) whose whole book is temperature BANDS
bought at 15-55c: London 24C at 32.1c -> 74.5c, Paris 33C at 18c ->
69c, Milan 37C at 3.5c -> 17c. Same market structure Kalshi lists, and
the same forecast skill our ensemble already has - but a completely
different payoff. Our live lane buys 88c favorites and collects 12c
when right; he pays 30c and collects 70c.

The catch, stated honestly up front: mid-band bets LOSE MOST OF THE
TIME (a 30c band is a 30% shot), our model is least calibrated exactly
here (40-60%% bucket: predicted 46%%, actual 30%%), and Kalshi charges
~1.5c/contract at 30c where Polymarket charges ~0. So this book exists
to answer one question with our own ledger before a dollar is risked:

    does the payoff asymmetry survive our calibration and our fees?

Rules (deliberately close to the live book so the comparison is fair):
  - entry band 15-55c on the ASK, real quotes, exact Kalshi fees
  - the ensemble must give the band an edge over the ask
  - one position per city+date (no adjacent-band stacking - that is
    what made 8/12's Miami tail, and it would flatter this book)
  - exits: convergence target (sell into the bid at +TARGET_PCT) or the
    pre-close flatten, mirroring the live velocity build. Never held to
    settlement unless there is no bid at all.
  - graded on TURNS: win rate, per-turn net, and P&L vs the 80c+ lane
"""
from __future__ import annotations

import datetime
import json
import os

import weather_edge as we
import weather_ensemble as wx

ERA = "midband1"
STATE = os.path.join("logs", "midband_paper_state.json")

ENTRY_MIN = int(os.environ.get("MIDBAND_ENTRY_MIN", "15"))
ENTRY_MAX = int(os.environ.get("MIDBAND_ENTRY_MAX", "55"))
EDGE_MIN_C = float(os.environ.get("MIDBAND_EDGE_MIN_C", "4"))
SIZE = int(os.environ.get("MIDBAND_SIZE", "5"))
MAX_OPEN = int(os.environ.get("MIDBAND_MAX_OPEN", "12"))
MAX_SPREAD = int(os.environ.get("MIDBAND_MAX_SPREAD", "6"))
TARGET_PCT = float(os.environ.get("MIDBAND_TARGET_PCT", "0.40"))
FLATTEN_H = float(os.environ.get("MIDBAND_FLATTEN_H", "1.0"))
GATE_N = int(os.environ.get("MIDBAND_GATE_N", "200"))


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today():
    return datetime.date.today().isoformat()


def fee_cents(price_c, count, taker=True):
    """Kalshi's fee, same formula the live book uses."""
    import math
    p = max(0.01, min(0.99, price_c / 100.0))
    return int(math.ceil(0.07 * count * p * (1 - p) * 100))


class MidbandPaper:
    def __init__(self):
        self.bets = {}
        self.history = []
        self.turns = {"n": 0, "net_c": 0.0, "wins": 0}
        self.placed = 0
        self.realized_c = 0.0
        self.fees_c = 0.0
        self.miss = {}
        self.load()

    # ---- persistence ----
    def load(self):
        try:
            d = json.load(open(STATE))
            for k in ("bets", "history", "turns", "placed", "realized_c",
                      "fees_c", "miss"):
                if k in d:
                    setattr(self, k, d[k])
        except Exception:
            pass

    def save(self):
        n = self.turns.get("n", 0)
        w = self.turns.get("wins", 0)
        net_c = self.turns.get("net_c", 0.0)
        try:
            os.makedirs("logs", exist_ok=True)
            json.dump({
                "updated": now(), "era": ERA,
                "bets": self.bets, "history": self.history[-300:],
                "turns": self.turns, "placed": self.placed,
                "realized_c": self.realized_c, "fees_c": self.fees_c,
                "miss": self.miss,
                "summary": {
                    "mode": "PAPER",
                    "open": len(self.bets), "placed": self.placed,
                    "net": round(self.realized_c / 100.0, 2),
                    "fees": round(self.fees_c / 100.0, 2),
                    "turns": n, "wins": w, "losses": n - w,
                    "win_rate": round(w / n, 3) if n else None,
                    "per_turn": round(net_c / n / 100.0, 3) if n else None,
                    "gate": {"n": n, "need": GATE_N,
                             "ready": bool(n >= GATE_N and net_c > 0)},
                    "rules": {"band": [ENTRY_MIN, ENTRY_MAX],
                              "edge_min_c": EDGE_MIN_C, "size": SIZE,
                              "max_open": MAX_OPEN,
                              "target_pct": TARGET_PCT,
                              "flatten_h": FLATTEN_H,
                              "exit": "convergence target or pre-close "
                                      "flatten; never held to settlement"},
                },
            }, open(STATE, "w"))
        except Exception:
            pass

    def _miss(self, why):
        self.miss[why] = self.miss.get(why, 0) + 1

    def _turn(self, net_c):
        self.turns["n"] = int(self.turns.get("n", 0)) + 1
        self.turns["net_c"] = round(float(self.turns.get("net_c", 0))
                                    + net_c, 1)
        if net_c > 0:
            self.turns["wins"] = int(self.turns.get("wins", 0)) + 1

    # ---- the model's read on one band ----
    def band_prob(self, mk, cache):
        key = (mk["city"], mk["date"])
        if key not in cache:
            try:
                lat, lon = we.CITY_COORDS[mk["city"]]
                cache[key] = wx.forecast(mk["city"], mk["date"], lat, lon,
                                         mk["hrs"], log=False)
            except Exception:
                cache[key] = None
        fc = cache[key]
        if not fc:
            return None
        dist = fc["min"] if mk["is_low"] else fc["max"]
        if not dist.ok() or fc["n_sources"] < wx.MIN_SOURCES:
            return None
        return we.kind_prob(dist.prob_at_least, mk.get("kind", "ge"),
                            mk["strike"], mk.get("cap"))

    # ---- exits: convergence, then the pre-close flatten ----
    def exits(self, mkts):
        by_tk = {m["ticker"]: m for m in mkts}
        for tk, b in list(self.bets.items()):
            mk = by_tk.get(tk)
            if mk is None:
                continue
            bid = mk["yes_bid"]
            hrs = mk.get("hrs")
            target = int(round(b["entry"] * (1.0 + TARGET_PCT)))
            hit = bid >= target
            late = hrs is not None and hrs <= FLATTEN_H
            if not (hit or late) or bid <= 0:
                continue
            cnt = b["count"]
            fee = fee_cents(bid, cnt, taker=True)
            net = (bid - b["entry"]) * cnt - b.get("fee", 0) - fee
            self.realized_c += net
            self.fees_c += fee
            self._turn(net)
            self.history.append({
                "tk": tk, "city": b["city"], "strike": b["strike"],
                "kind": b.get("kind"), "entry": b["entry"], "exit": bid,
                "count": cnt, "fair": b.get("fair"),
                "why": "target" if hit else "flatten",
                "pnl": round(net / 100.0, 2), "ts": now(),
                "ots": b.get("ots", ""), "era": ERA})
            self.history = self.history[-300:]
            del self.bets[tk]

    # ---- entries ----
    def place(self, mkts):
        if len(self.bets) >= MAX_OPEN:
            return 0
        cache, placed = {}, 0
        open_keys = {(b["city"], b["date"]) for b in self.bets.values()}
        for mk in mkts:
            if len(self.bets) >= MAX_OPEN:
                break
            tk = mk["ticker"]
            if tk in self.bets:
                continue
            key = (mk["city"], mk["date"])
            if key in open_keys:
                continue          # one opinion per city+date, like the live book
            ask, bid = mk["yes_ask"], mk["yes_bid"]
            if ask <= 0 or bid <= 0:
                continue
            if ask < ENTRY_MIN or ask > ENTRY_MAX:
                continue
            if ask - bid > MAX_SPREAD:
                self._miss("spread")
                continue
            hrs = mk.get("hrs")
            if hrs is None or hrs <= FLATTEN_H:
                self._miss("too_late")
                continue
            fair = self.band_prob(mk, cache)
            if fair is None:
                self._miss("no_model")
                continue
            fee = fee_cents(ask, SIZE, taker=True)
            edge_c = fair * 100 - ask - fee / SIZE
            if edge_c < EDGE_MIN_C:
                self._miss("thin_edge")
                continue
            self.bets[tk] = {
                "city": mk["city"], "strike": mk["strike"],
                "kind": mk.get("kind", "ge"), "cap": mk.get("cap"),
                "is_low": mk["is_low"], "date": mk["date"],
                "entry": ask, "count": SIZE, "fee": fee,
                "fair": round(fair, 3), "edge": round(edge_c, 1),
                "ots": now(), "era": ERA}
            self.fees_c += fee
            open_keys.add(key)
            self.placed += 1
            placed += 1
            print(f"  MIDBAND(paper) BUY {tk}: {SIZE}x @ {ask}c "
                  f"(model {fair:.2f}, edge {edge_c:.1f}c)")
        return placed

    # ---- the cycle ----
    def step(self, mkts=None):
        if mkts is None:
            try:
                mkts = we.find_temp_markets(max_days=1)
            except Exception:
                return len(self.bets)
        self.exits(mkts)
        self.place(mkts)
        self.save()
        return len(self.bets)
