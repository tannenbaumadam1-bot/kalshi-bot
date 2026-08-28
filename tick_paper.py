"""TICK BOOK (8/25) - paper trading the 15-minute commodity windows.

Adam's thesis, in his words: "most of the money in those markets is made
on the structural side, not by predicting gold's direction over 15
minutes." Right. And the structural edge he named is the one this shop
already lives on: BUY NEAR-CERTAINTY CHEAP, let the clock deliver it.
Weather works because by afternoon the day's high is mostly realized
while the book still quotes doubt. A 15-minute gold window is the same
trade on a 900-second clock: as the window drains, the distance between
spot and strike stops being a coin flip and starts being arithmetic.

PHASE 0: ZERO DOLLARS AT RISK. There is no API key here, no client, no
code path to any order endpoint. This file can only read.

WHAT THE VERIFICATION FOUND (8/25, before a line of this was written -
both findings changed the design):

 1. THE COMMODITY WINDOWS DO NOT SETTLE ON A 60-SECOND AVERAGE.
    KXGOLD15M / KXWTI15M / KXSILVER15M settle on the CLOSE OF THE
    1-MINUTE PYTH CANDLESTICK at the boundary - a point in time, not an
    average. The 60-second-average structure (where the outcome
    progressively locks in as the averaging window fills, and the last
    seconds are pure arithmetic) is real, but it lives in the CRYPTO
    15-minute series (KXBTC15M et al, CF Benchmarks BRTI). So the
    "partial average" edge is NOT available on gold, and any bot that
    assumed it would have been modelling a settlement rule that does not
    exist. Different series, different feed, different math - exactly
    the thing Adam said to check first.

 2. THE EDGE THAT IS ACTUALLY THERE ON COMMODITIES is distance-vs-clock.
    The strike is frozen at the window's open (the 2:15 candle close);
    the question is only whether spot is >= that number at 2:30. So the
    honest probability is P(a random walk of known volatility, currently
    d dollars from the line, with t seconds left, ends on the right
    side). That is computable every cycle. Retail reads a price ticker
    and a green/red arrow. THAT is the mispricing to measure.

REFERENCE FEED. Kalshi settles gold on Pyth's Metal.Index.GOLD/USD,
which is not enumerable on the public Hermes feed list. We therefore
track the closest PUBLIC Pyth feed as a PROXY (Metal.XAU/USD, live and
within ~0.03% of Kalshi's posted strike at build time) and - this is the
part that matters - we RECORD THE PROXY'S ERROR against Kalshi's own
settled result on every window. If the proxy cannot predict settlement,
the calibration table says so out loud and the lane dies on its own
evidence instead of on a hunch.

FILL REALISM, inherited from the phantom book because it is the only
honest way we know: we post no orders, so a paper fill requires a REAL
print to trade THROUGH our resting price (STRICT). Prints AT our price
are counted separately (LOOSE) and never believed - queue position is
not ours to assume.

FEES follow the published schedule and the shape drives everything:
ceil(rate x C x P x (1-P)) peaks at the money and vanishes at the
extremes. A round trip at 50c costs ~3.5% of notional; at 95c it costs
~0.35%. This is why both lanes here only ever buy the CHEAP-CERTAIN
side, and why nothing in this file trades at the money.

TWO LANES, GRADED SEPARATELY (one thesis each, so the tape can convict
one without the other):
  ENDGAME - late in the window, model says >= ENDGAME_P but the book
            still offers it below the model by >= EDGE_C. Buy certainty
            at a discount. This is the weather trade, 900-second clock.
  TAIL    - the longshot-bias harvest. Retail overpays for the
            far-from-strike side; we take the other end when the model
            says the far side is nearly dead. Short gamma by nature, so
            it is sized small and watched for the headline that runs it
            over.

KILL SWITCH: PAPER_TICK=0.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import time
import urllib.request

try:
    from recorder import Recorder
except Exception:                                    # pragma: no cover
    Recorder = None

KALSHI = os.environ.get("TICK_KALSHI",
                        "https://api.elections.kalshi.com/trade-api/v2")
HERMES = os.environ.get("TICK_HERMES", "https://hermes.pyth.network")
STATE = os.environ.get("TICK_STATE", os.path.join("logs", "tick_state.json"))
ERA = os.environ.get("TICK_ERA", "tick2")

# --- the surface -------------------------------------------------------
# series -> (public Pyth proxy feed id, human label). Only series whose
# settlement feed we can actually approximate are listed; adding one
# without a live proxy would produce a model with nothing behind it.
SERIES = {
    "KXGOLD15M": ("765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f"
                  "2f8ba4ee34bb2", "gold"),
    "KXSILVER15M": ("f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f01"
                    "2337cabe93871e", "silver"),
    "KXWTI15M": ("925ca92ff005ae943c158e3563f59698ce7e75c5a8c8dd43303a0"
                 "a154887b3e6", "wti"),
}
# ---------------------------------------------------------------------
# CRYPTO 15-MINUTE WINDOWS (8/27) - the structurally best target on the
# exchange, and the one Adam described from the very beginning.
#
# THREE THINGS MAKE THESE STRICTLY BETTER THAN THE METALS:
# 1. THEY SETTLE ON A 60-SECOND AVERAGE, not a point-in-time candle
#    close. "the simple average of the sixty seconds of CF Benchmarks'
#    BRTI before 3:15 PM". That is the progressive lock-in Adam named on
#    day one: as the final minute elapses, part of the settlement value
#    is ALREADY DETERMINED, and the true probability decouples from
#    where spot happens to be sitting. It is arithmetic, not forecasting
#    - the one shape this shop has repeatedly proven it can harvest.
# 2. THE DATA IS FREE, FOREVER. Coinbase and Kraken serve BTC/ETH/SOL
#    spot with no key, no tier and no 13-day trial - unlike the metals,
#    where the settlement feed sits behind a paid plan and even our
#    proxy expires. A lane that cannot lose its data is worth more than
#    a lane that might.
# 3. THEY TRADE 24/7. Metals windows stop overnight and at weekends;
#    crypto never does, which roughly triples the sample rate per day.
# BTC's book is also the deepest of any 15-minute market (~8,200
# contracts at touch when measured).
# 8/28 EXPANSION. Adam wants more trades. The measured edge (the fav
# lane) fires on roughly one window in four to eight, so the honest way
# to get more of them is MORE INDEPENDENT MARKETS AT THE SAME
# SELECTIVITY - never a lower bar on the same few. Loosening the bar is
# how the scalping backtest lost money in all 64 configurations; adding
# uncorrelated windows just multiplies the chances to be selective.
# Every pair below has a free Coinbase spot feed (verified 8/28), so
# this costs nothing and cannot expire.
#   3 series x 4 windows/hr  ->  9 series x 4 windows/hr
CRYPTO = {
    "KXBTC15M": ("BTC-USD", "btc"),
    "KXETH15M": ("ETH-USD", "eth"),
    "KXSOL15M": ("SOL-USD", "sol"),
    "KXXRP15M": ("XRP-USD", "xrp"),
    "KXDOGE15M": ("DOGE-USD", "doge"),
    "KXZEC15M": ("ZEC-USD", "zec"),
    "KXNEAR15M": ("NEAR-USD", "near"),
    "KXBNB15M": ("BNB-USD", "bnb"),
    "KXHYPE15M": ("HYPE-USD", "hype"),
}
CRYPTO = {k: v for k, v in CRYPTO.items()
          if k in os.environ.get("TICK_CRYPTO", ",".join(CRYPTO))}
# settlement is the mean of the sixty one-second prints before the
# boundary, so the model needs per-second resolution in that last minute
AVG_WINDOW_S = int(os.environ.get("TICK_AVG_WINDOW", "60"))
BURST_AT_S = int(os.environ.get("TICK_BURST_AT", "75"))    # sample fast
COINBASE = os.environ.get("TICK_COINBASE", "https://api.coinbase.com")

SERIES = {k: v for k, v in SERIES.items()
          if k in os.environ.get("TICK_SERIES", ",".join(SERIES))}

TAKER_RATE = 0.07
MAKER_RATE = float(os.environ.get("TICK_MAKER_RATE", "0.0175"))

# ---------------------------------------------------------------------
# AGGRESSIVE REGIME (Adam 8/26: "be insanely aggressive on the paper
# book, you can trade in and out of the market over and over to recycle
# capital"). Set deliberately at the tick2 reset, because the project
# rule is that a ledger cannot be compared across a constraint change -
# so the constraints get chosen ONCE, at the start of an era, and then
# left alone.
#
# The case for aggression here is not bravado, it is SAMPLE RATE. The
# gate is 200 settled windows and the data trial is 13 days. A timid
# book measures nothing in that window. Paper carries no dollar risk,
# so the only real cost of trading more is that we learn faster, and
# widening the entry bar fills the calibration table across the WHOLE
# probability range instead of only the 90%+ bucket - which makes the
# table strictly more informative about whether the model can be
# trusted at all.
#
# What aggression must NOT do is re-open the accounting hole that
# manufactured +$304 this morning. Every cap below is still ENFORCED AT
# FILL TIME; they are simply larger numbers now.
BOOK_CAPITAL_C = int(os.environ.get("TICK_CAPITAL", "100000"))  # $1,000
SIZE = int(os.environ.get("TICK_SIZE", "10"))         # contracts per entry
MAX_POS = int(os.environ.get("TICK_MAX_POS", "50"))   # per market
# Only ever buy the cheap-certain side. Above MAX_PX_C there is no room
# left to pay for the fee; below MIN_PX_C we are buying a lottery ticket,
# which is the side we intend to SELL to.
# Wide band: the old 55-97 floor could only ever express one opinion
# ("buy the favourite"). The fee curve, not a hard floor, is what makes
# mid-prices expensive - and the edge test below is now NET OF FEES, so
# the arithmetic refuses bad prices on its own merits instead of a
# blanket ban. 15-97 keeps us off the 1-14c lottery tickets, where a
# one-tick move is a 100% swing and our model has no resolution.
MIN_PX_C = int(os.environ.get("TICK_MIN_PX", "15"))
MAX_PX_C = int(os.environ.get("TICK_MAX_PX", "97"))
EDGE_C = float(os.environ.get("TICK_EDGE", "2"))      # NET of fees, cents
ENDGAME_S = int(os.environ.get("TICK_ENDGAME_S", "900"))   # whole window
ENDGAME_P = float(os.environ.get("TICK_ENDGAME_P", "0.75"))
TAIL_P = float(os.environ.get("TICK_TAIL_P", "0.90"))      # tail lane bar
TAIL_MAX_S = int(os.environ.get("TICK_TAIL_MAX_S", "900"))
VOL_WINDOW = int(os.environ.get("TICK_VOL_WINDOW", "180"))  # ticks kept
MIN_VOL_N = int(os.environ.get("TICK_MIN_VOL_N", "12"))
# SMALL-SAMPLE / JUMP HAIRCUT. Measured on the first live tape (8/25):
# gold moved 3.28 in 258s against a naive 1-sigma of 1.09 - a 3-sigma
# move inside four minutes, on the very first window we watched. A
# short-window realized-vol estimate systematically understates a market
# that trends and jumps, and an overconfident model is far more
# dangerous here than a shy one: it manufactures fake "certainty at a
# discount" and then buys it. So sigma is the MAX of a short and a long
# lookback, inflated by VOL_MULT, and the resulting probability is
# capped - we never claim more certainty than the tape has earned.
VOL_MULT = float(os.environ.get("TICK_VOL_MULT", "1.6"))
CONF_CAP = float(os.environ.get("TICK_CONF_CAP", "0.98"))
SHORT_N = int(os.environ.get("TICK_SHORT_N", "20"))
# PROXY LIVENESS GATE (added 8/25 hours after launch, on live evidence).
# The WTI proxy (Commodities.USOILSPOT) printed 82.2930 -> 82.2933 over
# 40 seconds while gold moved two full points on the same clock. A proxy
# that barely moves parks our measured distance at ~0 forever, so the
# model says "coin flip" while the market - reading the REAL settlement
# feed - said 94%. That produced a 43-cent "edge" that was pure
# measurement error, and taking it would have meant buying the wrong
# side of a market that was right. A series whose proxy moves less than
# MIN_LIVE_BP of its own price across the tape is declared dead and
# quoted by nobody until its feed is fixed.
MIN_LIVE_BP = float(os.environ.get("TICK_MIN_LIVE_BP", "1.0"))   # bp
LIVE_MIN_N = int(os.environ.get("TICK_LIVE_MIN_N", "15"))
# EXIT LANE (Adam 8/25: "we can trade in and out of it as it moves
# towards settlement"). The live weather book's ledger is unambiguous on
# this - lifts +$88.82 over 387 turns, settles -$91.05 over 168. Getting
# paid early and recycling the collateral beats riding a position into
# a binary outcome. EXIT_EDGE_C is how far the market must come back
# toward (or past) our model before we take the money.
EXIT_EDGE_C = float(os.environ.get("TICK_EXIT_EDGE", "1"))
EXIT_MIN_HOLD_S = int(os.environ.get("TICK_EXIT_MIN_HOLD", "5"))
# THESIS-BROKEN STOP. Distinct from the live book's flatten leak, which
# pays the spread to abandon trades that are still RIGHT (-0.097/ch, the
# worst number in that ledger). This fires only when the model has
# crossed to the other side of the coin - we bought a 75% and it is now
# a 40% - which is not impatience, it is the reason for holding having
# evaporated. Cutting there is what frees collateral to be redeployed
# inside the same window.
STOP_P = float(os.environ.get("TICK_STOP_P", "0.45"))
# ---------------------------------------------------------------------
# THE FAVOURITE LANE (8/28) - the trade Adam was making by hand, which
# this bot could not find because it was asking the wrong question.
#
# Every lane until now demanded that our MODEL BEAT THE MARKET by more
# than the fee. The market prices distance-vs-clock about as well as we
# do, so that bar was almost never cleared and the book barely traded.
#
# But beating the market on probability is not the only way to be paid.
# Measured over 85 real settled windows across BTC/ETH/SOL/gold: buying
# the FAVOURITE at the market's own price inside the final minute, in
# the 80-95c band, returns
#       +9.2c/trade over the last 60s   (10 trades, 10 wins)
#       +8.7c/trade over the last 30s   ( 8 trades,  8 wins)
#       +4.7c/trade over the last 150s  (21 trades, 20 wins)
# and is positive in 14 of 16 (window x band) configurations tested.
#
# The two LOSING configurations are both the 70-90c band (-2.7c). That
# is the whole finding in one line: at 70-90c you are buying genuine
# uncertainty, at 80-95c you are buying near-certainty the book has not
# finished repricing. Retail sells the almost-sure side too cheaply -
# the favourite-longshot bias, which is the same effect the live
# weather book has harvested for two months.
#
# So the model stops being the ENTRY TRIGGER and becomes a SAFETY
# CHECK: take the favourite unless our own arithmetic actively
# contradicts the market. That inversion is the fix.
#
# SAMPLE CAUTION: 10-21 trades per configuration. Positive everywhere it
# should be and negative exactly where theory says it should be, which
# is encouraging - but this is paper, and the shadow table is what will
# confirm or kill it.
# 8/28 RETUNE ON OUT-OF-SAMPLE DATA - and a correction of my own error.
#
# The original 80-95c band was chosen on 85 windows from FOUR markets.
# I then gathered 132 windows across ELEVEN, which gave seven markets
# that had played no part in choosing anything. Tested there:
#       85-95c  ->  -0.5c   (the band I had shipped)
#       80-90c  ->  +2.2c
#       75-85c  ->  +5.0c
#       70-80c  ->  +7.8c
# The band I picked was the WORST of the four out of sample. That is
# textbook overfitting: I tuned a boundary on the same data that
# suggested it, and called the resulting cliff a discovery.
#
# The honest optimum, positive on ALL THREE splits (7 fresh markets /
# the original 4 / all 11):
#       70-88c, last 240s  ->  +6.2c / +2.8c / +7.8c
# versus what was live (80-95c, 180s) -> +3.6c on the full set.
#
# WHY LOWER IS BETTER, and it is not subtle: the break-even win rate is
# just the entry price. At 75c you must win 75% and you win ~88% - an
# 13-point cushion. At 92c you must win 92% and you win ~95% - a 3-point
# cushion. The hit rate rises with price, but nowhere near fast enough
# to keep up with what you are risking. The cheap favourite is the
# better trade, which is the exact opposite of where I first looked.
FAV_MIN_C = float(os.environ.get("TICK_FAV_MIN", "70"))
FAV_MAX_C = float(os.environ.get("TICK_FAV_MAX", "88"))
# 8/28 RETUNE, on window-level samples. Widening the CLOCK adds volume
# without costing expectancy - 90s gave 35 trades at +4.7c, 120s gave 40
# at +7.7c, 180s gives 50 at +5.8c. Chosen for the TRADE COUNT, not the
# backtest EV: at n=35-50 those EV differences are noise, and the thing
# we actually need right now is a bigger honest sample.
# Widening the PRICE BAND does the opposite and was rejected: 78-95c
# turns NEGATIVE (-2.6c) and 75-92c over the whole window is -5.4c. The
# 80c floor is a genuine cliff, not a preference.
# 240s beat every other clock on both independent splits; 480s turns
# negative on both, so the window is real and not a knob to widen.
FAV_AT_S = int(os.environ.get("TICK_FAV_AT", "240"))
FAV_VETO_P = float(os.environ.get("TICK_FAV_VETO", "0.60"))  # model veto
MAX_TRIPS = int(os.environ.get("TICK_MAX_TRIPS", "6"))   # per window
# TRUE ARB: if YES ask + NO ask < 100 minus both fees, buying both sides
# locks a profit no matter how it settles. Almost certainly absent on a
# liquid book - which is exactly why it is worth counting rather than
# assuming.
ARB_MIN_C = float(os.environ.get("TICK_ARB_MIN", "1"))
# BASIS CORRECTION - the fix that turns a proxy into an instrument.
# The strike is set from KALSHI'S settlement feed at the window's open;
# our distance is measured against OUR proxy feed. Any constant offset
# between the two lands entirely in the distance. Measured live 8/25:
#   gold   +2.4520 (+5.3bp)  - tolerable, a window moves ~5-7
#   silver -0.0105 (-1.5bp)  - tolerable
#   WTI    -0.0754 (-9.2bp)  - FATAL: a whole WTI window moves ~0.05,
#                              so the measurement error exceeded the
#                              signal and the model read coin-flip
#                              while the market correctly read 27%.
# Self-calibrating fix: every new window hands us one free observation,
# because at the moment a window opens the settlement feed EQUALS the
# new strike. So basis = (our proxy at that instant) - strike. We keep a
# rolling median and subtract it before computing any distance. No lane
# may quote a series until it has measured its own offset MIN_BASIS_N
# times - trading before you know your instrument's zero error is how
# the WTI row happened.
BASIS_N = int(os.environ.get("TICK_BASIS_N", "8"))
# Two anchors is enough to START. The offset keeps refining as every
# new window adds one, and trading with a two-sample offset is far
# closer to the truth than refusing to trade at all - the alternative
# is not "more accuracy", it is no data.
MIN_BASIS_N = int(os.environ.get("TICK_MIN_BASIS_N", "2"))
BASIS_WINDOW_S = int(os.environ.get("TICK_BASIS_WINDOW", "90"))
# PAIR / LEGGED-ARB TRACKER (Adam 8/25: "over 15 minutes you can buy yes
# and no for a guaranteed arb continuously").
#
# The arithmetic is exact and the insight is right: buying YES at its low
# and NO at its low costs min_yes + (100 - max_yes) = 100 MINUS the price
# range. So the locked profit IS the intra-window range. Measured over 59
# real settled windows the median range is 59-79c, which looks like free
# money - and that is precisely why it has to be simulated rather than
# admired.
#
# BACKTEST VERDICT (54-60 windows, 8/25-8/26, STRICT fills - a print must
# trade THROUGH the bid, the standard phantom taught us to use):
#     bid 44c  both legs 74%   EV -4.26c/window
#     bid 45c  both legs 78%   EV -4.00c/window
#     bid 46c  both legs 78%   EV -5.78c/window
#     bid 48c  both legs 89%   EV -3.67c/window
#   cutting the naked leg early helps but never rescues it (-4.00 ->
#   -2.79 at 45c/T-180s). NEGATIVE AT EVERY LEVEL AND EVERY POLICY.
#
# WHY, structurally: the leg that fills ALONE fills precisely because the
# price ran away from it and did not come back - and "ran away and did
# not come back" is the definition of that leg losing. In the sample this
# correlation was perfect: 12 of 12 one-legged windows at 45c lost the
# leg outright. So the trade is 78% x +6c against 22% x -46c.
#
# Bid deeper and the lock grows but both-fills collapse; bid shallower and
# both-fills approach certainty while the lock shrinks toward the fee. The
# market prices that trade-off almost exactly right - which is what an
# efficient book looks like from the inside.
#
# BREAKEVEN, so a regime change can be recognised instead of argued about:
#   P(both) / P(one) = (L + fee) / (100 - 2L - 2*fee)
#   at 45c that needs both-legs ~88.5% (we see 78%); at 48c ~96% (89%).
# This tracker therefore does NOT trade. It measures the completion rate
# every window and publishes it against the breakeven line, so if the
# market ever turns choppy enough to clear the bar, we find out from the
# tape rather than from a hunch.
PAIR_L = float(os.environ.get("TICK_PAIR_L", "45"))
# SHADOW CALIBRATION (8/27) - the highest-value instrument in this file.
#
# The calibration table only filled when we TRADED, which is both slow
# and biased: we trade where we think we have edge, so the table
# measured the model exactly where it was most confident and nowhere
# else. At ~3 settles a day the 200-window clock was a month away.
#
# But the model can be scored on EVERY window whether we trade it or
# not: record its probability at a fixed point in the window, then grade
# it against Kalshi's settled result. That is ~192 observations a day
# across gold and silver, unbiased by our own selection, and it fills
# the clock in about a day. It also costs nothing and risks nothing.
#
# This is the number that decides whether the model has any edge at all,
# and therefore whether this lane is ever worth paying for data.
SHADOW_AT_S = int(os.environ.get("TICK_SHADOW_AT", "120"))  # T-minus 2 min
CLOCK_GOAL = int(os.environ.get("TICK_CLOCK", "200"))       # settle gate
UA = {"User-Agent": "kalshibot-tick/1.0"}
# Pyth closed public Hermes access (auth required from 2026-07-31); every
# request now 401s without a key. Set PYTH_API_KEY to restore the model
# lanes. Until then the feed-independent work continues - see feed_state.
_KEY_JUNK = re.compile(r"\x1b\[[0-9;]*[a-zA-Z~]|[\x00-\x1f\x7f]")


def _clean_key(raw):
    """Scrub a key of terminal paste artefacts.

    The DigitalOcean web console wraps pasted text in bracketed-paste
    markers (ESC[200~ ... ESC[201~). Those bytes land in the file and are
    then sent inside an Authorization header, which fails with a 401 that
    looks exactly like a bad key - a genuinely nasty hour of debugging
    for anyone who hits it. Strip escape sequences and control bytes, and
    keep only the characters an API key can actually contain, so paste
    works as well as typing."""
    if not raw:
        return ""
    k = _KEY_JUNK.sub("", str(raw)).strip()
    for marker in ("200~", "201~"):
        k = k.replace(marker, "")
    k = "".join(c for c in k if c.isalnum() or c in "-_.=+/")
    return k.strip()


def _migrate_rows(rows):
    """Bring pre-ledger rows up to the auditable schema.

    Rows written before 8/28 carry only px/exit_px/fee, so a ledger that
    renders cost, gross and net would show dashes for them - a ledger
    with holes is not a ledger. A settlement is an exit at 100c (won) or
    0c (lost), which is exactly how the new schema already describes it,
    so the conversion is exact rather than a guess."""
    out, run = [], 0.0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        r = dict(r)
        if "entry" not in r:
            n = float(r.get("n") or 0)
            entry = float(r.get("px") or 0)
            ex = r.get("exit_px")
            ex = (float(ex) if ex is not None
                  else (100.0 if r.get("won") else 0.0))
            r["entry"] = round(entry, 2)
            r["exit"] = round(ex, 2)
            r["cost"] = round(entry * n / 100.0, 2)
            r["gross"] = round((ex - entry) * n / 100.0, 2)
            r["fees"] = round(float(r.get("fee") or 0), 2)
            r["how"] = ("STOPPED" if r.get("stop")
                        else ("SOLD" if r.get("exit_px") is not None
                              else ("WON" if r.get("won") else "LOST")))
            r.setdefault("avg", False)
        run = round(run + float(r.get("pnl") or 0), 2)
        r["run"] = run
        out.append(r)
    return out


def _mark_of(p):
    """Mid of the side we hold, in cents, or None if unmarked."""
    mid = (getattr(p, "_mid", None) if not isinstance(p, dict)
           else p.get("_mid"))
    if mid is None:
        return None
    return round(mid if p["side"] == "yes" else 100.0 - mid, 2)


def _unreal_of(p):
    """Mark-to-market P&L on an open position, in dollars."""
    mk = _mark_of(p)
    if mk is None:
        return None
    n = float(p.get("n") or 0)
    return round((mk * n - p["cost_c"] - p["fee_c"]) / 100.0, 2)


def _shadow_rows(v):
    """Accept only a dict of per-ticker RECORDS.

    The one line in load() that still read the old "shadow" key picked
    up the PUBLISHED REPORT instead - {"n":4,"pending":0,"table":[...]}
    - and the very next cycle did row["close_ts"] on the integer 4.
    TypeError, thrown before save() could run, so the ledger froze and
    looked exactly like a dead thread. That is the THIRD distinct
    failure caused by one key collision.

    Two fixes, not one: read the right key, AND refuse a value of the
    wrong shape. A loader that trusts whatever is on disk turns any
    past bug into a permanent one."""
    if not isinstance(v, dict):
        return {}
    return {k: r for k, r in v.items()
            if isinstance(r, dict) and "close_ts" in r}


def _pyth_key():
    """The key, from the environment or from a file on the server.

    The file path matters: logs/ is gitignored and lives only on the
    droplet, so the key never enters the repo, this chat, or a systemd
    unit that someone might paste into a screenshot. Putting it there is
    ONE short line typed at the DigitalOcean console - which is the only
    thing that console reliably accepts (pasting into it injects
    bracketed-paste markers that corrupt the command)."""
    k = _clean_key(os.environ.get("PYTH_API_KEY", ""))
    if k:
        return k
    for path in (os.path.join("logs", "pyth_key.txt"),
                 "/opt/kalshibot/logs/pyth_key.txt"):
        try:
            with open(path) as f:
                k = _clean_key(f.read())
            if k:
                return k
        except Exception:
            continue
    return ""


PYTH_KEY = _pyth_key()
FEED_BACKOFF_S = int(os.environ.get("TICK_FEED_BACKOFF", "300"))


def _get(url, timeout=15, key=False):
    h = dict(UA)
    if key and PYTH_KEY:
        h["Authorization"] = "Bearer " + PYTH_KEY
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _num(v, dflt=0.0):
    """Kalshi hands back ints, strings, or nothing at all for the same
    field depending on the endpoint. Fourth time this shop has been bitten
    by it; never let a str reach a comparison."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return dflt


def _px_c(row, base):
    """Fractional-schema aware price read, in CENTS.

    The 15-minute books quote in the newer fractional schema:
    `yes_price_dollars: '0.4500'` and `count_fp: '2.09'`, with the legacy
    integer fields present but NULL. Reading the legacy field alone
    returns None on every row - which is how a scanner ends up believing
    a market with 8,000 contracts of depth is empty."""
    v = row.get(base + "_dollars")
    if v not in (None, ""):
        try:
            return round(float(v) * 100.0, 2)
        except (TypeError, ValueError):
            pass
    v = row.get(base)
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def fee_c(px_c, n, maker=True):
    """Kalshi fee in CENTS: ceil(rate x C x P x (1-P)), to the penny.

    The whole strategy is downstream of this curve: it peaks at 50c and
    collapses at the extremes, so at-the-money churn is unaffordable and
    near-certainty is nearly free."""
    p = max(0.0, min(1.0, px_c / 100.0))
    rate = MAKER_RATE if maker else TAKER_RATE
    # round before ceil: 0.07*100*0.25*100 is 175.00000000000003 in float
    # and a naive ceil silently overcharges every single trade
    return math.ceil(round(rate * n * p * (1.0 - p) * 100.0, 6))


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def model_p(spot, strike, sigma_s, t_left_s):
    """P(spot_at_close >= strike) for a driftless random walk.

    sigma_s = per-second volatility in PRICE units (not %), measured from
    the proxy's own recent ticks. Drift is deliberately omitted: over 900
    seconds the drift term is noise next to the diffusion term, and
    pretending to know direction is the trade we already decided not to
    make.

    Degenerate cases matter more than the formula. At t=0 the answer is
    not "50%" - it is the arithmetic fact of where the price already is.
    A model that hedges at the buzzer would refuse the exact trade this
    lane exists to take."""
    if t_left_s <= 0:
        return 1.0 if spot >= strike else 0.0
    s = sigma_s * math.sqrt(max(0.0, t_left_s))
    if s <= 0:
        return 1.0 if spot >= strike else 0.0
    return _norm_cdf((spot - strike) / s)


def avg_model_p(partial, n_have, spot, strike, sigma_s, t_left):
    """P(settlement average >= strike) for a 60-second averaging window.

    THE WHOLE POINT. Settlement is mean(60 one-second prints before T).
    With n_have of those seconds already observed and summed into
    `partial`, the remaining k = 60 - n_have seconds are the only thing
    still unknown - and they enter the average diluted by k/60.

        A = (partial + R) / 60,  R = sum of the k unknown prints
        A >= K   <=>   R >= 60K - partial

    R has mean ~ k*spot and, for a random walk sampled once a second,
    standard deviation ~ sigma * k^1.5 / sqrt(3) (the integrated-variance
    term: later seconds have drifted further, so the sum's variance grows
    faster than k). That factor is why certainty accrues so sharply in the
    last twenty seconds - and why a trader reading a price ticker cannot
    see it. When k = 0 the answer is arithmetic, not probability.
    """
    k = max(0, AVG_WINDOW_S - int(n_have))
    if k <= 0:
        return 1.0 if (partial / max(1, n_have)) >= strike else 0.0
    need = AVG_WINDOW_S * strike - partial
    mean_r = k * spot
    sd_r = max(1e-9, sigma_s * (k ** 1.5) / math.sqrt(3.0))
    # plus the uncertainty of where spot itself will be when the window
    # opens, if we are still ahead of it
    lead = max(0.0, t_left - AVG_WINDOW_S)
    if lead > 0:
        sd_r = math.sqrt(sd_r ** 2 + (k * sigma_s * math.sqrt(lead)) ** 2)
    return _norm_cdf((mean_r - need) / sd_r)


def _ts(s):
    if not s:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.datetime.fromisoformat(
            str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


class TickBook:
    """15-minute window paper book. Holds no money and cannot trade."""

    def __init__(self):
        self.rec = Recorder() if Recorder else None
        self.ticks = {}        # series -> [(ts, price), ...] proxy tape
        self.quotes = {}       # tk -> our resting paper bid this window
        self.resting = {}      # what was resting during the LAST window
        self.pos = {}          # tk -> position dict
        self.settled = []      # graded windows
        self.calib = {}        # model bucket -> [n, wins] reliability
        self.proxy_err = []    # |proxy - strike| behaviour vs outcome
        self.fills = []        # for adverse-selection scoring
        self.arbs = []         # true crossed books, if they ever appear
        self.fine = {}         # series -> per-second prints (averaging)
        self.shadow = {}       # tk -> the model's claim, awaiting outcome
        self.shadow_calib = {}  # bucket -> [n, hits] on EVERY window
        self.trips = {}        # tk -> completed round trips this window
        self.pend_calib = []   # exited claims awaiting the real outcome
        self.pair = {}         # tk -> {lo, hi} yes-price extremes seen
        self.pair_stats = {"n": 0, "both": 0, "one": 0, "none": 0}
        self.basis = {}        # series -> [proxy - strike observations]
        self._basis_seen = set()   # windows already measured
        self.pnl_days = {}
        self.realized_c = 0.0
        self.stats = {"cycles": 0, "quoted": 0, "fills_strict": 0,
                      "fills_loose": 0, "trades_seen": 0, "settled": 0,
                      "no_vol": 0, "no_proxy": 0, "capped": 0,
                      "band_skip": 0, "no_edge": 0, "wins": 0,
                      "proxy_dead": 0, "exits": 0, "arb_seen": 0,
                      "no_basis": 0,
                      "losses": 0, "endgame_n": 0, "tail_n": 0}
        self.errs = 0
        self._seen = set()
        self._last_ts = 0.0
        self._t0 = time.time()
        self._beat = time.time()
        self._feed_block_until = 0.0
        self._feed_err = ""
        self._dead_ids = set()   # feed ids this plan cannot see
        self.load()

    # ---------------- persistence ----------------
    # Every key read here is written in save(). The 8/15 nav_days bug and
    # the 8/25 rung_stats bug were both "loaded but never saved", so this
    # file keeps ONE list and a test asserts the two sides match.
    PERSIST = ("pos", "settled", "calib", "proxy_err", "fills",
               "pnl_days", "realized_c", "stats", "errs", "t0", "ticks",
               "arbs", "basis_obs", "basis_seen", "pair_obs",
               "pair_stats", "pend_calib", "trips",
               "shadow_calib", "fine", "shadow_obs")

    def load(self):
        try:
            d = json.load(open(STATE))
            if d.get("era") != ERA:
                # A LEDGER from another regime is not ours - P&L, settles
                # and calibration all reset. But the PRICE TAPE and the
                # INSTRUMENT OFFSET are not ledger entries: they are
                # properties of the feeds themselves, true regardless of
                # what strategy we were running when we measured them.
                # Wiping them on an era bump cost 45 minutes of blind
                # dead time after every reset, for no epistemic gain -
                # like recalibrating a thermometer because you changed
                # your mind about dinner. Carry them across.
                self.ticks = {k: [tuple(x) for x in v][-VOL_WINDOW:]
                              for k, v in (d.get("ticks") or {}).items()}
                self.basis = {k: list(v)[-BASIS_N:] for k, v in
                              (d.get("basis_obs") or {}).items()}
                self._basis_seen = set(d.get("basis_seen") or [])
                # shadow calibration measures the MODEL, not a trading
                # regime - it survives an era bump for the same reason
                # the price tape does
                self.shadow = _shadow_rows(d.get("shadow_obs"))
                self.shadow_calib = d.get("shadow_calib") or {}
                return
            self.pos = d.get("pos") or {}
            self.settled = _migrate_rows((d.get("settled") or [])[-200:])
            self.calib = d.get("calib") or {}
            self.proxy_err = (d.get("proxy_err") or [])[-200:]
            self.fills = (d.get("fills") or [])[-400:]
            self.pnl_days = d.get("pnl_days") or {}
            self.realized_c = d.get("realized_c") or 0.0
            self.stats.update(d.get("stats") or {})
            self.errs = d.get("errs") or 0
            self._t0 = d.get("t0") or self._t0
            self.arbs = (d.get("arbs") or [])[-40:]
            self.pair = d.get("pair_obs") or {}
            self.pend_calib = (d.get("pend_calib") or [])[-200:]
            self.trips = d.get("trips") or {}
            self.fine = {k: [tuple(x) for x in v]
                         for k, v in (d.get("fine") or {}).items()}
            self.shadow = _shadow_rows(d.get("shadow_obs"))
            self.shadow_calib = d.get("shadow_calib") or {}
            self.pair_stats.update(d.get("pair_stats") or {})
            self.basis = {k: list(v)[-BASIS_N:] for k, v in
                          (d.get("basis_obs") or {}).items()}
            # a window already measured must not be measured twice after
            # a restart - that would double-weight one boundary print
            self._basis_seen = set(d.get("basis_seen") or [])
            self.ticks = {k: [tuple(x) for x in v][-VOL_WINDOW:]
                          for k, v in (d.get("ticks") or {}).items()}
        except Exception:
            pass

    def save(self, state):
        try:
            os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
            state["pos"] = self.pos
            state["settled"] = self.settled[-200:]
            state["calib"] = self.calib
            state["proxy_err"] = self.proxy_err[-200:]
            state["fills"] = self.fills[-400:]
            state["pnl_days"] = self.pnl_days
            state["realized_c"] = self.realized_c
            state["stats"] = self.stats
            state["errs"] = self.errs
            state["t0"] = self._t0
            state["arbs"] = self.arbs[-40:]
            # NB: persist under a DIFFERENT key than the published
            # median - step() publishes state["basis"] as the corrected
            # offset the model actually uses, and writing the raw
            # observation list into the same key silently replaced it
            # on the tracker (caught live on the first deploy).
            state["basis_obs"] = {k: v[-BASIS_N:]
                                  for k, v in self.basis.items()}
            state["basis_seen"] = sorted(self._basis_seen)[-200:]
            # persist under a DIFFERENT key than the published report -
            # step() publishes state["pair"] as the completion REPORT,
            # and writing the raw per-window extremes into the same key
            # replaced it on the tracker. Second time I have made this
            # exact mistake in one session (see basis_obs); hence the
            # test below that forbids any save key from colliding with a
            # published one.
            state["pair_obs"] = self.pair
            state["pair_stats"] = self.pair_stats
            state["pend_calib"] = self.pend_calib[-200:]
            state["trips"] = self.trips
            # THIRD time a raw save key has clobbered a published
            # report of the same name (basis, pair, now shadow). The
            # test below now derives the collision set automatically
            # instead of listing keys I have to remember.
            state["shadow_obs"] = self.shadow
            state["shadow_calib"] = self.shadow_calib
            state["fine"] = {k: v[-400:] for k, v in self.fine.items()}
            state["ticks"] = {k: v[-VOL_WINDOW:]
                              for k, v in self.ticks.items()}
            json.dump(state, open(STATE, "w"))
        except Exception:
            self.errs += 1

    # ---------------- the reference feed ----------------
    def fetch_proxy(self):
        """One Hermes call for every feed we track. Returns
        series -> (price, age_s). A stale feed is reported, never
        silently used: modelling a 900-second window off a 5-minute-old
        price is worse than not modelling it at all."""
        out = self.fetch_crypto()          # free, always available
        ids = sorted({v[0] for v in SERIES.values()}
                     - self._dead_ids)
        if not ids:
            return out
        url = (HERMES + "/v2/updates/price/latest?"
               + "&".join("ids[]=" + i for i in ids)
               + "&parsed=true&encoding=hex")
        # BACKOFF: a feed that is refusing us will refuse us again in 20
        # seconds. Retrying on every cycle burned 327 errors and 1,446
        # refusals into the ledger before anyone looked. Back off, and
        # say WHY on the tracker instead of failing quietly.
        if self._feed_block_until > time.time():
            return out
        # re-read every cycle: writing the key file should take effect on
        # the next tick, with no deploy and no service restart
        global PYTH_KEY
        if not PYTH_KEY:
            PYTH_KEY = _pyth_key()
        try:
            d = _get(url, key=True)
            self._feed_err = ""
        except Exception as e:
            self.errs += 1
            msg = str(e)
            self._feed_err = msg[:140]
            if "401" in msg or "403" in msg or "429" in msg:
                self._feed_block_until = time.time() + FEED_BACKOFF_S
            elif "404" in msg:
                # ONE UNAVAILABLE FEED BLINDS ALL OF THEM. Hermes 404s
                # the entire batch when any single id is missing, naming
                # the offender in the body: "Price IDs not found: <id>".
                # Measured 8/26 - WTI (USOILSPOT) is not in this plan's
                # feed set, so gold and silver, both perfectly available,
                # returned nothing for hours. Prune the offender and keep
                # the rest of the book alive; if the plan later covers
                # it, clearing _dead_ids on restart picks it up again.
                body = ""
                try:
                    body = e.read().decode("utf-8", "ignore")
                except Exception:
                    pass
                found = False
                for fid in {v[0] for v in SERIES.values()}:
                    if fid in body or fid in msg:
                        self._dead_ids.add(fid)
                        found = True
                if not found:
                    self._feed_block_until = time.time() + FEED_BACKOFF_S
            return out          # crypto prices survive a Pyth outage
        now = time.time()
        by_id = {}
        for p in d.get("parsed", []):
            try:
                px = p["price"]
                val = int(px["price"]) * (10 ** int(px["expo"]))
                by_id[p["id"]] = (val, now - float(px["publish_time"]))
            except Exception:
                continue
        for st, (fid, _lab) in SERIES.items():
            if fid in by_id:
                out[st] = by_id[fid]
                price, age = by_id[fid]
                tape = self.ticks.setdefault(st, [])
                if not tape or tape[-1][0] != round(now):
                    tape.append((round(now), price))
                del tape[:-VOL_WINDOW]
        return out

    def fetch_crypto(self):
        """Free spot prices. No key, no tier, no expiry.

        Coinbase first, Kraken as a fallback - two independent public
        endpoints, so one going down does not blind the lane. This is
        the argument for crypto over metals in one line: the data cannot
        be taken away from us."""
        out = {}
        for st, (pair, _lab) in CRYPTO.items():
            px = None
            try:
                d = _get(f"{COINBASE}/v2/prices/{pair}/spot", timeout=8)
                px = float(d["data"]["amount"])
            except Exception:
                try:
                    kp = pair.replace("BTC", "XBT").replace("-", "")
                    d = _get("https://api.kraken.com/0/public/Ticker"
                             f"?pair={kp}", timeout=8)
                    r = list((d.get("result") or {}).values())[0]
                    px = float(r["c"][0])
                except Exception:
                    self.errs += 1
            if px and px > 0:
                out[st] = (px, 0.0)
                now = time.time()
                tape = self.ticks.setdefault(st, [])
                if not tape or tape[-1][0] != round(now):
                    tape.append((round(now), px))
                del tape[:-VOL_WINDOW]
                # SECOND-BY-SECOND tape, kept separately: the settlement
                # average needs the individual prints, and the coarse
                # vol tape above is deliberately thinned.
                fine = self.fine.setdefault(st, [])
                fine.append((round(now, 1), px))
                cut = now - (AVG_WINDOW_S + BURST_AT_S + 30)
                self.fine[st] = [r for r in fine if r[0] >= cut]
        return out

    def partial_avg(self, series, close_ts):
        """(sum, count) of the settlement window's prints seen so far."""
        lo = close_ts - AVG_WINDOW_S
        rows = [p for t, p in (self.fine.get(series) or [])
                if lo <= t < close_ts]
        return (sum(rows), len(rows))

    def sigma_s(self, series):
        """Per-second price volatility from the proxy's own tape.

        Deliberately simple and deliberately EMPIRICAL: no implied vol,
        no GARCH, just the realized standard deviation of log returns
        scaled by the observed spacing between ticks. If there aren't
        enough ticks yet, return None and let the caller refuse to quote
        rather than invent a number."""
        tape = self.ticks.get(series) or []
        if len(tape) < MIN_VOL_N:
            return None

        def _sd(rows):
            rets = []
            for (t0, p0), (t1, p1) in zip(rows, rows[1:]):
                dt = max(1.0, t1 - t0)
                if p0 > 0 and p1 > 0:
                    rets.append((p1 - p0) / math.sqrt(dt))
            if len(rets) < MIN_VOL_N // 2:
                return None
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
            return math.sqrt(max(0.0, var))

        # long lookback catches the regime, short catches today's burst.
        # Taking the MAX means a calm hour cannot lull the model into
        # certainty right before a jump - the exact failure we measured.
        cands = [x for x in (_sd(tape),
                             _sd(tape[-SHORT_N:])
                             if len(tape) > SHORT_N else None) if x]
        if not cands:
            return None
        return max(cands) * VOL_MULT

    def track_pair(self, mkts):
        """Record each window's YES-price extremes and, when the window
        closes, whether a legged pair at PAIR_L would have COMPLETED.

        Measures, never trades - see the PAIR_L note above for the
        backtest that put this lane on ice. What it is watching for is a
        regime change: completion has to clear ~88.5% at 45c before the
        trade pays, and the only honest way to know is to keep counting."""
        live = set()
        for m in mkts:
            tk = m["tk"]
            live.add(tk)
            yb, ya = m.get("yes_bid"), m.get("yes_ask")
            if yb is None or ya is None:
                continue
            mid = (yb + ya) / 2.0
            r = self.pair.setdefault(tk, {"lo": mid, "hi": mid,
                                          "close": m["close_ts"]})
            r["lo"] = min(r["lo"], yb)     # cheapest YES on offer
            r["hi"] = max(r["hi"], ya)     # dearest YES = cheapest NO
        now = time.time()
        for tk, r in list(self.pair.items()):
            if tk in live or r.get("close", 0) > now - 30:
                continue
            yes_leg = r["lo"] < PAIR_L
            no_leg = r["hi"] > (100.0 - PAIR_L)
            self.pair_stats["n"] += 1
            if yes_leg and no_leg:
                self.pair_stats["both"] += 1
            elif yes_leg or no_leg:
                self.pair_stats["one"] += 1
            else:
                self.pair_stats["none"] += 1
            self.pair.pop(tk, None)
        for tk in list(self.trips):
            r = self.pair.get(tk)
            if tk not in live and not r:
                self.trips.pop(tk, None)
        if len(self.pair) > 40:
            for k in sorted(self.pair,
                            key=lambda x: self.pair[x].get("close", 0))[:20]:
                self.pair.pop(k, None)

    def pair_report(self):
        """Completion rate against the breakeven it must clear to pay."""
        st = dict(self.pair_stats)
        n = st.get("n", 0)
        f = fee_c(PAIR_L, 1, maker=True)
        lock = 100.0 - 2 * PAIR_L - 2 * f          # profit if both fill
        risk = PAIR_L + f                          # loss if only one does
        # P*lock = (1-P)*risk  ->  P = risk / (lock + risk)
        need = risk / (lock + risk) if (lock + risk) else None
        got = (st["both"] / n) if n else None
        return {"n": n, "both": st.get("both", 0), "one": st.get("one", 0),
                "none": st.get("none", 0), "L": PAIR_L,
                "lock_c": round(lock, 2), "risk_c": round(risk, 2),
                "rate": round(got, 3) if got is not None else None,
                "breakeven": round(need, 3) if need else None,
                "pays": (got is not None and need is not None
                         and got >= need)}

    def backfill_basis(self):
        """Learn the instrument offset from windows that ALREADY closed.

        The offset is only measurable at a window's open, which yields
        one sample per 15 minutes - so an era reset cost 45 minutes of
        dead time before the book could quote anything. That is a design
        flaw, and an avoidable one: every RECENTLY CLOSED window carries
        a strike that was, by definition, the settlement feed's value at
        a known past instant. Our own price tape covers the last hour.
        Matching the two backfills the whole warmup in one pass, on the
        next cycle after a restart, with no extra assumptions."""
        # CRYPTO TOO. The backfill loop iterated only the metals, so the
        # crypto lane could never recover its anchors from history and
        # sat blind waiting a full window for each one.
        for st in list(SERIES) + list(CRYPTO):
            if len(self.basis.get(st) or []) >= MIN_BASIS_N:
                continue
            tape = self.ticks.get(st) or []
            if not tape:
                continue
            oldest = tape[0][0]
            try:
                d = _get(f"{KALSHI}/markets?series_ticker={st}"
                         f"&status=settled&limit=20")
                rows = d.get("markets", [])
            except Exception:
                self.errs += 1
                continue
            for m in rows:
                tk = m.get("ticker")
                ot = _ts(m.get("open_time"))
                strike = _num(m.get("floor_strike"), None)
                if (not tk or tk in self._basis_seen or not ot
                        or strike in (None, 0.0) or ot < oldest):
                    continue
                near = [(abs(t - ot), p) for t, p in tape
                        if abs(t - ot) <= BASIS_WINDOW_S]
                if not near:
                    continue
                _dt, px = min(near)
                self.basis.setdefault(st, []).append(
                    round(px - strike, 6))
                del self.basis[st][:-BASIS_N]
                self._basis_seen.add(tk)
                self.stats["basis_backfill"] = self.stats.get(
                    "basis_backfill", 0) + 1

    def measure_basis(self, mkts):
        """One free observation per window: at the instant a window
        opens, Kalshi's settlement feed EQUALS the new strike, so the
        gap between our proxy and that strike at that moment IS our
        instrument's zero error."""
        for m in mkts:
            tk, st, ot = m["tk"], m["series"], m.get("open_ts") or 0
            if not ot or tk in self._basis_seen:
                continue
            tape = self.ticks.get(st) or []
            near = [(abs(t - ot), p) for t, p in tape
                    if abs(t - ot) <= BASIS_WINDOW_S]
            if not near:
                continue
            _dt, px = min(near)
            self.basis.setdefault(st, []).append(
                round(px - m["strike"], 6))
            del self.basis[st][:-BASIS_N]
            self._basis_seen.add(tk)
        if len(self._basis_seen) > 400:
            self._basis_seen = set(list(self._basis_seen)[-200:])

    def basis_of(self, series):
        """Rolling MEDIAN offset - median, not mean, so one bad print at
        a window boundary cannot drag the correction."""
        rows = sorted(self.basis.get(series) or [])
        if len(rows) < MIN_BASIS_N:
            return None
        n = len(rows)
        return (rows[n // 2] if n % 2
                else (rows[n // 2 - 1] + rows[n // 2]) / 2.0)

    def adj_spot(self, series, spot):
        """Our proxy price expressed in the settlement feed's units."""
        b = self.basis_of(series)
        return None if b is None else spot - b

    def liveness_bp(self, series):
        """How much does this proxy actually MOVE, in basis points of
        its own price? A feed can be perfectly fresh (publish_time
        seconds old) and still be useless if it reprints the same
        number - freshness and liveness are different failures, and only
        the second one produced a 43-cent phantom edge on WTI."""
        tape = self.ticks.get(series) or []
        if len(tape) < LIVE_MIN_N:
            return None
        px = [p for _t, p in tape if p]
        if not px:
            return None
        lo, hi, mid = min(px), max(px), sum(px) / len(px)
        if mid <= 0:
            return None
        return 10000.0 * (hi - lo) / mid

    def proxy_dead(self, series):
        bp = self.liveness_bp(series)
        return (bp is not None) and bp < MIN_LIVE_BP

    # ---------------- the exchange surface ----------------
    def fetch_markets(self):
        """The open window for each series, with its book. One market per
        series is live at a time - these are single-strike up/down
        windows, not ladders."""
        out = []
        for st in list(SERIES) + list(CRYPTO):
            try:
                d = _get(f"{KALSHI}/markets?series_ticker={st}"
                         f"&status=open&limit=5")
            except Exception:
                self.errs += 1
                continue
            for m in d.get("markets", []):
                close = _ts(m.get("close_time"))
                if not close or close <= time.time():
                    continue
                strike = _num(m.get("floor_strike"), None)
                if strike in (None, 0.0):
                    continue
                out.append({
                    "tk": m.get("ticker"), "series": st,
                    "label": (SERIES[st][1] if st in SERIES
                              else CRYPTO[st][1]),
                    "avg": st in CRYPTO,
                    "strike": strike,
                    "open_ts": _ts(m.get("open_time")),
                    "close_ts": close,
                    "title": m.get("title") or "",
                    "yes_bid": _px_c(m, "yes_bid"),
                    "yes_ask": _px_c(m, "yes_ask"),
                    "no_bid": _px_c(m, "no_bid"),
                    "no_ask": _px_c(m, "no_ask"),
                })
        return out

    def fetch_book(self, tk):
        """Top of book from the orderbook endpoint.

        The /markets rows on these series carry NULL yes_bid/yes_ask even
        while the book holds thousands of contracts (verified 8/25:
        8,000-deep on both sides, every legacy price field null). So the
        orderbook is the only trustworthy quote source here, and the NO
        ladder has to be inverted into YES terms by hand: a NO bid at 44c
        is a YES offer at 56c."""
        try:
            d = _get(f"{KALSHI}/markets/{tk}/orderbook?depth=8")
        except Exception:
            self.errs += 1
            return None, None, 0.0, 0.0
        ob = d.get("orderbook_fp") or d.get("orderbook") or {}
        fp = "no_dollars" in ob or "yes_dollars" in ob

        def _side(key_fp, key):
            rows = ob.get(key_fp if fp else key) or []
            out = []
            for r in rows:
                try:
                    px = float(r[0]) * (100.0 if fp else 1.0)
                    ct = float(r[1])
                    out.append((round(px, 2), ct))
                except (TypeError, ValueError, IndexError):
                    continue
            return out

        yes = _side("yes_dollars", "yes")
        no = _side("no_dollars", "no")
        # best YES bid = highest price someone will pay for YES
        yb = max((p for p, _c in yes), default=None)
        ybc = sum(c for p, c in yes if p == yb) if yb is not None else 0.0
        # best YES ask = 100 - highest NO bid
        nb = max((p for p, _c in no), default=None)
        ya = (100.0 - nb) if nb is not None else None
        yac = sum(c for p, c in no if p == nb) if nb is not None else 0.0
        return yb, ya, ybc, yac

    def fetch_trades(self, since_s):
        """Real prints since the last cycle - the only thing allowed to
        fill a paper order."""
        out = []
        for st in SERIES:
            for m in list(self.quotes.values()):
                if m["series"] != st:
                    continue
                try:
                    d = _get(f"{KALSHI}/markets/trades?ticker={m['tk']}"
                             f"&limit=200")
                except Exception:
                    self.errs += 1
                    continue
                for t in d.get("trades", []):
                    ts = _ts(t.get("created_time"))
                    if ts < since_s:
                        continue
                    tid = t.get("trade_id")
                    if not tid or tid in self._seen:
                        continue
                    self._seen.add(tid)
                    yp = _px_c(t, "yes_price")
                    ct = _num(t.get("count_fp"), None)
                    if ct is None:
                        ct = _num(t.get("count"), 0.0)
                    if yp is None or ct <= 0:
                        continue
                    out.append({"tk": t.get("ticker"), "px": yp,
                                "ct": ct, "ts": ts,
                                "taker": t.get("taker_side")})
        if len(self._seen) > 20000:
            self._seen = set(list(self._seen)[-8000:])
        self.stats["trades_seen"] += len(out)
        return out

    # ---------------- the two lanes ----------------
    def _capital_c(self):
        return sum(p["cost_c"] for p in self.pos.values())

    def decide(self, m, spot, sig, t_left):
        """Pick the best available side, or None.

        8/25 (Adam: "make sure you are trading no and yes if there is a
        delta between the no and the spot price"): the two sides are NOT
        one number. YES and NO are separate books with their own bids,
        asks and depth, and the arithmetic linking them (a NO at 12 is a
        YES at 88) only holds if you cross the spread. So both sides are
        priced independently against the model, each on the price we
        would actually pay for it, and we take the BETTER edge - not
        whichever side the model happens to favour. A 96%-likely YES
        offered at 95 is a worse trade than a 60%-likely NO offered at
        48, and the old code could not see the second one at all."""
        if m.get("avg"):
            # 60-second averaging settlement: part of the answer is
            # already determined and the rest is diluted by k/60
            ps, pn = self.partial_avg(m["series"], m["close_ts"])
            p_raw = avg_model_p(ps, pn, spot, m["strike"], sig, t_left)
        else:
            p_raw = model_p(spot, m["strike"], sig, t_left)
        # Never claim more certainty than CONF_CAP. The far tail is
        # exactly where a wrong vol estimate does its damage, and a
        # "99%" that is really 90% is a losing trade dressed as a gift.
        p_yes = min(CONF_CAP, max(1.0 - CONF_CAP, p_raw))
        yb, ya = m["yes_bid"], m["yes_ask"]
        if yb is None or ya is None:
            return None
        # price we would pay to BUY each side, resting one tick inside
        # that side's own spread. NO's book is the mirror of YES's.
        cands = []
        for side, p_side, px in (
                ("yes", p_yes, min(ya - 1.0, yb + 1.0)),
                ("no", 1.0 - p_yes,
                 min((100.0 - yb) - 1.0, (100.0 - ya) + 1.0))):
            if px is None:
                continue
            px = round(px, 2)
            if not (MIN_PX_C <= px <= MAX_PX_C):
                self.stats["band_skip"] += 1
                continue
            # NET of the round trip. Widening the band into mid-prices
            # is only safe if the arithmetic prices the fee, which peaks
            # at 50c (0.07 x P x (1-P)) and vanishes at the extremes. A
            # gross-edge test would happily buy a 4c edge that costs 5c
            # to trade - which is precisely how the phantom book lost
            # 2.45c on every pair it captured.
            rt_fee = (fee_c(px, 1, maker=True)
                      + fee_c(min(99.0, px + EDGE_C), 1, maker=True))
            edge = p_side * 100.0 - px - rt_fee
            if edge < EDGE_C:
                self.stats["no_edge"] += 1
                continue
            if t_left <= ENDGAME_S and p_side >= ENDGAME_P:
                cands.append((edge, "endgame", px, side, p_side))
            elif t_left <= TAIL_MAX_S and p_side >= TAIL_P:
                cands.append((edge, "tail", px, side, p_side))
        # THE FAVOURITE LANE. Checked last, and deliberately NOT gated on
        # the model beating the market - only on the model not
        # contradicting it. See the note at FAV_MIN_C.
        if not cands and t_left <= FAV_AT_S:
            # THE FAVOURITE IS THE EXPENSIVE SIDE. Buying YES costs the
            # ask; buying NO costs 100 - the yes bid. Whichever costs
            # MORE is the near-certain one - the first version of this
            # line took the cheaper side, i.e. the longshot, which is
            # precisely the trade the 70-90c band shows losing money.
            fav_side = "yes" if ya >= (100.0 - yb) else "no"
            fav_px = round(ya if fav_side == "yes" else 100.0 - yb, 2)
            fav_p = p_yes if fav_side == "yes" else 1.0 - p_yes
            if FAV_MIN_C <= fav_px <= FAV_MAX_C:
                if fav_p >= FAV_VETO_P:
                    return "fav", p_yes, fav_px, fav_side, fav_p
                self.stats["fav_vetoed"] = self.stats.get(
                    "fav_vetoed", 0) + 1
        if not cands:
            return None
        edge, lane, px, side, p_side = max(cands)
        return lane, p_yes, px, side, p_side

    def check_arb(self, m):
        """TRUE arbitrage, as opposed to a model opinion.

        If we can BUY yes and BUY no for less than 100c combined (after
        both fees), the pair pays exactly 100c however it settles and
        the profit is locked with no view at all. This is the only thing
        in this file that deserves the word 'arbitrage'. On a liquid
        book it should essentially never appear - which is precisely why
        it is counted rather than assumed away. Recorded, never traded
        blind: a printed crossed book is usually a stale quote that will
        vanish before a resting order can touch it."""
        yb, ya = m.get("yes_bid"), m.get("yes_ask")
        if yb is None or ya is None:
            return None
        no_ask = 100.0 - yb          # buying NO lifts the YES bid side
        cost = ya + no_ask
        fees = (fee_c(ya, 1, maker=True) + fee_c(no_ask, 1, maker=True))
        net = 100.0 - cost - fees
        if net >= ARB_MIN_C:
            self.stats["arb_seen"] += 1
            return {"tk": m["tk"], "label": m["label"],
                    "yes_ask": ya, "no_ask": round(no_ask, 2),
                    "cost": round(cost, 2), "fees": fees,
                    "net_c": round(net, 2),
                    "ts": datetime.datetime.now().isoformat(
                        timespec="seconds")}
        return None

    def quote(self, mkts, proxy):
        """Post paper bids. Nothing here can reach an order endpoint."""
        self.quotes = {}
        now = time.time()
        self.measure_basis(mkts)
        # only while still warming up - one cheap call, then never again
        if any(len(self.basis.get(st) or []) < MIN_BASIS_N
               for st in list(SERIES) + list(CRYPTO)):
            self.backfill_basis()
        self.track_pair(mkts)
        for m in mkts:
            px_age = proxy.get(m["series"])
            if not px_age:
                self.stats["no_proxy"] += 1
                continue
            spot, age = px_age
            if age > 90:
                self.stats["no_proxy"] += 1
                continue
            # book first, THEN the vol gate: during warmup we still want
            # the tape to record what the market looked like, otherwise
            # the first hour of telemetry is blank exactly when we are
            # trying to learn whether these books are ever loose
            yb, ya, ybc, yac = self.fetch_book(m["tk"])
            if yb is not None:
                m["yes_bid"] = yb
            if ya is not None:
                m["yes_ask"] = ya
            m["depth"] = round((ybc or 0) + (yac or 0), 1)
            _arb = self.check_arb(m)
            if _arb:
                self.arbs.append(_arb)
                del self.arbs[:-40]
            spot = self.adj_spot(m["series"], spot)
            if spot is None:
                # we do not yet know this feed's offset against the
                # settlement feed, so every distance would be guesswork
                self.stats["no_basis"] += 1
                continue
            if self.proxy_dead(m["series"]):
                # measured, not assumed: this series' reference feed is
                # not tracking its market, so every distance we compute
                # from it is noise. Refuse the whole series rather than
                # trade our own instrument error.
                self.stats["proxy_dead"] += 1
                m["proxy_dead"] = True
                continue
            sig = self.sigma_s(m["series"])
            if sig is None:
                self.stats["no_vol"] += 1
                continue
            t_left = m["close_ts"] - now
            d = self.decide(m, spot, sig, t_left)
            if not d:
                continue
            lane, p_yes, our_px, side, p_side = d
            held = self.pos.get(m["tk"], {}).get("n", 0.0)
            if held >= MAX_POS:
                self.stats["capped"] += 1
                continue
            # RE-ENTRY IS THE POINT (Adam: "trade in and out over and
            # over"). Nothing blocks quoting a market we have already
            # traded and exited this window - but cap the round trips so
            # one choppy window cannot dominate the sample and so the
            # fee drag of churn stays visible rather than infinite.
            if self.trips.get(m["tk"], 0) >= MAX_TRIPS:
                self.stats["trip_capped"] = self.stats.get(
                    "trip_capped", 0) + 1
                continue
            add_c = our_px * SIZE
            if self._capital_c() + add_c > BOOK_CAPITAL_C:
                self.stats["capped"] += 1
                continue
            _prev = self.resting.get(m["tk"]) or {}
            _carry = (float(_prev.get("filled", 0.0))
                      if (_prev.get("our_px") == our_px
                          and _prev.get("side") == side) else 0.0)
            self.quotes[m["tk"]] = dict(
                m, lane=lane, our_px=our_px, side=side, filled=_carry,
                model_p=round(p_yes, 4), p_side=round(p_side, 4),
                spot=spot, sigma=sig, t_left=round(t_left),
                edge=round(p_side * 100.0 - our_px, 2), ts=now)
            self.stats["quoted"] += 1
        return len(self.quotes)

    # ---------------- fills ----------------
    def check_fills(self, trades, since):
        """A paper bid fills only when a REAL print trades THROUGH it.

        Our resting price is expressed in the currency of the side we
        bid. A YES bid at 92 is filled by a print at <= 91 (someone sold
        YES through us). A NO bid at 92 means YES changed hands at >= 9,
        i.e. a print at yes_px >= 100 - 91. Prints exactly AT our price
        are counted LOOSE and never believed - we have no claim on queue
        position."""
        by_tk = {}
        for t in trades:
            by_tk.setdefault(t["tk"], []).append(t)
        for tk, q in self.resting.items():
            if q.get("lane") == "fav":
                # a taker order fills at once, at the price we saw. No
                # queue to wait in and no pretending otherwise.
                left = float(SIZE) - float(q.get("filled", 0.0))
                if left > 0:
                    got = self._fill(q, left, q["our_px"])
                    if got > 0:
                        q["filled"] = float(q.get("filled", 0.0)) + got
                        self.stats["fills_taker"] = self.stats.get(
                            "fills_taker", 0) + 1
                continue
            prints = by_tk.get(tk) or []
            if not prints:
                continue
            our, side = q["our_px"], q["side"]
            for t in prints:
                if t["ts"] < q["ts"]:
                    continue            # posted after the print: no fill
                yes_px = t["px"]
                if side == "yes":
                    through, at = yes_px < our, abs(yes_px - our) < 0.01
                else:
                    no_px = 100.0 - yes_px
                    through, at = no_px < our, abs(no_px - our) < 0.01
                if at:
                    self.stats["fills_loose"] += 1
                    continue
                if not through:
                    continue
                # A RESTING ORDER HAS A FINITE SIZE. This loop used to
                # call _fill once per print for up to SIZE contracts
                # EACH, so a single 5-lot quote sitting in a busy book
                # accumulated hundreds of contracts - 677 on one window,
                # $623 of collateral on a $100 book, and a fabricated
                # +$304 P&L. An order for 5 can fill 5 in total, ever.
                left = float(SIZE) - float(q.get("filled", 0.0))
                if left <= 0:
                    break
                n = min(float(t["ct"]), left)
                if n <= 0:
                    continue
                got = self._fill(q, n, our)
                if got <= 0:
                    break               # a cap refused it: stop trying
                q["filled"] = float(q.get("filled", 0.0)) + got
                self.stats["fills_strict"] += 1

    def _fill(self, q, n, px):
        """Book a paper fill, and ENFORCE THE CAPS HERE.

        Position and capital limits used to be checked only when posting
        a quote, which is the wrong place: quoting is an intention, a
        fill is the thing that actually consumes the book. Both are now
        enforced at the moment inventory is created, and the fill is
        trimmed to whatever room is genuinely left. Returns the size
        actually taken, so the caller can stop when a cap bites."""
        held = float(self.pos.get(q["tk"], {}).get("n", 0.0))
        room = float(MAX_POS) - held
        if room <= 0:
            self.stats["capped"] += 1
            return 0.0
        n = min(float(n), room)
        cap_left = (float(BOOK_CAPITAL_C) - self._capital_c()) / max(1e-9, px)
        if cap_left <= 0:
            self.stats["cap_full"] = self.stats.get("cap_full", 0) + 1
            return 0.0
        n = min(n, cap_left)
        if n <= 0:
            return 0.0
        p = self.pos.setdefault(q["tk"], {
            "tk": q["tk"], "series": q["series"], "label": q["label"],
            "lane": q["lane"], "side": q["side"], "n": 0.0, "cost_c": 0.0,
            "avg": bool(q.get("avg")),
            "fee_c": 0.0, "strike": q["strike"], "close_ts": q["close_ts"],
            "model_p": q["model_p"], "p_side": q["p_side"],
            "spot_at_entry": q["spot"], "t_left": q["t_left"],
            "opened": datetime.datetime.now().isoformat(timespec="seconds")})
        p["n"] += n
        p["cost_c"] += px * n
        p["fee_c"] += fee_c(px, n, maker=(q.get("lane") != "fav"))
        self.fills.append({"tk": q["tk"], "px": px, "n": n,
                           "side": q["side"], "lane": q["lane"],
                           "model_p": q["p_side"], "ts": time.time()})
        del self.fills[:-400]
        return n

    # ---------------- the exit lane ----------------
    def check_exits(self, mkts, proxy):
        """Take the money when the market comes to us, instead of riding
        a position into a binary outcome.

        Adam, 8/25: "we can trade in and out of it as it moves towards
        settlement." The live weather book settles the argument - lifts
        +$88.82 over 387 turns against settles -$91.05 over 168. Getting
        paid early and recycling the collateral is the entire business;
        holding to expiry is how inventory becomes a coin flip.

        The exit test is the ENTRY test in reverse. We bought because
        the book was priced under our model. We sell when the book has
        come back to (or through) the model, i.e. when the remaining
        edge no longer pays for the risk of the last minutes. And we
        sell PASSIVELY - resting one tick inside the bid - because a
        taker exit at these prices hands back more than the edge we
        came for. Every exit is graded against what holding would have
        paid, so this lane can be convicted by its own tape too."""
        now = time.time()
        by_tk = {m["tk"]: m for m in mkts}
        for tk, pos in list(self.pos.items()):
            m = by_tk.get(tk)
            if not m or m.get("yes_bid") is None:
                continue
            if now - _ts(pos.get("opened")) < EXIT_MIN_HOLD_S:
                continue
            pa = proxy.get(pos["series"])
            sig = self.sigma_s(pos["series"])
            if not pa or sig is None or self.proxy_dead(pos["series"]):
                continue
            t_left = max(0.0, pos["close_ts"] - now)
            _sp = self.adj_spot(pos["series"], pa[0])
            if _sp is None:
                continue
            if pos.get("avg"):
                _ps, _pn = self.partial_avg(pos["series"], pos["close_ts"])
                p_raw = avg_model_p(_ps, _pn, _sp, pos["strike"], sig,
                                    t_left)
            else:
                p_raw = model_p(_sp, pos["strike"], sig, t_left)
            p_yes = min(CONF_CAP, max(1.0 - CONF_CAP, p_raw))
            p_side = p_yes if pos["side"] == "yes" else 1.0 - p_yes
            # what we could sell into right now, passively
            if pos["side"] == "yes":
                bid = m["yes_bid"]
            else:
                bid = 100.0 - m["yes_ask"]
            if bid is None:
                continue
            sell_px = round(bid, 2)
            avg = pos["cost_c"] / max(1e-9, pos["n"])
            remaining = p_side * 100.0 - sell_px
            broken = p_side < STOP_P
            if not broken:
                if remaining > EXIT_EDGE_C:
                    continue    # still cheap: the trade is not finished
                if sell_px <= avg:
                    continue    # never pay to leave a thesis INTACT
            # ...but when the thesis is BROKEN - we bought a 75% and the
            # model now reads it below STOP_P - the reason for holding
            # has evaporated, and taking the loss frees the collateral
            # to be redeployed inside the same window. That is the
            # difference between a stop and the flatten leak.
            n = pos["n"]
            fee = fee_c(sell_px, n, maker=True)
            pnl_c = sell_px * n - pos["cost_c"] - pos["fee_c"] - fee
            self.realized_c += pnl_c
            self.stats["exits"] += 1
            if broken:
                self.stats["stops"] = self.stats.get("stops", 0) + 1
            self.stats["settled"] += 1
            self.trips[tk] = self.trips.get(tk, 0) + 1
            self.stats["wins" if pnl_c > 0 else "losses"] += 1
            # CALIBRATION INTEGRITY: an exit used to credit itself an
            # automatic win here, on the reasoning that the market
            # agreeing with us IS the model being right. That is the
            # 8/17 sold_net winner-selection bias wearing a new hat - we
            # exit the trades that are working, so scoring exits as wins
            # guarantees a flattering table no matter how bad the model
            # is. Instead the claim is parked and graded later against
            # Kalshi's actual result, P&L unaffected.
            self.pend_calib.append({
                "tk": tk, "p_side": pos["p_side"], "side": pos["side"],
                "close_ts": pos["close_ts"]})
            del self.pend_calib[:-200]
            self.settled.append(self._row(
                tk, pos, n, avg, sell_px, pos["fee_c"] + fee, pnl_c,
                "STOPPED" if broken else "SOLD", pos.get("p_side"),
                {"entry_lane": pos.get("lane"), "lane": "exit",
                 "stop": broken, "t_left": round(t_left)}))
            del self.settled[:-200]
            self.pos.pop(tk, None)

    # ---------------- settlement ----------------
    def observe_shadow(self, mkts, proxy):
        """Record the model's claim on EVERY window, traded or not."""
        now = time.time()
        for m in mkts:
            tk, st = m["tk"], m["series"]
            if tk in self.shadow:
                continue
            left = m["close_ts"] - now
            if left > SHADOW_AT_S or left <= 0:
                continue            # one observation, at a fixed point
            pa = proxy.get(st)
            sig = self.sigma_s(st)
            if not pa or sig is None or self.proxy_dead(st):
                continue
            spot = self.adj_spot(st, pa[0])
            if spot is None:
                continue
            if m.get("avg"):
                _ps, _pn = self.partial_avg(st, m["close_ts"])
                p = avg_model_p(_ps, _pn, spot, m["strike"], sig, left)
            else:
                p = model_p(spot, m["strike"], sig, left)
            p = min(CONF_CAP, max(1.0 - CONF_CAP, p))
            # MOMENTUM, recorded as a FEATURE rather than traded on
            # (Adam wants momentum trades; the disciplined order is to
            # find out whether it predicts BEFORE betting on it). Recent
            # drift of the proxy, in price units per second.
            tape = self.ticks.get(st) or []
            mom = None
            if len(tape) >= 6:
                (t0, p0), (t1, p1) = tape[-6], tape[-1]
                if t1 > t0:
                    mom = round((p1 - p0) / (t1 - t0), 8)
            self.shadow[tk] = {"p": round(p, 4), "close_ts": m["close_ts"],
                               "label": m["label"], "mom": mom,
                               "d": round(spot - m["strike"], 6),
                               "px": m.get("yes_bid")}
        if len(self.shadow) > 400:
            for k in sorted(self.shadow,
                            key=lambda x: self.shadow[x]["close_ts"])[:200]:
                self.shadow.pop(k, None)

    def grade_shadow(self):
        """Grade every recorded claim against Kalshi's settled result."""
        now = time.time()
        for tk, row in list(self.shadow.items()):
            if row["close_ts"] > now - 60:
                continue
            try:
                m = _get(f"{KALSHI}/markets/{tk}")["market"]
            except Exception:
                self.errs += 1
                continue
            res = (m.get("result") or "").lower()
            if res not in ("yes", "no"):
                if row["close_ts"] < now - 3600:
                    self.shadow.pop(tk, None)
                continue
            p = row["p"]
            b = str(int(min(0.99, max(0.0, p)) * 10) * 10)
            c = self.shadow_calib.setdefault(b, [0, 0])
            c[0] += 1
            c[1] += 1 if res == "yes" else 0
            self.stats["shadow_n"] = self.stats.get("shadow_n", 0) + 1
            self.shadow.pop(tk, None)

    def shadow_table(self):
        """Reliability curve: when the model says X%, does X% happen?"""
        out = []
        for b in sorted(self.shadow_calib, key=lambda x: int(x)):
            n, w = self.shadow_calib[b]
            said = int(b) + 5
            hit = (100.0 * w / n) if n else None
            out.append({"bucket": f"{b}-{int(b) + 9}%", "n": n,
                        "hit": round(hit, 1) if hit is not None else None,
                        "said": said,
                        "dev": (round(hit - said, 1)
                                if hit is not None else None)})
        return out

    def grade_pending(self):
        """Resolve exited claims against Kalshi's real result.

        P&L was banked at the exit; this only decides whether the
        model's stated probability came true, so the calibration table
        covers every prediction we made rather than only the ones we
        chose to sit through."""
        now = time.time()
        for row in list(self.pend_calib):
            if row["close_ts"] > now - 60:
                continue
            try:
                m = _get(f"{KALSHI}/markets/{row['tk']}")["market"]
            except Exception:
                self.errs += 1
                continue
            res = (m.get("result") or "").lower()
            if res not in ("yes", "no"):
                if row["close_ts"] < now - 3600:
                    self.pend_calib.remove(row)
                continue
            b = str(int(min(0.99, max(0.0, row["p_side"])) * 10) * 10)
            c = self.calib.setdefault(b, [0, 0])
            c[0] += 1
            c[1] += 1 if res == row["side"] else 0
            self.pend_calib.remove(row)

    def settle_check(self):
        """Grade finished windows against KALSHI'S OWN RESULT.

        The proxy feed is never allowed to decide a P&L - it is only ever
        the thing being TESTED. Every settled window also writes one
        calibration row (did the model's stated probability come true?)
        and one proxy row (did our reference price agree with the
        exchange's line?). Those two tables, not the P&L, are the actual
        output of phase 0."""
        now = time.time()
        for tk, p in list(self.pos.items()):
            if p["close_ts"] > now - 60:
                continue                     # give settlement a minute
            try:
                m = _get(f"{KALSHI}/markets/{tk}")["market"]
            except Exception:
                self.errs += 1
                continue
            res = (m.get("result") or "").lower()
            if res not in ("yes", "no"):
                if p["close_ts"] < now - 3600:
                    self.pos.pop(tk, None)   # never resolved: drop it
                continue
            won = (res == p["side"])
            payout_c = 100.0 * p["n"] if won else 0.0
            pnl_c = payout_c - p["cost_c"] - p["fee_c"]
            self.realized_c += pnl_c
            self.stats["settled"] += 1
            self.stats["wins" if pnl_c > 0 else "losses"] += 1
            self.stats[p["lane"] + "_n"] = self.stats.get(
                p["lane"] + "_n", 0) + 1
            b = str(int(min(0.99, max(0.0, p["p_side"])) * 10) * 10)
            row = self.calib.setdefault(b, [0, 0])
            row[0] += 1
            row[1] += 1 if won else 0
            # a settlement is an exit at 100 (won) or 0 (lost), so the
            # same row builder describes it exactly
            self.settled.append(self._row(
                tk, p, p["n"], p["cost_c"] / max(1e-9, p["n"]),
                100.0 if won else 0.0, p["fee_c"], pnl_c,
                "WON" if won else "LOST", p.get("p_side"),
                {"t_left": p.get("t_left"), "result": res}))
            del self.settled[:-200]
            self.proxy_err.append({
                "tk": tk, "spot": p["spot_at_entry"],
                "strike": p["strike"],
                "d": round(p["spot_at_entry"] - p["strike"], 4),
                "res": res, "t_left": p.get("t_left")})
            del self.proxy_err[:-200]
            self.pos.pop(tk, None)

    # ---------------- reporting ----------------
    def _calib_table(self):
        out = []
        for b in sorted(self.calib, key=lambda x: int(x)):
            n, w = self.calib[b]
            out.append({"bucket": f"{b}-{int(b) + 9}%", "n": n,
                        "hit": round(100.0 * w / n, 1) if n else None})
        return out

    def _adverse(self):
        """Did the market move against us right after we were filled?
        The phantom book died on this number; the same clock rules here."""
        if not self.fills:
            return {"n": 0, "avg": None}
        rows = [f for f in self.fills if f.get("after") is not None]
        if not rows:
            return {"n": 0, "avg": None}
        return {"n": len(rows),
                "avg": round(sum(f["after"] for f in rows) / len(rows), 2)}

    def score_adverse(self, mkts):
        by_tk = {m["tk"]: m for m in mkts}
        for f in self.fills[-200:]:
            if f.get("after") is not None:
                continue
            m = by_tk.get(f["tk"])
            if not m or m.get("yes_bid") is None:
                continue
            if time.time() - f["ts"] < 20:
                continue
            mid = ((m["yes_bid"] or 0) + (m["yes_ask"] or 0)) / 2.0
            now_px = mid if f["side"] == "yes" else 100.0 - mid
            f["after"] = round(now_px - f["px"], 2)

    def _row(self, tk, pos, n, entry_c, exit_c, fee_c_tot, pnl_c,
             how, model_p, extra=None):
        """One ledger row, built the SAME way for every exit path.

        Adam asked for a ledger he can check by hand, so every row
        carries the whole arithmetic rather than a bare P&L:
            cost  = contracts x entry
            gross = contracts x (exit - entry)      [exit=100 or 0 on a
                                                     settlement]
            net   = gross - fees
        `how` names the ending in plain words - SOLD, WON, LOST,
        STOPPED - because "won: false" cannot distinguish a losing
        settlement from a deliberate stop, and those are different
        events with different lessons."""
        cost = entry_c * n
        gross = (exit_c - entry_c) * n
        row = {
            "tk": tk, "label": pos["label"],
            "lane": pos.get("lane"), "side": pos["side"],
            "avg": bool(pos.get("avg")),
            "n": round(n, 2),
            "entry": round(entry_c, 2),
            "exit": round(exit_c, 2),
            "cost": round(cost / 100.0, 2),
            "gross": round(gross / 100.0, 2),
            "fees": round(fee_c_tot / 100.0, 2),
            "pnl": round(pnl_c / 100.0, 2),
            "how": how,
            "won": pnl_c > 0,
            "model_p": round(model_p, 3) if model_p is not None else None,
            "hold_s": round(max(0.0, time.time() - _ts(pos.get("opened")))),
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        if extra:
            row.update(extra)
        # running total, so the ledger reconciles to the headline on the
        # page instead of asking anyone to add it up
        prev = self.settled[-1]["run"] if self.settled else 0.0
        row["run"] = round(prev + row["pnl"], 2)
        return row

    def _by_market(self):
        """P&L per MARKET (btc, eth, gold...), which is the split Adam
        actually reads - 'is the crypto lane working?' is a question the
        lane-level table cannot answer."""
        out = {}
        for r in self.settled:
            lab = r.get("label") or "?"
            m = out.setdefault(lab, {"n": 0, "w": 0, "pnl": 0.0,
                                     "fees": 0.0, "avg": False})
            m["n"] += 1
            m["w"] += 1 if r.get("won") else 0
            m["pnl"] = round(m["pnl"] + (r.get("pnl") or 0.0), 2)
            m["fees"] = round(m["fees"] + (r.get("fee") or 0.0), 2)
        for st, (_pair, lab) in CRYPTO.items():
            if lab in out:
                out[lab]["avg"] = True
        for lab, m in out.items():
            m["per_turn"] = round(m["pnl"] / m["n"], 3) if m["n"] else None
            m["hit"] = round(100.0 * m["w"] / m["n"], 0) if m["n"] else None
        # markets we are watching but have not traded yet still deserve a
        # row - an empty row is information, a missing row looks like a bug
        for st in list(SERIES) + list(CRYPTO):
            lab = (SERIES.get(st) or CRYPTO.get(st))[1]
            out.setdefault(lab, {"n": 0, "w": 0, "pnl": 0.0, "fees": 0.0,
                                 "avg": st in CRYPTO, "per_turn": None,
                                 "hit": None})
        return out

    def _lane_report(self):
        out = {}
        for s in self.settled:
            r = out.setdefault(s["lane"], {"n": 0, "w": 0, "pnl": 0.0,
                                           "fees": 0.0})
            r["n"] += 1
            r["w"] += 1 if s["won"] else 0
            r["pnl"] = round(r["pnl"] + s["pnl"], 2)
            r["fees"] = round(r["fees"] + s.get("fee", 0.0), 2)
        for r in out.values():
            r["per_turn"] = round(r["pnl"] / r["n"], 3) if r["n"] else None
        return out

    def _mark_book(self, mkts):
        """One place that decides what a held position is worth right
        now - so the positions table, the open-P&L headline and the exit
        logic can never quote three different marks for one position."""
        self._marks = {}
        for m in mkts:
            yb, ya = m.get("yes_bid"), m.get("yes_ask")
            if yb is None or ya is None:
                continue
            self._marks[m["tk"]] = (yb + ya) / 2.0

    def _open_pnl_c(self, mkts):
        by_tk = {m["tk"]: m for m in mkts}
        tot = 0.0
        for tk, p in self.pos.items():
            m = by_tk.get(tk)
            if not m or m.get("yes_bid") is None:
                tot -= p["fee_c"]
                continue
            mid = ((m["yes_bid"] or 0) + (m["yes_ask"] or 0)) / 2.0
            mk = mid if p["side"] == "yes" else 100.0 - mid
            tot += mk * p["n"] - p["cost_c"] - p["fee_c"]
        return tot

    def _clock(self):
        n = self.stats.get("settled", 0)
        return {"n": n, "goal": CLOCK_GOAL, "verdict_due": n >= CLOCK_GOAL}

    # ---------------- the cycle ----------------
    def step(self):
        """ORDER MATTERS (the phantom lesson): settle the window that
        just elapsed against the quotes that were actually resting during
        it, THEN post new quotes. A quote can only be hit by a print that
        happened after it existed - getting this backwards is the
        look-ahead bug that invalidated an entire phantom ledger."""
        now0 = time.time()
        proxy = self.fetch_proxy()
        mkts = self.fetch_markets()
        since = self._last_ts or (now0 - 120)
        trades = self.fetch_trades(since)
        self.check_fills(trades, since)
        # exits BEFORE settlement: a window that just closed should be
        # graded by the exchange, but one still open should get the
        # chance to be sold. Running these the other way round would
        # let a position expire that we had already decided to leave.
        self.observe_shadow(mkts, proxy)
        self.grade_shadow()
        self.check_exits(mkts, proxy)
        self.grade_pending()
        self.settle_check()
        quoted = self.quote(mkts, proxy)
        self.resting = dict(self.quotes)
        self._last_ts = now0
        self.score_adverse(mkts)
        self.stats["cycles"] += 1
        self.heartbeat()

        self._mark_book(mkts)
        for _tk, _p in self.pos.items():
            _p["_mid"] = self._marks.get(_tk)
        open_c = self._open_pnl_c(mkts)
        hrs = max(1e-9, (time.time() - self._t0) / 3600.0)
        total_c = self.realized_c + open_c
        rows = []
        for m in mkts:
            st = m["series"]
            pa = proxy.get(st)
            sig = self.sigma_s(st)
            _adj = self.adj_spot(st, pa[0]) if pa else None
            _tl = m["close_ts"] - now0
            if _adj is not None and sig:
                if m.get("avg"):
                    _ps, _pn = self.partial_avg(st, m["close_ts"])
                    p = avg_model_p(_ps, _pn, _adj, m["strike"], sig, _tl)
                else:
                    p = model_p(_adj, m["strike"], sig, _tl)
            else:
                p = None
            rows.append({
                "tk": m["tk"], "label": m["label"],
                "strike": m["strike"],
                "spot": round(_adj, 4) if _adj is not None else None,
                "raw_spot": round(pa[0], 4) if pa else None,
                "d": (round(_adj - m["strike"], 4)
                      if _adj is not None else None),
                "t_left": round(m["close_ts"] - now0),
                "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
                "depth": m.get("depth"),
                "model_p": round(p, 3) if p is not None else None,
                "quoted": m["tk"] in self.quotes,
                "avg": bool(m.get("avg")),
                "locked": (round(100.0 * self.partial_avg(
                    st, m["close_ts"])[1] / AVG_WINDOW_S, 0)
                    if m.get("avg") else None),
                "dead": self.proxy_dead(st)})
        state = {
            "updated": datetime.datetime.now().isoformat(timespec="seconds"),
            "era": ERA, "mode": "PAPER",
            "series": sorted(SERIES),
            "windows": rows,
            "quoted": quoted,
            "cycles": self.stats["cycles"],
            "hours": round(hrs, 2),
            "fills_strict": self.stats["fills_strict"],
            "fills_loose": self.stats["fills_loose"],
            "trades_seen": self.stats["trades_seen"],
            "open_n": len(self.pos),
            # OPEN trades get the SAME columns as closed ones. Adam
            # asked for the P&L of every trade, and a position we are
            # still holding is a trade - showing only settled rows hides
            # exactly the exposure that is live right now.
            "positions": [
                {"tk": p["tk"], "label": p["label"], "lane": p["lane"],
                 "side": p["side"], "n": round(p["n"], 2),
                 "avg": bool(p.get("avg")),
                 "entry": round(p["cost_c"] / max(1e-9, p["n"]), 2),
                 "cost": round(p["cost_c"] / 100.0, 2),
                 "mark": _mark_of(p),
                 "unreal": _unreal_of(p),
                 "fees": round(p["fee_c"] / 100.0, 2),
                 "held_s": round(max(0.0, time.time()
                                     - _ts(p.get("opened")))),
                 "t_left": round(max(0.0, p["close_ts"] - now0)),
                 "model_p": (round(p["p_side"], 3)
                             if p.get("p_side") is not None else None)}
                for p in self.pos.values()],
            "realized": round(self.realized_c / 100.0, 2),
            "open_pnl": round(open_c / 100.0, 2),
            "total": round(total_c / 100.0, 2),
            "settled_n": self.stats["settled"],
            "wins": self.stats["wins"], "losses": self.stats["losses"],
            "capital": round(self._capital_c() / 100.0, 2),
            "capital_max": round(BOOK_CAPITAL_C / 100.0, 2),
            # THE deliverable of phase 0. Not the P&L - the calibration.
            # If the model says 90% and 90% of them win, the edge is
            # real and sizing is the only remaining question. If it says
            # 90% and 70% win, the model is a liar and the lane dies.
            "calibration": self._calib_table(),
            "by_lane": self._lane_report(),
            "by_market": self._by_market(),
            "crypto_pnl": round(sum(
                m["pnl"] for lab, m in self._by_market().items()
                if m.get("avg")), 2),
            "metals_pnl": round(sum(
                m["pnl"] for lab, m in self._by_market().items()
                if not m.get("avg")), 2),
            "adverse": self._adverse(),
            "clock": self._clock(),
            "settled": self.settled[-60:][::-1],
            # totals computed from the SAME rows the table renders, so
            # the ledger and the headline can never disagree
            "ledger": {
                "n": len(self.settled),
                "contracts": round(sum(r.get("n") or 0
                                       for r in self.settled), 2),
                "gross": round(sum(r.get("gross") or 0
                                   for r in self.settled), 2),
                "fees": round(sum(r.get("fees") or 0
                                  for r in self.settled), 2),
                "net": round(sum(r.get("pnl") or 0
                                 for r in self.settled), 2),
                "how": {k: sum(1 for r in self.settled
                               if r.get("how") == k)
                        for k in ("SOLD", "WON", "LOST", "STOPPED")},
            },
            "exits": self.stats.get("exits", 0),
            "stops": self.stats.get("stops", 0),
            "trips": sum(self.trips.values()),
            "trip_capped": self.stats.get("trip_capped", 0),
            # CAPITAL TURNOVER - how many times the whole book recycles
            # per hour. Volume alone is vanity; turnover x edge is P&L.
            "turns_h": round(
                sum(self.trips.values())
                / max(1e-9, (time.time() - self._t0) / 3600.0), 2),
            "arbs": self.arbs[-8:][::-1],
            "arb_seen": self.stats.get("arb_seen", 0),
            "live_bp": {st: (round(self.liveness_bp(st), 2)
                             if self.liveness_bp(st) is not None else None)
                        for st in SERIES},
            "dead": sorted(st for st in SERIES if self.proxy_dead(st)),
            "crypto": sorted(CRYPTO),
            # our instrument's measured zero error, per series, in the
            # settlement feed's own units and in basis points
            "basis": {st: (round(self.basis_of(st), 5)
                           if self.basis_of(st) is not None else None)
                      for st in list(SERIES) + list(CRYPTO)},
            "basis_n": {st: len(self.basis.get(st) or [])
                        for st in list(SERIES) + list(CRYPTO)},
            "basis_backfill": self.stats.get("basis_backfill", 0),
            "fine_n": {st: len(self.fine.get(st) or []) for st in CRYPTO},
            "tape": {st: {"n": len(self.ticks.get(st) or []),
                          "span_min": round(
                              ((self.ticks.get(st) or [(0, 0)])[-1][0]
                               - (self.ticks.get(st) or [(0, 0)])[0][0])
                              / 60.0, 1)}
                     for st in list(SERIES) + list(CRYPTO)},
            "pair": self.pair_report(),
            "shadow": {"n": self.stats.get("shadow_n", 0),
                       "pending": len(self.shadow),
                       "at_s": SHADOW_AT_S,
                       "table": self.shadow_table()},
            "feed": {
                "ok": not self._feed_err,
                "err": self._feed_err,
                "keyed": bool(PYTH_KEY),
                "blocked_for_s": max(0, round(self._feed_block_until
                                              - time.time())),
                # what still works WITHOUT the price feed: everything
                # that reads Kalshi's own book
                "book_lanes_ok": True,
                # feeds this plan cannot see - named, not silently absent
                "unavailable": sorted(
                    lab for st, (fid, lab) in SERIES.items()
                    if fid in self._dead_ids)},
            "refuse": {k: self.stats.get(k, 0) for k in
                       ("no_vol", "no_proxy", "capped", "band_skip",
                        "no_edge", "proxy_dead", "no_basis")},
            "fills_taker": self.stats.get("fills_taker", 0),
            "rules": {"size": SIZE, "band": [MIN_PX_C, MAX_PX_C],
                      "fav_band": [FAV_MIN_C, FAV_MAX_C],
                      "fav_at_s": FAV_AT_S, "fav_veto": FAV_VETO_P,
                      "edge_c": EDGE_C, "endgame_s": ENDGAME_S,
                      "endgame_p": ENDGAME_P, "tail_p": TAIL_P,
                      "max_pos": MAX_POS, "maker_rate": MAKER_RATE,
                      "vol_n": MIN_VOL_N,
                      "exit_edge_c": EXIT_EDGE_C,
                      "min_live_bp": MIN_LIVE_BP,
                      "capital": BOOK_CAPITAL_C / 100.0,
                      "stop_p": STOP_P, "max_trips": MAX_TRIPS,
                      "endgame_s": ENDGAME_S},
            "vol": {st: (round(self.sigma_s(st), 5)
                         if self.sigma_s(st) else None) for st in SERIES},
            "ticks": {st: len(self.ticks.get(st) or []) for st in SERIES},
            "errs": self.errs,
        }
        self.pnl_days[datetime.date.today().isoformat()] = round(
            total_c / 100.0, 2)
        if len(self.pnl_days) > 60:
            for k in sorted(self.pnl_days)[:-60]:
                self.pnl_days.pop(k, None)
        state["pnl_days"] = dict(sorted(self.pnl_days.items())[-10:])
        _days = sorted(self.pnl_days.items())
        _delta, _prev = {}, 0.0
        for _d, _v in _days[-11:]:
            _delta[_d] = round(float(_v) - _prev, 2)
            _prev = float(_v)
        state["pnl_delta"] = dict(list(_delta.items())[-10:])
        self.last = state
        state["_mkts"] = [{"tk": m["tk"], "avg": m.get("avg"),
                           "close_ts": m["close_ts"]} for m in mkts]
        if self.rec is not None:
            try:
                self.rec.write({"ts": state["updated"], "kind": "tick",
                                "windows": rows, "quoted": quoted,
                                "total": state["total"]})
            except Exception:
                pass
        _pub = dict(state)
        _pub.pop("_mkts", None)      # loop-only, never persisted/published
        self.save(_pub)
        return state


    def burst_needed(self, mkts):
        """Is any crypto window inside its final minute right now?"""
        now = time.time()
        for m in mkts:
            if m.get("avg") and 0 < (m["close_ts"] - now) <= BURST_AT_S:
                return True
        return False

    def heartbeat(self):
        """Proof of life, written every cycle."""
        self._beat = time.time()
        return self._beat


ERRFILE = os.environ.get("TICK_ERRFILE",
                         os.path.join("logs", "tick_error.json"))
LOCK = os.environ.get("TICK_LOCK", os.path.join("logs", "tick.lock"))
LEASE_S = int(os.environ.get("TICK_LEASE_S", "120"))


def _lease_read():
    try:
        with open(LOCK) as f:
            return json.load(f)
    except Exception:
        return None


def take_lease(owner):
    """Single-writer lease over the tick ledger.

    WHY THIS EXISTS (8/28): kalshi-dashboard restarts on every deploy;
    kalshi-paper does not, and its tick thread had been dead 19 hours
    with new code unable to reach it. Hosting the worker in whichever
    process actually gets deployed fixes that - but two processes both
    writing one json ledger would corrupt it, which is a far worse
    failure than a stalled one.

    So: a lease, not a free-for-all. A process may claim it only if it
    is unheld or the holder has stopped refreshing for LEASE_S. The
    holder re-affirms every cycle and steps down the moment it finds
    someone else's name on it. At most one writer, always."""
    cur = _lease_read()
    now = time.time()
    if cur and cur.get("owner") != owner:
        if (now - float(cur.get("ts") or 0)) < LEASE_S:
            return False            # someone else is alive and writing
    try:
        os.makedirs(os.path.dirname(LOCK) or ".", exist_ok=True)
        with open(LOCK, "w") as f:
            json.dump({"owner": owner, "pid": os.getpid(), "ts": now}, f)
        return True
    except Exception:
        return False


def hold_lease(owner):
    """Refresh, or report that we have lost it."""
    cur = _lease_read()
    if cur and cur.get("owner") != owner:
        if (time.time() - float(cur.get("ts") or 0)) < LEASE_S:
            return False
    return take_lease(owner)


def start_thread(owner="paper"):
    """Run the book on its OWN clock, in a daemon thread.

    WHY THIS EXISTS: paper.py's main loop cycles every 90 seconds. A
    15-minute window would get ~10 observations and the endgame lane -
    whose entire thesis lives in the final five minutes - would get
    three. Measuring a seconds-clock edge on a 90-second sampler is how
    you conclude "no edge" when what you actually had was no resolution.

    A thread, not a systemd unit, deliberately: it ships with the
    existing deploy, needs nobody at the DigitalOcean console, and dies
    with its parent. This thread is the ONLY writer of the state file -
    paper.py must not also call step(), or two writers race over one
    json.dump."""
    import threading

    if not take_lease(owner):
        return None                 # another process already owns it

    def _loop():
        # BUILT AFTER THE THREAD DIED SILENTLY (8/27). It stopped for 29
        # minutes while the rest of paper.py kept running, and nothing
        # noticed - the exact failure I had built an alarm for on the
        # LIVE book that same morning, reproduced one file over. A
        # worker that can die quietly will.
        b = TickBook()
        sleep_s = int(os.environ.get("TICK_SLEEP", "20"))
        burst_s = float(os.environ.get("TICK_BURST_SLEEP", "2.0"))
        while True:
            try:
                if not hold_lease(owner):
                    # someone else took over; stand down cleanly rather
                    # than write a second time into one ledger
                    return
                st = b.step()
                # BURST MODE. The settlement average is the mean of SIXTY
                # ONE-SECOND PRINTS, so a 20-second sampler sees three of
                # them and the whole lock-in edge is invisible. Inside the
                # final minute we poll the free crypto feeds every ~1.5s -
                # cheap, keyless, and it is the difference between
                # measuring this edge and merely believing in it.
                if b.burst_needed(st.get("_mkts") or []):
                    # bounded, and it keeps beating throughout so a long
                    # burst can never be mistaken for a hang
                    t_end = time.time() + BURST_AT_S
                    while time.time() < t_end:
                        try:
                            b.fetch_crypto()
                            b.heartbeat()
                        except Exception:
                            b.errs += 1
                        time.sleep(burst_s)
                    continue
            except BaseException as e:
                # BaseException, not Exception: a bare Exception handler
                # still lets the thread die on anything outside that
                # tree, which is how a worker vanishes without a trace.
                b.errs += 1
                try:
                    b.heartbeat()
                except Exception:
                    pass
                # WRITE THE FAILURE DOWN. If step() raises before it can
                # save, the ledger simply freezes and looks identical to
                # a dead thread - which cost an hour of guessing on
                # 8/28. A separate file, so a broken cycle can never
                # damage a good ledger.
                try:
                    import traceback
                    os.makedirs(os.path.dirname(ERRFILE) or ".",
                                exist_ok=True)
                    with open(ERRFILE, "w") as f:
                        json.dump({
                            "ts": datetime.datetime.now().isoformat(
                                timespec="seconds"),
                            "owner": owner, "errs": b.errs,
                            "error": repr(e)[:300],
                            "where": traceback.format_exc()[-900:]}, f)
                except Exception:
                    pass
            time.sleep(sleep_s)

    t = threading.Thread(target=_loop, name="tick-" + owner,
                         daemon=True)
    t.start()
    return t


def main():                                          # pragma: no cover
    b = TickBook()
    while True:
        try:
            s = b.step()
            print(f"TICK: {len(s['windows'])} windows | {s['quoted']} quoted"
                  f" | open {s['open_n']} | settled {s['settled_n']}"
                  f" | total ${s['total']:+.2f}")
        except Exception as e:
            print("tick step failed:", e)
        time.sleep(int(os.environ.get("TICK_SLEEP", "20")))


if __name__ == "__main__":                           # pragma: no cover
    main()
