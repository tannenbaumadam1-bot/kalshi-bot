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
# 8/13 manual resume switch (see _check_resume): repo file, not logs -
# it ships with a git pull so a halt can be lifted without console access
UNHALT_FILE = os.environ.get("DRIFT_LIVE_UNHALT_FILE", "unhalt.txt")
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
# 8/4 pursuit escalation (Adam: "fix the miss leak for good"): the 8/3
# ladder didn't stem the leak (49->52 misses, $26.72, ALL would-won in
# its first 20h). Cross at 30 min not 45, chase to 97c, and cross
# one-sided books (see _cross_expiring).
# 8/14: on 8/14 the book held $124.75 NAV with only $15.39 FILLED - the
# other $86.84 sat in unfilled joins, and because caps.open counts
# COMMITTED capital those idle bids were eating the entire risk budget
# (4,739 slate refusals). A join that hasn't filled in 15 minutes is
# priced at a market that has moved; recycling it beats holding it.
REST_MAX_H = float(os.environ.get("DRIFT_LIVE_REST_MAX_H", "0.25"))
CHASE_MAX_E = int(os.environ.get("DRIFT_LIVE_CHASE_MAX_E", "97"))
# 8/14: 11 graded misses died at the 97c ceiling and EVERY graded miss
# would have won (miss_would_won 107/107, miss_cost $54.60). PROVEN
# buckets - and only those - may chase two cents further. Unproven and
# self-blocked lanes keep the old ceiling. This is not a general
# loosening; it is paying up only on lanes that already earned it.
CHASE_MAX_E_PROVEN = int(os.environ.get("DRIFT_LIVE_CHASE_MAX_E_PROVEN", "99"))
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
# 8/4 Adam: small-account boost - 6% per bet until ACCOUNT NAV reaches
# $300, then auto-revert to the standard 3%. Note the handoff is a step
# DOWN in bet dollars (~$9 -> ~$4.50); that's the design, not a bug.
BET_PCT_BOOST = float(os.environ.get("DRIFT_LIVE_BET_PCT_BOOST", "0.06"))
# 8/12 (Adam): boost extended - 6%/bet holds until $500 account NAV
# (was $300), then the standard 3% takes over. At the current growth
# rate the step-down was days away; the machine keeps its aggression
# while the caps (city/slate/open/halt) keep the tail bounded.
BOOST_NAV_C = int(os.environ.get("DRIFT_LIVE_BOOST_NAV_C", "50000"))
# 8/13 pre-close flatten (defined here because the risk budget keys off
# it): inventory that never reaches settlement carries a different risk
# shape from inventory held overnight, so it earns a bigger allowance.
FLATTEN_ON = os.environ.get("DRIFT_LIVE_FLATTEN", "1") == "1"
# HOLDING-TIME-SCALED RISK: 85%% of NAV may be deployed when everything
# is flattened before close (the tail that killed 8/12 can't happen),
# 60%% when positions ride into settlement. Utilization was ~30%% - the
# bankroll, not the edge, was the binding constraint on compounding.
OPEN_PCT = float(os.environ.get("DRIFT_LIVE_OPEN_PCT",
                                "0.85" if FLATTEN_ON else "0.60"))
# 8/13 (Adam): 10%% was set when positions rode into settlement. Now
# that everything flattens before close, a day's loss is intraday drift
# plus fees rather than a settlement tail - and at ~99%% utilization a
# 10%% stop trips on noise, ending sessions that would have recovered.
# 15%% gives the book room to work a full day; the manual resume
# (unhalt.txt) remains for the exceptional case.
HALT_PCT = float(os.environ.get("DRIFT_LIVE_HALT_PCT", "0.15"))  # day loss
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
TAKER_MAX_SPREAD = int(os.environ.get("DRIFT_LIVE_TAKER_SPREAD", "6"))
# 8/10 TAKER-FIRST (Adam: "stop the misses, even if it means market
# orders over limit orders - at our NAV the spread doesn't matter").
# The 8/10 autopsy: 78 missed weather orders, 78/78 WOULD HAVE WON,
# $36.72 forfeited - double what the crypto book ever lost. Every
# level/climb entry now tries the ask FIRST (fenced by the band ceiling
# and TAKER_MAX_SPREAD, widened 4->6); the maker join is the fallback,
# not the default. Nickels keep resting - their edge IS the limit price.
TAKER_FIRST = os.environ.get("DRIFT_LIVE_TAKER_FIRST", "1") == "1"
# 7/28 widening (Adam-approved): miss-autopsy day one - 9/9 canceled
# unfilled orders would have WON, $3.44 forfeited to patience. Was 88/2.
# Live disaster stop (7/28, Adam-approved): exit autopsy says 5/6 stops
# would have WON (+ today's 4 stops were all intraday nowcast wobbles that
# recovered) - weather favorites routinely dip through 50c and settle
# green. Only a true collapse (<35c) gets cut now. Paper brain keeps 50.
STOP_C = float(os.environ.get("DRIFT_LIVE_STOP_C", "35"))
# 8/10 (Adam-approved, mirrors the crypto stop retirement): the weather
# disaster stop is RETIRED. Exit autopsy over 48 graded exits: 14 would
# have WON at settlement and the policy netted -$1.91 - the same
# wobble-tax every earlier exit rule paid. A binary that truly collapses
# between 3-minute polls can't be protected by a price stop anyway
# (8/8 Austin: 84c entry sold at 4c). Hold to settlement; the daily-loss
# halt and the NAV %-caps remain the risk controls.
# DRIFT_LIVE_STOP_ON=1 restores the old behaviour.
WSTOP_ON = os.environ.get("DRIFT_LIVE_STOP_ON", "0") == "1"
# --- Bucket routing (7/25): capital flows ONLY to trigger x entry-band
# cells that aren't proven losers on the live ledger. ---
BUCKET_GATE_ON = os.environ.get("DRIFT_LIVE_BUCKET_GATE", "1") == "1"
BUCKET_MIN_N = int(os.environ.get("DRIFT_LIVE_BUCKET_MIN_N", "8"))
# 8/14 STICKY BLOCKS. _bucket_stats() reads self.history, which TRIMS to
# 400 rows - so a blocked lane's evidence decays. level:90-92 sat at
# n=10 against a threshold of 8: three rows rolling off would have
# dropped it to 7 and silently UNBLOCKED a lane that lost money, with no
# alert. (Observed churn: level:80-84 fell 83 -> 47 in under two hours.)
# A proven-negative lane now stays blocked in a persistent map. The
# rolling window can still ADD blocks; it can no longer remove them.
BUCKET_STICKY = os.environ.get("DRIFT_LIVE_BUCKET_STICKY", "1") == "1"
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
# 8/10 (Adam-approved): lane cap tightened 30% -> 25% of NAV. The lane
# is 26-0 (+$12.86) but its payoff is ~1:6 against - survival math says
# the streak doesn't yet prove the win rate the price implies, so the
# lane's blast radius shrinks while the evidence accumulates.
NICKEL_LANE_PCT = float(os.environ.get("DRIFT_LIVE_NICKEL_LANE_PCT", "0.25"))
# Cross-on-expiry (7/30, Adam-approved): miss-autopsy hit 20/20 - EVERY
# unfilled cancel went on to WIN, $10.90 forfeited vs +$3.09 era profit.
# When a maker join goes stale (2h) and the signal still holds (side-mid
# >= our entry, ask within the trigger's band and <= CROSS_CHASE cents
# above it), pay the ask instead of dying on the vine. All caps, bucket
# blocks and the NAV nickel guardrails still apply to the cross.
CROSS_EXPIRY = os.environ.get("DRIFT_LIVE_CROSS_EXPIRY", "1") == "1"
# 8/7 THE LEAK, THIRD ATTEMPT - this time measured, not guessed. The 8/3
# and 8/4 passes both tuned cent-caps and both failed (49 -> 52 -> 63
# misses, ALL would-won). Root cause was never a cap value: it was that
# CROSS_MAX_CHASE anchors the decision to the price we happened to quote
# 30 minutes ago. A winner that runs 85c -> 96c is REFUSED for being 11c
# away from a stale number, even though buying at 96c something that
# settles at 100 is still profitable. The anchor is irrelevant to the
# economics; what matters is whether there is still edge left AT THE ASK.
#
# So: cross whenever the remaining room to settlement covers fees plus a
# margin (CROSS_MIN_EDGE_C), the signal still holds (smid >= entry) and
# the ask is inside the trigger's ceiling. CROSS_MAX_CHASE stays as an
# env-only rollback (0 = off).
CROSS_MAX_CHASE = int(os.environ.get("DRIFT_LIVE_CROSS_CHASE", "0"))
CROSS_MIN_EDGE_C = int(os.environ.get("DRIFT_LIVE_CROSS_MIN_EDGE", "3"))
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
# 8/7 FEE-ROUNDING FLOOR (Adam, both books). Kalshi rounds the fee UP to
# the next whole cent PER ORDER, so a 1-contract fill pays for a cent it
# never used - at 96c the raw fee is 0.27c but you are charged 1c, 25% of
# a 4c win against a true drag of 6.7%. Three contracts at 96c pay the
# SAME 1c. Across the crypto ledger 35% of trades were 1 contract and
# fees ate 8.4% of gross winnings; the weather book has the same shape.
# 8/7 (Adam, revised): the floor OVERRIDES the per-bet cap - every signal
# the book would have traded still gets traded, at >= MIN_CONTRACTS,
# rather than being skipped for not fitting the cap. Sizes above the
# floor still trim to the cap. Balance/reserve and the OPEN cap still
# bind. Kelly takes back control on its own once the bankroll asks for
# >= MIN_CONTRACTS unaided. Nickels are exempt - their own ladder
# already sizes well above the floor.
# 8/10 (Adam: "make all the weather positions a minimum of 5 contracts,
# the strategy is working"): floor raised 3 -> 5. Same override
# semantics as 8/7 - the floor beats the per-bet cap; balance/reserve
# and the OPEN cap still bind. The nickel lane's own trim floor rises
# to match (it was allowed to trim to 1).
MIN_CONTRACTS = int(os.environ.get("DRIFT_LIVE_MIN_CONTRACTS", "5"))
# --- 8/10 TWO-SIDED QUOTING (Adam-approved): the taker-first entries
# are the BID side of a market-making book; this is the OFFER side.
# Every filled position rests a maker SELL at a premium price -
# min(99, max(SELL_MIN, entry + SELL_MARKUP)), nickels floored at
# NICKEL_SELL_MIN. Anyone lifting it pays us more than settlement EV
# at typical entry psides, AND the capital comes back the same day
# instead of waiting for tomorrow's 11:00 settlement - so it can be
# re-deployed into the next signal (recycling > per-trade edge).
# Both floors sit above the 97c entry/chase ceiling, so a fill is
# always a realized profit by construction (net of the maker fee).
# This is NOT the retired stop/trail: we never sell below entry.
# --- 8/11 CONCENTRATION CAPS (Adam-approved; the autopsy's #1 tail
# risk): losses now come from correlation, not strategy - all 8 open
# positions settled the same morning and ~$17 sat on ONE Chicago
# thermometer. New entries respect:
#   per-CITY:  open+pending cost per city  <= CITY_CAP_PCT  x NAV
#   per-SLATE: open+pending cost per settlement date <= SLATE_CAP_PCT x NAV
# Existing positions are never touched; caps only gate NEW risk.
# Pyramid adds are exempt (already capped at 2 per position).
CITY_CAP_PCT = float(os.environ.get("DRIFT_LIVE_CITY_CAP_PCT",
                                    "0.15" if FLATTEN_ON else "0.10"))
