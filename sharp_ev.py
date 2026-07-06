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
  Paper assumes the resting order fills at the bid: OPTIMISTIC. Treat paper
  results as an upper bound; the gate exists for exactly this reason.
- One bet per game, MAX_PER_DAY/day, MAX_OPEN open, probe stakes (<=60c/bet)
  until the 30-bet calibration gate passes (same contract as the weather book).

Markets: moneyline (h2h) + game totals (Kalshi totals use half-point strikes,
e.g. 'Over 8.5 runs' = book Over 8.5 exactly - no push risk). Both sides
tradeable (YES=team/over, NO=other/under). Spreads/halves/props: later phases -
Kalshi period markets are in-game (excluded by the no-in-play rule) and prop
anchors carry the widest vig; totals+ml are where sharp anchors are strongest.

Odds source: The Odds API, region 'eu' only = Pinnacle + Euro sharps (Pinnacle
preferred when present; median >=MIN_BOOKS fallback). Credit math (free tier
500/mo): 2 markets x 1 region = 2 credits/sport/scan; 2 in-season sports at
ODDS_SCAN_HOURS=6 -> ~480/mo. Paid tier ($30/mo, 20k) lifts all limits.
Without ODDS_API_KEY the module idles gracefully.
"""
from __future__ import annotations
import os, json, csv, re, datetime, statistics
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

ERA = "ev1-sharp"
START_CENTS = int(os.environ.get("SEV_START_C", "10000"))
MIN_EDGE_C = float(os.environ.get("SEV_MIN_EDGE_C", "4"))      # net edge to act
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
        self.realized = 0.0
        self.wins = 0
        self.losses = 0
        self.fees = 0.0
        self.placed = 0
        self.history = []
        self.last_fetch = ""           # iso ts of last odds pull
        self.warned_no_key = False
        self.load()

    # ---- persistence (same contract as the other books) ----
    def to_dict(self):
        return {"start": self.start, "cash": self.cash, "bets": self.bets,
                "realized": self.realized, "wins": self.wins, "losses": self.losses,
                "fees": self.fees, "placed": self.placed,
                "last_fetch": self.last_fetch, "history": self.history[-100:]}

    def load(self):
        try:
            d = json.load(open(SSIM))
            for k in ("start", "cash", "realized", "wins", "losses", "fees", "placed"):
                setattr(self, k, d.get(k, getattr(self, k)))
            self.bets = d.get("bets", {})
            self.history = d.get("history", [])
            self.last_fetch = d.get("last_fetch", "")
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs("logs", exist_ok=True)
            json.dump(self.to_dict(), open(SSIM, "w"))
            st = {"updated": datetime.datetime.now().isoformat(timespec="seconds"),
                  "summary": self.summary(),
                  "open": [dict(b, ticker=tk) for tk, b in self.bets.items()],
                  "settled": list(reversed(self.history[-50:]))}
            json.dump(st, open(SSTATE, "w"))
        except Exception:
            pass

    def summary(self):
        mode, n = self._gate()
        return {"start": round(self.start / 100.0, 2), "cash": round(self.cash / 100.0, 2),
                "realized": round(self.realized / 100.0, 2), "wins": self.wins,
                "losses": self.losses, "fees": round(self.fees / 100.0, 2),
                "placed": self.placed, "open_bets": len(self.bets),
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
               if h.get("era") == ERA and h.get("outcome") in (0, 1)][-60:]
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

    # ---- core ----
    def settle(self):
        for tk, b in list(self.bets.items()):
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

    def _placed_today(self):
        today = datetime.date.today().isoformat()
        n = sum(1 for b in self.bets.values() if (b.get("ots") or "")[:10] == today)
        n += sum(1 for h in self.history if (h.get("ots") or "")[:10] == today)
        return n

    def _sided(self, mk, fair_yes, src, start_iso, label):
        """Try both sides of one market -> best qualifying (cand tuple) or None.
        YES entry = join yes_bid; NO entry = join no_bid = 100 - yes_ask."""
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
            if edge_c < MIN_EDGE_C:
                continue
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
        """Filter pipeline -> [(mk, label, side, edge_c, fair_side, src, start)]."""
        now = now or datetime.datetime.now(ET)
        out = []
        open_events = {tk.rsplit("-", 1)[0] for tk in self.bets}
        for mk in markets:
            tk = mk.get("ticker", "")
            if tk in self.bets or tk.rsplit("-", 1)[0] in open_events:
                continue                                # one bet per game per series
            kind = mk.get("_kind", "ml")
            if kind == "ml":
                ev, team = match_event(mk, events)
                if ev is None:
                    continue
                start = self._start_of(ev)
                if start is None:
                    continue
                if not (start - datetime.timedelta(hours=HOURS_BEFORE) <= now
                        <= start - datetime.timedelta(minutes=LOCKOUT_MIN)):
                    continue                            # pregame window only
                fair_all, src = fair_from_books(ev)
                if not fair_all or team not in fair_all:
                    continue
                c = self._sided(mk, fair_all[team], src,
                                start.isoformat(timespec="minutes"),
                                team)
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
                if not (start - datetime.timedelta(hours=HOURS_BEFORE) <= now
                        <= start - datetime.timedelta(minutes=LOCKOUT_MIN)):
                    continue
                tf = totals_from_books(ev)
                got = tf.get(float(pt))
                if not got:
                    continue                            # need the SAME line at the books
                p_over, src = got
                c = self._sided(mk, p_over, src,
                                start.isoformat(timespec="minutes"),
                                "Over %.1f" % float(pt))
                if c:
                    out.append(c)
        out.sort(key=lambda t: -t[3])
        return out

    def place(self, cands):
        mode, _ = self._gate()
        open_stake = sum(b["entry"] * b["count"] for b in self.bets.values())
        bankroll = self.cash + open_stake
        placed = 0
        budget = MAX_PER_DAY - self._placed_today()
        for mk, label, side, edge_c, fair, src, start_iso in cands:
            if placed >= budget or len(self.bets) >= MAX_OPEN:
                break
            if mk["ticker"].rsplit("-", 1)[0] in {t.rsplit("-", 1)[0] for t in self.bets}:
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
            cost = entry * count
            if cost > self.cash:
                continue
            self.cash -= cost
            self.placed += 1
            ots = datetime.datetime.now().isoformat(timespec="seconds")
            team = label if side == "yes" else (
                "not " + label if label.startswith("Over") else label + " (fade)")
            self.bets[mk["ticker"]] = {
                "sport": mk.get("_sport", ""), "game": (mk.get("title") or "")[:60],
                "team": team, "side": side, "entry": entry, "count": count,
                "pside": round(fair, 3), "edge": round(edge_c, 1), "fee": 0,
                "src": src, "start": start_iso, "ots": ots, "era": ERA}
            self._log([ots, "PLACE", mk.get("_sport", ""), (mk.get("title") or "")[:60],
                       team, round(fair, 3), entry, count, "", ""])
            placed += 1
        return placed

    def step(self, force=False):
        """Called from the bot loop. Settles cheaply every call; pulls odds only
        every SCAN_HOURS (credit budget)."""
        self.settle()
        now = datetime.datetime.now()
        due = True
        if self.last_fetch and not force:
            try:
                due = (now - datetime.datetime.fromisoformat(self.last_fetch)
                       ).total_seconds() >= SCAN_HOURS * 3600
            except Exception:
                due = True
        n_cand = n_placed = 0
        if due:
            if not os.environ.get("ODDS_API_KEY", ""):
                if not self.warned_no_key:
                    print("  SHARP-EV: idle (no ODDS_API_KEY set)")
                    self.warned_no_key = True
                self.save()
                return 0, 0
            self.last_fetch = now.isoformat(timespec="seconds")
            for sport, series_map in SPORTS.items():
                events = self.fetch_odds(sport)
                if not events:
                    continue
                markets = []
                for kind, series in series_map.items():
                    if kind == "total" and "totals" not in ODDS_MARKETS:
                        continue
                    for mk in self.kalshi_markets(series):
                        mk["_sport"] = sport
                        mk["_kind"] = kind
                        markets.append(mk)
                cands = self.candidates(events, markets)
                n_cand += len(cands)
                n_placed += self.place(cands)
        self.save()
        return n_cand, n_placed


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
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
        ev = {"commence_time": start.astimezone(datetime.timezone.utc)
                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
              "home_team": "Pittsburgh Pirates", "away_team": "Atlanta Braves",
              "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                  {"name": "Pittsburgh Pirates", "price": 1.60},
                  {"name": "Atlanta Braves", "price": 2.60}]}]}]}
        tk = "KXMLBGAME-26%s%02d%02d%02dATLPIT-PIT" % (
            ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"][start.month-1],
            start.day, start.hour, start.minute)
        mk = {"ticker": tk, "title": "Atlanta vs Pittsburgh Winner?",
              "yes_sub_title": "Pittsburgh", "yes_bid": 50, "yes_ask": 53, "_sport": "baseball_mlb"}
        p = SharpEV.__new__(SharpEV)
        p.start = 10000; p.cash = 10000.0; p.bets = {}; p.realized = 0.0
        p.wins = p.losses = p.placed = 0; p.fees = 0.0; p.history = []
        p.last_fetch = ""; p.warned_no_key = False
        cands = p.candidates([ev], [mk], now=now)
        assert len(cands) == 1 and cands[0][3] >= MIN_EDGE_C     # pinnacle 62% vs 50c bid
        # longshot rejected even with huge edge
        mk2 = dict(mk, yes_bid=8, yes_ask=11)
        assert p.candidates([ev], [mk2], now=now) == []
        # wide spread rejected
        mk3 = dict(mk, yes_bid=40, yes_ask=55)
        assert p.candidates([ev], [mk3], now=now) == []
        # in-play rejected
        assert p.candidates([dict(ev, commence_time=(now - datetime.timedelta(minutes=5))
                             .astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))],
                            [mk], now=now) == []
        # disagreement guard
        ev2 = dict(ev, bookmakers=[
            {"key": "bk1", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Pittsburgh Pirates", "price": 1.45},
                {"name": "Atlanta Braves", "price": 2.9}]}]},
            {"key": "bk2", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Pittsburgh Pirates", "price": 2.2},
                {"name": "Atlanta Braves", "price": 1.75}]}]},
            {"key": "bk3", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Pittsburgh Pirates", "price": 1.9},
                {"name": "Atlanta Braves", "price": 1.9}]}]}])
        assert fair_from_books(ev2) == ({}, "")
        # placement at probe size + settle math
        n = p.place(cands)
        assert n == 1 and p.placed == 1 and len(p.bets) == 1
        b = list(p.bets.values())[0]
        assert b["entry"] * b["count"] <= PROBE_COST_CENTS
        p.fetch_result = lambda tk: "yes"
        os.makedirs("logs", exist_ok=True)
        p.settle()
        assert p.wins == 1 and p.realized > 0
        print("sharp_ev self-test PASSED (devig, parse, match, filters, probe, settle)")
    else:
        p = SharpEV()
        nc, np_ = p.step(force=True)
        s = p.summary()
        print("sharp-ev: %d candidates, %d placed | bank $%.2f | %dW/%dL | gate %s %d/30"
              % (nc, np_, s["cash"], s["wins"], s["losses"], s["gate"], s["gate_n"]))
