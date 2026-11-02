"""Job identity (BL-36): tier and the verified payment id separate orders; the SDK job context carries the payment
id into the handler on the payment path and on recovery; the pre-v2 result fallback is tightly restricted."""

import asyncio
import dataclasses
import itertools
import json
import os
import time

import httpx
import masumi.server
import pytest
import respx
from masumi import Config, InMemoryJobStorage
from masumi.hitl import clear_job_context, set_job_context

from app import drafts as drafts_module
from app.agent import INPUT_SCHEMA, process_job, stored_result
from app.job_identity import JobIdentityError, current_identity, identity_for_job, job_tag, legacy_tag
from app.job_store import recover_jobs
from app.server import ReliableAgentServer
from conftest import FakeGemini

APOLLO_URL = "https://treg.test/call/apollo.people.search"
ENRICH_URL = "https://treg.test/call/apollo.people.bulk_enrich"
ROUTED_URL = "https://treg.test/call/treg.people.search"
INPUT = {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"}
BUYER = "abcdef1234567890"
FREE = {"X-Treg-Cost-Micro": "0"}

requires_pg = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))


def _apollo(request):
    if json.loads(request.content)["q_organization_domains_list"] != ["acme.io"]:
        return httpx.Response(200, json={"people": []}, headers=FREE)
    return httpx.Response(200, json={"people": [
        {"id": f"p{i}", "first_name": n, "last_name_obfuscated": "S***h", "title": "Head of BD",
         "organization": {"name": "Acme Labs"}} for i, n in enumerate(["Jane", "Ann", "Max", "Ola", "Kim"])]},
        headers=FREE)


def _job(job_id, payment_id, agent="test-agent", identifier=BUYER, input_data=INPUT):
    future = int((time.time() + 3600) * 1000)
    return {"job_id": job_id, "status": "running", "payment_id": payment_id, "identifier_from_purchaser": identifier,
            "input_data": input_data, "pay_by_time": future, "submit_result_time": future, "agent_identifier": agent,
            "seller_vkey": "v", "input_hash": "h", "result": None, "error": None}


class _Storage(InMemoryJobStorage):
    """In-memory job storage that, like PostgresJobStorage, knows when each order's row was created."""

    def __init__(self, created=None):
        super().__init__()
        self.created = created or {}

    async def created_at(self, job_id):
        return self.created.get(job_id)


class _Manager:
    def __init__(self, storage):
        self.storage = storage

    async def get_job(self, job_id):
        return await self.storage.get_job(job_id)


async def _in_job(manager, job_id, coro_fn):
    """Runs the handler the way masumi's _execute_agent_job does: inside the SDK job context."""
    set_job_context(job_id, manager)
    try:
        return await coro_fn()
    finally:
        clear_job_context()


# --- the tag ----------------------------------------------------------------------------------------------------

def test_tag_separates_tier_payment_and_agent_and_is_stable_per_order():
    base = job_tag("standard", "agent-s", "pay-1", BUYER, INPUT)
    assert base == job_tag("standard", "agent-s", "pay-1", BUYER, dict(reversed(list(INPUT.items()))))
    assert len({base, job_tag("pro", "agent-s", "pay-1", BUYER, INPUT), job_tag("standard", "agent-s", "pay-2", BUYER,
                INPUT), job_tag("standard", "agent-p", "pay-1", BUYER, INPUT), legacy_tag(BUYER, INPUT)}) == 5


async def test_without_a_job_context_the_identity_is_unpaid():
    identity = await current_identity(BUYER, INPUT, "standard")
    assert identity.payment_id is None and identity.tag == job_tag("standard", "", None, BUYER, INPUT)


async def test_a_job_context_that_does_not_match_or_lacks_a_payment_fails_closed(settings, _fresh_ledger):
    storage = _Storage()
    await storage.create_job("j1", _job("j1", "pay-1", input_data={**INPUT, "company": "Other"}))
    await storage.create_job("j2", _job("j2", ""))
    with pytest.raises(JobIdentityError, match="does not match"):
        await _in_job(_Manager(storage), "j1", lambda: process_job(BUYER, INPUT, settings))
    with pytest.raises(JobIdentityError, match="no payment id"):
        await _in_job(_Manager(storage), "j2", lambda: process_job(BUYER, INPUT, settings))
    assert not _fresh_ledger.attempts  # nothing reserved, nothing spent


# --- Standard and Pro never share -------------------------------------------------------------------------------

