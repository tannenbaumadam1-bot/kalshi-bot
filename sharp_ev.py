#!/usr/bin/env python3
"""+EV sharp-line anchoring - PAPER book (era "ev1-sharp").

Fair value = devigged sharp-sportsbook consensus (Pinnacle when present, else
median of >=MIN_BOOKS books) from The Odds API, shrunk toward the Kalshi mid.
Buy the Kalshi side priced far enough below fair to clear fees with margin.
No forecasting model; exchanges don't limit winners, so the classic +EV death
at soft books doesn't apply.

Deliberate scope (the "smart" filters - each one earned by a past loss):
- NO longshots: fair prob and entry price both confined to 20-80.
- NO in-play: bet window is [start-24h, start-10min]. Lines are sharpest and
  Kalshi most liquid near start; in-play is a latency war we skip.
- NO soccer (3-way draw mismatch), NO props/futures (soft or capital-locking),
  NO games we can't date+team match EXACTLY between feed and ticker.
- Sharp disagreement guard: if devigged book probs disagree > DISAGREE_MAX,
  the "fair" value isn't trustworthy - skip.
- Shrinkage (weather lesson: overconfidence poisons Kelly): fair is blended
  FAIR_W sharp + (1-FAIR_W) Kalshi mid, so we only act on big dislocations.
- Maker-only entries (join the bid): Kalshi maker fee rounds to ~$0 at our
  size vs taker 7c*P*(1-P) (~1.75c at 50c = ~3.5% of basis - fatal to a 3c edge).
- REALISTIC FILLS (v2, 7/10): a placed order RESTS in self.pending and only
  becomes a position when the market trades through our price (last_price at or
  through us, or the book crosses us); unfilled orders CANCEL at the lockout.
  This models the adverse selection a real resting order eats - the old
  "instant fill at the bid" sim was an upper bound that could pass the gate on
  fills that would never happen.
- One bet per game, MAX_PER_DAY/day, MAX_OPEN open, probe stakes (<=60c/bet)
  until the 30-bet calibration gate passes (same contract as the weather book).

Markets: moneyline (h2h) + game totals (Kalshi totals use half-point strikes,
e.g. 'Over 8.5 runs' = book Over 8.5 exactly - no push risk). Both sides
tradeable (YES=team/over, NO=other/under). Spreads/halves/props: later phases -
Kalshi period markets are in-game (excluded by the no-in-play rule) and prop
anchors carry the widest vig; totals+ml are where sharp anchors are strongest.

Odds source: The Odds API, region 'eu' only = Pinnacle + Euro sharps (Pinnacle
preferred when present; median >=MIN_BOOKS fallback).

Credit discipline (v2, 7/10 - the free tier is 500/mo and running dry mid-month
silently idles the whole strategy):
- x-requests-remaining response headers are tracked and shown on the dashboard.
- A sport is only scanned if Kalshi has a QUOTED game inside the bet window
  (ticker-parsed, free) - kills the NFL-preseason burn (quoted futures months
  out => 2 credits/scan for 0 evaluable markets).
- Scan interval adapts to the remaining monthly budget (credits left / days
  left), clamped [1h, 24h].
- BURST scans: when ahead of credit pace and a matched game starts within
  ~100min, rescan after >=45min - edges cluster in the last look before start.

Shadow calibration (v2, 7/10): every band-qualifying edge was already logged to
logs/sharpev_shadow.csv; now a daily joiner fetches outcomes into
sharpev_shadow_res.csv and buckets realized win rate by edge size - ~30x more
calibration data per day than actual bets. Judge the anchor on this table.

v3 (7/13) - the "big edges lose" fixes, each earned by 34 settled + 1938 shadow
rows (realized 2.0-2.5c edges: 3W/14L; shadow 2-3c: act 16.7% vs fair 42.2%):
- EDGE CEILING (SEV_MAX_EDGE_C, 2.0c): the tradeable band is
  [PROBE_MIN_EDGE_C, MAX_EDGE_C) in both gate modes - when Kalshi disagrees
  hard with the sharp books, Kalshi has been right; a big "edge" is our fair
  being stale/wrong, not mispricing. The gate changes sizing, never the band.
- ODDS-AGE GATE (SEV_MAX_ODDS_AGE_MIN, 30m): candidates anchored to a stale
  book line are skipped; every shadow row now logs odds_age_m so stale-line
  artifacts can be separated from true adverse selection as data grows.
- PENDING REVALIDATION: each scan recomputes fair for every RESTING order and
  cancels it when the edge fell below SEV_CANCEL_EDGE_C (0.5c) or blew past
  the ceiling - a resting order priced off an old fair is a free option for
  informed flow (we get filled exactly when we're wrong).
- REST-TIME CAP (SEV_MAX_REST_H, 2h): orders also expire MAX_REST_H after
  placement, not just at the game lockout.

Without ODDS_API_KEY the module idles gracefully.
"""
from __future__ import annotations
import os, json, csv, re, calendar, datetime, statistics
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = datetime.timezone(datetime.timedelta(hours=-4))
import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ODDS = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
SSIM = os.path.join("logs", "sharpev_sim.json")
SSTATE = os.path.join("logs", "sharpev_state.json")
SLOG = os.path.join("logs", "sharpev_bets.csv")
SSHADOW = os.path.join("logs", "sharpev_shadow.csv")   # every evaluated edge, bet or not
SSHADOWR = os.path.join("logs", "sharpev_shadow_res.csv")  # shadow rows + outcome

ERA = "ev3-band"            # current strategy: thin [1.5,2.0)c band + stale-odds gate + rest cap
V3_TS = "2026-07-13T15:00"  # v3 ship time (UTC); bets placed before = legacy "ev1-sharp"


def era_of(b):
    """Resolve a bet's strategy era. Rows placed pre-7/13 carry era 'ev1-sharp';
    rows placed after the v3 ship (band ceiling, stale-odds gate, pending
    revalidation, 2h rest cap) are the current strategy regardless of tag."""
    if b.get("era") == ERA:
        return ERA
    return ERA if (b.get("ots") or "") >= V3_TS else "ev1-sharp"


def _era_stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0, "wins": 0, "losses": 0, "net": 0.0,
                "expectancy": None, "pred": None, "actual": None}
    wins = sum(1 for b in rows if b.get("outcome") == 1)
    net = sum(float(b.get("pnl", 0) or 0) for b in rows)
    pred = sum(float(b.get("pside", 0) or 0) for b in rows) / n
    return {"n": n, "wins": wins, "losses": n - wins, "net": round(net, 2),
            "expectancy": round(net / n, 3), "pred": round(100 * pred, 1),
            "actual": round(100 * wins / n, 1)}
