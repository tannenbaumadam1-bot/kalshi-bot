#!/usr/bin/env python3
"""Momentum drift book, WIDE universe (era "driftw1") - PAPER, own $100
ledger, own gate. Soros #4 (Adam 2026-07-23): the drift edge looks like
market MICROSTRUCTURE (the crowd overprices doubt, underprices certainty),
not meteorology - so test the same rules on every OTHER Kalshi market and
attack the weather books' tiny capacity ceiling.

Same discipline contract as drift1, different universe:
  - universe: ALL open Kalshi markets EXCLUDING the weather series (drift1's
    turf) and MVE combos; resolving within DRIFTW_MAX_H (48h) so the gate
    accumulates fast; liquidity-guarded (24h volume floor + max spread) so a
    "mid" actually means something out here
  - level entries >= 80c side-mid (the proven signal), maker join, entry
    50..90c; climbs 65-80c need +2c on RISING volume AND close within
    DRIFTW_CLIMB_H (24h) - the wide-universe analog of "same-day only"
  - probe stakes (<= $1.50/bet) until the 30-bet gate on era "driftw1" shows
    positive expectancy AND calibration within 5pts; pside = market prob
  - momentum stop <50c, trailing exit 15c off peak - identical to drift1
  - one bet per EVENT (ladder strikes are one opinion)
  - NO nickel lane, NO pyramiding in v1: unknown universe, fewest moving
    parts; both are post-gate upgrades if the premium proves out here too

State -> logs/driftw_state.json (dashboard picks it up)
Bets  -> logs/driftw_bets.csv
"""
from __future__ import annotations
import os, csv, json, datetime

import requests

import weather_edge as we
from weather_paper import fetch_result
from kalshibot.fees import fee_cents

STATE = os.path.join("logs", "driftw_state.json")
BETS = os.path.join("logs", "driftw_bets.csv")
ERA = "driftw1"

