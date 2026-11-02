"""Preflight B1 (expired unpaid orders free their slot), BL-33 all-or-nothing budgets on the payment path, and the
provisional rate-limit defaults (preflight D1)."""

import asyncio
import dataclasses
import json
import time
from types import SimpleNamespace

import httpx
import masumi.server
import pytest
import respx
from masumi import Config, InMemoryJobStorage

from app import drafts as drafts_module
from app.agent import INPUT_SCHEMA, _spend_ceiling, process_job
from app.job_gate import StartJobGate
from app.job_store import recover_jobs
from app.ledger import MemoryLedger
from app.server import ReliableAgentServer
from app.settings import Settings
from app.spend import BudgetUnavailable
from conftest import FakeGemini

APOLLO_URL = "https://treg.test/call/apollo.people.search"
ROUTED_URL = "https://treg.test/call/treg.people.search"
INPUT = {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"}
START = {"identifier_from_purchaser": "abcdef1234567890", "input_data": {"company": "Example Labs"}}
FREE = {"X-Treg-Cost-Micro": "0"}
T = 1_800_000_000.0  # a fixed wall-clock second


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))


def _apollo(request):
    if json.loads(request.content)["q_organization_domains_list"] != ["acme.io"]:
        return httpx.Response(200, json={"people": []}, headers=FREE)
    return httpx.Response(200, json={"people": [
        {"id": "p1", "first_name": "Jane", "last_name_obfuscated": "S***h", "title": "Head of BD",
         "organization": {"name": "Acme Labs"}}]}, headers=FREE)


class _Jobs:
    def __init__(self, pay_bys):
        self.jobs = [{"status": "awaiting_payment", "pay_by_time": p} for p in pay_bys]

    async def list_jobs(self, status=None):
        await asyncio.sleep(0)
        return [j for j in self.jobs if status is None or j["status"] == status]


def _gate(settings, pay_bys, limit, wall=lambda: T):
    s = dataclasses.replace(settings, max_awaiting_payment=limit, start_job_per_client_per_hour=-1,
                            start_job_per_hour=-1)
    return StartJobGate(s, server=SimpleNamespace(job_manager=_Jobs(pay_bys)), clock=time.monotonic, wall=wall)


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={})


# --- B1: only orders whose Payment Service pay-by time has not passed hold a slot ------------------------------

async def test_expired_unpaid_orders_do_not_hold_the_30_slots(settings):
    expired = [int((T - 1) * 1000)] * 30
    assert await _gate(settings, expired, 30).check(_request()) is None
    pending = [int((T + 3600) * 1000)] * 30
    refusal = await _gate(settings, pending, 30).check(_request())
    assert refusal.status_code == 429 and "unpaid" in json.loads(refusal.body)["detail"]


@pytest.mark.parametrize("pay_by,counts", [
    (int(T * 1000), False),           # exactly now: passed
    (int((T - 0.001) * 1000), False),
    (int((T + 1) * 1000), True),      # one second ahead: still payable
    (T + 60, True),                   # seconds instead of milliseconds
    (T - 60, False),
    (str(int((T + 60) * 1000)), True),  # as the Payment Service string
    (None, True), ("not a time", True),  # unreadable: counts (fail closed)
    (True, True), (float("nan"), True), (float("inf"), True), (0, True), (-5, True),  # not a time: counts
])
async def test_pay_by_boundary(settings, pay_by, counts):
    gate = _gate(settings, [pay_by], 1)
    assert await gate.unpaid_jobs() == (1 if counts else 0)
    assert (await gate.check(_request()) is None) is (not counts)


