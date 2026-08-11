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
import math
import os

try:
    from zoneinfo import ZoneInfo
except Exception:                              # py<3.9 / missing tzdata
    ZoneInfo = None

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
# 8/10 (Adam: "pause the crypto bot, it is clearly not working"): the
# book is PAUSED, in wind-down. No new entries, no arb pairs, no shadow
# notes - but check_orders/settle/mirror keep running so the open
# positions settle out honestly and the Kalshi-truth sync stays exact.
# The lifetime ledgers are untouched (the record is the record). Flip
# CRYPTO_PAUSED=0 to resume.
PAUSED = os.environ.get("CRYPTO_PAUSED", "1") == "1"
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
# 8/10 (Adam-approved): entry band narrowed to 80-88c. The gate era
# settled the hi-band question with clean data: 161-9 (94.7% wins) and
# STILL -$4.69, because breakeven at 95-96c after fees is ~95.7%. The
# 8/7 audit found the edge lives at cheap entries (80-85c: +11.4pts
# over breakeven, 4.9:1 payoff) and shrinks monotonically as price
# rises. So the book now buys only where the ledger says the edge is.
ENTRY_MAX = int(os.environ.get("CRYPTO_ENTRY_MAX", "88"))
# 8/4 HIGH-BAND PROBE (Adam: "take advantage of convergence to certainty,
# ship at half kelly"): entries 93-96c allowed as a SEPARATELY TRACKED
# bucket - weather's nickel lane playbook. Our shadow calibration has
# 90-95c markets settling YES 24/24, but crypto above 92c is UNPROVEN
# (the audition never traded there) and the payoff is +4-7c vs ~-60c
# after a stop, so this bucket keeps its own W/L ledger and earns (or
# loses) its lane on evidence, in public, on the tracker.
# 8/10: PROBE_MAX pulled down to ENTRY_MAX - the 93-96c hi band is
# STRUCTURALLY RETIRED (no entries above 88c at all), not just gated.
# Its lifetime ledger (207-12, -$9.00) stays on the tracker as the
# honest record. The paper-shadow book (below) keeps watching the band
# with zero dollars, so if the market regime ever changes the evidence
# will say so without costing anything.
PROBE_MAX = int(os.environ.get("CRYPTO_PROBE_MAX", "88"))
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
# 8/7 GATE ERA. Both lanes blocked themselves on 8/7 (hi -$4.31, core
# -$0.03) - correctly, on the evidence they had. But that evidence was
# produced by a book that doubled every position across the band and
# threshold markets of the same coin, and that stopped out at 1-2c. Both
# defects are fixed, so the old rows describe a system that no longer
# exists. The LIFETIME ledger is never reset (it is the honest record);
# the GATE reads a per-era ledger that starts fresh when the config
# changes materially. Bump this string to re-arm the lanes; never edit
# the lifetime numbers.
# g4 (8/10, Adam-approved): band narrowed to 80-88c, gate criterion
# moved from "pnl<0 at n>=8" to Wilson-bound-vs-breakeven (see
# _lane_blocked), and blocked lanes now run a paper-shadow book that
# can re-arm them on evidence. The core lane re-arms here: its g3 block
# (9-1, -$0.22) hinged on a single loss at n=10 - statistically noise.
GATE_ERA = os.environ.get("CRYPTO_GATE_ERA", "g4-core80-88-wilson")
# a blocked lane's shadow book needs this many settled paper outcomes,
# with the Wilson LOWER bound clearing the lane's own fee-adjusted
# breakeven, before the lane re-arms with real money
SHADOW_UNBLOCK_N = int(os.environ.get("CRYPTO_SHADOW_UNBLOCK_N", "30"))
# 8/11 (Adam): the shadow book is RETIRED for now. It already did its
# job - 25-2 in shadow and STILL -$0.76 proved the hi band edgeless
# with zero dollars at risk. The gate ledgers keep that evidence;
# CRYPTO_SHADOW_ON=1 revives collection if the book ever resumes.
SHADOW_ON = os.environ.get("CRYPTO_SHADOW_ON", "0") == "1"
# ---- 8/10 LADDER-COHERENCE ARB LANE (Adam-approved, real money) ----
# Two threshold markets on the same coin+hour must obey arithmetic:
# P(above lower strike) >= P(above higher strike). When separate order
# books cross that line by more than both taker fees, buy YES at the
# lower strike's ask AND NO at the higher strike's bid. Every outcome
# then pays at least 100c/contract (between the strikes pays 200), so
# the violation is banked AT ENTRY - no forecast anywhere, this lane
# trades arithmetic, not opinions. Settles within the hour: recycled
# capital. The only real risk is legging (one fill without the other);
# arb_reconcile() unwinds orphans immediately.
ARB_ON = os.environ.get("CRYPTO_ARB_ON", "1") == "1"
# minimum locked profit per contract AFTER both taker fees
ARB_MIN_NET_C = float(os.environ.get("CRYPTO_ARB_MIN_NET_C", "1"))
ARB_MAX_PAIRS = int(os.environ.get("CRYPTO_ARB_MAX_PAIRS", "3"))
# 8/10 (Adam): 5-contract minimum across ALL strategies
ARB_CONTRACTS = int(os.environ.get("CRYPTO_ARB_CONTRACTS", "5"))
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
# 8/7 (Adam-approved): the crypto stop is RETIRED. The 8/7 autopsy shows
# it did nothing where it mattered and real harm where it didn't:
#   ETH  entry 96c -> stopped at  1c   (no protection: 96 -> 1 between
#   BTC  entry 93c -> stopped at  2c    two 3-minute polls)
#   XRP  entry 82c -> stopped at 32c   (5pm market, HOURS left to run)
#   SOL  entry 84c -> stopped at 31c   (same)
# An hourly binary resolves inside the poll interval, so a price-level
# stop cannot exit a collapsing position - it just realises the loss and
# pays a taker fee to do it. On the longer-dated legs it did the weather
# book's documented wobble-tax: converted a recoverable dip into a
# locked-in loss. Hold to settlement and accept the binary.
# CRYPTO_STOP_ON=1 restores the old behaviour.
STOP_ON = os.environ.get("CRYPTO_STOP_ON", "0") == "1"
# 8/7 (Adam-approved): the daily ORDER-COUNT cap is retired. It was not
# a risk control - open exposure is bounded by OPEN_PCT of bank and the
# day is stopped by the daily-loss halt - but it WAS a selection bias:
# the counter refilled at 00:00 UTC, the 8pm-midnight ET hourlies ate all
# 40 slots by 04:00 UTC (39/40 on both 8/6 and 8/7), and every daytime
# market after that was invisible no matter how good. 0 = unlimited.
MAX_PER_DAY = int(os.environ.get("CRYPTO_MAX_PER_DAY", "0"))
# What replaces it is a per-CYCLE ceiling, which is runaway protection
# rather than an opportunity budget: a normal cycle sees ~9 distinct
# events, so this never binds in ordinary operation - it only stops a
# bad feed or a bug from firing hundreds of orders in one pass.
MAX_PER_CYCLE = int(os.environ.get("CRYPTO_MAX_PER_CYCLE", "15"))
# 8/7 FEE-ROUNDING FLOOR (Adam). Kalshi rounds the fee UP to the next whole
# cent PER ORDER, so a 1-contract fill pays for a cent it never used: at
# 96c the raw fee is 0.27c but you are charged 1c - 25% of a 4c win, ~4x
# the true 6.7% drag. Three contracts at 96c pay the SAME 1c. Live proof:
# 35% of all trades were 1 contract and fees ate 8.4% of gross winnings.
# 8/7 (Adam, revised): the floor OVERRIDES the per-bet cap. Every signal
# the book would have traded still gets traded - at >= MIN_CONTRACTS -
# rather than being skipped for not fitting the cap. Sizes above the
# floor still trim to the cap; the floor itself never trims and never
# skips. Balance/reserve and the OPEN cap still bind (those stop the book
# overdrawing or overexposing in aggregate). Kelly resumes control
# automatically once the bankroll asks for >= MIN_CONTRACTS unaided.
# 8/10 (Adam): 5-contract minimum across ALL strategies (book is
# paused; this applies the moment it ever resumes)
MIN_CONTRACTS = int(os.environ.get("CRYPTO_MIN_CONTRACTS", "5"))
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

