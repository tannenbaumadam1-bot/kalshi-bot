#!/usr/bin/env python3
"""Delta-neutral crypto FUNDING-RATE carry - PAPER simulator.

The one genuinely UNCORRELATED strategy in the stack: hold a market-neutral
position (long spot + short perp when funding is positive, or the reverse when
negative) and collect the perpetual funding rate as income. No directional view;
income comes from the funding others pay. Data: Hyperliquid public API (funding
settles hourly). Majors (BTC/ETH) currently pay ~11% APY; select alts far more.

v2 (2026-07-08) - three honesty/yield upgrades:
- PERSISTENCE RANKING: rank/accrue on a 7-day EWMA of hourly funding (free
  fundingHistory endpoint), not the instantaneous snapshot. Funding mean-reverts;
  snapshot-ranking systematically buys the top of the spike. Sign-stability
  guard: if the EWMA disagrees in sign with current funding, the carry is
  unstable - skip. History fetched for the top FUND_HIST_TOP candidates only.
- ROTATION HYSTERESIS: incumbents keep their seat unless a challenger beats the
  worst incumbent by SWITCH_EDGE APY points (default 10 = the amortized cost of
  4 legs of fees+slippage over a ~7-day hold). Rotations after the initial
  build are charged an explicit SWITCH_COST x alloc entry fee in the daily net.
- FUNDING-WEIGHTED ALLOCATION: allocation proportional to (EWMA) APY instead of
  equal-weight, still capped at MAX_PER_ASSET per asset, with one redistribution
  pass so capped weight flows to the uncapped names.
HAIRCUT still covers steady-state fees + basis/slippage on the carry itself;
SWITCH_COST specifically penalizes churn (which equal-daily-repick hid).

HONEST scope: this PAPER book models net carry after haircut. LIVE execution
needs a US-legal venue. US-LEGALITY: Hyperliquid is DATA ONLY - geoblocked to
US persons, VPN forbidden. A live US path = Coinbase perpetual-style futures
(CFTC, up to 10x, hourly funding), Kraken, or CME basis - majors carry, not the
high alt rates. Treat this paper book as research until then.
"""
from __future__ import annotations
import os, json, time, datetime
import requests

HL = "https://api.hyperliquid.xyz/info"
FSTATE = os.path.join("logs", "funding_state.json")

START_USD       = float(os.environ.get("FUND_START", "100"))
MAX_ASSETS      = int(os.environ.get("FUND_MAX_ASSETS", "5"))
MAX_PER_ASSET   = float(os.environ.get("FUND_MAX_PER_ASSET", "0.30"))  # <=30% of book per asset
RESERVE_FRAC    = float(os.environ.get("FUND_RESERVE", "0.10"))
HAIRCUT         = float(os.environ.get("FUND_HAIRCUT", "0.30"))        # steady-state fees + basis/slippage
MIN_APY         = float(os.environ.get("FUND_MIN_APY", "0.05"))        # ignore < 5% annualized
MAX_APY         = float(os.environ.get("FUND_MAX_APY", "3.0"))         # exclude > 300% (blowup risk)
MAJORS          = {"BTC", "ETH", "SOL"}
GATE_MIN_DAYS   = 20
PROBE_FRAC      = float(os.environ.get("FUND_PROBE", "1.0"))           # scale of deployment while proving
SWITCH_EDGE     = float(os.environ.get("FUND_SWITCH_EDGE", "0.10"))    # APY pts a challenger must add
SWITCH_COST     = float(os.environ.get("FUND_SWITCH_COST", "0.002"))   # 4 legs fees+slip per rotation
HIST_TOP        = int(os.environ.get("FUND_HIST_TOP", "12"))           # candidates to pull history for
HIST_DAYS       = float(os.environ.get("FUND_HIST_DAYS", "7"))
EWMA_HL_H       = float(os.environ.get("FUND_EWMA_HL_H", "48"))        # EWMA half-life, hours


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


def fetch_history_ewma(asset, days=None):
    """Signed EWMA of hourly funding over `days` (half-life EWMA_HL_H hours),
    or None if the history is unavailable/too short."""
    days = HIST_DAYS if days is None else days
    try:
        start = int((time.time() - days * 86400) * 1000)
        r = requests.post(HL, json={"type": "fundingHistory", "coin": asset,
                                    "startTime": start}, timeout=15).json()
        rates = [float(x["fundingRate"]) for x in r if x.get("fundingRate") is not None]
        if len(rates) < 24:                      # need at least a day of history
            return None
        alpha = 1 - 0.5 ** (1.0 / max(1.0, EWMA_HL_H))
        e = rates[0]
        for v in rates[1:]:
            e = alpha * v + (1 - alpha) * e
        return e
    except Exception:
        return None


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


