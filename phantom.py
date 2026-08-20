"""PHANTOM BOOK (8/20) - paper market-making across MLB + tennis props.

Adam's thesis, in his words: "I want to be the book for retail." Quote
BOTH sides of thousands of low-attention props, manufacture our own
overround (sell YES at 53 + sell NO at 53 = collect 106c on a dollar
that always pays exactly a dollar), and earn the spread on VOLUME
instead of being right about anything. This is what Susquehanna does on
this exchange; the long tail is where nobody competes with them.

PHASE 0: ZERO DOLLARS AT RISK. We post phantom two-sided quotes, then
watch the REAL tape to see whether those quotes would have filled, and
what the price did right afterwards. Three questions decide whether the
business exists:
  1. FLOW    - where do prints actually happen? (thousands of markets
               are listed; only some have customers)
  2. SPREAD  - how wide can we quote and still get hit?
  3. ADVERSE - after we're filled, does the price run us over? A maker
               who is filled only when he's wrong is not the house,
               he's the fish.
The headline KPI is the MATCH RATE: paired fills / total fills. A
matched pair is a locked spread that doesn't care who wins. Unmatched
inventory is a directional bet against someone who CHOSE to hit us -
and retail flow is one-sided by nature, so this number is the whole
argument.

FILL REALISM (the point of the exercise - paper fills usually lie):
  STRICT: a print traded THROUGH our price -> we would certainly have
          been filled, whatever our place in the queue.
  LOOSE:  a print traded AT our price -> queue-dependent, upper bound.
We publish both. STRICT is the number we believe; the gap between them
is the size of our uncertainty about queue position.

Fees follow Kalshi's published schedule: taker = ceil(0.07 x C x P x
(1-P)), maker approximately a quarter of that (and resting orders are
exempt on many markets, so this is the conservative end).

PAPER_PHANTOM=0 kills it. Nothing here can place an order: there is no
client, no key, and no code path to the trade endpoints.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import time

import requests

try:
    from recorder import Recorder
except Exception:
    Recorder = None

KALSHI = os.environ.get("PHANTOM_KALSHI",
                        "https://api.elections.kalshi.com/trade-api/v2")
STATE = os.environ.get("PHANTOM_STATE",
                       os.path.join("logs", "phantom_state.json"))
SERIES_TTL_S = int(os.environ.get("PHANTOM_SERIES_TTL", "21600"))
# the known-liquid spine: game and match lines print all day
CORE_SERIES = tuple(x for x in os.environ.get(
    "PHANTOM_CORE",
    "KXMLBGAME,KXMLBTOTAL,KXWTAMATCH,KXATPMATCH,"
    "KXATPCHALLENGERMATCH,KXWTACHALLENGERMATCH,KXATPSETWINNER,"
    "KXATPANYSET,KXATPTIEBREAK,KXATPACES,KXWTAACES").split(",") if x)
ROTATE_N = int(os.environ.get("PHANTOM_ROTATE", "30"))
MAX_SERIES = int(os.environ.get("PHANTOM_MAX_SERIES", "60"))
_SERIES_RX = re.compile(r"MLB|BASEBALL|ATP|WTA|TENNIS|USOPEN", re.I)
TRADE_PAGES = int(os.environ.get("PHANTOM_TRADE_PAGES", "4"))
ERA = "phantom1"

# --- quoting policy (all in cents) ---
# Only quote where the market is wide enough that stepping inside still
# leaves us an overround. 4c market spread -> we quote 2c wide.
MIN_SPREAD_C = int(os.environ.get("PHANTOM_MIN_SPREAD", "4"))
EDGE_C = int(os.environ.get("PHANTOM_EDGE", "1"))     # improve by this
MIN_PX_C = int(os.environ.get("PHANTOM_MIN_PX", "8"))
MAX_PX_C = int(os.environ.get("PHANTOM_MAX_PX", "92"))
SIZE = int(os.environ.get("PHANTOM_SIZE", "10"))      # phantom lots/side
MAX_QUOTES = int(os.environ.get("PHANTOM_MAX_QUOTES", "400"))
# 8/20 audit: in a 25c-wide book, stepping 1c inside each side posts a
# 23c-wide "quote" that no ordinary customer will ever cross - only
# someone who knows something will. Those fills are pure adverse
# selection and they poison the adverse metric. Real makers quote
# COMPETITIVELY or not at all, so cap our own width and let the extra
# room sit on the market's side of the spread.
MAX_WIDTH_C = int(os.environ.get("PHANTOM_MAX_WIDTH", "8"))
# 8/20 audit #2: quotes are re-posted every cycle with fresh size and
# nothing capped the RESULT, so one market accumulated 40 lots on one
# side over 13 cycles - a martingale, not a market. Real makers cap
# inventory per market AND per correlated cluster, then skew price
# rather than keep taking the same side. Without these caps the book
# measures gambling instead of making.
MAX_POS = int(os.environ.get("PHANTOM_MAX_POS", "20"))
MAX_CLUSTER_POS = int(os.environ.get("PHANTOM_MAX_CLUSTER", "40"))
# 8/20 build 3 (Adam: "why don't we include it in the actual book").
# 97% of prints happen in 1-3c books we were refusing on principle. So
# we quote them too - in the SAME book, one inventory - but every quote
# carries a LANE tag so the ledger can still answer which width pays.
# wide = step inside a >=4c market; tight = JOIN the touch of a 1-3c
# market and earn a penny on a hundred times the flow.
TIGHT_MIN_C = int(os.environ.get("PHANTOM_TIGHT_MIN", "1"))
# INVENTORY SKEW - the real fix for a 42% match rate. We were quoting
# symmetrically around the mid no matter what we held, so when retail
# kept selling us overs we kept buying at the same price forever. A
# book moves its line: as we get long, shade BOTH sides down so we are
# likelier to sell and less likely to add. Adam, 8/20: this is what
# turns "we got lucky on STL" into "we chose that exposure".
SKEW_MAX_C = int(os.environ.get("PHANTOM_SKEW_MAX", "3"))
# STALENESS - a fill is information. If the market has jumped away from
# where we quoted, a real maker has already cancelled; anyone still
# hitting us knows something. And after being hit, back off briefly.
STALE_C = int(os.environ.get("PHANTOM_STALE", "8"))
# 300s not 120s: phantom quotes refresh every ~3rd paper cycle, so a
# 120s cooldown always expired before the next quote and the back-off
# could never fire (widened read 0 on the first live pass).
HIT_COOLDOWN_S = int(os.environ.get("PHANTOM_HIT_COOLDOWN", "300"))
WIDEN_C = int(os.environ.get("PHANTOM_WIDEN", "2"))
# adverse-selection clocks
ADV_FAST_S = int(os.environ.get("PHANTOM_ADV_FAST", "300"))     # 5 min
ADV_SLOW_S = int(os.environ.get("PHANTOM_ADV_SLOW", "1800"))    # 30 min
TAKER_RATE = 0.07
MAKER_RATE = float(os.environ.get("PHANTOM_MAKER_RATE", "0.0175"))

_SPORTS = (
    ("mlb", re.compile(r"MLB|BASEBALL|INNING|STRIKEOUT|\bHOMER|"
                       r"\bRBI\b|\bRUNS?\b|PITCH", re.I)),
    ("tennis", re.compile(r"TENNIS|ATP|WTA|USOPEN|US OPEN|"
                          r"WIMBLEDON|ROLAND|\bACES?\b|TIEBREAK|"
                          r"\bSETS?\b", re.I)),
)


def sport(ticker, title=""):
    """Which of Adam's two starting sports is this? None = not ours."""
    blob = f"{ticker} {title}"
    for name, rx in _SPORTS:
        if rx.search(blob):
            return name
    return None


