"""Idempotent re-runs, recovery, cost ledger and daily cap, payment acceptance, log redaction, admin stats."""

import asyncio
import dataclasses
import json
import logging
import time

import httpx
import pytest
import respx
from masumi import Config, InMemoryJobStorage

from app import drafts as drafts_module
from app import ledger as ledger_module
from app.agent import INPUT_SCHEMA, process_job
from app.job_identity import identity_for_job, job_tag
from app.job_store import recover_jobs
from app.logs import RedactFilter, job_tag_var
from app.pricing import TUSDM_UNIT
from app.server import ReliableAgentServer, check_payment
from app.spend import BudgetUnavailable
from app.treg_client import TregError
from conftest import CHAIN_ID, FakeGemini

APOLLO_URL = "https://treg.test/call/apollo.people.search"
ROUTED_URL = "https://treg.test/call/treg.people.search"
INPUT = {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"}
FREE = {"X-Treg-Cost-Micro": "0"}  # Apollo's search is free: treg reports a zero cost


def _tag(buyer, input_data=INPUT):
    """Tag of a job run outside the payment flow (no SDK job context)."""
    return job_tag("standard", "", None, buyer, input_data)


def _apollo(request):
    body = json.loads(request.content)
    if body["q_organization_domains_list"] != ["acme.io"]:
        return httpx.Response(200, json={"people": []}, headers=FREE)
    return httpx.Response(200, json={"people": [
        {"id": "p1", "first_name": "Jane", "last_name_obfuscated": "S***h", "title": "Head of BD",
         "organization": {"name": "Acme Labs"}}]}, headers=FREE)


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))


# --- idempotency and ledger -----------------------------------------------------

@respx.mock
async def test_rerun_returns_stored_result_without_calling_providers(settings, gemini, _fresh_ledger):
    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo)
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}},
                                                                     headers={"X-Treg-Cost-Micro": "20000"}))
    first = await process_job("buyer-1", INPUT, settings)
    calls = apollo.call_count + routed.call_count
    second = await process_job("buyer-1", INPUT, settings)
    assert second == first
    assert apollo.call_count + routed.call_count == calls  # nothing paid or fetched again

    row = _fresh_ledger.jobs[_tag("buyer-1")]
    assert row["outcome"] == "delivered" and row["leads"] == 1 and row["treg_usd"] == pytest.approx(0.02)
    assert row["price_tusdm"] == 0.27 and row["company"] == "Acme Labs"
    assert await _fresh_ledger.spend_today() == pytest.approx(0.02)

    other = await process_job("buyer-2", INPUT, settings)  # another purchaser runs fresh
    assert apollo.call_count + routed.call_count > calls and other


@respx.mock
async def test_failed_job_is_recorded_and_not_cached(settings, gemini, _fresh_ledger):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(500, json={}))
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(402, json={"error": "x"}, headers={"X-Treg-Error": "1"}))
    with pytest.raises(Exception):
        await process_job("buyer-f", INPUT, settings)
    tag = _tag("buyer-f")
    assert _fresh_ledger.jobs[tag]["outcome"] == "failed"
    assert await _fresh_ledger.get_result(tag) is None


@respx.mock
async def test_daily_cap_reached_makes_no_treg_call_at_all(settings, gemini, _fresh_ledger):
    # BL-13: Apollo and the domain probes are metered too, so a used-up cap stops every treg call and the job
    # fails (refundable) instead of delivering an empty result.
    now = time.time()
    await _fresh_ledger.record_job({"tag": "old", "tier": "standard", "company": "x", "started_at": now,
                                    "finished_at": now, "duration_ms": 1, "treg_usd": 0.9, "enrich_usd": 0.1,
                                    "leads": 1, "llm_used": True, "outcome": "delivered", "price_tusdm": 0.27})
    apollo = respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}, headers=FREE))
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    with pytest.raises(BudgetUnavailable, match="data budget"):
        await process_job("buyer-cap", INPUT, dataclasses.replace(settings, daily_spend_cap_usd=1.0))
    assert not apollo.called and not routed.called
    row = _fresh_ledger.jobs[_tag("buyer-cap")]
    assert row["outcome"] == "failed" and row["treg_usd"] == 0 and not [a for a in _fresh_ledger.attempts.values() if a["status"] == "open"]


