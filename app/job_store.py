"""Durable job state for the masumi SDK, plus recovery after a restart.

The SDK keeps jobs in memory by default, so a restart would lose paid jobs. `PostgresJobStorage`
implements the SDK's `JobStorage` interface on PostgreSQL. `recover_jobs` re-attaches payment
monitoring for unfinished jobs: a job whose escrow is (still) locked runs again, and treg's
idempotency key means a re-run does not pay for the same search twice.

Written against masumi==1.2.0 (pinned in requirements.txt); it uses two SDK internals,
`JobManager._payment_instances` and `MasumiAgentServer._handle_payment_confirmed`.
"""

import json
import re
import logging
import time
from typing import Any, Dict, Optional

import asyncpg
from masumi import JobStorage, MasumiAgentServer
from masumi.models import JobStatus

from .masumi_compat import V2Payment

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    job_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS {t}_status_idx ON {t} (status);
"""


class PostgresJobStorage(JobStorage):
    def __init__(self, dsn: str, table: str = "agent_jobs"):
        # One table per agent (standard and Pro share the database): recovery must only see its own jobs.
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", table):
            raise ValueError(f"invalid table name {table!r}")
        self._dsn, self._t = dsn, table
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            async with self._pool.acquire() as conn:
                await conn.execute(SCHEMA.format(t=self._t))

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _p(self) -> asyncpg.Pool:
        await self.connect()
        return self._pool

    async def create_job(self, job_id: str, job_data: Dict[str, Any]) -> None:
        pool = await self._p()
        await pool.execute(
            f"INSERT INTO {self._t} (job_id, status, data) VALUES ($1, $2, $3::jsonb)",
            job_id, job_data.get("status", ""), json.dumps(job_data),
        )

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        pool = await self._p()
        row = await pool.fetchval(f"SELECT data FROM {self._t} WHERE job_id = $1", job_id)
        return json.loads(row) if row is not None else None

    async def update_job(self, job_id: str, updates: Dict[str, Any]) -> None:
        pool = await self._p()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchval(f"SELECT data FROM {self._t} WHERE job_id = $1 FOR UPDATE", job_id)
            if row is None:
                raise ValueError(f"Job {job_id} not found in storage")
            data = {**json.loads(row), **updates}
            await conn.execute(
                f"UPDATE {self._t} SET data = $2::jsonb, status = $3, updated_at = now() WHERE job_id = $1",
                job_id, json.dumps(data), data.get("status", ""),
            )

    async def created_at(self, job_id: str) -> Optional[float]:
        """When the job row (the order) was created, epoch seconds; restricts the pre-v2 result fallback."""
        pool = await self._p()
        v = await pool.fetchval(f"SELECT extract(epoch FROM created_at)::float8 FROM {self._t} WHERE job_id = $1",
                                job_id)
        return float(v) if v is not None else None

    async def delete_job(self, job_id: str) -> None:
        pool = await self._p()
        await pool.execute(f"DELETE FROM {self._t} WHERE job_id = $1", job_id)

    async def list_jobs(self, status: Optional[str] = None) -> list:
        pool = await self._p()
        if status:
            rows = await pool.fetch(f"SELECT data FROM {self._t} WHERE status = $1", status)
        else:
            rows = await pool.fetch(f"SELECT data FROM {self._t}")
        return [json.loads(r["data"]) for r in rows]


def _epoch_s(value: Any) -> Optional[float]:
    """Payment Service times arrive as epoch milliseconds; accept seconds too."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v / 1000 if v > 1e11 else v


# On-chain states after which the job must not run again.
_RESULT_ON_CHAIN = {"ResultSubmitted", "Withdrawn", "Disputed", "DisputedWithdrawn"}
_REFUNDED = {"RefundRequested", "RefundWithdrawn", "FundsOrDatumInvalid"}
# Payment Service next actions meaning "our result hash was handed over and is being put on-chain".
# The on-chain state is still FundsLocked (or RefundRequested) until the transaction confirms, and the
# submit-result endpoint only accepts a payment whose next action is WaitingForExternalAction, so a
# second submit would be refused (404) and the job marked failed although its result is on its way.
# Source: masumi-payment-service `prisma/schema.prisma` (enum PaymentAction),
# `src/routes/api/payments/submit-result/index.ts`, `src/utils/logic/state-transitions/transition-tables.ts`.
_SUBMIT_IN_PROGRESS = {"SubmitResultRequested", "SubmitResultInitiated"}


