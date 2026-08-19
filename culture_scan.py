"""CULTURE SCANNER (8/19) - phase 0 of the culture nowcast engine.

BlueWalker (Odds on Open, 8/13/26): "Culture markets, man, there's so
much alpha there... systematic... so much information in the internet
that is not structured."

The thesis, in our house style: Kalshi's Entertainment books settle on
PUBLIC COUNTERS that accumulate toward settlement - Spotify daily
streams (kworb.net mirrors them, free), Rotten Tomatoes / Metacritic
scores (converge review by review), Billboard chart math. A counter
that ticks all week is a thermometer that climbs all day - the same
nowcast shape as weather - and the counterparty is FANS, the most
emotional retail on the exchange. Nobody systematic sits in these
books.

PHASE 0 (this module): watch, match, record. NO TRADING.
  - Scan every open Entertainment market on Kalshi with its book.
  - Classify into families (spotify / rt / metacritic / billboard /
    award / other) and measure LIQUIDITY (two-sided books).
  - Pull kworb's global daily chart and try to MATCH spotify-family
    markets to their live counter (the sports no_anchor lesson:
    matching is instrumented from day one, match failures are named).
  - Write everything to the tape (logs/ticks via Recorder) and publish
    logs/culture_state.json so the tracker proves it's alive.
Phase 1 (fair value + paper book with the 200-settle gate) is built ON
this data, not before it. PAPER_CULTURE=0 kills the scanner.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time

import requests

try:
    from recorder import Recorder
except Exception:
    Recorder = None

KALSHI = os.environ.get("CULTURE_KALSHI",
                        "https://api.elections.kalshi.com/trade-api/v2")
KWORB = os.environ.get("CULTURE_KWORB",
                       "https://kworb.net/spotify/country/global_daily.html")
STATE = os.environ.get("CULTURE_STATE",
                       os.path.join("logs", "culture_state.json"))
EVENT_PAGES = int(os.environ.get("CULTURE_EVENT_PAGES", "6"))
KWORB_TTL_S = int(os.environ.get("CULTURE_KWORB_TTL", "21600"))  # 6h
ERA = "culture0"

# ordered: SPECIFIC families first; spotify last because its terms
# (SONG/ALBUM/STREAM) appear inside award titles too
_FAMS = (
    ("rt", re.compile(r"^KXRT|ROTTEN", re.I)),
    ("metacritic", re.compile(r"^KXMC|METACRITIC", re.I)),
    ("billboard", re.compile(r"BILLBOARD", re.I)),
    ("youtube", re.compile(r"^KXYT|YOUTUBE", re.I)),
    ("award", re.compile(r"GRAM|OSCAR|EMMY|^KXGG|AWARD|CRITICS|SAG|^KXAMA"
                         r"|^KXBET|CMA|EUROVISION|GOLDEN", re.I)),
    ("spotify", re.compile(r"SPOTIFY|STREAM|RANKLISTSONG|TOPSTRM|TOPALBUM"
                           r"|SONG|ALBUM", re.I)),
)

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "will", "be", "on", "of", "by", "how", "many",
         "much", "streams", "stream", "song", "album", "get", "have",
         "spotify", "top", "in", "at", "what", "who", "vs", "to", "for"}


def _tokens(s):
    return {w for w in _WORD.findall((s or "").lower()) if w not in _STOP}


def _cents(mk, base):
    """Kalshi dual schema: '<base>_dollars' string-floats or '<base>'
    int cents (the same lesson as drift_live._kval)."""
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


def classify(ticker, title=""):
    blob = f"{ticker} {title}"
    for fam, rx in _FAMS:
        if rx.search(blob):
            return fam
    return "other"


class CultureScan:
    def __init__(self):
        self.rec = Recorder() if Recorder else None
        self._kworb = {"ts": 0.0, "rows": []}
        self.last = {}

    # ---- kworb: the public counter ----
    def fetch_kworb(self):
        """Rows: {artist, title, streams, d1 (day delta), d7, total} -
        cached KWORB_TTL_S. Parser is deliberately tolerant: phase 0's
        job is to MEASURE how well this source parses, kworb_rows on
        the tracker is the health gauge."""
        if time.time() - self._kworb["ts"] < KWORB_TTL_S and \
                self._kworb["rows"]:
            return self._kworb["rows"]
        rows = []
        try:
            html = requests.get(KWORB, timeout=20,
                                headers={"User-Agent":
                                         "kalshibot-culture/0.1"}).text
            # kworb uses RELATIVE hrefs (../artist/ID.html) - match
            # loosely on the path tail (verified against live HTML 8/19)
            for m in re.finditer(
                    r'artist/[^"]+\.html"[^>]*>([^<]+)</a>'
                    r'.{0,80}?track/[^"]+\.html"[^>]*>([^<]+)</a>'
                    r'(.*?)</tr>', html, re.S):
                artist, title, rest = m.group(1), m.group(2), m.group(3)
                nums = re.findall(r'>\s*([+-]?[\d,]{4,})\s*<', rest)
                vals = []
                for n in nums[:6]:
                    try:
                        vals.append(int(n.replace(",", "")))
                    except ValueError:
                        pass
                rows.append({"artist": artist.strip(),
                             "title": title.strip(),
                             "streams": vals[0] if vals else None,
                             "d1": vals[1] if len(vals) > 1 else None,
                             "d7": vals[2] if len(vals) > 2 else None,
                             "total": vals[-1] if len(vals) > 3 else None,
                             "toks": sorted(_tokens(f"{artist} {title}"))})
        except Exception:
            pass
        self._kworb = {"ts": time.time(), "rows": rows}
        return rows

    def match_kworb(self, title, rows):
        """Best token-overlap match of a Kalshi market title against the
        chart. Returns (row, score) or (None, 0)."""
        mt = _tokens(title)
        if not mt:
            return None, 0.0
        best, bs = None, 0.0
        for r in rows:
            rt = set(r.get("toks") or [])
            if not rt:
                continue
            inter = len(mt & rt)
            if not inter:
                continue
            score = inter / min(len(mt), len(rt))
            if score > bs:
                best, bs = r, score
        return (best, bs) if bs >= 0.5 else (None, bs)

    # ---- kalshi: every entertainment book ----
    def fetch_markets(self):
        """Open events with nested markets, filtered to Entertainment.
        Falls back to family-regex if the category field is absent -
        phase 0 logs which path fired."""
        out, cursor, used_category = [], None, False
        try:
            for _ in range(EVENT_PAGES):
                params = {"status": "open", "limit": 200,
                          "with_nested_markets": "true"}
                if cursor:
                    params["cursor"] = cursor
                d = requests.get(KALSHI + "/events", params=params,
                                 timeout=20).json()
                for ev in d.get("events") or []:
                    cat = (ev.get("category") or "")
                    is_ent = "entertain" in cat.lower()
                    if cat:
                        used_category = True
                    for mk in ev.get("markets") or []:
                        tk = mk.get("ticker") or ""
                        fam = classify(tk, mk.get("title") or "")
                        if not (is_ent or (not cat and fam != "other")):
                            continue
                        out.append({
                            "tk": tk, "event": ev.get("event_ticker"),
                            "title": (mk.get("title")
                                      or ev.get("title") or "")[:90],
                            "fam": fam,
                            "yb": _cents(mk, "yes_bid"),
                            "ya": _cents(mk, "yes_ask"),
                            "vol": (mk.get("volume")
                                    or mk.get("volume_fp")),
                            "oi": (mk.get("open_interest")
                                   or mk.get("open_interest_fp")),
                            "close": mk.get("close_time")})
                cursor = d.get("cursor")
                if not cursor:
                    break
        except Exception:
            pass
        return out, used_category

    # ---- the scan ----
    def step(self):
        mkts, used_cat = self.fetch_markets()
        rows = self.fetch_kworb()
        fams, liquid, matched, examples = {}, 0, 0, []
        for m in mkts:
            fams[m["fam"]] = fams.get(m["fam"], 0) + 1
            two_sided = bool(m["yb"]) and bool(m["ya"])
            if two_sided:
                liquid += 1
            if m["fam"] in ("spotify", "youtube") and rows:
                row, score = self.match_kworb(m["title"], rows)
                if row is not None:
                    matched += 1
                    m["kworb"] = {"artist": row["artist"],
                                  "title": row["title"],
                                  "streams": row["streams"],
                                  "d7": row["d7"]}
                    m["mscore"] = round(score, 2)
                    if len(examples) < 5 and two_sided:
                        examples.append({
                            "tk": m["tk"], "title": m["title"],
                            "yb": m["yb"], "ya": m["ya"],
                            "counter": row["title"],
                            "streams": row["streams"]})
        state = {"updated": datetime.datetime.now().isoformat(
                     timespec="seconds"),
                 "era": ERA, "mode": "SCAN",
                 "scanned": len(mkts), "families": fams,
                 "liquid": liquid, "matched": matched,
                 "kworb_rows": len(rows), "used_category": used_cat,
                 "examples": examples}
        self.last = state
        # the tape gets the FULL scan; the state file gets the summary
        if self.rec is not None:
            self.rec.write({"ts": state["updated"], "kind": "culture",
                            "mkts": [[m["tk"], m["fam"], m["yb"],
                                      m["ya"], m["vol"], m["oi"],
                                      (m.get("kworb") or {}).get(
                                          "streams")]
                                     for m in mkts]})
        try:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            json.dump(state, open(STATE, "w"))
        except Exception:
            pass
        return state