async def test_a_cancelled_admission_gives_its_rate_slots_back(settings):
    gate = _gate(settings, [], 30)
    gate.settings = dataclasses.replace(gate.settings, start_job_per_client_per_hour=1, start_job_per_hour=1)
    gate.total.limit = 1
    async with gate._admission:  # another request is being admitted
        waiting = asyncio.create_task(gate.check(_request()))
        await asyncio.sleep(0.01)
        waiting.cancel()  # the client goes away while waiting
        with pytest.raises(asyncio.CancelledError):
            await waiting
    assert gate.total.empty() and all(w.empty() for w in gate.clients.values()) and gate.admitting == 0
    assert await gate.check(_request()) is None  # the slot is free again


async def test_an_order_frees_its_slot_when_its_pay_by_passes(settings):
    now = [T]
    gate = _gate(settings, [int((T + 600) * 1000)], 1, wall=lambda: now[0])
    assert (await gate.check(_request())).status_code == 429
    now[0] = T + 600
    assert await gate.check(_request()) is None


def _payment_request(monkeypatch, delay=None, fail=False, pay_by_s=12 * 3600):
    async def create(self, metadata=None):
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("payment service down")
        ms = int((time.time() + pay_by_s) * 1000)
        return {"data": {"blockchainIdentifier": f"chain-{time.time_ns()}", "payByTime": str(ms),
                         "submitResultTime": str(ms + 1), "unlockTime": str(ms + 2),
                         "externalDisputeUnlockTime": str(ms + 3), "sellerVKey": "test-vkey"}}

    monkeypatch.setattr(masumi.server.Payment, "create_payment_request", create)


async def _app(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ADMIN_TOKEN", "adm-1")
    from main import create_app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(job_ledger=MemoryLedger())),
                             base_url="http://agent")


async def test_concurrent_order_creation_cannot_overshoot_the_unpaid_limit(env, payment_mock, monkeypatch):
    _payment_request(monkeypatch, delay=0.05)  # the SDK is still creating the first orders when the others arrive
    async with await _app(monkeypatch, MAX_AWAITING_PAYMENT="2") as c:
        answers = await asyncio.gather(*[c.post("/start_job", json=START) for _ in range(5)])
        stats = (await c.get("/admin/stats", headers={"x-admin-token": "adm-1"})).json()
    assert sorted(a.status_code for a in answers) == [200, 200, 429, 429, 429]
    assert stats["start_job_gate"]["accepted"] == 2 and stats["start_job_gate"]["unpaid_limit"] == 3


async def test_a_failed_order_creation_gives_its_admission_back(env, payment_mock, monkeypatch):
    async with await _app(monkeypatch, MAX_AWAITING_PAYMENT="1") as c:
        _payment_request(monkeypatch, fail=True)
        assert (await c.post("/start_job", json=START)).status_code >= 400  # no order was stored
        _payment_request(monkeypatch)
        assert (await c.post("/start_job", json=START)).status_code == 200  # the slot was given back
        assert (await c.post("/start_job", json=START)).status_code == 429  # now one order awaits payment


async def test_orders_past_their_pay_by_do_not_block_new_ones(env, payment_mock, monkeypatch):
    _payment_request(monkeypatch, pay_by_s=-60)  # the Payment Service's pay-by time is already over
    async with await _app(monkeypatch, MAX_AWAITING_PAYMENT="2") as c:
        codes = [(await c.post("/start_job", json=START)).status_code for _ in range(5)]
    assert codes == [200] * 5  # expired unpaid orders stay awaiting_payment in storage but hold no slot


# --- D1: provisional limits, the same for both agents -----------------------------------------------------------

@pytest.mark.parametrize("tier", ["standard", "pro"])
def test_both_agents_default_to_30_per_client_and_30_total(monkeypatch, tier):
    for k in ("START_JOB_PER_CLIENT_PER_HOUR", "START_JOB_PER_HOUR", "MAX_AWAITING_PAYMENT", "RATE_LIMIT_CLIENT_HEADER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENT_TIER", tier)
    s = Settings.from_env()
    assert (s.tier, s.start_job_per_client_per_hour, s.start_job_per_hour, s.max_awaiting_payment,
            s.rate_limit_client_header) == (tier, 30, 30, 30, "")
    monkeypatch.setenv("START_JOB_PER_CLIENT_PER_HOUR", "30")
    assert Settings.from_env().start_job_per_client_per_hour == 30