@respx.mock
async def test_concurrent_standard_and_pro_orders_with_the_same_identifier_share_nothing(settings, gemini,
                                                                                        _fresh_ledger):
    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo)
    respx.post(url__startswith=ENRICH_URL).mock(return_value=httpx.Response(200, json={"matches": []}, headers=FREE))
    storage = _Storage()
    await storage.create_job("j-std", _job("j-std", "pay-std", agent="agent-std"))
    await storage.create_job("j-pro", _job("j-pro", "pay-pro", agent="agent-pro"))
    manager = _Manager(storage)
    standard = settings
    pro = dataclasses.replace(settings, tier="pro", enrich_budget_usd=0.3)

    std_result, pro_result = await asyncio.gather(
        _in_job(manager, "j-std", lambda: process_job(BUYER, INPUT, standard)),
        _in_job(manager, "j-pro", lambda: process_job(BUYER, INPUT, pro)))
    assert std_result.startswith("# BD leads") and not std_result.startswith("# BD leads (Pro)")
    assert pro_result.startswith("# BD leads (Pro)")
    std_tag = identity_for_job(await storage.get_job("j-std"), "standard").tag
    pro_tag = identity_for_job(await storage.get_job("j-pro"), "pro").tag
    assert std_tag != pro_tag and set(_fresh_ledger.results) == {std_tag, pro_tag}
    assert _fresh_ledger.results[pro_tag]["tier"] == "pro"
    assert {a["job_tag"] for a in _fresh_ledger.attempts.values()} == {std_tag, pro_tag}
    keys = {c.request.headers["Idempotency-Key"] for c in apollo.calls}
    assert len(keys) == len(apollo.calls)  # no treg key shared between the two orders

    # A later Pro order with the same identifier and input runs fresh; the Standard order's retry is cached.
    await storage.create_job("j-pro2", _job("j-pro2", "pay-pro2", agent="agent-pro"))
    calls = apollo.call_count
    assert (await _in_job(manager, "j-pro2", lambda: process_job(BUYER, INPUT, pro))).startswith("# BD leads (Pro)")
    assert apollo.call_count > calls
    calls = apollo.call_count
    assert await _in_job(manager, "j-std", lambda: process_job(BUYER, INPUT, standard)) == std_result
    assert apollo.call_count == calls


# --- the real SDK paths -----------------------------------------------------------------------------------------

def _server(storage=None):
    return ReliableAgentServer(
        config=Config(payment_service_url="https://payment.test/api/v1", payment_api_key="k"),
        agent_identifier="test-agent", network="Preprod", job_storage=storage or InMemoryJobStorage(),
        start_job_handler=process_job, input_schema_handler=INPUT_SCHEMA, seller_vkey="test-vkey")


async def _wait(server, job_id):
    for _ in range(200):
        job = await server.job_manager.get_job(job_id)
        if job["status"] in ("completed", "failed"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


@respx.mock(assert_all_called=False)
async def test_the_sdk_payment_path_hands_the_verified_payment_id_to_the_handler(env, payment_mock, gemini,
                                                                                 respx_mock, monkeypatch,
                                                                                 _fresh_ledger):
    respx_mock.route(host="a").pass_through()
    respx_mock.post(APOLLO_URL).mock(side_effect=_apollo)
    counter = itertools.count(1)

    async def unique_payment_request(self, metadata=None):
        n = next(counter)
        return {"data": {"blockchainIdentifier": f"chain-{n}", "payByTime": "1700000000000",
                         "submitResultTime": "1700000100000", "unlockTime": "1700000200000",
                         "externalDisputeUnlockTime": "1700000300000", "sellerVKey": "test-vkey"}}

    monkeypatch.setattr(masumi.server.Payment, "create_payment_request", unique_payment_request)
    server = _server()
    ids = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.get_app()), base_url="http://a") as c:
        for _ in range(2):  # two orders, same buyer-chosen identifier and input
            r = await c.post("/start_job", json={"identifier_from_purchaser": BUYER, "input_data": INPUT})
            ids.append(r.json()["id"])
    for n in (1, 2):
        await payment_mock["callbacks"][f"chain-{n}"]({"blockchainIdentifier": f"chain-{n}"})
    jobs = [await _wait(server, job_id) for job_id in ids]
    assert all(j["status"] == "completed" for j in jobs) and {j["payment_id"] for j in jobs} == {"chain-1", "chain-2"}
    tags = {identity_for_job(j, "standard").tag for j in jobs}
    assert len(tags) == 2 and tags == set(_fresh_ledger.results)  # each order under its own verified identity


@respx.mock(assert_all_called=False)
async def test_a_rejected_payment_never_reaches_the_handler(env, payment_mock, gemini, respx_mock, _fresh_ledger):
    respx_mock.route(host="a").pass_through()
    apollo = respx_mock.post(APOLLO_URL).mock(side_effect=_apollo)
    payment_mock["record"] = {**payment_mock["record"], "agentIdentifier": "someone-else"}
    server = _server()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.get_app()), base_url="http://a") as c:
        job_id = (await c.post("/start_job", json={"identifier_from_purchaser": BUYER, "input_data": INPUT})).json()["id"]
    await payment_mock["callback"]({"blockchainIdentifier": "block_chain_id_123"})
    assert (await server.job_manager.get_job(job_id))["status"] == "failed"
    assert not apollo.called and not _fresh_ledger.attempts


