"""THE TAPE RECORDER (8/19) - record everything, forever.

BlueWalker lesson #1 (Odds on Open, 8/13/26): "We record in the
billions a day... I think we have probably the best data set in
prediction markets out there... that allows us to run better
experiments and that allows us to yield better alpha."

Our version, sized to our niche: one JSONL line per executor cycle
capturing every order book the scan saw, our full position state,
marked NAV, and the cumulative counters. A few MB a day on the
droplet buys retroactive answers to every future question of the
ewin/dip/adverse-selection kind - studies that today take days of
forward telemetry become ten-minute queries over months of tape.

DESIGN RULES:
  - The recorder may NEVER break trading. Every public method is
    fully guarded; on any error it goes silent (and says so on the
    tracker via err_today).
  - Append-only daily files: logs/ticks/YYYY-MM-DD.jsonl
  - Retention sweep keeps RETAIN_DAYS days (default 120; ~1GB ceiling
    at current sizes, droplet has 25GB).
  - DRIFT_LIVE_RECORD=0 kills it.

Record shape (one line per cycle):
  {"ts": iso, "kind": "cycle", "mkts": [[tk, yb, ya, bsz, asz, vol,
   hrs], ...], "bets": [[tk, side, entry, ct, mk_px], ...],
   "resting": n, "offers": n, "dips": n, "nav": {...}, "exec": {...}}
"""

from __future__ import annotations

import datetime
import json
import os

DIR = os.environ.get("DRIFT_LIVE_RECORD_DIR", os.path.join("logs", "ticks"))
ON = os.environ.get("DRIFT_LIVE_RECORD", "1") == "1"
RETAIN_DAYS = int(os.environ.get("DRIFT_LIVE_RECORD_DAYS", "120"))


class Recorder:
    def __init__(self):
        self.lines_today = 0
        self.bytes_today = 0
        self.err_today = 0
        self._day = None
        self._fh = None

    # ---- internals ----
    def _roll(self):
        d = datetime.date.today().isoformat()
        if d == self._day and self._fh is not None:
            return
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._day = d
        self.lines_today = 0
        self.bytes_today = 0
        self.err_today = 0
        os.makedirs(DIR, exist_ok=True)
        self._fh = open(os.path.join(DIR, f"{d}.jsonl"), "a", buffering=1)
        self._sweep()

    def _sweep(self):
        """Retention: drop tape older than RETAIN_DAYS."""
        try:
            cut = (datetime.date.today()
                   - datetime.timedelta(days=RETAIN_DAYS)).isoformat()
            for f in os.listdir(DIR):
                if f.endswith(".jsonl") and f[:10] < cut:
                    try:
                        os.remove(os.path.join(DIR, f))
                    except OSError:
                        pass
        except Exception:
            pass

    # ---- public: one guarded write ----
    def write(self, obj):
        if not ON:
            return
        try:
            self._roll()
            line = json.dumps(obj, separators=(",", ":"),
                              default=str) + "\n"
            self._fh.write(line)
            self.lines_today += 1
            self.bytes_today += len(line)
        except Exception:
            self.err_today += 1

    def cycle(self, mkts, bets, resting_n, offers_n, dips_n, nav):
        """The per-cycle snapshot - the heart of the tape."""
        if not ON:
            return
        try:
            rec = {"ts": datetime.datetime.now().isoformat(
                       timespec="seconds"),
                   "kind": "cycle",
                   "mkts": [[m.get("ticker"), m.get("yes_bid"),
                             m.get("yes_ask"), m.get("bid_size"),
                             m.get("ask_size"), m.get("vol"),
                             m.get("hrs")] for m in (mkts or [])],
                   "bets": [[tk, b.get("side"), b.get("entry"),
                             b.get("count"), b.get("mk_px")]
                            for tk, b in (bets or {}).items()],
                   "resting": resting_n, "offers": offers_n,
                   "dips": dips_n, "nav": nav}
            self.write(rec)
        except Exception:
            self.err_today += 1

    def event(self, kind, **kw):
        """Point events worth their own line: fills, sells, cuts,
        refusal details - callers add them over time."""
        if not ON:
            return
        try:
            self.write({"ts": datetime.datetime.now().isoformat(
                            timespec="seconds"),
                        "kind": kind, **kw})
        except Exception:
            self.err_today += 1

    def stats(self):
        return {"on": ON, "lines": self.lines_today,
                "kb": round(self.bytes_today / 1024.0, 1),
                "errs": self.err_today, "days": RETAIN_DAYS}
