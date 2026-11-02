"""Checks in front of `/start_job`, before the SDK creates a payment request (BL-01, BL-15, BL-33).

A refused request creates no Payment Service record, no monitoring task and no escrow; nothing in the payment
path changes. In order:

1. Static reasons (Pro without enrichment, PAUSE_NEW_JOBS): 503, also reported by /availability.
2. Rate limits (BL-15), per agent process, sliding one-hour windows: per client and in total. The client is the
   connecting address, or the first entry of RATE_LIMIT_CLIENT_HEADER when the proxy sets one. Behind a proxy
   without that header, and for orders relayed by a marketplace, every request shares one client, so the
   per-client limit acts as a second total limit.
3. Unpaid orders (BL-15): at most MAX_AWAITING_PAYMENT orders whose pay-by time has not passed. The pay-by time
   is the Payment Service's (`payByTime` of the payment request, stored by the SDK as the job's `pay_by_time`);
   an order without a readable one counts (fail closed). Expired unpaid orders do not count: masumi 1.2.0 never
   expires them while it runs (only `recover_jobs` does, at startup), so counting them would block sales until a
   restart (DEPLOY_PREFLIGHT B1). Requests admitted but still being created by the SDK count too, under a lock,
   so concurrent requests cannot overshoot the limit.
4. Daily budget (BL-33): refused while what is left of today's shared cap (settled spend + open holds, all agents)
   is below one job's worst-case spend, or when the ledger cannot be read. This is a check, not a reservation:
   unpaid orders hold nothing, so they cannot use up the cap. The cap can still run out between /start_job and
   the payment; then the job's all-or-nothing reservation gets nothing, it makes no treg call and fails
   (refundable; see agent.BudgetUnavailable).
429 and 503 answers carry Retry-After and a generic message; spend figures are never shown to buyers.
"""

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .job_store import _epoch_s
from .ledger import end_of_utc_day, get_ledger
from .resolver import MAX_DOMAIN_CANDIDATES
from .settings import Settings
from .treg_client import APOLLO_MAX_COST_USD

logger = logging.getLogger(__name__)

HOUR = 3600.0


def max_spend_ceiling(settings: Settings) -> float:
    """The largest spend ceiling any job of this agent can reserve (every domain candidate probed)."""
    enrich = settings.enrich_budget_usd if settings.enrichment_enabled else 0.0
    return round((MAX_DOMAIN_CANDIDATES + 1) * APOLLO_MAX_COST_USD + settings.job_budget_usd + enrich, 6)


class _Window:
    def __init__(self, limit: int, clock: Callable[[], float]):
        self.limit, self.clock, self.hits = limit, clock, deque()

    def _trim(self) -> None:
        now = self.clock()
        while self.hits and now - self.hits[0] >= HOUR:
            self.hits.popleft()

    def full(self) -> bool:
        self._trim()
        return self.limit >= 0 and len(self.hits) >= self.limit

    def retry_after(self) -> int:
        self._trim()
        return max(1, math.ceil(HOUR - (self.clock() - self.hits[0]))) if self.hits else 1

    def add(self) -> float:
        at = self.clock()
        self.hits.append(at)
        return at

    def undo(self, at: float) -> None:
        try:
            self.hits.remove(at)
        except ValueError:
            pass

    def empty(self) -> bool:
        self._trim()
        return not self.hits


