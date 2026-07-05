#!/usr/bin/env python3
"""Delta-neutral crypto FUNDING-RATE carry - PAPER simulator.

The one genuinely UNCORRELATED strategy in the stack: hold a market-neutral
position (long spot + short perp when funding is positive, or the reverse when
negative) and collect the perpetual funding rate as income. No directional view;
income comes from the funding others pay. Data: Hyperliquid public API (funding
settles hourly). Majors (BTC/ETH) currently pay ~11% APY; select alts far more.

HONEST scope: this PAPER book models the net carry after a haircut for
amortized fees + basis/slippage. LIVE execution needs a US-accessible perp venue
(Hyperliquid) with a real spot hedge - a separate lift behind the usual gate.
Risks modeled/monitored: funding can flip (forcing a costly rotation), basis can
gap, and extreme-funding meme coins are excluded as blowup risk.
"""
from __future__ import annotations
import os, json, datetime
import requests

HL = "https://api.hyperliquid.xyz/info"
FSTATE = os.path.join("logs", "funding_state.json")

START_USD       = float(os.environ.get("FUND_START", "100"))
MAX_ASSETS      = int(os.environ.get("FUND_MAX_ASSETS", "5"))
MAX_PER_ASSET   = float(os.environ.get("FUND_MAX_PER_ASSET", "0.30"))  # <=30% of book per asset
RESERVE_FRAC    = float(os.environ.get("FUND_RESERVE", "0.10"))
HAIRCUT         = float(os.environ.get("FUND_HAIRCUT", "0.30"))        # amortized fees + basis/slippage
MIN_APY         = float(os.environ.get("FUND_MIN_APY", "0.05"))        # ignore < 5% annualized
MAX_APY         = float(os.environ.get("FUND_MAX_APY", "3.0"))         # exclude > 300% (blowup risk)
MAJORS          = {"BTC", "ETH", "SOL"}
GATE_MIN_DAYS   = 20
PROBE_FRAC      = float(os.environ.get("FUND_PROBE", "1.0"))           # scale of deployment while proving


def fetch_funding():
    """[{asset, funding_hr, mark, apy(abs, annualized)}], richest |funding| first."""
    try:
        r = requests.post(HL, json={"type": "metaAndAssetCtxs"}, timeout=15).json()
        meta, ctxs = r[0]["universe"], r[1]
    except Exception:
        return []
    out = []
    for u, c in zip(meta, ctxs):
        f, px = c.get("funding"), c.get("markPx")
        try:
            f = float(f); px = float(px)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        out.append({"asset": u.get("name", "?"), "funding_hr": f, "mark": px,
                    "apy": abs(f) * 24 * 365})
    out.sort(key=lambda a: -a["apy"])
    return out


def opportunities(funding):
    """Filter to sane, tradeable carry opportunities."""
    ops = []
    for a in funding:
        if a["apy"] < MIN_APY or a["apy"] > MAX_APY:
            continue
        if a["mark"] < 0.10 and a["asset"] not in MAJORS:
            continue                                   # skip micro-cap meme perps
        a = dict(a)
        a["side"] = "short perp / long spot" if a["funding_hr"] > 0 else "long perp / short spot"
        ops.append(a)
    return ops


