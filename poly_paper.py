#!/usr/bin/env python3
"""Polymarket reward-farming PAPER simulator - no keys, no USDC, no orders.

Estimates net reward-farming P&L using LIVE reward configs + order-book
competition, then compounds a paper USDC ledger. It is deliberately
CONSERVATIVE and clearly a MODEL: the naive (our-share x daily-pool) figure
overstates reality by ~20x (quadratic scoring, adverse selection, uptime, and
unit questions on rewardsDailyRate), so we apply a capture-efficiency factor
plus an adverse-selection haircut and a hard daily cap. Once real payouts are
observed on a tiny live stake, CAPTURE_EFF should be re-calibrated to them.

Risk controls: max markets, max fraction per market, min-size affordability,
skip too-crowded markets, cash reserve, per-market daily-net cap.
"""
from __future__ import annotations
import os, json, datetime
import poly_client as pc

PSTATE = os.path.join("logs", "poly_state.json")

START_USDC        = float(os.environ.get("POLY_START", "100"))
MAX_MARKETS       = int(os.environ.get("POLY_MAX_MARKETS", "6"))
MAX_PER_MKT_FRAC  = float(os.environ.get("POLY_MAX_PER_MKT", "0.25"))  # <=25% of bank per market
RESERVE_FRAC      = float(os.environ.get("POLY_RESERVE", "0.10"))       # keep 10% cash
CAPTURE_EFF       = float(os.environ.get("POLY_CAPTURE_EFF", "0.08"))   # naive->real gap (CALIBRATE)
ADVERSE_HAIRCUT   = float(os.environ.get("POLY_ADVERSE", "0.35"))       # inventory/adverse loss
DAILY_NET_CAP     = float(os.environ.get("POLY_DAILY_CAP", "0.01"))     # <=1%/day/market sanity cap


def est_net_daily(alloc_usd, mkt, competing_shares):
    """Modeled NET USDC/day from resting alloc_usd in this market's reward band."""
    mid = mkt.get("mid") or 0.5
    if mid <= 0:
        return 0.0
    our_shares = alloc_usd / mid
    share = our_shares / (our_shares + max(1.0, competing_shares))
    gross = share * mkt.get("pool_daily", 0.0) * CAPTURE_EFF
    net = gross * (1 - ADVERSE_HAIRCUT)
    return min(net, DAILY_NET_CAP * alloc_usd)          # sanity cap vs unit errors


class PolyPaper:
    def __init__(self):
        self.start = START_USDC
        self.cash = START_USDC
        self.days = 0
        self.earned = 0.0
        self.last_date = ""
        self.history = []
        self.load()

    def load(self):
        try:
            d = json.load(open(PSTATE))
            self.start = d.get("start", self.start); self.cash = d.get("cash", self.cash)
            self.days = d.get("days", 0); self.earned = d.get("earned", 0.0)
            self.last_date = d.get("last_date", "")
            self.history = d.get("history", [])
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs("logs", exist_ok=True)
            json.dump({"updated": datetime.datetime.now().isoformat(timespec="seconds"),
                       "start": self.start, "cash": round(self.cash, 4), "days": self.days,
                       "earned": round(self.earned, 4), "apy_annualized": self.apy(),
                       "last_date": self.last_date,
                       "history": self.history[-120:]}, open(PSTATE, "w"))
        except Exception:
            pass

    def apy(self):
        if self.days < 1 or self.start <= 0:
            return None
        return round(100 * ((self.cash / self.start) ** (365.0 / self.days) - 1), 1)

    def _pick(self, markets, comp_fn):
        """Choose markets + allocations under the risk caps; returns list of
        (market, alloc_usd, competing_shares, est_net)."""
        bank = self.cash
        deployable = bank * (1 - RESERVE_FRAC)
        per_cap = MAX_PER_MKT_FRAC * bank
        cand = []
        for m in markets:
            mid = m.get("mid") or 0.5
            min_size_usd = m.get("min_size", 0) * mid           # min qualifying order in $
            if min_size_usd <= 0 or min_size_usd > per_cap:
                continue                                        # can't afford the minimum
            comp = comp_fn(m)
            alloc = min(per_cap, max(min_size_usd, deployable / MAX_MARKETS))
            net = est_net_daily(alloc, m, comp)
            if net <= 0:
                continue
            cand.append((net / alloc, m, alloc, comp, net))     # rank by net-yield
        cand.sort(reverse=True, key=lambda t: t[0])
        picks, used = [], 0.0
        for _y, m, alloc, comp, net in cand:
            if len(picks) >= MAX_MARKETS or used + alloc > deployable:
                continue
            picks.append((m, alloc, comp, net)); used += alloc
        return picks

    def step(self, markets=None, comp_fn=None, force=False):
        """One paper DAY: allocate to reward markets, accrue modeled net rewards.
        Idempotent within a calendar day (safe to call every loop cycle)."""
        today = datetime.date.today().isoformat()
        if not force and markets is None and self.last_date == today:
            return 0.0, []                       # already accrued today
        if markets is None:
            markets = pc.reward_markets()
            markets = sorted(markets, key=lambda m: -m.get("pool_daily", 0))[:15]  # bound network
        comp_fn = comp_fn or pc.market_competition
        picks = self._pick(markets, comp_fn)
        day_net = sum(net for _m, _a, _c, net in picks)
        self.cash += day_net
        self.earned += day_net
        self.days += 1
        self.last_date = today
        self.history.append({"day": self.days,
                             "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                             "markets": len(picks), "net": round(day_net, 4),
                             "cash": round(self.cash, 4)})
        self.history = self.history[-120:]
        self.save()
        return day_net, picks

    def summary(self):
        return {"start": self.start, "cash": round(self.cash, 2), "days": self.days,
                "earned": round(self.earned, 2), "apy": self.apy()}


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        m = {"q": "T", "mid": 0.5, "pool_daily": 10000.0, "min_size": 100, "max_spread_c": 1.5}
        n = est_net_daily(500, m, competing_shares=682000)
        assert n > 0 and n <= DAILY_NET_CAP * 500
        print("poly_paper self-test PASSED (net/day on $500 ~ $%.2f, %.2f%%/day)" % (n, 100 * n / 500))
    else:
        p = PolyPaper()
        net, picks = p.step()
        print("paper day %d: %d markets, +$%.3f, bank $%.2f, APY~%s%%" %
              (p.days, len(picks), net, p.cash, p.apy()))
        for m, a, c, nt in picks[:6]:
            print("  %-42s alloc $%.0f  net/day $%.3f" % (m["q"][:42], a, nt))
