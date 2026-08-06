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
