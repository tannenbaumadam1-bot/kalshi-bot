"""Simple CSV journal so you can see exactly what the bot did and why."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Any, Dict

FIELDS = [
    "timestamp", "environment", "event", "ticker", "action", "side",
    "count", "price_cents", "est_fee_cents", "reason", "detail",
]


class Journal:
    def __init__(self, path: str = "logs/trades.csv", environment: str = "demo"):
        self.path = path
        self.environment = environment
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def log(self, event: str, **kw: Any) -> None:
        row: Dict[str, Any] = {k: "" for k in FIELDS}
        row.update(kw)
        row["timestamp"] = datetime.now().isoformat(timespec="seconds")
        row["environment"] = self.environment
        row["event"] = event
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(
                {k: row.get(k, "") for k in FIELDS}
            )