# 8/7 THE DOUBLING BUG. Kalshi lists every coin-hour as TWO events - the
# range-band market (KXETH-26AUG0713) and the threshold market
# (KXETHD-26AUG0713). The old one-bet-per-EVENT rule saw two different
# tickers and let both through, but they resolve off the SAME print at
# the SAME instant, so a single directional view got taken twice at full
# size. On 8/7 the book held 13 positions across only 7 underlyings:
#   ETH 1pm  NO @96c on "$1,905-1,909.99"  -> needs ETH >= $1,910
#   ETH 1pm  YES @96c on "$1,910 or above" -> needs ETH >= $1,910
# ETH settled ~$1,906 and both died together. Same on XRP. Those doubled
# pairs were -$10.21 of a -$15.56 hour.
# Dedup is now by (underlying, settlement hour), never by event ticker.
_COIN = {"KXBTC": "BTC", "KXBTCD": "BTC", "KXETH": "ETH", "KXETHD": "ETH",
         "KXSOL": "SOL", "KXSOLD": "SOL", "KXSOLE": "SOL",
         "KXXRP": "XRP", "KXXRPD": "XRP", "KXDOGE": "DOGE",
         "KXDOGED": "DOGE", "KXHYPE": "HYPE", "KXHYPED": "HYPE"}


def underlying_key(tk):
    """(coin, settlement-hour) - the real unit of correlated risk.

    KXETH-26AUG0713-B1907  and  KXETHD-26AUG0713-T1909.99
    both -> ("ETH", "26AUG0713"). Unknown series fall back to the raw
    series code so a new listing is never silently merged with another.
    """
    parts = (tk or "").split("-")
    series = parts[0] if parts else ""
    coin = _COIN.get(series)
    if coin is None:                      # unseen series: strip one D/E
        base = series[2:] if series.startswith("KX") else series
        coin = base[:-1] if base[-1:] in ("D", "E") and len(base) > 3 else base
    return coin, (parts[1] if len(parts) > 1 else "")


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


