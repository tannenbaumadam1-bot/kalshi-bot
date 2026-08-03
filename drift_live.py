#!/usr/bin/env python3
"""Drift momentum LIVE executor - same brain as drift_paper, real orders.

The drift book is the first to pass its calibration gate (53-0 at settlement,
era drift1) and Adam ordered live prep 2026-07-23. Per Adam ("the bot exactly
as it is paper trading but for real") this executor mirrors the FULL paper
book: maker-only entries (join the side bid), level trigger >=80c / climb
trigger 65-80c (+2c on rising volume, same-day only), momentum stop <50c,
trailing exit 15c off peak, one bet per city-day event, NICKEL lane (>=95c
mid, entry 93-96c, 5 lanes, own event ledger, size steps 10->15->20 on <=96c
proof, EXCLUDED from the gate), PYRAMIDING (adds on +10c runners, max 2),
probe stakes until the LIVE book passes its OWN 30-bet gate (era "dlive1").

Risk caps come from config_live.yaml `risk_drift` (falling back to defaults
sized to the paper book, NOT the weather caps - a nickel is ~$9.40/bet):
  max_position_dollars 2 (regular bets) / nickels exempt up to their own size
  max_open_dollars 60 / max_daily_loss_dollars 12 (one nickel gap loss
  survives, a second halts the day) / min_cash_reserve_dollars 2.

MODES (same safety ladder as weather_live):
  DRY   - full pipeline, logs every would-be order, sends NOTHING. Default.
  DEMO  - real orders to Kalshi's demo exchange (KALSHI_ENV=demo).
  LIVE  - real money. Requires ALL of:
            1. config_live.yaml api.key_id set + private key file present
            2. environment KALSHI_DRIFT_LIVE=1
            3. arm file logs/DRIFT_LIVE_ARMED exists (or --yes-live + typed LIVE)

Hard caps (config_live.yaml risk.*, enforced before every order):
  max_position_dollars / max_open_dollars / max_daily_loss_dollars /
  min_cash_reserve_dollars.

Run:   python3 drift_live.py             (interactive)
       python3 drift_live.py --once      (single cycle, for tests/cron)
Service: deploy/kalshi-drift-live.service (disabled by default).
State -> logs/drift_live_state.json (dashboard picks it up)
Bets  -> logs/drift_live_bets.csv
"""
from __future__ import annotations
import os, sys, json, csv, time, datetime

import yaml

import weather_edge as we
import drift_paper as dp
from kalshibot.fees import fee_cents
from weather_paper import fetch_result

CONFIG = "config_live.yaml"
STATE = os.path.join("logs", "drift_live_state.json")
BETS = os.path.join("logs", "drift_live_bets.csv")
ARM_FILE = os.path.join("logs", "DRIFT_LIVE_ARMED")
LIVE_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

# THE PURSUIT LADDER (8/3, Adam: 'fix this, this is unacceptable' - 49/49
# expired joins would have WON, $25.01 forfeited). Root causes found: the
# requote refused to chase past DRIFT_MAX_ENTRY (~92c) while winners ran
# to 95c+; one-sided books (no ask near settlement) were skipped
# entirely; and the 2h stale window meant everything was long gone.
# Now: join -> requote every cycle up to 96c -> unfilled at 45min
# converts to a taker cross (chase cap 8c). Politeness has a deadline.
REST_MAX_H = float(os.environ.get("DRIFT_LIVE_REST_MAX_H", "0.75"))
CHASE_MAX_E = int(os.environ.get("DRIFT_LIVE_CHASE_MAX_E", "96"))
# Nickel trail-bleed fix (7/25: trail exits cost -$5+ on a lane that was 3-0
# at settlement - wobble papercuts exceeded the gap risk they insure against).
# Live nickels now HOLD TO SETTLEMENT like the original design; the <50c
# hard stop remains as the disaster brake. Re-enable with =1 if it backfires.
NICKEL_TRAIL = os.environ.get("DRIFT_LIVE_NICKEL_TRAIL", "0") == "1"
# Trail-exit removal, ALL lanes (7/27, Adam-approved): exit autopsy graded
# every live exit against eventual settlement - 4 of 5 would have WON, and
# exiting cost -$1.56. Same wobble-tax the nickels paid. Level/climb now
# also hold to settlement; the <50c momentum stop stays as the disaster
# brake. Re-enable trailing with =1 if the autopsy verdict flips.
TRAIL_ON = os.environ.get("DRIFT_LIVE_TRAIL", "0") == "1"
# --- Compounding caps (7/27, Adam-approved; gate passed at 60 settled) ---
# Fixed go-live caps ($2/bet) strangled quarter-Kelly the moment the book
# earned scale mode. Caps are now % of NAV (balance + filled-position cost),
# refreshed every place() cycle: they GROW as Leonard compounds and SHRINK
# in drawdown. Floors keep probes viable. Set DYN_CAPS=0 to freeze.
DYN_CAPS = os.environ.get("DRIFT_LIVE_DYN_CAPS", "1") == "1"
BET_PCT = float(os.environ.get("DRIFT_LIVE_BET_PCT", "0.03"))    # per bet
OPEN_PCT = float(os.environ.get("DRIFT_LIVE_OPEN_PCT", "0.60"))  # exposure
HALT_PCT = float(os.environ.get("DRIFT_LIVE_HALT_PCT", "0.10"))  # day loss
BET_FLOOR_C = 200    # a probe must always be placeable
HALT_FLOOR_C = 200
# Leonard's era began at arming (7/23 19:10 UTC). The settlements endpoint
# returns the ACCOUNT'S LIFETIME history - Adam's pre-bot trades (NCAA/US
# Open, Aug-Sep 2025, hundreds of contracts) must never pollute the
# scoreboard. ISO strings compare lexicographically.
LIVE_EPOCH = os.environ.get("DRIFT_LIVE_EPOCH", "2026-07-23T19:10:00")
# --- Execution engine (7/25: ~40% fill rate, fills skewed adverse) ---
# Requote: an unfilled maker join whose market ran away >= REQUOTE_C gets
# cancelled and re-joined at the new bid (instead of dying stale at 2h).
REQUOTE_C = int(os.environ.get("DRIFT_LIVE_REQUOTE_C", "2"))
REQUOTE_MAX = int(os.environ.get("DRIFT_LIVE_REQUOTE_MAX", "2"))
# Taker: on high-certainty level/climb signals with a thin spread, pay the
# 1-2c toll instead of missing the winner entirely.
TAKER_ON = os.environ.get("DRIFT_LIVE_TAKER", "1") == "1"
TAKER_MIN_SMID = float(os.environ.get("DRIFT_LIVE_TAKER_SMID", "84"))
TAKER_MAX_SPREAD = int(os.environ.get("DRIFT_LIVE_TAKER_SPREAD", "4"))
# 7/28 widening (Adam-approved): miss-autopsy day one - 9/9 canceled
# unfilled orders would have WON, $3.44 forfeited to patience. Was 88/2.
# Live disaster stop (7/28, Adam-approved): exit autopsy says 5/6 stops
# would have WON (+ today's 4 stops were all intraday nowcast wobbles that
# recovered) - weather favorites routinely dip through 50c and settle
# green. Only a true collapse (<35c) gets cut now. Paper brain keeps 50.
STOP_C = float(os.environ.get("DRIFT_LIVE_STOP_C", "35"))
# --- Bucket routing (7/25): capital flows ONLY to trigger x entry-band
# cells that aren't proven losers on the live ledger. ---
BUCKET_GATE_ON = os.environ.get("DRIFT_LIVE_BUCKET_GATE", "1") == "1"
BUCKET_MIN_N = int(os.environ.get("DRIFT_LIVE_BUCKET_MIN_N", "8"))
# Evidence-weighted Kelly (7/28, Adam: 'increase positions as we
# accumulate gains'): a bucket that has PROVEN itself on the live ledger
# (n >= KELLY_PROVEN_N settled, net > 0) earns half-Kelly sizing; every
# unproven lane stays at quarter-Kelly. Aggression is earned, never
# assumed - and a proven bucket that turns negative falls back (or gets
# bucket-blocked outright). NAV %-caps still bound everything.
KELLY_BASE = float(os.environ.get("DRIFT_LIVE_KELLY_BASE", "0.25"))
KELLY_PROVEN_MULT = float(os.environ.get("DRIFT_LIVE_KELLY_PROVEN", "0.5"))
KELLY_PROVEN_N = int(os.environ.get("DRIFT_LIVE_KELLY_PROVEN_N", "10"))
# Nickel NAV guardrails (7/29, Adam-approved): the lane's payoff is
# ~1-to-6 against (a loss eats ~6 wins) and the contract ladder was
# escalating in raw lots while every regular bet scaled with NAV. Now no
# single nickel may cost more than NICKEL_POS_PCT of NAV and the whole
# lane (filled + resting) stays under NICKEL_LANE_PCT - same trades,
# sized to survive the bad week. Ladder (10->15->20) unchanged.
NICKEL_POS_PCT = float(os.environ.get("DRIFT_LIVE_NICKEL_POS_PCT", "0.10"))
NICKEL_LANE_PCT = float(os.environ.get("DRIFT_LIVE_NICKEL_LANE_PCT", "0.30"))
# Cross-on-expiry (7/30, Adam-approved): miss-autopsy hit 20/20 - EVERY
# unfilled cancel went on to WIN, $10.90 forfeited vs +$3.09 era profit.
# When a maker join goes stale (2h) and the signal still holds (side-mid
# >= our entry, ask within the trigger's band and <= CROSS_CHASE cents
# above it), pay the ask instead of dying on the vine. All caps, bucket
# blocks and the NAV nickel guardrails still apply to the cross.
CROSS_EXPIRY = os.environ.get("DRIFT_LIVE_CROSS_EXPIRY", "1") == "1"
CROSS_MAX_CHASE = int(os.environ.get("DRIFT_LIVE_CROSS_CHASE", "8"))
# THE CONCENTRATION PACKAGE (7/31, Adam-approved). Bucket attribution
# after 8 days live: ALL profit (+$7.60) came from entries at 80-96c
# (level:80-84 +3.54, nickel +4.06); EVERY band below 80c lost money on
# BOTH triggers (-$8.10 over 36 settled, no exceptions ever). 1) Hard
# entry floor at 80c - no more ~$2 'tuition' per band while the bucket
# router accumulates its n>=8 evidence; the climb trigger (65-80c mids)
# is retired by construction. 2) Taker-FIRST in proven buckets: 28/28
# expired joins would have WON ($14.59 forfeited) - on a half-Kelly
# lane with a tight spread, pay the ask immediately instead of queueing.
ENTRY_FLOOR = int(os.environ.get("DRIFT_LIVE_ENTRY_FLOOR", "80"))
CYCLE_S = int(os.environ.get("DRIFT_LIVE_CYCLE_S", "600"))
GATE_MIN_N = dp.GATE_MIN_N
GATE_MAX_GAP = dp.GATE_MAX_GAP
PROBE_COST_CENTS = dp.PROBE_COST_CENTS
ERA = "dlive1"
# --- TWO BOOKS, ONE ACCOUNT (8/3, Adam: crypto live, 50/50 NAV split) ---
# Universe fence: this executor manages ONLY weather-series tickers; the
# crypto executor (crypto_live.py, same process) manages only its own.
# Without the fence, reconcile_positions would ADOPT the crypto book's
# positions and stop-sell them. Capital: this book's bankroll =
# WX_ALLOC x account NAV (balance + BOTH books' position cost).
WX_ALLOC = float(os.environ.get("DRIFT_WX_ALLOC", "0.5"))
CRYPTO_STATE_PATH = os.path.join("logs", "crypto_live_state.json")


