"""The engine: ties data, strategy, risk, and execution together."""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from .client import KalshiClient, KalshiError
from .config import Config
from .fees import fee_cents
from .journal import Journal
from .risk import RiskManager
from .strategies import OrderIntent, Position, MarketSnapshot, build_strategy
from .arb import find_arbs, plan_basket_buy, plan_basket_sell, reconcile_fills


def parse_orderbook(ob: Dict) -> Dict[str, int]:
    """Best bid/ask in cents. Supports new fixed-point and legacy formats."""
    ob = ob or {}
    if "orderbook_fp" in ob:
        ob = ob["orderbook_fp"] or {}

    if ob.get("yes_dollars") is not None or ob.get("no_dollars") is not None:
        yes = ob.get("yes_dollars") or []
        no = ob.get("no_dollars") or []
        to_price = lambda p: int(round(float(p) * 100))
        to_size = lambda s: int(float(s))
    else:
        yes = ob.get("yes") or []
        no = ob.get("no") or []
        to_price = lambda p: int(p)
        to_size = lambda s: int(s)

    def best(levels):
        if not levels:
            return 0, 0
        top = max(levels, key=lambda lv: float(lv[0]))
        return to_price(top[0]), to_size(top[1])

    yes_bid, yes_bid_size = best(yes)
    no_bid, no_bid_size = best(no)
    yes_ask = (100 - no_bid) if no_bid else 0
    no_ask = (100 - yes_bid) if yes_bid else 0
    return {
        "yes_bid": yes_bid, "yes_ask": yes_ask,
        "no_bid": no_bid, "no_ask": no_ask,
        "yes_bid_size": yes_bid_size, "yes_ask_size": no_bid_size,
    }