@respx.mock
async def test_a_job_never_runs_on_part_of_its_budget(settings, gemini, _fresh_ledger):
    """BL-33 (owner decision): part of the budget left = no treg call, the job fails and stays refundable."""
    now = time.time()
    await _fresh_ledger.record_job({"tag": "old", "tier": "standard", "company": "x", "started_at": now,
                                    "finished_at": now, "duration_ms": 1, "treg_usd": 0.81, "enrich_usd": 0.0,
                                    "leads": 1, "llm_used": True, "outcome": "delivered", "price_tusdm": 0.27})
    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo)
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}},
                                                                     headers=FREE))
    # $0.19 left, the job needs $0.20 (4 probes + Apollo search + routed budget): not run at all.
    with pytest.raises(BudgetUnavailable):
        await process_job("buyer-near", INPUT, settings)
    assert not apollo.called and not routed.called
    row = _fresh_ledger.jobs[_tag("buyer-near")]
    assert row["outcome"] == "failed" and row["treg_usd"] == 0
    assert [a["reserved_usd"] for a in _fresh_ledger.attempts.values()] == [0.0]  # no partial hold
    assert await _fresh_ledger.spend_today() == pytest.approx(0.81)

    _fresh_ledger.jobs["old"]["treg_usd"] = 0.80  # exactly one job's budget left: it runs with all of it
    result = await process_job("buyer-near-2", INPUT, settings)
    assert apollo.called and routed.calls.last.request.headers["X-Treg-Route-Max-Cost"] == "0.1500"
    assert "Jane" in result and await _fresh_ledger.spend_today() <= 1.0 + 1e-9


@respx.mock
async def test_unreadable_ledger_makes_no_treg_call(settings, gemini, monkeypatch):
    class Broken(ledger_module.MemoryLedger):
        async def spend_today(self, now=None):
            raise RuntimeError("db down")

    ledger_module.set_ledger(Broken())
    apollo = respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}, headers=FREE))
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    with pytest.raises(BudgetUnavailable):  # fail closed
        await process_job("buyer-x", INPUT, settings)
    assert not apollo.called and not routed.called


async def test_ledger_stats_aggregate_by_day_and_tier(_fresh_ledger):
    now = time.time()
    for i, (tier, outcome, cost) in enumerate([("standard", "delivered", 0.0), ("standard", "empty", 0.05),
                                               ("pro", "delivered", 0.26), ("pro", "failed", 0.0)]):
        await _fresh_ledger.record_job({"tag": f"t{i}", "tier": tier, "company": "x", "started_at": now,
                                        "finished_at": now, "duration_ms": 1000, "treg_usd": cost,
                                        "enrich_usd": 0.0, "leads": 0 if outcome != "delivered" else 10,
                                        "llm_used": True, "outcome": outcome,
                                        "price_tusdm": 1.05 if tier == "pro" else 0.27})
    s = await _fresh_ledger.stats(7)
    pro = next(d for d in s["days"] if d["tier"] == "pro")
    assert (pro["jobs"], pro["delivered"], pro["failed"], pro["revenue_tusdm"]) == (2, 1, 1, 1.05)
    assert s["totals"] == {"jobs": 4, "cost_usd": 0.31, "revenue_tusdm": 1.59, "cost_assumed_calls": 0,
                           "unconfirmed_usd": 0.0, "crashed_attempts": 0}
    only_pro = await _fresh_ledger.stats(7, tier="pro")
    assert only_pro["totals"] == {"jobs": 2, "cost_usd": 0.26, "revenue_tusdm": 1.05, "cost_assumed_calls": 0,
                                "unconfirmed_usd": 0.0, "crashed_attempts": 0}


# --- payment acceptance ---------------------------------------------------------

def _record(**over):
    base = {"blockchainIdentifier": "pid", "onChainState": "FundsLocked", "agentIdentifier": "agent-1",
            "inputHash": "h1", "RequestedFunds": [{"unit": TUSDM_UNIT, "amount": "270000"}]}
    return {**base, **over}


