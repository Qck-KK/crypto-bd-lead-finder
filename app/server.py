"""Masumi server with payment acceptance, health-aware availability and admin stats.

Payment acceptance runs when the Payment Service reports FundsLocked, before any provider is called.
The job only runs if the payment is for this agent, in the expected token, at least the expected
amount, for this job's input hash. Otherwise the job is marked failed and nothing is delivered (the
buyer can reclaim the escrow after the result deadline). A status lookup that keeps failing is
treated the same way: no proof of payment, no work.
"""

import asyncio
import hmac
import logging
import os
import time
from typing import Any, Optional

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from masumi import MasumiAgentServer

from . import feedback
from .ledger import get_ledger
from .pricing import TUSDM_DECIMALS, TUSDM_UNIT, estimate

logger = logging.getLogger(__name__)

# Mainnet USDM (policy id + asset name), from the Masumi registration docs.
USDM_MAINNET_UNIT = "c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d"
STATUS_RETRIES = 3


def expected_payment(network: str, tier: str) -> tuple[str, int]:
    unit = os.getenv("EXPECTED_PAYMENT_UNIT") or (TUSDM_UNIT if network == "Preprod" else USDM_MAINNET_UNIT)
    raw = os.getenv("EXPECTED_PAYMENT_ATOMIC")
    amount = int(raw) if raw else round(estimate(tier)["price_tusdm"] * 10**TUSDM_DECIMALS)
    return unit, amount


def check_payment(payment: dict, *, job: dict, agent_identifier: str, payment_id: str, unit: str,
                  min_amount: int) -> list[str]:
    """Problems with a payment record from /payment/resolve-blockchain-identifier; [] means accept."""
    problems = []
    if payment.get("blockchainIdentifier") not in (None, payment_id):
        problems.append("payment id mismatch")
    if payment.get("onChainState") != "FundsLocked":
        problems.append(f"funds not locked (state {payment.get('onChainState')})")
    if payment.get("agentIdentifier") != agent_identifier:
        problems.append("payment is for another agent")
    if job.get("input_hash") and payment.get("inputHash") and payment["inputHash"] != job["input_hash"]:
        problems.append("input hash mismatch")
    funds = payment.get("RequestedFunds") or payment.get("PaidFunds") or []
    paid = sum(int(f.get("amount", 0)) for f in funds if isinstance(f, dict) and f.get("unit") == unit)
    if paid < min_amount:
        problems.append(f"amount {paid} below expected {min_amount} in the expected token")
    return problems


class ReliableAgentServer(MasumiAgentServer):
    def __init__(self, *args, tier: str = "standard", **kwargs):
        super().__init__(*args, **kwargs)
        self.tier = tier
        self.counters = {"payments_accepted": 0, "payments_rejected": 0}

    async def _handle_payment_confirmed(self, job_id: str, payment_id: str):
        job = await self.job_manager.get_job(job_id)
        payment = self.job_manager.get_payment_instance(job_id)
        unit, amount = expected_payment(self.network, self.tier)
        record: Optional[dict] = None
        for attempt in range(STATUS_RETRIES):
            try:
                record = (await payment.check_payment_status_by_identifier(payment_id)).get("data") or {}
                break
            except Exception as e:
                logger.warning("Payment lookup failed (attempt %d): %s", attempt + 1, type(e).__name__)
                await asyncio.sleep(2 ** attempt)
        problems = (["payment status could not be confirmed"] if record is None else
                    check_payment(record, job=job or {}, agent_identifier=self.agent_identifier,
                                  payment_id=payment_id, unit=unit, min_amount=amount))
        if problems:
            self.counters["payments_rejected"] += 1
            logger.error("Payment rejected for job %s: %s", job_id, "; ".join(problems))
            await self.job_manager.set_job_failed(job_id, "payment rejected: " + "; ".join(problems))
            await self.job_manager.cleanup_payment_instance(job_id)
            return
        self.counters["payments_accepted"] += 1
        await super()._handle_payment_confirmed(job_id, payment_id)


