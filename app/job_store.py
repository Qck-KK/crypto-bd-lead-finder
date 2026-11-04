"""Durable job state for the masumi SDK, plus recovery after a restart.

The SDK keeps jobs in memory by default, so a restart would lose paid jobs. `PostgresJobStorage`
implements the SDK's `JobStorage` interface on PostgreSQL. `recover_jobs` re-attaches payment
monitoring for unfinished jobs: a job whose escrow is (still) locked runs again, and treg's
idempotency key means a re-run does not pay for the same search twice.

Written against masumi==1.2.0 (pinned in requirements.txt); it uses two SDK internals,
`JobManager._payment_instances` and `MasumiAgentServer._handle_payment_confirmed`.
"""

import asyncio
import hmac
import json
import re
import logging
import time
from typing import Any, Dict, Optional

import asyncpg
from masumi import JobStorage, MasumiAgentServer
from masumi.helper_functions import create_masumi_output_hash
from masumi.models import JobStatus

from .logs import sanitize
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


# On-chain states (Payment Service 0.29 `OnChainState`, prisma/schema.prisma) after which the job must not run again.
_RESULT_ON_CHAIN = {"ResultSubmitted", "Withdrawn", "Disputed", "DisputedWithdrawn"}
# The refund is decided or paid out: the seller authorized it (RefundAuthorized, which also clears the result hash)
# or the buyer withdrew it. Final whatever the next action says, so checked before the submission check below.
_REFUND_FINAL = {"RefundAuthorized", "RefundWithdrawn"}
_REFUNDED = {"RefundRequested", "FundsOrDatumInvalid"}
# The buyer authorized the seller's withdrawal after a dispute; only reachable from Disputed, so a result was
# submitted and its hash is still on the payment (`resultHash`).
_WITHDRAW_AUTHORIZED = "WithdrawAuthorized"
_KNOWN_STATES = _RESULT_ON_CHAIN | _REFUND_FINAL | _REFUNDED | {_WITHDRAW_AUTHORIZED, "FundsLocked"}
# Payment Service next actions meaning "our result hash was handed over and is being put on-chain".
# The on-chain state is still FundsLocked (or RefundRequested) until the transaction confirms, and the
# submit-result endpoint only accepts a payment whose next action is WaitingForExternalAction, so a
# second submit would be refused (404) and the job marked failed although its result is on its way.
# Source: masumi-payment-service `prisma/schema.prisma` (enum PaymentAction),
# `src/routes/api/payments/submit-result/index.ts`, `src/utils/logic/state-transitions/transition-tables.ts`.
_SUBMIT_IN_PROGRESS = {"SubmitResultRequested", "SubmitResultInitiated"}


# Upper bound for one payment lookup during recovery. Recovery runs before the agent serves requests, and the SDK's
# lookup has no timeout of its own (aiohttp's default is 5 minutes).
LOOKUP_TIMEOUT_S = 15.0
# _settle_running_job: the on-chain state could not be read, so nothing may be inferred from it.
_UNVERIFIED = "unverified"


async def _payment_state(payment: V2Payment, payment_id: str) -> tuple[Optional[str], Optional[str], dict]:
    """(onChainState, NextAction.requestedAction, payment record) of the payment, or (None, None, {}) if unreadable
    (error, timeout after LOOKUP_TIMEOUT_S, or no record)."""
    try:
        response = await asyncio.wait_for(payment.check_payment_status_by_identifier(payment_id), LOOKUP_TIMEOUT_S)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            data = {}
        next_action = data.get("NextAction")
        action = next_action.get("requestedAction") if isinstance(next_action, dict) else None
        state = data.get("onChainState")
        return (state if isinstance(state, str) else None), action, data
    except asyncio.TimeoutError:
        logger.warning("Could not read the on-chain state of %s...: no answer within %.0f s", payment_id[:12],
                       LOOKUP_TIMEOUT_S)
        return None, None, {}
    except Exception as e:
        logger.warning("Could not read the on-chain state of %s...: %s", payment_id[:12], type(e).__name__)
        return None, None, {}


def _hash_matches(result: str, identifier_from_purchaser: str, on_chain_hash: Any) -> bool:
    """Whether `result` is what was submitted: its MIP-004 output hash (the one the SDK submits) equals the payment's
    on-chain `resultHash`."""
    if not isinstance(on_chain_hash, str) or not on_chain_hash:
        return False
    local = create_masumi_output_hash(result, identifier_from_purchaser)
    return hmac.compare_digest(local.lower(), on_chain_hash.strip().lower())