@pytest.mark.parametrize("over, problem", [
    ({}, None),
    ({"agentIdentifier": "agent-2"}, "another agent"),
    ({"onChainState": "RefundRequested"}, "funds not locked"),
    ({"inputHash": "h2"}, "input hash mismatch"),
    ({"RequestedFunds": [{"unit": TUSDM_UNIT, "amount": "100000"}]}, "below expected"),
    ({"RequestedFunds": [{"unit": "", "amount": "270000000"}]}, "below expected"),  # ADA, not tUSDM
    ({"blockchainIdentifier": "other"}, "payment id mismatch"),
    # BL-08: identity fields must be present, not merely "not different".
    ({"blockchainIdentifier": None}, "payment id missing"),
    ({"blockchainIdentifier": ""}, "payment id missing"),
    ({"inputHash": None}, "input hash missing"),
    ({"inputHash": ""}, "input hash missing"),
    ({"agentIdentifier": None}, "another agent"),
    ({"onChainState": "FundsOrDatumInvalid"}, "funds not locked"),  # Payment Service saw a wrong amount
    ({"onChainState": None}, "funds not locked"),
    # Amounts: no PaidFunds fallback (the field does not exist), malformed entries count as 0.
    ({"RequestedFunds": None, "PaidFunds": [{"unit": TUSDM_UNIT, "amount": "270000"}]}, "below expected"),
    ({"RequestedFunds": []}, "below expected"),
    ({"RequestedFunds": {"unit": TUSDM_UNIT, "amount": "270000"}}, "below expected"),  # not a list
    ({"RequestedFunds": [{"unit": TUSDM_UNIT, "amount": "abc"}]}, "below expected"),
    ({"RequestedFunds": [{"unit": TUSDM_UNIT, "amount": None}]}, "below expected"),
    ({"RequestedFunds": [{"unit": TUSDM_UNIT, "amount": "269999"}]}, "below expected"),
    ({"RequestedFunds": [{"unit": TUSDM_UNIT, "amount": "270000"}, {"unit": "", "amount": "2000000"}]}, None),
    ({"RequestedFunds": [{"unit": TUSDM_UNIT, "amount": "300000"}]}, None),
])
def test_check_payment(over, problem):
    problems = check_payment(_record(**over), job={"input_hash": "h1"}, agent_identifier="agent-1",
                             payment_id="pid", unit=TUSDM_UNIT, min_amount=270000)
    assert (problems == []) if problem is None else any(problem in p for p in problems), problems


@pytest.mark.parametrize("job", [{}, {"input_hash": ""}, {"input_hash": None}])
def test_check_payment_rejects_job_without_input_hash(job):
    problems = check_payment(_record(), job=job, agent_identifier="agent-1", payment_id="pid",
                             unit=TUSDM_UNIT, min_amount=270000)
    assert "input hash missing" in problems


def test_check_payment_reports_every_problem():
    problems = check_payment({}, job={}, agent_identifier="agent-1", payment_id="pid", unit=TUSDM_UNIT,
                             min_amount=270000)
    assert len(problems) == 5


@respx.mock(assert_all_called=False)
async def test_payment_without_input_hash_is_rejected_before_any_work(env, payment_mock, gemini, respx_mock):
    respx_mock.route(host="a").pass_through()
    apollo = respx_mock.post(APOLLO_URL).mock(side_effect=_apollo)
    payment_mock["record"]["inputHash"] = None
    server = _server()
    job_id = await _start(server)
    await payment_mock["callback"]({"blockchainIdentifier": CHAIN_ID})
    job = await server.job_manager.get_job(job_id)
    assert job["status"] == "failed" and "input hash missing" in job["error"]
    assert not apollo.called and payment_mock["submitted"] is None


def _server(storage=None):
    return ReliableAgentServer(
        config=Config(payment_service_url="https://payment.test/api/v1", payment_api_key="k"),
        agent_identifier="test-agent", network="Preprod", job_storage=storage or InMemoryJobStorage(),
        start_job_handler=process_job, input_schema_handler=INPUT_SCHEMA, seller_vkey="test-vkey")


async def _start(server):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.get_app()), base_url="http://a") as c:
        r = await c.post("/start_job", json={"identifier_from_purchaser": "abcdef1234567890", "input_data": INPUT})
        assert r.status_code == 200, r.text
        return r.json()["id"]


