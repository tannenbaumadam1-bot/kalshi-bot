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
import weather_ensemble as wx
import weather_shadow as ws
import weather_nowcast as nc

WSIM = os.path.join("logs", "weather_sim.json")
WBETS = os.path.join("logs", "weather_bets.csv")
WSTATE = os.path.join("logs", "weather_state.json")

# Total-book exposure cap: open cost basis may never exceed this fraction of
# bankroll (cash + open stake). Keeps one bad day from being a blowout and
# leaves dry powder for tomorrow's edges.
# v5 (2026-07-03): tightened 0.50 -> 0.30 after exposure hit 74-90% of NAV
# and max drawdown reached 62%.
MAX_BOOK_FRAC = 0.30

# ---- v5-cal: prove the edge before sizing into it -------------------------
# Every era so far settled with actual win rate far below predicted (model
# overconfident in all four calibration buckets). So: full VOLUME, tiny
# STAKES ("probe mode") until the current model demonstrates, on its own
# settled bets, that (a) expectancy is positive and (b) predicted-vs-actual
# calibration gap is within GATE_MAX_GAP. Only then scale to Kelly sizing.
ERA = "v7-obs"   # nowcast + ticker-date fix + learned blend (bumped 2026-07-10)
PER_BET_CAP = 0.015        # max bankroll fraction per bet once proven (was 3%)
PROBE_COST_CENTS = 60      # max cost basis per bet while unproven
GATE_MIN_N = 30            # settled current-era bets needed before scaling
GATE_MAX_GAP = 0.05        # max (mean pside - actual win rate) to scale
# v7: MIN_PRICE 15 -> 30. Across every era the 15-30c entries were the loss
# center (v6: actual 15% win vs ~21c paid); the only +EV bucket was 30c+.
MIN_PRICE, MAX_PRICE = 30, 85
# v8: retire sub-50% confidence bets. Third independent confirmation that our
# under-50 psides lose (v5 buckets, 7/10 review, 7/18 calibration: 30-50%
# bucket pred 39 vs act 28 = -11pts, while 50%+ buckets run UNDERconfident
# +7/+8pts). We only bet when the blended model makes OUR side the favorite;
# the shadow log still measures the retired band for free.
MIN_PSIDE = float(os.environ.get("WX_MIN_PSIDE", "0.50"))
# v7: LOW-temp markets were 40/44 of v6 bets and carried all the losses
# (act 20% vs pred 36.5%). Cap them to half the book allowance until the
# shadow report proves lo-calibration.
LO_BOOK_FRAC = 0.50
# v8 multi-strike: bands/or-below strikes now tradeable; cap correlated bets
# in the same city-day event (mutually exclusive strikes, shared weather risk)
EVENT_MAX_BETS = int(os.environ.get("WX_EVENT_MAX_BETS", "3"))
# v7: churn killer - after a forecast-based exit, do NOT re-enter the same
# ticker for this many hours (Phoenix was exit/re-entered 5x in one day).
COOLDOWN_H = 12
# v7: an exit must be confirmed on this many CONSECUTIVE checks before selling
# (single-scan model swings caused -EV churn).
EXIT_CONFIRMS = 2


