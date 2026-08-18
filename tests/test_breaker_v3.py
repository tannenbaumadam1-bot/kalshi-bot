"""8/17 build: breaker v3 (marked NAV), CUT downside exit, metric-slate cap.

The 8/17 morning is the fixture: five low-temp NO positions collapsed
85c->5c (-$23 of marks) while the breaker read loss -$4.30, because it
measured cash + ENTRY COST - a number that cannot see mark damage.
"""
import os
import sys
import time
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drift_live as dl

TODAY = _dt.date.today().isoformat()


def _mk(tk="KXLOWTHOU-26AUG17-B73.5", bid=82, ask=85, city="houston",
        is_low=True, strike=73, kind="ge", cap=None, date=None, vol=100.0,
        hrs=10.0):
    return {"ticker": tk, "city": city, "is_low": is_low, "strike": strike,
            "kind": kind, "cap": cap, "yes_bid": bid, "yes_ask": ask,
            "date": date or TODAY, "hrs": hrs, "title": "", "sub": "",
            "bid_size": 50.0, "ask_size": 50.0, "vol": vol}


def _bot(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(dl, "BETS", str(tmp_path / "b.csv"))
    # 8/18: legacy tests run WITHOUT the Adam override lane; tests that
    # exercise BUCKET_ALLOW set it explicitly
    monkeypatch.setattr(dl, "BUCKET_ALLOW", set())
    return dl.DriftLive(None, mode="DRY")


def _no_pos(tk, entry=88, count=5, city="houston", hl="lo", date=None):
    return {"side": "no", "entry": entry, "count": count, "city": city,
            "strike": 73, "kind": "ge", "cap": None, "hl": hl,
            "pside": 0.9, "date": date or TODAY, "trig": "level",
            "peak": 90.0, "fee": 10, "oid": "x", "ots": dl.now(),
            "era": dl.ERA}


# ---------------- breaker v3: marked NAV ----------------

def test_marked_nav_sees_the_damage_cost_basis_cannot(tmp_path, monkeypatch):
    """The 8/17 scenario in miniature: marks collapse, cost basis frozen."""
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    b.dry_balance_c = 5000
    # market now prices our NO side at ~2c (yes_ask 98 -> no bid 2)
    b._mark_nav([_mk("T1", bid=95, ask=98)])
    assert b.last_mnav_c > 0
    # marked nav = cash 5000 + 2*5 = ~5010c, NOT 5000 + 88*5 = 5440c
    assert b.last_mnav_c <= 5100, b.last_mnav_c
    assert b.mnav_ts > 0


def test_breaker_uses_marked_nav_when_fresh(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    b.dry_balance_c = 5000
    b.last_nav_c = 5440.0                      # cost basis: blind
    b.nav_days = {TODAY: 13000.0}              # a real recorded peak
    b._mark_nav([_mk("T1", bid=95, ask=98)])   # marks: ~5010
    tripped = b._week_loss_exceeded()
    assert b.week_basis == "marked"
    # drawdown vs 13000 peak on ~5010 marked nav is way past 15%
    assert tripped


def test_breaker_falls_back_to_cost_when_marks_stale(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.last_nav_c = 10000.0
    b.last_mnav_c = 5000.0
    b.mnav_ts = time.time() - dl.MNAV_FRESH_S - 60   # stale
    b.nav_days = {TODAY: 10000.0}
    b._week_loss_exceeded()
    assert b.week_basis == "cost"


def test_mark_pass_refuses_thin_coverage(tmp_path, monkeypatch):
    """A scan that prices too little of the book must not publish."""
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    b.bets["T2"] = _no_pos("T2", entry=85, count=5, city="denver")
    b.dry_balance_c = 5000
    b._mark_nav([])                    # no quotes at all
    assert b.last_mnav_c == 0.0        # nothing published


def test_marked_nav_feeds_nav_days_peak(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    b.dry_balance_c = 5000
    b.nav_days = {}
    b._mark_nav([_mk("T1", bid=95, ask=98)])
    assert TODAY in b.nav_days and b.nav_days[TODAY] == b.last_mnav_c


def test_dust_marks_at_entry_not_quotes(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=2, count=10)   # dust adopt
    b.dry_balance_c = 5000
    b._mark_nav([_mk("T1", bid=1, ask=99)])
    # dust contributes cost (20c), not a mark; nav = 5000 + 20
    assert b.last_mnav_c == 5020.0


# ---------------- CUT: band-broken downside exit ----------------

def test_cut_sells_a_broken_band_after_confirmation(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    # NO-side mid = 100 - (30+34)/2 = 68 > 50: healthy, no cut
    assert b.cut_check([_mk("T1", bid=30, ask=34)]) == 0
    # band breaks: yes mid 62 -> our smid 38 <= 50; cycle 1 = confirm only
    assert b.cut_check([_mk("T1", bid=60, ask=64)]) == 0
    assert b.bets["T1"]["cut_n"] == 1
    # cycle 2 confirms -> sell into no-bid (100-64=36)
    assert b.cut_check([_mk("T1", bid=60, ask=64)]) == 1
    assert "T1" not in b.bets
    row = b.history[-1]
    assert row.get("cut") and row["exit_px"] == 36
    assert b.autopsy[-1]["kind"] == "CUT"
    assert b.exec_stats.get("cuts") == 1
    assert b.turns["kinds"]["cut"] == 1


def test_cut_confirmation_resets_on_recovery(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    b.cut_check([_mk("T1", bid=60, ask=64)])      # broken, cut_n = 1
    assert b.bets["T1"]["cut_n"] == 1
    b.cut_check([_mk("T1", bid=30, ask=34)])      # recovered
    assert b.bets["T1"]["cut_n"] == 0
    b.cut_check([_mk("T1", bid=60, ask=64)])      # must re-confirm from 0
    assert "T1" in b.bets


def test_cut_spares_cheap_entries_and_dust(tmp_path, monkeypatch):
    """Lottery tickets (adopts at 1-3c) are never cut - pure fee burn."""
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=2, count=10)
    for _ in range(dl.CUT_CONFIRM + 1):
        assert b.cut_check([_mk("T1", bid=97, ask=99)]) == 0
    assert "T1" in b.bets


def test_cut_defers_to_flatten_near_close(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    mk = _mk("T1", bid=60, ask=64, hrs=0.5)       # inside FLATTEN_H
    for _ in range(dl.CUT_CONFIRM + 1):
        assert b.cut_check([mk]) == 0
    assert "T1" in b.bets


def test_cut_realizes_the_loss_in_the_day_ledger(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    day0 = b.day_pnl_c
    for _ in range(dl.CUT_CONFIRM):
        b.cut_check([_mk("T1", bid=60, ask=64)])
    # sold 5 @ 36 vs entry 88: net well negative, booked realized
    assert b.day_pnl_c < day0


# ---------------- metric-slate cap ----------------

def test_conc_cost_reports_metric_axis(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["L1"] = _no_pos("L1", entry=80, count=5, city="houston", hl="lo")
    b.bets["L2"] = _no_pos("L2", entry=80, count=5, city="denver", hl="lo")
    b.bets["H1"] = _no_pos("H1", entry=80, count=5, city="miami", hl="hi")
    c, d, m = b._conc_cost_c("houston", TODAY, hl="lo")
    assert c == 400            # one houston position
    assert d == 1200           # all three settle today
    assert m == 800            # only the two LOWS count on the lo axis


def test_metric_slate_cap_blocks_the_correlated_axis(tmp_path, monkeypatch):
    """8/17 fixture: lows already at the metric cap -> a new LOW is
    refused (mslate) while a HIGH on the same slate still places."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "METRIC_SLATE_PCT", 0.10)
    b.last_nav_c = 10000.0     # metric room = 1000c
    b.bets["L1"] = _no_pos("L1", entry=88, count=5, city="houston", hl="lo")
    b.bets["L2"] = _no_pos("L2", entry=88, count=5, city="denver", hl="lo")
    # lows cost 880c of a 1000c metric budget -> 5 more @82 must refuse
    lo_mk = _mk("KXLOWTSEA-X", bid=82, ask=85, city="seattle", is_low=True)
    hi_mk = _mk("KXHIGHMIA-X", bid=82, ask=85, city="miami", is_low=False)
    n = b.place(mkts=[lo_mk, hi_mk])
    tks = set(b.bets) | {o["ticker"] for o in b.pending.values()}
    assert "KXLOWTSEA-X" not in tks
    assert "KXHIGHMIA-X" in tks
    assert b.exec_stats.get("mslate_capped", 0) >= 1


def test_metric_cap_counts_exchange_view_via_ticker(tmp_path, monkeypatch):
    """k_positions rows carry no hl - the ticker must encode it."""
    b = _bot(tmp_path, monkeypatch)
    b.k_positions = [{"ticker": "KXLOWTDEN-X", "entry": 88, "count": 5,
                      "city": "denver", "date": TODAY}]
    b.settled_tks = []
    _, _, m = b._conc_cost_c("houston", TODAY, hl="lo")
    assert m == 440
    _, _, m_hi = b._conc_cost_c("houston", TODAY, hl="hi")
    assert m_hi == 0


# ---------------- rung reweight ----------------

def test_97_rung_is_gone_from_the_base_ladder():
    assert dl.SELL_MIN_C >= 98
    assert all(lo != 97 or h_min < 2.0
               for h_min, lo, hi in dl.DECAY_LADDER)


# ---------------- v3 migration: purge cost-basis peaks ----------------

def test_migration_clears_legacy_nav_days_and_halt_latch(tmp_path,
                                                         monkeypatch):
    """The 13:28 false halt: honest marks measured against a phantom
    cost-basis 'peak'. Loading a pre-v3 state must clear both."""
    b = _bot(tmp_path, monkeypatch)
    b.nav_days = {TODAY: 14451.0}      # the phantom peak
    b.week_halted = True
    b.save()
    import json
    d = json.load(open(dl.STATE))
    assert d.get("nav_v3") is True     # new saves are tagged
    del d["nav_v3"]                    # simulate a pre-v3 state file
    json.dump(d, open(dl.STATE, "w"))
    b2 = dl.DriftLive(None, mode="DRY")
    assert b2.nav_days == {}
    assert b2.week_halted is False


def test_v3_state_survives_reload_untouched(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.nav_days = {TODAY: 12000.0}      # a MARKED peak (post-v3)
    b.save()
    b2 = dl.DriftLive(None, mode="DRY")
    assert b2.nav_days == {TODAY: 12000.0}   # marked peaks survive
    # (week_halted itself is never persisted - the gate re-measures on
    # the live ledger every cycle, so a real halt re-fires on its own)


def test_refresh_caps_no_longer_writes_cost_basis_peaks(tmp_path,
                                                        monkeypatch):
    """Re-contamination guard: only _mark_nav may feed nav_days."""
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    b.nav_days = {}
    b._refresh_caps(5000)
    assert b.nav_days == {}


# ---------------- 8/17 pm velocity build ----------------

def test_ewin_buckets():
    f = dl.DriftLive._ewin
    assert f(2.0) == "0-4" and f(5.5) == "4-8"
    assert f(10.0) == "8-16" and f(20.0) == "16+"
    assert f(None) == "na" and f("x") == "na"


def test_turn_add_accumulates_entry_window(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b._turn_add(50.0, "lift", hold_h=2.0, ehrs=3.0)
    b._turn_add(30.0, "lift", hold_h=4.0, ehrs=3.5)
    b._turn_add(-20.0, "settle", hold_h=9.0, ehrs=12.0)
    ew = b.turns["ewin"]
    assert ew["0-4"] == {"n": 2, "net_c": 80.0, "hold_h": 6.0}
    assert ew["8-16"]["n"] == 1
    st = b._turn_stats()["ewin"]
    assert st["0-4"]["net"] == 0.8
    assert st["0-4"]["per_ch"] == round(0.8 / 6.0, 4)


def test_entry_carries_ehrs_to_the_bet(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.place(mkts=[_mk("KXHIGHMIA-X", bid=85, ask=88, is_low=False,
                      city="miami", hrs=6.5)])
    bet = b.bets.get("KXHIGHMIA-X")
    assert bet is not None and bet.get("ehrs") == 6.5


def test_proven_bucket_outranks_unproven_on_a_tight_budget(tmp_path,
                                                           monkeypatch):
    """Order IS allocation: with room for ONE bet, the proven-bucket
    candidate must win the budget regardless of scan order."""
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "METRIC_SLATE_PCT", 1.0)
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 1.0)
    monkeypatch.setattr(dl, "CITY_CAP_PCT", 1.0)
    # prove level:85-89 (>= KELLY_PROVEN_N wins on the trigger+band)
    b.history = [{"tk": f"P{i}", "ots": f"o{i}", "trig": "level",
                  "entry": 87, "pnl": 0.3} for i in range(30)]
    b.last_nav_c = 10000.0
    b.dry_balance_c = 10000
    # room for exactly one 5-lot at ~88: cap the open budget tight
    b.max_open_c = 5 * 88 + 40
    b.max_bet_c = 800
    monkeypatch.setattr(dl, "DYN_CAPS", False)   # keep the tight caps
    # unproven 82c candidate scans FIRST, proven-band 87c second
    n = b.place(mkts=[
        _mk("KXHIGHDEN-U", bid=80, ask=82, is_low=False, city="denver",
            strike=1),
        _mk("KXHIGHMIA-P", bid=85, ask=88, is_low=False, city="miami",
            strike=2)])
    tks = set(b.bets) | {o["ticker"] for o in b.pending.values()}
    assert "KXHIGHMIA-P" in tks          # the proven band got the budget
    assert "KXHIGHDEN-U" not in tks


def test_util_publishes_unquoted_and_yield(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)      # not quoted
    b.bets["T2"] = _no_pos("T2", entry=85, count=5, city="denver")
    b.offers["T2"] = {"legs": [{"oid": "o", "px": 98, "count": 5}],
                      "count": 5, "rungs": [98], "ots": ""}
    b.bets["D"] = _no_pos("D", entry=2, count=10)        # dust: excluded
    b._turn_add(100.0, "lift", hold_h=1.0, ehrs=2.0)     # $1 today
    u = b._util_stats()
    assert u["unquoted"] == 4.40                         # T1 only
    assert u["yield_day"] is not None and u["yield_day"] > 0


def test_dip_starvation_is_counted(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.last_nav_c = 10000.0
    b.max_open_c = 100            # full book: no room for any dip
    monkeypatch.setattr(dl, "DYN_CAPS", False)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)   # fills the book
    # unheld favorite, bid 88 -> dip bid rests at 86... except no room
    b.quote_dips([_mk("T9", bid=88, ask=90, city="boston")], 10000, {})
    assert b.exec_stats.get("dip_capped", 0) >= 1


# ---------------- 8/18: CUT blind-spot fix ----------------

def test_cut_reaches_positions_missing_from_the_scan(tmp_path, monkeypatch):
    """8/18 live finding: DEN/SEA (yesterday's slate) sat at 3-7c all
    day with zero cuts - the scan only returns today-forward markets.
    cut_check must fetch quotes for held tickers the scan missed."""
    b = _bot(tmp_path, monkeypatch)
    b.bets["OLD1"] = _no_pos("OLD1", entry=88, count=5)
    monkeypatch.setattr(dl.dp.DriftPaper, "_quotes",
                        lambda self, tks: {"OLD1": (93, 95)})
    # no-side mid = 100-94 = 6 <= 50; bid = 100-95 = 5
    assert b.cut_check([]) == 0            # confirm cycle 1
    assert b.bets["OLD1"]["cut_n"] == 1
    assert b.cut_check([]) == 1            # fires from fallback quotes
    assert "OLD1" not in b.bets
    assert b.history[-1]["exit_px"] == 5
    assert b.exec_stats.get("cuts", 0) == 1


def test_cut_skips_closed_markets_with_no_bid(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["OLD2"] = _no_pos("OLD2", entry=88, count=5)
    monkeypatch.setattr(dl.dp.DriftPaper, "_quotes",
                        lambda self, tks: {})   # market closed: no book
    for _ in range(dl.CUT_CONFIRM + 1):
        assert b.cut_check([]) == 0
    assert "OLD2" in b.bets                # settlement decides, as before


def test_new_mechanism_counters_publish_zero(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    for k in ("cuts", "mslate_capped", "dip_capped"):
        assert b.exec_stats.get(k) == 0, k


# ---------------- 8/18: day anchor on marked NAV ----------------

def test_day_anchor_prefers_fresh_marked_nav(tmp_path, monkeypatch):
    """The phantom $141.37: cost-basis anchor counted busted positions
    at full entry cost. Marked NAV is the anchor now."""
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)   # marked ~0 elsewhere
    b.last_mnav_c = 12800.0
    b.mnav_ts = time.time()
    assert b._day_anchor_c(10000) == 12800            # marked, not cost


def test_day_anchor_falls_back_to_cost_when_marks_old(tmp_path,
                                                      monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.bets["T1"] = _no_pos("T1", entry=88, count=5)
    b.last_mnav_c = 12800.0
    b.mnav_ts = time.time() - 7300                    # > 2h stale
    assert b._day_anchor_c(10000) == 10000 + 440      # cost fail-safe


def test_nav0_v3_migration_discards_the_phantom_anchor(tmp_path,
                                                       monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.day_nav0_c = 14137                              # the phantom
    b.save()
    import json
    d = json.load(open(dl.STATE))
    assert d.get("nav0_v3") is True
    del d["nav0_v3"]                                  # pre-fix state file
    json.dump(d, open(dl.STATE, "w"))
    b2 = dl.DriftLive(None, mode="DRY")
    assert b2.day_nav0_c is None                      # re-anchors next cycle


# ---------------- 8/18: day halt v2 + caps-before-gates ----------------

def test_caps_refresh_before_the_halt_gate(tmp_path, monkeypatch):
    """The $2/$60/$12 deadlock: a tripped gate judged against boot
    defaults returned before _refresh_caps could ever fix them."""
    b = _bot(tmp_path, monkeypatch)
    b.day_pnl_c = -1361                       # the 8/18 morning
    b.max_day_loss_c = 1200                   # stale boot default
    b.last_mnav_c = 0.0                       # no marks: realized path
    b.place(mkts=[])                          # halts on realized...
    # ...but caps refreshed FIRST off the live balance regardless
    assert b.max_day_loss_c > 1200
    assert b.max_bet_pv_c > 0


def test_day_halt_measures_marked_day_not_realized(tmp_path, monkeypatch):
    """8/18: -13.61 REALIZED (old busts settling) while the marked book
    was +7 on the day. The day gate must read the marked day."""
    b = _bot(tmp_path, monkeypatch)
    b.day_pnl_c = -1361                       # yesterday's damage, realized today
    b.day_nav0_c = 12158
    b.last_mnav_c = 12889.0                   # day is UP $7.31 marked
    b.mnav_ts = time.time()
    b.halted = True                           # stale latch from the deadlock
    b.place(mkts=[])
    assert b.halted is False                  # cleared: the day is green
    assert b.day_halt_basis == "marked"


def test_day_halt_still_fires_on_a_real_marked_drop(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.day_nav0_c = 12000
    b.last_mnav_c = 9500.0                    # -25% marked day
    b.mnav_ts = time.time()
    b.place(mkts=[])
    assert b.halted is True
    assert b.day_halt_basis == "marked"


def test_day_halt_realized_failsafe_when_marks_dark(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    b.last_mnav_c = 0.0                       # marks never published
    b.day_pnl_c = -9999
    b.place(mkts=[])
    assert b.halted is True
    assert b.day_halt_basis == "realized"


# ---------------- 8/18: Adam override - unblock 85-89 at 8% ----------------

def test_allow_list_exempts_the_lane_from_the_gate(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "BUCKET_ALLOW", {"level:85-89"})
    b.bucket_blocked_cum = {"level:85-89": {"n": 46, "net": -1.72},
                            "level:80-84": {"n": 8, "net": -9.27}}
    bstats = {"level:85-89": {"n": 47, "net": -6.08, "blocked": True}}
    assert b._bucket_blocked(bstats, "level", 87) is False
    assert "level:85-89" not in b.bucket_blocked_cum   # sticky purged
    # non-allowed lanes stay gated
    assert b._bucket_blocked({}, "level", 82) is True


def test_allow_list_grants_full_earned_status(tmp_path, monkeypatch):
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "BUCKET_ALLOW", {"level:85-89"})
    # proven despite a red recent window: Adam's explicit 8/18 order
    assert b._bucket_is_proven("level", 87, {}) is True
    assert b._kelly_frac({}, "level", 87) == dl.KELLY_PROVEN_MULT
    # a different band is untouched
    assert b._kelly_frac({}, "level", 91) == dl.KELLY_BASE


def test_allowed_lane_sizes_to_the_earned_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "CITY_CAP_PCT", 1.0)
    monkeypatch.setattr(dl, "SLATE_CAP_PCT", 1.0)
    monkeypatch.setattr(dl, "METRIC_SLATE_PCT", 1.0)
    b = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(dl, "BUCKET_ALLOW", {"level:85-89"})  # after _bot reset
    b.dry_balance_c = 60000                     # $600: past the boost step
    # scale mode via history in a DIFFERENT lane (90-92), so the 8%
    # treatment of 85-89 can only come from the override
    b.history = [{"tk": f"G{i}", "ots": f"o{i}", "trig": "level",
                  "entry": 91, "pnl": 0.2, "sold": True,
                  "pside": 0.91} for i in range(70)]
    b._refresh_caps(b.balance_c())
    assert b.place(mkts=[_mk("KXHIGHMIA-A8", bid=85, ask=88, is_low=False,
                             city="miami")]) == 1
    bet = next(iter(b.bets.values()))
    cost = bet["entry"] * bet["count"]
    assert cost > b.max_bet_c                   # beyond base 3%
    assert cost <= b.max_bet_pv_c               # inside the 8% earned cap
