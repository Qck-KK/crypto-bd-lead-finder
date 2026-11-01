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


async def recover_jobs(server: MasumiAgentServer, now: Optional[float] = None) -> dict:
    """Re-attach payment monitoring for jobs left awaiting payment or running."""
    now = time.time() if now is None else now
    manager = server.job_manager
    counts = {"resumed": 0, "expired": 0}
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
            manager._payment_instances[job_id] = payment

            async def on_locked(_payment: Dict[str, Any], job_id=job_id, payment_id=payment_id):
                await server._handle_payment_confirmed(job_id, payment_id)

            await payment.start_status_monitoring(callback=on_locked)
            counts["resumed"] += 1
    if counts["resumed"] or counts["expired"]:
        logger.info("Recovered jobs after restart: %s", counts)
    return counts
