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
SIZE = int(os.environ.get("SPORTS_SIZE", "5"))
MAX_OPEN = int(os.environ.get("SPORTS_MAX_OPEN", "10"))
SELL_LO_C = int(os.environ.get("SPORTS_SELL_LO", "97"))
SELL_HI_C = int(os.environ.get("SPORTS_SELL_HI", "99"))
CLOSE_H = float(os.environ.get("SPORTS_CLOSE_H", "36"))
GATE_N = int(os.environ.get("SPORTS_GATE_N", "200"))
PM_GAMMA = os.environ.get("SPORTS_PM_GAMMA",
                          "https://gamma-api.polymarket.com")

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
        rows, cursor = [], None
        try:
            for _ in range(4):
                params = {"status": "open", "limit": 200,
                          "with_nested_markets": "true"}
                if cursor:
                    params["cursor"] = cursor
                d = requests.get(we.KALSHI + "/events", params=params,
                                 timeout=20).json()
                for ev in d.get("events") or []:
                    if (ev.get("category") or "").lower() != "sports":
                        continue
                    for mk in ev.get("markets") or []:
                        rows.append((ev, mk))
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
            for offset in (0, 500):
                d = requests.get(
                    PM_GAMMA + "/markets",
                    params={"closed": "false", "limit": 500,
                            "offset": offset}, timeout=20).json()
                for m in (d if isinstance(d, list) else []):
                    cat = (m.get("category") or "").lower()
                    if "sport" not in cat and cat not in (
                            "nfl", "nba", "mlb", "nhl", "soccer"):
                        continue
                    try:
                        outs = json.loads(m.get("outcomes") or "[]")
                        prices = json.loads(m.get("outcomePrices") or "[]")
                    except Exception:
                        continue
                    if len(outs) != len(prices) or not outs:
                        continue
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
        return None

    # ---- the cycle ----
    def step(self):
        self.settle()
        mkts = self.fetch_kalshi_sports()
        self.offer_check(mkts)
        self.place(mkts)
        self.save()
        return len(self.bets)

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
                             "edge": round(edge_c, 1),
                             "rungs": rungs, "ots": now(), "era": ERA}
            self.fees_c += fee
            self.placed += 1
            placed += 1
            print(f"  SPORTS(paper) BUY {tk}: {SIZE}x @ {ask}c "
                  f"(fair {fair:.2f}, edge {edge_c:.1f}c) - {mk['team']}")
        return placed