def _t_strike(tk):
    """KXBTCD-26AUG1017-T66499.99 -> 66499.99; None for non-threshold
    tickers (band markets have B-suffixes and are not cumulative, so
    the monotonicity rule doesn't apply to them)."""
    last = (tk or "").rsplit("-", 1)[-1]
    if not last.startswith("T"):
        return None
    try:
        return float(last[1:])
    except ValueError:
        return None


def _wilson(w, n, z=1.0):
    """Wilson score interval for a binomial win rate (8/10 gate math)."""
    if n <= 0:
        return 0.0, 1.0
    ph = w / n
    d = 1.0 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    r = (z / d) * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return max(0.0, c - r), min(1.0, c + r)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def today():
    """8/7: the trading day rolls at midnight ET, not 00:00 UTC.

    The droplet runs UTC, so the old day boundary landed at 8pm ET -
    mid-session. Combined with the daily order cap that meant the budget
    refilled every evening, was spent on the 8pm-midnight ET hourlies,
    and every US-daytime market (Adam's 10am/11am/12pm/1pm) arrived with
    the day already exhausted. The day-loss halt reset at 8pm too.
    """
    return _et_now().date().isoformat()


def _et_now():
    n = datetime.datetime.now(datetime.timezone.utc)
    if ZoneInfo is not None:
        try:
            return n.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            pass
    return n - datetime.timedelta(hours=5)   # EST fallback, never crashes


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
        self.sync_bad = []      # the actual mismatched tickers, if any
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
        # gate ledgers: same shape as hi/core but scoped to GATE_ERA
        self.hi_g = {"w": 0, "l": 0, "pnl": 0.0}
        self.core_g = {"w": 0, "l": 0, "pnl": 0.0}
        self.gate_era = GATE_ERA
        # day-loss budget is measured from here, so a mid-day config
        # change gets a fresh budget without rewriting the day ledger
        self.halt_base_c = 0.0
        # 8/10 ladder-coherence arb lane: its own ledger (never mixed
        # into core/hi - arithmetic profits must not launder a
        # forecasting lane's evidence) + live pair tracking for the
        # legging unwind
        self.arb = {"w": 0, "l": 0, "pnl": 0.0, "pairs": 0,
                    "scratches": 0, "best_gap_c": None}
        self.arb_pairs = {}
        # 8/10: paper-shadow book for BLOCKED lanes - same signals, zero
        # dollars. A blocked lane keeps gathering settlement evidence
        # here, and settle() re-arms it if the shadow record clears its
        # breakeven (Wilson lower bound, n >= SHADOW_UNBLOCK_N). Blocks
        # stop being one-way doors without risking a cent to find out.
        self.shadow = {}
        # settlement receivable (8/10): NAV no longer dips while the
        # exchange's cash credit lags our settlement detection
        self.recv = []
        self.recv_bal_c = None
        # 8/7: the crypto book had NO miss ledger at all - an order that
        # died unfilled just incremented `canceled` and was forgotten, so
        # the fastest-growing book was the one flying blind on execution.
        # Every unfilled death is now graded against settlement, exactly
        # like the weather book's miss autopsy.
        self.miss = []
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
                          "hi", "core", "stops", "miss", "shadow",
                          "recv", "recv_bal_c", "arb", "arb_pairs",
                          "hi_g", "core_g", "gate_era", "halt_base_c"):
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
        # 8/7: config changed materially (doubling bug + stop retired), so
        # the gate starts a fresh era and the lanes re-arm. The lifetime
        # hi/core ledgers above are untouched - they stay the honest
        # record. Clearing `halted` here reopens the book in the same
        # move; the daily-loss halt re-arms immediately from day_pnl_c on
        # the next cycle, so this is a reopen, not a disabled control.
        if self.gate_era != GATE_ERA:
            self.hi_g = {"w": 0, "l": 0, "pnl": 0.0, "ben": 0.0}
            self.core_g = {"w": 0, "l": 0, "pnl": 0.0, "ben": 0.0}
            self.shadow = {}
            self.gate_era = GATE_ERA
            # keep the true day ledger; just rebase the risk budget
            self.halt_base_c = self.day_pnl_c
            self.halted = False
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
            recv_c = self._recv_c(balance_c)   # consume/expire pre-save
            d = {"updated": now(), "era": ERA, "mode": self.mode,
                 "bets": self.bets, "pending": self.pending,
                 "history": self.history[-120:],
                 "pnl_days": self.pnl_days,
                 "hi": self.hi, "core": self.core, "stops": self.stops,
                 "shadow": self.shadow,
                 "arb": self.arb, "arb_pairs": self.arb_pairs,
                 "recv_c": recv_c,
                 "recv": self.recv, "recv_bal_c": self.recv_bal_c,
                 "hi_g": self.hi_g, "core_g": self.core_g,
                 "gate_era": self.gate_era,
                 "halt_base_c": self.halt_base_c,
                 "miss": self.miss[-200:],
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
                            "blocked": self._hi_blocked(),
                            "gw": self.hi_g.get("w", 0),
                            "gl": self.hi_g.get("l", 0),
                            "gpnl": round(self.hi_g.get("pnl", 0.0), 2),
                            "sw": self.hi_g.get("sw", 0),
                            "sl": self.hi_g.get("sl", 0),
                            "spnl": round(self.hi_g.get("spnl", 0.0), 2)},
                     "core": {"w": self.core.get("w", 0),
                              "l": self.core.get("l", 0),
                              "pnl": round(self.core.get("pnl", 0.0), 2),
                              "open": sum(1 for b in self.bets.values()
                                          if b.get("band", "core") == "core"),
                              "min": ENTRY_MIN, "max": ENTRY_MAX,
                              "blocked": self._core_blocked(),
                              "gw": self.core_g.get("w", 0),
                              "gl": self.core_g.get("l", 0),
                              "gpnl": round(self.core_g.get("pnl", 0.0), 2),
                              "sw": self.core_g.get("sw", 0),
                              "sl": self.core_g.get("sl", 0),
                              "spnl": round(self.core_g.get("spnl", 0.0), 2)},
                     "paused": PAUSED,
                     "arb": {"on": ARB_ON,
                             "w": self.arb.get("w", 0),
                             "l": self.arb.get("l", 0),
                             "pnl": round(self.arb.get("pnl", 0.0), 2),
                             "pairs": self.arb.get("pairs", 0),
                             "open_pairs": len(self.arb_pairs),
                             "scratches": self.arb.get("scratches", 0),
                             "best_gap_c": self.arb.get("best_gap_c"),
                             "min_net_c": ARB_MIN_NET_C,
                             "max_pairs": ARB_MAX_PAIRS,
                             "size": ARB_CONTRACTS},
                     "gate_era": self.gate_era,
                     "stop_on": STOP_ON,
                     "stops": self.stops,
                     "today_n": self._placed_today(),
                     "max_day": MAX_PER_DAY,
                     "max_cycle": MAX_PER_CYCLE,
                     "day_tz": "ET",
                     "min_ct": MIN_CONTRACTS,
                     **self._miss_summary(),
                     "sync_diffs": self._sync_diffs(),
                     "sync_bad": self.sync_bad},
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
        w, l = self.hi_g.get("w", 0), self.hi_g.get("l", 0)
        if self.hi_g.get("pnl", 0.0) <= 0:
            return self._bet_pct()
        if w + l >= HI_STEP2_N:
            return max(self._bet_pct(), HI_PCT2)
        if w + l >= HI_STEP1_N:
            return max(self._bet_pct(), HI_PCT1)
        return self._bet_pct()

    def _hi_cap_c(self):
        return max(150, int(self.bank_c * self._hi_pct()))

    def _lane_blocked(self, lane, min_n):
        """8/10 gate redesign (Adam-approved). The old rule - pnl<0 at
        n>=8 - killed the core lane on a single loss at n=10 (noise)
        while win-rate gates elsewhere passed lanes that lost money.
        New rule: block when the evidence says LOSER with confidence,
        i.e. the Wilson UPPER bound (z=1) of the lane's win rate sits
        below its own fee-adjusted breakeven - even the optimistic read
        loses. Dollar backstop: a large sample (5x min_n) that is still
        net-negative blocks regardless. Legacy ledgers with no breakeven
        data keep the old rule. The daily-loss halt remains the fast
        brake for acute bleeding; this is the slow statistical one."""
        w, l = lane.get("w", 0), lane.get("l", 0)
        n = w + l
        if n < min_n:
            return False
        ben = lane.get("ben")
        if not ben:
            return lane.get("pnl", 0.0) < 0          # legacy rows
        _, ub = _wilson(w, n)
        if ub < ben / n:
            return True
        return n >= 5 * min_n and lane.get("pnl", 0.0) < 0

    def _hi_blocked(self):
        return self._lane_blocked(self.hi_g, HI_BLOCK_N)

    def _core_blocked(self):
        return self._lane_blocked(self.core_g, CORE_BLOCK_N)

    def _lane_add(self, band, net_c, won, be=None):
        """Book a realized outcome to the lifetime ledger AND the gate
        ledger. Lifetime is the honest record and is never reset; the
        gate ledger restarts whenever GATE_ERA changes. `be` is the
        bet's fee-adjusted breakeven win probability - the gate compares
        the lane's Wilson interval against the average of these."""
        if band == "arb":
            # arithmetic lane: its own ledger, never the forecast gates
            k = "w" if won else "l"
            self.arb[k] = self.arb.get(k, 0) + 1
            self.arb["pnl"] = round(self.arb.get("pnl", 0.0)
                                    + net_c / 100.0, 2)
            return
        hi = (band == "hi")
        for lane in ((self.hi if hi else self.core),
                     (self.hi_g if hi else self.core_g)):
            lane["w" if won else "l"] += 1
            lane["pnl"] = round(lane.get("pnl", 0.0) + net_c / 100.0, 2)
            if be is not None:
                lane["ben"] = round(lane.get("ben", 0.0) + be, 4)

    # ---- settlement receivable (8/10, same fix as the weather book):
    # bridges the minutes between our settlement detection and the
    # exchange's cash credit. Consumed as the balance rises; hard-expired
    # at 15 minutes so it can never overstate NAV for long. ----
    def _recv_add(self, amount_c):
        self.recv.append([now(), int(round(amount_c))])

    def _recv_c(self, balance_c=None):
        if balance_c is not None:
            prev = self.recv_bal_c
            self.recv_bal_c = balance_c
            if prev is not None and balance_c > prev and self.recv:
                gain = balance_c - prev
                keep = []
                for ts, amt in self.recv:
                    if gain >= amt:
                        gain -= amt
                        continue
                    keep.append([ts, amt - gain])
                    gain = 0
                self.recv = keep
        cut = (datetime.datetime.now()
               - datetime.timedelta(minutes=15)).isoformat(timespec="seconds")
        self.recv = [r for r in self.recv if str(r[0]) > cut]
        return int(sum(r[1] for r in self.recv))

    def _shadow_note(self, tk, side, e_ask, band, smid):
        """Paper-shadow a blocked lane's refused entry (8/10): records
        the trade it WOULD have made, sized at the 3-lot floor, settled
        virtually in settle(). Zero dollars at risk."""
        if not SHADOW_ON:
            return              # 8/11: retired (see SHADOW_ON)
        if tk in self.shadow or len(self.shadow) >= 300:
            return
        uk = underlying_key(tk)
        if any(underlying_key(t) == uk for t in self.shadow):
            return
        fee = fee_cents(e_ask, MIN_CONTRACTS, taker=True)
        self.shadow[tk] = {"side": side, "entry": int(e_ask),
                           "count": MIN_CONTRACTS, "fee": fee,
                           "band": band, "pside": round(smid / 100.0, 3),
                           "ots": now()}

    # ---- 8/10 ladder-coherence arb lane -------------------------------
    def arb_scan(self, mkts, balance_c):
        """Scan same-event threshold ladders for monotonicity
        violations big enough to clear both taker fees, and buy the
        contradiction: YES at the lower strike's ask + NO at the higher
        strike's bid. Minimum payout is 100c/contract in EVERY outcome
        (both strikes between pays 200), so the profit is locked at
        entry. Returns the updated working balance."""
        if not ARB_ON or not mkts:
            return balance_c
        open_pairs = sum(1 for p in self.arb_pairs.values()
                         if p.get("status") in ("pending", "on"))
        u_keys = {underlying_key(t) for t in
                  list(self.bets) + [o["ticker"]
                                     for o in self.pending.values()]}
        best_gap, n = None, max(1, ARB_CONTRACTS)
        byev = {}
        for mk in mkts:
            s = _t_strike(mk["ticker"])
            if s is None or _is_15m(mk["ticker"], mk.get("name", "")):
                continue
            if (mk["yes_bid"] or 0) <= 0 or (mk["yes_ask"] or 0) <= 0:
                continue
            byev.setdefault(mk["event"], []).append((s, mk))
        for ev, rows in sorted(byev.items()):
            if open_pairs >= ARB_MAX_PAIRS:
                break
            if len(rows) < 2:
                continue
            if underlying_key(rows[0][1]["ticker"]) in u_keys:
                continue        # this coin+hour already has an opinion
            rows.sort(key=lambda r: r[0])
            best = None
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    lo, hi = rows[i][1], rows[j][1]
                    gap = hi["yes_bid"] - lo["yes_ask"]
                    if best_gap is None or gap > best_gap:
                        best_gap = gap
                    if gap <= 0:
                        continue
                    yes_px, no_px = lo["yes_ask"], 100 - hi["yes_bid"]
                    if yes_px <= 0 or no_px <= 0:
                        continue
                    fees = (fee_cents(yes_px, n, taker=True)
                            + fee_cents(no_px, n, taker=True))
                    net_c = gap * n - fees
                    if net_c < ARB_MIN_NET_C * n:
                        continue
                    if best is None or net_c > best[0]:
                        best = (net_c, lo, hi, yes_px, no_px)
            if best is None:
                continue
            net_c, lo, hi, yes_px, no_px = best
            cost = (yes_px + no_px) * n
            if self.open_cost_c() + cost > self._open_cap_c():
                continue
            if balance_c - cost < RESERVE_C:
                continue
            pid = f"arb-{self.placed + 1}"
            legs = []
            for tk, side, px, mk in ((lo["ticker"], "yes", yes_px, lo),
                                     (hi["ticker"], "no", no_px, hi)):
                oid = f"{pid}-{side}"
                if self.client is not None:
                    try:
                        resp = self.client.create_order(
                            tk, action="buy", side=side, count=n,
                            price_cents=px)
                        ro = resp.get("order") or {}
                        oid = (ro.get("order_id") or ro.get("id")
                               or resp.get("order_id")
                               or resp.get("id") or oid)
                    except Exception:
                        break   # legs placed so far: reconcile unwinds
                o = {"ticker": tk, "side": side, "entry": px, "count": n,
                     "pside": px / 100.0, "name": mk.get("name", tk),
                     "event": mk["event"], "peak": px, "exec": "taker",
                     "band": "arb", "pid": pid,
                     "filled_seen": 0, "ots": now(), "era": ERA}
                self.pending[oid] = o
                legs.append(oid)
                self.placed += 1
                balance_c -= px * n
                self._log([now(), "ARB", self.mode, tk,
                           mk.get("name", "")[:60], side, "", px, n,
                           "", round(net_c / 100.0, 2), pid])
                print(f"  {self.mode} ARB {pid}: {side.upper()} {n}x "
                      f"{tk} @ {px}c (locked {net_c / 100.0:.2f})")
                if self.client is None:
                    self.dry_balance_c -= px * n
                    self._promote(oid, o, n)
                    del self.pending[oid]
            if not legs:
                continue        # first leg rejected: nothing at risk
            self.arb_pairs[pid] = {"t_yes": lo["ticker"],
                                   "t_no": hi["ticker"], "n": n,
                                   "net_c": round(net_c, 1),
                                   "ots": now(), "status": "pending"}
            self.arb["pairs"] = self.arb.get("pairs", 0) + 1
            u_keys.add(underlying_key(lo["ticker"]))
            open_pairs += 1
        self.arb["best_gap_c"] = best_gap
        return balance_c

    def arb_reconcile(self):
        """A pair with one leg filled and the other dead is not an arb -
        it's an accidental directional bet. Unwind the live leg at the
        bid immediately, book the scratch to the arb ledger, move on.
        Pairs whose legs both filled are marked 'on' (they hold to
        settlement, where every outcome pays); fully departed pairs are
        cleaned up."""
        for pid, p in list(self.arb_pairs.items()):
            tks = (p["t_yes"], p["t_no"])
            live = [tk for tk in tks if tk in self.bets
                    and self.bets[tk].get("pid") == pid]
            if any(o.get("pid") == pid for o in self.pending.values()):
                continue                    # legs still working
            if len(live) == 2:
                p["status"] = "on"
                continue
            if not live:
                del self.arb_pairs[pid]     # settled (or never filled)
                continue
            if p.get("status") == "on":
                continue    # sibling settled first; this one settles too
            tk = live[0]                    # orphan: unwind now
            b = self.bets[tk]
            try:
                q = dw.DriftWide._quotes(self, [tk]).get(tk)
            except Exception:
                q = None
            if not q:
                p["status"] = "on"          # can't quote: hold to settle
                continue
            yb, ya = q
            bid = yb if b["side"] == "yes" else ((100 - ya) if ya else 0)
            if not bid or bid <= 0:
                p["status"] = "on"
                continue
            if self.client is not None:
                try:
                    self.client.create_order(tk, action="sell",
                                             side=b["side"],
                                             count=b["count"],
                                             price_cents=bid)
                except Exception:
                    continue                # retry next cycle
            exit_fee = fee_cents(bid, b["count"], taker=True)
            net = ((bid - b["entry"]) * b["count"]
                   - b.get("fee", 0) - exit_fee)
            self.realized_c += net
            self._day_add(net)
            self.day_pnl_c += net
            self.fees_c += exit_fee
            self.wins += int(net > 0)
            self.losses += int(net <= 0)
            self.arb["scratches"] = self.arb.get("scratches", 0) + 1
            self._lane_add("arb", net, net > 0)
            if self.client is None:
                self.dry_balance_c += bid * b["count"] - exit_fee
            self.history.append({"name": b.get("name", tk), "tk": tk,
                                 "side": b["side"], "pside": b["pside"],
                                 "entry": b["entry"], "count": b["count"],
                                 "outcome": None, "stopped": True,
                                 "band": "arb",
                                 "pnl": round(net / 100.0, 2),
                                 "ts": now(), "ots": b.get("ots", ""),
                                 "era": ERA})
            self._log([now(), "UNWIND", self.mode, tk,
                       b.get("name", "")[:60], b["side"], "", bid,
                       b["count"], "", round(net / 100.0, 2), pid])
            del self.bets[tk]
            del self.arb_pairs[pid]

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
            self.halt_base_c = 0.0
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
                    # 8/11: fractional fills counted exactly (see weather)
                    fc = round(float(f.get("count_fp") or f.get("count") or 0), 2)
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
                self._log_miss(o, o["count"] - seen - filled, "order_vanished")
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
                self._log_miss(o, o["count"] - int(o.get("filled_seen", 0)),
                               "rest_expired")
                del self.pending[oid]

    # ---- miss autopsy (8/7): grade the road not taken --------------
    def _log_miss(self, o, unfilled, why):
        if unfilled <= 0:
            return
        self.miss.append({"tk": o["ticker"], "side": o["side"],
                          "entry": o["entry"], "count": int(unfilled),
                          "band": o.get("band", "core"),
                          "pside": round(o.get("pside", 0), 3),
                          "why": why, "cts": now(), "res": None})
        self.miss = self.miss[-200:]

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
            fee = fee_cents(row["entry"], row["count"], taker=True)
            row["res"] = res
            row["would_pnl"] = round(
                (((100 if won else 0) - row["entry"]) * row["count"] - fee)
                / 100.0, 2)

    def _miss_summary(self):
        graded = [r for r in self.miss if r.get("res") is not None]
        why = {}
        for r in self.miss:
            a = why.setdefault(r.get("why") or "unknown", {"n": 0})
            a["n"] += 1
        return {"miss_n": len(self.miss), "miss_settled": len(graded),
                "miss_would_won": sum(1 for r in graded
                                      if r.get("would_pnl", 0) > 0),
                "miss_cost": round(sum(r.get("would_pnl", 0)
                                       for r in graded), 2),
                "miss_why": why}

    def _settled_tks(self):
        """Tickers this era has already closed out. A Kalshi market
        settles exactly once, so any of these is dead - nothing may
        re-open a position in it."""
        return {h.get("tk") for h in self.history if h.get("tk")}

    def _sweep_phantoms(self):
        """Drop bets whose market has already been settled and booked.

        8/7: a position that settled at 19:02 was back in the open book
        at 19:26. check_orders() promotes a vanished order to a fill -
        and when the fills API is unavailable it ASSUMES the whole order
        filled - so a leftover pending order on an already-settled ticker
        resurrected the bet AFTER settle() had deleted it and booked the
        P&L. Left alone the ghost would settle a SECOND time and
        double-count, besides inflating open cost and NAV.

        Removed silently and with no P&L: the real outcome was already
        booked when the market settled the first time.
        """
        dead = [tk for tk in list(self.bets) if tk in self._settled_tks()]
        for tk in dead:
            b = self.bets.pop(tk)
            self._log([now(), "PHANTOM", self.mode, tk,
                       b.get("name", "")[:60], b.get("side", ""), "",
                       b.get("entry", ""), b.get("count", ""), "", "", ""])
        return len(dead)

    def _promote(self, oid, o, filled):
        tk0 = o.get("ticker")
        if tk0 not in self.bets and tk0 in self._settled_tks():
            return              # already settled: never resurrect it
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
                             "pid": o.get("pid", ""),
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
                # 8/11: fractional positions mirrored exactly
                pos = (float(v) if v is not None
                       else float(p.get("position") or 0))
                if abs(pos) < 0.01:
                    continue
                tk = p.get("ticker") or ""
                if tk.split("-")[0] in wx:
                    continue
                kp.append({"ticker": tk,
                           "side": "yes" if pos > 0 else "no",
                           "count": round(abs(pos), 2)})
            self.k_positions = kp
        except Exception:
            pass

    def _sync_diffs(self):
        """How far our book diverges from Kalshi's positions.

        8/7 FIX: this used to be a value STORED inside mirror(), assigned
        AFTER self.k_positions and inside a bare `except Exception: pass`.
        Two ways that lied: (a) anything throwing in between left the
        counter frozen at a stale number while positions refreshed, and
        (b) mirror() runs before settle()/place(), so the stored value was
        always a phase behind the bets the payload then serialised. It
        read 3 for hours while the books matched 10/10 exactly.

        Now it is COMPUTED AT SAVE TIME from the same two structures the
        payload publishes, so the number can never disagree with the data
        shown next to it. Reads defensively - a malformed bet can no
        longer take the whole calculation down. 0 = perfect mirror.
        """
        if self.client is None:
            return None
        def norm(side, count):
            try:
                return (side, round(float(count or 0), 2))
            except (TypeError, ValueError):
                return (side, 0.0)
        kp = {r.get("ticker"): norm(r.get("side"), r.get("count"))
              for r in (self.k_positions or [])}
        mine = {tk: norm(b.get("side"), b.get("count"))
                for tk, b in self.bets.items()}
        # Kalshi's positions API keeps reporting a market for a few
        # minutes after it settles, so anything this era has already
        # booked is expected to differ and is not a divergence.
        done = self._settled_tks()
        bad = sorted(tk for tk in set(kp) | set(mine)
                     if kp.get(tk) != mine.get(tk) and tk not in done)
        # name the offenders, so a real divergence is diagnosable instead
        # of being a bare count nobody can act on
        self.sync_bad = [{"tk": tk, "kalshi": kp.get(tk), "book": mine.get(tk)}
                         for tk in bad[:10]]
        return len(bad)

    def stop_check(self):
        if not STOP_ON:
            return 0          # 8/7: retired - see STOP_ON
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
            self._lane_add(b.get("band"), net, net > 0,
                           be=(b["entry"] + b.get("fee", 0)
                               / max(1, b["count"])) / 100.0)
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
            elif won:
                self._recv_add(payout * b["count"])
            self._lane_add(b.get("band"), net, won,
                           be=(b["entry"] + b.get("fee", 0)
                               / max(1, b["count"])) / 100.0)
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
        self._settle_shadow()

    def _settle_shadow(self):
        """Resolve the paper-shadow book and re-arm any blocked lane
        whose shadow record has EARNED it (8/10). Shadow outcomes land
        in the gate ledger's s* counters, never in the money ledgers."""
        if not SHADOW_ON and not self.shadow:
            return
        for tk, sb in list(self.shadow.items()):
            res = fetch_result(tk)
            if res is None:
                ots = (sb.get("ots") or "")[:10]
                cut = (datetime.date.today()
                       - datetime.timedelta(days=2)).isoformat()
                if ots and ots < cut:
                    del self.shadow[tk]      # unresolvable: expire
                continue
            won = (res == sb["side"])
            net = (((100 if won else 0) - sb["entry"]) * sb["count"]
                   - sb["fee"])
            lane = self.hi_g if sb.get("band") == "hi" else self.core_g
            k = "sw" if won else "sl"
            lane[k] = lane.get(k, 0) + 1
            lane["spnl"] = round(lane.get("spnl", 0.0) + net / 100.0, 2)
            lane["sben"] = round(
                lane.get("sben", 0.0)
                + (sb["entry"] + sb["fee"] / sb["count"]) / 100.0, 4)
            del self.shadow[tk]
        # re-arm: a blocked lane whose SHADOW record clears its own
        # breakeven with room (Wilson lower bound, n >= SHADOW_UNBLOCK_N)
        # earns a fresh gate ledger - evidence, not a manual override.
        for name, lane, min_n in (("hi", self.hi_g, HI_BLOCK_N),
                                  ("core", self.core_g, CORE_BLOCK_N)):
            sw, sl = lane.get("sw", 0), lane.get("sl", 0)
            sn = sw + sl
            if sn < SHADOW_UNBLOCK_N or not self._lane_blocked(lane, min_n):
                continue
            lb, _ = _wilson(sw, sn)
            if lb >= lane.get("sben", 0.0) / sn:
                self._log([now(), "REARM", self.mode, name, "shadow",
                           "", "", "", "", sn, "",
                           round(lane.get("spnl", 0.0), 2)])
                lane.clear()
                lane.update({"w": 0, "l": 0, "pnl": 0.0, "ben": 0.0})

    def _placed_today(self):
        t = today()
        n = sum(1 for b in list(self.bets.values())
                + list(self.pending.values())
                if (b.get("ots") or "")[:10] == t)
        n += sum(1 for h in self.history if (h.get("ots") or "")[:10] == t)
        return n

    def place(self, mkts=None):
        # 8/7 DEADLOCK FIX: the halt used to be checked BEFORE the bank
        # was refreshed. On a fresh process bank_c is 0, so _halt_c()
        # returned its $2 floor instead of 10% of bank, the book halted
        # on a trivial daily loss, and place() returned before ever
        # calling refresh_bank - so bank stayed 0 and the halt could
        # never lift for the rest of the day. Bank first, then halt.
        try:
            balance_c = self.balance_c()
        except Exception:
            return 0
        self.refresh_bank(balance_c)
        if PAUSED:
            return 0        # wind-down: no new positions of any kind
        if self.day_pnl_c - self.halt_base_c <= -self._halt_c():
            self.halted = True
            return 0
        if mkts is None:
            mkts = fetch_crypto_mkts()      # 8/6: direct, no sweep lottery
            if not mkts:
                try:
                    with dcfg._cfg():
                        mkts = dw.find_wide_markets()
                except Exception:
                    return 0
        budget = (MAX_PER_DAY - self._placed_today() if MAX_PER_DAY > 0
                  else MAX_PER_CYCLE)
        # 8/10: arithmetic first - locked-at-entry arb pairs get capital
        # priority over forecast-band candidates (surer money, and the
        # dedupe below then keeps directional bets off those coin-hours)
        balance_c = self.arb_scan(mkts, balance_c)
        # one opinion per UNDERLYING per settlement hour - not per event
        # ticker, which let the band market and the threshold market on
        # the same coin/hour both through (see underlying_key).
        u_keys = {underlying_key(t) for t in
                  list(self.bets) + [o["ticker"] for o in
                                     self.pending.values()]}
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
            if (tk in self.bets or tk in pend_tks
                    or underlying_key(tk) in u_keys):
                continue
            mid = (bid + ask) / 2.0
            if mid >= 80:
                side, e_bid, e_ask, smid = "yes", bid, ask, mid
            elif mid <= 20:
                side, e_bid, e_ask, smid = "no", 100 - ask, 100 - bid, 100 - mid
            else:
                continue
            if e_bid < 1 or e_ask < ENTRY_MIN:
                continue
            if e_ask > PROBE_MAX:
                # retired hi band (89-96c): paper-shadow only, so the
                # evidence keeps accumulating at zero cost (8/10)
                if e_ask <= 96:
                    self._shadow_note(tk, side, e_ask, "hi", smid)
                continue
            band = "hi" if e_ask > ENTRY_MAX else "core"
            if self._hi_blocked() if band == "hi" else self._core_blocked():
                # proven-negative lane: closed for money, open for paper
                self._shadow_note(tk, side, e_ask, band, smid)
                continue
            cands.append((smid, mk, side, e_bid, e_ask, band))
        cands.sort(key=lambda c: -c[0])
        placed = 0
        for smid, mk, side, e_bid, e_ask, band in cands:
            if placed >= budget:
                break
            tk = mk["ticker"]
            if underlying_key(tk) in u_keys:
                continue
            pside = smid / 100.0
            # Kelly at the BID (the signal), cost at the ASK (the toll)
            b_odds = (100 - e_bid) / e_bid
            f_star = max(0.0, pside - (1 - pside) / b_odds) * self._kelly()
            pct = self._hi_pct() if band == "hi" else self._bet_pct()
            cap_c = self._hi_cap_c() if band == "hi" else self._bet_cap_c()
            size = int(min(f_star, pct) * self.bank_c // e_ask)
            if size < 1:
                continue
            size = max(size, MIN_CONTRACTS)      # fee-rounding floor
            while size > MIN_CONTRACTS and e_ask * size > cap_c:
                size -= 1                        # trim ABOVE the floor only
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
            u_keys.add(underlying_key(tk))   # coin+hour is ONE opinion
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
        self._sweep_phantoms()      # drop already-settled ghosts
        self.mirror()
        self.settle()
        self.arb_reconcile()       # orphaned arb legs: unwind, not hope
        self.stop_check()
        self.miss_check()          # grade unfilled deaths vs settlement
        self.place()
        try:
            bal = self.balance_c()
        except Exception:
            bal = None
        self.save(balance_c=bal)