class StartJobGate:
    def __init__(self, settings: Settings, static_reason: Optional[str] = None, server: Any = None,
                 clock: Callable[[], float] = time.monotonic, budget_ttl_s: float = 15.0,
                 wall: Callable[[], float] = time.time):
        self.settings, self.static_reason, self.server, self.clock, self.wall = (settings, static_reason, server,
                                                                                 clock, wall)
        self.admitting = 0  # admitted /start_job requests the SDK is still turning into stored orders
        self._admission = asyncio.Lock()
        self.total = _Window(settings.start_job_per_hour, clock)
        self.clients: dict[str, _Window] = {}
        self.budget_ttl_s = budget_ttl_s
        self._budget: tuple[float, Optional[str]] = (-math.inf, None)
        self.counters = {"rate_limited": 0, "unpaid_limit": 0, "budget_refused": 0, "accepted": 0}

    def client_key(self, request: Request) -> str:
        header = self.settings.rate_limit_client_header
        if header:
            value = request.headers.get(header, "")
            first = value.split(",")[0].strip()
            if first:
                return first[:64]
        return request.client.host if request.client else "?"

    async def budget_problem(self) -> Optional[str]:
        """Why the shared daily budget cannot fund another job now, or None. Cached for budget_ttl_s."""
        at, cached = self._budget
        if self.clock() - at < self.budget_ttl_s:
            return cached
        need = max_spend_ceiling(self.settings)
        try:
            spent = await get_ledger().spend_today()
        except Exception as e:  # fail closed: a job would reserve 0 and fail after payment
            logger.warning("Spend ledger unavailable for the budget check: %s", type(e).__name__)
            problem = "the spend ledger is unavailable"
        else:
            left = self.settings.daily_spend_cap_usd - spent
            problem = None if left + 1e-9 >= need else "today's data budget is used up"
            if problem:
                logger.warning("Not accepting jobs: %.4f USD left of the %.2f USD daily cap, a job needs %.4f",
                               left, self.settings.daily_spend_cap_usd, need)
        self._budget = (self.clock(), problem)
        return problem

    async def unpaid_jobs(self) -> int:
        """Orders awaiting payment whose Payment Service pay-by time has not passed (unreadable ones count)."""
        if self.server is None:
            return 0
        now = self.wall()
        open_orders = 0
        for job in await self.server.job_manager.list_jobs("awaiting_payment"):
            pay_by = _epoch_s(job.get("pay_by_time"))
            open_orders += 1 if pay_by is None or pay_by > now else 0
        return open_orders

    def release(self) -> None:
        """The admitted request has finished (its order is stored, or creating it failed)."""
        self.admitting = max(0, self.admitting - 1)

    async def availability_problem(self) -> Optional[str]:
        return self.static_reason or await self.budget_problem()

    async def check(self, request: Request) -> Optional[JSONResponse]:
        """None if the order may proceed (it is then counted, and `release` must follow), else the refusal."""
        if self.static_reason:
            return _refuse(503, f"Not accepting jobs: {self.static_reason}")
        client = self.clients.setdefault(self.client_key(request),
                                         _Window(self.settings.start_job_per_client_per_hour, self.clock))
        for window in (client, self.total):
            if window.full():
                self.counters["rate_limited"] += 1
                return _refuse(429, "Too many new jobs; try again later", window.retry_after())
        # Take the slots before the awaits below, so concurrent requests cannot all pass; give them back if refused.
        slots = (client.add(), self.total.add())
        refusal = await self._unpaid_or_budget_refusal()
        if refusal is not None:
            client.undo(slots[0])
            self.total.undo(slots[1])
            return refusal
        if len(self.clients) > 10_000:  # bound memory: forget clients with no request in the last hour
            self.clients = {k: w for k, w in self.clients.items() if not w.empty()}
        self.counters["accepted"] += 1
        return None

    async def _unpaid_or_budget_refusal(self) -> Optional[JSONResponse]:
        async with self._admission:  # count and admit in one step: concurrent requests cannot both take the last slot
            limit = self.settings.max_awaiting_payment
            if limit >= 0:
                try:
                    unpaid = await self.unpaid_jobs()
                except Exception as e:
                    logger.warning("Could not count unpaid jobs: %s", type(e).__name__)
                    unpaid = limit  # fail closed
                if unpaid + self.admitting >= limit:
                    self.counters["unpaid_limit"] += 1
                    return _refuse(429, "Too many unpaid jobs; try again later", 300)
            problem = await self.budget_problem()
            if problem:
                self.counters["budget_refused"] += 1
                now = datetime.now(timezone.utc).timestamp()
                return _refuse(503, f"Not accepting jobs: {problem}; try again after 00:00 UTC",
                               max(60, math.ceil(end_of_utc_day(now) - now)))
            self.admitting += 1
            return None


def _refuse(status: int, detail: str, retry_after: Optional[int] = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse(status_code=status, content={"detail": detail}, headers=headers)


def add_job_gate(app: FastAPI, gate: StartJobGate) -> None:
    if gate.static_reason:
        logger.warning("Not accepting new jobs: %s", gate.static_reason)

    @app.middleware("http")
    async def start_job_gate(request: Request, call_next):
        if request.method == "POST" and request.url.path.rstrip("/") == "/start_job":
            refusal = await gate.check(request)
            if refusal is not None:
                return refusal
            try:
                return await call_next(request)
            finally:
                gate.release()
        return await call_next(request)