async def recover_jobs(server: MasumiAgentServer, now: Optional[float] = None) -> dict:
    """Bring unfinished jobs back after a restart.

    - awaiting_payment: monitoring is re-attached; the job runs when the escrow is locked.
    - running, result already on-chain, or its submission already requested from the Payment Service
      (crash between "submit result" and bookkeeping): marked completed with the stored result, never
      run or submitted twice.
    - running, refund authorized or withdrawn: marked failed, whatever the next action says (checked first).
    - running, withdrawal authorized after a dispute: marked completed only if the stored result hashes to the
      payment's on-chain resultHash; otherwise failed with a manual-reconciliation message. Never run or
      submitted again.
    - running, refunded (and no result submission in progress): marked failed.
    - running, escrow still locked: runs again; the stored result (or treg's idempotency keys) keeps
      the on-chain hash identical and avoids paying providers twice.
    - running, an on-chain state this code does not know: logged, then treated as before (deadline, else
      monitoring re-attached, which only runs the job on FundsLocked).
    - running, on-chain state unreadable (Payment Service down, timeout, no record): nothing is inferred. Before
      its result deadline monitoring is re-attached as before (it re-checks the payment before running); after it,
      the job is left running, untouched, for the next start or manual reconciliation (counted in `errors`).
    Running jobs are settled from their on-chain state before the deadline rule, so a job whose result or refund
    is already on-chain is not reported as a missed deadline. Each job is recovered on its own: an error (database,
    ledger, ...) leaves that job unchanged, is logged and counted in `errors`, and the next job is recovered.
    Finished jobs are not listed again, so running this twice changes nothing more.
    """
    now = time.time() if now is None else now
    manager = server.job_manager
    counts = {"resumed": 0, "expired": 0, "completed": 0, "refunded": 0, "reconcile": 0, "errors": 0}
    for status in (JobStatus.AWAITING_PAYMENT.value, JobStatus.RUNNING.value):
        for job in await manager.list_jobs(status):
            job_id = job.get("job_id") if isinstance(job, dict) else None
            try:
                outcome = await _recover_job(server, job, status, now)
            except Exception as e:
                if job_id is not None:
                    manager._payment_instances.pop(job_id, None)
                logger.error("Recovery of job %s failed; left unchanged for the next start: %s: %s",
                             sanitize(job_id, 64), type(e).__name__, sanitize(e, 200))
                outcome = "errors"
            counts[outcome] += 1
    if any(counts.values()):
        logger.info("Recovered jobs after restart: %s", counts)
    return counts


async def _recover_job(server: MasumiAgentServer, job: dict, status: str, now: float) -> str:
    """Recover one unfinished job; returns the `recover_jobs` count it falls under."""
    manager = server.job_manager
    job_id, payment_id = job["job_id"], job.get("payment_id")
    if not payment_id:
        await manager.set_job_failed(job_id, f"Deadline passed while the agent was offline ({status})")
        return "expired"

    payment = V2Payment(
        agent_identifier=job.get("agent_identifier") or server.agent_identifier,
        config=server.config,
        identifier_from_purchaser=job["identifier_from_purchaser"],
        input_data=job.get("input_data") or {},
        network=server.network,
    )
    payment.payment_ids.add(payment_id)
    deadline = job.get("pay_by_time") if status == JobStatus.AWAITING_PAYMENT.value else job.get("submit_result_time")
    deadline_s = _epoch_s(deadline)
    deadline_passed = deadline_s is not None and now > deadline_s

    if status == JobStatus.RUNNING.value:
        outcome = await _settle_running_job(server, job, payment, payment_id)
        if outcome == _UNVERIFIED:
            if deadline_passed:
                # Its payment may hold a submitted result or a refund: failing it on the local deadline alone
                # could mislabel it. Keep it as it is.
                logger.warning("Job %s: payment state unreadable and the result deadline has passed; left running "
                               "for the next start or manual reconciliation", job_id)
                return "errors"
        elif outcome:
            return outcome

    if deadline_passed:
        await manager.set_job_failed(job_id, f"Deadline passed while the agent was offline ({status})")
        return "expired"

    manager._payment_instances[job_id] = payment

    async def on_locked(_payment: Dict[str, Any], job_id=job_id, payment_id=payment_id):
        await server._handle_payment_confirmed(job_id, payment_id)

    await payment.start_status_monitoring(callback=on_locked)
    return "resumed"


async def _settle_running_job(server: MasumiAgentServer, job: dict, payment: V2Payment,
                              payment_id: str) -> Optional[str]:
    """Finish a running job whose on-chain state says it must not run again. Returns the `recover_jobs` count it
    falls under, or None when the job stays with the deadline / monitoring path. Never runs or submits anything."""
    from .agent import stored_result  # local import: agent imports this module's dependencies
    from .job_identity import identity_for_job
    from .ledger import get_ledger

    manager = server.job_manager
    job_id = job["job_id"]
    state, action, record = await _payment_state(payment, payment_id)

    async def stored() -> Optional[str]:
        created = await manager.storage.created_at(job_id) if hasattr(manager.storage, "created_at") else None
        identity = identity_for_job({**job, "job_id": job_id}, getattr(server, "tier", "standard"), created)
        return await stored_result(get_ledger(), identity)

    if state in _REFUND_FINAL:
        await manager.set_job_failed(job_id, f"Payment refunded while the agent was offline ({state})")
        return "refunded"
    if state == _WITHDRAW_AUTHORIZED:
        result = await stored()
        if result is not None and _hash_matches(result, job["identifier_from_purchaser"], record.get("resultHash")):
            await manager.storage.update_job(job_id, {"status": JobStatus.COMPLETED.value, "result": result})
            return "completed"
        why = "no stored result" if result is None else "stored result does not match the on-chain result hash"
        logger.warning("Job %s needs manual reconciliation: %s (%s)", job_id, why, state)
        await manager.set_job_failed(job_id, f"Manual reconciliation needed: withdrawal authorized on-chain but {why}")
        return "reconcile"
    if state in _REFUNDED and action not in _SUBMIT_IN_PROGRESS:
        await manager.set_job_failed(job_id, f"Payment refunded while the agent was offline ({state})")
        return "refunded"
    if state in _RESULT_ON_CHAIN or action in _SUBMIT_IN_PROGRESS:
        # Result on-chain, or its submission already handed to the Payment Service: never
        # run or submit again; the Payment Service finishes (or retries) the submission.
        result = await stored()
        if result is not None:
            await manager.storage.update_job(job_id, {"status": JobStatus.COMPLETED.value, "result": result})
        else:
            await manager.set_job_failed(job_id, "Result was submitted before the restart but is not stored locally")
        return "completed"
    if state is None:
        return _UNVERIFIED
    if state not in _KNOWN_STATES:
        logger.warning("Job %s: unknown on-chain state %r; not settled, handled as before", job_id,
                       sanitize(state, 40))
    return None