START_CENTS = int(os.environ.get("SEV_START_C", "10000"))
MIN_EDGE_C = float(os.environ.get("SEV_MIN_EDGE_C", "4"))      # net edge to act (post-gate)
# Probe mode uses a LOWER bar: maker fee ~$0 and stakes <=60c mean any positive
# shrunk edge is +EV in expectation; probing exists to COLLECT calibration data.
# 1.5c shrunk = ~2.1c raw sharp-vs-mid disagreement. The realistic fill sim
# (cancel-at-lockout, trade-through fills) is what makes a low bar safe to run.
PROBE_MIN_EDGE_C = float(os.environ.get("SEV_PROBE_MIN_EDGE_C", "1.5"))
# v3 (7/13): EDGE CEILING. 34 settled + 1938 shadow rows agree: edges >=2c are
# where the money dies (realized 2.0-2.5c: 3W/14L -$4.89; shadow 2-3c: act 16.7%
# vs fair 42.2%, EV -23c/contract) while 1.5-2.0c is flat-to-positive. When
# Kalshi disagrees HARD with the sharp consensus, Kalshi has been right - a big
# "edge" is a stale/wrong fair value (line move, injury news), not mispricing.
# So the tradeable band is [PROBE_MIN_EDGE_C, MAX_EDGE_C) in BOTH modes; the
# gate changes SIZING (probe stakes -> Kelly), never the band. Shadow still
# logs every edge, so the ceiling can be raised if the table ever earns it.
MAX_EDGE_C = float(os.environ.get("SEV_MAX_EDGE_C", "2.0"))
MAX_ODDS_AGE_MIN = float(os.environ.get("SEV_MAX_ODDS_AGE_MIN", "30"))  # stale-line guard
MAX_REST_H = float(os.environ.get("SEV_MAX_REST_H", "2.0"))    # cap the free option
CANCEL_EDGE_C = float(os.environ.get("SEV_CANCEL_EDGE_C", "0.5"))  # revalidation floor
FADE_MIN_EDGE_C = float(os.environ.get("SEV_FADE_MIN_EDGE_C", "2.0"))  # fade study bar
MIN_P, MAX_P = 0.20, 0.80          # fair-prob band: no longshots (Adam's rule)
MIN_PRICE, MAX_PRICE = 20, 80      # entry-price band
MAX_SPREAD_C = int(os.environ.get("SEV_MAX_SPREAD_C", "6"))
FAIR_W = float(os.environ.get("SEV_FAIR_W", "0.70"))           # sharp vs kalshi-mid blend
DISAGREE_MAX = 0.08                # books' devigged probs range guard
MIN_BOOKS = 3                      # needed when Pinnacle absent
HOURS_BEFORE = 24                  # earliest bet vs start
LOCKOUT_MIN = 10                   # latest bet vs start
MAX_PER_DAY = int(os.environ.get("SEV_MAX_PER_DAY", "12"))
MAX_OPEN = 20
PROBE_COST_CENTS = 60
GATE_MIN_N = 30
GATE_MAX_GAP = 0.05
PER_BET_CAP = 0.015                # post-gate quarter-Kelly cap
SCAN_HOURS = float(os.environ.get("ODDS_SCAN_HOURS", "6"))
ODDS_MARKETS = os.environ.get("ODDS_MARKETS", "h2h,totals")
ODDS_REGIONS = os.environ.get("ODDS_REGIONS", "eu")   # eu = Pinnacle & co
CREDITS_MO = float(os.environ.get("SEV_CREDITS_MO", "500"))    # monthly odds budget
CREDIT_RESERVE = float(os.environ.get("SEV_CREDIT_RESERVE", "40"))
BURST_WITHIN_MIN = float(os.environ.get("SEV_BURST_WITHIN_MIN", "100"))
BURST_GAP_H = float(os.environ.get("SEV_BURST_GAP_H", "0.75"))
SHADOW_LOOKUPS_MAX = int(os.environ.get("SEV_SHADOW_LOOKUPS", "150"))

SPORTS = {   # odds-api key -> kalshi series (ml = winner, total = game total)
    "baseball_mlb":        {"ml": "KXMLBGAME",  "total": "KXMLBTOTAL"},
    "basketball_wnba":     {"ml": "KXWNBAGAME", "total": "KXWNBATOTAL"},
    "americanfootball_nfl": {"ml": "KXNFLGAME", "total": "KXNFLTOTAL"},
    "basketball_nba":      {"ml": "KXNBAGAME",  "total": "KXNBATOTAL"},
    "icehockey_nhl":       {"ml": "KXNHLGAME",  "total": "KXNHLTOTAL"},
}


def _cents(mk, key):
    """Kalshi sports markets quote in *_dollars strings; normalize to int cents."""
    v = mk.get(key)
    if isinstance(v, (int, float)) and v > 0:
        return int(round(float(v)))
    try:
        return int(round(float(mk.get(key + "_dollars") or 0) * 100))
    except (TypeError, ValueError):
        return 0
_MON = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_TKRE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z]+)-([A-Z0-9]+)$")


def devig(prices):
    """Multiplicative devig of decimal odds {team: dec} -> {team: fair_p}."""
    raw = {t: 1.0 / p for t, p in prices.items() if p and p > 1.0}
    s = sum(raw.values())
    if len(raw) != 2 or s <= 0:
        return {}
    return {t: v / s for t, v in raw.items()}


def _consensus(per_book, pinn):
    """[{k: p}] book probs + optional pinnacle -> (fair {k: p}, src) or ({}, '')."""
    if not per_book:
        return {}, ""
    keys = set(per_book[0].keys())
    per_book = [f for f in per_book if set(f.keys()) == keys]
    k0 = sorted(keys)[0]
    ps = [f[k0] for f in per_book]
    if len(ps) >= 2 and (max(ps) - min(ps)) > DISAGREE_MAX:
        return {}, ""                    # books disagree -> fair not trustworthy
    if pinn is not None and set(pinn.keys()) == keys:
        return pinn, "pinnacle"
    if len(per_book) >= MIN_BOOKS:
        med = statistics.median(ps)
        other = (keys - {k0}).pop()
        return {k0: med, other: 1 - med}, "median%d" % len(per_book)
    return {}, ""


def fair_from_books(event):
    """(ml_fair {team:p}, src) - moneyline consensus for one event."""
    per_book, pinn = [], None
    for bk in event.get("bookmakers") or []:
        for m in bk.get("markets") or []:
            if m.get("key") != "h2h":
                continue
            f = devig({o["name"]: float(o["price"]) for o in m.get("outcomes") or []
                       if o.get("price")})
            if f:
                per_book.append(f)
                if bk.get("key") == "pinnacle":
                    pinn = f
    return _consensus(per_book, pinn)


def totals_from_books(event):
    """{point: (p_over, src)} - devigged game-total consensus per line."""
    by_pt = {}
    for bk in event.get("bookmakers") or []:
        for m in bk.get("markets") or []:
            if m.get("key") != "totals":
                continue
            pts = {}
            for o in m.get("outcomes") or []:
                pt, pr, nm = o.get("point"), o.get("price"), (o.get("name") or "").lower()
                if pt is None or not pr or nm not in ("over", "under"):
                    continue
                pts.setdefault(float(pt), {})[nm] = float(pr)
            for pt, pair in pts.items():
                if "over" in pair and "under" in pair:
                    f = devig(pair)
                    if f:
                        by_pt.setdefault(pt, []).append(
                            ({"over": f["over"], "under": f["under"]},
                             bk.get("key") == "pinnacle"))
    out = {}
    for pt, lst in by_pt.items():
        pinn = next((f for f, isp in lst if isp), None)
        fair, src = _consensus([f for f, _ in lst], pinn)
        if fair:
            out[pt] = (fair["over"], src)
    return out