SLATE_CAP_PCT = float(os.environ.get("DRIFT_LIVE_SLATE_CAP_PCT", "0.40"))
# --- 8/11 EARNED SIZING (Adam-approved): buckets the ledger has PROVEN
# (half-Kelly lanes) size to PROVEN_BET_PCT of NAV; everything else
# stays at the base pct. Aggression is earned per bucket, never global.
PROVEN_BET_PCT = float(os.environ.get("DRIFT_LIVE_PROVEN_BET_PCT", "0.08"))
QUOTE_ON = os.environ.get("DRIFT_LIVE_QUOTES", "1") == "1"
# --- 8/11 STANDING BID SIDE (Adam-approved; completes the MM loop):
# the offer side proved inventory-constrained - 65% of everything
# quoted gets lifted and the shelf sells out. This manufactures more
# inventory CHEAPER: on markets with established context (held now, or
# sold this era), rest a maker BUY a few cents under the market and let
# intraday wobbles fill us - the stop autopsy proved those dips are
# noise that recovers. Fills merge into held positions (cheapening the
# eventual offer) or reopen sold markets as their own evidence lane
# (trig "dip" buckets earn scale like every other lane). Never bids
# below the 80c floor where entries measurably lose; city/slate/open
# caps all apply at placement.
DIP_ON = os.environ.get("DRIFT_LIVE_DIPS", "1") == "1"
# 8/13: the miss ledger says 22 of 23 unfilled orders would have won
# ($13.69 of lost edge vs $1.10 recovered) - at 30%% utilization a
# tighter bid is the cheapest turn we can buy
DIP_DISCOUNT_C = int(os.environ.get("DRIFT_LIVE_DIP_DISCOUNT", "2"))
DIP_MIN_ROOM_C = 2      # bid must sit >= 2c under the market bid
DIP_REFRESH_C = int(os.environ.get("DRIFT_LIVE_DIP_REFRESH", "3"))
DIP_MAX_PCT = float(os.environ.get("DRIFT_LIVE_DIP_MAX_PCT", "0.25"))
SELL_MIN_C = int(os.environ.get("DRIFT_LIVE_SELL_MIN", "97"))
NICKEL_SELL_MIN_C = int(os.environ.get("DRIFT_LIVE_NICKEL_SELL_MIN", "98"))
SELL_MARKUP_C = int(os.environ.get("DRIFT_LIVE_SELL_MARKUP", "6"))
SELL_CAP_C = 99
# 8/11 OFFER LADDER (Adam-approved): night one lifted 60% of quotes at
# a flat 97c - the shelf was priced too low. Quotes now split across
# two rungs: half the position at the low rung (97 / nickel 98), the
# rest at 99. sold_check() grades every sale against the eventual
# settlement so the pricing argument is settled by the ledger, not
# opinion. DRIFT_LIVE_SELL_LADDER=0 restores single-rung quoting.
SELL_LADDER_ON = os.environ.get("DRIFT_LIVE_SELL_LADDER", "1") == "1"
# ---- 8/13 velocity build (Adam: make the offer side go parabolic) ----
# The offer engine earns ~37c per lift; the binding constraint is TURNS,
# not edge or capital. Capital parked until settlement can't turn, and
# settlement is also where the fat left tail lives (8/12 Miami: -$38.72
# in one night). So: flatten everything before close, walk the quote
# down as time runs out, and buy inventory in more markets.
FLATTEN_H = float(os.environ.get("DRIFT_LIVE_FLATTEN_H", "1.0"))
# time-decay rungs: (hours_left_at_least, low_rung, high_rung)
DECAY_ON = os.environ.get("DRIFT_LIVE_SELL_DECAY", "1") == "1"
DECAY_LADDER = ((6.0, 98, 99), (2.0, 97, 99), (0.0, 96, 97))
# dip lane: context-only was the training-wheels version - inventory is
# the constraint, so any scanned favorite is now fair game
DIP_CONTEXT_ONLY = os.environ.get("DRIFT_LIVE_DIP_CONTEXT", "0") == "1"
CYCLE_S = int(os.environ.get("DRIFT_LIVE_CYCLE_S", "600"))
# 8/13 velocity: during US trading hours the book re-scans every
# ACTIVE_CYCLE_S instead of the full nap - more requotes, more lifts,
# faster reaction. 180s keeps the 45-page event sweep well inside
# Kalshi's rate limits (~15 calls/min).
ACTIVE_CYCLE_S = int(os.environ.get("DRIFT_LIVE_ACTIVE_CYCLE_S", "90"))
ACTIVE_UTC_FROM = int(os.environ.get("DRIFT_LIVE_ACTIVE_FROM", "11"))
ACTIVE_UTC_TO = int(os.environ.get("DRIFT_LIVE_ACTIVE_TO", "24"))


# 8/14 CASH-IN ANCHOR (Adam confirmed total deposits = $100). Every
# percentage on the tracker anchors HERE, not to a `baseline` that
# merely happened to start near $100. If Adam deposits more, bump this
# (or set DRIFT_LIVE_DEPOSITS_C) or every % silently overstates.
DEPOSITS_C = int(os.environ.get("DRIFT_LIVE_DEPOSITS_C", "10000"))

# 8/14 WEEKLY CIRCUIT BREAKER (proposed 8/11, unbuilt until now). The
# DAILY halt is 15% of NAV, so three bad days in a row trip nothing and
# compound to ~-45%. This is the stop that survives a losing streak.
# Resume is deliberately manual: same unhalt.txt token as the day halt.
WEEK_HALT_PCT = float(os.environ.get("DRIFT_LIVE_WEEK_HALT_PCT", "0.15"))
WEEK_HALT_ON = os.environ.get("DRIFT_LIVE_WEEK_HALT", "1") == "1"


def _hold_hours(ots):
    """Hours of capital locked between order time and now. Returns None
    when the entry timestamp is missing or unparseable - callers treat
    None as 'not measurable' rather than zero, so a bad parse can never
    silently inflate a per-capital-hour figure."""
    if not ots:
        return None
    try:
        t0 = datetime.datetime.fromisoformat(str(ots).replace("Z", ""))
    except (ValueError, TypeError):
        return None
    try:
        dh = (datetime.datetime.now() - t0).total_seconds() / 3600.0
    except (TypeError, OverflowError):
        return None
    if dh < 0 or dh > 24 * 14:
        return None          # clock skew or a stale row: not measurable
    return round(dh, 3)


def _cycle_s():
    """Seconds to nap before the next drift cycle."""
    h = datetime.datetime.now(datetime.timezone.utc).hour
    if ACTIVE_UTC_FROM <= h < ACTIVE_UTC_TO:
        return min(ACTIVE_CYCLE_S, CYCLE_S)
    return CYCLE_S