def _is_wx(tk):
    return (tk or "").split("-")[0] in we.SERIES


def _crypto_cost_c():
    try:
        d = json.load(open(CRYPTO_STATE_PATH))
        return sum(float(b.get("entry", 0)) * int(b.get("count", 0))
                   for b in (d.get("bets") or {}).values())
    except Exception:
        return 0


# Self-restart on deploy (7/25: kalshi-update.timer never restarted this
# service, so every drift_live.py fix needed a manual DO-console restart -
# and the console kept eating commands/expiring). Now: when the source files
# change on disk (git pull), the loop exits cleanly and systemd
# Restart=always brings it back on the new build within 30s.
_SRC_FILES = [os.path.abspath(__file__), os.path.abspath(dp.__file__)]
try:
    import crypto_live as _cl_mod
    _SRC_FILES.append(os.path.abspath(_cl_mod.__file__))
except Exception:
    _cl_mod = None
_SRC_MTIMES = {f: os.path.getmtime(f) for f in _SRC_FILES if os.path.exists(f)}


def _code_changed():
    for f, m in _SRC_MTIMES.items():
        try:
            if os.path.getmtime(f) != m:
                return True
        except OSError:
            pass
    return False


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today():
    return datetime.date.today().isoformat()


class DriftLive:
    """Live executor. client=None -> DRY mode with a simulated $100 balance."""

    def __init__(self, client=None, mode="DRY"):
        cfg = {}
        try:
            cfg = yaml.safe_load(open(CONFIG)) or {}
        except Exception:
            pass
        r = (cfg.get("risk_drift") or {}) if isinstance(cfg, dict) else {}
        self.max_bet_c = int(float(r.get("max_position_dollars", 2.0)) * 100)
        self.max_open_c = int(float(r.get("max_open_dollars", 60.0)) * 100)
        self.max_day_loss_c = int(float(r.get("max_daily_loss_dollars", 12.0)) * 100)
        self.reserve_c = int(float(r.get("min_cash_reserve_dollars", 2.0)) * 100)
        self.client = client
        self.mode = mode
        self.bets = {}        # ticker -> filled position
        self.pending = {}     # order_id -> resting order intent
        self.last_mid = {}    # ticker -> yes-mid at previous scan (momentum)
        self.last_vol = {}    # ticker -> 24h volume at previous scan
        self.realized_c = 0.0
        self.fees_c = 0.0
        self.wins = 0
        self.losses = 0
        self.placed = 0
        self.canceled = 0
        self.day = today()
        self.day_pnl_c = 0.0
        self.halted = False
        self.history = []
        self.settled_tks = []    # tickers already settled by us (anti-double-settle)
        self.k_settlements = []  # Kalshi's OWN settlement records (the proof)
        self.k_exit_realized_c = 0.0   # Kalshi's realized pnl on open markets
        self.day_nav0_c = None   # NAV anchor at day start (for true today-P&L)
        self.autopsy = []        # every exit, graded vs eventual settlement
        self.miss = []           # every unfilled cancel, graded vs settlement
        self.exec_stats = {}     # maker/taker placed+filled, requotes
        self.k_positions = []    # Kalshi's positions, verbatim (the display)
        self.k_resting = []      # Kalshi's resting orders, verbatim
        self.dry_balance_c = 10000
        self.load()

    # ---- persistence ----
    def load(self):
        if os.path.exists(STATE):
            try:
                d = json.load(open(STATE))
                if d.get("mode") != self.mode:
                    return          # fresh book on any mode change (DRY->LIVE etc.)
                for k in ("bets", "pending", "last_mid", "last_vol",
                          "realized_c", "fees_c", "wins", "losses", "placed",
                          "canceled", "day", "day_pnl_c", "history",
                          "settled_tks", "k_settlements", "k_exit_realized_c",
                          "day_nav0_c", "autopsy", "miss", "exec_stats",
                          "k_positions", "k_resting", "dry_balance_c"):
                    if k in d:
                        setattr(self, k, d[k])
            except Exception:
                pass

    def _real_record(self):
        """The HONEST record (Adam 7/25: 'bets have lost'): every realized
        outcome counts - a stopped/trailed loser IS a loss even though it
        never reached settlement. Deduped by ticker+open-time so any
        double-booked settle rows can't inflate it."""
        seen, rw, rl = set(), 0, 0
        for h in self.history:
            k = (h.get("tk") or (h.get("city"), h.get("strike"),
                                 h.get("kind"), h.get("cap"), h.get("hl")),
                 h.get("side"), h.get("ots"))
            if k in seen:
                continue
            seen.add(k)
            p = h.get("pnl") or 0
            if p > 0:
                rw += 1
            elif p < 0:
                rl += 1
        return rw, rl

    # ---- bucket calibration & capital routing ----
    @staticmethod
    def _bucket_key(trig, entry):
        e = entry or 0
        if e < 70:
            band = "<70"
        elif e < 80:
            band = "70-79"
        elif e < 85:
            band = "80-84"
        elif e < 90:
            band = "85-89"
        elif e < 93:
            band = "90-92"
        else:
            band = "93-96"
        return f"{trig}:{band}"

    def _bucket_stats(self):
        """Live evidence per trigger x entry-band (deduped, ALL realized
        outcomes). A bucket with n >= BUCKET_MIN_N and negative net is
        BLOCKED - capital only flows where the live edge isn't disproven."""
        agg, seen = {}, set()
        for h in self.history:
            k = (h.get("tk") or (h.get("city"), h.get("strike")), h.get("ots"))
            if k in seen or h.get("trig") in (None, "adopt"):
                continue
            seen.add(k)
            bk = self._bucket_key(h.get("trig"), h.get("entry"))
            a = agg.setdefault(bk, {"n": 0, "wins": 0, "net": 0.0})
            a["n"] += 1
            p = h.get("pnl") or 0
            a["wins"] += 1 if p > 0 else 0
            a["net"] = round(a["net"] + p, 2)
        for bk, a in agg.items():
            a["blocked"] = bool(BUCKET_GATE_ON and a["n"] >= BUCKET_MIN_N
                                and a["net"] < 0)
        return agg

    def _bucket_blocked(self, bstats, trig, entry):
        a = bstats.get(self._bucket_key(trig, entry))
        return bool(a and a.get("blocked"))

    def _kelly_frac(self, bstats, trig, entry):
        """Evidence-weighted Kelly: proven buckets earn half-Kelly."""
        a = bstats.get(self._bucket_key(trig, entry))
        if a and a.get("n", 0) >= KELLY_PROVEN_N and a.get("net", 0) > 0:
            return KELLY_PROVEN_MULT
        return KELLY_BASE

    # ---- exit autopsy: grade every exit against eventual settlement ----
    def autopsy_check(self, max_lookups=10):
        done = 0
        for row in self.autopsy:
            if row.get("res") is not None or done >= max_lookups:
                continue
            res = fetch_result(row["tk"])
            if res is None:
                continue
            done += 1
            won = (res == row["side"])
            would = ((100 if won else 0) - row["entry"]) * row["count"] - row.get("fee", 0)
            row["res"] = res
            row["would_pnl"] = round(would / 100.0, 2)
            row["saved"] = round(row["exit_pnl"] - row["would_pnl"], 2)

    def _autopsy_summary(self):
        graded = [r for r in self.autopsy if r.get("res") is not None]
        return {"autopsy_exits": len(self.autopsy),
                "autopsy_n_settled": len(graded),
                "autopsy_saved": round(sum(r.get("saved", 0) for r in graded), 2),
                "autopsy_would_won": sum(1 for r in graded
                                         if r.get("would_pnl", 0) > 0)}

    # ---- miss-autopsy (7/27): grade the road NOT taken. Every canceled
    # unfilled buy is scored against eventual settlement so "should we
    # cross the spread more?" is answered by the ledger, not opinion. ----
    def _log_miss(self, o, unfilled):
        if unfilled <= 0:
            return
        self.miss.append({"tk": o["ticker"], "side": o["side"],
                          "entry": o["entry"], "count": int(unfilled),
                          "trig": o.get("trig"),
                          "pside": round(o.get("pside", 0), 3),
                          "ots": o.get("ots", ""), "cts": now(), "res": None})
        self.miss = self.miss[-200:]

    def _cross_expiring(self, o, count, q=None):
        """A stale unfilled join whose signal still holds crosses the
        spread as a taker instead of being forfeited. Returns True if a
        replacement taker order was placed for `count` (possibly trimmed
        to caps) contracts."""
        if count <= 0:
            return False
        tk = o["ticker"]
        if q is None:
            try:
                q = dp.DriftPaper._quotes(self, [tk]).get(tk)
            except Exception:
                q = None
        if not q or not q[0] or not q[1]:
            return False
        yb, ya = q
        bid_s = yb if o["side"] == "yes" else 100 - ya
        ask_s = ya if o["side"] == "yes" else 100 - yb
        smid = (bid_s + ask_s) / 2.0
        max_e = (dp.NICKEL_MAX_ENTRY if o.get("trig") == "nickel"
                 else CHASE_MAX_E)
        if (ask_s <= 0 or ask_s > max_e or smid < o["entry"]
                or ask_s - o["entry"] > CROSS_MAX_CHASE):
            return False
        if (o.get("trig") != "nickel"
                and self._bucket_blocked(self._bucket_stats(),
                                         o.get("trig"), ask_s)):
            return False
        size = int(count)
        if o.get("trig") == "nickel":
            nav_c = getattr(self, "last_nav_c", 0)
            if nav_c:
                cap = int(nav_c * NICKEL_POS_PCT)
                while size > 1 and ask_s * size > cap:
                    size -= 1
                if ask_s * size > cap:
                    return False
        else:
            while size > 1 and ask_s * size > self.max_bet_c:
                size -= 1
            if ask_s * size > self.max_bet_c:
                return False
        try:
            bal = self.balance_c()
        except Exception:
            return False
        if bal - ask_s * size < self.reserve_c:
            return False
        if self.open_cost_c() + ask_s * size > self.max_open_c:
            return False
        new_oid = f"xc-{self.placed + 1}"
        if self.client is not None:
            try:
                resp = self.client.create_order(tk, action="buy",
                                                side=o["side"], count=size,
                                                price_cents=ask_s)
                ro = resp.get("order") or {}
                new_oid = (ro.get("order_id") or ro.get("id")
                           or resp.get("order_id") or resp.get("id")
                           or new_oid)
            except Exception:
                return False
        no = dict(o)
        no.update({"entry": ask_s, "count": size, "exec": "taker",
                   "filled_seen": 0, "requotes": 0, "ots": now()})
        self.pending[new_oid] = no
        self.placed += 1
        self.exec_stats["placed_taker"] = self.exec_stats.get("placed_taker", 0) + 1
        self.exec_stats["cross_expiry"] = self.exec_stats.get("cross_expiry", 0) + 1
        if self.client is None:
            self.dry_balance_c -= ask_s * size
            self._promote_fill(new_oid, no, size)
            del self.pending[new_oid]
        self._log([now(), "XCROSS", self.mode, o["city"], o["strike"],
                   o["hl"], o["side"], round(o["pside"], 3), ask_s, size,
                   "", "", new_oid])
        return True

    def miss_check(self, max_lookups=10):
        done = 0
        for row in self.miss:
            if row.get("res") is not None or done >= max_lookups:
                continue
            res = fetch_result(row["tk"])
            if res is None:
                continue
            done += 1
            won = (res == row["side"])
            mfee = fee_cents(row["entry"], row["count"], taker=False)
            would = ((100 if won else 0) - row["entry"]) * row["count"] - mfee
            row["res"] = res
            row["would_pnl"] = round(would / 100.0, 2)

    def _miss_summary(self):
        graded = [r for r in self.miss if r.get("res") is not None]
        return {"miss_n": len(self.miss),
                "miss_settled": len(graded),
                "miss_would_won": sum(1 for r in graded
                                      if r.get("would_pnl", 0) > 0),
                # positive = money left on the table by not filling;
                # negative = patience dodged losers and saved money
                "miss_cost": round(sum(r.get("would_pnl", 0)
                                       for r in graded), 2)}

    def _sync_diffs(self):
        """How far our internal book diverges from Kalshi's positions.
        0 = perfect mirror; anything else is displayed loudly, never hidden."""
        if self.client is None:
            return None
        kp = {r["ticker"]: (r["side"], r["count"]) for r in self.k_positions}
        diffs = sum(1 for tk, b in self.bets.items()
                    if kp.get(tk) != (b.get("side"), int(b.get("count", 0))))
        diffs += sum(1 for tk in kp if tk not in self.bets)
        return diffs

    def save(self, balance_c=None):
        os.makedirs("logs", exist_ok=True)
        mode_gate, gate_n = self._gate()
        real_w, real_l = self._real_record()
        d = {"updated": now(), "mode": self.mode,
             "balance_c": balance_c,
             "bets": self.bets, "pending": self.pending,
             "last_mid": self.last_mid, "last_vol": self.last_vol,
             "realized_c": self.realized_c, "fees_c": self.fees_c,
             "wins": self.wins, "losses": self.losses,
             "placed": self.placed, "canceled": self.canceled,
             "day": self.day, "day_pnl_c": self.day_pnl_c,
             "day_nav0_c": self.day_nav0_c,
             "dry_balance_c": self.dry_balance_c,
             "settled_tks": self.settled_tks[-300:],
             "k_settlements": self.k_settlements[:300],
             "k_exit_realized_c": self.k_exit_realized_c,
             "autopsy": self.autopsy[-200:],
             "miss": self.miss[-200:],
             "exec_stats": self.exec_stats,
             "k_positions": self.k_positions,
             "k_resting": self.k_resting,
             "nickel": self._nickel_stats(),
             "history": self.history[-200:],
             "summary": {
                 "mode": self.mode,
                 "net": round(self.realized_c / 100, 2),
                 "wins": self.wins, "losses": self.losses,
                 "real_wins": real_w, "real_losses": real_l,
                 # Kalshi-derived truth (only shown numbers):
                 "k_wins": sum(1 for s in self.k_settlements if s["pnl"] > 0),
                 "k_losses": sum(1 for s in self.k_settlements if s["pnl"] < 0),
                 "k_settle_realized": round(sum(s["pnl"] for s in self.k_settlements), 2),
                 "k_exit_realized": round(self.k_exit_realized_c / 100.0, 2),
                 "k_realized": round(sum(s["pnl"] for s in self.k_settlements)
                                     + self.k_exit_realized_c / 100.0, 2),
                 "day_nav0": (round(self.day_nav0_c / 100.0, 2)
                              if self.day_nav0_c is not None else None),
                 "has_kalshi_truth": bool(self.k_settlements) or self.k_exit_realized_c != 0,
                 "exec": dict(self.exec_stats),
                 # mirror counts + fees straight from Kalshi's records
                 "k_open": (len(self.k_positions) if self.client is not None else None),
                 "k_resting_n": (len(self.k_resting) if self.client is not None else None),
                 "k_fees": round(sum(s.get("fee", 0) for s in self.k_settlements)
                                 + sum(p.get("fee", 0) for p in self.k_positions) / 100.0, 2),
                 "sync_diffs": self._sync_diffs(),
                 # live risk caps as currently applied (proof the dynamic
                 # NAV-% compounding is active, straight from the trader)
                 "caps": {"bet": round(self.max_bet_c / 100.0, 2),
                          "open": round(self.max_open_c / 100.0, 2),
                          "halt": round(self.max_day_loss_c / 100.0, 2),
                          "dyn": DYN_CAPS, "floor": ENTRY_FLOOR},
                 **self._autopsy_summary(),
                 **self._miss_summary(),
                 "buckets": [dict(v, bucket=k,
                                  proven=bool(not v["blocked"]
                                              and v["n"] >= KELLY_PROVEN_N
                                              and v["net"] > 0))
                             for k, v in
                             sorted(self._bucket_stats().items())],
                 "open": len(self.bets), "resting": len(self.pending),
                 "placed": self.placed, "canceled": self.canceled,
                 "fees": round(self.fees_c / 100, 2),
                 "day_pnl": round(self.day_pnl_c / 100, 2),
                 "halted": self.halted,
                 "gate": mode_gate, "gate_n": gate_n}}
        with open(STATE, "w") as f:
            json.dump(d, f)

    def _log(self, row):
        os.makedirs("logs", exist_ok=True)
        new = not os.path.exists(BETS)
        with open(BETS, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "event", "mode", "city", "strike", "hl",
                            "side", "mkt_prob", "entry_c", "count",
                            "outcome", "pnl_$", "order_id"])
            w.writerow(row)

    # ---- shared gate math (same contract as the paper book: nickels are
    # their own experiment and never count toward the drift gate) ----
    def _gate(self):
        cur = [h for h in self.history if h.get("outcome") in (0, 1)
               and h.get("trig") != "nickel"][-60:]
        n = len(cur)
        if n < GATE_MIN_N:
            return "probe", n
        expectancy = sum(h["pnl"] for h in cur) / n
        pred = sum(h["pside"] for h in cur) / n
        act = sum(h["outcome"] for h in cur) / n
        if expectancy > 0 and (pred - act) <= GATE_MAX_GAP:
            return "scale", n
        return "probe", n

    def _nickel_count(self):
        """Contracts per nickel: base 10, steps to 15/20 as the <=96c-entry
        era proves itself on the LIVE ledger (same rule as paper)."""
        rows = [h for h in self.history
                if h.get("trig") == "nickel" and h.get("outcome") in (0, 1)
                and (h.get("entry") or 99) <= dp.NICKEL_MAX_ENTRY]
        net = sum(h.get("pnl", 0) for h in rows)
        if len(rows) >= dp.NICKEL_STEP2_N and net > 0:
            return dp.NICKEL_STEP2_CT
        if len(rows) >= dp.NICKEL_STEP1_N and net > 0:
            return dp.NICKEL_STEP1_CT
        return dp.NICKEL_COUNT

    def _nickel_stats(self):
        """Honest nickel record: EVERY realized nickel outcome counts by
        P&L sign (a trail-exited nickel that lost money is a loss), deduped."""
        rows, seen = [], set()
        for h in self.history:
            if h.get("trig") != "nickel":
                continue
            k = (h.get("tk") or (h.get("city"), h.get("strike")), h.get("ots"))
            if k in seen:
                continue
            seen.add(k)
            rows.append(h)
        nk_open = sum(1 for b in list(self.bets.values())
                      + list(self.pending.values())
                      if b.get("trig") == "nickel")
        out = {"open": nk_open, "n": len(rows),
               "wins": sum(1 for h in rows if (h.get("pnl") or 0) > 0),
               "losses": sum(1 for h in rows if (h.get("pnl") or 0) < 0),
               "net": round(sum(h.get("pnl", 0) for h in rows), 2),
               "size": self._nickel_count(), "max_open": dp.NICKEL_MAX_OPEN}
        nav_c = getattr(self, "last_nav_c", 0)
        if nav_c:
            out["pos_cap"] = round(nav_c * NICKEL_POS_PCT / 100.0, 2)
            out["lane_cap"] = round(nav_c * NICKEL_LANE_PCT / 100.0, 2)
        return out

    def _roll_day(self):
        if today() != self.day:
            self.day = today()
            self.day_pnl_c = 0.0
            self.day_nav0_c = None   # re-anchor today's P&L off Kalshi NAV
            self.halted = False

    # ---- Kalshi truth sync: the exchange's own records are the ONLY
    # numbers the scoreboard shows (Adam 7/25) ----
    @staticmethod
    def _kval(row, base):
        """Read a Kalshi money/count field across both schemas:
        '<base>_dollars'/'<base>_fp' string-floats or '<base>' int cents."""
        v = row.get(base + "_dollars")
        if v is not None:
            try:
                return float(v) * 100.0
            except (TypeError, ValueError):
                return None
        v = row.get(base + "_fp")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        v = row.get(base)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def sync_kalshi_truth(self):
        """Pull Kalshi's settlements + per-market realized P&L. Every W/L and
        realized figure shown to Adam comes from here - never from our own
        bookkeeping (which double-counted and laundered losses into 'exits')."""
        if self.client is None:
            return
        try:
            rows = []
            for s in self.client.get_settlements(limit=200):
                ts = s.get("settled_time", "") or ""
                if ts and ts < LIVE_EPOCH:
                    continue        # pre-Leonard account history: not ours
                tk = s.get("ticker") or ""
                if not _is_wx(tk):
                    continue    # crypto settlements: the other book's ledger
                # payout comes split: 'revenue' (NO-side wins) + 'value'
                # (YES-side wins), both int cents; costs are dollar-strings
                rev = self._kval(s, "revenue") or 0.0
                val = self._kval(s, "value") or 0.0
                cy = self._kval(s, "yes_total_cost") or 0.0
                cn = self._kval(s, "no_total_cost") or 0.0
                # fee_cost is a dollars-STRING without the _dollars suffix
                fraw = s.get("fee_cost_dollars", s.get("fee_cost"))
                try:
                    fee = float(fraw) * 100.0 if isinstance(fraw, str) else float(fraw or 0)
                except (TypeError, ValueError):
                    fee = 0.0
                pnl_c = rev + val - cy - cn - fee
                rows.append({"tk": tk, "pnl": round(pnl_c / 100.0, 2),
                             "fee": round(fee / 100.0, 2), "ts": ts})
            self.k_settlements = rows[:300]
        except Exception:
            pass
        # THE MIRROR (Adam 7/25: "the tracker should perfectly reflect
        # kalshi"): positions and resting orders are stored VERBATIM from the
        # exchange each cycle - the dashboard renders these, never our book.
        try:
            kp = []
            for p in self.client.get_positions():
                pos = int(round(self._kval(p, "position") or 0))
                if pos == 0:
                    continue
                tk = p.get("ticker") or ""
                if not _is_wx(tk):
                    continue    # mirror shows THIS book vs ITS universe
                cnt = abs(pos)
                exp = abs(self._kval(p, "market_exposure") or 0)
                b = self.bets.get(tk) or {}
                kp.append({"ticker": tk, "side": "yes" if pos > 0 else "no",
                           "count": cnt,
                           "entry": (int(round(exp / cnt)) if exp and cnt
                                     else b.get("entry")),
                           "fee": int(round(self._kval(p, "fees_paid") or 0)),
                           "realized": round((self._kval(p, "realized_pnl") or 0) / 100.0, 2),
                           "trig": b.get("trig"), "pside": b.get("pside"),
                           **self._tk_meta(tk)})
            self.k_positions = kp
        except Exception:
            pass
        try:
            kr = []
            for o in self.client.get_resting_orders():
                tk = o.get("ticker") or ""
                if not _is_wx(tk):
                    continue    # crypto book's resting orders, not ours
                side = o.get("side") or "?"
                cnt = int(round(self._kval(o, "remaining_count")
                                or self._kval(o, "count") or 0))
                px = self._kval(o, "yes_price" if side == "yes" else "no_price")
                if px is None:
                    px = self._kval(o, "price")
                ob = next((x for x in self.pending.values()
                           if x.get("ticker") == tk), {})
                kr.append({"ticker": tk, "side": side, "count": cnt,
                           "entry": (int(round(px)) if px else ob.get("entry")),
                           "action": o.get("action", "buy"),
                           "trig": ob.get("trig"), "pside": ob.get("pside"),
                           **self._tk_meta(tk)})
            self.k_resting = kr
        except Exception:
            pass
        try:
            tot = 0.0
            for p in self.client.get_positions():
                rp = self._kval(p, "realized_pnl")
                if rp:
                    tot += rp
            self.k_exit_realized_c = round(tot, 2)
        except Exception:
            pass

    def open_cost_c(self):
        oc = sum(b["entry"] * b["count"] + b.get("fee", 0)
                 for b in self.bets.values())
        oc += sum(o["entry"] * o["count"] for o in self.pending.values())
        return oc

    def balance_c(self):
        if self.client is None:
            return self.dry_balance_c
        return self.client.get_balance_cents()

    # ---- resting order lifecycle ----
    def _promote_fill(self, oid, o, filled):
        """Fold `filled` contracts of a (possibly partial) fill into the book."""
        tk = o["ticker"]
        is_taker = o.get("exec") == "taker"
        fee = fee_cents(o["entry"], filled, taker=is_taker)
        self.fees_c += fee
        ek = "filled_taker" if is_taker else "filled_maker"
        self.exec_stats[ek] = self.exec_stats.get(ek, 0) + 1
        if tk in self.bets:
            self._merge_fill(tk, o["entry"], filled, fee)
        else:
            self.bets[tk] = {**{k: o[k] for k in
                                ("side", "entry", "city", "strike",
                                 "kind", "cap", "hl", "pside", "date",
                                 "trig", "peak")},
                             "count": filled, "fee": fee, "oid": oid,
                             "ots": o.get("ots", now()), "era": ERA}
        self._log([now(), "FILL", self.mode, o["city"], o["strike"],
                   o["hl"], o["side"], round(o["pside"], 3),
                   o["entry"], filled, "", "", oid])

    def check_orders(self):
        """Promote fills (INCLUDING partial fills on still-resting orders -
        learned live 7/23: balance dropped $6+ at '0 filled' because Kalshi
        fills resting makers incrementally), cancel stale orders, and never
        lose the filled portion of a canceled order. Also HEALS order-ids:
        if our stored oid doesn't match the resting book but an order for
        the same ticker is resting, adopt its real order_id (learned live
        7/23: a create-order response parse fallback left synthetic ids ->
        every order looked canceled one cycle later while the real orders
        kept resting and filling unmanaged)."""
        if not self.pending:
            return
        resting_ids = set()
        resting_by_tk = {}
        fills_by_oid = None
        if self.client is not None:
            try:
                for ro in self.client.get_resting_orders():
                    roid = ro.get("order_id") or ro.get("id")
                    resting_ids.add(roid)
                    resting_by_tk.setdefault(ro.get("ticker"), []).append(roid)
            except Exception:
                return                      # can't verify -> touch nothing
            # heal synthetic/mismatched oids by ticker before lifecycle checks
            owned = {oid for oid in self.pending}
            for oid, o in list(self.pending.items()):
                if oid in resting_ids:
                    continue
                for cand in resting_by_tk.get(o["ticker"], []):
                    if cand and cand not in owned:
                        self.pending[cand] = self.pending.pop(oid)
                        owned.add(cand)
                        self._log([now(), "HEAL-OID", self.mode, o["city"],
                                   o["strike"], o["hl"], o["side"],
                                   round(o["pside"], 3), o["entry"],
                                   o["count"], "", "", f"{oid}->{cand}"])
                        break
            try:
                fills_by_oid = {}
                for f in self.client.get_fills(limit=100):
                    fo = f.get("order_id")
                    fc = int(round(float(f.get("count_fp") or f.get("count") or 0)))
                    fills_by_oid[fo] = fills_by_oid.get(fo, 0) + fc
            except Exception:
                fills_by_oid = None         # fills unknown this cycle
        nowdt = datetime.datetime.now()
        for oid, o in list(self.pending.items()):
            seen = int(o.get("filled_seen", 0))
            if self.client is not None and oid not in resting_ids:
                # gone from the resting book: filled and/or canceled
                if fills_by_oid is not None:
                    filled = max(0, fills_by_oid.get(oid, 0) - seen)
                else:
                    filled = max(0, o["count"] - seen)  # assume rest filled
                if filled > 0:
                    self._promote_fill(oid, o, filled)
                if filled == 0 and seen == 0:
                    self.canceled += 1
                self._log_miss(o, o["count"] - seen - filled)
                del self.pending[oid]
                continue
            # still resting: promote any PARTIAL fills so stops/settles
            # protect those contracts immediately
            if self.client is not None and fills_by_oid is not None:
                new = max(0, fills_by_oid.get(oid, 0) - seen)
                if new > 0:
                    self._promote_fill(oid, o, new)
                    o["filled_seen"] = seen + new
            try:
                age_h = (nowdt - datetime.datetime.fromisoformat(o["ots"])).total_seconds() / 3600
            except Exception:
                age_h = 0
            if age_h > REST_MAX_H:
                if self.client is not None:
                    try:
                        self.client.cancel_order(oid)
                    except Exception:
                        continue
                unfilled = o["count"] - int(o.get("filled_seen", 0))
                crossed = CROSS_EXPIRY and self._cross_expiring(o, unfilled)
                if not crossed:
                    self._log_miss(o, unfilled)
                    if int(o.get("filled_seen", 0)) == 0:
                        self.canceled += 1
                self._log([now(), "CANCEL", self.mode, o["city"], o["strike"],
                           o["hl"], o["side"], round(o["pside"], 3),
                           o["entry"], o["count"], "", "", oid])
                del self.pending[oid]

    # ---- position reconciliation: Kalshi is the source of truth ----
    def _tk_meta(self, tk):
        """Best-effort market meta from a weather ticker (display only -
        stops/settles need side/entry/count, not meta)."""
        city, is_low = we.SERIES.get(tk.split("-")[0], ("?", False))
        strike, kind, cap = 0, "ge", None
        try:
            seg = tk.rsplit("-", 1)[1]
            if seg.startswith("B"):
                v = float(seg[1:])
                strike, kind, cap = int(v), "band", int(v) + 1
            elif seg.startswith("T"):
                strike = int(float(seg[1:]))
        except Exception:
            pass
        try:
            date = we.ticker_date(tk) or ""
        except Exception:
            date = ""
        return {"city": city, "strike": strike, "kind": kind, "cap": cap,
                "hl": "lo" if is_low else "hi", "date": date}

    def reconcile_positions(self):
        """Every cycle: adopt any position Kalshi reports that our book
        doesn't hold (orphans from missed fills - live 7/23: ~$31 of real
        fills were invisible and unprotected), sync mismatched counts, and
        drop book entries Kalshi says are flat. The exchange's portfolio is
        the ledger of record; ours is a cache."""
        if self.client is None:
            return 0
        try:
            mps = self.client.get_positions()
        except Exception:
            return 0
        def _num(p, fp_key, int_key, dollars=False):
            """Kalshi runs two schemas: new '*_fp'/'*_dollars' STRING floats
            (live 7/23) and old integer fields. Read either."""
            v = p.get(fp_key)
            if v is None:
                v = p.get(int_key)
                if v is None:
                    return 0.0
                return float(v) / (100.0 if dollars else 1.0)
            return float(v)

        by_tk = {}
        for p in mps:
            if p.get("ticker"):
                by_tk[p["ticker"]] = p
        changed = 0
        done = set(self.settled_tks)
        for tk, p in by_tk.items():
            if not _is_wx(tk):
                continue    # crypto book's turf (8/3 universe fence)
            pos = int(round(_num(p, "position_fp", "position")))
            if pos == 0:
                continue
            if tk in done:
                continue    # we already settled this - the positions API can
                            # lag settlement; re-adopting would double-count
            side = "yes" if pos > 0 else "no"
            cnt = abs(pos)
            b = self.bets.get(tk)
            if b is not None and b.get("side") == side and int(b.get("count", 0)) == cnt:
                continue
            if b is None and fetch_result(tk) is not None:
                continue    # market already settled: the payout is in the
                            # balance; adopting a stale position row would
                            # book a phantom settlement
            exposure_c = abs(_num(p, "market_exposure_dollars",
                                  "market_exposure", dollars=True)) * 100.0
            entry = (b or {}).get("entry")
            if exposure_c and cnt:
                entry = max(1, min(99, int(round(exposure_c / cnt))))
            if not entry:
                entry = 50
            meta = self._tk_meta(tk)
            keep = b or {}
            fee_c = _num(p, "fees_paid_dollars", "fees_paid", dollars=True) * 100.0
            self.bets[tk] = {"side": side, "entry": entry, "count": cnt,
                             "fee": int(round(fee_c)),
                             "pside": round(entry / 100.0, 3), **meta,
                             "trig": keep.get("trig", "adopt"),
                             "peak": float(keep.get("peak", entry)),
                             "ots": keep.get("ots", now()), "era": ERA}
            self._log([now(), "ADOPT", self.mode, meta["city"], meta["strike"],
                       meta["hl"], side, round(entry / 100.0, 3), entry, cnt,
                       "", "", ""])
            changed += 1
        for tk in list(self.bets):
            p = by_tk.get(tk)
            if p is None or int(round(_num(p, "position_fp", "position"))) == 0:
                # flat on the exchange but open in our book: settled markets
                # are handled by settle() (which runs right after and books
                # the P&L properly); anything else was closed outside us
                res = fetch_result(tk)
                if res is None:
                    self._log([now(), "DESYNC-DROP", self.mode,
                               self.bets[tk].get("city", ""),
                               self.bets[tk].get("strike", ""),
                               self.bets[tk].get("hl", ""),
                               self.bets[tk].get("side", ""), "", "", "",
                               "", "", tk])
                    del self.bets[tk]
                    changed += 1
        return changed

    # ---- settle ----
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
            if self.client is None:
                self.dry_balance_c += payout * b["count"]
            self.wins += int(won)
            self.losses += int(not won)
            self.settled_tks.append(tk)
            self.settled_tks = self.settled_tks[-300:]
            self.history.append({"tk": tk, "city": b["city"], "strike": b["strike"],
                                 "kind": b.get("kind", "ge"), "cap": b.get("cap"),
                                 "hl": b["hl"], "side": b["side"],
                                 "trig": b.get("trig"),
                                 "pside": round(b["pside"], 3), "entry": b["entry"],
                                 "count": b["count"], "outcome": 1 if won else 0,
                                 "pnl": round(net / 100, 2), "ts": now(),
                                 "ots": b.get("ots", ""), "era": ERA})
            self._log([now(), "SETTLE", self.mode, b["city"], b["strike"], b["hl"],
                       b["side"], round(b["pside"], 3), b["entry"], b["count"],
                       1 if won else 0, round(net / 100, 2), b.get("oid", "")])
            del self.bets[tk]

    # ---- momentum stop + trailing exit (taker sells, same rules as paper) ----
    def stop_check(self, quotes=None):
        if not self.bets:
            return 0
        if quotes is None:
            quotes = dp.DriftPaper._quotes(self, list(self.bets))
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
            trail_ok = NICKEL_TRAIL if b.get("trig") == "nickel" else TRAIL_ON
            fade = (smid >= STOP_C and peak - smid >= dp.FADE_DROP_C
                    and trail_ok)
            if smid >= STOP_C and not fade:
                continue
            bid = yb if b["side"] == "yes" else 100 - ya
            if bid <= 0:
                continue                      # nothing to sell into; settle decides
            cnt = b["count"]
            if self.client is not None:
                try:
                    self.client.create_order(tk, action="sell", side=b["side"],
                                             count=cnt, price_cents=bid)
                except Exception:
                    continue
            exit_fee = fee_cents(bid, cnt, taker=True)
            net = (bid - b["entry"]) * cnt - b.get("fee", 0) - exit_fee
            self.realized_c += net
            self.day_pnl_c += net
            self.fees_c += exit_fee
            if self.client is None:
                self.dry_balance_c += bid * cnt - exit_fee
            self.history.append({"tk": tk, "city": b["city"], "strike": b["strike"],
                                 "kind": b.get("kind", "ge"), "cap": b.get("cap"),
                                 "hl": b["hl"], "side": b["side"],
                                 "trig": b.get("trig"),
                                 "pside": round(b["pside"], 3),
                                 "entry": b["entry"], "count": cnt,
                                 "outcome": None, "exited": True,
                                 "stopped": not fade, "faded": fade,
                                 "exit_px": bid,
                                 "pnl": round(net / 100, 2), "ts": now(),
                                 "ots": b.get("ots", ""), "era": ERA})
            self._log([now(), "FADE" if fade else "STOP", self.mode, b["city"],
                       b["strike"], b["hl"], b["side"], round(b["pside"], 3),
                       bid, cnt, "", round(net / 100, 2), b.get("oid", "")])
            # exit autopsy: grade this exit against eventual settlement
            self.autopsy.append({"tk": tk, "side": b["side"],
                                 "entry": b["entry"], "count": cnt,
                                 "fee": b.get("fee", 0),
                                 "exit_pnl": round(net / 100, 2),
                                 "kind": "FADE" if fade else "STOP",
                                 "trig": b.get("trig"), "ts": now()})
            self.autopsy = self.autopsy[-200:]
            del self.bets[tk]
            stopped += 1
        return stopped

    # ---- placement (maker resting orders, paper-identical triggers) ----
    def _refresh_caps(self, balance_c):
        """Compounding: risk caps track NAV instead of go-live dollars.

        NAV basis = balance + cost of FILLED positions (Kalshi's balance
        already includes cash held for resting orders). Percentages mean
        winning grows the bets and drawdowns shrink them - risk stays
        proportional without manual raises."""
        # account NAV = balance + BOTH books' position cost; this book's
        # bankroll is its allocated share (8/3: 50/50 weather/crypto)
        nav_c = int((balance_c
                     + sum(b["entry"] * b["count"] for b in self.bets.values())
                     + _crypto_cost_c()) * WX_ALLOC)
        if nav_c > 0:
            self.last_nav_c = nav_c    # nickel guardrails read this too
        if not DYN_CAPS:
            return
        if nav_c <= 0:
            return
        self.max_bet_c = max(BET_FLOOR_C, int(nav_c * BET_PCT))
        self.max_open_c = int(nav_c * OPEN_PCT)
        self.max_day_loss_c = max(HALT_FLOOR_C, int(nav_c * HALT_PCT))

    def place(self, mkts=None):
        if self.day_pnl_c <= -self.max_day_loss_c:
            self.halted = True
            return 0
        try:
            balance_c = self.balance_c()
        except Exception:
            return 0
        self._refresh_caps(balance_c)
        if mkts is None:
            try:
                mkts = we.find_temp_markets(max_days=1)
            except Exception:
                return 0
        gate_mode, _n = self._gate()
        bstats = self._bucket_stats()
        ev_keys, nk_keys = set(), set()
        for b in list(self.bets.values()) + list(self.pending.values()):
            k = (b["city"], b.get("date", ""), b["hl"])
            (nk_keys if b.get("trig") == "nickel" else ev_keys).add(k)
        new_mid, new_vol, cands = {}, {}, []
        today_iso = today()
        pending_tks = {o["ticker"] for o in self.pending.values()}
        for mk in mkts:
            tk = mk["ticker"]
            bid, ask = mk["yes_bid"], mk["yes_ask"]
            if tk in pending_tks:
                # 8/3: pursue pending joins BEFORE the two-sided filter -
                # near-settlement books go one-sided (no ask) and winners
                # were escaping unchased behind that skip
                self._maybe_requote(tk, mk)
                continue
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2.0
            prev = self.last_mid.get(tk)
            prev_vol = self.last_vol.get(tk)
            vol = float(mk.get("vol", 0) or 0)
            new_mid[tk] = mid
            new_vol[tk] = vol
            if tk in self.bets:
                # runner re-qualified -> maybe rest a pyramid add (paper rule)
                self._maybe_pyramid_order(tk, mk, mid, gate_mode, balance_c)
                continue
            ekey = (mk["city"], mk.get("date", ""),
                    "lo" if mk["is_low"] else "hi")
            if mid >= dp.DRIFT_MIN_C:
                side, entry, smid = "yes", bid, mid
                climb_c = (mid - prev) if prev is not None else None
            elif mid <= 100 - dp.DRIFT_MIN_C:
                side, entry, smid = "no", 100 - ask, 100 - mid
                climb_c = (prev - mid) if prev is not None else None
            else:
                continue
            climbing = climb_c is not None and climb_c >= dp.DRIFT_UP_C
            # NICKEL zone first (paper-identical): >=95c mid, entry 93..96c,
            # own event ledger, ranked by payoff (cheapest entry first)
            if dp.NICKEL_ON and smid >= dp.NICKEL_MIN_C:
                if entry < 93 or entry > dp.NICKEL_MAX_ENTRY:
                    continue
                if ekey in nk_keys:
                    continue
                cands.append(("nickel", 100.0 - entry, mk, side, entry, smid,
                              ekey, "maker", entry))
                continue
            if ekey in ev_keys:
                continue
            if smid >= dp.DRIFT_LEVEL_C:
                trig, score = "level", smid
            elif climbing:
                if dp.CLIMB_SAMEDAY and mk.get("date", "") != today_iso:
                    continue
                if dp.VOL_CONFIRM and not (prev_vol is not None and vol > prev_vol):
                    continue
                trig, score = "climb", climb_c
            else:
                continue
            if entry < ENTRY_FLOOR or entry > dp.DRIFT_MAX_ENTRY:
                continue
            # execution engine: on high-certainty thin-spread signals, cross
            # the spread as taker (a 1-2c toll beats missing an 88%+ winner)
            exec_kind, bid_entry = "maker", entry
            if TAKER_ON and trig in ("level", "climb"):
                # taker-FIRST on proven (half-Kelly) lanes; high-certainty
                # mids keep the original gate
                proven_lane = (self._kelly_frac(bstats, trig, entry)
                               > KELLY_BASE)
                if smid >= TAKER_MIN_SMID or proven_lane:
                    ask_side = (mk["yes_ask"] if side == "yes"
                                else 100 - mk["yes_bid"])
                    if (0 < ask_side - entry <= TAKER_MAX_SPREAD
                            and ask_side <= dp.DRIFT_MAX_ENTRY):
                        entry, exec_kind = ask_side, "taker"
            # capital routing: never re-enter a bucket the live ledger has
            # already proven negative (n >= BUCKET_MIN_N, net < 0)
            if trig != "nickel" and self._bucket_blocked(bstats, trig, entry):
                self.exec_stats["bucket_blocked"] = self.exec_stats.get("bucket_blocked", 0) + 1
                continue
            cands.append((trig, score, mk, side, entry, smid, ekey, exec_kind,
                          bid_entry))
        cands.sort(key=lambda c: ({"nickel": 0, "level": 1}.get(c[0], 2), -c[1]))
        placed = 0
        for (trig, score, mk, side, entry, smid, ekey, exec_kind,
             bid_entry) in cands:
            if ekey in (nk_keys if trig == "nickel" else ev_keys):
                continue
            tk = mk["ticker"]
            pside = smid / 100.0
            if trig == "nickel":
                if sum(1 for b in list(self.bets.values())
                       + list(self.pending.values())
                       if b.get("trig") == "nickel") >= dp.NICKEL_MAX_OPEN:
                    continue
                size = self._nickel_count()   # own lane: exempt from max_bet_c
                # NAV guardrails: single nickel <= 10% NAV, lane <= 30%
                nav_c = getattr(self, "last_nav_c", 0) or (
                    balance_c + sum(b["entry"] * b["count"]
                                    for b in self.bets.values()))
                pos_cap_c = int(nav_c * NICKEL_POS_PCT)
                lane_cost = sum(b["entry"] * b["count"]
                                for b in list(self.bets.values())
                                + list(self.pending.values())
                                if b.get("trig") == "nickel")
                while size > 1 and entry * size > pos_cap_c:
                    size -= 1
                if (entry * size > pos_cap_c
                        or lane_cost + entry * size > int(nav_c * NICKEL_LANE_PCT)):
                    continue
            else:
                if gate_mode == "probe":
                    size = max(1, PROBE_COST_CENTS // entry)
                else:
                    # Kelly edge is measured at the BID (the drift signal);
                    # the cost basis is whatever we actually pay (ask for
                    # takers). Sizing takers at the ask made f*=0 always -
                    # scale mode silently dropped every taker candidate
                    # (and its maker join with it) until 7/28.
                    b_odds = (100 - bid_entry) / bid_entry
                    f_star = (max(0.0, pside - (1 - pside) / b_odds)
                              * self._kelly_frac(bstats, trig, bid_entry))
                    bankroll = balance_c + self.open_cost_c()
                    size = int(min(f_star, dp.PER_BET_CAP) * bankroll // entry)
                    if size < 1 and exec_kind == "taker":
                        # edge too thin to pay the toll: rest a maker join
                        entry, exec_kind = bid_entry, "maker"
                        size = int(min(f_star, dp.PER_BET_CAP)
                                   * bankroll // entry)
                    if size < 1:
                        continue
                while size > 1 and entry * size > self.max_bet_c:
                    size -= 1
                if entry * size > self.max_bet_c:
                    continue
            if self.open_cost_c() + entry * size > self.max_open_c:
                continue
            if balance_c - entry * size < self.reserve_c:
                continue
            oid = f"dry-{self.placed + 1}"
            if self.client is not None:
                try:
                    resp = self.client.create_order(tk, action="buy", side=side,
                                                    count=size, price_cents=entry)
                    ro = resp.get("order") or {}
                    oid = (ro.get("order_id") or ro.get("id")
                           or resp.get("order_id") or resp.get("id") or oid)
                except Exception as e:
                    print(f"  order failed {tk}: {e}")
                    continue
            balance_c -= entry * size
            if self.client is None:
                self.dry_balance_c -= entry * size
            self.pending[oid] = {
                "ticker": tk, "side": side, "entry": entry, "count": size,
                "pside": pside, "city": mk["city"], "strike": mk["strike"],
                "kind": mk.get("kind", "ge"), "cap": mk.get("cap"),
                "hl": ("lo" if mk["is_low"] else "hi"),
                "date": mk.get("date", ""), "trig": trig, "peak": smid,
                "exec": exec_kind, "ots": now()}
            pk = "placed_" + exec_kind
            self.exec_stats[pk] = self.exec_stats.get(pk, 0) + 1
            (nk_keys if trig == "nickel" else ev_keys).add(ekey)
            self.placed += 1
            placed += 1
            self._log([now(), "REST", self.mode, mk["city"], mk["strike"],
                       ("lo" if mk["is_low"] else "hi"), side, round(pside, 3),
                       entry, size, "", "", oid])
            print(f"  {self.mode} DRIFT ORDER {tk}: {side.upper()} {size}x @ "
                  f"{entry}c {exec_kind} ({trig}, p={pside:.2f})")
        self.last_mid = new_mid             # momentum memory = last scan only
        self.last_vol = new_vol
        # DRY mode: resting orders "fill" instantly at maker price (upper
        # bound, same optimistic assumption the paper book makes)
        if self.client is None:
            for oid, o in list(self.pending.items()):
                is_taker = o.get("exec") == "taker"
                fee = fee_cents(o["entry"], o["count"], taker=is_taker)
                self.fees_c += fee
                ek = "filled_taker" if is_taker else "filled_maker"
                self.exec_stats[ek] = self.exec_stats.get(ek, 0) + 1
                tk0 = o["ticker"]
                if tk0 in self.bets and o.get("is_add"):
                    self._merge_fill(tk0, o["entry"], o["count"], fee)
                else:
                    self.bets[tk0] = {**{k: o[k] for k in
                                         ("side", "entry", "count", "city",
                                          "strike", "kind", "cap", "hl",
                                          "pside", "date", "trig", "peak")},
                                      "fee": fee, "oid": oid,
                                      "ots": o["ots"], "era": ERA}
                del self.pending[oid]
        return placed

    def _maybe_requote(self, tk, mk):
        """Execution engine: if the market ran away from an unfilled maker
        join by >= REQUOTE_C, chase it - cancel and re-join at the new bid
        (capped at REQUOTE_MAX chases and the trigger's max entry)."""
        oid = next((k for k, o in self.pending.items()
                    if o["ticker"] == tk and not o.get("is_add")), None)
        if oid is None:
            return False
        o = self.pending[oid]
        if int(o.get("requotes", 0)) >= REQUOTE_MAX or o.get("exec") == "taker":
            return False
        join = mk["yes_bid"] if o["side"] == "yes" else 100 - mk["yes_ask"]
        # 8/3 pursuit ladder: chase to CHASE_MAX_E (96c), not the entry band
        # ceiling - winners run past 92c and were escaping unchased
        max_e = (dp.NICKEL_MAX_ENTRY if o.get("trig") == "nickel"
                 else CHASE_MAX_E)
        if join - o["entry"] < REQUOTE_C or join > max_e or join <= 0:
            return False
        new_oid = f"rq-{self.placed + 1}"
        if self.client is not None:
            try:
                self.client.cancel_order(oid)
            except Exception:
                return False
            try:
                resp = self.client.create_order(tk, action="buy", side=o["side"],
                                                count=o["count"], price_cents=join)
                ro = resp.get("order") or {}
                new_oid = (ro.get("order_id") or ro.get("id")
                           or resp.get("order_id") or resp.get("id") or new_oid)
            except Exception:
                self._log_miss(o, o["count"] - int(o.get("filled_seen", 0)))
                del self.pending[oid]       # canceled but not replaced
                return False
        if self.client is None:
            self.dry_balance_c -= (join - o["entry"]) * o["count"]
        o = self.pending.pop(oid)
        o.update({"entry": join, "requotes": int(o.get("requotes", 0)) + 1,
                  "filled_seen": 0, "ots": now()})
        self.pending[new_oid] = o
        self.exec_stats["requotes"] = self.exec_stats.get("requotes", 0) + 1
        self._log([now(), "REQUOTE", self.mode, o["city"], o["strike"],
                   o["hl"], o["side"], round(o["pside"], 3), join,
                   o["count"], "", "", new_oid])
        return True

    def _merge_fill(self, tk, price, count, fee):
        """Fold a pyramid add-on fill into the existing position."""
        b = self.bets[tk]
        tot = b["count"] + count
        b["entry"] = round((b["entry"] * b["count"] + price * count) / tot, 1)
        b["count"] = tot
        b["fee"] = b.get("fee", 0) + fee
        b["adds"] = int(b.get("adds", 0)) + 1

    def _maybe_pyramid_order(self, tk, mk, mid, gate_mode, balance_c):
        """Rest a probe-size ADD on a runner (paper-identical: +PYRAMID_UP_C
        past avg entry, never nickels, capped adds, probe-active unless
        DRIFT_PYRAMID_PROBE=0)."""
        if gate_mode != "scale" and not dp.PYRAMID_PROBE:
            return False
        b = self.bets[tk]
        if b.get("trig") == "nickel":
            return False                    # nickels never pyramid
        if int(b.get("adds", 0)) >= dp.PYRAMID_MAX:
            return False
        if any(o["ticker"] == tk for o in self.pending.values()):
            return False                    # one resting add at a time
        smid = mid if b["side"] == "yes" else 100 - mid
        if smid < b["entry"] + dp.PYRAMID_UP_C:
            return False
        entry_add = mk["yes_bid"] if b["side"] == "yes" else 100 - mk["yes_ask"]
        if entry_add <= 0 or entry_add > dp.DRIFT_MAX_ENTRY:
            return False
        size = max(1, PROBE_COST_CENTS // entry_add)
        while size > 1 and entry_add * size > self.max_bet_c:
            size -= 1
        if entry_add * size > self.max_bet_c:
            return False
        if self.open_cost_c() + entry_add * size > self.max_open_c:
            return False
        if balance_c - entry_add * size < self.reserve_c:
            return False
        oid = f"dry-add-{self.placed + 1}"
        if self.client is not None:
            try:
                resp = self.client.create_order(tk, action="buy", side=b["side"],
                                                count=size, price_cents=entry_add)
                ro = resp.get("order") or {}
                oid = (ro.get("order_id") or ro.get("id")
                       or resp.get("order_id") or resp.get("id") or oid)
            except Exception:
                return False
        if self.client is None:
            self.dry_balance_c -= entry_add * size
        self.pending[oid] = {
            "ticker": tk, "side": b["side"], "entry": entry_add, "count": size,
            "pside": round(smid / 100.0, 3), "city": b["city"],
            "strike": b["strike"], "kind": b.get("kind", "ge"),
            "cap": b.get("cap"), "hl": b["hl"], "date": b.get("date", ""),
            "trig": b.get("trig"), "peak": smid, "is_add": True, "ots": now()}
        self.placed += 1
        self._log([now(), "PYRAMID", self.mode, b["city"], b["strike"], b["hl"],
                   b["side"], round(smid / 100.0, 3), entry_add, size, "", "", oid])
        return True

    def step(self):
        self._roll_day()
        self.check_orders()
        self.reconcile_positions()   # exchange = source of truth
        self.sync_kalshi_truth()     # W/L + realized from Kalshi's records
        self.settle()
        self.autopsy_check()         # grade past exits vs settlement
        self.miss_check()            # grade unfilled cancels vs settlement
        self.stop_check()
        self.place()
        try:
            bal = self.balance_c()
        except Exception:
            bal = None
        if bal is not None and self.day_nav0_c is None:
            # day anchor: balance + cost of open positions at day start
            self.day_nav0_c = bal + sum(
                b["entry"] * b["count"] for b in self.bets.values())
        self.save(balance_c=bal)


def build():
    """Decide mode from config/env/arm-file and construct the trader."""
    cfg = {}
    try:
        cfg = yaml.safe_load(open(CONFIG)) or {}
    except Exception:
        pass
    api = cfg.get("api", {}) if isinstance(cfg, dict) else {}
    key_id = str(api.get("key_id", "") or "")
    key_path = str(api.get("private_key_path", "kalshi-live.key") or "")
    demo = os.environ.get("KALSHI_ENV", "").lower() == "demo"
    if demo:
        key_id = os.environ.get("KALSHI_DEMO_KEY_ID", key_id)
        key_path = os.environ.get("KALSHI_DEMO_KEY_PATH", "kalshi-demo.key")
    have_key = key_id and "PASTE" not in key_id and os.path.exists(key_path)
    armed = (os.environ.get("KALSHI_DRIFT_LIVE", "") == "1"
             and os.path.exists(ARM_FILE))
    if demo and have_key:
        from kalshibot.client import KalshiClient
        return DriftLive(KalshiClient(key_id, key_path, DEMO_BASE), mode="DEMO")
    if have_key and armed:
        from kalshibot.client import KalshiClient
        return DriftLive(KalshiClient(key_id, key_path, LIVE_BASE), mode="LIVE")
    return DriftLive(None, mode="DRY")


def main():
    dl = build()
    if dl.mode == "LIVE" and "--yes-live" not in sys.argv and sys.stdin.isatty():
        if input("Type LIVE (all caps) to trade REAL money: ") != "LIVE":
            print("Cancelled.")
            return 0
    print(f"[{now()}] drift executor started in {dl.mode} mode - FULL paper "
          f"brain incl. nickel x{dl._nickel_count()} + pyramiding "
          f"(caps: {'DYNAMIC %-of-NAV ' if DYN_CAPS else ''}"
          f"${dl.max_bet_c/100:.2f}/bet regular, nickels own lane, "
          f"${dl.max_open_c/100:.2f} open, ${dl.max_day_loss_c/100:.2f} daily "
          f"halt; trail={'on' if TRAIL_ON else 'OFF - hold to settlement'}; "
          f"rest<= {REST_MAX_H}h)")
    # LANE 2 LIVE (8/3): the crypto executor rides in this armed process -
    # same key, same kill switch. Its own ledger, caps and universe.
    cl = None
    if _cl_mod is not None and _cl_mod.CRYPTO_ON:
        try:
            cl = _cl_mod.CryptoLive(client=dl.client, mode=dl.mode)
            print(f"[{now()}] crypto book UP in {cl.mode} mode - "
                  f"alloc {_cl_mod.ALLOC:.0%} of NAV, taker-first, "
                  f"band {_cl_mod.ENTRY_MIN}-{_cl_mod.ENTRY_MAX}c, "
                  f"stop {_cl_mod.STOP_C:.0f}c, no trail")
        except Exception as e:
            print(f"[{now()}] crypto book failed to start: {e}")
    if "--once" in sys.argv:
        dl.step()
        if cl is not None:
            cl.step()
        return 0
    while True:
        try:
            dl.step()
        except KeyboardInterrupt:
            print("stopped.")
            return 0
        except Exception as e:
            print(f"[{now()}] cycle error: {e}")
        if cl is not None:
            try:
                cl.step()
            except Exception as e:
                print(f"[{now()}] crypto cycle error: {e}")
        if _code_changed():
            print(f"[{now()}] new build on disk - restarting (systemd revives)")
            return 0
        time.sleep(CYCLE_S)


if __name__ == "__main__":
    raise SystemExit(main())