def _before(iso, cutoff):
    try:
        return datetime.datetime.fromisoformat(iso) < cutoff
    except Exception:
        return True


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
        self.cooldown = {}  # ticker -> iso ts of forecast-based exit (no re-entry)
        self.load()

    # ---- persistence ----
    def to_dict(self):
        return {"start": self.start, "cash": self.cash, "bets": self.bets,
                "realized": self.realized, "wins": self.wins, "losses": self.losses,
                "fees": self.fees, "placed": self.placed,
                "cooldown": self.cooldown,
                "history": self.history[-100:]}

    def save(self):
        try:
            os.makedirs("logs", exist_ok=True)
            with open(WSIM, "w") as f:
                json.dump(self.to_dict(), f)
            st = {"updated": datetime.datetime.now().isoformat(timespec="seconds"),
                  "summary": self.summary(),
                  "depth": dict(we.LAST_DEPTH) if we.LAST_DEPTH else None,
                  "open": [{"ticker": tk, "city": b["city"], "strike": b["strike"],
                            "kind": b.get("kind", "ge"), "cap": b.get("cap"),
                            "hl": b["hl"], "side": b["side"], "entry": b["entry"],
                            "count": b["count"], "pside": round(b["pside"], 2),
                            "fee": b.get("fee", 0), "src": b.get("src", ""),
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
            self.cooldown = d.get("cooldown", {})
        except Exception:
            pass

    def _cooled(self, ticker, now=None):
        """True if this ticker was forecast-exited within COOLDOWN_H hours."""
        ts = self.cooldown.get(ticker)
        if not ts:
            return False
        now = now or datetime.datetime.now()
        try:
            return (now - datetime.datetime.fromisoformat(ts)).total_seconds() < COOLDOWN_H * 3600
        except Exception:
            return False

    def _prune_cooldown(self, now=None):
        now = now or datetime.datetime.now()
        cut = now - datetime.timedelta(hours=COOLDOWN_H * 2)
        self.cooldown = {k: v for k, v in self.cooldown.items()
                         if not _before(v, cut)}

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
                                 "kind": b.get("kind", "ge"), "cap": b.get("cap"),
                                 "side": b["side"], "pside": round(b["pside"], 3),
                                 "entry": b["entry"], "count": b["count"],
                                 "fee": b.get("fee", 0),
                                 "outcome": (1 if won else 0), "pnl": round(net / 100.0, 2),
                                 "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                 "ots": b.get("ots", ""), "era": b.get("era", "v2")})
            self.history = self.history[-100:]
            self._log([datetime.datetime.now().isoformat(timespec="seconds"), "SETTLE",
                       b["city"], b["strike"], b["hl"], b["side"], round(b["pside"], 3),
                       b["entry"], b["count"], (1 if won else 0), round(net / 100, 2)])
            del self.bets[tk]

    def _gate(self):
        """('probe'|'scale', n): scale only when the CURRENT era has proven
        itself on >= GATE_MIN_N settled bets: positive expectancy AND
        predicted win rate within GATE_MAX_GAP of actual."""
        cur = [h for h in self.history
               if h.get("era") == ERA and h.get("outcome") in (0, 1)][-60:]
        n = len(cur)
        if n < GATE_MIN_N:
            return "probe", n
        expectancy = sum(h["pnl"] for h in cur) / n
        pred = sum(h["pside"] for h in cur) / n
        act = sum(h["outcome"] for h in cur) / n
        if expectancy > 0 and (pred - act) <= GATE_MAX_GAP:
            return "scale", n
        return "probe", n

    def _calibrate(self, p):
        """v7: map a model probability through this era's OWN settled
        pred-vs-actual buckets (Laplace-smoothed, piecewise-linear). Raw psides
        ran overconfident in every prior era, which inflates Kelly f* and
        oversizes exactly the worst bets; sizing must use corrected probs."""
        rows = [h for h in self.history
                if h.get("era") == ERA and h.get("outcome") in (0, 1)]
        if len(rows) < GATE_MIN_N:
            return p
        pts = []
        for lo, hi in [(0, .3), (.3, .5), (.5, .7), (.7, 1.01)]:
            sel = [h for h in rows if lo <= h["pside"] < hi]
            if len(sel) >= 5:
                c = sum(h["pside"] for h in sel) / len(sel)
                a = (sum(h["outcome"] for h in sel) + 1.0) / (len(sel) + 2.0)
                pts.append((c, a))
        if not pts:
            return p
        pts.sort()
        if p <= pts[0][0]:               # below first center: scale toward 0
            c, a = pts[0]
            return max(0.0, min(1.0, p * a / c)) if c > 0 else p
        if p >= pts[-1][0]:              # above last center: scale toward 1
            c, a = pts[-1]
            return max(0.0, min(1.0, 1 - (1 - p) * (1 - a) / (1 - c))) if c < 1 else p
        for (c0, a0), (c1, a1) in zip(pts, pts[1:]):
            if c0 <= p <= c1:
                t = (p - c0) / max(1e-9, c1 - c0)
                return max(0.0, min(1.0, a0 + t * (a1 - a0)))
        return p

    def place(self):
        edges = we.scan(min_edge_cents=4, max_edge_cents=20, verbose=False)
        # bankroll = cash + cost basis of open bets (so sizing scales with equity)
        open_stake = sum(b["entry"] * b["count"] for b in self.bets.values())
        # v7: lo-market concentration cap (the loss center until proven)
        lo_stake = sum(b["entry"] * b["count"] for b in self.bets.values()
                       if b.get("hl") == "lo")
        bankroll = self.cash + open_stake
        mode, _gate_n = self._gate()
        self._prune_cooldown()
        ev_counts = {}
        for b in self.bets.values():
            ek = (b["city"], b.get("date", ""), b["hl"])
            ev_counts[ek] = ev_counts.get(ek, 0) + 1
        for ev, side, mk, fair, ftemp in edges:
            tk = mk["ticker"]
            if tk in self.bets:
                continue
            if self._cooled(tk):          # v7: no re-entry churn after an exit
                continue
            ekey = (mk["city"], mk.get("date", ""), "lo" if mk["is_low"] else "hi")
            if ev_counts.get(ekey, 0) >= EVENT_MAX_BETS:
                continue                  # correlated event already at cap
            s = "yes" if side == "YES" else "no"
            # maker entry (rest at the bid) chosen by the scanner; fall back to
            # the taker touch only if an older scan didn't tag one.
            maker = bool(mk.get("maker", False))
            price = int(mk.get("entry_price",
                               mk["yes_ask"] if s == "yes" else (100 - mk["yes_bid"])))
            if price < MIN_PRICE or price > MAX_PRICE:
                continue
            # direction check via Kelly; sizing depends on gate mode
            p = fair if s == "yes" else (1 - fair)
            if p < MIN_PSIDE:
                continue          # v8: sub-50% confidence measured -EV in 3 eras
            b_odds = (100 - price) / price
            f_star = p - (1 - p) / b_odds
            if f_star <= 0:
                continue
            if mode == "probe":
                # unproven model: full volume, tiny fixed stakes -> this is
                # calibration data collection, not bankroll deployment
                size = max(1, PROBE_COST_CENTS // price)
            else:
                # proven (gated) model: quarter-Kelly on the CALIBRATION-
                # CORRECTED probability (v7 - raw psides oversize bad bets),
                # hard-capped per bet
                p_cal = self._calibrate(p)
                f_cal = p_cal - (1 - p_cal) / b_odds
                if f_cal <= 0:
                    continue
                frac = min(0.25 * f_cal, PER_BET_CAP)
                size = int((frac * bankroll) // price)
            if size < 1:
                continue
            fee = fee_cents(price, size, taker=not maker)
            cost = price * size + fee
            # total-book cap: never let open cost basis exceed MAX_BOOK_FRAC
            # of bankroll (edges are sorted best-first, so the best fit first)
            if open_stake + price * size > MAX_BOOK_FRAC * bankroll:
                continue
            # v7: lo-market cap - lows carried every loss so far; cap their
            # share of the book allowance until shadow data clears them
            is_lo = bool(mk["is_low"])
            if is_lo and lo_stake + price * size > LO_BOOK_FRAC * MAX_BOOK_FRAC * bankroll:
                continue
            if self.cash - cost < 100:        # keep a $1 reserve
                continue
            self.cash -= cost
            open_stake += price * size
            if is_lo:
                lo_stake += price * size
            self.fees += fee
            pside = fair if s == "yes" else (1 - fair)
            ev_counts[ekey] = ev_counts.get(ekey, 0) + 1
            self.bets[tk] = {"side": s, "entry": price, "count": size, "fee": fee,
                             "pside": pside, "city": mk["city"], "strike": mk["strike"],
                             "kind": mk.get("kind", "ge"), "cap": mk.get("cap"),
                             "hl": ("lo" if mk["is_low"] else "hi"),
                             "ots": datetime.datetime.now().isoformat(timespec="seconds"),
                             "era": ERA, "maker": maker, "date": mk.get("date", ""),
                             "src": mk.get("src", "forecast"), "w": mk.get("w", we.MODEL_WEIGHT),
                             "mkt_bid": mk["yes_bid"], "mkt_ask": mk["yes_ask"]}
            self.placed += 1
            self._log([datetime.datetime.now().isoformat(timespec="seconds"), "OPEN",
                       mk["city"], mk["strike"], ("lo" if mk["is_low"] else "hi"), s,
                       round(pside, 3), price, size, "", ""])

    def _quote(self, ticker):
        """Current (yes_bid, yes_ask) in cents, or (None, None)."""
        try:
            d = requests.get(we.KALSHI + "/markets/" + ticker, timeout=15).json()
            mk = d.get("market", d)
            yb = int(round(float(mk.get("yes_bid_dollars") or 0) * 100))
            ya = int(round(float(mk.get("yes_ask_dollars") or 0) * 100))
            return yb, ya
        except Exception:
            return None, None

    def _reprice(self, city, date, lat, lon, strike, is_low, kind="ge", cap=None):
        """(p_side_yes, weight) re-forecast for one market. Same-day markets
        use the OBS-anchored nowcast (hard data); else the forecast ensemble.
        Returns (None, None) if we cannot re-evaluate."""
        try:
            stt = nc.day_state(city, date, lat, lon)
        except Exception:
            stt = None
        if stt and stt.get("n_obs", 0) >= nc.MIN_OBS:
            p = we.kind_prob(lambda k: nc.prob_from_state(stt, k, is_low),
                             kind, strike, cap)
            if p is not None:
                return p, we.NOWCAST_WEIGHT
        try:
            dd = datetime.datetime.strptime(date, "%Y-%m-%d")
            hrs = max(1.0, ((dd + datetime.timedelta(days=1)) -
                            datetime.datetime.now()).total_seconds() / 3600)
        except Exception:
            hrs = 6.0
        if kind == "ge":
            p, _, nsrc = wx.prob(city, date, lat, lon, strike, is_low, hrs, log=False)
            if p is None or nsrc < wx.MIN_SOURCES:
                return None, None
            return p, we.blend_weight()
        try:
            fc = wx.forecast(city, date, lat, lon, hrs, log=False)
        except Exception:
            return None, None
        d = fc["min"] if is_low else fc["max"]
        if not d.ok() or fc["n_sources"] < wx.MIN_SOURCES:
            return None, None
        p = we.kind_prob(d.prob_at_least, kind, strike, cap)
        if p is None:
            return None, None
        return p, we.blend_weight()

    def exit_check(self, margin_c=2):
        """SMART stop-loss: NOT a price stop. For each underwater position we
        RE-FORECAST (nowcast on same-day markets); if the market's bid exceeds
        our updated fair value (+ churn margin, after the taker exit fee), we
        sell - the market is paying more than the bet is now worth to us.
        v7 anti-churn: (1) hold value uses the SAME model/market blend as
        entry (raw p_new alone whipsawed 0.9->0.3 within hours and caused
        exit/re-enter loops); (2) an exit must be confirmed on EXIT_CONFIRMS
        consecutive checks; (3) the ticker goes on a re-entry cooldown.
        Winners still ride to settlement (calibration data)."""
        for tk, b in list(self.bets.items()):
            city, strike, is_low, side = b["city"], b["strike"], b["hl"] == "lo", b["side"]
            date = b.get("date", "")
            if not date or city not in we.CITY_COORDS:
                continue
            lat, lon = we.CITY_COORDS[city]
            p_yes, wgt = self._reprice(city, date, lat, lon, strike, is_low,
                                       b.get("kind", "ge"), b.get("cap"))
            if p_yes is None:
                continue                      # can't re-evaluate -> hold
            p_new = p_yes if side == "yes" else (1 - p_yes)
            yb, ya = self._quote(tk)
            if yb is None:
                continue
            bid = yb if side == "yes" else (100 - ya)   # price we can sell our side into
            ask = ya if side == "yes" else (100 - yb)
            if bid <= 0 or bid >= b["entry"]:           # only CUT LOSSES (underwater)
                b["exit_streak"] = 0
                continue
            mid_p = max(0.0, min(1.0, (bid + ask) / 200.0))
            exit_fee_per = fee_cents(bid, 1, taker=True)
            exit_ev = bid - exit_fee_per                # per-contract net if we sell now
            # hold value = entry-consistent blend of updated model and market
            hold_ev = (wgt * p_new + (1 - wgt) * mid_p) * 100
            if exit_ev > hold_ev + margin_c:
                b["exit_streak"] = int(b.get("exit_streak", 0)) + 1
                if b["exit_streak"] < EXIT_CONFIRMS:
                    continue                  # flagged; confirm on the next check
                cnt = b["count"]
                exit_fee = fee_cents(bid, cnt, taker=True)
                net = (bid - b["entry"]) * cnt - b.get("fee", 0) - exit_fee
                self.cash += bid * cnt - exit_fee
                self.realized += net
                self.fees += exit_fee
                self.history.append({"city": city, "strike": strike,
                                     "kind": b.get("kind", "ge"), "cap": b.get("cap"),
                                     "hl": b["hl"], "side": side, "pside": round(b["pside"], 3),
                                     "entry": b["entry"], "count": cnt, "fee": b.get("fee", 0),
                                     "outcome": None, "exited": True,
                                     "pnl": round(net / 100.0, 2), "p_new": round(p_new, 3),
                                     "exit_px": bid,
                                     "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                     "ots": b.get("ots", ""), "era": b.get("era", "v2")})
                self.history = self.history[-100:]
                self._log([datetime.datetime.now().isoformat(timespec="seconds"), "EXIT",
                           city, strike, b["hl"], side, round(p_new, 3), bid, cnt, "",
                           round(net / 100.0, 2)])
                self.cooldown[tk] = datetime.datetime.now().isoformat(timespec="seconds")
                del self.bets[tk]
            else:
                b["exit_streak"] = 0

    def step(self):
        """v7.1: each phase is isolated and state is saved after EVERY phase,
        so one bad phase can neither lose the others' work nor hide - errors
        land in logs/weather_err.txt, which the dashboard serves on /public."""
        import traceback
        errs = []
        for name, fn in (("settle", self.settle),
                         ("exit", self.exit_check),
                         ("place", self.place)):
            try:
                fn()
            except Exception:
                errs.append("%s: %s" % (name, traceback.format_exc(limit=4)))
            self.save()
        try:
            ws.settle_daily()   # resolve shadow-logged markets (1x/day, bounded)
        except Exception:
            pass
        try:
            ws.fit_daily()      # refresh learned blend weight (1x/day)
        except Exception:
            pass
        try:
            os.makedirs("logs", exist_ok=True)
            ep = os.path.join("logs", "weather_err.txt")
            if errs:
                with open(ep, "w") as f:
                    f.write(datetime.datetime.now().isoformat(timespec="seconds")
                            + "\n" + "\n".join(errs))
            elif os.path.exists(ep):
                os.remove(ep)
        except Exception:
            pass
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