def odds_age_min(event, key="h2h", book=None, now=None):
    """Minutes since the newest last_update among books quoting `key` for this
    event (or a specific `book`, e.g. the pinnacle anchor). None = unknown
    (older feed rows without the field are treated as fresh for compat)."""
    newest = None
    for bk in event.get("bookmakers") or []:
        if book and bk.get("key") != book:
            continue
        for m in bk.get("markets") or []:
            if m.get("key") != key:
                continue
            lu = m.get("last_update") or bk.get("last_update")
            if not lu:
                continue
            try:
                t = datetime.datetime.fromisoformat(lu.replace("Z", "+00:00"))
            except Exception:
                continue
            if newest is None or t > newest:
                newest = t
    if newest is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (now - newest).total_seconds() / 60.0)


def parse_ticker(tk):
    """-> (start_dt_ET|date, has_time, yes_code) from a Kalshi game ticker."""
    m = _TKRE.search(tk or "")
    if not m:
        return None, False, ""
    yy, mon, dd, hhmm, _teams, yes_code = m.groups()
    try:
        y, mo, d = 2000 + int(yy), _MON[mon], int(dd)
        if hhmm:
            return (datetime.datetime(y, mo, d, int(hhmm[:2]), int(hhmm[2:]),
                                      tzinfo=ET), True, yes_code)
        return datetime.date(y, mo, d), False, yes_code
    except Exception:
        return None, False, ""


def team_matches(sub, full):
    """Kalshi yes_sub_title ('Los Angeles D') vs odds team ('Los Angeles Dodgers')."""
    sub, full = (sub or "").lower().strip(), (full or "").lower().strip()
    if not sub or not full:
        return False
    toks = sub.split()
    if len(toks) > 1 and len(toks[-1]) == 1:          # 'los angeles d' disambiguator
        city, letter = " ".join(toks[:-1]), toks[-1]
        if full.startswith(city):
            nick = full[len(city):].strip()
            return nick.startswith(letter)
        return False
    return full.startswith(sub) or sub in full