def enrich_persistence(ops, hist_fn=fetch_history_ewma, top=None):
    """Replace snapshot APY with the 7d EWMA APY for the top candidates.
    Drops sign-unstable names (EWMA disagrees with current funding direction).
    Names without history keep the snapshot rate (identity fallback)."""
    top = HIST_TOP if top is None else top
    out = []
    for i, a in enumerate(ops):
        a = dict(a)
        e = hist_fn(a["asset"]) if i < top else None
        if e is not None:
            if e * a["funding_hr"] < 0:
                continue                               # unstable carry - skip
            a["funding_hr_ewma"] = e
            a["apy"] = abs(e) * 24 * 365
        else:
            a["funding_hr_ewma"] = a["funding_hr"]
        if MIN_APY <= a["apy"] <= MAX_APY:
            out.append(a)
    out.sort(key=lambda x: -x["apy"])
    return out


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

    def _roster(self, ops):
        """Hysteresis: qualifying incumbents keep their seat; a challenger evicts
        the worst incumbent only by beating it by SWITCH_EDGE APY points."""
        by_asset = {o["asset"]: o for o in ops}
        held = [p["asset"] for p in self.positions]
        roster = [by_asset[a] for a in held if a in by_asset][:MAX_ASSETS]
        chall = [o for o in ops if o["asset"] not in {r["asset"] for r in roster}]
        for c in chall:                                # fill empty seats first (free)
            if len(roster) >= MAX_ASSETS:
                break
            roster.append(c)
        for c in chall:                                # then evictions (cost a rotation)
            if any(c["asset"] == r["asset"] for r in roster):
                continue
            worst = min(roster, key=lambda o: o["apy"]) if roster else None
            if worst is not None and c["apy"] > worst["apy"] + SWITCH_EDGE:
                roster[roster.index(worst)] = c
        roster.sort(key=lambda o: -o["apy"])
        return roster

    def _allocate(self, ops):
        """APY-weighted allocation over the hysteresis roster, per-asset capped,
        one redistribution pass. Rotations after the initial build pay SWITCH_COST."""
        bank = self.cash
        deployable = bank * (1 - RESERVE_FRAC) * PROBE_FRAC
        per_cap = MAX_PER_ASSET * bank
        roster = self._roster(ops)
        if not roster or deployable <= 0:
            return []
        prev = {p["asset"] for p in self.positions}
        w = {o["asset"]: max(o["apy"], 1e-9) for o in roster}
        tot = sum(w.values())
        alloc = {o["asset"]: min(per_cap, deployable * w[o["asset"]] / tot) for o in roster}
        leftover = deployable - sum(alloc.values())
        uncapped = [o for o in roster if alloc[o["asset"]] < per_cap - 1e-9]
        if leftover > 1e-9 and uncapped:               # redistribute capped weight once
            wt = sum(w[o["asset"]] for o in uncapped)
            for o in uncapped:
                a = o["asset"]
                alloc[a] = min(per_cap, alloc[a] + leftover * w[a] / wt)
        picks = []
        for o in roster:
            a = alloc[o["asset"]]
            if a <= 0:
                continue
            hr = o.get("funding_hr_ewma", o["funding_hr"])
            daily_net = a * abs(hr) * 24 * (1 - HAIRCUT)
            rot = SWITCH_COST * a if (self.days > 0 and o["asset"] not in prev) else 0.0
            picks.append({"asset": o["asset"], "side": o["side"], "alloc": round(a, 2),
                          "apy": round(o["apy"] * 100, 1),
                          "net": round(daily_net - rot, 4),
                          "rot_cost": round(rot, 4)})
        return picks

    def step(self, funding=None, force=False):
        today = datetime.date.today().isoformat()
        if not force and funding is None and self.last_date == today:
            return 0.0, self.positions
        live = funding is None
        ops = opportunities(funding if funding is not None else fetch_funding())
        if live:
            ops = enrich_persistence(ops)              # network history only in live mode
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
              {"asset": "ZRO", "funding_hr": 0.0000633, "mark": 0.93, "apy": 0.0000633*24*365}]
        ops = opportunities(fk)
        assert all(o["asset"] != "MEME" for o in ops)             # meme excluded
        assert any(o["asset"] == "BTC" for o in ops)              # major kept
        # persistence: EWMA replaces snapshot; sign-flip dropped; no-history identity
        hist = {"ZRO": 0.0003, "BTC": -0.00001}                   # BTC flipped sign -> drop
        en = enrich_persistence(ops, hist_fn=lambda a: hist.get(a))
        assert all(o["asset"] != "BTC" for o in en)
        zro = next(o for o in en if o["asset"] == "ZRO")
        assert abs(zro["apy"] - 0.0003*24*365) < 1e-9
        # weighted allocation + hysteresis + rotation cost
        p = FundingPaper.__new__(FundingPaper)
        p.start=100.0; p.cash=100.0; p.days=0; p.earned=0.0; p.last_date=""; p.positions=[]; p.history=[]
        net, picks = p.step(funding=fk, force=True)
        assert net > 0 and p.cash > 100 and picks
        assert all(pk["rot_cost"] == 0 for pk in picks)           # initial build free
        print("funding_arb self-test PASSED (day net $%.4f on $100, %d legs)" % (net, len(picks)))
    else:
        p = FundingPaper()
        net, picks = p.step(force=True)
        print("funding paper: +$%.4f today | bank $%.2f | %dd | APY~%s%%" %
              (net, p.cash, p.days, p.apy()))
        for pk in picks:
            print("  %-6s %-22s alloc $%.2f  APY %.1f%%  net/day $%.4f" %
                  (pk["asset"], pk["side"], pk["alloc"], pk["apy"], pk["net"]))