# --- BL-33: no partial budget, on the real payment path ----------------------------------------------------------

@respx.mock
async def test_concurrent_paid_jobs_never_run_on_part_of_a_budget(settings, gemini, _fresh_ledger):
    async def slow(request):
        await asyncio.sleep(0.02)
        return _apollo(request)

    apollo = respx.post(APOLLO_URL).mock(side_effect=slow)
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}, headers=FREE))
    ceiling = _spend_ceiling(settings, "Acme Labs", "acme.io")
    capped = dataclasses.replace(settings, daily_spend_cap_usd=1.5 * ceiling)  # one and a half jobs
    out = await asyncio.gather(*[process_job(f"buyer-{i}", INPUT, capped) for i in range(3)], return_exceptions=True)
    assert sum(isinstance(r, str) for r in out) == 1 and sum(isinstance(r, BudgetUnavailable) for r in out) == 2
    grants = sorted(a["reserved_usd"] for a in _fresh_ledger.attempts.values())
    assert grants == pytest.approx([0.0, 0.0, ceiling])  # never a partial grant
    assert apollo.call_count == 2  # the one funded job's probe and search; the others made no treg call


def _server(storage):
    return ReliableAgentServer(
        config=Config(payment_service_url="https://payment.test/api/v1", payment_api_key="k"),
        agent_identifier="test-agent", network="Preprod", job_storage=storage,
        start_job_handler=process_job, input_schema_handler=INPUT_SCHEMA, seller_vkey="test-vkey")


@respx.mock(assert_all_called=False)
async def test_a_paid_job_without_budget_fails_once_submits_nothing_and_is_not_rerun(env, payment_mock, gemini,
                                                                                     respx_mock, monkeypatch,
                                                                                     _fresh_ledger):
    respx_mock.route(host="a").pass_through()
    apollo = respx_mock.post(APOLLO_URL).mock(side_effect=_apollo)
    refunds = []

    async def authorize_refund(self, blockchain_identifier):
        refunds.append(blockchain_identifier)

    monkeypatch.setattr(masumi.server.Payment, "authorize_refund", authorize_refund)
    now = time.time()
    await _fresh_ledger.record_job({"tag": "spent", "tier": "standard", "company": "x", "started_at": now,
                                    "finished_at": now, "duration_ms": 1, "treg_usd": 0.9, "enrich_usd": 0.0,
                                    "leads": 1, "llm_used": True, "outcome": "delivered", "price_tusdm": 0.27})
    storage = InMemoryJobStorage()
    server = _server(storage)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.get_app()), base_url="http://a") as c:
        job_id = (await c.post("/start_job", json={"identifier_from_purchaser": "abcdef1234567890",
                                                   "input_data": INPUT})).json()["id"]
    await payment_mock["callback"]({"blockchainIdentifier": "block_chain_id_123"})  # paid; $0.10 left < $0.20
    for _ in range(100):
        job = await server.job_manager.get_job(job_id)
        if job["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)

    assert job["status"] == "failed" and "not run" in job["error"] and "$" not in job["error"]
    assert payment_mock["submitted"] is None  # no result hash on-chain: the escrow stays refundable
    assert not refunds  # the agent never moves funds itself, so it cannot refund twice
    assert not apollo.called and job_id not in server.job_manager._payment_instances  # no spend, no monitoring
    (attempt,) = _fresh_ledger.attempts.values()
    assert attempt["reserved_usd"] == 0 and attempt["status"] == "settled"

    for _ in range(2):  # restarts: a failed job is neither re-run nor resubmitted
        assert await recover_jobs(_server(storage)) == {"resumed": 0, "expired": 0, "completed": 0, "refunded": 0}
    assert (await storage.get_job(job_id))["status"] == "failed" and len(_fresh_ledger.attempts) == 1
