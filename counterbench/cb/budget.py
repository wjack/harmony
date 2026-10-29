"""Spend tracking against the shared ledger."""

import json
from datetime import datetime, timezone

from .models import LEDGER


class Budget:
    def __init__(self, cap: float):
        self.cap = cap
        self.spent = sum(json.loads(line)["cost_usd"] for line in LEDGER.open()) if LEDGER.exists() else 0.0
        self.in_flight = 0
        self.max_seen = 0.0

    def try_start(self) -> bool:
        reserve = max(self.max_seen, 0.6) * 1.3
        if self.spent + (self.in_flight + 1) * reserve > self.cap:
            return False
        self.in_flight += 1
        return True

    def finish(self, cost: float, record: dict) -> None:
        self.in_flight -= 1
        self.spent += cost
        self.max_seen = max(self.max_seen, cost)
        with LEDGER.open("a") as f:
            f.write(json.dumps({**record, "cost_usd": round(cost, 5), "ts": datetime.now(timezone.utc).isoformat()}) + "\n")