async def _payment_state(payment: V2Payment, payment_id: str) -> tuple[Optional[str], Optional[str]]:
    """(onChainState, NextAction.requestedAction) of the payment, or (None, None) if unreadable."""
    try:
        data = (await payment.check_payment_status_by_identifier(payment_id)).get("data") or {}
        next_action = data.get("NextAction")
        action = next_action.get("requestedAction") if isinstance(next_action, dict) else None
        return data.get("onChainState"), action
    except Exception as e:
        logger.warning("Could not read the on-chain state of %s...: %s", payment_id[:12], type(e).__name__)
        return None, None


async def recover_jobs(server: MasumiAgentServer, now: Optional[float] = None) -> dict:
    """Bring unfinished jobs back after a restart.

    - awaiting_payment: monitoring is re-attached; the job runs when the escrow is locked.
    - running, result already on-chain, or its submission already requested from the Payment Service
      (crash between "submit result" and bookkeeping): marked completed with the stored result, never
      run or submitted twice.
    - running, refunded (and no result submission in progress): marked failed.
    - running, escrow still locked: runs again; the stored result (or treg's idempotency keys) keeps
      the on-chain hash identical and avoids paying providers twice.
    """
    from .agent import stored_result  # local import: agent imports this module's dependencies
    from .job_identity import identity_for_job
    from .ledger import get_ledger

    now = time.time() if now is None else now
    manager = server.job_manager
    counts = {"resumed": 0, "expired": 0, "completed": 0, "refunded": 0}
    for status in (JobStatus.AWAITING_PAYMENT.value, JobStatus.RUNNING.value):
        for job in await manager.list_jobs(status):
            job_id, payment_id = job["job_id"], job.get("payment_id")
            deadline = job.get("pay_by_time") if status == JobStatus.AWAITING_PAYMENT.value else job.get("submit_result_time")
            deadline_s = _epoch_s(deadline)
            if not payment_id or (deadline_s is not None and now > deadline_s):
                await manager.set_job_failed(job_id, f"Deadline passed while the agent was offline ({status})")
                counts["expired"] += 1
                continue

            payment = V2Payment(
                agent_identifier=job.get("agent_identifier") or server.agent_identifier,
                config=server.config,
                identifier_from_purchaser=job["identifier_from_purchaser"],
                input_data=job.get("input_data") or {},
                network=server.network,
            )
            payment.payment_ids.add(payment_id)

            if status == JobStatus.RUNNING.value:
                state, action = await _payment_state(payment, payment_id)
                if state in _REFUNDED and action not in _SUBMIT_IN_PROGRESS:
                    await manager.set_job_failed(job_id, f"Payment refunded while the agent was offline ({state})")
                    counts["refunded"] += 1
                    continue
                if state in _RESULT_ON_CHAIN or action in _SUBMIT_IN_PROGRESS:
                    # Result on-chain, or its submission already handed to the Payment Service: never
                    # run or submit again; the Payment Service finishes (or retries) the submission.
                    created = await manager.storage.created_at(job_id) if hasattr(manager.storage, "created_at") \
                        else None
                    identity = identity_for_job({**job, "job_id": job_id}, getattr(server, "tier", "standard"),
                                                created)
                    stored = await stored_result(get_ledger(), identity)
                    if stored is not None:
                        await manager.storage.update_job(job_id, {"status": JobStatus.COMPLETED.value,
                                                                  "result": stored})
                    else:
                        await manager.set_job_failed(job_id, "Result was submitted before the restart but is "
                                                             "not stored locally")
                    counts["completed"] += 1
                    continue

            manager._payment_instances[job_id] = payment

            async def on_locked(_payment: Dict[str, Any], job_id=job_id, payment_id=payment_id):
                await server._handle_payment_confirmed(job_id, payment_id)

            await payment.start_status_monitoring(callback=on_locked)
            counts["resumed"] += 1
    if any(counts.values()):
        logger.info("Recovered jobs after restart: %s", counts)
    return counts