class Health:
    """Cached dependency checks for /availability (the registry hides unavailable agents)."""

    def __init__(self, payment_service_url: str, ttl_s: float = 30.0):
        self.url, self.ttl = payment_service_url.rstrip("/"), ttl_s
        self._at, self._state = 0.0, ("available", "ok")

    async def check(self) -> tuple[str, str]:
        if time.monotonic() - self._at < self.ttl:
            return self._state
        problems = []
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
                async with s.get(f"{self.url}/health/") as r:
                    if r.status != 200:
                        problems.append("payment service unhealthy")
        except Exception:
            problems.append("payment service unreachable")
        ledger = get_ledger()
        if hasattr(ledger, "ping"):
            try:
                await ledger.ping()
            except Exception:
                problems.append("database unreachable")
        self._state = ("unavailable", "; ".join(problems)) if problems else ("available", "ok")
        self._at = time.monotonic()
        return self._state


def add_sale_guard(app: FastAPI, reason: Optional[str]) -> None:
    """Refuse new jobs (/start_job: 503) while the agent must not be sold. /availability reports the same
    reason, so the registry hides the agent; paid jobs already in progress are not affected."""
    if not reason:
        return
    logger.warning("Not accepting new jobs: %s", reason)

    @app.middleware("http")
    async def sale_guard(request: Request, call_next):
        if request.method == "POST" and request.url.path.rstrip("/") == "/start_job":
            return JSONResponse(status_code=503, content={"detail": f"Not accepting jobs: {reason}"})
        return await call_next(request)


def _admin_ok(request: Request, token: str) -> bool:
    return bool(token) and hmac.compare_digest(request.headers.get("x-admin-token", ""), token)


def add_admin_routes(app: FastAPI, server: ReliableAgentServer, settings: Any, admin_token: str) -> None:
    async def snapshot() -> dict:
        ledger = get_ledger()
        jobs = await server.job_manager.list_jobs()
        by_status: dict[str, int] = {}
        for j in jobs:
            by_status[j.get("status", "?")] = by_status.get(j.get("status", "?"), 0) + 1
        try:
            fb = await feedback.get_store().summary()
        except Exception:
            fb = None
        # spend_today covers every agent on this database: they share one treg balance and one cap.
        return {"tier": settings.tier, "jobs_by_status": by_status, "payments": server.counters,
                "spend_today_usd": round(await ledger.spend_today(), 4),
                "daily_spend_cap_usd": settings.daily_spend_cap_usd,
                "ledger": await ledger.stats(7, tier=settings.tier),
                "feedback": fb}

    @app.get("/admin/stats", include_in_schema=False)
    async def admin_stats(request: Request):
        if not _admin_ok(request, admin_token):
            raise HTTPException(status_code=403, detail="forbidden")
        return await snapshot()

    @app.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
    async def metrics(request: Request):
        if not _admin_ok(request, admin_token):
            raise HTTPException(status_code=403, detail="forbidden")
        s = await snapshot()
        t = s["ledger"]["totals"]
        lines = [f'bdlf_jobs_by_status{{tier="{s["tier"]}",status="{k}"}} {v}' for k, v in s["jobs_by_status"].items()]
        lines += [f'bdlf_payments_total{{tier="{s["tier"]}",result="{k.split("_")[1]}"}} {v}'
                  for k, v in s["payments"].items()]
        lines += [f'bdlf_spend_today_usd{{tier="{s["tier"]}"}} {s["spend_today_usd"]}',
                  f'bdlf_daily_spend_cap_usd{{tier="{s["tier"]}"}} {s["daily_spend_cap_usd"]}',
                  f'bdlf_jobs_7d{{tier="{s["tier"]}"}} {t["jobs"]}',
                  f'bdlf_cost_7d_usd{{tier="{s["tier"]}"}} {t["cost_usd"]}',
                  f'bdlf_revenue_7d_tusdm{{tier="{s["tier"]}"}} {t["revenue_tusdm"]}']
        return "\n".join(lines) + "\n"