MIN_C = int(os.environ.get("DRIFTW_MIN_C", "65"))          # side price floor
UP_C = float(os.environ.get("DRIFTW_UP_C", "2"))           # min climb / scan
MAX_ENTRY = int(os.environ.get("DRIFTW_MAX_ENTRY", "90"))  # no near-certainties
LEVEL_C = int(os.environ.get("DRIFTW_LEVEL_C", "80"))      # level-alone trigger
STOP_C = int(os.environ.get("DRIFTW_STOP_C", "50"))        # thesis-dead stop
FADE_DROP_C = int(os.environ.get("DRIFTW_FADE_DROP_C", "15"))  # trail off peak
MAX_PER_DAY = int(os.environ.get("DRIFTW_MAX_PER_DAY", "20"))
MAX_H = float(os.environ.get("DRIFTW_MAX_H", "48"))        # close horizon (h)
CLIMB_H = float(os.environ.get("DRIFTW_CLIMB_H", "24"))    # climbs need close soon
MIN_VOL24 = float(os.environ.get("DRIFTW_MIN_VOL24", "200"))   # stale-quote guard
MAX_SPREAD_C = int(os.environ.get("DRIFTW_MAX_SPREAD", "6"))   # mid must be real
PROBE_COST_CENTS = int(os.environ.get("DRIFTW_PROBE_COST", "150"))
GATE_MIN_N = 30
GATE_MAX_GAP = 0.05
PER_BET_CAP = float(os.environ.get("DRIFTW_PER_BET_CAP", "0.03"))


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def find_wide_markets(max_hrs=MAX_H):
    """All open non-weather, non-MVE Kalshi markets closing within max_hrs,
    with a real two-sided quote. One paged /events sweep (same endpoint the
    spread book uses - no key, no auth)."""
    out, cursor, pages = [], None, 0
    nowdt = datetime.datetime.now(datetime.timezone.utc)
    while pages < 45:
        p = {"limit": 200, "status": "open", "with_nested_markets": "true"}
        if cursor:
            p["cursor"] = cursor
        try:
            d = requests.get(we.KALSHI + "/events", params=p, timeout=20).json()
        except Exception:
            break
        pages += 1
        for ev in d.get("events", []) or []:
            st = ev.get("series_ticker", "") or ""
            if "MVE" in st or st in we.SERIES:
                continue                    # combos out; weather = drift1's turf
            ev_tk = ev.get("event_ticker", "") or ""
            ev_title = ev.get("title", "") or ""
            for mk in ev.get("markets", []) or []:
                ct = mk.get("close_time", "")
                try:
                    close = datetime.datetime.strptime(
                        ct, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                except Exception:
                    continue
                hrs = (close - nowdt).total_seconds() / 3600
                if hrs < -2 or hrs > max_hrs:
                    continue
                yb = int(round(float(mk.get("yes_bid_dollars") or 0) * 100))
                ya = int(round(float(mk.get("yes_ask_dollars") or 0) * 100))
                if yb <= 0 or ya <= 0:
                    continue
                base = mk.get("title") or ev_title or mk["ticker"]
                sub = mk.get("yes_sub_title") or ""
                name = (base + " - " + sub) if sub and sub.lower() not in base.lower() else base
                out.append({
                    "ticker": mk["ticker"],
                    "event": ev_tk or mk["ticker"].rsplit("-", 1)[0],
                    "name": name[:90],
                    "yes_bid": yb, "yes_ask": ya,
                    "vol": float(mk.get("volume_24h_fp") or 0),
                    "hrs": hrs,
                })
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


class DriftWide:
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
                    w.writerow(["timestamp", "event", "ticker", "name",
                                "side", "mkt_prob", "entry_c", "count",
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
            self.history.append({"name": b.get("name", tk),
                                 "side": b["side"], "trig": b.get("trig"),
                                 "pside": round(b["pside"], 3),
                                 "entry": b["entry"], "count": b["count"],
                                 "fee": b.get("fee", 0),
                                 "outcome": 1 if won else 0,
                                 "pnl": round(net / 100.0, 2), "ts": now(),
                                 "ots": b.get("ots", ""), "era": ERA})
            self.history = self.history[-120:]
            self._log([now(), "SETTLE", tk, b.get("name", tk), b["side"],
                       round(b["pside"], 3), b["entry"], b["count"],
                       1 if won else 0, round(net / 100.0, 2)])
            del self.bets[tk]

    def _quotes(self, tickers):
        """Batch (yes_bid, yes_ask) for open tickers - one API call per 40."""
        out = {}
        for i in range(0, len(tickers), 40):
            try:
                d = requests.get(we.KALSHI + "/markets",
                                 params={"tickers": ",".join(tickers[i:i + 40]),
                                         "limit": 100}, timeout=15).json()
                for m in d.get("markets") or []:
                    yb = int(round(float(m.get("yes_bid_dollars") or 0) * 100))
                    ya = int(round(float(m.get("yes_ask_dollars") or 0) * 100))
                    out[m.get("ticker")] = (yb, ya)
            except Exception:
                pass
        return out

    def stop_check(self, quotes=None):
        """Momentum stop + trailing exit, identical to drift1: below STOP_C
        the thesis is dead (sell the bid); FADE_DROP_C off the peak the
        momentum stalled (take the exit). Exits are outcome None: excluded
        from the calibration gate."""
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
            fade = (smid >= STOP_C and peak - smid >= FADE_DROP_C)
            if smid >= STOP_C and not fade:
                continue
            bid = yb if b["side"] == "yes" else 100 - ya
            if bid <= 0:
                continue                      # nothing to sell into; settle decides
            cnt = b["count"]
            exit_fee = fee_cents(bid, cnt, taker=True)
            net = (bid - b["entry"]) * cnt - b.get("fee", 0) - exit_fee
            self.cash += bid * cnt - exit_fee
            self.fees += exit_fee
            self.history.append({"name": b.get("name", tk),
                                 "side": b["side"], "trig": b.get("trig"),
                                 "pside": round(b["pside"], 3),
                                 "entry": b["entry"], "count": cnt,
                                 "fee": b.get("fee", 0),
                                 "outcome": None, "exited": True,
                                 "stopped": not fade, "faded": fade,
                                 "exit_px": bid,
                                 "pnl": round(net / 100.0, 2), "ts": now(),
                                 "ots": b.get("ots", ""), "era": ERA})
            self.history = self.history[-120:]
            self._log([now(), "FADE" if fade else "STOP", tk,
                       b.get("name", tk), b["side"], round(b["pside"], 3),
                       bid, cnt, "", round(net / 100.0, 2)])
            del self.bets[tk]
            stopped += 1
        return stopped

    def place(self, mkts=None):
        """Ranked entries: proven level entries (>=80c) first by price, then
        climbs by climb size. Climbs must be REAL: rising 24h volume and a
        market that resolves within CLIMB_H."""
        if mkts is None:
            try:
                mkts = find_wide_markets()
            except Exception:
                return 0
        mode, _n = self._gate()
        budget = MAX_PER_DAY - self._placed_today()
        ev_keys = {b.get("event", "") for b in self.bets.values()}
        new_mid, new_vol, cands = {}, {}, []
        for mk in mkts:
            tk = mk["ticker"]
            bid, ask = mk["yes_bid"], mk["yes_ask"]
            if bid <= 0 or ask <= 0 or (ask - bid) > MAX_SPREAD_C:
                continue
            if float(mk.get("vol", 0) or 0) < MIN_VOL24:
                continue
            mid = (bid + ask) / 2.0
            prev = self.last_mid.get(tk)
            prev_vol = self.last_vol.get(tk)
            vol = float(mk.get("vol", 0) or 0)
            new_mid[tk] = mid
            new_vol[tk] = vol
            if tk in self.bets or mk["event"] in ev_keys:
                continue
            if mid >= MIN_C:
                side, entry, smid = "yes", bid, mid
                climb_c = (mid - prev) if prev is not None else None
            elif mid <= 100 - MIN_C:
                side, entry, smid = "no", 100 - ask, 100 - mid
                climb_c = (prev - mid) if prev is not None else None
            else:
                continue
            climbing = climb_c is not None and climb_c >= UP_C
            if smid >= LEVEL_C:
                trig, score = "level", smid
            elif climbing:
                if mk.get("hrs", 99) > CLIMB_H:
                    continue                # climb far from resolution = noise
                if not (prev_vol is not None and vol > prev_vol):
                    continue                # no volume behind the move
                trig, score = "climb", climb_c
            else:
                continue
            if entry < 50 or entry > MAX_ENTRY:
                continue                    # favorite at a real price only
            cands.append((trig, score, mk, side, entry, smid))
        cands.sort(key=lambda c: ({"level": 0}.get(c[0], 1), -c[1]))
        placed = 0
        for trig, score, mk, side, entry, smid in cands:
            if placed >= budget:
                break
            if mk["event"] in ev_keys:
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
                             "fee": fee, "pside": pside,
                             "name": mk["name"], "event": mk["event"],
                             "ots": now(), "era": ERA, "trig": trig,
                             "peak": smid, "from_mid": self.last_mid.get(tk),
                             "at_mid": (mk["yes_bid"] + mk["yes_ask"]) / 2.0}
            ev_keys.add(mk["event"])
            self.placed += 1
            placed += 1
            self._log([now(), "OPEN", tk, mk["name"], side,
                       round(pside, 3), entry, size, "", ""])
        self.last_mid = new_mid             # momentum memory = last scan only
        self.last_vol = new_vol
        return placed

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
    d = DriftWide()
    d.step()
    print(json.dumps(d.summary(), indent=2))
