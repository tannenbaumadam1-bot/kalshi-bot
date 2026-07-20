#!/usr/bin/env python3
"""Momentum drift book (era "drift1") - PAPER, own $100 ledger, own gate.

Adam's momentum thesis (2026-07-20): in a binary market, price finishes at 0
or 100. When a weather contract strengthens (60c -> 70c) the market is usually
right but UNDERreacting - our shadow data shows the favorite side is
systematically underpriced (mid 65c wins ~75%, mid 42c wins ~48%). So: buy
the strong side at maker when it is BOTH high-priced and still climbing, no
model opinion at all, hold to settlement.

Discipline (identical contract to every other book):
  - probe stakes (<= 60c cost/bet) until a 30-bet gate on era "drift1" shows
    positive expectancy AND calibration within 5pts; pside recorded = the
    MARKET's own implied prob, so the gate directly measures the drift premium
  - maker entries only (join the bid of the side we buy)
  - momentum trigger: side price >= DRIFT_MIN_C (65) AND it moved up
    >= DRIFT_UP_C (2c) since the previous scan (memory persisted)
  - 1 bet per city-day event (strikes in one ladder are the same weather call)
  - no exits: drift bets ride to settlement by design

State -> logs/drift_state.json (dashboard picks it up)
Bets  -> logs/drift_bets.csv
"""
from __future__ import annotations
import os, csv, json, datetime

import requests

import weather_edge as we
from weather_paper import fetch_result
from kalshibot.fees import fee_cents

STATE = os.path.join("logs", "drift_state.json")
BETS = os.path.join("logs", "drift_bets.csv")
ERA = "drift1"

