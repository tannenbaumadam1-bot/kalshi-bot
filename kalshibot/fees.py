"""Kalshi trading fee math.

Kalshi's trading fee scales with how close the price is to 50 cents:

    fee_per_contract = round_up( rate * price * (1 - price) )   [in cents]

where price is in dollars (0.01 - 0.99). Taker rate is ~0.07, maker
(resting limit) orders are charged at a much lower rate (~0.0175).

Fees are rounded UP to the next cent, so on tiny orders the rounding
matters a lot. Always confirm the current schedule:
https://docs.kalshi.com/getting_started/fee_rounding
"""

from __future__ import annotations

import math

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


def fee_cents(price_cents: int, count: int, taker: bool = True) -> int:
    """Estimated fee in CENTS for `count` contracts at `price_cents`."""
    if count <= 0:
        return 0
    price = max(1, min(99, price_cents)) / 100.0
    rate = TAKER_RATE if taker else MAKER_RATE
    raw_cents = rate * price * (1.0 - price) * count * 100.0
    # shave float noise so an exact value like 175.0 isn't bumped to 176
    return int(math.ceil(raw_cents - 1e-9))


def round_trip_edge_cents(buy_price_cents, sell_price_cents, count=1, taker=True):
    """Net profit in CENTS from buying then selling, after fees on both legs."""
    gross = (sell_price_cents - buy_price_cents) * count
    fees = (fee_cents(buy_price_cents, count, taker)
            + fee_cents(sell_price_cents, count, taker))
    return gross - fees


def min_profitable_spread_cents(price_cents, taker=True):
    """Smallest bid-ask spread (cents) that nets >0 after fees on a round trip."""
    two_leg_fees = fee_cents(price_cents, 1, taker) * 2
    return two_leg_fees + 1
