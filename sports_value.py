#!/usr/bin/env python3
"""Sports value engine: de-vig a sportsbook line into a sharp 'fair'
probability, then measure the edge vs a Kalshi price (after fees).

The hard, valuable math lives here and is fully testable with no API key.
Matching Kalshi markets to games and pulling live odds are separate layers
(odds_feed.py / matching) that plug a 'fair_prob' into edge_cents().

Run `python sports_value.py` to self-test the math.
"""
from __future__ import annotations

from kalshibot.fees import fee_cents


def american_to_prob(odds: float) -> float:
    """American moneyline odds -> implied probability (incl. vig)."""
    o = float(odds)
    if o < 0:
        return (-o) / ((-o) + 100.0)
    return 100.0 / (o + 100.0)


def devig_two_way(odds_a: float, odds_b: float):
    """Two American prices -> vig-free fair probabilities (sum to 1)."""
    ia, ib = american_to_prob(odds_a), american_to_prob(odds_b)
    s = ia + ib
    if s <= 0:
        return 0.0, 0.0
    return ia / s, ib / s


def devig_multi(odds_list):
    """N-way outright market (e.g., championship) -> fair probs summing to 1."""
    imp = [american_to_prob(o) for o in odds_list]
    s = sum(imp)
    return [i / s for i in imp] if s > 0 else [0.0] * len(imp)


def edge_cents(fair_prob: float, kalshi_yes_ask_cents: int, taker: bool = True) -> float:
    """Expected value, in cents per contract, of BUYING YES at the Kalshi ask.

    EV = (fair_prob * 100  -  price)  -  fee.   Positive => value.
    """
    if kalshi_yes_ask_cents <= 0:
        return -999.0
    fee = fee_cents(kalshi_yes_ask_cents, 1, taker=taker)
    return fair_prob * 100.0 - kalshi_yes_ask_cents - fee


def best_side(fair_yes_prob: float, yes_ask_cents: int, no_ask_cents: int, taker: bool = True):
    """Given fair YES prob and both ask prices, return the better value side.

    Buying NO at no_ask is buying YES-not-happening; its fair prob is 1-fair.
    Returns (side, ev_cents).
    """
    ev_yes = edge_cents(fair_yes_prob, yes_ask_cents, taker)
    ev_no = edge_cents(1.0 - fair_yes_prob, no_ask_cents, taker)
    return ("yes", ev_yes) if ev_yes >= ev_no else ("no", ev_no)


# ----------------------------- self-test ---------------------------------
def _selftest():
    ok = True

    # 1) American -> prob
    assert abs(american_to_prob(-150) - 0.60) < 1e-9
    assert abs(american_to_prob(+150) - 0.40) < 1e-9
    assert abs(american_to_prob(+100) - 0.50) < 1e-9

    # 2) de-vig two-way: -150 / +130 -> remove the overround
    pa, pb = devig_two_way(-150, +130)
    assert abs((pa + pb) - 1.0) < 1e-9
    assert 0.57 < pa < 0.59, pa          # ~0.5798 fair for the favorite

    # 3) edge: book-fair 58%, Kalshi YES ask 53c -> clearly +EV
    ev = edge_cents(0.58, 53, taker=True)
    print(f"  fair 58%, Kalshi ask 53c -> EV {ev:+.1f}c per contract")
    assert ev > 2

    # 4) no edge when Kalshi is already fair/expensive
    ev2 = edge_cents(0.58, 60, taker=True)
    print(f"  fair 58%, Kalshi ask 60c -> EV {ev2:+.1f}c (should be negative)")
    assert ev2 < 0

    # 5) best_side picks NO when NO is the value
    side, ev3 = best_side(0.40, yes_ask_cents=55, no_ask_cents=52, taker=True)
    print(f"  fair YES 40%: better side = {side} (EV {ev3:+.1f}c)")
    assert side == "no" and ev3 > 0

    # 6) multi-way de-vig sums to 1
    probs = devig_multi([+200, +300, +250, +800])
    assert abs(sum(probs) - 1.0) < 1e-9

    print("ALL sports-value math tests passed" if ok else "FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