@respx.mock(assert_all_called=False)
async def test_recovery_reruns_with_the_same_identity_and_returns_the_stored_result(env, payment_mock, gemini,
                                                                                    respx_mock, _fresh_ledger):
    respx_mock.route(host="a").pass_through()
    apollo = respx_mock.post(APOLLO_URL).mock(side_effect=_apollo)
    from app.masumi_compat import V2Payment

    storage = InMemoryJobStorage()
    job = _job("j1", "pay-j1")
    job["input_hash"] = V2Payment(agent_identifier="test-agent", config=Config(payment_service_url="https://p.test",
                                  payment_api_key="k"), identifier_from_purchaser=BUYER, input_data=INPUT,
                                  network="Preprod").input_hash  # what the SDK stored at /start_job
    await storage.create_job("j1", job)
    await _fresh_ledger.put_result(identity_for_job(job, "standard").tag, "standard", "STORED BEFORE THE CRASH")
    payment_mock["record"] = {**payment_mock["record"], "NextAction": {"requestedAction": "WaitingForExternalAction"}}
    server = _server(storage)
    assert (await recover_jobs(server))["resumed"] == 1
    await payment_mock["callbacks"]["pay-j1"]({"blockchainIdentifier": "pay-j1"})
    done = await _wait(server, "j1")
    assert done["status"] == "completed" and done["result"] == "STORED BEFORE THE CRASH" and not apollo.called


# --- pre-v2 results: same tier, order and row from before v2 only ------------------------------------------------

async def test_the_legacy_fallback_is_restricted(_fresh_ledger):
    since = _fresh_ledger.v2_since = time.time()
    old = legacy_tag(BUYER, INPUT)
    _fresh_ledger.results[old] = {"tier": "standard", "result": "OLD", "at": since - 50}

    def ident(tier="standard", created=since - 100, payment="pay-1"):
        return identity_for_job(_job("j", payment), tier, created)

    assert await stored_result(_fresh_ledger, ident()) == "OLD"  # pre-v2 order, same tier, stored after it
    assert await stored_result(_fresh_ledger, ident(tier="pro")) is None  # never across tiers
    assert await stored_result(_fresh_ledger, ident(created=since + 10)) is None  # a v2 order never falls back
    assert await stored_result(_fresh_ledger, ident(created=since - 10)) is None  # stored before this order existed
    assert await stored_result(_fresh_ledger, ident(created=None)) is None  # creation time unknown
    unpaid = await current_identity(BUYER, INPUT, "standard")
    assert await stored_result(_fresh_ledger, unpaid) is None
    _fresh_ledger.results[old]["at"] = since + 5  # stored after the upgrade: not a pre-v2 result
    assert await stored_result(_fresh_ledger, ident()) is None


@respx.mock
async def test_a_pre_v2_order_in_flight_gets_its_legacy_result_without_paying_again(settings, gemini,
                                                                                   _fresh_ledger):
    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo)
    since = _fresh_ledger.v2_since = time.time()
    _fresh_ledger.results[legacy_tag(BUYER, INPUT)] = {"tier": "standard", "result": "OLD", "at": since - 50}
    storage = _Storage(created={"j-old": since - 100})
    await storage.create_job("j-old", _job("j-old", "pay-old"))
    assert await _in_job(_Manager(storage), "j-old", lambda: process_job(BUYER, INPUT, settings)) == "OLD"
    assert not apollo.called and not _fresh_ledger.attempts


@requires_pg
async def test_postgres_legacy_fallback_uses_the_upgrade_time_and_tier():
    from app.ledger import PostgresLedger

    led = PostgresLedger(os.environ["TEST_DATABASE_URL"])
    try:
        pool = await led._p()
        since = await led.identity_v2_since()
        old = "test-bl36-" + legacy_tag(BUYER, INPUT)[:20]
        await pool.execute("DELETE FROM job_results WHERE tag LIKE 'test-bl36-%'")
        await pool.execute("INSERT INTO job_results (tag, tier, result, created_at) VALUES ($1, 'standard', 'OLD', "
                           "to_timestamp($2))", old, since - 50)
        assert await led.get_legacy_result(old, "standard", since - 100) == "OLD"
        assert await led.get_legacy_result(old, "pro", since - 100) is None
        assert await led.get_legacy_result(old, "standard", since + 10) is None
        assert await led.get_legacy_result(old, "standard", since - 10) is None
        await pool.execute("UPDATE job_results SET created_at = to_timestamp($2) WHERE tag = $1", old, since + 5)
        assert await led.get_legacy_result(old, "standard", since - 100) is None
    finally:
        pool = await led._p()
        await pool.execute("DELETE FROM job_results WHERE tag LIKE 'test-bl36-%'")
        await led.close()


# --- stopping new jobs (rollback runbook) -------------------------------------------------------------------------

async def test_pause_new_jobs_stops_sales_before_any_payment_request(env, payment_mock, monkeypatch):
    monkeypatch.setenv("PAUSE_NEW_JOBS", "true")
    from main import create_app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://agent") as c:
        av = (await c.get("/availability")).json()
        r = await c.post("/start_job", json={"identifier_from_purchaser": BUYER, "input_data": INPUT})
    assert av["status"] == "unavailable" and "PAUSE_NEW_JOBS" in av["message"]
    assert r.status_code == 503 and payment_mock["callback"] is None
