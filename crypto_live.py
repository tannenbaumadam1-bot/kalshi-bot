#!/usr/bin/env python3
"""LANE 2 LIVE - crypto drift executor (era "clive1"), REAL MONEY.

Adam 8/3: "let's go live with the crypto book - half of NAV to weather,
half to crypto." The audition passed its pre-registered gate in 3 days
(148 settled, 140W/8L, +$13.04 realized AFTER fees). This is the live
version, running INSIDE the armed kalshi-drift-live service (same key,
same self-restart, same kill switch: systemctl stop kalshi-drift-live).

Design (the audition's config + the live book's scar tissue):
  - TAKER-FIRST BY DEFAULT (8/3 mandate): entries pay the ask on deep
    tight books. No resting-and-hoping; the $25 miss-leak class is
    designed out. Wide spreads (>4c) are skipped, not joined.
  - entry band 80-92c at the ASK, side-mid >= 80, vol >= 500, close
    <= 24h, one bet per event, level entries only
  - 35c stop, NO trail (exit autopsies), hold to settlement
  - CAPITAL SPLIT: this book's bankroll = CRYPTO_ALLOC (default 0.5) x
    account NAV (balance + BOTH books' position cost, peer read from the
    weather book's state file). Caps: 3%/bet, 60% open, 10% day-halt of
    the ALLOCATED half - both books rebalance every cycle as NAV moves.
  - UNIVERSE PARTITION: manages ONLY tickers it placed; the weather
    executor is fenced to weather series. Neither adopts the other's.
  - quarter-Kelly sizing (live is unproven even though paper passed -
    half-Kelly is earned back via the same bucket evidence, later)

State -> logs/crypto_live_state.json   Bets -> logs/crypto_live_bets.csv
"""
from __future__ import annotations

import csv
import datetime
import json
import os

import requests

import drift_wide as dw
import drift_crypto as dcfg
from weather_paper import fetch_result
from kalshibot.fees import fee_cents

STATE = os.path.join("logs", "crypto_live_state.json")
BETS = os.path.join("logs", "crypto_live_bets.csv")
PEER_STATE = os.environ.get("CRYPTO_PEER_STATE",
                            os.path.join("logs", "drift_live_state.json"))
