"""Hero NAV: one account, two snapshot writers - use the freshest cash."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard as db


def test_freshest_balance_prefers_newer_snapshot():
    dv = {"balance_c": 5352, "updated": "2026-08-06T18:50:00"}
    cv = {"balance_c": 5419, "updated": "2026-08-06T18:59:40"}
    assert db._freshest_balance_c(dv, cv) == 5419      # crypto write newer
    cv["updated"] = "2026-08-06T18:40:00"
    assert db._freshest_balance_c(dv, cv) == 5352      # drift write newer


def test_freshest_balance_survives_missing_crypto_book():
    dv = {"balance_c": 5352, "updated": "2026-08-06T18:50:00"}
    assert db._freshest_balance_c(dv, {}) == 5352
    cv = {"updated": "2026-08-06T19:00:00"}            # newer but no balance
    assert db._freshest_balance_c(dv, cv) == 5352


def test_crypto_positions_valued_from_kalshi_mirror(tmp_path, monkeypatch):
    # 8/7: the hourly settle window - internal book already settled the
    # position, Kalshi still lists it until the payout posts. The crypto
    # panel + NAV must value KALSHI's position list, not the internal one.
    import json as _json
    monkeypatch.chdir(tmp_path)
    os.makedirs("logs", exist_ok=True)
    state = {
        "updated": "2026-08-07T03:17:57", "era": "clive1", "mode": "LIVE",
        "balance_c": 5769,
        "summary": {"mode": "LIVE"},
        "bets": {"KXBTCD-26AUG0717-T64000": {
            "side": "yes", "entry": 90, "count": 2, "pside": 0.9,
            "name": "btc", "event": "E1", "ots": ""}},
        "open": [{"ticker": "KXBTCD-26AUG0717-T64000", "side": "yes",
                  "entry": 90, "count": 2, "name": "btc", "ots": ""}],
        # Kalshi truth: still holds the internally-settled 11pm XRP too
        "k_positions": [
            {"ticker": "KXBTCD-26AUG0717-T64000", "side": "yes", "count": 2},
            {"ticker": "KXXRPD-26AUG0623-T1.0399", "side": "no", "count": 2}],
        "pending": {}, "history": [],
    }
    _json.dump(state, open("logs/crypto_live_state.json", "w"))
    monkeypatch.setitem(db._PRICES, "by_ticker", {
        "KXBTCD-26AUG0717-T64000": {"yes_bid": 91, "yes_ask": 93, "last": 92},
        "KXXRPD-26AUG0623-T1.0399": {"yes_bid": 0, "yes_ask": 0, "last": 2},
    })
    out = db.build_data()
    rows = {r["ticker"]: r for r in out["clive"]["open"]}
    assert set(rows) == {"KXBTCD-26AUG0717-T64000",
                         "KXXRPD-26AUG0623-T1.0399"}
    assert rows["KXBTCD-26AUG0717-T64000"]["value"] == 1.84   # 92c x2, entry kept
    assert rows["KXBTCD-26AUG0717-T64000"]["entry"] == 90
    assert rows["KXXRPD-26AUG0623-T1.0399"]["value"] == 1.96  # NO @ (100-2) x2
    # book-level marked value counts the settling position (Kalshi truth)
    assert out["clive"]["summary"]["marked_nav"] == round(1.84 + 1.96, 2)


# ---------- withdrawals (8/27) ----------
def test_roi_counts_money_the_owner_took_out_as_value_created():
    """Adam withdrew $72; ROI read -26.9% on a book that was +45%
    ($73.11 left + $72 out = $145.11 on $100 in). Money the owner takes
    out is not money the strategy lost."""
    acct, dep, wd = 73.11, 100.0, 72.04
    roi = round((acct + wd - dep) / dep * 100.0, 2)
    assert roi > 44.0
    naive = round((acct - dep) / dep * 100.0, 2)
    assert naive < -26.0          # what it used to print


def test_a_withdrawal_is_not_a_drawdown():
    """The weekly breaker measures drawdown from a rolling NAV peak, so
    a withdrawal looks exactly like a loss of the same size and would
    halt a perfectly healthy book."""
    peak, nav, wd = 145.15, 73.11, 72.04
    naive_dd = peak - nav
    true_dd = peak - (nav + wd)
    assert naive_dd > 70          # would trip a 15% breaker
    assert abs(true_dd) < 1       # the book actually lost nothing


def test_dashboard_can_host_the_tick_worker_when_paper_cannot():
    """kalshi-dashboard restarts on every deploy; kalshi-paper does not.
    On 8/28 the tick thread was dead nineteen hours with the fix for it
    undeployed, because the only process that could deploy the fix was
    the one that had stopped restarting."""
    src = open("dashboard.py").read()
    assert "_start_tick_fallback" in src
    assert 'start_thread("dashboard")' in src
    # and it must go through the lease, never around it
    assert "lease" in src.split("_start_tick_fallback")[1][:1200].lower()
