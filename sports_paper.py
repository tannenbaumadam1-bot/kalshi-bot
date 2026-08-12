"""SPORTS PAPER BOOK (sports1) - the offer-side template on sports,
zero real dollars, built to produce a GO-LIVE-GRADE dataset.

The thesis (8/12, Adam-approved): the weather machine's economics -
buy a favorite with measurable edge, rest premium sells at 97/99 that
certainty-chasers lift, recycle - should transfer to sports, where the
books are 100x deeper. Entries are anchored to POLYMARKET's sports
prices (deep, public, free): we buy a Kalshi favorite only when its ask
is cheap against the anchor by EDGE_MIN_C after the exact taker fee.

REALISM RULES (so the dataset supports a live/no-live decision):
  - entries fill at the REAL Kalshi ask, never mid, fee_cents() exact
  - offer lifts require the market BID to trade AT/THROUGH our rung -
    strictly conservative (real lifts also come from takers crossing
    early, which we do not credit)
  - settlements graded from Kalshi's own market result, the same
    truth feed the live books use
  - one position per EVENT (game), MAX_OPEN games, fixed 5-lot size
  - every refusal counted; every sale graded vs eventual settlement
GO-LIVE GATE: >= GATE_N settled outcomes AND the Wilson lower bound
of the win rate clearing the lane's own fee-adjusted breakeven.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re

import requests

import weather_edge as we
from kalshibot.fees import fee_cents
from weather_paper import fetch_result

STATE = os.environ.get("SPORTS_PAPER_STATE",
                       os.path.join("logs", "sports_paper_state.json"))
ERA = "sports1"
EDGE_MIN_C = float(os.environ.get("SPORTS_EDGE_MIN_C", "3"))
ENTRY_MIN = int(os.environ.get("SPORTS_ENTRY_MIN", "65"))
ENTRY_MAX = int(os.environ.get("SPORTS_ENTRY_MAX", "90"))
MAX_SPREAD = int(os.environ.get("SPORTS_MAX_SPREAD", "5"))
# 8/12 (Adam): 5-contract MINIMUM, same floor as the live books - the
# fee-rounding math (a 1-lot pays the same rounded-up cent as 5) and
# the go-live dataset both need real-sized trades. No config may take
# the size below the floor.
MIN_CONTRACTS = int(os.environ.get("SPORTS_MIN_CONTRACTS", "5"))
SIZE = max(MIN_CONTRACTS, int(os.environ.get("SPORTS_SIZE", "5")))
MAX_OPEN = int(os.environ.get("SPORTS_MAX_OPEN", "10"))
SELL_LO_C = int(os.environ.get("SPORTS_SELL_LO", "97"))
SELL_HI_C = int(os.environ.get("SPORTS_SELL_HI", "99"))
CLOSE_H = float(os.environ.get("SPORTS_CLOSE_H", "36"))
GATE_N = int(os.environ.get("SPORTS_GATE_N", "200"))
PM_GAMMA = os.environ.get("SPORTS_PM_GAMMA",
                          "https://gamma-api.polymarket.com")
# 8/12 anchor-quality guards (the pro-desk standard): an anchor is only
# an anchor if it is LIQUID and SANE. Thin Polymarket markets can show
# stale prints that manufacture phantom edge.
PM_MIN_VOL = float(os.environ.get("SPORTS_PM_MIN_VOL", "10000"))
# 8/12 DUAL-ANCHOR VETO (the sharp-desk standard): Polymarket AND the
# devigged DraftKings/FanDuel consensus (SharpAPI free tier) must agree
# within VETO_DIFF or the trade is refused. When the sharp feed has no
# match, PM-only entries are still allowed but TAGGED (anchors=1) so
# the gate dataset can judge the cohorts separately.
VETO_DIFF = float(os.environ.get("SPORTS_ANCHOR_VETO_DIFF", "0.05"))
SHARP_URL = os.environ.get("SPORTS_SHARP_URL",
                           "https://api.sharpapi.io/api/v1")


def _sharp_key():
    k = os.environ.get("SPORTS_SHARPAPI_KEY")
    if k:
        return k.strip()
    try:
        return open("sharpapi_key.txt").read().strip()
    except Exception:
        return ""
FAIR_MIN = float(os.environ.get("SPORTS_FAIR_MIN", "0.50"))
FAIR_MAX = float(os.environ.get("SPORTS_FAIR_MAX", "0.98"))

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s):
    return set(_WORD.findall((s or "").lower()))


def _wilson_lb(w, n, z=1.0):
    if n <= 0:
        return 0.0
    ph = w / n
    d = 1.0 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    r = (z / d) * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return max(0.0, c - r)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class SportsPaper:
    def __init__(self):
        self.bets = {}        # tk -> position (with offer rungs)
        self.history = []
        self.pnl_days = {}
        self.wins = 0
        self.losses = 0
        self.realized_c = 0.0
        self.fees_c = 0.0
        self.placed = 0
        self.sold = 0
        self.sold_net_c = 0.0
        self.miss = {}        # refusal reason -> count
        self.gate = {"w": 0, "l": 0, "pnl": 0.0, "ben": 0.0}
        self.sold_log = []    # sales graded vs settlement
        self._pm_cache = {"ts": None, "rows": []}
        self._sharp_cache = {"ts": None, "rows": []}
        self.load()

    # ---- persistence ----
    def load(self):
        if os.path.exists(STATE):
            try:
                d = json.load(open(STATE))
                if d.get("era") != ERA:
                    return
                for k in ("bets", "history", "pnl_days", "wins", "losses",
                          "realized_c", "fees_c", "placed", "sold",
                          "sold_net_c", "miss", "gate", "sold_log"):
                    if k in d:
                        setattr(self, k, d[k])
            except Exception:
                pass

    def save(self):
        n = self.gate.get("w", 0) + self.gate.get("l", 0)
        lb = _wilson_lb(self.gate.get("w", 0), n)
        be = (self.gate.get("ben", 0.0) / n) if n else None
        try:
            os.makedirs("logs", exist_ok=True)
            json.dump(
                {"updated": now(), "era": ERA,
                 "bets": self.bets, "history": self.history[-200:],
                 "pnl_days": self.pnl_days,
                 "wins": self.wins, "losses": self.losses,
                 "realized_c": self.realized_c, "fees_c": self.fees_c,
                 "placed": self.placed, "sold": self.sold,
                 "sold_net_c": self.sold_net_c,
                 "miss": self.miss, "gate": self.gate,
                 "sold_log": self.sold_log[-200:],
                 # 8/12: anchor-index sizes on the tracker - an EMPTY pm
                 # index (this launch bug) is now one glance, not a
                 # session of funnel archaeology
                 "pm_idx": len(self._pm_cache.get("rows") or []),
                 "sharp_idx": len(self._sharp_cache.get("rows") or []),
                 "summary": {
                     "mode": "PAPER",
                     "net": round(self.realized_c / 100.0, 2),
                     "fees": round(self.fees_c / 100.0, 2),
                     "wins": self.wins, "losses": self.losses,
                     "open": len(self.bets), "placed": self.placed,
                     "sold": self.sold,
                     "sold_net": round(self.sold_net_c / 100.0, 2),
                     "day_pnl": self.pnl_days.get(
                         datetime.date.today().isoformat(), 0.0),
                     "gate": {"n": n, "w": self.gate.get("w", 0),
                              "l": self.gate.get("l", 0),
                              "pnl": round(self.gate.get("pnl", 0.0), 2),
                              "lb": round(lb, 4) if n else None,
                              "be": round(be, 4) if be is not None else None,
                              "need": GATE_N,
                              "ready": bool(n >= GATE_N and be is not None
                                            and lb > be)},
                     "rules": {"edge_min_c": EDGE_MIN_C,
                               "band": [ENTRY_MIN, ENTRY_MAX],
                               "size": SIZE, "max_open": MAX_OPEN,
                               "sell": [SELL_LO_C, SELL_HI_C],
                               "anchor": "polymarket",
                               "fills": "real ask + exact fees; lifts "
                                        "only on bid-through (conservative)"}},
                 }, open(STATE, "w"))
        except Exception:
            pass

    def _day_add(self, net_c):
        d = datetime.date.today().isoformat()
        self.pnl_days[d] = round(self.pnl_days.get(d, 0.0) + net_c / 100.0, 2)

    def _miss_add(self, why):
        self.miss[why] = self.miss.get(why, 0) + 1

    # ---- data feeds (each overridable in tests) ----
    def fetch_kalshi_sports(self):
        """Open Kalshi sports WINNER markets closing within CLOSE_H, with
        live quotes. Discovery: events pages filtered to Sports."""
        # 8/12 FIX (Adam caught the empty book): /events pagination never
        # reaches tonight's games - page one is full of 2043-dated
        # futures. Sweep /markets by CLOSE-TIME WINDOW instead, which is
        # exactly the recycling question: what settles soon?
        rows, cursor = [], None
        try:
            import time as _t
            nowts = int(_t.time())
            for _ in range(8):
                params = {"status": "open", "limit": 1000,
                          "min_close_ts": nowts,
                          "max_close_ts": nowts + int(CLOSE_H * 3600)}
                if cursor:
                    params["cursor"] = cursor
                d = requests.get(we.KALSHI + "/markets", params=params,
                                 timeout=25).json()
                for mk in d.get("markets") or []:
                    tk = mk.get("ticker") or ""
                    # GAME WINNERS ONLY: no parlays/multi-game products
                    # (KXMVE*), no spreads/totals/props/series - wrong
                    # anchors manufacture phantom edge
                    if tk.startswith("KXMVE"):
                        continue
                    t = (mk.get("title") or "").lower()
                    if "winner" not in t:
                        continue
                    if any(w in t for w in ("series", "championship",
                                            "by ", "margin", "total",
                                            "spread", "parlay")):
                        continue
                    rows.append((
                        {"event_ticker": mk.get("event_ticker"),
                         "title": mk.get("title")}, mk))
                cursor = d.get("cursor")
                if not cursor:
                    break
        except Exception:
            return []
        out, nowdt = [], datetime.datetime.now(datetime.timezone.utc)
        for ev, mk in rows:
            try:
                ct = mk.get("close_time") or ""
                cdt = datetime.datetime.fromisoformat(
                    ct.replace("Z", "+00:00"))
                hrs = (cdt - nowdt).total_seconds() / 3600.0
            except Exception:
                continue
            if hrs <= 0 or hrs > CLOSE_H:
                continue
            def _px(m, key):
                v = m.get(key + "_dollars")
                if v is not None:
                    try:
                        return int(round(float(v) * 100))
                    except (TypeError, ValueError):
                        return 0
                try:
                    return int(m.get(key) or 0)
                except (TypeError, ValueError):
                    return 0
            yb, ya = _px(mk, "yes_bid"), _px(mk, "yes_ask")
            if yb <= 0 or ya <= 0:
                continue
            out.append({"ticker": mk.get("ticker"),
                        "event": ev.get("event_ticker"),
                        "title": ev.get("title") or "",
                        "team": mk.get("yes_sub_title") or "",
                        "yes_bid": yb, "yes_ask": ya, "hrs": hrs})
        return out

    def fetch_pm_index(self):
        """Polymarket sports markets: [(question_tokens, outcome->prob)].
        Cached 10 minutes - the anchor, not the venue."""
        ts = self._pm_cache.get("ts")
        if ts and (datetime.datetime.now()
                   - datetime.datetime.fromisoformat(ts)
                   ).total_seconds() < 600:
            return self._pm_cache["rows"]
        rows = []
        try:
            # 8/12 FIX (book placed ZERO since launch): gamma /markets
            # rows carry NO 'category' field at all - the old category
            # filter matched nothing, the index was always EMPTY, and
            # every candidate died no_anchor (38/38 on the tracker).
            # Verified live: game rows are identified by
            # sportsMarketType == "moneyline", and the close-window
            # params return today's games directly instead of paging
            # blind through thousands of futures.
            t0 = datetime.datetime.utcnow()
            t1 = t0 + datetime.timedelta(hours=CLOSE_H)
            for offset in (0, 500):
                d = requests.get(
                    PM_GAMMA + "/markets",
                    params={"closed": "false", "limit": 500,
                            "offset": offset,
                            "order": "volumeNum", "ascending": "false",
                            "end_date_min":
                                t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "end_date_max":
                                t1.strftime("%Y-%m-%dT%H:%M:%SZ")},
                    timeout=20).json()
                for m in (d if isinstance(d, list) else []):
                    if m.get("sportsMarketType") != "moneyline":
                        continue    # games only: no futures/spreads/props
                    try:
                        outs = json.loads(m.get("outcomes") or "[]")
                        prices = json.loads(m.get("outcomePrices") or "[]")
                    except Exception:
                        continue
                    if len(outs) != len(prices) or not outs:
                        continue
                    try:
                        vol = float(m.get("volume") or m.get("volumeNum")
                                    or 0)
                    except (TypeError, ValueError):
                        vol = 0.0
                    if vol < PM_MIN_VOL:
                        continue    # thin anchor = no anchor
                    probs = {}
                    for o, p in zip(outs, prices):
                        try:
                            probs[str(o)] = float(p)
                        except (TypeError, ValueError):
                            pass
                    if probs:
                        rows.append({"q": m.get("question") or "",
                                     "toks": sorted(_tokens(
                                         m.get("question"))),
                                     "probs": probs})
        except Exception:
            rows = self._pm_cache["rows"] or []
        self._pm_cache = {"ts": now(), "rows": rows}
        return rows

    def fetch_sharp_index(self):
        """Devigged DK/FanDuel moneyline consensus via SharpAPI (free
        tier, 60s-delayed - irrelevant pre-game). Same row shape as the
        PM index so anchor_prob works on both. Cached 10 minutes."""
        key = _sharp_key()
        if not key:
            return []
        ts = self._sharp_cache.get("ts")
        if ts and (datetime.datetime.now()
                   - datetime.datetime.fromisoformat(ts)
                   ).total_seconds() < 600:
            return self._sharp_cache["rows"]
        games = {}
        try:
            offset = 0
            for _ in range(2):
                d = requests.get(SHARP_URL + "/odds",
                                 params={"market_type": "moneyline",
                                         "limit": 500, "offset": offset},
                                 headers={"X-API-Key": key},
                                 timeout=20).json()
                for r in d.get("data") or []:
                    if (r.get("market_type") != "moneyline"
                            or r.get("is_live")):
                        continue
                    g = games.setdefault(
                        r.get("event_id"),
                        {"home": r.get("home_team") or "",
                         "away": r.get("away_team") or "", "books": {}})
                    try:
                        p = float(r.get("odds_probability") or 0)
                    except (TypeError, ValueError):
                        continue
                    if p > 0:
                        g["books"].setdefault(
                            r.get("sportsbook"), {})[
                            r.get("selection") or ""] = p
                pg = d.get("pagination") or {}
                if not pg.get("has_more") or not pg.get("next_offset"):
                    break
                offset = pg["next_offset"]
            rows = []
            for g in games.values():
                acc = {}
                for probs in g["books"].values():
                    if len(probs) != 2:
                        continue        # need both sides to strip vig
                    tot = sum(probs.values())
                    if tot <= 0:
                        continue
                    for sel, p in probs.items():
                        acc.setdefault(sel, []).append(p / tot)
                if not acc:
                    continue
                q = f"{g['away']} at {g['home']}"
                rows.append({"q": q, "toks": sorted(_tokens(q)),
                             "probs": {s: sum(v) / len(v)
                                       for s, v in acc.items()}})
            self._sharp_cache = {"ts": now(), "rows": rows}
        except Exception:
            rows = self._sharp_cache["rows"] or []
        return rows

    def anchor_prob(self, mk, pm_rows):
        """Polymarket-implied probability for this Kalshi market's YES
        team, or None when no confident match exists (skip, never guess)."""
        team_t = _tokens(mk.get("team"))
        title_t = _tokens(mk.get("title"))
        if not team_t:
            return None
        best = None
        for r in pm_rows:
            toks = set(r["toks"])
            if not team_t & toks:
                continue
            overlap = len((title_t | team_t) & toks)
            if overlap >= max(2, len(team_t)):
                score = overlap
                if best is None or score > best[0]:
                    best = (score, r)
        if best is None:
            return None
        probs = best[1]["probs"]
        for name, p in probs.items():
            if _tokens(name) & team_t:
                return p
        # 8/12: PM prices many games as per-team "Will <team> win on
        # <date>?" markets with Yes/No outcomes - the team lives in the
        # QUESTION (already token-matched above, opponents can't match
        # because the gate is on TEAM tokens), so Yes IS this team's
        # probability. Moneyline-only indexing keeps "win by 3+" and
        # futures phrasing out of this fallback.
        keys = {str(k).strip().lower() for k in probs}
        if keys == {"yes", "no"}:
            for k, p in probs.items():
                if str(k).strip().lower() == "yes":
                    return p
        return None

    # ---- the cycle ----
    def step(self):
        self.settle()
        mkts = self.fetch_kalshi_sports()
        self.offer_check(mkts)
        self.place(mkts)
        self.track_anchors()
        self.save()
        return len(self.bets)

    def track_anchors(self):
        """8/12: sports lines move on NEWS (injuries, lineups) in a way
        weather never does. Every cycle the anchor is re-read for held
        positions and the worst reading recorded - the dataset itself
        will show whether an anchor-stop is needed before going live,
        instead of importing weather's hold-forever doctrine untested."""
        if not self.bets:
            return
        pm_rows = self.fetch_pm_index()
        for tk, b in self.bets.items():
            fair = self.anchor_prob({"team": b.get("team"),
                                     "title": b.get("title")}, pm_rows)
            if fair is None:
                continue
            b["fair_now"] = round(fair, 3)
            lo = b.get("fair_min")
            b["fair_min"] = round(min(fair, lo) if lo is not None
                                  else fair, 3)

    def settle(self):
        for tk, b in list(self.bets.items()):
            res = fetch_result(tk)
            if res is None:
                continue
            won = (res == "yes")        # we only ever hold YES on the team
            remaining = b["count"]
            payout = 100 if won else 0
            net = (payout - b["entry"]) * remaining - b.get("fee", 0)
            self._book(tk, b, net, won, kind="settle",
                       exit_px=payout, count=remaining)
            del self.bets[tk]
        # grade past sales vs settlement (max 8 lookups/cycle)
        done = 0
        for row in self.sold_log:
            if row.get("res") is not None or done >= 8:
                continue
            res = fetch_result(row["tk"])
            if res is None:
                continue
            done += 1
            won = (res == "yes")
            would = (((100 if won else 0) - row["entry"]) * row["count"]
                     - fee_cents(row["entry"], row["count"], taker=True))
            row["res"] = res
            row["would_pnl"] = round(would / 100.0, 2)
            row["kept"] = round(row["pnl"] - row["would_pnl"], 2)

    def _book(self, tk, b, net_c, won, kind, exit_px, count):
        self.realized_c += net_c
        self._day_add(net_c)
        self.wins += int(net_c > 0)
        self.losses += int(net_c <= 0)
        # gate ledger: fee-adjusted breakeven per outcome
        self.gate["w" if net_c > 0 else "l"] = self.gate.get(
            "w" if net_c > 0 else "l", 0) + 1
        self.gate["pnl"] = round(self.gate.get("pnl", 0.0) + net_c / 100.0, 2)
        self.gate["ben"] = round(
            self.gate.get("ben", 0.0)
            + (b["entry"] + b.get("fee", 0) / max(1, b["count"])) / 100.0, 4)
        self.history.append({"tk": tk, "title": b.get("title", ""),
                             "team": b.get("team", ""),
                             "entry": b["entry"], "count": count,
                             "fair": b.get("fair"), "edge": b.get("edge"),
                             "kind": kind, "exit_px": exit_px,
                             "fair_end": b.get("fair_now"),
                             "fair_min": b.get("fair_min"),
                             "anchors": b.get("anchors"),
                             "sold": kind == "sold",
                             "pnl": round(net_c / 100.0, 2),
                             "ts": now(), "ots": b.get("ots", ""),
                             "era": ERA})
        self.history = self.history[-400:]

    def offer_check(self, mkts):
        """Conservative lift sim: a rung sells only when the market BID
        trades at/through it."""
        by_tk = {m["ticker"]: m for m in mkts}
        for tk, b in list(self.bets.items()):
            mk = by_tk.get(tk)
            if mk is None:
                continue
            bid = mk["yes_bid"]
            for rung in list(b.get("rungs") or []):
                px, ct = rung
                if bid < px or ct <= 0:
                    continue
                sell_fee = fee_cents(px, ct, taker=False)
                fee_share = int(round(b.get("fee", 0) * ct
                                      / max(1, b["count"])))
                net = (px - b["entry"]) * ct - fee_share - sell_fee
                self.fees_c += sell_fee
                self.sold += 1
                self.sold_net_c = round(self.sold_net_c + net, 1)
                self.sold_log.append({"tk": tk, "entry": b["entry"],
                                      "px": px, "count": ct,
                                      "pnl": round(net / 100.0, 2),
                                      "ts": now(), "res": None})
                self._book(tk, b, net, net > 0, kind="sold",
                           exit_px=px, count=ct)
                b["count"] -= ct
                b["fee"] = max(0, b.get("fee", 0) - fee_share)
                b["rungs"].remove(rung)
            if b["count"] <= 0:
                del self.bets[tk]

    def place(self, mkts):
        if len(self.bets) >= MAX_OPEN:
            return 0
        pm_rows = self.fetch_pm_index()
        sharp_rows = self.fetch_sharp_index()
        open_events = {b.get("event") for b in self.bets.values()}
        placed = 0
        for mk in mkts:
            if len(self.bets) >= MAX_OPEN:
                break
            tk = mk["ticker"]
            if tk in self.bets or mk.get("event") in open_events:
                continue
            ask, bid = mk["yes_ask"], mk["yes_bid"]
            if ask < ENTRY_MIN or ask > ENTRY_MAX:
                continue
            if ask - bid > MAX_SPREAD:
                self._miss_add("spread")
                continue
            fair = self.anchor_prob(mk, pm_rows)
            if fair is None:
                self._miss_add("no_anchor")
                continue
            if not (FAIR_MIN <= fair <= FAIR_MAX):
                self._miss_add("anchor_insane")
                continue        # a 65-90c ask against a <50% or >98%
                                # anchor is a mismatch, not an edge
            # 8/12 dual-anchor veto: the sharp consensus must agree
            sh_fair = (self.anchor_prob(mk, sharp_rows)
                       if sharp_rows else None)
            anchors = 1
            if sh_fair is not None:
                if abs(sh_fair - fair) > VETO_DIFF:
                    self._miss_add("anchor_disagree")
                    continue
                fair = (fair + sh_fair) / 2.0
                anchors = 2
            fee = fee_cents(ask, SIZE, taker=True)
            edge_c = fair * 100 - ask - fee / SIZE
            if edge_c < EDGE_MIN_C:
                self._miss_add("thin_edge")
                continue
            lo = max(SELL_LO_C, ask + 6)
            rungs = ([[lo, SIZE - SIZE // 2], [SELL_HI_C, SIZE // 2]]
                     if lo < SELL_HI_C else [[SELL_HI_C, SIZE]])
            self.bets[tk] = {"event": mk.get("event"),
                             "title": mk.get("title"),
                             "team": mk.get("team"),
                             "entry": ask, "count": SIZE, "fee": fee,
                             "fair": round(fair, 3),
                             "fair_sharp": (round(sh_fair, 3)
                                            if sh_fair is not None else None),
                             "anchors": anchors,
                             "edge": round(edge_c, 1),
                             "rungs": rungs, "ots": now(), "era": ERA}
            self.fees_c += fee
            self.placed += 1
            placed += 1
            print(f"  SPORTS(paper) BUY {tk}: {SIZE}x @ {ask}c "
                  f"(fair {fair:.2f}, edge {edge_c:.1f}c) - {mk['team']}")
        return placed