# 8/6: crypto sub-cycle - the crypto book scans every CRYPTO_SUB_S
# seconds inside the drift book's CYCLE_S nap (hourly markets are too
# short-lived for a 10-minute look interval).
CRYPTO_SUB_S = int(os.environ.get("DRIFT_CRYPTO_SUB_S", "180"))
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
# 8/10 (Adam): crypto book PAUSED - weather gets the whole bankroll.
# Every %-of-NAV cap (bet, open, halt, nickel pos/lane) now computes on
# 100% of account NAV instead of half, so weather positions roughly
# double at today's NAV. Restore 0.5 when/if crypto earns its way back.
WX_ALLOC = float(os.environ.get("DRIFT_WX_ALLOC", "1.0"))
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
        self.k_cum = {}          # cumulative settlement ledger (never rolls)
        self.pnl_days = {}       # date -> realized $ that day (never trimmed)
        self.k_exit_realized_c = 0.0   # Kalshi's realized pnl on open markets
        # 8/11 K-TRUTH v2: gross sale proceeds per ticker (premium
        # offers + any exits). Kalshi's settlement rows carry OUR costs
        # but the BUYER's payout for contracts we sold - without this,
        # every profitable sale scored as a phantom settlement loss
        # (k_losses inflated 65->86 the first night the offer side ran).
        self.k_sold = {}         # tk -> proceeds in cents
        self.day_nav0_c = None   # NAV anchor at day start (for true today-P&L)
        self.autopsy = []        # every exit, graded vs eventual settlement
        self.miss = []           # every unfilled cancel, graded vs settlement
        self.sync_bad = []       # the actual mismatched tickers, if any
        self.exec_stats = {}     # maker/taker placed+filled, requotes
        self.k_positions = []    # Kalshi's positions, verbatim (the display)
        self.k_resting = []      # Kalshi's resting orders, verbatim
        self.dry_balance_c = 10000
        # 8/10 settlement receivable: we detect a win minutes before the
        # exchange credits the cash, so NAV briefly under-read by the
        # position's whole payout. The credit now sits here (consumed as
        # the balance rises, hard-expired at 15 min) and marked_nav adds
        # it - the dip class is closed.
        self.recv = []           # [[ts, amount_c], ...]
        self.recv_bal_c = None   # last balance seen by _recv_c
        # 8/10 two-sided book: resting premium SELL quotes, one per held
        # position - tk -> {legs: [{oid, px, count}], count, ots}
        self.offers = {}
        # 8/11: every sale graded against eventual settlement
        self.sold_log = []
        # 8/11 standing bid side: resting dip-bids, tk -> {oid, px,
        # count, city, strike, kind, cap, hl, date, pside, ots}
        self.dips = {}
        # 8/13 velocity ledger: a TURN = inventory acquired and resold
        # (lifted quote, pre-close flatten, or stop exit). Round trips -
        # not settlements - are what compound the offer engine, so they
        # get their own metric: {n, net_c, days:{d:{n,net_c}}, kinds:{}}
        self.turns = {}
        self.halt_base_c = 0.0   # halt measures loss since last resume
        self.bucket_blocked_cum = {}   # 8/14: bk -> evidence that blocked it
        self.orphan_legs = []     # 8/14: legs whose cancel FAILED
        self.week_halted = False  # 8/14 rolling-7-day circuit breaker
        self.week_halt_base_c = 0.0
        self.last_nav_c = 0.0     # PERSISTED: arms the breaker on cycle 1
        self.week_loss_c = None   # None = not yet evaluated this run
        self.week_limit_c = None  # (never render an uncomputed 0 as a cap)
        self.resume_token = ""   # the unhalt.txt date already consumed
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
                          "k_sold",
                          "day_nav0_c", "autopsy", "miss", "exec_stats",
                          "k_cum", "pnl_days", "recv", "recv_bal_c",
                          "offers", "sold_log", "dips", "turns",
                          "halt_base_c", "resume_token",
                          "week_halted", "week_halt_base_c", "last_nav_c",
                          "bucket_blocked_cum", "orphan_legs",
                          "k_positions", "k_resting", "dry_balance_c"):
                    if k in d:
                        setattr(self, k, d[k])
                if not d.get("nav0_v2"):
                    # 8/6 one-time: pre-fix anchor excluded crypto cost -
                    # discard it; next cycle re-anchors with _day_anchor_c
                    self.day_nav0_c = None
            except Exception:
                pass
        # 8/11 K-TRUTH v2 one-time rebuild: backfill sale proceeds from
        # the retained history (sold offers, stops), then clear the
        # cumulative ledger so the next sync_kalshi_truth reseeds the
        # FULL era history with sales folded in. Rows older than the
        # 200-row history window can't be adjusted - the era is young
        # enough that everything sold so far is still in the window.
        if not self.k_cum.get("v2_sold"):
            for h in self.history:
                tk = h.get("tk")
                if tk and h.get("exited") and h.get("exit_px"):
                    self.k_sold[tk] = round(
                        self.k_sold.get(tk, 0)
                        + h["exit_px"] * float(h.get("count", 0)), 1)
            self.k_cum = {"v2_sold": True}
            self.k_settlements = []
        self._seed_pnl_days()

    def _day_add(self, net_c):
        """Fold a realized P&L (cents) into today's bucket of the daily
        ledger. The ledger is one float per date and is NEVER trimmed -
        weekly/monthly performance derives from it, so it must not rot
        the way the 200-row history window does (8/3 lesson)."""
        d = today()
        self.pnl_days[d] = round(self.pnl_days.get(d, 0.0) + net_c / 100.0, 2)

    def _seed_pnl_days(self):
        """One-time backfill from the surviving history rows. Rows already
        trimmed off can't be dated - their P&L lands on the era epoch so
        every column still sums exactly to lifetime realized."""
        if self.pnl_days or not self.history:
            return
        tot = 0.0
        for h in self.history:
            p = h.get("pnl")
            ts = (h.get("ts") or "")[:10]
            if p is None or not ts:
                continue
            self.pnl_days[ts] = round(self.pnl_days.get(ts, 0.0) + float(p), 2)
            tot += float(p)
        resid = round(self.realized_c / 100.0 - tot, 2)
        if abs(resid) >= 0.01:
            d0 = LIVE_EPOCH[:10]
            self.pnl_days[d0] = round(self.pnl_days.get(d0, 0.0) + resid, 2)

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
        if not isinstance(getattr(self, "bucket_blocked_cum", None), dict):
            self.bucket_blocked_cum = {}
        for bk, a in agg.items():
            roll = bool(BUCKET_GATE_ON and a["n"] >= BUCKET_MIN_N
                        and a["net"] < 0)
            # latch the evidence the FIRST time a lane proves negative,
            # so the reason survives the window that produced it
            if roll and BUCKET_STICKY and bk not in self.bucket_blocked_cum:
                self.bucket_blocked_cum[bk] = {
                    "n": a["n"], "net": a["net"], "ts": now()}
            a["sticky"] = bool(BUCKET_STICKY
                               and bk in self.bucket_blocked_cum)
            a["blocked"] = bool(BUCKET_GATE_ON and (roll or a["sticky"]))
        return agg

    def _bucket_blocked(self, bstats, trig, entry):
        bk = self._bucket_key(trig, entry)
        # A lane whose rows have rolled off ENTIRELY vanishes from bstats,
        # and `bstats.get(bk)` would return None -> not blocked. That is
        # the same unblock-by-decay hole, one step further along, so the
        # persistent map is consulted directly rather than via bstats.
        if (BUCKET_STICKY
                and bk in (getattr(self, "bucket_blocked_cum", None) or {})):
            return bool(BUCKET_GATE_ON)
        a = bstats.get(bk)
        return bool(a and a.get("blocked"))

    def _bucket_is_proven(self, trig, entry, bstats=None):
        """Has this trigger x band actually earned the right to pay up?

        Same bar _kelly_frac already uses for half-Kelly (n >= proven
        threshold AND positive net), so 'proven' means one thing in this
        codebase. Used by the 8/14 chase ceiling: only lanes with live
        positive evidence may chase past CHASE_MAX_E.
        """
        if bstats is None:
            bstats = self._bucket_stats()
        if self._bucket_blocked(bstats, trig, entry):
            return False        # a blocked lane can never be "proven"
        a = bstats.get(self._bucket_key(trig, entry))
        return bool(a and not a.get("blocked")
                    and a.get("n", 0) >= KELLY_PROVEN_N
                    and a.get("net", 0) > 0)

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
    def _log_miss(self, o, unfilled, why="", ask=0):
        if unfilled <= 0.009:
            return
        self.miss.append({"tk": o["ticker"], "side": o["side"],
                          "entry": o["entry"], "count": round(float(unfilled), 2),
                          "trig": o.get("trig"),
                          "pside": round(o.get("pside", 0), 3),
                          "why": why or "unknown",
                          # 8/7: the ask we WALKED AWAY FROM. would_pnl is
                          # scored at our stale join price, which is a price
                          # we could no longer get - it overstates what was
                          # actually recoverable. cross_pnl below is honest.
                          "ask": int(ask or 0),
                          "ots": o.get("ots", ""), "cts": now(), "res": None})
        self.miss = self.miss[-200:]

    def _cross_expiring(self, o, count, q=None):
        """A stale unfilled join whose signal still holds crosses the
        spread as a taker instead of being forfeited. Returns True if a
        replacement taker order was placed for `count` (possibly trimmed
        to caps) contracts."""
        self._cross_why, self._cross_ask = "", 0
        if count <= 0:
            self._cross_why = "nothing_unfilled"
            return False
        tk = o["ticker"]
        if q is None:
            try:
                q = dp.DriftPaper._quotes(self, [tk]).get(tk)
            except Exception:
                q = None
        if not q:
            self._cross_why = "no_quote"
            return False
        yb, ya = q
        # 8/4: one-sided books cross too. Near settlement the runaway
        # winner's book often shows only an ask - exactly where all 52
        # logged misses died. Crossing needs OUR side's ask only; when
        # the bid is gone the signal-hold check falls back to that ask
        # (still fenced by max_e and CROSS_MAX_CHASE below).
        if o["side"] == "yes":
            if not ya:
                self._cross_why = "no_ask"
                return False
            bid_s, ask_s = (yb or 0), ya
        else:
            if not yb:
                self._cross_why = "no_ask"
                return False
            bid_s, ask_s = ((100 - ya) if ya else 0), 100 - yb
        smid = (bid_s + ask_s) / 2.0 if bid_s else float(ask_s)
        self._cross_ask = int(ask_s)
        if o.get("trig") == "nickel":
            max_e = dp.NICKEL_MAX_ENTRY
        else:
            # proven-bucket ceiling only. _bucket_blocked below still has
            # the final say, so a blocked lane can never reach this price.
            max_e = (CHASE_MAX_E_PROVEN
                     if self._bucket_is_proven(o.get("trig"), int(ask_s))
                     else CHASE_MAX_E)
        if ask_s <= 0:
            self._cross_why = "no_ask"
            return False
        if ask_s > max_e:
            self._cross_why = "ask_above_ceiling"
            return False
        if smid < o["entry"]:
            self._cross_why = "signal_faded"      # legitimate refusal
            return False
        # what's left to settlement after paying the ask, net of the
        # taker fee - this, not the distance from a stale quote, decides.
        edge = 100 - ask_s - fee_cents(ask_s, 1, taker=True)
        if edge < CROSS_MIN_EDGE_C:
            self._cross_why = "no_edge_left"
            return False
        if CROSS_MAX_CHASE > 0 and ask_s - o["entry"] > CROSS_MAX_CHASE:
            self._cross_why = "chase_cap"         # rollback path only
            return False
        if (o.get("trig") != "nickel"
                and self._bucket_blocked(self._bucket_stats(),
                                         o.get("trig"), ask_s)):
            self._cross_why = "bucket_blocked"
            return False
        size = int(count)
        if o.get("trig") == "nickel":
            nav_c = getattr(self, "last_nav_c", 0)
            if nav_c:
                cap = int(nav_c * NICKEL_POS_PCT)
                while size > 1 and ask_s * size > cap:
                    size -= 1
                if ask_s * size > cap:
                    self._cross_why = "nickel_pos_cap"
                    return False
        else:
            # NB: no MIN_CONTRACTS floor here - this path tops up an order
            # that may already be partly filled, so lifting the remainder
            # to the floor would overshoot the position's intended size.
            while size > 1 and ask_s * size > self.max_bet_c:
                size -= 1
            if ask_s * size > self.max_bet_c:
                self._cross_why = "bet_cap"
                return False
        try:
            bal = self.balance_c()
        except Exception:
            self._cross_why = "balance_error"
            return False
        if bal - ask_s * size < self.reserve_c:
            self._cross_why = "reserve"
            return False
        if self.open_cost_c() + ask_s * size > self.max_open_c:
            self._cross_why = "open_cap"
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
                self._cross_why = "order_rejected"
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
            # what crossing at the ask we refused would ACTUALLY have paid
            ask = int(row.get("ask") or 0)
            if ask > 0:
                tfee = fee_cents(ask, row["count"], taker=True)
                row["cross_pnl"] = round(
                    (((100 if won else 0) - ask) * row["count"] - tfee)
                    / 100.0, 2)

    def _miss_summary(self):
        graded = [r for r in self.miss if r.get("res") is not None]
        why = {}
        for r in self.miss:
            # rows from before the 8/7 instrumentation have no why key
            # at all - label them honestly instead of "unknown" (8/10)
            k = (r.get("why") or "unknown") if "why" in r else "pre_8/7_rows"
            a = why.setdefault(k, {"n": 0, "cost": 0.0})
            a["n"] += 1
            a["cost"] = round(a["cost"] + (r.get("cross_pnl") or 0), 2)
        # 8/12 era split (Adam): the cumulative log mixes pre-fix
        # history with post-fix residue and scared the tile. Pre-fix =
        # rows before taker-first shipped (8/10 15:30) or unclassified;
        # since-fix, the honest number is RECOVERABLE (scored at the
        # ask we actually refused - misses at unreachable prices are
        # the chase ceiling working, not a leak).
        _FIX = "2026-08-10T15:30:00"
        _new = [r for r in self.miss
                if "why" in r and (r.get("cts") or "") >= _FIX]
        _newg = [r for r in _new if r.get("res") is not None]
        return {"miss_n": len(self.miss),
                "miss_since": {
                    "n": len(_new),
                    "would_won": sum(1 for r in _newg
                                     if r.get("would_pnl", 0) > 0),
                    "cost": round(sum(r.get("would_pnl", 0)
                                      for r in _newg), 2),
                    "recoverable": round(sum(r.get("cross_pnl") or 0
                                             for r in _newg), 2)},
                "miss_pre": {"n": len(self.miss) - len(_new),
                             "cost": round(sum(r.get("would_pnl", 0)
                                               for r in graded)
                                           - sum(r.get("would_pnl", 0)
                                                 for r in _newg), 2)},
                "miss_settled": len(graded),
                "miss_would_won": sum(1 for r in graded
                                      if r.get("would_pnl", 0) > 0),
                # positive = money left on the table by not filling;
                # negative = patience dodged losers and saved money
                "miss_cost": round(sum(r.get("would_pnl", 0)
                                       for r in graded), 2),
                # 8/7: the HONEST number - scored at the ask we actually
                # refused, not at the stale join price we could never have
                # got. This is what the fix can really recover.
                "miss_recoverable": round(
                    sum(r.get("cross_pnl") or 0 for r in graded), 2),
                "miss_why": dict(sorted(why.items(),
                                        key=lambda kv: -kv[1]["n"]))}

    def _sync_diffs(self):
        """How far our internal book diverges from Kalshi's positions.
        0 = perfect mirror; anything else is displayed loudly, never hidden."""
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
        # 8/11 (the stuck sync_diffs=3): Kalshi's positions API keeps
        # listing settled AND sold-away markets for a while - both are
        # expected divergence, not drift. Excluded, exactly like the
        # crypto book has done since 8/7. A ticker we re-bought after
        # selling stays checked (only excluded when NOT in the book).
        done = set(self.settled_tks or [])
        sold = {t for t in (self.k_sold or {}) if t not in self.bets}
        bad = sorted(tk for tk in set(kp) | set(mine)
                     if kp.get(tk) != mine.get(tk)
                     and tk not in done and tk not in sold)
        # 8/7: name the offenders - a bare count is not actionable
        self.sync_bad = [{"tk": tk, "kalshi": kp.get(tk), "book": mine.get(tk)}
                         for tk in bad[:10]]
        return len(bad)

    def save(self, balance_c=None):
        os.makedirs("logs", exist_ok=True)
        mode_gate, gate_n = self._gate()
        real_w, real_l = self._real_record()
        recv_c = self._recv_c(balance_c)     # consume/expire BEFORE saving
        d = {"updated": now(), "mode": self.mode,
             "balance_c": balance_c,
             "recv": self.recv, "recv_bal_c": self.recv_bal_c,
             "recv_c": recv_c,
             "bets": self.bets, "pending": self.pending,
             "last_mid": self.last_mid, "last_vol": self.last_vol,
             "realized_c": self.realized_c, "fees_c": self.fees_c,
             "wins": self.wins, "losses": self.losses,
             "placed": self.placed, "canceled": self.canceled,
             "day": self.day, "day_pnl_c": self.day_pnl_c,
             "day_nav0_c": self.day_nav0_c, "nav0_v2": True,
             "dry_balance_c": self.dry_balance_c,
             "settled_tks": self.settled_tks[-300:],
             "k_settlements": self.k_settlements[:300],
             "k_cum": self.k_cum, "k_sold": self.k_sold,
             "sold_log": self.sold_log[-200:],
             "dips": self.dips,
             "offers": self.offers,
             "turns": self.turns,
             "halt_base_c": getattr(self, "halt_base_c", 0.0),
             "resume_token": getattr(self, "resume_token", ""),
             "pnl_days": self.pnl_days,
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
                 # LIFETIME record from the cumulative ledger - never a
                 # rolling window (falls back to the recent rows pre-seed)
                 "k_wins": (self.k_cum.get("w")
                            if self.k_cum.get("seeded")
                            else sum(1 for s in self.k_settlements if s["pnl"] > 0)),
                 "k_losses": (self.k_cum.get("l")
                              if self.k_cum.get("seeded")
                              else sum(1 for s in self.k_settlements if s["pnl"] < 0)),
                 "k_settle_realized": (round(self.k_cum.get("pnl", 0), 2)
                                       if self.k_cum.get("seeded")
                                       else round(sum(s["pnl"] for s in self.k_settlements), 2)),
                 "k_exit_realized": round(self.k_exit_realized_c / 100.0, 2),
                 "k_realized": round((self.k_cum.get("pnl", 0)
                                      if self.k_cum.get("seeded")
                                      else sum(s["pnl"] for s in self.k_settlements))
                                     + self.k_exit_realized_c / 100.0, 2),
                 "quotes": {"on": QUOTE_ON,
                            "resting": sum(len(o.get("legs") or [0])
                                           for o in self.offers.values()),
                            "positions": len(self.offers),
                            "sold": self.exec_stats.get("offers_sold", 0),
                            "sold_net": round(self.exec_stats.get(
                                "offers_sold_net_c", 0) / 100.0, 2),
                            "ladder": SELL_LADDER_ON,
                            # 8/11 sold-vs-settled verdict, from the ledger:
                            # kept > 0 = selling BEAT holding outright
                            "graded": sum(1 for r in self.sold_log
                                          if r.get("res") is not None),
                            "would_won": sum(1 for r in self.sold_log
                                             if (r.get("would_pnl") or 0) > 0),
                            "kept": round(sum(r.get("kept") or 0
                                              for r in self.sold_log), 2),
                            "min": SELL_MIN_C,
                            "nickel_min": NICKEL_SELL_MIN_C},
                 # 8/13 velocity: round trips are the compounding unit,
                 # and utilization says how much of the bankroll is
                 # actually working (it was ~30%% - the real ceiling)
                 "turns": self._turn_stats(),
                 "util": self._util_stats(),
                 "dips": {"on": DIP_ON,
                          "resting": len(self.dips),
                          "cost": round(sum(d.get("entry", 0)
                                            * d.get("count", 0)
                                            for d in self.dips.values())
                                        / 100.0, 2),
                          "fills": self.exec_stats.get("dip_fills", 0),
                          "fill_cost": round(self.exec_stats.get(
                              "dip_fill_cost_c", 0) / 100.0, 2),
                          "discount": DIP_DISCOUNT_C,
                          "max_pct": DIP_MAX_PCT},
                 "day_nav0": (round(self.day_nav0_c / 100.0, 2)
                              if self.day_nav0_c is not None else None),
                 # 8/14 CASH-IN ANCHOR. `baseline` (100.09) only LOOKS
                 # like the deposit - it is a launch artifact. This is the
                 # real money in; the dashboard anchors every % here.
                 "deposits": round(DEPOSITS_C / 100.0, 2),
                 # 8/14 WEEKLY CIRCUIT BREAKER state.
                 "week": {"halted": bool(getattr(self, "week_halted", False)),
                          "on": WEEK_HALT_ON,
                          # armed = has actually evaluated and holds a real
                          # cap. on=true + armed=false means enabled but
                          # not yet measured - a meaningful difference.
                          "armed": getattr(self, "week_limit_c", None) is not None,
                          "loss": (round(float(self.week_loss_c) / 100.0, 2)
                                   if getattr(self, "week_loss_c", None)
                                   is not None else None),
                          "limit": (round(float(self.week_limit_c) / 100.0, 2)
                                    if getattr(self, "week_limit_c", None)
                                    is not None else None),
                          "pct": WEEK_HALT_PCT},
                 "has_kalshi_truth": bool(self.k_settlements) or self.k_exit_realized_c != 0,
                 "exec": dict(self.exec_stats),
                 # mirror counts + fees straight from Kalshi's records
                 "k_open": (len(self.k_positions) if self.client is not None else None),
                 "k_resting_n": (len(self.k_resting) if self.client is not None else None),
                 "k_fees": round((self.k_cum.get("fees", 0)
                                  if self.k_cum.get("seeded")
                                  else sum(s.get("fee", 0) for s in self.k_settlements))
                                 + sum(p.get("fee", 0) for p in self.k_positions) / 100.0, 2),
                 "sync_diffs": self._sync_diffs(),
                 # 8/11: name the offenders on the tracker (crypto has
                 # done this since 8/7; weather's bare count was
                 # undiagnosable - Adam: "fix the weather sync detail")
                 "sync_bad": (self.sync_bad or [])[:10],
                 # live risk caps as currently applied (proof the dynamic
                 # NAV-% compounding is active, straight from the trader)
                 "caps": {"bet": round(self.max_bet_c / 100.0, 2),
                          "bet_pv": round(getattr(self, "max_bet_pv_c", 0)
                                          / 100.0, 2),
                          "open": round(self.max_open_c / 100.0, 2),
                          "halt": round(self.max_day_loss_c / 100.0, 2),
                          "city": CITY_CAP_PCT, "slate": SLATE_CAP_PCT,
                          "dyn": DYN_CAPS, "floor": ENTRY_FLOOR,
                          "chase": CHASE_MAX_E, "rest_h": REST_MAX_H,
                          "min_ct": MIN_CONTRACTS,
                          "bet_pct": getattr(self, "bet_pct_now", BET_PCT)},
                 # 8/12: the last concentration refusals, with the
                 # arithmetic that produced them (kind/ticker/px/ct +
                 # city_c/slate_c/nav_c cents at refusal time)
                 "cap_refuse_last": (getattr(self, "cap_refuse", [])
                                     or [])[-5:],
                 **self._autopsy_summary(),
                 **self._miss_summary(),
                 "buckets": [dict(v, bucket=k,
                                  proven=bool(not v["blocked"]
                                              and v["n"] >= KELLY_PROVEN_N
                                              and v["net"] > 0))
                             for k, v in
                             sorted(self._bucket_stats().items())],
                 # 8/14: lanes latched blocked by the persistent map, WITH
                 # the evidence that blocked them. A lane whose rows have
                 # rolled off entirely no longer appears in `buckets` at
                 # all - without this it would look unblocked on the
                 # tracker while still (correctly) refusing trades.
                 "buckets_sticky": dict(
                     getattr(self, "bucket_blocked_cum", None) or {}),
                 # 8/14: contracts offered beyond the position, per
                 # ticker. Must stay {} - anything here means we could
                 # sell more than we hold and flip the position.
                 "over_offer": self._over_offer_c(),
                 "orphan_legs": len(getattr(self, "orphan_legs", None) or []),
                 "open": len(self.bets), "resting": len(self.pending),
                 "placed": self.placed, "canceled": self.canceled,
                 "fees": round(self.fees_c / 100, 2),
                 "day_pnl": round(self.day_pnl_c / 100, 2),
                 "halted": self.halted,
                 "halt_base": round(float(getattr(self, "halt_base_c", 0))
                                    / 100.0, 2),
                 "resumed": getattr(self, "resume_token", "") or None,
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
        """Scale vs probe sizing, graded on the last 60 CHOSEN outcomes.

        8/13, two changes, both about measuring the strategy rather than
        the accidents around it:

        (1) `adopt` rows are excluded, exactly as _bucket_stats has
        always excluded them. An adopted position is one the bot chose
        neither the size, the price, nor the timing of - 8/12's Miami
        44-lot stack was a bug artifact, and letting a -$38.72 row it
        never decided on throttle every future bet measures the outage,
        not the edge. The loss stays in the ledger, NAV and every P&L
        number; it just doesn't get a vote on calibration.

        (2) completed TURNS count alongside settlements. The pre-close
        flatten means most inventory now exits before it ever settles,
        so a settlement-only window would advance a few rows a week and
        freeze the gate wherever it happened to be. A turn is graded on
        realized P&L (no outcome to compare a forecast against), so it
        votes on expectancy only - the calibration test stays settlement
        -only, where a forecast can actually be scored."""
        # only "nickel" (own lane, own guardrails) and "adopt" (never
        # chosen) are excluded - an unlabelled row is still a decision
        rows = [h for h in self.history
                if h.get("trig") not in ("nickel", "adopt")]
        cur = [h for h in rows
               if h.get("outcome") in (0, 1) or h.get("sold")][-60:]
        n = len(cur)
        if n < GATE_MIN_N:
            return "probe", n
        expectancy = sum(h.get("pnl") or 0 for h in cur) / n
        graded = [h for h in cur if h.get("outcome") in (0, 1)]
        gap = 0.0
        if graded:
            pred = sum(h.get("pside") or 0 for h in graded) / len(graded)
            act = sum(h["outcome"] for h in graded) / len(graded)
            gap = pred - act
        if expectancy > 0 and gap <= GATE_MAX_GAP:
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
            self.halt_base_c = 0.0
        self._check_resume()

    def _check_resume(self):
        """8/13 manual resume: `unhalt.txt` in the repo holding today's
        date (YYYY-MM-DD) lifts a daily halt once, on the next cycle
        after deploy. Deliberately file-based - it ships through the
        normal git pull, needs no console, and leaves an auditable
        record of who un-halted which day.

        Resume does NOT erase the day's loss. It rebases the halt so the
        book gets one more full daily budget (max_day_loss) from here;
        if that budget is spent too, the halt fires again and the file
        won't lift it twice."""
        try:
            with open(UNHALT_FILE) as f:
                token = (f.read() or "").strip()[:10]
        except Exception:
            return
        if not token or token != today():
            return
        if getattr(self, "resume_token", "") == token:
            return                       # already consumed for this day
        self.resume_token = token
        self.halt_base_c = float(self.day_pnl_c)
        self.halted = False
        # 8/14: one resume clears BOTH breakers. The weekly base rebases
        # to the current 7-day total so the next week's budget is fresh
        # rather than instantly re-tripping on the same losses.
        try:
            today_d = datetime.date.today()
            self.week_halt_base_c = sum(
                float(self.pnl_days.get(
                    (today_d - datetime.timedelta(days=i)).isoformat()) or 0)
                for i in range(7)) * 100.0
        except (TypeError, ValueError, OverflowError, AttributeError):
            self.week_halt_base_c = 0.0
        self.week_halted = False
        self.exec_stats["resumes"] = self.exec_stats.get("resumes", 0) + 1
        self._log([now(), "RESUME", self.mode, "", "", "", "", "",
                   "", "", "", round(self.day_pnl_c / 100.0, 2), token])
        print(f"  RESUME {token}: halt lifted, fresh daily budget "
              f"(day P&L stays {self.day_pnl_c / 100.0:+.2f})")

    def _day_anchor_c(self, bal):
        # day anchor = cash + BOTH books' open cost. 8/6 bug (Adam caught
        # it: tile +14.6% vs Kalshi ~flat): the anchor ignored the CRYPTO
        # book's overnight positions, so the moment lane 2 started holding
        # positions across midnight the baseline read ~$14 low and
        # "today's return" claimed the whole crypto book as profit.
        return (bal
                + sum(b["entry"] * b["count"] for b in self.bets.values())
                + _crypto_cost_c())

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

        def _row(s):
            ts = s.get("settled_time", "") or ""
            if ts and ts < LIVE_EPOCH:
                return None         # pre-Leonard account history: not ours
            tk = s.get("ticker") or ""
            if not _is_wx(tk):
                return None     # crypto settlements: the other book's ledger
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
            # 8/11 K-TRUTH v2: for contracts we SOLD, the settlement row
            # carries our cost but the BUYER's payout - fold our sale
            # proceeds back in so the ledger scores the actual round
            # trip instead of a phantom loss. (Kalshi's realized_pnl on
            # still-listed positions overlaps this for a few minutes
            # around settlement; the k_exit tile is a snapshot, this is
            # the permanent record.)
            sold_c = self.k_sold.get(tk) or 0
            if sold_c:
                pnl_c += sold_c
            return {"tk": tk, "pnl": round(pnl_c / 100.0, 2),
                    "sold": bool(sold_c),
                    "fee": round(fee / 100.0, 2), "ts": ts}

        try:
            # CUMULATIVE LEDGER (8/3, Adam: 'the tracker needs to be
            # perfect'): two books share one settlement feed, so the
            # 200-row window ROLLS - the record must never shrink. Every
            # settlement is folded into persistent counters exactly once
            # (keyed ticker+time). One-time seed paginates the FULL era
            # history so the lifetime record is exact from day one.
            if not self.k_cum.get("seeded"):
                cur, pages = None, 0
                allrows = []
                try:
                    while pages < 15:
                        batch, cur = self.client.get_settlements_page(
                            limit=200, cursor=cur)
                        pages += 1
                        allrows.extend(batch)
                        if not cur or not batch:
                            break
                        oldest = min((b.get("settled_time") or "~")
                                     for b in batch)
                        if oldest < LIVE_EPOCH:
                            break       # reached pre-era history
                    self.k_cum["seeded"] = True
                except Exception:
                    allrows = self.client.get_settlements(limit=200)
                    self.k_cum["seeded"] = True
                src = allrows
            else:
                src = self.client.get_settlements(limit=200)
            rows, seen = [], set(self.k_cum.get("keys") or [])
            for s in src:
                r = _row(s)
                if r is None:
                    continue
                rows.append(r)
                key = f"{r['ts']}|{r['tk']}"
                if key in seen:
                    continue
                seen.add(key)
                if r["pnl"] > 0:
                    self.k_cum["w"] = self.k_cum.get("w", 0) + 1
                elif r["pnl"] < 0:
                    self.k_cum["l"] = self.k_cum.get("l", 0) + 1
                self.k_cum["pnl"] = round(self.k_cum.get("pnl", 0) + r["pnl"], 2)
                self.k_cum["fees"] = round(self.k_cum.get("fees", 0) + r["fee"], 2)
            self.k_cum["keys"] = sorted(seen)[-800:]
            rows.sort(key=lambda r: r["ts"], reverse=True)
            self.k_settlements = rows[:300]
        except Exception:
            pass
        # THE MIRROR (Adam 7/25: "the tracker should perfectly reflect
        # kalshi"): positions and resting orders are stored VERBATIM from the
        # exchange each cycle - the dashboard renders these, never our book.
        try:
            kp = []
            for p in self.client.get_positions():
                # 8/11: Kalshi trades FRACTIONAL contracts now (a buyer
                # took 4.75 of a 5-lot offer, leaving a 0.25 stub the
                # int-rounding made invisible). Mirror the exact float.
                pos = float(self._kval(p, "position") or 0)
                if abs(pos) < 0.01:
                    continue
                tk = p.get("ticker") or ""
                if not _is_wx(tk):
                    continue    # mirror shows THIS book vs ITS universe
                cnt = round(abs(pos), 2)
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
                           "oid": o.get("order_id") or o.get("id"),
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

    def _turn_add(self, net_c, kind, hold_h=None):
        """Book one completed round trip.

        8/14: settlements ARE turns now. The old ledger counted only
        inventory we bought and resold (lift/flatten/stop), which made
        `per_turn` a WINNERS-ONLY sample - positions that never lifted
        and rode to settlement never appeared in the denominator. That
        is the same class of flattering gauge as the $100 baseline, and
        it nearly justified scaling. kinds/kinds_net_c keep the lift-only
        view available so the two can still be compared.

        hold_h (item 5 groundwork): capital-hours locked by this turn.
        On a capital-constrained book the objective is net per
        CAPITAL-HOUR, not net per turn - a 97c ask that lifts in 2h beats
        a 99c ask that lifts in 9h. Nothing tunes on this yet; it only
        starts accumulating the data so the ladder can be retuned on
        evidence instead of instinct.
        """
        t = self.turns if isinstance(self.turns, dict) else {}
        d = datetime.date.today().isoformat()
        t["n"] = int(t.get("n", 0)) + 1
        t["net_c"] = round(float(t.get("net_c", 0)) + net_c, 1)
        day = t.setdefault("days", {}).setdefault(d, {"n": 0, "net_c": 0.0})
        day["n"] = int(day.get("n", 0)) + 1
        day["net_c"] = round(float(day.get("net_c", 0)) + net_c, 1)
        k = t.setdefault("kinds", {})
        k[kind] = int(k.get(kind, 0)) + 1
        kn = t.setdefault("kinds_net_c", {})
        kn[kind] = round(float(kn.get(kind, 0)) + net_c, 1)
        # capital-hours, per kind, for the per-capital-hour objective
        if hold_h is not None and hold_h >= 0:
            kh = t.setdefault("kinds_hold_h", {})
            kh[kind] = round(float(kh.get(kind, 0)) + float(hold_h), 2)
        # keep the day map bounded
        if len(t.get("days", {})) > 60:
            for old in sorted(t["days"])[:-60]:
                t["days"].pop(old, None)
        self.turns = t

    def _cancel_leg(self, oid, tk):
        """Cancel one resting leg. Returns True only if the exchange
        actually took the cancel.

        8/14 ROOT CAUSE of the over-offer bug: both offer-cleanup paths
        did `except Exception: pass` and then deleted the book entry, so
        a cancel that FAILED left a live sell leg on the exchange with
        nothing tracking it. Those orphans accumulated until positions
        were offered 2-3x over (NOLA held 10, offered 30). Selling more
        than we hold does not merely close the position - it flips us
        long the outcome we bet against.

        A failed cancel is now remembered and retried every cycle
        instead of being dropped on the floor.
        """
        if self.client is None:
            return True
        try:
            self.client.cancel_order(oid)
            return True
        except Exception:
            if not any(o.get("oid") == oid for o in self.orphan_legs):
                self.orphan_legs.append({"oid": oid, "tk": tk,
                                         "ts": now()})
                self.exec_stats["orphan_legs"] = (
                    self.exec_stats.get("orphan_legs", 0) + 1)
            return False

    def _retry_orphan_legs(self):
        """Re-attempt every cancel that previously failed. Bounded, and
        drops an orphan once the exchange no longer lists it (it either
        canceled, filled, or the market settled)."""
        if self.client is None or not self.orphan_legs:
            return
        live = {r.get("oid") for r in (self.k_resting or [])}
        keep = []
        for o in self.orphan_legs[:200]:
            oid = o.get("oid")
            if live and oid not in live:
                self.exec_stats["orphan_cleared"] = (
                    self.exec_stats.get("orphan_cleared", 0) + 1)
                continue        # gone from the book: nothing to cancel
            try:
                self.client.cancel_order(oid)
                self.exec_stats["orphan_cleared"] = (
                    self.exec_stats.get("orphan_cleared", 0) + 1)
            except Exception:
                keep.append(o)
        self.orphan_legs = keep[-200:]

    def _over_offer_c(self):
        """Contracts offered beyond what we actually hold, per ticker.

        The invariant that matters is a QUANTITY one - offering more
        than the position - so it is checked directly rather than
        inferred from order bookkeeping. Published so the condition can
        never again be invisible for two days.
        """
        out = {}
        for tk, off in (self.offers or {}).items():
            held = float((self.bets.get(tk) or {}).get("count", 0))
            offered = sum(float(l.get("count", 0))
                          for l in (off.get("legs") or []))
            if offered > held + 0.009:
                out[tk] = round(offered - held, 2)
        return out

    def _week_loss_exceeded(self):
        """True when the rolling 7-day realized P&L is worse than
        WEEK_HALT_PCT of NAV, measured from the last weekly resume.

        Reads the persistent daily ledger (pnl_days), which is never
        trimmed, so this survives restarts. Returns False on any missing
        or unparseable data - a broken ledger must never halt a healthy
        book, and must never be mistaken for a healthy one either (the
        `week` block on the tracker publishes what it actually saw).
        """
        if not isinstance(getattr(self, "pnl_days", None), dict):
            return False
        try:
            today = datetime.date.today()
            win = [(today - datetime.timedelta(days=i)).isoformat()
                   for i in range(7)]
            # pnl_days is in DOLLARS (see _day_add); everything else in
            # this class is cents. Convert once, here, explicitly.
            wk_c = sum(float(self.pnl_days.get(d) or 0)
                       for d in win) * 100.0
        except (TypeError, ValueError, OverflowError):
            return False
        nav_c = float(getattr(self, "last_nav_c", 0) or 0)
        if nav_c <= 0:
            # NAV unknown (cold start, before _refresh_caps has run). Fail
            # safe - but publish None, not 0.0: a limit of "0.00" on the
            # tracker reads like an armed cap of zero when it actually
            # means the breaker has not evaluated yet.
            self.week_loss_c = None
            self.week_limit_c = None
            return False
        limit_c = max(HALT_FLOOR_C, nav_c * WEEK_HALT_PCT)
        self.week_loss_c = round(wk_c - float(
            getattr(self, "week_halt_base_c", 0) or 0), 1)
        self.week_limit_c = round(limit_c, 1)
        return self.week_loss_c <= -limit_c

    def _util_stats(self):
        """How much of the risk budget is actually deployed. Growth =
        edge/turn x turns/day x UTILIZATION, and the third term was the
        one nobody could see."""
        dep = self.open_cost_c()
        dip = sum(d.get("entry", 0) * d.get("count", 0)
                  for d in self.dips.values())
        cap = max(1, self.max_open_c)
        # 8/14 WORKING vs COMMITTED. `deployed` sums FILLED positions AND
        # unfilled resting joins, so 96% "utilization" hid the fact that
        # only 12% of NAV was actually earning ($15.39 filled against
        # $86.84 bid out and idle). Those are not the same thing for a
        # compounding book: committed capital blocks new trades via
        # caps.open without generating a cent. Split so the difference
        # can never hide inside one number again.
        work = sum(b["entry"] * b["count"] + b.get("fee", 0)
                   for b in self.bets.values())
        commit = max(0.0, dep - work)
        return {"working": round(work / 100.0, 2),
                "committed": round(commit / 100.0, 2),
                "working_pct": round(work / cap, 3),
                "deployed": round(dep / 100.0, 2),
                "dips": round(dip / 100.0, 2),
                "cap": round(self.max_open_c / 100.0, 2),
                "pct": round(dep / cap, 3),
                "pct_with_dips": round((dep + dip) / cap, 3),
                "positions": len(self.bets),
                "resting": len(self.pending),
                "flatten_h": FLATTEN_H if FLATTEN_ON else None,
                "cycle_s": _cycle_s()}

    def _turn_stats(self):
        t = self.turns if isinstance(self.turns, dict) else {}
        d = datetime.date.today().isoformat()
        day = (t.get("days") or {}).get(d, {"n": 0, "net_c": 0.0})
        n = int(t.get("n", 0))
        dn = int(day.get("n", 0))
        return {"n": n,
                "net": round(float(t.get("net_c", 0)) / 100.0, 2),
                "per_turn": (round(float(t.get("net_c", 0)) / n / 100.0, 3)
                             if n else None),
                "today_n": dn,
                "today_net": round(float(day.get("net_c", 0)) / 100.0, 2),
                "today_per_turn": (round(float(day.get("net_c", 0))
                                         / dn / 100.0, 3) if dn else None),
                "kinds": dict(t.get("kinds") or {}),
                # 8/14: net and capital-hours BY KIND. `kinds` alone can't
                # answer the only question that matters for the ladder -
                # whether lifts and settlements earn differently per hour
                # of capital locked. per_ch is the objective the decay
                # ladder gets retuned against once these accumulate.
                "kinds_net": {k: round(float(v) / 100.0, 2)
                              for k, v in (t.get("kinds_net_c") or {}).items()},
                "kinds_hold_h": dict(t.get("kinds_hold_h") or {}),
                "per_ch": {
                    k: round(float((t.get("kinds_net_c") or {}).get(k, 0))
                             / 100.0 / h, 3)
                    for k, h in (t.get("kinds_hold_h") or {}).items()
                    if h and float(h) > 0},
                "flatten_h": FLATTEN_H if FLATTEN_ON else None}

    def _sell_rungs(self, b, hrs):
        """8/13 time-decay ladder: hold out for 99c early, walk the ask
        down as the close approaches. A quote that never lifts earns
        nothing and locks the capital for the whole session - the last
        hour is worth more as a completed turn than as a proud price.
        Never quotes at or below cost."""
        floor = (NICKEL_SELL_MIN_C if b.get("trig") == "nickel"
                 else SELL_MIN_C)
        lo_t, hi_t = SELL_MIN_C, SELL_CAP_C
        if DECAY_ON and hrs is not None:
            for h_min, lo_d, hi_d in DECAY_LADDER:
                if hrs >= h_min:
                    lo_t, hi_t = lo_d, hi_d
                    break
        lo_t = max(lo_t, floor if hrs is None or hrs >= 2.0 else 1)
        entry = int(b["entry"])
        px = min(SELL_CAP_C, max(lo_t, entry + SELL_MARKUP_C))
        hi = max(px, min(SELL_CAP_C, hi_t))
        if px <= entry:
            return None
        n_all = int(float(b["count"]))
        if SELL_LADDER_ON and n_all >= 2 and px < hi:
            lo_n = n_all - n_all // 2
            return [(px, lo_n), (hi, n_all - lo_n)]
        return [(px, n_all)]

    def flatten(self, mkts=None):
        """8/13 pre-close flatten: sell EVERY open position at the bid
        once its market is within FLATTEN_H of close.

        This is the velocity build's centrepiece. Two things it buys:
        (1) the capital comes back the same session instead of being
        locked to settlement, so it can turn again - that is what
        compounds; (2) it removes settlement risk entirely, which is the
        only place this book has ever taken a big loss (8/12 Miami
        -$38.72 overnight). A held favorite risks 88c to make 12c; a
        flattened one banks the spread and goes back to work."""
        if not FLATTEN_ON or not self.bets:
            return 0
        by_tk = {m["ticker"]: m for m in (mkts or [])}
        done = 0
        for tk, b in list(self.bets.items()):
            mk = by_tk.get(tk)
            if mk is None:
                continue
            try:
                hrs = float(mk.get("hrs"))
            except (TypeError, ValueError):
                continue
            if hrs > FLATTEN_H:
                continue
            yb, ya = mk.get("yes_bid"), mk.get("yes_ask")
            bid = yb if b["side"] == "yes" else (100 - ya if ya else 0)
            if not bid or bid <= 0:
                continue            # no honest exit: settlement decides
            cnt = int(float(b.get("count", 0)))
            if cnt < 1:
                continue
            # cancel our resting quotes first - selling into our own
            # book would double-sell the position
            off = self.offers.get(tk)
            if off and self.client is not None:
                for leg in (off.get("legs") or []):
                    try:
                        self.client.cancel_order(leg.get("oid"))
                    except Exception:
                        pass
            self.offers.pop(tk, None)
            if self.client is not None:
                try:
                    self.client.create_order(tk, action="sell",
                                             side=b["side"], count=cnt,
                                             price_cents=int(bid))
                except Exception:
                    continue
            fee = fee_cents(int(bid), cnt, taker=True)
            net = (int(bid) - b["entry"]) * cnt - b.get("fee", 0) - fee
            self._k_sold_add(tk, int(bid) * cnt)
            self.realized_c += net
            self._day_add(net)
            self.day_pnl_c += net
            self.fees_c += fee
            self._turn_add(net, "flatten",
                           hold_h=_hold_hours(b.get("ots")))
            self.exec_stats["flattened"] = (
                self.exec_stats.get("flattened", 0) + 1)
            if self.client is None:
                self.dry_balance_c += int(bid) * cnt - fee
            self.history.append({"tk": tk, "city": b["city"],
                                 "strike": b["strike"],
                                 "kind": b.get("kind", "ge"),
                                 "cap": b.get("cap"), "hl": b["hl"],
                                 "side": b["side"], "trig": b.get("trig"),
                                 "pside": round(b.get("pside", 0), 3),
                                 "entry": b["entry"], "count": cnt,
                                 "outcome": None, "exited": True,
                                 "sold": True, "exit_px": int(bid),
                                 "pnl": round(net / 100.0, 2),
                                 "ts": now(), "ots": b.get("ots", ""),
                                 "era": ERA})
            self.history = self.history[-400:]
            self.autopsy.append({"tk": tk, "side": b["side"],
                                 "entry": b["entry"], "count": cnt,
                                 "fee": b.get("fee", 0),
                                 "exit_pnl": round(net / 100.0, 2),
                                 "kind": "FLATTEN", "trig": b.get("trig"),
                                 "ts": now()})
            self.autopsy = self.autopsy[-200:]
            self._log([now(), "FLATTEN", self.mode, b["city"], b["strike"],
                       b["hl"], b["side"], round(b.get("pside", 0), 3),
                       int(bid), cnt, "", round(net / 100.0, 2),
                       b.get("oid", "")])
            del self.bets[tk]
            done += 1
        return done

    def _conc_cost_c(self, city, date):
        """FILLED city/date cost in cents: the MAX of our book and
        Kalshi's own position feed. 8/12 Miami lesson ($43 = 33% of NAV
        on one thermometer, 3x the city cap): a fill our book hasn't
        seen yet (promotion bug, adoption lag - any cause) is invisible
        to caps that only read self.bets. The exchange view can lag a
        settlement (settled tickers excluded below) but it NEVER lags a
        fill, so the max of the two views is the honest exposure."""
        bc = dc = kc = kd = 0
        for b in self.bets.values():
            c0 = b.get("entry", 0) * b.get("count", 0)
            if b.get("city") == city:
                bc += c0
            if b.get("date", "") == date:
                dc += c0
        done = set(self.settled_tks)
        for p in (self.k_positions or []):
            tk = p.get("ticker")
            if not tk or tk in done:
                continue
            c0 = (p.get("entry") or 0) * (p.get("count") or 0)
            if p.get("city") == city:
                kc += c0
            if p.get("date", "") == date:
                kd += c0
        return max(bc, int(kc)), max(dc, int(kd))

    def _cap_refused(self, kind, tk, entry, size, c_cost, d_cost, nav_cc):
        """8/12: name every concentration refusal on the tracker. 2,353
        bare counter ticks were undiagnosable from outside - the zombie-
        pending saturation took a session and two /public diffs to find.
        Last 5 published; never again without the numbers."""
        self.cap_refuse = (getattr(self, "cap_refuse", []) + [{
            "kind": kind, "tk": tk, "px": entry, "ct": size,
            "city_c": c_cost, "slate_c": d_cost, "nav_c": nav_cc,
            "ts": now()}])[-20:]

    # ---- settlement receivable (8/10): bridges the minutes between our
    # settlement detection and the exchange's cash credit so NAV never
    # dips by a winner's payout. Consumed as the balance rises; anything
    # unconsumed hard-expires at 15 minutes so it can never overstate
    # NAV for long. ----
    def _k_sold_add(self, tk, proceeds_c):
        """Record gross sale proceeds for the k-truth ledger (8/11)."""
        self.k_sold[tk] = round(self.k_sold.get(tk, 0) + proceeds_c, 1)
        if len(self.k_sold) > 400:      # bounded; a market folds once
            for k in list(self.k_sold)[:len(self.k_sold) - 400]:
                del self.k_sold[k]

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

    def _cancel_pending(self, oid, o):
        """8/12 invariant (the Miami stack began as fills the book never
        saw): a pending BUY is canceled ONLY through here, and any fills
        the cancel response reveals are booked before the caller may
        delete the row. Kalshi's DELETE returns the final order object -
        the authoritative fill count at the moment of death. Returns
        False when the cancel itself failed (caller must keep the row)."""
        try:
            resp = self.client.cancel_order(oid)
        except Exception:
            return False
        ro = {}
        if isinstance(resp, dict):
            ro = resp.get("order") if isinstance(resp.get("order"),
                                                 dict) else resp

        def g(key):
            v = ro.get(key + "_fp")
            if v is None:
                v = ro.get(key)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        filled = g("filled_count")
        if filled is None:
            ic, rc = g("initial_count"), g("remaining_count")
            if ic is not None and rc is not None:
                filled = max(0.0, ic - rc)
        if filled is not None:
            seen = float(o.get("filled_seen", 0))
            new = round(filled - seen, 2)
            if new > 0.009:
                self._promote_fill(oid, o, new)
                o["filled_seen"] = round(seen + new, 2)
        return True

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
        if not self.pending and not self.offers:
            return
        resting_ids = set()
        resting_by_tk = {}
        fills_by_oid = None
        if self.client is not None:
            try:
                for ro in self.client.get_resting_orders():
                    roid = ro.get("order_id") or ro.get("id")
                    resting_ids.add(roid)
                    # 8/12 HARD-LEARNED: do NOT filter heal candidates by
                    # the row's action/side - GET /portfolio/orders
                    # projects EVERYTHING onto the yes ledger (our NO buy
                    # at 89c comes back as "sell yes 11c", our NO-position
                    # ladder leg at 97c as "buy yes 3c"). An action filter
                    # here spent 20 live minutes canceling real NO-side
                    # entry joins as "zombies". The owned-set below is the
                    # only correct guard against adopting our own quotes.
                    resting_by_tk.setdefault(ro.get("ticker"), []).append(roid)
            except Exception:
                return                      # can't verify -> touch nothing
            # heal synthetic/mismatched oids by ticker before lifecycle
            # checks. 8/10: our own SELL quotes rest on tickers we hold -
            # they must never be adopted as buy-order ids, so they count
            # as owned here.
            owned = ({oid for oid in self.pending}
                     | {leg.get("oid") for o in self.offers.values()
                        for leg in (o.get("legs") or [])}
                     | {o.get("oid") for o in self.offers.values()
                        if o.get("oid")}
                     | {d.get("oid") for d in self.dips.values()})
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
            # 8/12: ZOMBIE-DROP and the orphan sweep that briefly lived
            # here were REMOVED the same day - both were built on
            # misreading the yes-projected orders API (see note above)
            # and canceled live NO-side entry joins. Stray-leg hygiene
            # is handled where it belongs: _check_offers cancels every
            # surviving leg before dropping a book entry.
            try:
                fills_by_oid = {}
                for f in self.client.get_fills(limit=100):
                    fo = f.get("order_id")
                    # 8/11: fractional fills are real - count them exactly
                    fc = round(float(f.get("count_fp") or f.get("count") or 0), 2)
                    fills_by_oid[fo] = fills_by_oid.get(fo, 0) + fc
            except Exception:
                fills_by_oid = None         # fills unknown this cycle
                # 8/14: when the fills API is down we ASSUME the rest
                # filled (below), which silently invents fills. Count it
                # so a bad API day can never masquerade as clean data.
                self.exec_stats["fills_api_down"] = (
                    self.exec_stats.get("fills_api_down", 0) + 1)
        nowdt = datetime.datetime.now()
        for oid, o in list(self.pending.items()):
            seen = float(o.get("filled_seen", 0))
            if self.client is not None and oid not in resting_ids:
                # gone from the resting book: filled and/or canceled
                if fills_by_oid is not None:
                    filled = max(0.0, round(fills_by_oid.get(oid, 0)
                                            - seen, 2))
                else:
                    filled = max(0.0, o["count"] - seen)  # assume rest filled
                if filled > 0.009:
                    self._promote_fill(oid, o, filled)
                if filled <= 0.009 and seen <= 0.009:
                    self.canceled += 1
                unfilled_v = round(o["count"] - seen - filled, 2)
                # 8/10 (Adam: "stop the misses"): a vanished order is
                # re-entered at the ask RIGHT NOW instead of being
                # forfeited - signal, caps and balance are all
                # re-checked inside _cross_expiring. Since the 8/7
                # instrumentation EVERY vanished order (15/15 weather,
                # 9/9 crypto) settled as a winner we didn't hold.
                # (8/11: fractional remainders <1 can't be ordered -
                # they fall through to the miss log instead)
                if (unfilled_v >= 1 and CROSS_EXPIRY
                        and self._cross_expiring(o, int(unfilled_v))):
                    self.exec_stats["revanish"] = (
                        self.exec_stats.get("revanish", 0) + 1)
                else:
                    self._log_miss(o, unfilled_v,
                                   why="vanished_" + (getattr(
                                       self, "_cross_why", "") or "cross_off"),
                                   ask=getattr(self, "_cross_ask", 0))
                del self.pending[oid]
                continue
            # still resting: promote any PARTIAL fills so stops/settles
            # protect those contracts immediately
            if self.client is not None and fills_by_oid is not None:
                new = max(0.0, round(fills_by_oid.get(oid, 0) - seen, 2))
                if new > 0.009:
                    self._promote_fill(oid, o, new)
                    o["filled_seen"] = round(seen + new, 2)
            try:
                age_h = (nowdt - datetime.datetime.fromisoformat(o["ots"])).total_seconds() / 3600
            except Exception:
                age_h = 0
            if age_h > REST_MAX_H:
                if self.client is not None:
                    # 8/12: cancel-and-book - fills revealed by the
                    # cancel response land in bets BEFORE the row dies,
                    # so `unfilled` below is the truth, not a snapshot
                    if not self._cancel_pending(oid, o):
                        continue
                unfilled = round(o["count"]
                                 - float(o.get("filled_seen", 0)), 2)
                crossed = (CROSS_EXPIRY and unfilled >= 1
                           and self._cross_expiring(o, int(unfilled)))
                if not crossed:
                    self._log_miss(o, unfilled,
                                   why=("cross_off" if not CROSS_EXPIRY
                                        else getattr(self, "_cross_why", "")),
                                   ask=getattr(self, "_cross_ask", 0))
                    if float(o.get("filled_seen", 0)) <= 0.009:
                        self.canceled += 1
                self._log([now(), "CANCEL", self.mode, o["city"], o["strike"],
                           o["hl"], o["side"], round(o["pside"], 3),
                           o["entry"], o["count"], "", "", oid])
                del self.pending[oid]
        if self.client is not None:
            self._check_offers(resting_ids, fills_by_oid)
            self._check_dips(resting_ids, fills_by_oid)

    # ---- 8/10 two-sided book: the OFFER side --------------------------
    def quote_offers(self, mkts=None):
        """Rest a premium maker SELL against every held position. The
        entries (taker-first) are the bid side; this completes the book.
        A lifted offer = realized profit above entry AND same-day capital
        recycling. Never quotes below entry; the retired stop/trail
        machinery is untouched."""
        if not QUOTE_ON:
            return
        # legacy single-leg offers from before the 8/11 ladder: wrap
        for tk, off in list(self.offers.items()):
            if "legs" not in off and "oid" in off:
                self.offers[tk] = {"legs": [{"oid": off.get("oid"),
                                             "px": off.get("px"),
                                             "count": off.get("count")}],
                                   "count": off.get("count"),
                                   "ots": off.get("ots", now())}
        hrs_by_tk = {m["ticker"]: m.get("hrs") for m in (mkts or [])}
        # cleanup/resize pass: settled positions drop their quotes;
        # resized positions (partial sell, pyramid add) requote fresh,
        # and 8/13 a quote whose TIME-DECAY target has moved is
        # re-hung at the new rungs (a stale 99c on a market closing in
        # an hour is just a locked position)
        for tk, off in list(self.offers.items()):
            b = self.bets.get(tk)
            if (b is not None and int(float(b.get("count", 0)))
                    == int(off.get("count", 0))):
                want = self._sell_rungs(b, hrs_by_tk.get(tk))
                if want is None or [r[0] for r in want] == off.get("rungs"):
                    continue
                if self.client is not None:
                    ok = True
                    for leg in (off.get("legs") or []):
                        ok = self._cancel_leg(leg.get("oid"), tk) and ok
                    if not ok:
                        continue    # keep the book entry until the
                                    # exchange confirms; requote later
                del self.offers[tk]
                self.exec_stats["decay_requotes"] = (
                    self.exec_stats.get("decay_requotes", 0) + 1)
                continue
            if self.client is not None:
                for leg in off.get("legs") or []:
                    # settled markets kill their own orders, but a
                    # RESIZED position's legs do not die on their own -
                    # a failed cancel here is exactly how the strays
                    # were born, so it is retried rather than ignored
                    self._cancel_leg(leg.get("oid"), tk)
            del self.offers[tk]
        # 8/14: re-attempt cancels that failed on an earlier cycle
        # BEFORE the guard runs, so a transient API error costs one
        # cycle rather than leaving a live leg untracked forever.
        self._retry_orphan_legs()
        # 8/12 OVER-OFFER GUARD (found live: MIA B79.5 held 44 lots with
        # 70 lots of sells resting - 22/22 current ladder PLUS a stale
        # 13/13 ladder from when the position was smaller). Selling more
        # than we hold doesn't just close: it FLIPS us long the outcome
        # we bet against. Any resting order on a market we hold that
        # isn't one of our own known orders is a dead quote - cancel it.
        # Scoped hard: held tickers only, and never a ticker with a live
        # pending entry join (that's heal's turf, and mis-scoping it is
        # exactly how this morning's revert happened).
        if self.client is not None and self.k_resting:
            known = ({leg.get("oid") for o in self.offers.values()
                      for leg in (o.get("legs") or [])}
                     | {d.get("oid") for d in self.dips.values()}
                     | set(self.pending))
            pend_tks = {o["ticker"] for o in self.pending.values()}
            for ro in self.k_resting:
                tk0, roid = ro.get("ticker"), ro.get("oid")
                if not roid or tk0 not in self.bets or tk0 in pend_tks:
                    continue
                if roid in known:
                    continue
                try:
                    self.client.cancel_order(roid)
                except Exception:
                    continue
                self.exec_stats["stale_quotes_canceled"] = (
                    self.exec_stats.get("stale_quotes_canceled", 0) + 1)
                self._log([now(), "STALE-QUOTE-CANCEL", self.mode,
                           ro.get("city", ""), ro.get("strike", ""),
                           ro.get("hl", ""), ro.get("side", ""), "",
                           ro.get("entry", ""), ro.get("count", ""),
                           "", "", f"{tk0}:{roid}"])
        for tk, b in list(self.bets.items()):
            # fractional stubs (<1 contract) can't be quoted - they
            # hold to settlement
            if tk in self.offers or int(float(b.get("count", 0))) < 1:
                continue
            rungs = self._sell_rungs(b, hrs_by_tk.get(tk))
            if not rungs:
                continue        # never quote at or below cost
            n_all = int(float(b["count"]))
            legs = []
            for r_px, r_n in rungs:
                oid = f"of-{self.placed + 1}"
                if self.client is not None:
                    try:
                        resp = self.client.create_order(
                            tk, action="sell", side=b["side"],
                            count=r_n, price_cents=r_px)
                        ro = resp.get("order") or {}
                        oid = (ro.get("order_id") or ro.get("id")
                               or resp.get("order_id")
                               or resp.get("id") or oid)
                    except Exception:
                        continue
                legs.append({"oid": oid, "px": r_px, "count": r_n})
                self.placed += 1
                self.exec_stats["offers_placed"] = (
                    self.exec_stats.get("offers_placed", 0) + 1)
                self._log([now(), "OFFER", self.mode, b["city"],
                           b["strike"], b["hl"], b["side"],
                           round(b.get("pside", 0), 3), r_px, r_n,
                           "", "", oid])
            if legs:
                self.offers[tk] = {"legs": legs, "count": n_all,
                                   "rungs": [r[0] for r in rungs],
                                   "ots": now()}

    def _check_offers(self, resting_ids, fills_by_oid):
        """Book lifted offer legs: realized premium, position reduced or
        closed, cash freed for the next signal. Surviving legs keep
        resting; a resized position requotes via quote_offers."""
        for tk, off in list(self.offers.items()):
            b = self.bets.get(tk)
            if b is None:
                # 8/12: settlement usually kills the orders, but a
                # DESYNC-DROP doesn't - cancel anything still resting
                # so no leg outlives its book entry
                for leg in (off.get("legs") or []):
                    lo = leg.get("oid")
                    if lo in resting_ids:
                        try:
                            self.client.cancel_order(lo)
                        except Exception:
                            pass
                del self.offers[tk]         # settled first: quote is dead
                continue
            legs = off.get("legs") or (
                [{"oid": off.get("oid"), "px": off.get("px"),
                  "count": off.get("count")}] if off.get("oid") else [])
            keep = []
            for leg in legs:
                oid = leg.get("oid")
                if oid in resting_ids:
                    keep.append(leg)
                    continue                # still quoted
                sold = 0.0
                if fills_by_oid is not None:
                    sold = round(min(float(b["count"]),
                                     float(fills_by_oid.get(oid, 0))), 2)
                if sold <= 0.009:
                    continue    # leg canceled externally: requote later
                self._book_sale(tk, b, int(leg.get("px", 0)), sold, oid)
                if tk not in self.bets:
                    break                   # fully sold
            if tk in self.bets and keep and len(keep) == len(legs):
                continue                    # untouched: leave as-is
            if tk in self.bets and keep:
                self.offers[tk]["legs"] = keep
                # count stays the ORIGINAL total so a partial sale
                # triggers the resize/requote pass in quote_offers
            else:
                # 8/12: cancel every leg still resting BEFORE dropping
                # the book entry. Found live: when one ladder rung sold
                # the position out, the surviving 99c rung kept resting
                # unowned on the exchange (4 orphan sells: OKC/ATL/DC/
                # NYC) - unmanaged live orders that also fed the
                # heal-oid/slate-cap pollution below.
                for leg in legs:
                    lo = leg.get("oid")
                    if lo in resting_ids:
                        try:
                            self.client.cancel_order(lo)
                        except Exception:
                            pass
                self.offers.pop(tk, None)

    def _book_sale(self, tk, b, px, sold, oid):
        """One lifted sale: realized P&L, ledgers, sold-autopsy row."""
        fee_share = int(round(b.get("fee", 0) * sold
                              / max(0.01, float(b["count"]))))
        sell_fee = fee_cents(px, sold, taker=False)
        net = (px - b["entry"]) * sold - fee_share - sell_fee
        self.realized_c += net
        self._day_add(net)
        self.day_pnl_c += net
        self.fees_c += sell_fee
        self.exec_stats["offers_sold"] = (
            self.exec_stats.get("offers_sold", 0) + 1)
        self.exec_stats["offers_sold_net_c"] = round(
            self.exec_stats.get("offers_sold_net_c", 0) + net, 1)
        # 8/13: a lift IS a round trip. 8/14: with capital-hours, so the
        # decay ladder can be retuned on net-per-capital-hour - the only
        # objective that makes sense on a capital-constrained book.
        self._turn_add(net, "lift", hold_h=_hold_hours(b.get("ots")))
        self._k_sold_add(tk, px * sold)
        # 8/11 sold-vs-settled autopsy: every sale is graded against the
        # eventual settlement (sold_check), so "were we selling too
        # cheap?" is answered by the ledger
        self.sold_log.append({"tk": tk, "side": b["side"],
                              "entry": b["entry"], "px": px,
                              "count": sold, "pnl": round(net / 100, 2),
                              "ts": now(), "res": None})
        self.sold_log = self.sold_log[-200:]
        self.history.append({"tk": tk, "city": b["city"],
                             "strike": b["strike"],
                             "kind": b.get("kind", "ge"),
                             "cap": b.get("cap"), "hl": b["hl"],
                             "side": b["side"], "trig": b.get("trig"),
                             "pside": round(b.get("pside", 0), 3),
                             "entry": b["entry"], "count": sold,
                             "outcome": None, "exited": True,
                             "sold": True, "exit_px": px,
                             "pnl": round(net / 100, 2), "ts": now(),
                             "ots": b.get("ots", ""), "era": ERA})
        self._log([now(), "SOLD", self.mode, b["city"], b["strike"],
                   b["hl"], b["side"], round(b.get("pside", 0), 3),
                   px, sold, "", round(net / 100, 2), oid])
        if sold >= float(b["count"]) - 0.009:
            del self.bets[tk]
        else:
            b["count"] = round(float(b["count"]) - sold, 2)
            b["fee"] = max(0, b.get("fee", 0) - fee_share)

    def sold_check(self, max_lookups=8):
        """Grade past sales against eventual settlement (8/11): won =
        the sold side would have paid 100. kept = what selling earned
        MINUS what holding would have - positive means the sale beat
        holding; slightly negative is the price of same-day recycling."""
        done = 0
        for row in self.sold_log:
            if row.get("res") is not None or done >= max_lookups:
                continue
            res = fetch_result(row["tk"])
            if res is None:
                continue
            done += 1
            won = (res == row["side"])
            would = (((100 if won else 0) - row["entry"]) * row["count"]
                     - fee_cents(row["entry"], row["count"], taker=True))
            row["res"] = res
            row["would_pnl"] = round(would / 100.0, 2)
            row["kept"] = round(row["pnl"] - row["would_pnl"], 2)

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
            self._day_add(net)
            self.day_pnl_c += net
            if self.client is None:
                self.dry_balance_c += payout * b["count"]
            self.wins += int(won)
            self.losses += int(not won)
            if won and self.client is not None:
                self._recv_add(100 * b["count"])
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
            # 8/14: a position that rode to settlement is a completed
            # round trip too - book it so per_turn stops being a
            # winners-only sample.
            self._turn_add(net, "settle", hold_h=_hold_hours(b.get("ots")))
            del self.bets[tk]

    # ---- momentum stop + trailing exit (taker sells, same rules as paper) ----
    def stop_check(self, quotes=None):
        # stop retired 8/10 (see WSTOP_ON); trails were already off. The
        # peak tracking below is harmless bookkeeping either way.
        if not (WSTOP_ON or TRAIL_ON or NICKEL_TRAIL):
            return 0
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
            if not fade and (smid >= STOP_C or not WSTOP_ON):
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
            self._k_sold_add(tk, bid * cnt)     # k-truth: sale proceeds
            net = (bid - b["entry"]) * cnt - b.get("fee", 0) - exit_fee
            self.realized_c += net
            self._day_add(net)
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
        acct_nav_c = int(balance_c
                         + sum(b["entry"] * b["count"] for b in self.bets.values())
                         + _crypto_cost_c())
        nav_c = int(acct_nav_c * WX_ALLOC)
        if nav_c > 0:
            self.last_nav_c = nav_c    # nickel guardrails read this too
        if not DYN_CAPS:
            return
        if nav_c <= 0:
            return
        # 8/4 small-account boost: 6%/bet below $300 account NAV, 3% after
        bet_pct = BET_PCT_BOOST if acct_nav_c < BOOST_NAV_C else BET_PCT
        self.bet_pct_now = bet_pct
        self.max_bet_c = max(BET_FLOOR_C, int(nav_c * bet_pct))
        # 8/11 earned sizing: the per-bet ceiling for PROVEN buckets only
        self.max_bet_pv_c = max(self.max_bet_c,
                                int(nav_c * PROVEN_BET_PCT))
        self.max_open_c = int(nav_c * OPEN_PCT)
        self.max_day_loss_c = max(HALT_FLOOR_C, int(nav_c * HALT_PCT))
        self.last_nav_c = nav_c      # 8/14: weekly breaker measures on this

    def place(self, mkts=None):
        # 8/13: the halt measures the day's loss FROM THE LAST RESUME,
        # not from midnight. A manual resume (unhalt.txt) hands the book
        # a fresh full-size daily budget without ever touching the P&L
        # ledger - the day's true number stays honest on the tracker.
        if (self.day_pnl_c - float(getattr(self, "halt_base_c", 0))
                <= -self.max_day_loss_c):
            self.halted = True
            return 0
        # 8/14 WEEKLY CIRCUIT BREAKER: the daily halt is 15% of NAV, so a
        # three-day losing streak trips nothing and compounds to ~-45%.
        # This measures the rolling 7-day realized P&L against the same
        # percentage of NAV and stops the book until a human resumes.
        if WEEK_HALT_ON and self._week_loss_exceeded():
            self.week_halted = True
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
                if TAKER_FIRST or smid >= TAKER_MIN_SMID or proven_lane:
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
        _ccap_add, _dcap_add = {}, {}   # 8/12: same-cycle placements
        _done_tks = set(self.settled_tks)
        kpos_tks = {p.get("ticker") for p in (self.k_positions or [])
                    if p.get("ticker") and p.get("ticker") not in _done_tks}
        for (trig, score, mk, side, entry, smid, ekey, exec_kind,
             bid_entry) in cands:
            if ekey in (nk_keys if trig == "nickel" else ev_keys):
                continue
            tk = mk["ticker"]
            # 8/12 Miami lesson: if the EXCHANGE already holds this
            # market but our book hasn't adopted it yet, placing again
            # is how 44 lots stacked into one strike. Wait a cycle for
            # the mirror; pyramid adds (tk in bets) are unaffected.
            if tk in kpos_tks and tk not in self.bets:
                self.exec_stats["sync_wait"] = (
                    self.exec_stats.get("sync_wait", 0) + 1)
                continue
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
                while size > MIN_CONTRACTS and entry * size > pos_cap_c:
                    size -= 1               # 8/10: trim floor = 5, not 1
                # 8/10: the 5-lot floor overrides the single-position
                # cap (same semantics as the level lanes since 8/7);
                # the LANE aggregate cap still binds and skips.
                if lane_cost + entry * size > int(nav_c * NICKEL_LANE_PCT):
                    continue
            else:
                _pv = False          # probe mode: never the earned cap
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
                    # 8/11 earned sizing: a PROVEN bucket (half-Kelly
                    # lane) may size Kelly up to PROVEN_BET_PCT of
                    # bankroll; unproven lanes keep the base fraction
                    _pv = (self._kelly_frac(bstats, trig, bid_entry)
                           > KELLY_BASE)
                    _frac = PROVEN_BET_PCT if _pv else dp.PER_BET_CAP
                    size = int(min(f_star, _frac) * bankroll // entry)
                    if size < 1 and exec_kind == "taker":
                        # edge too thin to pay the toll: rest a maker join
                        entry, exec_kind = bid_entry, "maker"
                        size = int(min(f_star, _frac)
                                   * bankroll // entry)
                    if size < 1:
                        continue
                size = max(size, MIN_CONTRACTS)   # fee-rounding floor
                # ...and trims to the matching dollar ceiling
                _cap = (getattr(self, "max_bet_pv_c", 0) or self.max_bet_c
                        ) if _pv else self.max_bet_c
                while size > MIN_CONTRACTS and entry * size > _cap:
                    size -= 1                      # trim ABOVE the floor only
            # 8/11 concentration caps: correlation is the book's #1 tail
            # risk. A new entry may not push one CITY past CITY_CAP_PCT
            # of NAV or one settlement DATE past SLATE_CAP_PCT.
            nav_cc = getattr(self, "last_nav_c", 0)
            if nav_cc:
                # 8/12 rework (over-refusal autopsy): concentration is
                # FILLED risk only. Unfilled maker joins churn all day
                # and were double-reserving the slate - and with every
                # weather market sharing ONE settlement date, the slate
                # cap was acting as a hard 40% global cap fed by phantom
                # commitment (the 60% open cap still bounds
                # bets+pending). Same-cycle placements DO count (burst
                # guard), and an oversize candidate TRIMS to the room
                # left (>= the 5-lot floor) like every other cap here
                # instead of refusing outright.
                # 8/12 Miami hardening: exposure = max(book, exchange)
                c0d0 = self._conc_cost_c(mk["city"], mk.get("date", ""))
                c_cost = c0d0[0] + _ccap_add.get(mk["city"], 0)
                d_cost = c0d0[1] + _dcap_add.get(mk.get("date", ""), 0)
                room_c = int(nav_cc * CITY_CAP_PCT) - c_cost
                room_d = int(nav_cc * SLATE_CAP_PCT) - d_cost
                room = min(room_c, room_d)
                while size > MIN_CONTRACTS and entry * size > room:
                    size -= 1
                if entry * size > room:
                    kind0 = "city" if room_c <= room_d else "slate"
                    self.exec_stats[kind0 + "_capped"] = (
                        self.exec_stats.get(kind0 + "_capped", 0) + 1)
                    self._cap_refused(kind0, tk, entry, size,
                                      c_cost, d_cost, nav_cc)
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
            _ccap_add[mk["city"]] = (_ccap_add.get(mk["city"], 0)
                                     + entry * size)
            _dcap_add[mk.get("date", "")] = (
                _dcap_add.get(mk.get("date", ""), 0) + entry * size)
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
        self.quote_dips(mkts, balance_c, bstats)   # 8/11 bid side
        return placed

    # ---- 8/11 standing bid side --------------------------------------
    def quote_dips(self, mkts, balance_c, bstats):
        """Rest a maker BUY a few cents under the market on CONTEXT
        markets (held now, or sold this era): the stop autopsy proved
        intraday dips on favorites are noise that recovers, so wobbles
        fill us at wholesale-minus and the offer engine retails them.
        Never below the 80c floor; every cap applies at placement."""
        if not DIP_ON or not mkts:
            return
        nav_c = getattr(self, "last_nav_c", 0)
        if not nav_c:
            return
        floor_px = max(80, ENTRY_FLOOR)
        pend_tks = {o["ticker"] for o in self.pending.values()}
        by_tk = {m["ticker"]: m for m in mkts}

        def side_quotes(mk):
            yb, ya = mk["yes_bid"], mk["yes_ask"]
            if not yb or not ya:
                return None
            mid = (yb + ya) / 2.0
            if mid >= 80:
                return "yes", yb, mid
            if mid <= 20:
                return "no", 100 - ya, 100 - mid
            return None                     # not a favorite: no context bid

        # refresh pass: market moved -> cancel, replaced below
        for tk, d in list(self.dips.items()):
            mk = by_tk.get(tk)
            if mk is None:
                continue                    # not scanned this cycle: leave
            sq = side_quotes(mk)
            tgt = None
            if sq and sq[0] == d.get("side"):
                t = max(floor_px, int(sq[1]) - DIP_DISCOUNT_C)
                if t <= sq[1] - DIP_MIN_ROOM_C:
                    tgt = t
            if tgt is not None and abs(tgt - d.get("px", 0)) < DIP_REFRESH_C:
                continue                    # close enough: keep resting
            if self.client is not None:
                try:
                    self.client.cancel_order(d.get("oid"))
                except Exception:
                    continue
            del self.dips[tk]
        for mk in mkts:
            tk = mk["ticker"]
            if tk in self.dips or tk in pend_tks:
                continue
            # 8/13: context-only was the training-wheels rule. Cheap
            # inventory is what feeds the offer engine, so every scanned
            # favorite is fair game now (caps still bound everything).
            if DIP_CONTEXT_ONLY and tk not in self.bets and tk not in (
                    self.k_sold or {}):
                continue
            sq = side_quotes(mk)
            if not sq:
                continue
            side, sbid, smid = sq
            b0 = self.bets.get(tk)
            if b0 is not None and b0.get("side") != side:
                continue                    # never average an opposite side
            px = max(floor_px, int(sbid) - DIP_DISCOUNT_C)
            if px > sbid - DIP_MIN_ROOM_C:
                continue                    # no room under the market
            if self._bucket_blocked(bstats, "dip", px):
                continue                    # the dip lane lost this band
            size = MIN_CONTRACTS
            cost = px * size
            dip_tot = sum(d["entry"] * d["count"] for d in self.dips.values())
            if dip_tot + cost > int(nav_c * DIP_MAX_PCT):
                continue
            # 8/12: FILLED risk (max of book and exchange views, see
            # _conc_cost_c) + this lane's own resting bids - pending
            # maker joins no longer double-reserve the caps
            c_cost, d_cost = self._conc_cost_c(mk["city"],
                                               mk.get("date", ""))
            for x in self.dips.values():
                c0 = x.get("entry", 0) * x.get("count", 0)
                if x.get("city") == mk["city"]:
                    c_cost += c0
                if x.get("date", "") == mk.get("date", ""):
                    d_cost += c0
            if c_cost + cost > int(nav_c * CITY_CAP_PCT):
                continue
            if d_cost + cost > int(nav_c * SLATE_CAP_PCT):
                continue
            if self.open_cost_c() + dip_tot + cost > self.max_open_c:
                continue
            if balance_c - cost < self.reserve_c:
                continue
            oid = f"dp-{self.placed + 1}"
            if self.client is not None:
                try:
                    resp = self.client.create_order(
                        tk, action="buy", side=side, count=size,
                        price_cents=px)
                    ro = resp.get("order") or {}
                    oid = (ro.get("order_id") or ro.get("id")
                           or resp.get("order_id") or resp.get("id") or oid)
                except Exception:
                    continue
            self.dips[tk] = {"oid": oid, "px": px, "entry": px,
                             "count": size, "side": side,
                             "pside": round(smid / 100.0, 3),
                             "city": mk["city"], "strike": mk["strike"],
                             "kind": mk.get("kind", "ge"),
                             "cap": mk.get("cap"),
                             "hl": ("lo" if mk["is_low"] else "hi"),
                             "date": mk.get("date", ""), "ots": now()}
            self.placed += 1
            self.exec_stats["dips_placed"] = (
                self.exec_stats.get("dips_placed", 0) + 1)
            self._log([now(), "DIPBID", self.mode, mk["city"], mk["strike"],
                       ("lo" if mk["is_low"] else "hi"), side,
                       round(smid / 100.0, 3), px, size, "", "", oid])

    def _check_dips(self, resting_ids, fills_by_oid):
        """Book dip-bid fills: wholesale inventory for the offer side."""
        for tk, d in list(self.dips.items()):
            oid = d.get("oid")
            if oid in resting_ids:
                continue
            filled = 0.0
            if fills_by_oid is not None:
                filled = round(min(float(d["count"]),
                                   float(fills_by_oid.get(oid, 0))), 2)
            del self.dips[tk]               # gone; requoted next cycle
            if filled <= 0.009:
                continue                    # canceled externally
            fee = fee_cents(d["px"], filled, taker=False)
            self.fees_c += fee
            self.exec_stats["dip_fills"] = (
                self.exec_stats.get("dip_fills", 0) + 1)
            self.exec_stats["dip_fill_cost_c"] = round(
                self.exec_stats.get("dip_fill_cost_c", 0)
                + d["px"] * filled, 1)
            if tk in self.bets:
                self._merge_fill(tk, d["px"], filled, fee)
            else:
                self.bets[tk] = {"side": d["side"], "entry": d["px"],
                                 "count": filled, "fee": fee,
                                 "pside": d.get("pside", 0),
                                 "city": d.get("city"),
                                 "strike": d.get("strike"),
                                 "kind": d.get("kind", "ge"),
                                 "cap": d.get("cap"), "hl": d.get("hl"),
                                 "date": d.get("date", ""),
                                 "trig": "dip", "peak": d["px"],
                                 "ots": now(), "era": ERA}
            self._log([now(), "DIPFILL", self.mode, d.get("city"),
                       d.get("strike"), d.get("hl"), d["side"],
                       round(d.get("pside", 0), 3), d["px"], filled,
                       "", "", oid])

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
        rem = o["count"]
        if self.client is not None:
            # 8/12: cancel-and-book, then chase only the REMAINDER.
            # The old path re-ordered the FULL count after zeroing
            # filled_seen - a partially-filled join got its filled lots
            # AGAIN on every chase (same fills-go-invisible family as
            # the Miami stack, from the other direction).
            if not self._cancel_pending(oid, o):
                return False
            rem = round(float(o["count"])
                        - float(o.get("filled_seen", 0)), 2)
            if rem < 1:
                del self.pending[oid]       # the chase filled it whole
                return False
            rem = int(rem)
            try:
                resp = self.client.create_order(tk, action="buy", side=o["side"],
                                                count=rem, price_cents=join)
                ro = resp.get("order") or {}
                new_oid = (ro.get("order_id") or ro.get("id")
                           or resp.get("order_id") or resp.get("id") or new_oid)
            except Exception:
                self._log_miss(o, rem, why="requote_rejected")
                del self.pending[oid]       # canceled but not replaced
                return False
        if self.client is None:
            self.dry_balance_c -= (join - o["entry"]) * o["count"]
        o = self.pending.pop(oid)
        o.update({"entry": join, "count": rem,
                  "requotes": int(o.get("requotes", 0)) + 1,
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
        self.sold_check()            # grade lifted offers vs settlement
        self.stop_check()
        # 8/13 velocity build: one market scan feeds entries, the
        # pre-close flatten and the time-decay ladder. Flatten runs
        # BEFORE quoting so a position on its way out isn't re-hung.
        try:
            mkts = we.find_temp_markets(max_days=1)
        except Exception:
            mkts = None
        self.place(mkts)
        self.flatten(mkts)
        self.quote_offers(mkts)      # 8/10: the offer side of the book
        try:
            bal = self.balance_c()
        except Exception:
            bal = None
        if bal is not None and self.day_nav0_c is None:
            self.day_nav0_c = self._day_anchor_c(bal)
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
        # 8/6: hourly crypto events live only ~60 min, so the crypto book
        # re-scans every CRYPTO_SUB_S during the drift book's 10-min nap
        # (direct series fetch = ~12 cheap calls, not the global sweep).
        slept, cyc = 0, _cycle_s()
        while slept < cyc:
            nap = min(CRYPTO_SUB_S, cyc - slept)
            time.sleep(nap)
            slept += nap
            if _code_changed():
                print(f"[{now()}] new build on disk - restarting "
                      f"(systemd revives)")
                return 0
            if cl is not None and slept < cyc:
                try:
                    cl.step()
                except Exception as e:
                    print(f"[{now()}] crypto cycle error: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
