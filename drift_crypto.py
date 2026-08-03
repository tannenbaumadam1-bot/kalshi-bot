#!/usr/bin/env python3
"""LANE 2 AUDITION - the drift brain on Kalshi CRYPTO (era "driftc1"), PAPER.

Adam 7/31: the multi-lane thesis - one proven edge (near-resolution
convergence), many ponds. Crypto hourlies/dailies are the structural twin
of the weather books (continuous public settlement signal via CF
Benchmarks spot, hard convergence into each close, hundreds of settlement
cycles/day) with ~50-100x the depth. Whether the RETAIL-MISPRICING
ingredient survives sharper counterparties is exactly what this audition
answers, with paper money.

Config = the LIVE book's hard-won evidence, not the paper defaults:
  - entry floor 80c (7/31: every sub-80c band lost live money, -$8.10/36)
  - stop 35c, trail OFF (autopsies: exits were leaks, wobbles recover)
  - close horizon 24h (hourly/daily cycles only - convergence territory)
  - spread <= 3c, 24h volume >= 500 (crypto books are deep; demand it)
  - level entries only by construction (floor kills the climb band)
  - fee-inclusive accounting on every fill (the driftw2-fin lesson:
    92W/2L still lost money to fees - win rate is not edge)

THE GATE (promote/kill bar): 100+ settled on era driftc1 with net > 0
AFTER FEES -> goes live on the first capital-ladder rung. Anything less
-> killed, having cost $0.

LIVE MANDATE (8/3, Adam - after the live weather book forfeited $25 to
unfilled maker joins): when this lane goes live, entries are
TAKER-FIRST BY DEFAULT. Crypto books are deep and tight (1-3c toll vs
the audition's ~+9c/bet edge) - the missed-fill leak class gets
designed OUT of lane 2, not patched afterward. Maker joins only as the
wide-spread fallback, and any join follows the pursuit ladder
(requote every cycle, cross at 45min).

Implementation: wraps DriftWide with a config context that swaps the
module constants in and RESTORES them after every call, so importing this
module never mutates drift_wide for anyone else (tests, a revived driftw).
State -> logs/driftc_state.json   Bets -> logs/driftc_bets.csv
"""
from __future__ import annotations

import contextlib
import os

import drift_wide as dw

GATE_TARGET = int(os.environ.get("DRIFTC_GATE_TARGET", "100"))

_CFG = {
    "STATE": os.path.join("logs", "driftc_state.json"),
    "BETS": os.path.join("logs", "driftc_bets.csv"),
    "ERA": "driftc1",
    "CATEGORIES": {"Crypto"},
    "MIN_C": 80,
    "ENTRY_MIN_C": 80,
    "LEVEL_C": 80,
    "MAX_ENTRY": int(os.environ.get("DRIFTC_MAX_ENTRY", "92")),
    "STOP_C": 35,
    "FADE_DROP_C": 999,          # trail OFF: fade can never trigger
    "MAX_H": float(os.environ.get("DRIFTC_MAX_H", "24")),
    "CLIMB_H": 6.0,
    "MAX_PER_DAY": int(os.environ.get("DRIFTC_MAX_PER_DAY", "40")),
    "MIN_VOL24": float(os.environ.get("DRIFTC_MIN_VOL24", "500")),
    "MAX_SPREAD_C": int(os.environ.get("DRIFTC_MAX_SPREAD", "3")),
}


@contextlib.contextmanager
def _cfg():
    old = {k: getattr(dw, k) for k in _CFG}
    try:
        for k, v in _CFG.items():
            setattr(dw, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(dw, k, v)


class DriftCrypto(dw.DriftWide):
    """DriftWide with the crypto-audition config applied per call."""


def _wrap(name):
    base = getattr(dw.DriftWide, name)

    def method(self, *a, **kw):
        with _cfg():
            return base(self, *a, **kw)
    method.__name__ = name
    return method


for _n in ("__init__", "load", "save", "settle", "stop_check", "place",
           "step", "summary", "_gate", "_placed_today"):
    setattr(DriftCrypto, _n, _wrap(_n))