def match_event_by_time(mk, events):
    """Match a totals market to an odds event by ticker datetime + title teams."""
    start, has_time, _ = parse_ticker(mk.get("ticker", ""))
    if start is None:
        return None
    title = (mk.get("title") or "").lower()
    for ev in events:
        try:
            c = datetime.datetime.fromisoformat(
                (ev.get("commence_time") or "").replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            continue
        if has_time:
            if abs((c - start).total_seconds()) > 900:
                continue
        elif c.date() != start:
            continue
        w1 = (ev.get("home_team") or "").split()[0].lower()
        w2 = (ev.get("away_team") or "").split()[0].lower()
        if title and w1 and w2 and w1 in title and w2 in title:
            return ev
    return None


def match_event(mk, events):
    """Kalshi market -> matching odds event + our team name, else (None, '')."""
    start, has_time, _ = parse_ticker(mk.get("ticker", ""))
    if start is None:
        return None, ""
    sub = mk.get("yes_sub_title") or ""
    for ev in events:
        try:
            c = datetime.datetime.fromisoformat(
                (ev.get("commence_time") or "").replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            continue
        if has_time:
            if abs((c - start).total_seconds()) > 900:
                continue
        else:
            if c.date() != start:
                continue
        for team in (ev.get("home_team"), ev.get("away_team")):
            if team_matches(sub, team):
                other = ev["away_team"] if team == ev.get("home_team") else ev["home_team"]
                title = (mk.get("title") or "").lower()
                # both teams must appear consistent with the title (guard doubleheaders)
                if other and title and not any(
                        w in title for w in other.lower().split()[:2]):
                    pass  # title check is best-effort only
                return ev, team
    return None, ""


class SharpEV:
    def __init__(self):
        self.start = START_CENTS
        self.cash = float(START_CENTS)
        self.bets = {}
        self.pending = {}              # resting maker orders awaiting a real fill
        self.realized = 0.0
        self.wins = 0
        self.losses = 0
        self.fees = 0.0
        self.placed = 0
        self.canceled = 0              # resting orders that expired unfilled
        self.history = []
        self.last_fetch = ""           # iso ts of last odds pull
        self.credits_remaining = None  # from x-requests-remaining header
        self.next_starts = []          # upcoming matched game starts (iso, ET)
        self.shadow_day = ""           # last date the shadow joiner ran
        self.shadow_cache = {}         # cached shadow calibration report
        self.warned_no_key = False
        self._shadow_rows = []         # per-scan: every band-qualifying edge seen
        self._pending_eval = {}        # per-scan: fresh fair for resting orders
        self.last_scan = {}            # diagnostics for the dashboard
        self.load()

    # ---- persistence (same contract as the other books) ----
    def to_dict(self):
        return {"start": self.start, "cash": self.cash, "bets": self.bets,
                "pending": self.pending, "realized": self.realized,
                "wins": self.wins, "losses": self.losses,
                "fees": self.fees, "placed": self.placed, "canceled": self.canceled,
                "last_fetch": self.last_fetch, "last_scan": self.last_scan,
                "credits_remaining": self.credits_remaining,
                "next_starts": self.next_starts, "shadow_day": self.shadow_day,
                "history": self.history[-100:]}

    def load(self):
        try:
            d = json.load(open(SSIM))
            for k in ("start", "cash", "realized", "wins", "losses", "fees",
                      "placed", "canceled"):
                setattr(self, k, d.get(k, getattr(self, k)))
            self.bets = d.get("bets", {})
            self.pending = d.get("pending", {})
            self.history = d.get("history", [])
            self.last_fetch = d.get("last_fetch", "")
            self.last_scan = d.get("last_scan", {})
            self.credits_remaining = d.get("credits_remaining")
            self.next_starts = d.get("next_starts", [])
            self.shadow_day = d.get("shadow_day", "")
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs("logs", exist_ok=True)
            json.dump(self.to_dict(), open(SSIM, "w"))
            settled_all = [h for h in self.history if h.get("outcome") in (0, 1)]
            era_cur = _era_stats([h for h in settled_all if era_of(h) == ERA])
            era_cur["open"] = sum(1 for b in self.bets.values() if era_of(b) == ERA)
            st = {"updated": datetime.datetime.now().isoformat(timespec="seconds"),
                  "summary": self.summary(),
                  "era_current": era_cur,
                  "era_legacy": _era_stats([h for h in settled_all if era_of(h) != ERA]),
                  "last_scan": self.last_scan,
                  "shadow": self.shadow_cache or {},
                  "pending": [dict(o, ticker=tk, era_v=era_of(o)) for tk, o in self.pending.items()],
                  "open": [dict(b, ticker=tk, era_v=era_of(b)) for tk, b in self.bets.items()],
                  "settled": [dict(h, era_v=era_of(h)) for h in reversed(self.history[-50:])]}
            json.dump(st, open(SSTATE, "w"))
        except Exception:
            pass

    def summary(self):
        mode, n = self._gate()
        return {"start": round(self.start / 100.0, 2), "cash": round(self.cash / 100.0, 2),
                "realized": round(self.realized / 100.0, 2), "wins": self.wins,
                "losses": self.losses, "fees": round(self.fees / 100.0, 2),
                "placed": self.placed, "canceled": self.canceled,
                "open_bets": len(self.bets), "pending": len(self.pending),
                "gate": mode, "gate_n": n}

    def _log(self, row):
        try:
            new = not os.path.exists(SLOG)
            os.makedirs("logs", exist_ok=True)
            with open(SLOG, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "event", "sport", "game", "team", "fair",
                                "entry_c", "count", "outcome", "pnl_$"])
                w.writerow(row)
        except Exception:
            pass

    # ---- gate: identical contract to the weather book ----
    def _gate(self):
        cur = [h for h in self.history
               if era_of(h) == ERA and h.get("outcome") in (0, 1)][-60:]
        n = len(cur)
        if n < GATE_MIN_N:
            return "probe", n
        expectancy = sum(h["pnl"] for h in cur) / n
        pred = sum(h["pside"] for h in cur) / n
        act = sum(h["outcome"] for h in cur) / n
        return ("scale" if expectancy > 0 and (pred - act) <= GATE_MAX_GAP
                else "probe", n)

    # ---- data ----
    def fetch_odds(self, sport):
        key = os.environ.get("ODDS_API_KEY", "")
        if not key:
            return None
        try:
            r = requests.get(ODDS.format(sport=sport),
                             params={"apiKey": key, "regions": ODDS_REGIONS,
                                     "markets": ODDS_MARKETS, "oddsFormat": "decimal"},
                             timeout=20)
            rem = r.headers.get("x-requests-remaining")
            if rem is not None:
                try:
                    self.credits_remaining = float(rem)
                except (TypeError, ValueError):
                    pass
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None

    def kalshi_markets(self, series):
        try:
            d = requests.get(KALSHI + "/markets",
                             params={"series_ticker": series, "status": "open",
                                     "limit": 200}, timeout=15).json()
            ms = d.get("markets") or []
            for m in ms:                       # sports quotes arrive as *_dollars
                m["yes_bid"] = _cents(m, "yes_bid")
                m["yes_ask"] = _cents(m, "yes_ask")
            return ms
        except Exception:
            return []

    def fetch_result(self, tk):
        try:
            d = requests.get(KALSHI + f"/markets/{tk}", timeout=15).json()
            res = ((d.get("market", d) or {}).get("result") or "").lower()
            return res if res in ("yes", "no") else None
        except Exception:
            return None

    # ---- credit pacing ----
    def _interval_h(self):
        """Adaptive scan interval: stretch the remaining monthly credit budget
        over the remaining days; clamp [1h, 24h]. Unknown budget -> SCAN_HOURS."""
        rem = self.credits_remaining
        if rem is None:
            return SCAN_HOURS
        today = datetime.date.today()
        days_in = calendar.monthrange(today.year, today.month)[1]
        days_left = max(1, days_in - today.day + 1)
        n_sports = max(1, len((self.last_scan or {}).get("sports") or []) or 2)
        per_scan = 2.0 * n_sports          # 2 markets x 1 region = 2 credits/sport
        scans_left = max(1.0, (rem - CREDIT_RESERVE) / per_scan)
        return min(max(24.0 * days_left / scans_left, 1.0), 24.0)

    def _pace_ok(self):
        """True when cumulative credit use is AHEAD of a uniform monthly pace
        (i.e. we have slack to spend on a burst scan)."""
        rem = self.credits_remaining
        if rem is None:
            return False
        today = datetime.date.today()
        days_in = calendar.monthrange(today.year, today.month)[1]
        used = max(0.0, CREDITS_MO - rem)
        return used < CREDITS_MO * (today.day / days_in) * 0.9

    def _burst_near(self):
        """A matched game starts soon: worth a last-look scan (edges cluster
        just before start, when the books are sharpest and Kalshi most liquid)."""
        nowa = datetime.datetime.now(ET)
        for s in self.next_starts or []:
            try:
                st = datetime.datetime.fromisoformat(s)
            except Exception:
                continue
            d = st - nowa
            if datetime.timedelta(minutes=LOCKOUT_MIN) <= d <= \
                    datetime.timedelta(minutes=BURST_WITHIN_MIN):
                return True
        return False

    @staticmethod
    def _near_game(markets, now=None):
        """True if the series has a QUOTED game inside the bet window - the
        free pre-check that stops odds credits burning on out-of-season sports
        (e.g. NFL preseason: quoted futures months out, 0 evaluable markets)."""
        now = now or datetime.datetime.now(ET)
        for mk in markets:
            if not mk.get("yes_bid"):
                continue
            st, has_time, _ = parse_ticker(mk.get("ticker", ""))
            if st is None:
                continue
            if has_time:
                if now - datetime.timedelta(hours=6) <= st \
                        <= now + datetime.timedelta(hours=HOURS_BEFORE):
                    return True
            else:
                if st in (now.date(), (now + datetime.timedelta(days=1)).date()):
                    return True
        return False

    # ---- core ----
    def settle(self):
        now = datetime.datetime.now(ET)
        for tk, b in list(self.bets.items()):
            st = b.get("start")
            if st:
                try:
                    if now < datetime.datetime.fromisoformat(st):
                        continue               # game not started: nothing to settle
                except Exception:
                    pass
            res = self.fetch_result(tk)
            if res is None:
                continue
            won = (res == b.get("side", "yes"))
            net = ((100 if won else 0) - b["entry"]) * b["count"] - b.get("fee", 0)
            self.cash += (100 if won else 0) * b["count"]
            self.realized += net
            self.wins += int(won)
            self.losses += int(not won)
            row = dict(b, outcome=(1 if won else 0), pnl=round(net / 100.0, 2),
                       ts=datetime.datetime.now().isoformat(timespec="seconds"))
            self.history.append(row)
            self.history = self.history[-100:]
            self._log([row["ts"], "SETTLE", b.get("sport", ""), b.get("game", ""),
                       b.get("team", ""), b.get("pside", 0), b["entry"], b["count"],
                       row["outcome"], row["pnl"]])
            del self.bets[tk]

    def check_fills(self, quotes=None):
        """Resting-order simulation. A pending order fills only when the market
        actually trades through our price (or the book crosses us); it cancels
        at the lockout if untouched. quotes: {ticker: {yes_bid,yes_ask,last_price}}
        (fetched in one batch call when None)."""
        if not self.pending:
            return 0
        now = datetime.datetime.now(ET)
        if quotes is None:
            quotes = {}
            try:
                tks = ",".join(list(self.pending)[:40])
                d = requests.get(KALSHI + "/markets",
                                 params={"tickers": tks, "limit": 100},
                                 timeout=15).json()
                for m in d.get("markets") or []:
                    quotes[m.get("ticker")] = {
                        k: _cents(m, k) for k in ("yes_bid", "yes_ask", "last_price")}
            except Exception:
                quotes = {}
        filled = 0
        for tk, o in list(self.pending.items()):
            q = quotes.get(tk) or {}
            last = q.get("last_price") or 0
            yb, ya = q.get("yes_bid") or 0, q.get("yes_ask") or 0
            e = o["entry"]
            if o.get("side", "yes") == "yes":
                # our YES bid at e: a print at/below e or the ask dropping to us
                hit = (last and last <= e) or (ya and ya <= e)
            else:
                # our NO bid at e == YES offer at 100-e: print/bid at/through it
                hit = (last and last >= 100 - e) or (yb and yb >= 100 - e)
            if hit:
                cost = e * o["count"]
                if cost <= self.cash:
                    self.cash -= cost
                    b = {k: v for k, v in o.items() if k != "expire"}
                    b["fts"] = now.isoformat(timespec="seconds")
                    self.bets[tk] = b
                    filled += 1
                    self._log([b["fts"], "FILL", o.get("sport", ""), o.get("game", ""),
                               o.get("team", ""), o.get("pside", 0), e, o["count"],
                               "", ""])
                    del self.pending[tk]
                    continue
            exp = o.get("expire")
            expd = None
            if exp:
                try:
                    expd = datetime.datetime.fromisoformat(exp)
                except Exception:
                    expd = None
            if expd is not None and now >= expd:
                self.canceled += 1
                self._log([now.isoformat(timespec="seconds"), "CANCEL",
                           o.get("sport", ""), o.get("game", ""), o.get("team", ""),
                           o.get("pside", 0), e, o["count"], "unfilled", ""])
                del self.pending[tk]
        return filled

    def _placed_today(self):
        today = datetime.date.today().isoformat()
        n = sum(1 for b in list(self.bets.values()) + list(self.pending.values())
                if (b.get("ots") or "")[:10] == today)
        n += sum(1 for h in self.history if (h.get("ots") or "")[:10] == today)
        return n

    def _sided(self, mk, fair_yes, src, start_iso, label, min_edge=None,
               age_m=None):
        """Try both sides of one market -> best qualifying (cand tuple) or None.
        YES entry = join yes_bid; NO entry = join no_bid = 100 - yes_ask.
        Every band-qualifying side is shadow-logged (edge distribution data,
        incl. odds age); candidacy requires min_edge <= edge < MAX_EDGE_C and
        fresh odds (stale anchor lines manufacture phantom edges)."""
        if min_edge is None:
            min_edge = MIN_EDGE_C
        bid, ask = mk.get("yes_bid") or 0, mk.get("yes_ask") or 0
        if not bid or not ask or ask - bid > MAX_SPREAD_C:
            return None                                 # illiquid / wide
        mid = (bid + ask) / 2.0
        fair = FAIR_W * fair_yes + (1 - FAIR_W) * (mid / 100.0)
        best = None
        for side, p, entry in (("yes", fair, bid), ("no", 1 - fair, 100 - ask)):
            if not (MIN_P <= p <= MAX_P):               # no longshots (either side)
                continue
            if not (MIN_PRICE <= entry <= MAX_PRICE):
                continue
            edge_c = p * 100 - entry                    # maker fee ~ $0 at our size
            if getattr(self, "_shadow_rows", None) is None:
                self._shadow_rows = []                  # tolerate bare __new__ (tests)
            self._shadow_rows.append(
                [datetime.datetime.now().isoformat(timespec="seconds"),
                 mk.get("_sport", ""), mk.get("_kind", ""), mk.get("ticker", ""),
                 side, round(p, 4), entry, mid, round(edge_c, 2), src, start_iso,
                 "" if age_m is None else round(age_m, 1)])
            if edge_c < min_edge:
                continue
            if edge_c >= MAX_EDGE_C:
                continue        # too-good-to-be-true: big disagreement = we're wrong
            if age_m is not None and age_m > MAX_ODDS_AGE_MIN:
                continue        # anchor line is stale: the "edge" is a lag artifact
            if best is None or edge_c > best[3]:
                best = (mk, label, side, edge_c, p, src, start_iso)
        return best

    @staticmethod
    def _start_of(ev):
        try:
            return datetime.datetime.fromisoformat(
                ev["commence_time"].replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            return None

    def candidates(self, events, markets, now=None):
        """Filter pipeline -> [(mk, label, side, edge_c, fair_side, src, start)].
        The band is [PROBE_MIN_EDGE_C, MAX_EDGE_C) in both gate modes - the
        gate changes sizing, never the band (edges >=2c measured toxic).
        Markets with a RESTING order aren't candidates, but their fresh fair is
        captured into _pending_eval so revalidate_pending() can kill stale
        orders instead of leaving free options in the book."""
        now = now or datetime.datetime.now(ET)
        out = []
        min_edge = PROBE_MIN_EDGE_C
        pend = getattr(self, "pending", {})
        if getattr(self, "_pending_eval", None) is None:
            self._pending_eval = {}
        open_events = {tk.rsplit("-", 1)[0]
                       for tk in list(self.bets) + list(pend)}
        for mk in markets:
            tk = mk.get("ticker", "")
            is_pend = tk in pend
            if tk in self.bets or (not is_pend
                                   and tk.rsplit("-", 1)[0] in open_events):
                continue                                # one bet per game per series
            kind = mk.get("_kind", "ml")
            if kind == "ml":
                ev, team = match_event(mk, events)
                if ev is None:
                    continue
                start = self._start_of(ev)
                if start is None:
                    continue
                fair_all, src = fair_from_books(ev)
                if not fair_all or team not in fair_all:
                    continue
                if is_pend:
                    self._pending_eval[tk] = (fair_all[team],
                                              mk.get("yes_bid") or 0,
                                              mk.get("yes_ask") or 0)
                    continue
                if not (start - datetime.timedelta(hours=HOURS_BEFORE) <= now
                        <= start - datetime.timedelta(minutes=LOCKOUT_MIN)):
                    continue                            # pregame window only
                age = odds_age_min(ev, "h2h",
                                   "pinnacle" if src == "pinnacle" else None)
                c = self._sided(mk, fair_all[team], src,
                                start.isoformat(timespec="minutes"),
                                team, min_edge=min_edge, age_m=age)
                if c:
                    out.append(c)
            elif kind == "total":
                pt = mk.get("floor_strike")
                if pt is None or (mk.get("strike_type") or "greater") != "greater":
                    continue
                if abs(float(pt) * 2 - round(float(pt) * 2)) > 1e-6 \
                        or float(pt) == int(float(pt)):
                    continue                            # half-point lines only (no pushes)
                ev = match_event_by_time(mk, events)
                if ev is None:
                    continue
                start = self._start_of(ev)
                if start is None:
                    continue
                tf = totals_from_books(ev)
                got = tf.get(float(pt))
                if not got:
                    continue                            # need the SAME line at the books
                p_over, src = got
                if is_pend:
                    self._pending_eval[tk] = (p_over,
                                              mk.get("yes_bid") or 0,
                                              mk.get("yes_ask") or 0)
                    continue
                if not (start - datetime.timedelta(hours=HOURS_BEFORE) <= now
                        <= start - datetime.timedelta(minutes=LOCKOUT_MIN)):
                    continue
                age = odds_age_min(ev, "totals",
                                   "pinnacle" if src == "pinnacle" else None)
                c = self._sided(mk, p_over, src,
                                start.isoformat(timespec="minutes"),
                                "Over %.1f" % float(pt), min_edge=min_edge,
                                age_m=age)
                if c:
                    out.append(c)
        out.sort(key=lambda t: -t[3])
        return out

    def revalidate_pending(self):
        """Kill resting orders whose reason-to-exist is gone. A maker order
        priced off an old fair value is a free option for informed flow - we
        get filled exactly when the world learns we were wrong. On every scan,
        each pending order with a freshly computed fair (from _pending_eval)
        is re-edged at ITS resting entry; cancel when the edge fell below
        CANCEL_EDGE_C or blew past MAX_EDGE_C (fair moved = we're the fish)."""
        n = 0
        for tk, o in list(self.pending.items()):
            got = (getattr(self, "_pending_eval", None) or {}).get(tk)
            if not got:
                continue
            fair_yes, bid, ask = got
            if not bid or not ask:
                continue
            mid = (bid + ask) / 2.0
            fair = FAIR_W * fair_yes + (1 - FAIR_W) * (mid / 100.0)
            p = fair if o.get("side", "yes") == "yes" else 1 - fair
            edge_c = p * 100 - o["entry"]
            if CANCEL_EDGE_C <= edge_c < MAX_EDGE_C:
                continue                                # still sane: let it rest
            self.canceled += 1
            n += 1
            self._log([datetime.datetime.now().isoformat(timespec="seconds"),
                       "CANCEL", o.get("sport", ""), o.get("game", ""),
                       o.get("team", ""), o.get("pside", 0), o["entry"],
                       o["count"], "edge-gone %.1fc" % edge_c, ""])
            del self.pending[tk]
        return n

    def place(self, cands):
        """Rest maker orders for the best candidates. Cash moves at FILL time
        (check_fills), not here - a resting order costs nothing until it fills."""
        mode, _ = self._gate()
        open_stake = sum(b["entry"] * b["count"] for b in self.bets.values())
        bankroll = self.cash + open_stake
        placed = 0
        budget = MAX_PER_DAY - self._placed_today()
        taken = {t.rsplit("-", 1)[0] for t in list(self.bets) + list(self.pending)}
        for mk, label, side, edge_c, fair, src, start_iso in cands:
            if placed >= budget or len(self.bets) + len(self.pending) >= MAX_OPEN:
                break
            tk = mk["ticker"]
            if tk in self.bets or tk in self.pending or tk.rsplit("-", 1)[0] in taken:
                continue                                 # filled a sibling this pass
            entry = mk["yes_bid"] if side == "yes" else 100 - mk["yes_ask"]
            if mode == "probe":
                count = max(1, PROBE_COST_CENTS // entry)
            else:
                b_odds = (100 - entry) / entry
                f_star = max(0.0, (fair - (1 - fair) / b_odds)) * 0.25
                count = int(min(f_star, PER_BET_CAP) * bankroll // entry)
                if count < 1:
                    continue
            if entry * count > self.cash:
                continue
            self.placed += 1
            ots = datetime.datetime.now().isoformat(timespec="seconds")
            team = label if side == "yes" else (
                "not " + label if label.startswith("Over") else label + " (fade)")
            # expire at the game lockout OR after MAX_REST_H, whichever is
            # sooner: our fair value rots between scans, and a long-resting
            # order is a free option (long rests measured net-negative).
            cap = (datetime.datetime.now(ET)
                   + datetime.timedelta(hours=MAX_REST_H))
            expire = ""
            try:
                lock = (datetime.datetime.fromisoformat(start_iso)
                        - datetime.timedelta(minutes=LOCKOUT_MIN))
                expire = min(lock, cap).isoformat(timespec="seconds")
            except Exception:
                expire = cap.isoformat(timespec="seconds")
            self.pending[tk] = {
                "sport": mk.get("_sport", ""), "game": (mk.get("title") or "")[:60],
                "team": team, "side": side, "entry": entry, "count": count,
                "pside": round(fair, 3), "edge": round(edge_c, 1), "fee": 0,
                "src": src, "start": start_iso, "ots": ots, "era": ERA,
                "expire": expire}
            taken.add(tk.rsplit("-", 1)[0])
            self._log([ots, "REST", mk.get("_sport", ""), (mk.get("title") or "")[:60],
                       team, round(fair, 3), entry, count, "", ""])
            placed += 1
        return placed

    def step(self, force=False):
        """Called from the bot loop. Settles + checks resting fills cheaply every
        call; pulls odds on the adaptive credit-paced schedule (+ burst scans
        near game starts when ahead of pace)."""
        self.settle()
        self.check_fills()
        self.shadow_daily()
        now = datetime.datetime.now()
        elapsed_h = 1e9
        if self.last_fetch and not force:
            try:
                elapsed_h = (now - datetime.datetime.fromisoformat(self.last_fetch)
                             ).total_seconds() / 3600.0
            except Exception:
                pass
        interval = self._interval_h()
        due = force or elapsed_h >= interval
        burst = False
        if not due and elapsed_h >= BURST_GAP_H and self._pace_ok() \
                and self._burst_near():
            due = burst = True
        n_cand = n_placed = 0
        if due:
            if not os.environ.get("ODDS_API_KEY", ""):
                if not self.warned_no_key:
                    print("  SHARP-EV: idle (no ODDS_API_KEY set)")
                    self.warned_no_key = True
                self.save()
                return 0, 0
            self.last_fetch = now.isoformat(timespec="seconds")
            self._shadow_rows = []
            self._pending_eval = {}
            nowa = datetime.datetime.now(ET)
            starts = []
            scan = {"ts": self.last_fetch, "sports": [], "evaluated": 0,
                    "best_edge": None, "bar": None, "burst": burst}
            for sport, series_map in SPORTS.items():
                # credit guard: Kalshi first (free) - only spend odds credits on
                # sports that have QUOTED markets inside the bet window right now
                markets = []
                for kind, series in series_map.items():
                    if kind == "total" and "totals" not in ODDS_MARKETS:
                        continue
                    for mk in self.kalshi_markets(series):
                        mk["_sport"] = sport
                        mk["_kind"] = kind
                        markets.append(mk)
                if not any(m.get("yes_bid") for m in markets):
                    continue                       # out of season / nothing live
                if not self._near_game(markets, nowa):
                    continue                       # quoted, but no game in window
                events = self.fetch_odds(sport)
                if not events:
                    continue
                for ev in events:                  # cache starts for burst logic
                    st = self._start_of(ev)
                    if st and datetime.timedelta(0) <= st - nowa \
                            <= datetime.timedelta(hours=26):
                        starts.append(st.isoformat(timespec="minutes"))
                before = len(self._shadow_rows)
                cands = self.candidates(events, markets)
                n_cand += len(cands)
                n_placed += self.place(cands)
                scan["sports"].append(
                    {"sport": sport, "kalshi_mkts": len(markets),
                     "events": len(events),
                     "evaluated": len(self._shadow_rows) - before,
                     "cands": len(cands)})
            scan["evaluated"] = len(self._shadow_rows)
            if self._shadow_rows:
                scan["best_edge"] = max(r[8] for r in self._shadow_rows)
            scan["bar"] = PROBE_MIN_EDGE_C     # band min (both modes; gate = sizing)
            scan["ceil"] = MAX_EDGE_C
            scan["revalidated"] = self.revalidate_pending()
            scan["credits"] = self.credits_remaining
            scan["interval_h"] = round(self._interval_h(), 2)
            scan["pending"] = len(self.pending)
            self.next_starts = sorted(set(starts))[:40]
            self.last_scan = scan
            self._flush_shadow()
        self.save()
        return n_cand, n_placed

    def _flush_shadow(self):
        """Append this scan's evaluated edges to the shadow CSV - free
        calibration data (edge distribution) whether or not we bet."""
        if not self._shadow_rows:
            return
        try:
            os.makedirs("logs", exist_ok=True)
            new = not os.path.exists(SSHADOW)
            with open(SSHADOW, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts", "sport", "kind", "ticker", "side", "fair",
                                "entry_c", "mid_c", "edge_c", "src", "start",
                                "odds_age_m"])
                w.writerows(self._shadow_rows)
        except Exception:
            pass
        self._shadow_rows = []

    # ---- shadow outcomes: ~30x more calibration data/day than actual bets ----
    def shadow_daily(self, force=False):
        """Once a day, join settled outcomes onto the shadow log (bounded
        lookups) and refresh the edge-bucket calibration report."""
        today = datetime.date.today().isoformat()
        if self.shadow_day == today and not force:
            return
        self.shadow_day = today
        try:
            raw = list(csv.reader(open(SSHADOW)))
        except Exception:
            return
        if len(raw) < 2:
            return
        hdr, rows = raw[0], raw[1:]
        done = set()
        try:
            for r in csv.reader(open(SSHADOWR)):
                if len(r) >= 5 and r[0] != "ts":
                    done.add((r[0], r[3], r[4]))
        except Exception:
            pass
        now = datetime.datetime.now(ET)
        res_cache, looked, out = {}, 0, []
        for r in rows:
            if len(r) < 11 or (r[0], r[3], r[4]) in done:
                continue
            try:
                st = datetime.datetime.fromisoformat(r[10])
            except Exception:
                continue
            if not (now - datetime.timedelta(days=10) < st
                    < now - datetime.timedelta(hours=5)):
                continue                       # game must be over (and recent)
            tk = r[3]
            if tk not in res_cache:
                if looked >= SHADOW_LOOKUPS_MAX:
                    continue
                looked += 1
                res_cache[tk] = self.fetch_result(tk)
            res = res_cache[tk]
            if res is None:
                continue
            out.append(r + [1 if res == r[4] else 0])
        if out:
            try:
                new = not os.path.exists(SSHADOWR)
                with open(SSHADOWR, "a", newline="") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(hdr + ["outcome"])
                    w.writerows(out)
            except Exception:
                pass
        self.shadow_cache = self.shadow_report()
        fade = self.fade_report()
        if fade:
            self.shadow_cache["fade"] = fade

    def shadow_report(self):
        """Edge-bucket calibration from settled shadow rows: does sharp-vs-Kalshi
        disagreement actually predict outcomes? ev_c = mean(100*outcome - entry)
        per contract in cents, at the maker entry we would have joined."""
        try:
            rows = list(csv.reader(open(SSHADOWR)))[1:]
        except Exception:
            return {}
        buckets = [(-99, 0, "<0"), (0, 1, "0-1"), (1, 2, "1-2"),
                   (2, 3, "2-3"), (3, 5, "3-5"), (5, 999, "5+")]
        agg, n_all = {}, 0
        for r in rows:
            try:
                # outcome is the LAST column: rows are 12-wide (pre-odds-age)
                # or 13-wide (with odds_age_m) - both end in outcome.
                fair, entry = float(r[5]), float(r[6])
                edge, out = float(r[8]), int(r[-1])
            except (ValueError, IndexError):
                continue
            n_all += 1
            for lo, hi, lab in buckets:
                if lo <= edge < hi:
                    a = agg.setdefault(lab, [0, 0.0, 0, 0.0, 0.0])
                    a[0] += 1
                    a[1] += fair
                    a[2] += out
                    a[3] += entry
                    a[4] += 100.0 * out - entry
                    break
        rep = {"n": n_all, "buckets": []}
        for lo, hi, lab in buckets:
            a = agg.get(lab)
            if not a or a[0] < 3:
                continue
            rep["buckets"].append(
                {"edge": lab, "n": a[0],
                 "fair": round(100.0 * a[1] / a[0], 1),
                 "act": round(100.0 * a[2] / a[0], 1),
                 "entry": round(a[3] / a[0], 1),
                 "ev_c": round(a[4] / a[0], 2)})
        return rep

    def fade_report(self):
        """FADE STUDY (measurement only - no fade bets are placed). The v3
        finding: big sharp-vs-Kalshi disagreements LOSE at our maker price -
        so would the OPPOSITE side (bet WITH Kalshi, against the sharp
        consensus) make money at honest TAKER prices?
        Method: settled shadow rows with edge >= FADE_MIN_EDGE_C, DEDUPED to
        the last row per (ticker, side) - the same game is logged every scan
        and duplicate rows fake the sample size. Inverse economics per
        contract: pay 100-entry (cross the book), pay the taker fee
        7*P*(1-P) cents, collect 100 when OUR side loses:
            inv_ev_c = entry - 100*outcome - fee.
        Promote to a paper book ONLY if this stays positive at n >= 30
        deduped games; odds_age_m in the shadow rows shows how much of the
        effect is stale-feed artifact the v3 freshness gate already removes."""
        try:
            rows = list(csv.reader(open(SSHADOWR)))[1:]
        except Exception:
            return {}
        last = {}
        for r in rows:
            try:
                if float(r[8]) >= FADE_MIN_EDGE_C:
                    int(r[-1])
                    last[(r[3], r[4])] = r
            except (ValueError, IndexError):
                continue
        if not last:
            return {}
        buckets = [(2, 3, "2-3"), (3, 5, "3-5"), (5, 999, "5+")]
        agg = {}
        tot = [0, 0.0, 0]
        for r in last.values():
            entry, edge, out = float(r[6]), float(r[8]), int(r[-1])
            pe = entry / 100.0
            inv = entry - 100.0 * out - 7.0 * pe * (1.0 - pe)
            tot[0] += 1
            tot[1] += inv
            tot[2] += out
            for lo, hi, lab in buckets:
                if lo <= edge < hi:
                    a = agg.setdefault(lab, [0, 0.0])
                    a[0] += 1
                    a[1] += inv
                    break
        out_b = []
        for lo, hi, lab in buckets:
            a = agg.get(lab)
            if a and a[0] >= 3:
                out_b.append({"edge": lab, "n": a[0],
                              "inv_ev_c": round(a[1] / a[0], 2)})
        return {"min_edge": FADE_MIN_EDGE_C, "n": tot[0],
                "our_act": round(100.0 * tot[2] / tot[0], 1),
                "inv_ev_c": round(tot[1] / tot[0], 2), "buckets": out_b}


if __name__ == "__main__":
    import sys
    if "--shadow-report" in sys.argv:
        p = SharpEV()
        p.shadow_daily(force=True)
        rep = p.shadow_cache or {}
        print("settled shadow rows: %d" % rep.get("n", 0))
        for b in rep.get("buckets", []):
            print("  edge %-4s n=%-4d fair %5.1f%%  act %5.1f%%  entry %5.1f  "
                  "EV/contract %+.2fc" % (b["edge"], b["n"], b["fair"], b["act"],
                                          b["entry"], b["ev_c"]))
        fd = rep.get("fade") or {}
        if fd:
            print("FADE study (deduped, taker-priced inverse of edges >=%.1fc): "
                  "n=%d our_act %.1f%% inv EV %+.2fc/contract %s"
                  % (fd["min_edge"], fd["n"], fd["our_act"], fd["inv_ev_c"],
                     fd["buckets"]))
    elif "--selftest" in sys.argv:
        # devig
        f = devig({"A": 1.91, "B": 1.91})
        assert abs(f["A"] - 0.5) < 1e-9
        f = devig({"A": 1.50, "B": 2.80})
        assert 0.63 < f["A"] < 0.66 and abs(sum(f.values()) - 1) < 1e-9
        # ticker parse (with + without time)
        dt, ht, code = parse_ticker("KXMLBGAME-26JUL091235ATLPIT-PIT")
        assert ht and code == "PIT" and dt.hour == 12 and dt.minute == 35
        d2, ht2, code2 = parse_ticker("KXWNBAGAME-26JUL07CHIPHX-PHX")
        assert not ht2 and code2 == "PHX" and d2.month == 7
        # team matching incl. one-letter disambiguator
        assert team_matches("Pittsburgh", "Pittsburgh Pirates")
        assert team_matches("Los Angeles D", "Los Angeles Dodgers")
        assert not team_matches("Los Angeles D", "Los Angeles Angels")
        # candidate pipeline on fixtures
        now = datetime.datetime.now(ET)
        start = now + datetime.timedelta(hours=3)
        def _mkev(ph, pa):
            return {"commence_time": start.astimezone(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "home_team": "Pittsburgh Pirates", "away_team": "Atlanta Braves",
                    "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h",
                        "outcomes": [{"name": "Pittsburgh Pirates", "price": ph},
                                     {"name": "Atlanta Braves", "price": pa}]}]}]}
        ev_big = _mkev(1.60, 2.60)     # fair ~62% vs 50c bid = ~8.8c "edge"
        ev = _mkev(1.893, 2.034)       # fair ~51.8% vs 50c bid = ~1.7c edge (band)
        tk = "KXMLBGAME-26%s%02d%02d%02dATLPIT-PIT" % (
            ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"][start.month-1],
            start.day, start.hour, start.minute)
        mk = {"ticker": tk, "title": "Atlanta vs Pittsburgh Winner?",
              "yes_sub_title": "Pittsburgh", "yes_bid": 50, "yes_ask": 53, "_sport": "baseball_mlb"}
        p = SharpEV.__new__(SharpEV)
        p.start = 10000; p.cash = 10000.0; p.bets = {}; p.pending = {}
        p.realized = 0.0; p.wins = p.losses = p.placed = p.canceled = 0
        p.fees = 0.0; p.history = []; p.last_fetch = ""; p.warned_no_key = False
        p.credits_remaining = None; p.next_starts = []; p.shadow_day = ""
        p.shadow_cache = {}; p._shadow_rows = []; p.last_scan = {}
        p._pending_eval = {}
        cands = p.candidates([ev], [mk], now=now)
        assert len(cands) == 1 and PROBE_MIN_EDGE_C <= cands[0][3] < MAX_EDGE_C
        # EDGE CEILING: a huge sharp-vs-Kalshi disagreement is rejected
        # (measured: the biggest "edges" lose - stale/wrong fair, not mispricing)
        assert p.candidates([ev_big], [mk], now=now) == []
        # ...but still shadow-logged (calibration data)
        assert any(r[3] == mk["ticker"] and r[8] > MAX_EDGE_C
                   for r in p._shadow_rows)
        # STALE ODDS: same band edge, but the anchor line is 2h old -> skip
        ev_stale = _mkev(1.893, 2.034)
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ev_stale["bookmakers"][0]["markets"][0]["last_update"] = old
        assert p.candidates([ev_stale], [mk], now=now) == []
        # longshot rejected even with huge edge
        mk2 = dict(mk, yes_bid=8, yes_ask=11)
        assert p.candidates([ev_big], [mk2], now=now) == []
        # wide spread rejected
        mk3 = dict(mk, yes_bid=40, yes_ask=55)
        assert p.candidates([ev], [mk3], now=now) == []
        # in-play rejected
        assert p.candidates([dict(ev, commence_time=(now - datetime.timedelta(minutes=5))
                             .astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))],
                            [mk], now=now) == []
        # scale mode trades the SAME band (the gate changes sizing, not the band)
        p2 = SharpEV.__new__(SharpEV)
        p2.start = 10000; p2.cash = 10000.0; p2.bets = {}; p2.pending = {}
        p2.realized = 0.0; p2.wins = p2.losses = p2.placed = p2.canceled = 0
        p2.fees = 0.0; p2.last_fetch = ""; p2.warned_no_key = False
        p2.credits_remaining = None; p2.next_starts = []; p2.shadow_day = ""
        p2.shadow_cache = {}; p2._shadow_rows = []; p2.last_scan = {}
        p2._pending_eval = {}
        p2.history = [{"era": ERA, "outcome": 1, "pnl": 10, "pside": 0.5}] * 30
        assert p2._gate()[0] == "scale"
        assert len(p2.candidates([ev], [mk], now=now)) == 1
        assert p2.candidates([ev_big], [mk], now=now) == []
        # placement rests a maker order (no cash moves), fill on trade-through
        n = p.place(cands)
        assert n == 1 and p.placed == 1 and len(p.pending) == 1 and not p.bets
        assert p.cash == 10000.0
        tk0 = list(p.pending)[0]
        # REST-TIME CAP: expire is at most MAX_REST_H from placement
        expd = datetime.datetime.fromisoformat(p.pending[tk0]["expire"])
        assert expd <= datetime.datetime.now(ET) + datetime.timedelta(
            hours=MAX_REST_H, minutes=1)
        # PENDING REVALIDATION: fair collapses -> the resting order is canceled
        p._pending_eval = {tk0: (0.49, 50, 53)}   # blend ~49.8 vs entry 50 = edge<0
        assert p.revalidate_pending() == 1 and not p.pending and p.canceled == 1
        # re-place for the fill/settle flow
        p._pending_eval = {}
        cands = p.candidates([ev], [mk], now=now)
        assert p.place(cands) == 1
        # revalidation keeps a still-sane order
        p._pending_eval = {tk0: (0.518, 50, 53)}
        assert p.revalidate_pending() == 0 and len(p.pending) == 1
        p.check_fills(quotes={tk0: {"yes_bid": 50, "yes_ask": 53, "last_price": 50}})
        assert len(p.bets) == 1 and not p.pending and p.cash < 10000.0
        b = list(p.bets.values())[0]
        assert b["entry"] * b["count"] <= PROBE_COST_CENTS
        p.fetch_result = lambda tk: "yes"
        os.makedirs("logs", exist_ok=True)
        p.settle()                       # game hasn't started -> nothing settles
        assert p.wins == 0 and p.bets
        list(p.bets.values())[0]["start"] = (
            now - datetime.timedelta(hours=4)).isoformat(timespec="minutes")
        p.settle()
        assert p.wins == 1 and p.realized > 0
        # NFL-preseason style series (quoted, but games months out) is skipped
        far = now + datetime.timedelta(days=60)
        fmk = {"ticker": "KXNFLGAME-26%s%02d%02d%02dDALNYG-DAL" % (
            ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"][far.month-1],
            far.day, far.hour, far.minute), "yes_bid": 45, "yes_ask": 48}
        assert not SharpEV._near_game([fmk], now)
        assert SharpEV._near_game([mk], now)
        print("sharp_ev self-test PASSED (devig, parse, match, filters, band+ceiling, "
              "stale-odds gate, rest cap, revalidation, rest/fill, near-game, shadow, settle)")
    else:
        p = SharpEV()
        nc, np_ = p.step(force=True)
        s = p.summary()
        print("sharp-ev: %d candidates, %d placed | bank $%.2f | %dW/%dL | "
              "pending %d | gate %s %d/30 | credits left %s"
              % (nc, np_, s["cash"], s["wins"], s["losses"], s["pending"],
                 s["gate"], s["gate_n"], p.credits_remaining))
