"""Per-job spend budget against the daily cap (BL-13).

A job reserves its worst-case treg spend in the ledger before its first treg call (`ledger.reserve`), and
gets back what the daily cap still allows. Every treg call, the free Apollo ones included, must fit in what
is left of that grant at its max cost before it is sent; afterwards its reported cost (or, without a valid
cost header, its max cost) is deducted. Calls run one after another within a job, so a call that turns out
free gives its share back to the next one. At the end the job settles: its actual spend replaces the
reservation in the same ledger transaction.
"""

import math

from .treg_client import TregError


class SpendCapReached(TregError):
    """The job's grant does not cover the next call's max cost; the call is not sent."""


class JobBudget:
    def __init__(self, granted: float = math.inf):
        self.granted = granted
        self.treg_usd = 0.0
        self.enrich_usd = 0.0
        self.cost_assumed_calls = 0

    @property
    def spent(self) -> float:
        return self.treg_usd + self.enrich_usd

    @property
    def remaining(self) -> float:
        return max(0.0, self.granted - self.spent)

    def covers(self, max_cost_usd: float) -> bool:
        return self.remaining + 1e-9 >= max_cost_usd

    def charge(self, cost_usd: float, *, assumed: bool = False, kind: str = "treg") -> None:
        if kind == "enrich":
            self.enrich_usd += cost_usd
        else:
            self.treg_usd += cost_usd
        self.cost_assumed_calls += 1 if assumed else 0

    def charge_call(self, call, kind: str = "treg") -> float:
        """Deducts a TregSearchResult's or TregError's cost; returns it."""
        cost = getattr(call, "cost_usd", 0.0) or 0.0
        self.charge(cost, assumed=bool(getattr(call, "cost_assumed", False)), kind=kind)
        return cost

    def totals(self) -> dict:
        return {"treg_usd": self.treg_usd, "enrich_usd": self.enrich_usd,
                "cost_assumed_calls": self.cost_assumed_calls}