ERA = "clive1"
CRYPTO_ON = os.environ.get("DRIFT_LIVE_CRYPTO", "1") == "1"
ALLOC = float(os.environ.get("CRYPTO_ALLOC", "0.5"))
BET_PCT = float(os.environ.get("CRYPTO_BET_PCT", "0.03"))
# 8/4 Adam: small-account boost - 6% per bet until ACCOUNT NAV reaches
# $300, then auto-revert to the standard 3% (a step DOWN in bet dollars
# at the threshold, by design).
BET_PCT_BOOST = float(os.environ.get("CRYPTO_BET_PCT_BOOST", "0.06"))
BOOST_NAV_C = int(os.environ.get("CRYPTO_BOOST_NAV_C", "30000"))
OPEN_PCT = float(os.environ.get("CRYPTO_OPEN_PCT", "0.60"))
HALT_PCT = float(os.environ.get("CRYPTO_HALT_PCT", "0.10"))
RESERVE_C = int(os.environ.get("CRYPTO_RESERVE_C", "200"))
ENTRY_MIN = int(os.environ.get("CRYPTO_ENTRY_MIN", "80"))
ENTRY_MAX = int(os.environ.get("CRYPTO_ENTRY_MAX", "92"))
# 8/4 HIGH-BAND PROBE (Adam: "take advantage of convergence to certainty,
# ship at half kelly"): entries 93-96c allowed as a SEPARATELY TRACKED
# bucket - weather's nickel lane playbook. Our shadow calibration has
# 90-95c markets settling YES 24/24, but crypto above 92c is UNPROVEN
# (the audition never traded there) and the payoff is +4-7c vs ~-60c
# after a stop, so this bucket keeps its own W/L ledger and earns (or
# loses) its lane on evidence, in public, on the tracker.
PROBE_MAX = int(os.environ.get("CRYPTO_PROBE_MAX", "96"))
# 8/5 HI-BAND SIZE LADDER (Adam: "press the crypto nickel" - weather's
# earn-the-raise playbook, automated): the 93-96c bucket sizes at the
# base per-bet cap until it PROVES itself, then steps up on its own -
# 8% of bank once 10 settled hi bets are net-positive, 10% once 20 are.
# The raise is revoked instantly (back to base) whenever lifetime hi
# net is not positive, and a proven-negative bucket (>=8 settled, net
# < 0) is BLOCKED from new entries outright. No manual sizing, ever.
HI_STEP1_N = int(os.environ.get("CRYPTO_HI_STEP1_N", "10"))
HI_STEP2_N = int(os.environ.get("CRYPTO_HI_STEP2_N", "20"))
HI_PCT1 = float(os.environ.get("CRYPTO_HI_PCT1", "0.08"))
HI_PCT2 = float(os.environ.get("CRYPTO_HI_PCT2", "0.10"))
HI_BLOCK_N = int(os.environ.get("CRYPTO_HI_BLOCK_N", "8"))
# 8/7: evidence gate for the core (80-92c) band, mirroring HI_BLOCK_N.
CORE_BLOCK_N = int(os.environ.get("CRYPTO_CORE_BLOCK_N", "8"))
MAX_SPREAD = int(os.environ.get("CRYPTO_MAX_SPREAD", "4"))
MIN_VOL24 = float(os.environ.get("CRYPTO_MIN_VOL24", "500"))
# 8/6: Kalshi now lists each hourly crypto event only ~60 min before its
# close (open_time = top of the prior hour), so vol24 reads ~0 for most
# of an hourly's life and the 500 floor silently excluded EVERY hourly
# market - only the long-listed noon/5pm dailies ever traded (Adam
# spotted it: "only trading the 5pm market"). Near the close, the
# two-sided-quote + spread<=MAX_SPREAD gate is the real liquidity test
# for a taker buying 1-5 contracts, so within HOURLY_H hours of close
# the volume floor drops to MIN_VOL24_LATE.
HOURLY_H = float(os.environ.get("CRYPTO_HOURLY_H", "1.5"))
MIN_VOL24_LATE = float(os.environ.get("CRYPTO_MIN_VOL24_LATE", "0"))
STOP_C = float(os.environ.get("CRYPTO_STOP_C", "35"))
MAX_PER_DAY = int(os.environ.get("CRYPTO_MAX_PER_DAY", "40"))
REST_MAX_MIN = float(os.environ.get("CRYPTO_REST_MAX_MIN", "30"))
# COMPOUNDING LADDER (8/3, Adam): the bankroll side already compounds -
# bank = 50% of account NAV, refreshed EVERY cycle, so every settled win
# raises the very next bet across both books automatically. The sizing
# side now compounds on evidence too: quarter-Kelly until the LIVE book
# proves itself (>=100 settled, net realized > 0 - the audition's bar,
# re-earned with real fills), then half-Kelly. At crypto's settlement
# cadence that ladder can climb in days, not weeks.
KELLY = float(os.environ.get("CRYPTO_KELLY", "0.25"))
KELLY_PROVEN = float(os.environ.get("CRYPTO_KELLY_PROVEN", "0.5"))
# 8/4 Adam override ("fire the kelly leg up now, the strategy is clearly
# working"): evidence gate lowered 100 -> 25 (book was 34 settled, 33W/1L,
# +$2.33 net). The guard rails stay: upgrade only while lifetime realized
# is POSITIVE, and it auto-reverts to quarter-Kelly if the book goes red.
KELLY_PROVEN_N = int(os.environ.get("CRYPTO_KELLY_PROVEN_N", "25"))
# 8/3 (Adam): 15-minute series EXCLUDED. The audition measured them at
# ~zero edge (n=11, +$0.12 - noise) while hourly/daily carried all the
# profit, and the live book's first 15-min trade lost -$0.84. A price
# 15 minutes out is a coin flip at the wire, not convergence.
NO_15M = os.environ.get("CRYPTO_NO_15M", "1") == "1"


def _is_15m(tk, name=""):
    return ("15M" in (tk or "").split("-")[0].upper()
            or "15 min" in (name or "").lower()
            or "next 15" in (name or "").lower())