DRIFT_MIN_C = int(os.environ.get("DRIFT_MIN_C", "65"))       # side price floor
DRIFT_UP_C = float(os.environ.get("DRIFT_UP_C", "2"))        # min climb since last scan
DRIFT_MAX_ENTRY = int(os.environ.get("DRIFT_MAX_ENTRY", "90"))  # no near-certainties
DRIFT_MAX_PER_DAY = int(os.environ.get("DRIFT_MAX_PER_DAY", "20"))
# Momentum stop (Adam 7/21): this book's ONLY thesis is "trust the market's
# direction". If our side falls back below 50c it is no longer the favorite -
# the thesis is dead by its own logic, so cut the loss at the bid instead of
# riding to zero. (Model books get a model-guard; drift has no model.)
DRIFT_STOP_C = int(os.environ.get("DRIFT_STOP_C", "50"))
# Market-price calibration (7/21, n=451): 80c+ favorites went 18/18 vs 86-94
# implied, while 65-80c favorites underperformed (act 50 vs 73, n=8). So at
# >= DRIFT_LEVEL_C the LEVEL alone qualifies (no climb needed - certainty is
# underpriced); 65-80c entries remain the climb-gated experiment.
DRIFT_LEVEL_C = int(os.environ.get("DRIFT_LEVEL_C", "80"))
# Momentum-trader upgrades (Adam 7/21, from trend-following research):
VOL_CONFIRM = os.environ.get("DRIFT_VOL_CONFIRM", "1") == "1"   # climbs need volume
CLIMB_SAMEDAY = os.environ.get("DRIFT_CLIMB_SAMEDAY", "1") == "1"  # info arrives on settle day
PYRAMID_UP_C = int(os.environ.get("DRIFT_PYRAMID_UP_C", "10"))  # add-on trigger (post-gate)
PYRAMID_MAX = int(os.environ.get("DRIFT_PYRAMID_MAX", "2"))     # max adds per position
# Adam 7/21: pyramid during PROBE too (it's all paper) - the gate still
# controls base sizing; set DRIFT_PYRAMID_PROBE=0 to re-lock behind the gate
PYRAMID_PROBE = os.environ.get("DRIFT_PYRAMID_PROBE", "1") == "1"
# Trailing momentum exit (Adam 7/22: EVERY trade, no more A/B): sell when the
# price falls FADE_DROP_C off its peak even while still the favorite - exit
# when momentum stalls, not only when it dies. Winners that never stall still
# ride to settlement.
FADE_DROP_C = int(os.environ.get("DRIFT_FADE_DROP_C", "15"))
PROBE_COST_CENTS = int(os.environ.get("DRIFT_PROBE_COST", "60"))
GATE_MIN_N = 30
GATE_MAX_GAP = 0.05
PER_BET_CAP = 0.015


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class DriftPaper:
    def __init__(self, start_cents=10000):
        self.start = start_cents
        self.cash = float(start_cents)
        self.bets = {}
        self.history = []
        self.last_mid = {}       # ticker -> yes-mid at the previous scan
        self.last_vol = {}       # ticker -> 24h volume at the previous scan
        self.wins = 0
        self.losses = 0
        self.fees = 0.0
        self.placed = 0
        self.load()

    # ---- persistence ----
    def load(self):
        if os.path.exists(STATE):
            try:
                d = json.load(open(STATE))
                for k in ("start", "cash", "bets", "history", "last_mid",
                          "last_vol", "wins", "losses", "fees", "placed"):
                    if k in d:
                        setattr(self, k, d[k])
            except Exception:
                pass

    def save(self):
        try:
            os.makedirs("logs", exist_ok=True)
            mode, n = self._gate()
            d = {"updated": now(), "start": self.start, "cash": self.cash,
                 "bets": self.bets, "history": self.history[-120:],
                 "last_mid": self.last_mid, "last_vol": self.last_vol,
                 "wins": self.wins, "losses": self.losses, "fees": self.fees,
                 "placed": self.placed,
                 "summary": {
                     "start": round(self.start / 100.0, 2),
                     "cash": round(self.cash / 100.0, 2),
                     "net": round((self.cash + self._open_value_c()
                                   - self.start) / 100.0, 2),
                     "realized": round(sum(h.get("pnl", 0)
                                           for h in self.history), 2),
                     "wins": self.wins, "losses": self.losses,
                     "open": len(self.bets), "placed": self.placed,
                     "fees": round(self.fees / 100.0, 2),
                     "gate": mode, "gate_n": n},
                 "open": [dict(b, ticker=tk) for tk, b in self.bets.items()],
                 "settled": list(reversed(self.history[-40:]))}
            json.dump(d, open(STATE, "w"))
        except Exception:
            pass

    def _log(self, row):
        try:
            os.makedirs("logs", exist_ok=True)
            new = not os.path.exists(BETS)
            with open(BETS, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "event", "ticker", "city", "strike",
                                "hl", "side", "mkt_prob", "entry_c", "count",
                                "outcome", "pnl_$"])
                w.writerow(row)
        except Exception:
            pass

    def _open_value_c(self):
        return sum(b["entry"] * b["count"] for b in self.bets.values())

    # ---- gate: same contract as every other book ----
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

    def _placed_today(self):
        today = datetime.date.today().isoformat()
        n = sum(1 for b in self.bets.values() if (b.get("ots") or "")[:10] == today)
        n += sum(1 for h in self.history if (h.get("ots") or "")[:10] == today)
        return n

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
            self.wins += int(won)
            self.losses += int(not won)
            self.history.append({"city": b["city"], "strike": b["strike"],
                                 "kind": b.get("kind", "ge"), "cap": b.get("cap"),
                                 "hl": b["hl"], "side": b["side"],
                                 "pside": round(b["pside"], 3),
                                 "entry": b["entry"], "count": b["count"],
                                 "fee": b.get("fee", 0),
                                 "outcome": 1 if won else 0,
                                 "pnl": round(net / 100.0, 2), "ts": now(),
                                 "ots": b.get("ots", ""), "era": ERA})
            self.history = self.history[-120:]
            self._log([now(), "SETTLE", tk, b["city"], b["strike"], b["hl"],
                       b["side"], round(b["pside"], 3), b["entry"], b["count"],
                       1 if won else 0, round(net / 100.0, 2)])
            del self.bets[tk]

    def _quotes(self, tickers):
        """Batch (yes_bid, yes_ask) for open tickers - one API call."""
        out = {}
        try:
            d = requests.get(we.KALSHI + "/markets",
                             params={"tickers": ",".join(tickers[:40]),
                                     "limit": 100}, timeout=15).json()
            for m in d.get("markets") or []:
                yb = int(round(float(m.get("yes_bid_dollars") or 0) * 100))
                ya = int(round(float(m.get("yes_ask_dollars") or 0) * 100))
                out[m.get("ticker")] = (yb, ya)
        except Exception:
            pass
        return out

    def stop_check(self, quotes=None):
        """Momentum stop: our side's mid fell below DRIFT_STOP_C -> we are no
        longer holding the favorite -> sell at the bid (taker), log as a
        stopped exit (outcome None: excluded from the calibration gate)."""
        if not self.bets:
            return 0
        if quotes is None:
            quotes = self._quotes(list(self.bets))
        stopped = 0
        for tk, b in list(self.bets.items()):
            q = quotes.get(tk)
            if not q:
                continue
            yb, ya = q
            if not yb or not ya:
                continue
            mid = (yb + ya) / 2.0
            smid = mid if b["side"] == "yes" else 100 - mid
            peak = max(float(b.get("peak", smid)), smid)
            b["peak"] = peak
            # trailing exit (ALL bets, Adam 7/22): momentum stalled >=
            # FADE_DROP_C off the peak -> take the exit even while favorite
            fade = (smid >= DRIFT_STOP_C and peak - smid >= FADE_DROP_C)
            if smid >= DRIFT_STOP_C and not fade:
                continue
            bid = yb if b["side"] == "yes" else 100 - ya
            if bid <= 0:
                continue                      # nothing to sell into; settle decides
            cnt = b["count"]
            exit_fee = fee_cents(bid, cnt, taker=True)
            net = (bid - b["entry"]) * cnt - b.get("fee", 0) - exit_fee
            self.cash += bid * cnt - exit_fee
            self.fees += exit_fee
            self.history.append({"city": b["city"], "strike": b["strike"],
                                 "kind": b.get("kind", "ge"), "cap": b.get("cap"),
                                 "hl": b["hl"], "side": b["side"],
                                 "pside": round(b["pside"], 3),
                                 "entry": b["entry"], "count": cnt,
                                 "fee": b.get("fee", 0),
                                 "outcome": None, "exited": True,
                                 "stopped": not fade, "faded": fade,
                                 "exit_px": bid,
                                 "pnl": round(net / 100.0, 2), "ts": now(),
                                 "ots": b.get("ots", ""), "era": ERA})
            self.history = self.history[-120:]
            self._log([now(), "FADE" if fade else "STOP", tk, b["city"], b["strike"], b["hl"],
                       b["side"], round(b["pside"], 3), bid, cnt, "",
                       round(net / 100.0, 2)])
            del self.bets[tk]
            stopped += 1
        return stopped

    def place(self, mkts=None):
        """Momentum entries, RANKED: proven level entries (>=80c) first by
        price, then climbs by climb size (cross-sectional momentum). Climb
        entries additionally require a SAME-DAY market (that's when weather
        information actually arrives) and RISING 24h volume (a 2c climb on no
        volume is a stale quote, not momentum). Also pyramids post-gate."""
        if mkts is None:
            try:
                mkts = we.find_temp_markets(max_days=1)
            except Exception:
                return 0
        mode, _n = self._gate()
        budget = DRIFT_MAX_PER_DAY - self._placed_today()
        ev_keys = {(b["city"], b.get("date", ""), b["hl"])
                   for b in self.bets.values()}
        new_mid, new_vol, cands = {}, {}, []
        today_iso = datetime.date.today().isoformat()
        for mk in mkts:
            tk = mk["ticker"]
            bid, ask = mk["yes_bid"], mk["yes_ask"]
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2.0
            prev = self.last_mid.get(tk)
            prev_vol = self.last_vol.get(tk)
            vol = float(mk.get("vol", 0) or 0)
            new_mid[tk] = mid
            new_vol[tk] = vol
            if tk in self.bets:
                self._maybe_pyramid(tk, mk, mid, mode)
                continue
            ekey = (mk["city"], mk.get("date", ""),
                    "lo" if mk["is_low"] else "hi")
            if ekey in ev_keys:
                continue                    # one drift bet per weather event
            if mid >= DRIFT_MIN_C:
                side, entry, smid = "yes", bid, mid
                climb_c = (mid - prev) if prev is not None else None
            elif mid <= 100 - DRIFT_MIN_C:
                side, entry, smid = "no", 100 - ask, 100 - mid
                climb_c = (prev - mid) if prev is not None else None
            else:
                continue
            climbing = climb_c is not None and climb_c >= DRIFT_UP_C
            # >=80c: level alone is the proven signal; 65-80c: climb required,
            # and the climb must be REAL (same-day info + actual trading)
            if smid >= DRIFT_LEVEL_C:
                trig, score = "level", smid
            elif climbing:
                if CLIMB_SAMEDAY and mk.get("date", "") != today_iso:
                    continue                # tomorrow's climb = noise, skip
                if VOL_CONFIRM and not (prev_vol is not None and vol > prev_vol):
                    continue                # no volume behind the move = stale quote
                trig, score = "climb", climb_c
            else:
                continue
            if entry < 50 or entry > DRIFT_MAX_ENTRY:
                continue                    # favorite at a real price only
            cands.append((trig, score, mk, side, entry, smid, prev, mid, ekey))
        # cross-sectional ranking: strongest signals get the daily budget first
        cands.sort(key=lambda c: (0 if c[0] == "level" else 1, -c[1]))
        placed = 0
        for trig, score, mk, side, entry, smid, prev, mid, ekey in cands:
            if placed >= budget:
                break
            if ekey in ev_keys:
                continue
            tk = mk["ticker"]
            pside = smid / 100.0            # market's own prob = our prediction
            if mode == "probe":
                size = max(1, PROBE_COST_CENTS // entry)
            else:
                b_odds = (100 - entry) / entry
                f_star = max(0.0, pside - (1 - pside) / b_odds) * 0.25
                bankroll = self.cash + self._open_value_c()
                size = int(min(f_star, PER_BET_CAP) * bankroll // entry)
                if size < 1:
                    continue
            fee = fee_cents(entry, size, taker=False)   # maker join
            cost = entry * size + fee
            if self.cash - cost < 100:
                continue
            self.cash -= cost
            self.fees += fee
            self.bets[tk] = {"side": side, "entry": entry, "count": size,
                             "fee": fee, "pside": pside, "city": mk["city"],
                             "strike": mk["strike"], "kind": mk.get("kind", "ge"),
                             "cap": mk.get("cap"),
                             "hl": ("lo" if mk["is_low"] else "hi"),
                             "date": mk.get("date", ""), "ots": now(),
                             "era": ERA, "trig": trig,
                             "peak": smid, "adds": 0,
                             "from_mid": prev, "at_mid": mid}
            ev_keys.add(ekey)
            self.placed += 1
            placed += 1
            self._log([now(), "OPEN", tk, mk["city"], mk["strike"],
                       ("lo" if mk["is_low"] else "hi"), side,
                       round(pside, 3), entry, size, "", ""])
        self.last_mid = new_mid             # momentum memory = last scan only
        self.last_vol = new_vol
        return placed

    def _maybe_pyramid(self, tk, mk, mid, mode):
        """Trend-follower add-on: add one probe-size unit when an open
        position has run PYRAMID_UP_C past its (average) entry.
        Adds to winners, never losers; capped at PYRAMID_MAX adds.
        Active in probe too (paper, Adam 7/21) unless DRIFT_PYRAMID_PROBE=0."""
        if mode != "scale" and not PYRAMID_PROBE:
            return False
        b = self.bets[tk]
        if int(b.get("adds", 0)) >= PYRAMID_MAX:
            return False
        smid = mid if b["side"] == "yes" else 100 - mid
        if smid < b["entry"] + PYRAMID_UP_C:
            return False
        entry_add = mk["yes_bid"] if b["side"] == "yes" else 100 - mk["yes_ask"]
        if entry_add <= 0 or entry_add > DRIFT_MAX_ENTRY:
            return False
        add = max(1, PROBE_COST_CENTS // entry_add)
        fee = fee_cents(entry_add, add, taker=False)
        cost = entry_add * add + fee
        if self.cash - cost < 100:
            return False
        self.cash -= cost
        self.fees += fee
        tot = b["count"] + add
        b["entry"] = round((b["entry"] * b["count"] + entry_add * add) / tot, 1)
        b["count"] = tot
        b["fee"] = b.get("fee", 0) + fee
        b["adds"] = int(b.get("adds", 0)) + 1
        self._log([now(), "PYRAMID", tk, b["city"], b["strike"], b["hl"],
                   b["side"], round(b["pside"], 3), entry_add, add, "", ""])
        return True
    def step(self):
        self.settle()
        self.stop_check()
        n = self.place()
        self.save()
        return n

    def summary(self):
        mode, n = self._gate()
        return {"cash": round(self.cash / 100.0, 2), "wins": self.wins,
                "losses": self.losses, "open": len(self.bets),
                "gate": mode, "gate_n": n}


if __name__ == "__main__":
    d = DriftPaper()
    d.step()
    print(json.dumps(d.summary(), indent=2))