_EV_TAIL = re.compile(r"^(\d{2}[A-Z]{3}\d{2})(\d{3,4})?([A-Z]{4,10})$")
_SER_LABEL = {
    "KXMLBGAME": "winner", "KXMLBTOTAL": "total runs",
    "KXMLBF3": "first 3 innings", "KXMLBF5": "first 5 innings",
    "KXMLBFIRSTINNING": "first inning", "KXMLBSTATCOUNT": "stat count",
    "KXATPMATCH": "match", "KXWTAMATCH": "match",
    "KXATPCHALLENGERMATCH": "challenger match",
    "KXWTACHALLENGERMATCH": "challenger match",
    "KXATPSETWINNER": "set winner", "KXATPANYSET": "any set",
    "KXATPTIEBREAK": "tiebreak", "KXATPACES": "aces",
    "KXWTAACES": "aces",
}


def event_label(ev):
    """KXMLBTOTAL-26AUG201410SEAMIL -> 'SEA vs MIL - total runs'.
    Adam, 8/20: 'it doesn't show the real event'. A ticker is not a
    name; a cluster you can't read is a cluster you won't act on."""
    if not ev:
        return "-"
    parts = str(ev).split("-")
    ser = parts[0]
    label = _SER_LABEL.get(ser) or ser.replace("KX", "").lower()
    if len(parts) < 2:
        return label
    m = _EV_TAIL.match(parts[1])
    if not m:
        return f"{parts[1]} - {label}"
    teams = m.group(3)
    if len(teams) % 2 == 0:
        h = len(teams) // 2
        who = f"{teams[:h]} vs {teams[h:]}"
    else:
        who = teams
    return f"{who} - {label}"