# 8/6 DIRECT SERIES FETCH: the global /events sweep (45 pages x 200,
# category-filtered) was silently MISSING the new short-lived hourly
# events - a 90/91c in-band BTC 1pm candidate sat unseen for 40+ min
# while the sweep-fed scan placed nothing. Hourlies now open only ~60min
# before close, and a paged global sweep is both truncatable and
# breakable mid-page. So the crypto book asks Kalshi for ITS OWN series
# by name - ~12 cheap /markets calls, no pagination lottery. The old
# sweep stays as fallback if the direct fetch comes back empty.
CRYPTO_SERIES = [s.strip() for s in os.environ.get(
    "CRYPTO_SERIES",
    "KXBTCD,KXBTC,KXETHD,KXETH,KXSOLD,KXSOLE,KXXRPD,KXXRP,"
    "KXDOGED,KXDOGE,KXHYPED,KXHYPE").split(",") if s.strip()]
CRYPTO_MAX_H = float(os.environ.get("CRYPTO_MAX_H", "24"))


def fetch_crypto_mkts():
    out = []
    nowdt = datetime.datetime.now(datetime.timezone.utc)
    for s in CRYPTO_SERIES:
        try:
            d = requests.get(dw.we.KALSHI + "/markets",
                             params={"series_ticker": s, "status": "open",
                                     "limit": 200}, timeout=10).json()
        except Exception:
            continue
        for mk in d.get("markets") or []:
            ct = (mk.get("close_time") or "")[:19]
            try:
                close = datetime.datetime.strptime(
                    ct, "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            hrs = (close - nowdt).total_seconds() / 3600
            if hrs < -2 or hrs > CRYPTO_MAX_H:
                continue
            yb = int(round(float(mk.get("yes_bid_dollars") or 0) * 100))
            ya = int(round(float(mk.get("yes_ask_dollars") or 0) * 100))
            if yb <= 0 or ya <= 0:
                continue
            base = mk.get("title") or mk.get("ticker", "")
            sub = mk.get("yes_sub_title") or ""
            name = ((base + " - " + sub)
                    if sub and sub.lower() not in base.lower() else base)
            out.append({"ticker": mk["ticker"],
                        "event": (mk.get("event_ticker")
                                  or mk["ticker"].rsplit("-", 1)[0]),
                        "name": name[:90],
                        "yes_bid": yb, "yes_ask": ya,
                        "vol": float(mk.get("volume_24h_fp") or 0),
                        "hrs": hrs})
    return out


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today():
    return datetime.date.today().isoformat()


def _weather_prefixes():
    import weather_edge as we
    return set(we.SERIES)


class CryptoLive:
    def __init__(self, client=None, mode="DRY"):
        self.client = client
        self.mode = mode
        self.bets = {}
        self.pending = {}
        self.history = []
        self.wins = 0
        self.losses = 0
        self.fees_c = 0.0
        self.realized_c = 0.0
        self.placed = 0
        self.canceled = 0
        self.day = today()
        self.day_pnl_c = 0.0
        self.halted = False
        self.k_positions = []
        self.sync_diffs = None
        self.dry_balance_c = 10000
        self.bank_c = 0          # allocated bankroll, refreshed each cycle
        self.pnl_days = {}       # date -> realized $ that day (never trimmed)
        self.hi = {"w": 0, "l": 0, "pnl": 0.0}   # 93-96c probe ledger
        # 8/7: the core band (80-92c) carried EVERY realized loss in the
        # era (6 stops, -$7.33) while netting only +$1.16 over 49 bets.
        # It now gets the same evidence ledger + block rule as the hi
        # band instead of being the one unmetered lane.
        self.core = {"w": 0, "l": 0, "pnl": 0.0}
        self.stops = 0                  # realized stop-outs (see stop_check)
        self.load()

    # ---- persistence ----
    def load(self):
        if os.path.exists(STATE):
            try:
                d = json.load(open(STATE))
                if d.get("era") != ERA or d.get("mode") != self.mode:
                    return
                for k in ("bets", "pending", "history", "wins", "losses",
                          "fees_c", "realized_c", "placed", "canceled",
                          "day", "day_pnl_c", "dry_balance_c", "pnl_days",
                          "hi", "core", "stops"):
                    if k in d:
                        setattr(self, k, d[k])
            except Exception:
                pass
        # 8/7 one-time backfill of the core ledger + stop counter from
        # the retained history, so the new gate starts with the evidence
        # the era actually produced instead of an empty slate. Guarded by
        # its own flag so it can never double-count on a later restart.
        if not self.core.get("bf") and self.history:
            core = {"w": 0, "l": 0, "pnl": 0.0, "bf": 1}
            stops = 0
            for h in self.history:
                p = h.get("pnl")
                if p is None:
                    continue
                if h.get("stopped"):
                    stops += 1
                if h.get("band", "core") != "core":
                    continue
                core["w" if p > 0 else "l"] += 1
                core["pnl"] = round(core["pnl"] + float(p), 2)
            self.core = core
            # the old stop path never touched self.losses, so the era's
            # headline record understated losses by exactly the number of
            # stop-outs still visible in history. Fold them in once here;
            # stop_check counts every stop from now on.
            self.losses += max(0, stops - self.stops)
            self.stops = max(self.stops, stops)
        self.core.setdefault("bf", 1)
        # one-time backfill (history is complete this early in the era;
        # the daily ledger itself is never trimmed - 8/3 lesson)
        if not self.pnl_days and self.history:
            for h in self.history:
                p, ts = h.get("pnl"), (h.get("ts") or "")[:10]
                if p is not None and ts:
                    self.pnl_days[ts] = round(
                        self.pnl_days.get(ts, 0.0) + float(p), 2)

    def _day_add(self, net_c):
        d = today()
        self.pnl_days[d] = round(self.pnl_days.get(d, 0.0) + net_c / 100.0, 2)

    def save(self, balance_c=None):
        try:
            os.makedirs("logs", exist_ok=True)
            d = {"updated": now(), "era": ERA, "mode": self.mode,
                 "bets": self.bets, "pending": self.pending,
                 "history": self.history[-120:],
                 "pnl_days": self.pnl_days,
                 "hi": self.hi, "core": self.core, "stops": self.stops,
                 "wins": self.wins, "losses": self.losses,
                 "fees_c": self.fees_c, "realized_c": self.realized_c,
                 "placed": self.placed, "canceled": self.canceled,
                 "day": self.day, "day_pnl_c": self.day_pnl_c,
                 "dry_balance_c": self.dry_balance_c,
                 "k_positions": self.k_positions,
                 "balance_c": balance_c,
                 "summary": {
                     "mode": self.mode, "era": ERA,
                     "alloc": ALLOC,
                     "bank": round(self.bank_c / 100.0, 2),
                     "caps": {"bet": round(self._bet_cap_c() / 100.0, 2),
                              "open": round(self._open_cap_c() / 100.0, 2),
                              "halt": round(self._halt_c() / 100.0, 2),
                              "bet_pct": self._bet_pct()},
                     "wins": self.wins, "losses": self.losses,
                     "realized": round(self.realized_c / 100.0, 2),
                     "fees": round(self.fees_c / 100.0, 2),
                     "open": len(self.bets), "resting": len(self.pending),
                     "placed": self.placed, "canceled": self.canceled,
                     "day_pnl": round(self.day_pnl_c / 100.0, 2),
                     "halted": self.halted,
                     "no_15m": NO_15M,
                     "kelly": self._kelly(),
                     "kelly_n": self.wins + self.losses,
                     "kelly_gate": KELLY_PROVEN_N,
                     "hi": {"w": self.hi.get("w", 0),
                            "l": self.hi.get("l", 0),
                            "pnl": round(self.hi.get("pnl", 0.0), 2),
                            "open": sum(1 for b in self.bets.values()
                                        if b.get("band") == "hi"),
                            "max": PROBE_MAX,
                            "pct": self._hi_pct(),
                            "n1": HI_STEP1_N, "n2": HI_STEP2_N,
                            "blocked": self._hi_blocked()},
                     "core": {"w": self.core.get("w", 0),
                              "l": self.core.get("l", 0),
                              "pnl": round(self.core.get("pnl", 0.0), 2),
                              "open": sum(1 for b in self.bets.values()
                                          if b.get("band", "core") == "core"),
                              "min": ENTRY_MIN, "max": ENTRY_MAX,
                              "blocked": self._core_blocked()},
                     "stops": self.stops,
                     "sync_diffs": self.sync_diffs},
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
                    w.writerow(["timestamp", "kind", "mode", "ticker", "name",
                                "side", "mkt_prob", "price_c", "count",
                                "outcome", "pnl_$", "order_id"])
                w.writerow(row)
        except Exception:
            pass

    # ---- capital split ----
    def open_cost_c(self):
        oc = sum(b["entry"] * b["count"] + b.get("fee", 0)
                 for b in self.bets.values())
        oc += sum(o["entry"] * o["count"] for o in self.pending.values())
        return oc

    def _peer_cost_c(self):
        try:
            d = json.load(open(PEER_STATE))
            return sum(float(b.get("entry", 0)) * int(b.get("count", 0))
                       for b in (d.get("bets") or {}).values())
        except Exception:
            return 0

    def balance_c(self):
        if self.client is None:
            return self.dry_balance_c
        return self.client.get_balance_cents()

    def refresh_bank(self, balance_c):
        nav = balance_c + self.open_cost_c() + self._peer_cost_c()
        if nav > 0:
            self.bank_c = int(nav * ALLOC)
            self.acct_nav_c = int(nav)

    def _bet_pct(self):
        # 8/4 small-account boost: 6%/bet below $300 account NAV, 3% after
        nav = getattr(self, "acct_nav_c", 0)
        return BET_PCT_BOOST if 0 < nav < BOOST_NAV_C else BET_PCT

    def _bet_cap_c(self):
        return max(150, int(self.bank_c * self._bet_pct()))

    def _hi_pct(self):
        # 8/5 evidence ladder for the 93-96c bucket; never below base,
        # never raised unless the bucket's lifetime net is positive
        w, l = self.hi.get("w", 0), self.hi.get("l", 0)
        if self.hi.get("pnl", 0.0) <= 0:
            return self._bet_pct()
        if w + l >= HI_STEP2_N:
            return max(self._bet_pct(), HI_PCT2)
        if w + l >= HI_STEP1_N:
            return max(self._bet_pct(), HI_PCT1)
        return self._bet_pct()

    def _hi_cap_c(self):
        return max(150, int(self.bank_c * self._hi_pct()))

    def _hi_blocked(self):
        # weather bucket-routing rule: proven negative = no new entries
        w, l = self.hi.get("w", 0), self.hi.get("l", 0)
        return w + l >= HI_BLOCK_N and self.hi.get("pnl", 0.0) < 0

    def _core_blocked(self):
        # 8/7: same rule for the 80-92c band. Every loss the era has
        # taken came from here; if its lifetime net turns negative on
        # >= CORE_BLOCK_N realized outcomes the lane closes itself.
        w, l = self.core.get("w", 0), self.core.get("l", 0)
        return w + l >= CORE_BLOCK_N and self.core.get("pnl", 0.0) < 0

    def _open_cap_c(self):
        return int(self.bank_c * OPEN_PCT)

    def _halt_c(self):
        return max(200, int(self.bank_c * HALT_PCT))

    def _kelly(self):
        """Evidence-earned sizing: half-Kelly only after 100+ LIVE
        settlements with positive realized P&L (net of fees)."""
        if (self.wins + self.losses) >= KELLY_PROVEN_N and self.realized_c > 0:
            return KELLY_PROVEN
        return KELLY

    # ---- lifecycle ----
    def _roll_day(self):
        if today() != self.day:
            self.day = today()
            self.day_pnl_c = 0.0
            self.halted = False

    def check_orders(self):
        """Promote fills on our taker orders; cancel anything unfilled past
        REST_MAX_MIN (a taker that didn't fill = the ask moved; re-signal
        next cycle rather than resting into adverse selection)."""
        if not self.pending:
            return
        resting_ids, fills_by_oid = set(), None
        if self.client is not None:
            try:
                for ro in self.client.get_resting_orders():
                    resting_ids.add(ro.get("order_id") or ro.get("id"))
                fills_by_oid = {}
                for f in self.client.get_fills(limit=100):
                    fo = f.get("order_id")
                    fc = int(round(float(f.get("count_fp") or f.get("count") or 0)))
                    fills_by_oid[fo] = fills_by_oid.get(fo, 0) + fc
            except Exception:
                return
        nowdt = datetime.datetime.now()
        for oid, o in list(self.pending.items()):
            seen = int(o.get("filled_seen", 0))
            if self.client is not None and oid not in resting_ids:
                filled = (max(0, fills_by_oid.get(oid, 0) - seen)
                          if fills_by_oid is not None
                          else max(0, o["count"] - seen))
                if filled > 0:
                    self._promote(oid, o, filled)
                elif seen == 0:
                    self.canceled += 1
                del self.pending[oid]
                continue
            if self.client is not None and fills_by_oid is not None:
                new = max(0, fills_by_oid.get(oid, 0) - seen)
                if new > 0:
                    self._promote(oid, o, new)
                    o["filled_seen"] = seen + new
            try:
                age_m = (nowdt - datetime.datetime.fromisoformat(o["ots"])
                         ).total_seconds() / 60.0
            except Exception:
                age_m = 0
            if age_m > REST_MAX_MIN:
                if self.client is not None:
                    try:
                        self.client.cancel_order(oid)
                    except Exception:
                        continue
                if int(o.get("filled_seen", 0)) == 0:
                    self.canceled += 1
                del self.pending[oid]

    def _promote(self, oid, o, filled):
        fee = fee_cents(o["entry"], filled, taker=True)
        self.fees_c += fee
        tk = o["ticker"]
        if tk in self.bets:
            b = self.bets[tk]
            tot = b["count"] + filled
            b["entry"] = round((b["entry"] * b["count"]
                                + o["entry"] * filled) / tot, 1)
            b["count"] = tot
            b["fee"] = b.get("fee", 0) + fee
        else:
            self.bets[tk] = {"side": o["side"], "entry": o["entry"],
                             "count": filled, "fee": fee,
                             "pside": o["pside"], "name": o.get("name", tk),
                             "event": o.get("event", ""),
                             "peak": o.get("peak", o["entry"]),
                             "band": o.get("band", "core"),
                             "ots": o.get("ots", now()), "era": ERA}
        self._log([now(), "FILL", self.mode, tk, o.get("name", "")[:60],
                   o["side"], round(o["pside"], 3), o["entry"], filled,
                   "", "", oid])

    def mirror(self):
        """Kalshi-truth display: account positions OUTSIDE the weather
        universe, verbatim; sync count vs our book."""
        if self.client is None:
            return
        try:
            wx = _weather_prefixes()
            kp = []
            for p in self.client.get_positions():
                v = p.get("position_fp")
                pos = int(round(float(v))) if v is not None else int(p.get("position") or 0)
                if pos == 0:
                    continue
                tk = p.get("ticker") or ""
                if tk.split("-")[0] in wx:
                    continue
                kp.append({"ticker": tk,
                           "side": "yes" if pos > 0 else "no",
                           "count": abs(pos)})
            self.k_positions = kp
            mine = {tk: (b["side"], int(b["count"]))
                    for tk, b in self.bets.items()}
            diffs = sum(1 for r in kp
                        if mine.get(r["ticker"]) != (r["side"], r["count"]))
            diffs += sum(1 for tk in mine
                         if tk not in {r["ticker"] for r in kp})
            self.sync_diffs = diffs
        except Exception:
            pass

    def stop_check(self):
        if not self.bets:
            return 0
        quotes = {}
        try:
            with dcfg._cfg():
                quotes = dw.DriftWide._quotes(self, list(self.bets))
        except Exception:
            return 0
        stopped = 0
        for tk, b in list(self.bets.items()):
            q = quotes.get(tk)
            if not q or not q[0] or not q[1]:
                continue
            yb, ya = q
            mid = (yb + ya) / 2.0
            smid = mid if b["side"] == "yes" else 100 - mid
            if smid >= STOP_C:
                continue
            bid = yb if b["side"] == "yes" else 100 - ya
            if bid <= 0:
                continue
            if self.client is not None:
                try:
                    self.client.create_order(tk, action="sell",
                                             side=b["side"], count=b["count"],
                                             price_cents=bid)
                except Exception:
                    continue
            exit_fee = fee_cents(bid, b["count"], taker=True)
            net = (bid - b["entry"]) * b["count"] - b.get("fee", 0) - exit_fee
            self.realized_c += net
            self._day_add(net)
            self.day_pnl_c += net
            self.fees_c += exit_fee
            if self.client is None:
                self.dry_balance_c += bid * b["count"] - exit_fee
            # 8/7 TRUTH FIX: a stop-out is a REALIZED outcome and was
            # never counted in self.wins/self.losses - the headline
            # record read 125-1 while six positions had been stopped
            # out for -$7.33. Count every realized exit by P&L sign
            # (same convention as the weather book's nickel ledger).
            # kelly_n reads wins+losses, so this also stops the sizing
            # gate being fed a record that never happened.
            self.stops += 1
            self.wins += int(net > 0)
            self.losses += int(net <= 0)
            lane = self.hi if b.get("band") == "hi" else self.core
            lane["w" if net > 0 else "l"] += 1
            lane["pnl"] = round(lane["pnl"] + net / 100.0, 2)
            self.history.append({"name": b.get("name", tk), "tk": tk,
                                 "side": b["side"], "pside": b["pside"],
                                 "entry": b["entry"], "count": b["count"],
                                 "outcome": None, "stopped": True,
                                 "band": b.get("band", "core"),
                                 "exit_px": bid,
                                 "pnl": round(net / 100.0, 2),
                                 "ts": now(), "ots": b.get("ots", ""),
                                 "era": ERA})
            self._log([now(), "STOP", self.mode, tk, b.get("name", "")[:60],
                       b["side"], round(b["pside"], 3), bid, b["count"],
                       "", round(net / 100.0, 2), ""])
            del self.bets[tk]
            stopped += 1
        return stopped

    def settle(self):
        for tk, b in list(self.bets.items()):
            res = fetch_result(tk)
            if res is None:
                continue
            won = (res == b["side"])
            payout = 100 if won else 0
            net = (payout - b["entry"]) * b["count"] - b.get("fee", 0)
            self.realized_c += net
            self._day_add(net)
            self.day_pnl_c += net
            self.wins += int(won)
            self.losses += int(not won)
            if self.client is None:
                self.dry_balance_c += payout * b["count"]
            lane = self.hi if b.get("band") == "hi" else self.core
            lane["w" if won else "l"] += 1
            lane["pnl"] = round(lane["pnl"] + net / 100.0, 2)
            self.history.append({"name": b.get("name", tk), "tk": tk,
                                 "side": b["side"], "pside": b["pside"],
                                 "entry": b["entry"], "count": b["count"],
                                 "outcome": 1 if won else 0,
                                 "band": b.get("band", "core"),
                                 "pnl": round(net / 100.0, 2),
                                 "ts": now(), "ots": b.get("ots", ""),
                                 "era": ERA})
            self._log([now(), "SETTLE", self.mode, tk,
                       b.get("name", "")[:60], b["side"],
                       round(b["pside"], 3), b["entry"], b["count"],
                       1 if won else 0, round(net / 100.0, 2), ""])
            del self.bets[tk]

    def _placed_today(self):
        t = today()
        n = sum(1 for b in list(self.bets.values())
                + list(self.pending.values())
                if (b.get("ots") or "")[:10] == t)
        n += sum(1 for h in self.history if (h.get("ots") or "")[:10] == t)
        return n

    def place(self, mkts=None):
        if self.day_pnl_c <= -self._halt_c():
            self.halted = True
            return 0
        try:
            balance_c = self.balance_c()
        except Exception:
            return 0
        self.refresh_bank(balance_c)
        if mkts is None:
            mkts = fetch_crypto_mkts()      # 8/6: direct, no sweep lottery
            if not mkts:
                try:
                    with dcfg._cfg():
                        mkts = dw.find_wide_markets()
                except Exception:
                    return 0
        budget = MAX_PER_DAY - self._placed_today()
        ev_keys = {b.get("event", "") for b in
                   list(self.bets.values()) + list(self.pending.values())}
        pend_tks = {o["ticker"] for o in self.pending.values()}
        cands = []
        for mk in mkts:
            tk = mk["ticker"]
            if NO_15M and _is_15m(tk, mk.get("name", "")):
                continue    # coin flips at the wire, not convergence
            bid, ask = mk["yes_bid"], mk["yes_ask"]
            if bid <= 0 or ask <= 0 or (ask - bid) > MAX_SPREAD:
                continue
            vol_floor = (MIN_VOL24_LATE
                         if float(mk.get("hrs", 99) or 99) <= HOURLY_H
                         else MIN_VOL24)
            if float(mk.get("vol", 0) or 0) < vol_floor:
                continue
            if tk in self.bets or tk in pend_tks or mk["event"] in ev_keys:
                continue
            mid = (bid + ask) / 2.0
            if mid >= 80:
                side, e_bid, e_ask, smid = "yes", bid, ask, mid
            elif mid <= 20:
                side, e_bid, e_ask, smid = "no", 100 - ask, 100 - bid, 100 - mid
            else:
                continue
            if e_ask < ENTRY_MIN or e_ask > PROBE_MAX or e_bid < 1:
                continue
            band = "hi" if e_ask > ENTRY_MAX else "core"
            if self._hi_blocked() if band == "hi" else self._core_blocked():
                continue      # proven-negative lane: closed
            cands.append((smid, mk, side, e_bid, e_ask, band))
        cands.sort(key=lambda c: -c[0])
        placed = 0
        for smid, mk, side, e_bid, e_ask, band in cands:
            if placed >= budget:
                break
            if mk["event"] in ev_keys:
                continue
            tk = mk["ticker"]
            pside = smid / 100.0
            # Kelly at the BID (the signal), cost at the ASK (the toll)
            b_odds = (100 - e_bid) / e_bid
            f_star = max(0.0, pside - (1 - pside) / b_odds) * self._kelly()
            pct = self._hi_pct() if band == "hi" else self._bet_pct()
            cap_c = self._hi_cap_c() if band == "hi" else self._bet_cap_c()
            size = int(min(f_star, pct) * self.bank_c // e_ask)
            if size < 1:
                continue
            while size > 1 and e_ask * size > cap_c:
                size -= 1
            if e_ask * size > cap_c:
                continue
            if self.open_cost_c() + e_ask * size > self._open_cap_c():
                continue
            if balance_c - e_ask * size < RESERVE_C:
                continue
            oid = f"cl-{self.placed + 1}"
            if self.client is not None:
                try:
                    resp = self.client.create_order(
                        tk, action="buy", side=side, count=size,
                        price_cents=e_ask)
                    ro = resp.get("order") or {}
                    oid = (ro.get("order_id") or ro.get("id")
                           or resp.get("order_id") or resp.get("id") or oid)
                except Exception:
                    continue
            o = {"ticker": tk, "side": side, "entry": e_ask, "count": size,
                 "pside": pside, "name": mk.get("name", tk),
                 "event": mk["event"], "peak": smid, "exec": "taker",
                 "band": band,
                 "filled_seen": 0, "ots": now(), "era": ERA}
            self.pending[oid] = o
            ev_keys.add(mk["event"])     # a strike ladder is ONE opinion
            self.placed += 1
            placed += 1
            balance_c -= e_ask * size
            self._log([now(), "TAKER", self.mode, tk,
                       mk.get("name", "")[:60], side, round(pside, 3),
                       e_ask, size, "", "", oid])
            if self.client is None:
                self.dry_balance_c -= e_ask * size
                self._promote(oid, o, size)
                del self.pending[oid]
        return placed

    def step(self):
        self._roll_day()
        self.check_orders()
        self.mirror()
        self.settle()
        self.stop_check()
        self.place()
        try:
            bal = self.balance_c()
        except Exception:
            bal = None
        self.save(balance_c=bal)