@respx.mock(assert_all_called=False)
async def test_bad_payment_is_rejected_before_any_work(env, payment_mock, gemini, respx_mock):
    respx_mock.route(host="a").pass_through()
    apollo = respx_mock.post(APOLLO_URL).mock(side_effect=_apollo)
    payment_mock["record"]["RequestedFunds"] = [{"unit": TUSDM_UNIT, "amount": "1"}]
    server = _server()
    job_id = await _start(server)
    await payment_mock["callback"]({"blockchainIdentifier": CHAIN_ID})
    job = await server.job_manager.get_job(job_id)
    assert job["status"] == "failed" and "payment rejected" in job["error"]
    assert not apollo.called and payment_mock["submitted"] is None
    assert server.counters == {"payments_accepted": 0, "payments_rejected": 1}


@respx.mock(assert_all_called=False)
async def test_unconfirmable_payment_fails_closed(env, payment_mock, gemini, respx_mock, monkeypatch):
    respx_mock.route(host="a").pass_through()
    apollo = respx_mock.post(APOLLO_URL).mock(side_effect=_apollo)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    payment_mock["record"] = RuntimeError("payment service down")
    server = _server()
    job_id = await _start(server)
    await payment_mock["callback"]({"blockchainIdentifier": CHAIN_ID})
    assert (await server.job_manager.get_job(job_id))["status"] == "failed"
    assert payment_mock["status_calls"] == 3 and not apollo.called


