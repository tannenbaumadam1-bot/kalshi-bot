"""Weather LIVE executor v2: modes, caps, maker orders, fill lifecycle."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weather_live as wl
import weather_paper as wp


class FakeClient:
    def __init__(self, balance=10000):
        self.balance = balance
        self.orders = []          # bodies of created orders
        self.resting = {}         # oid -> True while resting
        self.fills = []
        self.canceled = []
        self._n = 0

    def get_balance_cents(self):
        return self.balance

    def create_order(self, ticker, action, side, count, price_cents=None, **kw):
        self._n += 1
        oid = f"o{self._n}"
        self.orders.append({"ticker": ticker, "action": action, "side": side,
                            "count": count, "price": price_cents, "oid": oid})
        self.resting[oid] = True
        return {"order": {"order_id": oid}}

    def get_resting_orders(self):
        return [{"order_id": oid} for oid, up in self.resting.items() if up]

    def get_fills(self, limit=100):
        return self.fills

    def cancel_order(self, oid):
        self.canceled.append(oid)
        self.resting.pop(oid, None)
        return {}


def _bot(client=None, mode="DRY", monkeypatch=None, tmp_path=None):
    if tmp_path is not None and monkeypatch is not None:
        monkeypatch.setattr(wl, "STATE", str(tmp_path / "state.json"))
        monkeypatch.setattr(wl, "BETS", str(tmp_path / "bets.csv"))
    b = wl.WeatherLive.__new__(wl.WeatherLive)
    b.max_bet_c, b.max_open_c = 200, 1500
    b.max_day_loss_c, b.reserve_c = 300, 200
    b.client, b.mode = client, mode
    b.bets, b.pending, b.cooldown, b.history = {}, {}, {}, []
    b.realized_c = b.fees_c = b.day_pnl_c = 0.0
    b.wins = b.losses = b.placed = b.canceled = 0
    b.day = wl.today()
    b.halted = False
    b.dry_balance_c = 10000
    return b


def _edge(ticker="KXHIGHNY-26JUL19-T86", price=40, fair=0.75, kind="ge"):
    mk = {"ticker": ticker, "city": "new york", "is_low": False, "strike": 87,
          "kind": kind, "cap": None, "yes_bid": price, "yes_ask": price + 4,
          "date": "2026-07-19", "hrs": 20.0, "title": "", "sub": "",
          "entry_price": price, "maker": True, "src": "forecast", "w": 0.35}
    return (5.0, "YES", mk, fair, 85.0)


def test_dry_mode_places_no_real_orders(monkeypatch, tmp_path):
    b = _bot(None, "DRY", monkeypatch, tmp_path)
    monkeypatch.setattr(wl.we, "scan", lambda **k: [_edge()])
    b.place()
    assert b.placed == 1
    assert len(b.bets) == 1               # dry fill promoted instantly
    assert not b.pending


def test_live_mode_rests_maker_order(monkeypatch, tmp_path):
    c = FakeClient()
    b = _bot(c, "LIVE", monkeypatch, tmp_path)
    monkeypatch.setattr(wl.we, "scan", lambda **k: [_edge(price=40)])
    b.place()
    assert len(c.orders) == 1
    o = c.orders[0]
    assert o["action"] == "buy" and o["price"] == 40   # maker price, not the ask
    assert len(b.pending) == 1 and not b.bets          # rests until filled


def test_per_bet_cap_respected(monkeypatch, tmp_path):
    c = FakeClient(balance=100000)
    b = _bot(c, "LIVE", monkeypatch, tmp_path)
    b.history = [{"outcome": 1, "pnl": 0.5, "pside": 0.5}] * 40   # gate passes
    monkeypatch.setattr(wl.we, "scan", lambda **k: [_edge(price=40, fair=0.9)])
    b.place()
    assert len(c.orders) == 1
    o = c.orders[0]
    assert o["price"] * o["count"] <= b.max_bet_c      # never > $2 cost basis


def test_daily_loss_halt_blocks_new_orders(monkeypatch, tmp_path):
    c = FakeClient()
    b = _bot(c, "LIVE", monkeypatch, tmp_path)
    b.day_pnl_c = -300
    monkeypatch.setattr(wl.we, "scan", lambda **k: [_edge()])
    b.place()
    assert not c.orders and b.halted


def test_open_exposure_cap(monkeypatch, tmp_path):
    c = FakeClient()
    b = _bot(c, "LIVE", monkeypatch, tmp_path)
    b.bets = {"X": {"entry": 50, "count": 29, "fee": 0, "city": "boston",
                    "date": "2026-07-19", "hl": "hi"}}   # $14.50 open
    monkeypatch.setattr(wl.we, "scan", lambda **k: [_edge(price=60)])
    b.place()
    assert not c.orders                                  # would exceed $15


def test_fill_promotion_and_stale_cancel(monkeypatch, tmp_path):
    c = FakeClient()
    b = _bot(c, "LIVE", monkeypatch, tmp_path)
    monkeypatch.setattr(wl.we, "scan", lambda **k: [_edge()])
    b.place()
    oid = next(iter(b.pending))
    # simulate a fill: order leaves the resting book, fill appears
    c.resting.pop(oid)
    c.fills = [{"order_id": oid, "count": 1}]
    b.check_orders()
    assert not b.pending and len(b.bets) == 1
    assert b.bets["KXHIGHNY-26JUL19-T86"]["count"] == 1
    assert b.bets["KXHIGHNY-26JUL19-T86"]["era"] == "live1"
    # stale path: new order older than REST_MAX_H gets canceled
    monkeypatch.setattr(wl.we, "scan", lambda **k: [_edge(ticker="KXHIGHLAX-26JUL19-T80")])
    b.place()
    oid2 = next(iter(b.pending))
    b.pending[oid2]["ots"] = "2026-01-01T00:00:00"
    b.check_orders()
    assert oid2 in c.canceled and not b.pending
    assert b.canceled >= 1


def test_event_cap_and_price_band(monkeypatch, tmp_path):
    c = FakeClient()
    b = _bot(c, "LIVE", monkeypatch, tmp_path)
    cheap = _edge(ticker="KXHIGHNY-26JUL19-T90", price=10)   # below MIN_PRICE 30
    monkeypatch.setattr(wl.we, "scan", lambda **k: [cheap])
    b.place()
    assert not c.orders
    # 3 pending in one event blocks the 4th
    b.pending = {f"o{i}": {"ticker": f"T{i}", "entry": 40, "count": 1,
                           "city": "new york", "date": "2026-07-19", "hl": "hi"}
                 for i in range(wp.EVENT_MAX_BETS)}
    monkeypatch.setattr(wl.we, "scan", lambda **k: [_edge(ticker="KXHIGHNY-26JUL19-T95")])
    b.place()
    assert len(c.orders) == 0


def test_mode_scoped_state(monkeypatch, tmp_path):
    sp = str(tmp_path / "state.json")
    monkeypatch.setattr(wl, "STATE", sp)
    monkeypatch.setattr(wl, "BETS", str(tmp_path / "bets.csv"))
    dry = _bot(None, "DRY", monkeypatch, tmp_path)
    dry.history = [{"outcome": 1, "pnl": 1.0, "pside": 0.5}] * 35
    dry.save(balance_c=10000)
    live = wl.WeatherLive.__new__(wl.WeatherLive)
    live.mode, live.history, live.bets = "LIVE", [], {}
    live.pending, live.cooldown = {}, {}
    live.realized_c = live.fees_c = live.day_pnl_c = 0.0
    live.wins = live.losses = live.placed = live.canceled = 0
    live.day, live.halted, live.dry_balance_c = wl.today(), False, 10000
    live.load()
    assert live.history == []       # DRY record never seeds the LIVE gate


def test_build_defaults_to_dry(monkeypatch):
    monkeypatch.delenv("KALSHI_WEATHER_LIVE", raising=False)
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    b = wl.build()
    assert b.mode == "DRY" and b.client is None
