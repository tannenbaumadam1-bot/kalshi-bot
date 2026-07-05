#!/usr/bin/env python3
"""Market-making VIABILITY analyzer for Kalshi (and Polymarket, read-only).

Answers one question with LIVE data: are spreads wide enough, on liquid enough
markets, for two-sided market-making (quote bid+ask, capture the spread) to be
profitable AFTER fees and adverse selection?

It measures real spreads on the highest-volume markets, computes the spread we
could actually capture net of maker fees, and runs a parametric adverse-
selection model to show where the EV turns negative.

Verdict (2026-07): NO for pure spread-capture on either venue - liquid markets
run 1-3c spreads (Kalshi) / ~1c (Polymarket), too tight to cover the ~1-2c
round-trip maker fee, and pro MMs already own that spread. The only real
"fee-earner" path is Polymarket's maker REBATES + liquidity REWARDS (it pays
LPs), which is reward-farming, not spread-capture. See research doc.

Run:  python3 mm_viability.py            (live measure both venues)
      python3 mm_viability.py --selftest
"""
from __future__ import annotations
import sys, statistics
import requests
from kalshibot.fees import fee_cents

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com/markets"


def _f(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0


def capture_net_cents(mkt_spread, mid):
    """Cents we net per round-trip IF we improve both sides by 1 tick and both
    fill with NO adverse move: (spread-2) captured minus two maker fees."""
    return (mkt_spread - 2) - fee_cents(mid, 1, taker=False) * 2


def mm_ev(mkt_spread, mid, p_both_fill=0.5, adverse_frac=0.25, adverse_loss=8):
    """Toy EV per quoting round-trip. Clean round-trips capture the net spread;
    a fraction of fills get adversely selected (informed flow) for adverse_loss
    cents each. Shows how little adverse selection it takes to go negative."""
    clean = capture_net_cents(mkt_spread, mid)
    return p_both_fill * clean - adverse_frac * adverse_loss


def kalshi_live(pages=8):
    rows, cursor = [], None
    for _ in range(pages):
        p = {"limit": 200, "status": "open", "with_nested_markets": "true"}
        if cursor: p["cursor"] = cursor
        try: d = requests.get(KALSHI + "/events", params=p, timeout=20).json()
        except Exception: break
        for ev in d.get("events", []) or []:
            st = ev.get("series_ticker", "") or ""
            if "MVE" in st: continue
            for m in ev.get("markets", []) or []:
                v = _f(m.get("volume_24h_fp"))
                yb = int(round(_f(m.get("yes_bid_dollars")) * 100))
                ya = int(round(_f(m.get("yes_ask_dollars")) * 100))
                if v > 0 and yb > 0 and ya > 0 and ya > yb:
                    rows.append((v, st, yb, ya, ya - yb))
        cursor = d.get("cursor")
        if not cursor: break
    rows.sort(reverse=True)
    return rows


def poly_live(limit=40):
    try:
        ms = requests.get(POLY_GAMMA, params={
            "active": "true", "closed": "false", "limit": limit,
            "order": "volume24hr", "ascending": "false"}, timeout=20).json()
    except Exception:
        return []
    out = []
    for m in ms:
        bb, ba, v = _f(m.get("bestBid")), _f(m.get("bestAsk")), _f(m.get("volume24hr"))
        if bb > 0 and ba > 0 and ba > bb:
            out.append((v, m.get("question", "")[:40], round((ba - bb) * 100),
                        bool(m.get("clobRewards") or m.get("rewardsMinSize"))))
    return out


def main():
    print("\n=== KALSHI (live) — can we capture the spread? ===")
    kr = kalshi_live()
    if kr:
        print("%-22s %9s %4s %6s" % ("top series", "vol24h", "spr", "captureNet"))
        for v, st, yb, ya, spr in kr[:12]:
            print("%-22s %9.0f %3dc %5dc" % (st[:22], v, spr, capture_net_cents(spr, (yb+ya)//2)))
        sp = sorted(r[4] for r in kr)
        liq = sorted(r[4] for r in kr[:max(10, len(kr)//4)])
        print("all %d live: median spread %dc, mean %.1fc | most-liquid quartile median %dc"
              % (len(sp), sp[len(sp)//2], statistics.mean(sp), liq[len(liq)//2]))
        pos = sum(1 for r in kr if capture_net_cents(r[4], (r[2]+r[3])//2) > 0)
        print("markets where naive spread-capture is even gross-positive (pre-adverse): %d / %d"
              % (pos, len(kr)))
    print("\n=== POLYMARKET (live) — spreads + rewards flag ===")
    pr = poly_live()
    if pr:
        for v, q, spr, rw in pr[:10]:
            print("%-42s vol24h $%9.0f spr %dc rewards=%s" % (q, v, spr, rw))
        sp = sorted(r[2] for r in pr)
        print("median spread %dc, mean %.1fc | %d/%d markets in a rewards program"
              % (sp[len(sp)//2], statistics.mean(sp), sum(1 for r in pr if r[3]), len(pr)))
    print("\n=== Adverse-selection sensitivity (Kalshi 3c spread @ 50c mid) ===")
    for af in (0.0, 0.05, 0.10, 0.20):
        print("  adverse fraction %4.0f%%  ->  EV/round-trip %+.2fc"
              % (100 * af, mm_ev(3, 50, p_both_fill=0.5, adverse_frac=af, adverse_loss=8)))
    print("  (breaks even only at near-zero adverse selection — unrealistic for a REST-poll bot)\n")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # 1c spread can't be captured after fees; wide spread positive pre-adverse
        assert capture_net_cents(1, 50) < 0
        assert capture_net_cents(10, 50) > 0
        # adverse selection erodes EV monotonically
        a = mm_ev(3, 50, adverse_frac=0.0); b = mm_ev(3, 50, adverse_frac=0.2)
        assert a > b
        print("mm_viability self-test PASSED")
    else:
        main()