@respx.mock(assert_all_called=False)
async def test_good_payment_runs_and_delivers(env, payment_mock, gemini, respx_mock):
    respx_mock.route(host="a").pass_through()
    respx_mock.post(APOLLO_URL).mock(side_effect=_apollo)
    respx_mock.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    server = _server()
    job_id = await _start(server)
    await payment_mock["callback"]({"blockchainIdentifier": CHAIN_ID})
    for _ in range(100):
        job = await server.job_manager.get_job(job_id)
        if job["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)
    assert job["status"] == "completed" and payment_mock["submitted"][1] == job["result"]
    assert server.counters["payments_accepted"] == 1


# --- recovery of half-finished jobs ---------------------------------------------

def _running_job(job_id, input_data=INPUT):
    future = int((time.time() + 3600) * 1000)
    return {"job_id": job_id, "status": "running", "payment_id": f"pay-{job_id}",
            "identifier_from_purchaser": "abcdef1234567890", "input_data": input_data, "pay_by_time": future,
            "submit_result_time": future, "agent_identifier": "test-agent", "seller_vkey": "v", "input_hash": "h",
            "result": None, "error": None}


async def test_recovery_completes_jobs_whose_result_is_already_on_chain(env, payment_mock, _fresh_ledger):
    storage = InMemoryJobStorage()
    await storage.create_job("j1", _running_job("j1"))
    await _fresh_ledger.put_result(identity_for_job(_running_job("j1"), "standard").tag, "standard", "THE RESULT")
    payment_mock["record"] = {"onChainState": "ResultSubmitted"}
    counts = await recover_jobs(_server(storage))
    job = await storage.get_job("j1")
    assert counts["completed"] == 1 and job["status"] == "completed" and job["result"] == "THE RESULT"
    assert payment_mock["submitted"] is None and "pay-j1" not in payment_mock["callbacks"]  # not resubmitted


@pytest.mark.parametrize("on_chain", ["FundsLocked", "RefundRequested"])
@pytest.mark.parametrize("action", ["SubmitResultRequested", "SubmitResultInitiated"])
async def test_recovery_does_not_resubmit_a_result_in_flight(env, payment_mock, _fresh_ledger, on_chain, action):
    # Crash after "submit result" was handed to the Payment Service but before it confirmed on-chain (BL-09).
    storage = InMemoryJobStorage()
    await storage.create_job("j1", _running_job("j1"))
    await _fresh_ledger.put_result(identity_for_job(_running_job("j1"), "standard").tag, "standard", "THE RESULT")
    payment_mock["record"] = {"onChainState": on_chain, "NextAction": {"requestedAction": action}}
    counts = await recover_jobs(_server(storage))
    job = await storage.get_job("j1")
    assert counts == {"resumed": 0, "expired": 0, "completed": 1, "refunded": 0}
    assert job["status"] == "completed" and job["result"] == "THE RESULT"
    assert payment_mock["submitted"] is None and "pay-j1" not in payment_mock["callbacks"]


@pytest.mark.parametrize("next_action", [{"requestedAction": "WaitingForExternalAction"}, None, "garbage"])
async def test_recovery_reruns_locked_job_without_submission(env, payment_mock, _fresh_ledger, next_action):
    # FundsLocked and nothing submitted yet: monitoring is re-attached so the job runs (and submits) once.
    storage = InMemoryJobStorage()
    await storage.create_job("j1", _running_job("j1"))
    payment_mock["record"] = {"onChainState": "FundsLocked", "NextAction": next_action}
    counts = await recover_jobs(_server(storage))
    assert counts["resumed"] == 1 and "pay-j1" in payment_mock["callbacks"]
    assert (await storage.get_job("j1"))["status"] == "running"


async def test_recovery_fails_refunded_and_unrecoverable_jobs(env, payment_mock, _fresh_ledger):
    storage = InMemoryJobStorage()
    await storage.create_job("j2", _running_job("j2"))
    payment_mock["record"] = {"onChainState": "RefundWithdrawn"}
    counts = await recover_jobs(_server(storage))
    assert counts["refunded"] == 1 and (await storage.get_job("j2"))["status"] == "failed"

    storage = InMemoryJobStorage()
    await storage.create_job("j4", _running_job("j4"))
    payment_mock["record"] = {"onChainState": "RefundRequested",
                              "NextAction": {"requestedAction": "WaitingForExternalAction"}}
    counts = await recover_jobs(_server(storage))
    assert counts["refunded"] == 1 and (await storage.get_job("j4"))["status"] == "failed"

    storage = InMemoryJobStorage()
    await storage.create_job("j3", _running_job("j3", {"company": "Other"}))
    payment_mock["record"] = {"onChainState": "ResultSubmitted"}
    await recover_jobs(_server(storage))
    assert "not stored locally" in (await storage.get_job("j3"))["error"]


# --- logs, availability, admin --------------------------------------------------

def test_redaction_and_job_tag():
    token = job_tag_var.set("tag123")
    try:
        rec = logging.LogRecord("root", logging.INFO, __file__, 1, "Escaped Output for hashing: # BD leads Jane Doe",
                                None, None)
        RedactFilter().filter(rec)
        assert rec.getMessage() == "Escaped Output for hashing: [redacted]" and rec.job == "tag123"
        rec2 = logging.LogRecord("masumi.server", logging.INFO, __file__, 1, "Input data for job %s: %s",
                                 ("j1", {"company": "Acme"}), None)
        RedactFilter().filter(rec2)
        assert "Acme" not in rec2.getMessage()
    finally:
        job_tag_var.reset(token)


async def test_degraded_dependencies_report_unavailable(env, payment_mock):
    from main import create_app

    class Down:
        async def check(self):
            return ("unavailable", "payment service unreachable")

    app = create_app(health=Down())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        body = (await c.get("/availability")).json()
    assert body["status"] == "unavailable" and "payment service unreachable" in body["message"]


async def test_admin_stats_and_metrics_need_the_token(env, payment_mock, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "adm-1")
    from main import create_app

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        assert (await c.get("/admin/stats")).status_code == 403
        assert (await c.get("/metrics", headers={"x-admin-token": "wrong"})).status_code == 403
        stats = (await c.get("/admin/stats", headers={"x-admin-token": "adm-1"})).json()
        metrics = (await c.get("/metrics", headers={"x-admin-token": "adm-1"})).text
    assert stats["tier"] == "standard" and stats["daily_spend_cap_usd"] == 1.0 and "ledger" in stats
    assert stats["reserved_today_usd"] == 0
    assert 'bdlf_spend_today_usd{tier="standard"}' in metrics and "bdlf_payments_total" in metrics
    assert 'bdlf_reserved_today_usd{tier="standard"} 0' in metrics
    assert 'bdlf_cost_assumed_calls_7d{tier="standard"} 0' in metrics