class Engine:
    def __init__(self, cfg: Config, client: KalshiClient, dry_run: bool = True):
        self.cfg = cfg
        self.client = client
        self.dry_run = dry_run
        self.risk = RiskManager(cfg.risk)
        self.strategy = build_strategy(cfg.strategy, cfg.strategy_params)
        self.journal = Journal(environment=cfg.environment)
        self._closed_markets = set()   # tickers that returned 'market closed'
        self._cycle_count = 0          # for periodic arb scanning
        self._arb_done = set()         # events already arb-traded this run
        self._resting_tickers = set()  # markets with a live order RIGHT NOW

    # ----- data helpers --------------------------------------------
    def _positions_by_ticker(self) -> Dict[str, Position]:
        out: Dict[str, Position] = {}
        for p in self.client.get_positions():
            raw = p.get("position", p.get("position_fp", 0))
            try:
                count = int(round(float(raw)))
            except (TypeError, ValueError):
                count = 0
            if count == 0:
                continue
            side = "yes" if count > 0 else "no"
            exp = p.get("market_exposure_dollars")
            if exp not in (None, ""):
                exposure = int(round(float(exp) * 100))
            else:
                exposure = abs(int(p.get("market_exposure", 0) or 0))
            avg = exposure // abs(count) if count else 0
            out[p["ticker"]] = Position(side=side, count=abs(count), avg_price_cents=avg)
        return out

    @staticmethod
    def _price_cents(mk: Dict, field_dollars: str, field_legacy: str) -> int:
        v = mk.get(field_dollars)
        if v not in (None, ""):
            try:
                return int(round(float(v) * 100))
            except (TypeError, ValueError):
                return 0
        return int(mk.get(field_legacy, 0) or 0)

    def _select_markets(self) -> List[Dict]:
        """Open markets worth looking at (quality filters)."""
        m = self.cfg.markets
        candidates: List[Dict] = []
        cursor = None
        for _ in range(m.scan_pages):
            data = self.client.get_markets(limit=200, status="open", cursor=cursor)
            for mk in data.get("markets", []) or []:
                yb = self._price_cents(mk, "yes_bid_dollars", "yes_bid")
                ya = self._price_cents(mk, "yes_ask_dollars", "yes_ask")
                if yb <= 0 or ya <= 0:
                    continue
                if not (m.min_price_cents <= yb <= m.max_price_cents):
                    continue
                if (ya - yb) < m.min_spread_cents:
                    continue
                if (ya - yb) > m.max_spread_cents:
                    continue   # huge spread = illiquid, orders won't fill
                try:
                    v = float(mk.get("volume_fp") or mk.get("volume") or 0)
                except (TypeError, ValueError):
                    v = 0.0
                if v < m.min_volume:
                    continue
                candidates.append(mk)
            cursor = data.get("cursor")
            if not cursor or len(candidates) >= m.scan_top_n * 3:
                break

        def vol(mk: Dict) -> float:
            try:
                return float(mk.get("volume_fp") or mk.get("volume") or 0)
            except (TypeError, ValueError):
                return 0.0

        candidates.sort(key=vol, reverse=True)
        return candidates[: m.scan_top_n]

    def _snapshot_for_ticker(self, ticker: str, positions: Dict[str, Position],
                             require_depth: bool = True) -> Optional[MarketSnapshot]:
        try:
            ob = self.client.get_orderbook(ticker, depth=5)
        except KalshiError:
            return None
        bb = parse_orderbook(ob)
        if require_depth:
            depth = self.cfg.markets.min_book_depth
            if bb["yes_bid_size"] < depth or bb["yes_ask_size"] < depth:
                return None
        return MarketSnapshot(
            ticker=ticker,
            yes_bid=bb["yes_bid"], yes_ask=bb["yes_ask"],
            no_bid=bb["no_bid"], no_ask=bb["no_ask"],
            yes_bid_size=bb["yes_bid_size"], yes_ask_size=bb["yes_ask_size"],
            position=positions.get(ticker, Position()),
        )

    def _snapshot(self, mk: Dict, positions: Dict[str, Position]) -> Optional[MarketSnapshot]:
        return self._snapshot_for_ticker(mk["ticker"], positions, require_depth=True)

    # ----- execution -----------------------------------------------
    def _exposure_cents(self, positions: Dict[str, Position]) -> int:
        return sum(p.count * p.avg_price_cents for p in positions.values())

    def _refresh_resting(self) -> None:
        """Snapshot which markets already have a live (resting) order, so we
        never stack a second order on the same market while the first sits
        unfilled."""
        self._resting_tickers = set()
        try:
            for o in self.client.get_resting_orders():
                tk = o.get("ticker")
                if tk:
                    self._resting_tickers.add(tk)
        except KalshiError:
            pass

    def _dedupe_resting(self) -> None:
        """Cancel piled-up duplicate orders: if a market has more than one
        live order on the same side, keep one and cancel the rest. Cleans up
        any stacking that happened before the duplicate-guard existed."""
        if self.dry_run:
            return
        try:
            resting = self.client.get_resting_orders()
        except KalshiError:
            return
        seen = set()
        for o in resting:
            key = (o.get("ticker"), o.get("action"), o.get("side"))
            if key[0] is None:
                continue
            if key in seen:
                try:
                    self.client.cancel_order(o["order_id"])
                    print(f"  cleaned up duplicate order on {key[0]}")
                except KalshiError:
                    pass
            else:
                seen.add(key)

    def _try_order(self, intent: OrderIntent, positions: Dict[str, Position],
                   balance_cents: int) -> None:
        # Don't stack duplicates: if a live order already exists on this
        # market, leave it to work (or get cancelled when stale) instead of
        # piling on another. Arb legs are IOC and never rest, so unaffected.
        if (not self.dry_run and not intent.arb
                and intent.ticker in self._resting_tickers):
            print(f"  skip {intent.ticker} {intent.action} {intent.side}"
                  f" - already have a working order there")
            return

        is_buy = intent.action == "buy"
        # dollar-based sizing: pick the contract count that gets closest to
        # target_position_dollars, capped by the per-market dollar limit.
        tgt = getattr(self.cfg.risk, "target_position_dollars", 0) or 0
        if is_buy and not intent.arb and tgt > 0 and intent.price_cents > 0:
            want = max(1, int(tgt * 100) // intent.price_cents)
            cap = max(1, int(self.cfg.risk.max_position_dollars * 100) // intent.price_cents)
            intent.count = min(want, cap)
        order_cost = intent.price_cents * intent.count if is_buy else 0
        pos = positions.get(intent.ticker, Position())
        pos_cost = pos.count * pos.avg_price_cents
        taker = intent.order_type == "market"
        est_fee = fee_cents(intent.price_cents, intent.count, taker=taker)

        # BUYS (new exposure) must pass the risk gate. SELLS (closing a
        # position) are always allowed - you should always be able to exit.
        if is_buy:
            ok, reason = self.risk.approve_order(
                order_cost_cents=order_cost,
                current_position_cents=pos_cost,
                open_exposure_cents=self._exposure_cents(positions),
                current_balance_cents=balance_cents,
            )
            if not ok:
                self.journal.log("blocked", ticker=intent.ticker, action=intent.action,
                                 side=intent.side, count=intent.count,
                                 price_cents=intent.price_cents, est_fee_cents=est_fee,
                                 reason=reason, detail=intent.reason)
                print(f"  blocked {intent.ticker} buy {intent.side}"
                      f" x{intent.count} @ {intent.price_cents}c -> {reason}")
                return

        if self.dry_run:
            self.journal.log("dry_run_order", ticker=intent.ticker, action=intent.action,
                             side=intent.side, count=intent.count,
                             price_cents=intent.price_cents, est_fee_cents=est_fee,
                             reason="dry run (not sent)", detail=intent.reason)
            print(f"  [DRY] would {intent.action} {intent.side} "
                  f"x{intent.count} {intent.ticker} @ {intent.price_cents}c"
                  f"  ({intent.reason})")
            self.risk.record_trade()
            return

        try:
            resp = self.client.create_order(
                ticker=intent.ticker, action=intent.action, side=intent.side,
                count=intent.count, order_type=intent.order_type,
                price_cents=intent.price_cents,
            )
            self.risk.record_trade()
            self._resting_tickers.add(intent.ticker)
            self.journal.log("order_sent", ticker=intent.ticker, action=intent.action,
                             side=intent.side, count=intent.count,
                             price_cents=intent.price_cents, est_fee_cents=est_fee,
                             reason="sent",
                             detail=str(resp.get("order_id")
                                        or resp.get("order", {}).get("order_id", "")))
            print(f"  SENT {intent.action} {intent.side} x{intent.count} "
                  f"{intent.ticker} @ {intent.price_cents}c")
        except KalshiError as e:
            msg = str(e)
            if "market_closed" in msg or "market closed" in msg:
                self._closed_markets.add(intent.ticker)
                print(f"  {intent.ticker}: market closed - leaving it to settle")
            else:
                self.journal.log("error", ticker=intent.ticker, action=intent.action,
                                 side=intent.side, count=intent.count,
                                 price_cents=intent.price_cents, est_fee_cents=est_fee,
                                 reason="api_error", detail=str(e))
                print(f"  ERROR placing {intent.ticker}: {e}")

    def _cancel_stale(self) -> None:
        try:
            resting = self.client.get_resting_orders()
        except KalshiError:
            return
        now = time.time()
        for o in resting:
            created = o.get("created_time_ts") or 0
            age = now - created if created else 0
            if age and age > self.cfg.engine.cancel_stale_after_s:
                if not self.dry_run:
                    try:
                        self.client.cancel_order(o["order_id"])
                    except KalshiError:
                        pass
                self.journal.log("cancel_stale", ticker=o.get("ticker", ""),
                                 detail=f"age {int(age)}s")

    def _report_arbs(self, balance_cents: int) -> None:
        """Scan all markets for logical-arb candidates and print them; if
        arb.trade is on (and not a dry run) try to execute the safe ones."""
        markets = []
        cursor = None
        for _ in range(self.cfg.markets.scan_pages):
            try:
                d = self.client.get_markets(limit=200, status="open", cursor=cursor)
            except KalshiError:
                return
            markets += d.get("markets", []) or []
            cursor = d.get("cursor")
            if not cursor:
                break
        res = find_arbs(markets)
        u, o = res["under"], res["over"]
        if u or o:
            print(f"  ARB SCAN: {len(u)} underround, {len(o)} overround candidate(s):")
            for net, ev, n, val, fees in u[:3]:
                print(f"    BUY-all-YES  {ev} ({n} legs) -> +{net}c net")
            for net, ev, n, val, fees in o[:3]:
                print(f"    SELL-all-YES {ev} ({n} legs) -> +{net}c net")
        else:
            print("  ARB SCAN: no logical-arb candidates this pass")

        if self.cfg.arb.trade and not self.dry_run and u:
            for cand in u:
                self._maybe_trade_arb(cand, balance_cents)

    def _maybe_trade_arb(self, candidate, balance_cents: int) -> None:
        """Auto-execute an arb (buy-all-YES underround OR sell-all-YES
        overround), ONLY on a Kalshi-confirmed mutually-exclusive event."""
        _net0, ev, n, _c, _f = candidate
        a = self.cfg.arb
        if ev in self._arb_done or n > a.max_legs:
            return
        try:
            evd = self.client.get_event(ev)
            evd = evd.get("event", evd)
            if not evd.get("mutually_exclusive"):
                return   # not a true one-winner set -> never trade it
            mkts = self.client.get_event_markets(ev)
        except KalshiError:
            return

        buy_legs, sell_legs = [], []
        for mk in mkts:
            if mk.get("status") not in ("active", "open", None):
                return   # a leg has closed/settled -> set incomplete, abort
            ask = self._price_cents(mk, "yes_ask_dollars", "yes_ask")
            bid = self._price_cents(mk, "yes_bid_dollars", "yes_bid")
            try:
                ask_sz = float(mk.get("yes_ask_size_fp") or 0)
            except (TypeError, ValueError):
                ask_sz = 0
            try:
                bid_sz = float(mk.get("yes_bid_size_fp") or 0)
            except (TypeError, ValueError):
                bid_sz = 0
            buy_legs.append((mk["ticker"], ask, ask_sz))
            sell_legs.append((mk["ticker"], bid, bid_sz))

        qty = a.qty_per_leg
        reserve = int(self.cfg.risk.min_cash_reserve_dollars * 100)

        net_u = plan_basket_buy(buy_legs, qty=qty, min_net_cents=a.min_net_cents)
        if net_u is not None:
            cost = sum(ask for _, ask, _ in buy_legs) * qty
            if balance_cents - cost >= reserve:
                print(f"  ARB TRADE (underround): {ev} | {len(buy_legs)} legs | net +{net_u}c")
                self._arb_done.add(ev)
                self._fire_arb_basket([(t, p) for t, p, _ in buy_legs], "buy", qty)
                return

        net_o = plan_basket_sell(sell_legs, qty=qty, min_net_cents=a.min_net_cents)
        if net_o is not None:
            collat = sum(100 - bid for _, bid, _ in sell_legs) * qty
            if balance_cents - collat >= reserve:
                print(f"  ARB TRADE (overround): {ev} | {len(sell_legs)} legs | net +{net_o}c")
                self._arb_done.add(ev)
                self._fire_arb_basket([(t, p) for t, p, _ in sell_legs], "sell", qty)
                return

    @staticmethod
    def _fill_count(resp) -> int:
        v = resp.get("fill_count")
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return 0

    def _fire_arb_basket(self, legs, action: str, qty: int) -> None:
        """Place every leg immediate-or-cancel, then flatten any leg that
        over-filled relative to the worst-filling leg, so we NEVER hold a
        lopsided (naked) basket. action 'buy'=buy YES at ask, 'sell'=sell YES
        at bid. If a leg fills 0, the whole thing unwinds back to flat."""
        fills = {}
        for tk, price in legs:
            try:
                resp = self.client.create_order(tk, action, "yes", qty,
                                                 order_type="market", price_cents=price)
                fills[tk] = self._fill_count(resp)
            except KalshiError as e:
                fills[tk] = 0
                print(f"    leg {tk}: order error ({e})")
            self.journal.log("arb_leg", ticker=tk, action=action, side="yes",
                             count=fills.get(tk, 0), price_cents=price,
                             reason="arb leg")

        m, excess = reconcile_fills(fills)
        opp = "sell" if action == "buy" else "buy"
        flat_price = 1 if opp == "sell" else 99   # marketable IOC to flatten now
        for tk, qx in excess.items():
            try:
                self.client.create_order(tk, opp, "yes", qx, order_type="market",
                                         price_cents=flat_price)
                print(f"    flattened {qx} excess on {tk}")
            except KalshiError:
                print(f"    WARNING: could not flatten {qx} on {tk} - residual position!")

        if m > 0:
            print(f"    arb LOCKED: {m} complete basket(s) held.")
        else:
            print("    arb did NOT complete - flattened back to flat (no naked bet).")

    # ----- public ---------------------------------------------------
    def run_cycle(self) -> None:
        balance = self.client.get_balance_cents()
        self.risk.start_day(balance)
        positions = self._positions_by_ticker()
        self._dedupe_resting()    # clean up any piled-up duplicate orders
        self._refresh_resting()   # know what orders are already working

        can, reason = self.risk.can_trade_today(balance)
        status = "OK" if can else ("entries HALTED: " + reason)
        print(f"Balance ${balance/100:.2f} | trades today "
              f"{self.risk.state.trades_today}/{self.cfg.risk.max_trades_per_day}"
              f" | positions {len(positions)} | {status}")

        # 1) ALWAYS manage exits for every held position (closing is allowed
        #    even when entries are halted, so positions never get stranded).
        for ticker in list(positions.keys()):
            if ticker in self._closed_markets:
                continue   # closed market - it will settle on its own
            snap = self._snapshot_for_ticker(ticker, positions, require_depth=False)
            if snap is None:
                continue
            for intent in self.strategy.decide(snap):
                if intent.action == "sell":
                    self._try_order(intent, positions, balance)

        # 2) ENTRIES only if the risk gate allows new exposure.
        if can:
            markets = self._select_markets()
            print(f"Scanning {len(markets)} liquid markets for entries...")
            held = set(positions.keys())
            for mk in markets:
                if not self.risk.can_trade_today(balance)[0]:
                    break
                if mk["ticker"] in held:
                    continue
                snap = self._snapshot(mk, positions)
                if snap is None:
                    continue
                for intent in self.strategy.decide(snap):
                    if intent.action == "buy":
                        self._try_order(intent, positions, balance)

        self._cycle_count += 1
        if self._cycle_count % 5 == 1:   # scan on cycle 1, 6, 11, ...
            self._report_arbs(balance)

        self._cancel_stale()

    def run_forever(self) -> None:
        print(f"Starting loop: strategy='{self.cfg.strategy}', "
              f"env='{self.cfg.environment}', "
              f"{'DRY RUN' if self.dry_run else 'LIVE EXECUTION'}")
        try:
            while True:
                try:
                    self.run_cycle()
                except Exception as e:
                    # never let one bad cycle kill an overnight run
                    print(f"cycle error (continuing): {e}")
                time.sleep(self.cfg.engine.cycle_seconds)
        except KeyboardInterrupt:
            print("\nStopped by user. Bye.")