def _cents(mk, base):
    """Kalshi dual schema: '<base>_dollars' string-floats or '<base>'
    int cents (same lesson as drift_live._kval / culture_scan._cents)."""
    v = mk.get(base + "_dollars")
    if v not in (None, ""):
        try:
            return int(round(float(v) * 100))
        except (TypeError, ValueError):
            pass
    v = mk.get(base)
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _num(v):
    """Kalshi returns volume/OI as int OR string depending on the field
    (the dual-schema lesson, third time now). Never let it reach a
    comparison as a str - that killed the first live scan."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fee_c(px_c, n, maker=True):
    """Kalshi fee in CENTS: ceil(rate x C x P x (1-P)) to the penny."""
    p = max(0.0, min(1.0, px_c / 100.0))
    rate = MAKER_RATE if maker else TAKER_RATE
    # round before ceil: 0.07*100*0.25*100 is 175.00000000000003 in
    # float, and a naive ceil would silently overcharge every trade
    return math.ceil(round(rate * n * p * (1.0 - p) * 100.0, 6))


def _ts(s):
    """Kalshi timestamps -> epoch seconds. Tolerant by design."""
    if not s:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    try:
        t = str(s).replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(t).timestamp()
    except Exception:
        return 0.0


class PhantomBook:
    """Two-sided quoting simulator. Holds no money and cannot trade."""

    def __init__(self):
        self.rec = Recorder() if Recorder else None
        self.quotes = {}      # tk -> {bid, ask, ts, mid, left_b, left_a}
        self.inv = {}         # tk -> {bn, bc, sn, sc, sport, event, title}
        self.fills = []       # rolling detail for adverse scoring
        self.stats = {"quoted": 0, "fills_strict": 0, "fills_loose": 0,
                      "fills_bid": 0, "fills_ask": 0, "pairs": 0,
                      "cycles": 0, "trades_seen": 0}
        self.last = {}
        self._seen = set()    # trade ids already scored
        self._series, self._ser_ts, self._rot = [], 0.0, 0
        self.hit_ts = {}      # tk -> when flow last ran into our quote
        self.hot = set()      # series the tape has shown printing
        self.flow = {"prints": 0, "contracts": 0, "in_ours": 0,
                     "by_spread": {}}
        self._t0 = time.time()
        self.load()

    # ---------------- persistence ----------------
    def load(self):
        try:
            d = json.load(open(STATE))
            if d.get("era") != ERA:
                return
            self.inv = d.get("inv") or {}
            self.stats.update(d.get("stats") or {})
            self.fills = (d.get("fills") or [])[-400:]
            self.hot = set(d.get("hot") or [])
            self.flow.update(d.get("flow") or {})
            self._t0 = d.get("t0") or self._t0
        except Exception:
            pass

    def save(self, state):
        try:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            state["inv"] = self.inv
            state["fills"] = self.fills[-400:]
            state["hot"] = sorted(self.hot)
            # 8/20 caught live: inventory persisted across a restart but
            # the fill COUNTERS did not, so match_rate read 0.0 while 40
            # contracts sat paired. A KPI that resets on deploy is worse
            # than no KPI - it lies quietly in the right direction.
            state["stats"] = self.stats
            state["t0"] = self._t0
            json.dump(state, open(STATE, "w"))
        except Exception:
            pass

    # ---------------- the surface ----------------
    def fetch_series(self):
        """Every MLB/tennis series on the exchange, cached. Kalshi lists
        ~3,500 sports series; ours is the subset that matches Adam's two
        starting sports."""
        if (time.time() - self._ser_ts) < SERIES_TTL_S and self._series:
            return self._series
        out = []
        try:
            d = requests.get(KALSHI + "/series",
                             params={"category": "Sports"},
                             timeout=25).json()
            for s in d.get("series") or []:
                tk = s.get("ticker") or ""
                if _SERIES_RX.search(tk):
                    out.append(tk)
        except Exception:
            pass
        if out:
            self._series = sorted(set(out))
            self._ser_ts = time.time()
        return self._series

    def targets(self):
        """Which series to price this cycle. CORE is the known-liquid
        spine (game/match lines), HOT is anything the tape has shown
        printing in our sports - flow earns a permanent slot - and the
        rest rotates so the whole surface gets seen over time."""
        ser = self.fetch_series()
        picked = list(CORE_SERIES)
        for s in sorted(self.hot):
            if s not in picked:
                picked.append(s)
        rest = [s for s in ser if s not in picked]
        if rest:
            i = self._rot % len(rest)
            picked.extend((rest + rest)[i:i + ROTATE_N])
            self._rot = (self._rot + ROTATE_N) % max(1, len(rest))
        return picked[:MAX_SERIES]

    def fetch_markets(self):
        """Open MLB + tennis markets with their books, fetched SERIES BY
        SERIES. The /events sweep looked cheaper but buried today's games
        behind years of long-dated prospect props - the same failure the
        crypto book hit in August, same fix: ask for the series."""
        out, hit = [], 0
        for st in self.targets():
            try:
                d = requests.get(KALSHI + "/markets",
                                 params={"series_ticker": st,
                                         "status": "open", "limit": 500},
                                 timeout=20).json()
            except Exception:
                continue
            mks = d.get("markets") or []
            if mks:
                hit += 1
            for mk in mks:
                tk = mk.get("ticker") or ""
                ttl = mk.get("title") or ""
                sp = sport(tk, ttl) or sport(st, "")
                if sp is None:
                    continue
                out.append({
                    "tk": tk, "event": mk.get("event_ticker") or st,
                    "series": st, "title": ttl[:90], "sport": sp,
                    "yb": _cents(mk, "yes_bid"),
                    "ya": _cents(mk, "yes_ask"),
                    "vol": _num(mk.get("volume") or mk.get("volume_fp")),
                    "oi": _num(mk.get("open_interest")
                               or mk.get("open_interest_fp"))})
        return out, hit

    def score_flow(self, trades, mkts):
        """WHERE ARE THE CUSTOMERS? The tension at the heart of this
        thesis is that wide spreads and real flow may live in different
        markets. So every print in our sports gets bucketed by the
        spread of the book it happened in, and by whether we were
        quoting there. If the prints are all in 1c books, being the
        book in the long tail is a fantasy and we'll know within days."""
        idx = {m["tk"]: m for m in mkts}
        for t in trades:
            m = idx.get(t.get("ticker"))
            if m is None:
                continue
            self.flow["prints"] += 1
            self.flow["contracts"] += int(
                _num(t.get("count_fp") or t.get("count")))
            if m["tk"] in self.quotes:
                self.flow["in_ours"] += 1
            ser = m.get("series")
            if ser:
                self.hot.add(ser)
            if m["yb"] and m["ya"]:
                sp = m["ya"] - m["yb"]
                b = ("1-3" if sp <= 3 else "4-7" if sp <= 7
                     else "8-14" if sp <= 14 else "15+")
                self.flow["by_spread"][b] = (
                    self.flow["by_spread"].get(b, 0) + 1)

    # ---------------- the quote ----------------
    def quote(self, mkts):
        """Post a phantom two-sided quote wherever the market is wide
        enough to step inside and still keep an overround. We never
        cross, never quote the extremes (collateral is brutal and the
        fee math is silly out there), and never quote a one-sided
        book - there is nothing to be the other side OF."""
        self.quotes = {}
        quotable = 0
        now = time.time()
        for m in sorted(mkts, key=lambda r: -_num(r.get("vol"))):
            yb, ya = m["yb"], m["ya"]
            if not yb or not ya or ya <= yb:
                continue
            if yb < MIN_PX_C or ya > MAX_PX_C:
                continue
            spread = ya - yb
            if spread < TIGHT_MIN_C:
                continue
            lane = "wide" if spread >= MIN_SPREAD_C else "tight"
            quotable += 1
            if len(self.quotes) >= MAX_QUOTES:
                continue
            if lane == "wide":
                bid, ask = yb + EDGE_C, ya - EDGE_C
                if ask - bid < 2:      # no overround left after stepping in
                    continue
                if ask - bid > MAX_WIDTH_C:
                    # tighten toward the mid until we're a real quote
                    mid0 = (yb + ya) / 2.0
                    bid = int(round(mid0 - MAX_WIDTH_C / 2.0))
                    ask = bid + MAX_WIDTH_C
                    self.stats["tightened"] = (
                        self.stats.get("tightened", 0) + 1)
            else:
                bid, ask = yb, ya      # JOIN the touch, don't cross it
            if now - self.hit_ts.get(m["tk"], 0.0) < HIT_COOLDOWN_S:
                bid -= WIDEN_C
                ask += WIDEN_C
                self.stats["widened"] = self.stats.get("widened", 0) + 1
            sk = self._skew(m["tk"])
            bid -= sk
            ask -= sk
            # never cross, never post a marketable order
            bid = max(1, min(bid, ya - 1))
            ask = min(99, max(ask, yb + 1))
            if ask <= bid:
                continue
            self.quotes[m["tk"]] = {
                "bid": bid, "ask": ask, "ts": now, "lane": lane,
                "mid": (yb + ya) / 2.0, "left_b": SIZE, "left_a": SIZE,
                "sport": m["sport"], "event": m["event"], "skew": sk,
                "title": m["title"], "mspread": spread}
        self.stats["quoted"] += len(self.quotes)
        return quotable

    # ---------------- the tape ----------------
    def fetch_trades(self, since_s):
        """Real prints across the exchange. One paginated sweep beats
        one call per ticker when we're watching hundreds of books."""
        out, cursor = [], None
        try:
            for _ in range(TRADE_PAGES):
                params = {"limit": 1000}
                if since_s:
                    params["min_ts"] = int(since_s)
                if cursor:
                    params["cursor"] = cursor
                d = requests.get(KALSHI + "/markets/trades",
                                 params=params, timeout=20).json()
                got = d.get("trades") or []
                out.extend(got)
                cursor = d.get("cursor")
                if not cursor or not got:
                    break
        except Exception:
            pass
        return out

    def check_fills(self, trades):
        """Would our resting quotes have been hit? taker_side tells us
        which side of the book the print consumed:
          taker BOUGHT yes at p -> lifted offers -> our ask fills if
                                   p >= ask (STRICT when p > ask)
          taker SOLD  yes at p  -> hit bids     -> our bid fills if
                                   p <= bid (STRICT when p < bid)
        """
        n_new = 0
        for t in trades:
            tid = t.get("trade_id") or t.get("id")
            if tid in self._seen:
                continue
            tk = t.get("ticker")
            q = self.quotes.get(tk)
            if q is None:
                continue
            self._seen.add(tid)
            self.stats["trades_seen"] += 1
            px = _cents(t, "yes_price")
            if px is None:
                continue
            # count arrives as count_fp, a STRING ("55.00"). Reading
            # the wrong field here would have meant zero fills forever
            # and a silent, confident, empty experiment - the same
            # class of bug as the sports book that placed nothing for
            # a week. Counters are seeded so silence never reads as
            # health.
            cnt = int(_num(t.get("count_fp") or t.get("count")))
            if cnt <= 0:
                continue
            # block trades are negotiated off-book: they would never
            # have lifted our resting order, so they are flow, not fills
            if t.get("is_block_trade"):
                self.flow["blocks"] = self.flow.get("blocks", 0) + 1
                continue
            if abs(px - q["mid"]) > STALE_C:
                # the market has jumped past where we quoted: a real
                # maker cancelled long ago, and whoever is still lifting
                # this price knows something we don't
                self.stats["stale_skipped"] = (
                    self.stats.get("stale_skipped", 0) + 1)
                continue
            side = (t.get("taker_side") or "").lower()
            if not self._room(tk, q, side):
                self.stats["pos_capped"] = self.stats.get(
                    "pos_capped", 0) + 1
                continue
            if side == "yes" and px >= q["ask"] and q["left_a"] > 0:
                n = min(cnt, q["left_a"], self._headroom(tk, -1))
                q["left_a"] -= n
                if n <= 0:
                    continue
                self._book_fill(tk, q, "sell", q["ask"], n,
                                strict=(px > q["ask"]))
                n_new += 1
            elif side == "no" and px <= q["bid"] and q["left_b"] > 0:
                n = min(cnt, q["left_b"], self._headroom(tk, 1))
                q["left_b"] -= n
                if n <= 0:
                    continue
                self._book_fill(tk, q, "buy", q["bid"], n,
                                strict=(px < q["bid"]))
                n_new += 1
        if len(self._seen) > 40000:
            self._seen = set(list(self._seen)[-20000:])
        return n_new

    def _skew(self, tk):
        """Cents to shift BOTH quotes by, given what we're holding.
        Long -> shade down (sell easier, buy harder). This is a book
        moving its line, and it is the structural answer to one-sided
        inventory."""
        r = self.inv.get(tk)
        if not r:
            return 0
        net = r["bn"] - r["sn"]
        if not net:
            return 0
        return int(round(SKEW_MAX_C * max(-1.0, min(1.0,
                                                    net / float(MAX_POS)))))

    def _headroom(self, tk, direction):
        """Contracts we may still add in this direction before the cap."""
        r = self.inv.get(tk)
        net = (r["bn"] - r["sn"]) if r else 0
        if net * direction < 0:        # reducing: always allowed
            return SIZE
        return max(0, MAX_POS - abs(net))

    def _room(self, tk, q, taker_side):
        """Would this fill push us past the inventory cap? A taker
        buying yes makes us SHORT; a taker selling makes us LONG."""
        r = self.inv.get(tk)
        net = (r["bn"] - r["sn"]) if r else 0
        adding = -1 if taker_side == "yes" else 1
        if abs(net + adding) > MAX_POS and (net * adding) >= 0:
            return False
        ev = q.get("event")
        if ev:
            cl = sum(abs(v["bn"] - v["sn"]) for v in self.inv.values()
                     if v.get("event") == ev)
            if cl >= MAX_CLUSTER_POS and (net * adding) >= 0:
                return False
        return True

    def _book_fill(self, tk, q, side, px, n, strict):
        self.hit_ts[tk] = time.time()
        rec = self.inv.setdefault(tk, {
            "bn": 0, "bc": 0, "sn": 0, "sc": 0, "sport": q["sport"],
            "event": q["event"], "title": q["title"],
            "lane": q.get("lane", "wide")})
        rec["lane"] = q.get("lane", rec.get("lane", "wide"))
        if side == "buy":
            rec["bn"] += n
            rec["bc"] += px * n
            self.stats["fills_bid"] += 1
        else:
            rec["sn"] += n
            rec["sc"] += px * n
            self.stats["fills_ask"] += 1
        self.stats["fills_strict" if strict else "fills_loose"] += 1
        self.fills.append({"tk": tk, "side": side, "px": px, "n": n,
                           "ts": time.time(), "mid": q["mid"],
                           "strict": bool(strict), "sport": q["sport"],
                           "lane": q.get("lane", "wide"),
                           "adv_f": None, "adv_s": None})
        if self.rec is not None:
            self.rec.event("phantom_fill", tk=tk, side=side, px=px,
                           n=n, strict=bool(strict))

    # ---------------- the verdict ----------------
    def score_adverse(self, mkts):
        """Where did the price go AFTER we were filled? For a buy,
        favourable = mid up; for a sell, favourable = mid down. Negative
        average = we're being picked off, and no spread saves us."""
        mid = {}
        for m in mkts:
            if m["yb"] and m["ya"]:
                mid[m["tk"]] = (m["yb"] + m["ya"]) / 2.0
        now = time.time()
        for f in self.fills:
            m = mid.get(f["tk"])
            if m is None:
                continue
            age = now - f["ts"]
            drift = (m - f["mid"]) * (1 if f["side"] == "buy" else -1)
            if f["adv_f"] is None and age >= ADV_FAST_S:
                f["adv_f"] = round(drift, 2)
            if f["adv_s"] is None and age >= ADV_SLOW_S:
                f["adv_s"] = round(drift, 2)

    def book(self, mkts):
        """Match what we can, mark what we can't. This is the P&L a
        real maker would report: locked spread on paired inventory,
        marked exposure on the residual."""
        mid = {}
        for m in mkts:
            if m["yb"] and m["ya"]:
                mid[m["tk"]] = (m["yb"] + m["ya"]) / 2.0
        pairs = locked_c = unmatched_n = unreal_c = 0
        fees_c = 0
        clusters, rows = {}, []
        lanes = {}
        for tk, r in self.inv.items():
            bn, sn = r["bn"], r["sn"]
            matched = min(bn, sn)
            m_locked = m_unreal = 0.0
            if matched:
                abuy = r["bc"] / bn
                asell = r["sc"] / sn
                gross = (asell - abuy) * matched
                f = (fee_c(abuy, matched) + fee_c(asell, matched))
                pairs += matched
                m_locked = gross - f
                locked_c += m_locked
                fees_c += f
            net = bn - sn
            if net:
                unmatched_n += abs(net)
                m = mid.get(tk)
                if m is not None:
                    ref = (r["bc"] / bn) if net > 0 else (r["sc"] / sn)
                    m_unreal = (m - ref) * net
                    unreal_c += m_unreal
            ln = lanes.setdefault(r.get("lane", "wide"),
                                  {"pairs": 0, "unmatched": 0,
                                   "spread_c": 0.0, "risk_c": 0.0,
                                   "mkts": 0})
            ln["pairs"] += matched
            ln["unmatched"] += abs(net)
            ln["spread_c"] += m_locked
            ln["risk_c"] += m_unreal
            ln["mkts"] += 1
            ev = r.get("event") or tk
            c = clusters.setdefault(ev, {"net": 0, "strikes": 0,
                                         "pnl_c": 0.0})
            c["net"] += abs(net)
            c["strikes"] += 1
            c["pnl_c"] += m_locked + m_unreal
            rows.append({
                "tk": tk, "title": r.get("title") or tk,
                "sport": r.get("sport"), "event": event_label(ev),
                "bn": bn, "sn": sn, "net": net,
                "px": round((r["bc"] / bn) if net > 0 else
                            ((r["sc"] / sn) if net < 0 else
                             (r["bc"] / bn if bn else 0)), 1),
                "mid": round(mid.get(tk), 1) if mid.get(tk) else None,
                "pnl": round((m_locked + m_unreal) / 100, 2)})
        self.stats["pairs"] = pairs
        top = sorted(clusters.items(), key=lambda kv: -kv[1]["net"])[:8]
        rows.sort(key=lambda r: abs(r["pnl"]), reverse=True)
        return {"pairs": pairs, "locked_c": round(locked_c, 1),
                "fees_c": fees_c, "unmatched": unmatched_n,
                "unreal_c": round(unreal_c, 1),
                "positions": rows[:14], "lanes": lanes,
                "clusters": [{"event": event_label(k), "ticker": k,
                              "net": v["net"], "strikes": v["strikes"],
                              "pnl": round(v["pnl_c"] / 100, 2)}
                             for k, v in top],
                "cluster_n": len(clusters)}

    def _lane_report(self, lanes):
        """Which WIDTH is the business? Same book, same inventory - the
        tag is what lets the ledger answer. wide = we stepped inside a
        4c+ market; tight = we joined the touch of a 1-3c one, where
        97% of the exchange's prints actually happen."""
        q = {}
        for tk, x in self.quotes.items():
            ln = x.get("lane", "wide")
            q[ln] = q.get(ln, 0) + 1
        adv = {}
        for f in self.fills:
            if f.get("adv_f") is None:
                continue
            a = adv.setdefault(f.get("lane", "wide"), [])
            a.append(f["adv_f"])
        out = {}
        for ln in set(list(lanes) + list(q)):
            d = lanes.get(ln) or {"pairs": 0, "unmatched": 0,
                                  "spread_c": 0.0, "risk_c": 0.0,
                                  "mkts": 0}
            ct = 2 * d["pairs"] + d["unmatched"]
            av = adv.get(ln) or []
            out[ln] = {
                "quoted": q.get(ln, 0), "mkts": d["mkts"],
                "pairs": d["pairs"], "unmatched": d["unmatched"],
                "match": round(2.0 * d["pairs"] / ct, 3) if ct else None,
                "spread": round(d["spread_c"] / 100, 2),
                "risk": round(d["risk_c"] / 100, 2),
                "per_pair_c": (round(d["spread_c"] / d["pairs"], 2)
                               if d["pairs"] else None),
                "adverse": round(sum(av) / len(av), 2) if av else None,
                "adv_n": len(av)}
        return out

    def _adverse_summary(self):
        out = {}
        for key, fld in (("fast", "adv_f"), ("slow", "adv_s")):
            vals = [f[fld] for f in self.fills if f.get(fld) is not None]
            out[key] = {"n": len(vals),
                        "avg": round(sum(vals) / len(vals), 2)
                        if vals else None}
        return out

    def _spread_profile(self, mkts):
        """What the surface looks like: how many books sit at each
        width. Pairs with flow.by_spread to answer the only question
        that matters early - do the wide books have customers?"""
        prof = {}
        for m in mkts:
            if not (m["yb"] and m["ya"]):
                continue
            sp = m["ya"] - m["yb"]
            b = ("1-3" if sp <= 3 else "4-7" if sp <= 7
                 else "8-14" if sp <= 14 else "15+")
            prof[b] = prof.get(b, 0) + 1
        return prof

    # ---------------- the cycle ----------------
    def step(self):
        mkts, series_hit = self.fetch_markets()
        quotable = self.quote(mkts)
        since = time.time() - 900
        trades = self.fetch_trades(since)
        self.check_fills(trades)
        self.score_flow(trades, mkts)
        self.score_adverse(mkts)
        bk = self.book(mkts)
        self.stats["cycles"] += 1
        fs, fl = self.stats["fills_strict"], self.stats["fills_loose"]
        tot_fills = fs + fl
        # match rate: how much of our filled flow paired off. THE number.
        fb, fa = self.stats["fills_bid"], self.stats["fills_ask"]
        # 8/20 audit: the event-weighted rate treats a 3-lot fill and a
        # 100-lot fill as equals and read 67% while the book was only
        # 39% paired BY CONTRACT. Contracts are what carry risk, so the
        # contract-weighted number is the headline and the event one is
        # kept beside it.
        match_rate = (2.0 * min(fb, fa) / (fb + fa)) if (fb + fa) else None
        tot_ct = 2 * bk["pairs"] + bk["unmatched"]
        match_ct = (2.0 * bk["pairs"] / tot_ct) if tot_ct else None
        hrs = max(1e-9, (time.time() - self._t0) / 3600.0)
        _pos = {r["tk"]: r for r in bk["positions"]}
        by_sport = {}
        for m in mkts:
            by_sport[m["sport"]] = by_sport.get(m["sport"], 0) + 1
        state = {
            "updated": datetime.datetime.now().isoformat(
                timespec="seconds"),
            "era": ERA, "mode": "PHANTOM",
            "scanned": len(mkts), "by_sport": by_sport,
            "quotable": quotable, "quoted": len(self.quotes),
            "series_hit": series_hit, "series_known": len(self._series),
            "hot_series": len(self.hot), "flow": dict(self.flow),
            "fills_strict": fs, "fills_loose": fl,
            "fills_bid": fb, "fills_ask": fa,
            "match_rate": (round(match_ct, 3)
                           if match_ct is not None else None),
            "match_events": (round(match_rate, 3)
                             if match_rate is not None else None),
            "contracts": tot_ct,
            "pairs": bk["pairs"], "locked": round(bk["locked_c"] / 100, 2),
            "fees": round(bk["fees_c"] / 100, 2),
            "unmatched": bk["unmatched"],
            "unreal": round(bk["unreal_c"] / 100, 2),
            "net": round((bk["locked_c"] + bk["unreal_c"]) / 100, 2),
            # THE distinction: spread earned by BEING THE BOOK vs the
            # mark on unpaired inventory, which is a directional bet we
            # did not choose. A headline that mixes them will report a
            # coin flip as a strategy.
            "spread_pnl": round(bk["locked_c"] / 100, 2),
            "risk_pnl": round(bk["unreal_c"] / 100, 2),
            "per_pair_c": (round(bk["locked_c"] / bk["pairs"], 2)
                           if bk["pairs"] else None),
            "pos_capped": self.stats.get("pos_capped", 0),
            "stale_skipped": self.stats.get("stale_skipped", 0),
            "widened": self.stats.get("widened", 0),
            "tightened": self.stats.get("tightened", 0),
            "clusters": bk["clusters"], "cluster_n": bk["cluster_n"],
            "adverse": self._adverse_summary(),
            "by_lane": self._lane_report(bk["lanes"]),
            "spreads": self._spread_profile(mkts),
            "trades_seen": self.stats["trades_seen"],
            "cycles": self.stats["cycles"],
            "hours": round(hrs, 2),
            "fills_per_h": round(tot_fills / hrs, 1),
            "rules": {"min_spread": MIN_SPREAD_C, "edge": EDGE_C,
                      "size": SIZE, "band": [MIN_PX_C, MAX_PX_C],
                      "max_quotes": MAX_QUOTES,
                      "max_width": MAX_WIDTH_C,
                      "max_pos": MAX_POS, "max_cluster": MAX_CLUSTER_POS,
                      "skew_max": SKEW_MAX_C, "stale": STALE_C,
                      "maker_rate": MAKER_RATE},
            "positions": bk["positions"],
            "examples": [
                {"tk": tk, "title": q["title"], "sport": q["sport"],
                 "bid": q["bid"], "ask": q["ask"],
                 "mspread": q["mspread"],
                 "event": event_label(q.get("event")),
                 "net": _pos.get(tk, {}).get("net", 0),
                 "pnl": _pos.get(tk, {}).get("pnl", 0.0)}
                for tk, q in list(self.quotes.items())[:10]],
        }
        self.last = state
        if self.rec is not None:
            self.rec.write({"ts": state["updated"], "kind": "phantom",
                            "quotes": [[tk, q["bid"], q["ask"],
                                        q["sport"]]
                                       for tk, q in self.quotes.items()],
                            "match_rate": state["match_rate"],
                            "pairs": bk["pairs"]})
        self.save(dict(state))
        return state
