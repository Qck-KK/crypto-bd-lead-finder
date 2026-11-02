"""Per-attempt spend budget against the daily cap (BL-13, BL-34).

Each run of a job (an attempt) reserves its worst-case treg spend in the ledger before its first treg call
(`ledger.reserve`) and gets back what the daily cap still allows. Every treg call goes through
`JobBudget.run`:

1. The call must fit in what is left of the grant at its max cost, or it is not sent (SpendCapReached).
2. Write-ahead: the call's max cost is written to the attempt's ledger row as pending *before* the call is
   sent. If that write fails the call is not sent (SpendLedgerUnavailable), so the ledger always knows about
   every call that may have been charged.
3. Afterwards the cost treg reported replaces the pending amount (confirmed); without a valid cost the max
   cost stays charged (assumed). A call that ends without an answer (cancelled, crashed) keeps its max cost.

Confirmed and assumed amounts are kept apart: confirmed is what treg reported, assumed is an upper bound
(the max cost we allowed), never presented as the actual cost.
"""

import logging
import math
from typing import Awaitable, Callable, Optional

from .treg_client import TregError, floor_cost

logger = logging.getLogger(__name__)

Progress = Callable[[dict], Awaitable[None]]


class SpendCapReached(TregError):
    """The attempt's grant does not cover the next call's max cost; the call is not sent."""


class SpendLedgerUnavailable(TregError):
    """The call's pending cost could not be written to the ledger; the call is not sent."""


class BudgetUnavailable(RuntimeError):
    """BL-33: the attempt did not get its full worst-case budget (cap used up, or ledger unreadable). The job makes
    no treg call and fails, so the buyer's payment stays refundable; it never runs on part of its budget."""


class JobBudget:
    def __init__(self, granted: float = math.inf, progress: Optional[Progress] = None):
        self.granted = granted
        self.confirmed = {"treg": 0.0, "enrich": 0.0}
        self.assumed = {"treg": 0.0, "enrich": 0.0}
        self.pending = 0.0
        self.cost_assumed_calls = 0
        self.progress_failed = False  # a post-call write failed: the ledger row may be behind (conservatively)
        self._progress = progress

    @property
    def treg_usd(self) -> float:
        return self.confirmed["treg"] + self.assumed["treg"]

    @property
    def enrich_usd(self) -> float:
        return self.confirmed["enrich"] + self.assumed["enrich"]

    @property
    def spent(self) -> float:
        return self.treg_usd + self.enrich_usd

    @property
    def remaining(self) -> float:
        return max(0.0, self.granted - self.spent - self.pending)

    def covers(self, max_cost_usd: float) -> bool:
        return self.remaining + 1e-9 >= max_cost_usd

    def charge(self, cost_usd: float, *, assumed: bool = False, kind: str = "treg") -> None:
        (self.assumed if assumed else self.confirmed)[kind] += cost_usd
        self.cost_assumed_calls += 1 if assumed else 0

    def snapshot(self) -> dict:
        return {"confirmed_usd": sum(self.confirmed.values()), "assumed_usd": sum(self.assumed.values()),
                "pending_usd": self.pending, "cost_assumed_calls": self.cost_assumed_calls}

    async def run(self, max_cost_usd: float, call: Callable[[], Awaitable], kind: str = "treg"):
        """Sends one treg call if the grant covers its max cost; returns its result. A TregError from the
        call propagates after its cost has been charged."""
        if not self.covers(max_cost_usd):
            raise SpendCapReached("daily spend budget used up")
        hold = floor_cost(max_cost_usd)
        self.pending = hold
        if self._progress is not None:
            try:
                await self._progress(self.snapshot())
            except Exception as e:
                self.pending = 0.0
                logger.warning("Spend ledger write failed, treg call not sent: %s", type(e).__name__)
                raise SpendLedgerUnavailable("spend ledger unavailable") from e
        answered = False
        try:
            result = await call()
            self.charge(result.cost_usd, assumed=result.cost_assumed, kind=kind)
            answered = True
            return result
        except TregError as e:
            self.charge(e.cost_usd, assumed=e.cost_assumed, kind=kind)
            answered = True
            raise
        finally:
            if not answered:  # cancelled or an unexpected error: the request may have been charged
                self.charge(hold, assumed=True, kind=kind)
            self.pending = 0.0
            if self._progress is not None:
                try:
                    await self._progress(self.snapshot())
                except Exception as e:  # the row keeps the pending max cost: conservative, never too low
                    self.progress_failed = True
                    logger.warning("Spend ledger update after a treg call failed: %s", type(e).__name__)

    def totals(self) -> dict:
        return {"treg_usd": self.treg_usd, "enrich_usd": self.enrich_usd,
                "unconfirmed_usd": sum(self.assumed.values()), "cost_assumed_calls": self.cost_assumed_calls}