class FundingPaper:
    def __init__(self):
        self.start = START_USD
        self.cash = START_USD
        self.days = 0
        self.earned = 0.0
        self.last_date = ""
        self.positions = []          # last day's deployed legs (for the dashboard)
        self.history = []
        self.load()

    def load(self):
        try:
            d = json.load(open(FSTATE))
            self.start = d.get("start", self.start); self.cash = d.get("cash", self.cash)
            self.days = d.get("days", 0); self.earned = d.get("earned", 0.0)
            self.last_date = d.get("last_date", ""); self.history = d.get("history", [])
            self.positions = d.get("positions", [])
        except Exception:
            pass

    def apy(self):
        if self.days < 1 or self.start <= 0:
            return None
        return round(100 * ((self.cash / self.start) ** (365.0 / self.days) - 1), 1)

    def save(self):
        try:
            os.makedirs("logs", exist_ok=True)
            json.dump({"updated": datetime.datetime.now().isoformat(timespec="seconds"),
                       "start": self.start, "cash": round(self.cash, 4), "days": self.days,
                       "earned": round(self.earned, 4), "apy_annualized": self.apy(),
                       "last_date": self.last_date, "positions": self.positions[:6],
                       "history": self.history[-120:]}, open(FSTATE, "w"))
        except Exception:
            pass

    def _allocate(self, ops):
        bank = self.cash
        deployable = bank * (1 - RESERVE_FRAC) * PROBE_FRAC
        per_cap = MAX_PER_ASSET * bank
        picks = []
        used = 0.0
        for a in ops[:MAX_ASSETS]:
            alloc = min(per_cap, deployable / MAX_ASSETS)
            if alloc <= 0 or used + alloc > deployable:
                continue
            daily_net = alloc * abs(a["funding_hr"]) * 24 * (1 - HAIRCUT)
            picks.append({"asset": a["asset"], "side": a["side"], "alloc": round(alloc, 2),
                          "apy": round(a["apy"] * 100, 1), "net": round(daily_net, 4)})
            used += alloc
        return picks

    def step(self, funding=None, force=False):
        today = datetime.date.today().isoformat()
        if not force and funding is None and self.last_date == today:
            return 0.0, self.positions
        ops = opportunities(funding if funding is not None else fetch_funding())
        picks = self._allocate(ops)
        day_net = sum(p["net"] for p in picks)
        self.cash += day_net
        self.earned += day_net
        self.days += 1
        self.last_date = today
        self.positions = picks
        self.history.append({"day": self.days, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                             "assets": len(picks), "net": round(day_net, 4), "cash": round(self.cash, 4)})
        self.history = self.history[-120:]
        self.save()
        return day_net, picks

    def gate(self):
        if self.days < GATE_MIN_DAYS:
            return "probe", self.days
        recent = [h["net"] for h in self.history[-GATE_MIN_DAYS:]]
        return ("scale" if sum(recent) > 0 else "probe"), self.days

    def summary(self):
        return {"start": self.start, "cash": round(self.cash, 2), "days": self.days,
                "earned": round(self.earned, 2), "apy": self.apy(), "open": len(self.positions)}


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        fk = [{"asset": "BTC", "funding_hr": 0.0000125, "mark": 60000, "apy": 0.0000125*24*365},
              {"asset": "MEME", "funding_hr": 0.01, "mark": 0.001, "apy": 0.01*24*365},   # excluded (mark<0.1)
              {"asset": "ZRO", "funding_hr": 0.00063, "mark": 0.93, "apy": 0.00063*24*365}]
        ops = opportunities(fk)
        assert all(o["asset"] != "MEME" for o in ops)             # meme excluded
        assert any(o["asset"] == "BTC" for o in ops)              # major kept
        p = FundingPaper.__new__(FundingPaper)
        p.start=100.0; p.cash=100.0; p.days=0; p.earned=0.0; p.last_date=""; p.positions=[]; p.history=[]
        net, picks = p.step(funding=fk, force=True)
        assert net > 0 and p.cash > 100 and picks
        print("funding_arb self-test PASSED (day net $%.4f on $100, %d legs)" % (net, len(picks)))
    else:
        p = FundingPaper()
        net, picks = p.step(force=True)
        print("funding paper: +$%.4f today | bank $%.2f | %dd | APY~%s%%" %
              (net, p.cash, p.days, p.apy()))
        for pk in picks:
            print("  %-6s %-22s alloc $%.2f  APY %.1f%%  net/day $%.4f" %
                  (pk["asset"], pk["side"], pk["alloc"], pk["apy"], pk["net"]))
