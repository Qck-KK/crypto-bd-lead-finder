"""Job identity (BL-36): which order a run of the agent belongs to.

tag = sha256("v2|agent identifier|tier|payment id|purchaser identifier|input")[:32]

It keys the result cache, the spend attempts, the treg idempotency keys and the feedback reference, so two
orders never share any of them: not two payments with the same purchaser identifier and input, and never a
Standard and a Pro order. A retry of the same payment (crash recovery) gets the same tag, so it returns the
stored result (same on-chain hash) and treg replays its calls at no cost.

The payment id is the Payment Service's blockchain identifier of this order, read from the SDK's job record,
not from the buyer: the SDK stores it when it creates the payment request (`job_manager.create_job`,
`payment_id = blockchainIdentifier`) and runs the handler only after `ReliableAgentServer._handle_payment_confirmed`
has checked the Payment Service record against it (`check_payment`: same blockchain identifier, agent, input
hash, amount). The SDK puts the job id in its job context right before calling the handler, on the normal path
and on recovery (`_execute_agent_job` -> `set_job_context`), see `masumi_compat.current_job`.

Without a job context (standalone CLI, unit tests) the payment id is "unpaid".
"""

import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class JobIdentity:
    tag: str
    legacy_tag: str           # pre-v2 tag (purchaser identifier + input), for results stored before the upgrade
    tier: str
    payment_id: Optional[str]
    job_id: Optional[str] = None
    job_created_at: Optional[float] = None  # when the order's job row was created (PostgreSQL job storage only)


def legacy_tag(identifier_from_purchaser: str, input_data: dict) -> str:
    seed = identifier_from_purchaser + "|" + repr(sorted(input_data.items()))
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def job_tag(tier: str, agent_identifier: str, payment_id: Optional[str], identifier_from_purchaser: str,
            input_data: dict) -> str:
    seed = "|".join(["v2", agent_identifier or "", tier, payment_id or "unpaid", identifier_from_purchaser,
                     repr(sorted(input_data.items()))])
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def identity_for_job(job: dict, tier: str, job_created_at: Optional[float] = None) -> JobIdentity:
    """Identity of a stored SDK job record (used by the handler and by recovery)."""
    identifier, input_data = job.get("identifier_from_purchaser") or "", job.get("input_data") or {}
    payment_id = job.get("payment_id")
    return JobIdentity(tag=job_tag(tier, job.get("agent_identifier") or "", payment_id, identifier, input_data),
                       legacy_tag=legacy_tag(identifier, input_data), tier=tier, payment_id=payment_id,
                       job_id=job.get("job_id"), job_created_at=job_created_at)


class JobIdentityError(RuntimeError):
    """The SDK job context does not describe a paid order matching this call; the job must not run."""


async def current_identity(identifier_from_purchaser: str, input_data: dict, tier: str) -> JobIdentity:
    from .masumi_compat import current_job

    found = await current_job()
    if found is None:
        return JobIdentity(tag=job_tag(tier, "", None, identifier_from_purchaser, input_data),
                           legacy_tag=legacy_tag(identifier_from_purchaser, input_data), tier=tier, payment_id=None)
    job, created_at = found
    if not job.get("payment_id"):
        raise JobIdentityError("job record has no payment id")
    if (job.get("identifier_from_purchaser") or "") != identifier_from_purchaser or \
            (job.get("input_data") or {}) != input_data:
        raise JobIdentityError("job context does not match the job being run")
    return identity_for_job(job, tier, created_at)
