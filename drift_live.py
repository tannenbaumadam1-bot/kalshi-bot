#!/usr/bin/env python3
"""Drift momentum LIVE executor - same brain as drift_paper, real orders.

The drift book is the first to pass its calibration gate (53-0 at settlement,
era drift1) and Adam ordered live prep 2026-07-23. This executor mirrors the
paper discipline exactly: maker-only entries (join the side bid), level
trigger >=80c / climb trigger 65-80c (+2c on rising volume, same-day only),
momentum stop <50c, trailing exit 15c off peak, one bet per city-day event,
probe stakes until the LIVE book passes its OWN 30-bet gate (era "dlive1").

Deliberately NOT in live v1 (post-gate upgrades, keep the first real dollars
simple): nickel lane (gap risk), pyramiding.

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
ARM_FILE = os.path.join("logs", "DRIFT_LIVE_ARMED")
LIVE_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

# momentum decays fast: an unfilled maker join is stale in 2h, not 4
REST_MAX_H = float(os.environ.get("DRIFT_LIVE_REST_MAX_H", "2"))
CYCLE_S = int(os.environ.get("DRIFT_LIVE_CYCLE_S", "600"))
GATE_MIN_N = dp.GATE_MIN_N
GATE_MAX_GAP = dp.GATE_MAX_GAP
PROBE_COST_CENTS = dp.PROBE_COST_CENTS
ERA = "dlive1"


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
        r = cfg.get("risk", {}) if isinstance(cfg, dict) else {}
        self.max_bet_c = int(float(r.get("max_position_dollars", 2.0)) * 100)
        self.max_open_c = int(float(r.get("max_open_dollars", 15.0)) * 100)
        self.max_day_loss_c = int(float(r.get("max_daily_loss_dollars", 3.0)) * 100)
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
        self.dry_balance_c = 10000
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
                          "dry_balance_c"):
                    if k in d:
                        setattr(self, k, d[k])
            except Exception:
                pass

    def save(self, balance_c=None):
        os.makedirs("logs", exist_ok=True)
        mode_gate, gate_n = self._gate()
        d = {"updated": now(), "mode": self.mode,
             "balance_c": balance_c,
             "bets": self.bets, "pending": self.pending,
             "last_mid": self.last_mid, "last_vol": self.last_vol,
             "realized_c": self.realized_c, "fees_c": self.fees_c,
             "wins": self.wins, "losses": self.losses,
             "placed": self.placed, "canceled": self.canceled,
             "day": self.day, "day_pnl_c": self.day_pnl_c,
             "dry_balance_c": self.dry_balance_c,
             "history": self.history[-200:],
             "summary": {
                 "mode": self.mode,
                 "net": round(self.realized_c / 100, 2),
                 "wins": self.wins, "losses": self.losses,
                 "open": len(self.bets), "resting": len(self.pending),
                 "placed": self.placed, "canceled": self.canceled,
                 "fees": round(self.fees_c / 100, 2),
                 "day_pnl": round(self.day_pnl_c / 100, 2),
                 "halted": self.halted,
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

    # ---- shared gate math (same contract as every book) ----
    def _gate(self):
        cur = [h for h in self.history if h.get("outcome") in (0, 1)][-60:]
        n = len(cur)
        if n < GATE_MIN_N:
            return "probe", n
        expectancy = sum(h["pnl"] for h in cur) / n
        pred = sum(h["pside"] for h in cur) / n
        act = sum(h["outcome"] for h in cur) / n
        if expectancy > 0 and (pred - act) <= GATE_MAX_GAP:
            return "scale", n
        return "probe", n

    def _roll_day(self):
        if today() != self.day:
            self.day = today()
            self.day_pnl_c = 0.0
            self.halted = False

    def open_cost_c(self):
        oc = sum(b["entry"] * b["count"] + b.get("fee", 0)
                 for b in self.bets.values())
        oc += sum(o["entry"] * o["count"] for o in self.pending.values())
        return oc

    def balance_c(self):
        if self.client is None:
            return self.dry_balance_c
        return self.client.get_balance_cents()

    # ---- resting order lifecycle (identical to weather_live) ----
    def check_orders(self):
        """Promote filled resting orders to positions; cancel stale ones."""
        if not self.pending:
            return
        resting_ids = set()
        if self.client is not None:
            try:
                resting_ids = {o.get("order_id") for o in self.client.get_resting_orders()}
            except Exception:
                return                      # can't verify -> touch nothing
        nowdt = datetime.datetime.now()
        for oid, o in list(self.pending.items()):
            tk = o["ticker"]
            if self.client is not None and oid not in resting_ids:
                filled = 0
                try:
                    for f in self.client.get_fills(limit=100):
                        if f.get("order_id") == oid:
                            filled += int(f.get("count", 0))
                except Exception:
                    filled = o["count"]     # assume full fill; settle() reconciles
                if filled > 0:
                    fee = fee_cents(o["entry"], filled, taker=False)
                    self.fees_c += fee
                    self.bets[tk] = {**{k: o[k] for k in
                                        ("side", "entry", "city", "strike",
                                         "kind", "cap", "hl", "pside", "date",
                                         "trig", "peak")},
                                     "count": filled, "fee": fee, "oid": oid,
                                     "ots": o.get("ots", now()), "era": ERA}
                    self._log([now(), "FILL", self.mode, o["city"], o["strike"],
                               o["hl"], o["side"], round(o["pside"], 3),
                               o["entry"], filled, "", "", oid])
                else:
                    self.canceled += 1
                del self.pending[oid]
                continue
            try:
                age_h = (nowdt - datetime.datetime.fromisoformat(o["ots"])).total_seconds() / 3600
            except Exception:
                age_h = 0
            if age_h > REST_MAX_H:
                if self.client is not None:
                    try:
                        self.client.cancel_order(oid)
                    except Exception:
                        continue
                self.canceled += 1
                self._log([now(), "CANCEL", self.mode, o["city"], o["strike"],
                           o["hl"], o["side"], round(o["pside"], 3),
                           o["entry"], o["count"], "", "", oid])
                del self.pending[oid]

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
            self.day_pnl_c += net
            if self.client is None:
                self.dry_balance_c += payout * b["count"]
            self.wins += int(won)
            self.losses += int(not won)
            self.history.append({"city": b["city"], "strike": b["strike"],
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
            del self.bets[tk]

    # ---- momentum stop + trailing exit (taker sells, same rules as paper) ----
    def stop_check(self, quotes=None):
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
            fade = (smid >= dp.DRIFT_STOP_C and peak - smid >= dp.FADE_DROP_C)
            if smid >= dp.DRIFT_STOP_C and not fade:
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
            net = (bid - b["entry"]) * cnt - b.get("fee", 0) - exit_fee
            self.realized_c += net
            self.day_pnl_c += net
            self.fees_c += exit_fee
            if self.client is None:
                self.dry_balance_c += bid * cnt - exit_fee
            self.history.append({"city": b["city"], "strike": b["strike"],
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
            del self.bets[tk]
            stopped += 1
        return stopped

    # ---- placement (maker resting orders, paper-identical triggers) ----
    def place(self, mkts=None):
        if self.day_pnl_c <= -self.max_day_loss_c:
            self.halted = True
            return 0
        try:
            balance_c = self.balance_c()
        except Exception:
            return 0
        if mkts is None:
            try:
                mkts = we.find_temp_markets(max_days=1)
            except Exception:
                return 0
        gate_mode, _n = self._gate()
        ev_keys = set()
        for b in list(self.bets.values()) + list(self.pending.values()):
            ev_keys.add((b["city"], b.get("date", ""), b["hl"]))
        new_mid, new_vol, cands = {}, {}, []
        today_iso = today()
        pending_tks = {o["ticker"] for o in self.pending.values()}
        for mk in mkts:
            tk = mk["ticker"]
            bid, ask = mk["yes_bid"], mk["yes_ask"]
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2.0
            prev = self.last_mid.get(tk)
            prev_vol = self.last_vol.get(tk)
            vol = float(mk.get("vol", 0) or 0)
            new_mid[tk] = mid
            new_vol[tk] = vol
            if tk in self.bets or tk in pending_tks:
                continue
            ekey = (mk["city"], mk.get("date", ""),
                    "lo" if mk["is_low"] else "hi")
            if ekey in ev_keys:
                continue
            if mid >= dp.DRIFT_MIN_C:
                side, entry, smid = "yes", bid, mid
                climb_c = (mid - prev) if prev is not None else None
            elif mid <= 100 - dp.DRIFT_MIN_C:
                side, entry, smid = "no", 100 - ask, 100 - mid
                climb_c = (prev - mid) if prev is not None else None
            else:
                continue
            climbing = climb_c is not None and climb_c >= dp.DRIFT_UP_C
            # NO nickel lane in live v1 (gap risk not yet priced with real $)
            if smid >= dp.NICKEL_MIN_C:
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
            if entry < 50 or entry > dp.DRIFT_MAX_ENTRY:
                continue
            cands.append((trig, score, mk, side, entry, smid, ekey))
        cands.sort(key=lambda c: ({"level": 0}.get(c[0], 1), -c[1]))
        placed = 0
        for trig, score, mk, side, entry, smid, ekey in cands:
            if ekey in ev_keys:
                continue
            tk = mk["ticker"]
            pside = smid / 100.0
            if gate_mode == "probe":
                size = max(1, PROBE_COST_CENTS // entry)
            else:
                b_odds = (100 - entry) / entry
                f_star = max(0.0, pside - (1 - pside) / b_odds) * 0.25
                bankroll = balance_c + self.open_cost_c()
                size = int(min(f_star, dp.PER_BET_CAP) * bankroll // entry)
                if size < 1:
                    continue
            while size > 1 and entry * size > self.max_bet_c:
                size -= 1
            if entry * size > self.max_bet_c:
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
                    oid = ((resp.get("order") or {}).get("order_id")
                           or resp.get("order_id") or oid)
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
                "ots": now()}
            ev_keys.add(ekey)
            self.placed += 1
            placed += 1
            self._log([now(), "REST", self.mode, mk["city"], mk["strike"],
                       ("lo" if mk["is_low"] else "hi"), side, round(pside, 3),
                       entry, size, "", "", oid])
            print(f"  {self.mode} DRIFT ORDER {tk}: {side.upper()} {size}x @ "
                  f"{entry}c maker ({trig}, p={pside:.2f})")
        self.last_mid = new_mid             # momentum memory = last scan only
        self.last_vol = new_vol
        # DRY mode: resting orders "fill" instantly at maker price (upper
        # bound, same optimistic assumption the paper book makes)
        if self.client is None:
            for oid, o in list(self.pending.items()):
                fee = fee_cents(o["entry"], o["count"], taker=False)
                self.fees_c += fee
                self.bets[o["ticker"]] = {**{k: o[k] for k in
                                             ("side", "entry", "count", "city",
                                              "strike", "kind", "cap", "hl",
                                              "pside", "date", "trig", "peak")},
                                          "fee": fee, "oid": oid,
                                          "ots": o["ots"], "era": ERA}
                del self.pending[oid]
        return placed

    def step(self):
        self._roll_day()
        self.check_orders()
        self.settle()
        self.stop_check()
        self.place()
        try:
            bal = self.balance_c()
        except Exception:
            bal = None
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
    print(f"[{now()}] drift executor started in {dl.mode} mode "
          f"(caps: ${dl.max_bet_c/100:.2f}/bet, ${dl.max_open_c/100:.2f} open, "
          f"${dl.max_day_loss_c/100:.2f} daily halt; rest<= {REST_MAX_H}h; "
          f"no nickel, no pyramid in live v1)")
    if "--once" in sys.argv:
        dl.step()
        return 0
    while True:
        try:
            dl.step()
        except KeyboardInterrupt:
            print("stopped.")
            return 0
        except Exception as e:
            print(f"[{now()}] cycle error: {e}")
        time.sleep(CYCLE_S)


if __name__ == "__main__":
    raise SystemExit(main())
