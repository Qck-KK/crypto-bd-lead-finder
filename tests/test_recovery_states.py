"""BL-32: recovery of running jobs whose payment reached WithdrawAuthorized, RefundAuthorized, RefundWithdrawn or a
state this code does not know. State strings are Payment Service 0.29.0's `OnChainState` (prisma/schema.prisma)."""

import asyncio
import logging
import time

import pytest
from masumi import Config, InMemoryJobStorage
from masumi.helper_functions import create_masumi_output_hash

from app import job_store
from app.agent import INPUT_SCHEMA, process_job
from app.job_identity import identity_for_job
from app.job_store import recover_jobs
from app.server import ReliableAgentServer

BUYER = "abcdef1234567890"
INPUT = {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"}
RESULT = "# BD leads\n\nJane S. — Head of BD \"Acme\"\n\nFor B2B professional outreach only."
PAYMENT_SERVICE_029_STATES = {"FundsLocked", "FundsOrDatumInvalid", "ResultSubmitted", "RefundRequested", "Disputed",
                              "WithdrawAuthorized", "RefundAuthorized", "Withdrawn", "RefundWithdrawn",
                              "DisputedWithdrawn"}


def _server(storage):
    return ReliableAgentServer(
        config=Config(payment_service_url="https://payment.test/api/v1", payment_api_key="k"),
        agent_identifier="test-agent", network="Preprod", job_storage=storage,
        start_job_handler=process_job, input_schema_handler=INPUT_SCHEMA, seller_vkey="test-vkey")


def _running_job(job_id, deadline_s=None):
    deadline = int(((time.time() + 3600) if deadline_s is None else deadline_s) * 1000)
    return {"job_id": job_id, "status": "running", "payment_id": f"pay-{job_id}", "identifier_from_purchaser": BUYER,
            "input_data": INPUT, "pay_by_time": deadline, "submit_result_time": deadline,
            "agent_identifier": "test-agent", "seller_vkey": "v", "input_hash": "h", "result": None, "error": None}


async def _setup(ledger, job=None, stored=RESULT):
    storage = InMemoryJobStorage()
    job = job or _running_job("j1")
    await storage.create_job(job["job_id"], job)
    if stored is not None:
        await ledger.put_result(identity_for_job(job, "standard").tag, "standard", stored)
    return storage


def _nothing_ran_or_submitted(payment_mock, job_id="j1"):
    assert payment_mock["submitted"] is None  # no result (re)submitted
    assert f"pay-{job_id}" not in payment_mock["callbacks"]  # no monitoring, so the job cannot run again


def test_state_sets_use_the_payment_service_strings():
    assert job_store._KNOWN_STATES == PAYMENT_SERVICE_029_STATES
    assert {"WithdrawAuthorized", "RefundAuthorized"} <= job_store._KNOWN_STATES


async def test_withdraw_authorized_with_matching_hash_completes(env, payment_mock, _fresh_ledger):
    storage = await _setup(_fresh_ledger)
    payment_mock["record"] = {"onChainState": "WithdrawAuthorized",
                              "resultHash": create_masumi_output_hash(RESULT, BUYER).upper(),
                              "NextAction": {"requestedAction": "WaitingForExternalAction"}}
    counts = await recover_jobs(_server(storage))
    job = await storage.get_job("j1")
    assert counts["completed"] == 1 and counts["reconcile"] == 0
    assert job["status"] == "completed" and job["result"] == RESULT
    _nothing_ran_or_submitted(payment_mock)


@pytest.mark.parametrize("on_chain_hash", ["0" * 64, "", None, 12345])
async def test_withdraw_authorized_with_other_or_missing_hash_needs_reconciliation(env, payment_mock, _fresh_ledger,
                                                                                    caplog, on_chain_hash):
    storage = await _setup(_fresh_ledger)
    payment_mock["record"] = {"onChainState": "WithdrawAuthorized", "resultHash": on_chain_hash}
    counts = await recover_jobs(_server(storage))
    job = await storage.get_job("j1")
    assert counts["reconcile"] == 1 and counts["completed"] == 0
    assert job["status"] == "failed" and job["error"].startswith("Manual reconciliation needed")
    assert "does not match" in job["error"]
    assert any("manual reconciliation" in r.getMessage() for r in caplog.records)
    _nothing_ran_or_submitted(payment_mock)


async def test_withdraw_authorized_without_stored_result_needs_reconciliation(env, payment_mock, _fresh_ledger):
    storage = await _setup(_fresh_ledger, stored=None)
    payment_mock["record"] = {"onChainState": "WithdrawAuthorized",
                              "resultHash": create_masumi_output_hash(RESULT, BUYER)}
    counts = await recover_jobs(_server(storage))
    job = await storage.get_job("j1")
    assert counts["reconcile"] == 1 and job["status"] == "failed" and "no stored result" in job["error"]
    assert _fresh_ledger.results == {}  # nothing regenerated
    _nothing_ran_or_submitted(payment_mock)


@pytest.mark.parametrize("state", ["RefundAuthorized", "RefundWithdrawn"])
@pytest.mark.parametrize("action", [None, "WaitingForExternalAction", "SubmitResultRequested", "SubmitResultInitiated"])
async def test_refund_authorized_or_withdrawn_fails_before_the_submission_check(env, payment_mock, _fresh_ledger,
                                                                                state, action):
    storage = await _setup(_fresh_ledger)
    payment_mock["record"] = {"onChainState": state, "resultHash": None,
                              "NextAction": {"requestedAction": action} if action else None}
    counts = await recover_jobs(_server(storage))
    job = await storage.get_job("j1")
    assert counts == {"resumed": 0, "expired": 0, "completed": 0, "refunded": 1, "reconcile": 0, "errors": 0}
    assert job["status"] == "failed" and state in job["error"] and job["result"] is None
    _nothing_ran_or_submitted(payment_mock)


async def test_unknown_state_is_logged_and_handled_as_before(env, payment_mock, _fresh_ledger, caplog):
    storage = await _setup(_fresh_ledger)
    payment_mock["record"] = {"onChainState": "SomeFutureState"}
    counts = await recover_jobs(_server(storage))
    assert counts["resumed"] == 1 and (await storage.get_job("j1"))["status"] == "running"
    assert any("unknown on-chain state" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    assert payment_mock["submitted"] is None  # monitoring only runs the job on FundsLocked

    storage = await _setup(_fresh_ledger, job=_running_job("j2", deadline_s=time.time() - 60))
    counts = await recover_jobs(_server(storage))
    assert counts["expired"] == 1 and "Deadline passed" in (await storage.get_job("j2"))["error"]


@pytest.mark.parametrize("state, outcome, status", [
    ("WithdrawAuthorized", "completed", "completed"),
    ("RefundAuthorized", "refunded", "failed"),
    ("RefundWithdrawn", "refunded", "failed"),
    ("ResultSubmitted", "completed", "completed"),
    ("FundsLocked", "expired", "failed"),
])
async def test_settled_states_win_over_a_passed_deadline(env, payment_mock, _fresh_ledger, state, outcome, status):
    # Disputes end long after the submit deadline: such a job is not a missed deadline.
    storage = await _setup(_fresh_ledger, job=_running_job("j1", deadline_s=time.time() - 86400))
    payment_mock["record"] = {"onChainState": state, "resultHash": create_masumi_output_hash(RESULT, BUYER)}
    counts = await recover_jobs(_server(storage))
    job = await storage.get_job("j1")
    assert counts[outcome] == 1 and job["status"] == status
    assert ("Deadline passed" in (job["error"] or "")) is (outcome == "expired")
    _nothing_ran_or_submitted(payment_mock)


# --- resilience: Payment Service down or slow, database or ledger errors -----------------------------------------

PAST, FUTURE = time.time() - 86400, None
DB_PASSWORD = "dbpass-SENTINEL-recovery"


def _closed_port_url():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/api/v1"


@pytest.fixture
def monitoring(monkeypatch):
    """Records re-attached monitoring instead of polling (the real lookup stays unpatched)."""
    from app.masumi_compat import V2Payment

    attached = []

    async def start_status_monitoring(self, callback=None, interval_seconds=10):
        attached.extend(self.payment_ids)

    monkeypatch.setattr(V2Payment, "start_status_monitoring", start_status_monitoring)
    return attached


def _assert_untouched(job, before):
    assert job["status"] == "running" and job["error"] is None and job["result"] is None
    assert job == before


@pytest.mark.parametrize("deadline, outcome", [(PAST, "errors"), (FUTURE, "resumed")], ids=["past-deadline", "before-deadline"])
async def test_lookup_timeout_infers_nothing(env, payment_mock, _fresh_ledger, monkeypatch, deadline, outcome):
    from app.masumi_compat import V2Payment

    async def hangs(self, blockchain_identifier):
        await asyncio.sleep(30)

    monkeypatch.setattr(V2Payment, "check_payment_status_by_identifier", hangs)
    monkeypatch.setattr(job_store, "LOOKUP_TIMEOUT_S", 0.05)
    storage = await _setup(_fresh_ledger, job=_running_job("j1", deadline_s=deadline))
    before = dict(await storage.get_job("j1"))
    started = time.monotonic()
    counts = await recover_jobs(_server(storage))
    assert time.monotonic() - started < 5
    assert counts[outcome] == 1 and sum(counts.values()) == 1
    job = await storage.get_job("j1")
    _assert_untouched(job, before)  # not refunded, not failed on the deadline
    assert payment_mock["submitted"] is None
    assert ("pay-j1" in payment_mock["callbacks"]) is (outcome == "resumed")  # before the deadline: as before


@pytest.mark.parametrize("deadline, outcome", [(PAST, "errors"), (FUTURE, "resumed")], ids=["past-deadline", "before-deadline"])
async def test_payment_service_unreachable_infers_nothing(env, _fresh_ledger, monitoring, caplog, deadline, outcome):
    storage = await _setup(_fresh_ledger, job=_running_job("j1", deadline_s=deadline))
    before = dict(await storage.get_job("j1"))
    server = ReliableAgentServer(
        config=Config(payment_service_url=_closed_port_url(), payment_api_key="k"), agent_identifier="test-agent",
        network="Preprod", job_storage=storage, start_job_handler=process_job, input_schema_handler=INPUT_SCHEMA,
        seller_vkey="test-vkey")
    counts = await recover_jobs(server)  # real aiohttp lookup, connection refused
    assert counts[outcome] == 1 and sum(counts.values()) == 1
    _assert_untouched(await storage.get_job("j1"), before)
    assert (monitoring == ["pay-j1"]) is (outcome == "resumed")
    assert any("Could not read the on-chain state" in r.getMessage() for r in caplog.records)


class _FlakyStorage(InMemoryJobStorage):
    """Database writes for job `bad` fail."""

    async def update_job(self, job_id, updates):
        if job_id == "bad":
            raise OSError(f"connection reset postgresql://masumi:{DB_PASSWORD}@postgres:5432/agent_jobs")
        return await super().update_job(job_id, updates)


async def test_a_database_error_on_one_job_does_not_stop_the_others(env, payment_mock, _fresh_ledger, caplog):
    storage = _FlakyStorage()
    for job_id in ("bad", "good"):
        await storage.create_job(job_id, _running_job(job_id))
    payment_mock["record"] = {"onChainState": "RefundAuthorized"}
    counts = await recover_jobs(_server(storage))
    assert counts["errors"] == 1 and counts["refunded"] == 1
    assert (await storage.get_job("bad"))["status"] == "running"  # left for the next start
    assert (await storage.get_job("good"))["status"] == "failed"
    lines = [r.getMessage() for r in caplog.records if "Recovery of job" in r.getMessage()]
    assert lines and "bad" in lines[0] and "OSError" in lines[0]
    assert DB_PASSWORD not in caplog.text


async def test_a_ledger_error_on_one_job_does_not_stop_the_others(env, payment_mock, _fresh_ledger, monkeypatch,
                                                                  caplog):
    storage = await _setup(_fresh_ledger, job=_running_job("j1"))
    await storage.create_job("j2", _running_job("j2"))
    # Same buyer, so the same result has the same hash; each order stores its own copy (identity has the payment id).
    await _fresh_ledger.put_result(identity_for_job(_running_job("j2"), "standard").tag, "standard", RESULT)
    payment_mock["record"] = {"onChainState": "WithdrawAuthorized",
                              "resultHash": create_masumi_output_hash(RESULT, BUYER)}
    real_get = _fresh_ledger.get_result
    calls = {"n": 0}

    async def get_result(tag):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError(f"ledger down password={DB_PASSWORD}")
        return await real_get(tag)

    monkeypatch.setattr(_fresh_ledger, "get_result", get_result)
    counts = await recover_jobs(_server(storage))
    statuses = sorted([(await storage.get_job(j))["status"] for j in ("j1", "j2")])
    assert counts["errors"] == 1 and counts["completed"] == 1 and statuses == ["completed", "running"]
    assert DB_PASSWORD not in caplog.text
    _nothing_ran_or_submitted(payment_mock)
    _nothing_ran_or_submitted(payment_mock, "j2")


async def test_repeated_recovery_with_the_payment_service_down_then_back(env, payment_mock, _fresh_ledger):
    storage = await _setup(_fresh_ledger, job=_running_job("j1", deadline_s=PAST))
    before = dict(await storage.get_job("j1"))
    payment_mock["record"] = ConnectionError("payment service down")
    for _ in range(3):  # restarts while it is down: one lookup each, nothing settled
        calls = payment_mock["status_calls"]
        counts = await recover_jobs(_server(storage))
        assert counts == {"resumed": 0, "expired": 0, "completed": 0, "refunded": 0, "reconcile": 0, "errors": 1}
        assert payment_mock["status_calls"] == calls + 1
        _assert_untouched(await storage.get_job("j1"), before)

    payment_mock["record"] = {"onChainState": "WithdrawAuthorized",
                              "resultHash": create_masumi_output_hash(RESULT, BUYER)}
    assert (await recover_jobs(_server(storage)))["completed"] == 1  # back: settled once
    calls = payment_mock["status_calls"]
    assert not any((await recover_jobs(_server(storage))).values())  # and never again
    assert payment_mock["status_calls"] == calls
    job = await storage.get_job("j1")
    assert job["status"] == "completed" and job["result"] == RESULT
    _nothing_ran_or_submitted(payment_mock)


@pytest.mark.parametrize("record", [
    {"onChainState": "WithdrawAuthorized", "resultHash": "match"},
    {"onChainState": "WithdrawAuthorized", "resultHash": "0" * 64},
    {"onChainState": "RefundAuthorized"},
    {"onChainState": "RefundWithdrawn", "NextAction": {"requestedAction": "SubmitResultInitiated"}},
])
async def test_recovery_is_idempotent(env, payment_mock, _fresh_ledger, record):
    if record.get("resultHash") == "match":
        record = {**record, "resultHash": create_masumi_output_hash(RESULT, BUYER)}
    storage = await _setup(_fresh_ledger)
    payment_mock["record"] = record
    first = await recover_jobs(_server(storage))
    after_first = dict(await storage.get_job("j1"))
    results_after_first = dict(_fresh_ledger.results)
    calls = payment_mock["status_calls"]

    second = await recover_jobs(_server(storage))  # another restart
    assert sum(first.values()) == 1 and not any(second.values())
    assert await storage.get_job("j1") == after_first and _fresh_ledger.results == results_after_first
    assert payment_mock["status_calls"] == calls  # a finished job is not looked up again
    _nothing_ran_or_submitted(payment_mock)


async def test_a_failed_state_update_is_settled_once_on_the_next_start(env, payment_mock, _fresh_ledger):
    # The stored result exists, the job update fails: the next start settles it once, nothing is resubmitted.
    class FailOnce(InMemoryJobStorage):
        failures = 1

        async def update_job(self, job_id, updates):
            if self.failures:
                self.failures -= 1
                raise OSError("connection reset")
            return await super().update_job(job_id, updates)

    storage = FailOnce()
    job = _running_job("j1")
    await storage.create_job("j1", job)
    await _fresh_ledger.put_result(identity_for_job(job, "standard").tag, "standard", RESULT)
    payment_mock["record"] = {"onChainState": "WithdrawAuthorized",
                              "resultHash": create_masumi_output_hash(RESULT, BUYER)}
    results = dict(_fresh_ledger.results)
    assert (await recover_jobs(_server(storage)))["errors"] == 1
    assert (await storage.get_job("j1"))["status"] == "running"
    assert (await recover_jobs(_server(storage)))["completed"] == 1
    assert not any((await recover_jobs(_server(storage))).values())
    job = await storage.get_job("j1")
    assert job["status"] == "completed" and job["result"] == RESULT and _fresh_ledger.results == results
    _nothing_ran_or_submitted(payment_mock)


async def test_lookup_timeout_is_enforced_on_a_real_hanging_connection(env, _fresh_ledger, monitoring, monkeypatch):
    # A server that accepts the connection and never answers: the SDK's own aiohttp lookup must be cut off.
    async def silent(reader, writer):
        await asyncio.sleep(60)

    tcp = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = tcp.sockets[0].getsockname()[1]
    monkeypatch.setattr(job_store, "LOOKUP_TIMEOUT_S", 0.3)
    storage = await _setup(_fresh_ledger, job=_running_job("j1", deadline_s=PAST))
    server = ReliableAgentServer(
        config=Config(payment_service_url=f"http://127.0.0.1:{port}/api/v1", payment_api_key="k"),
        agent_identifier="test-agent", network="Preprod", job_storage=storage, start_job_handler=process_job,
        input_schema_handler=INPUT_SCHEMA, seller_vkey="test-vkey")
    try:
        started = time.monotonic()
        counts = await recover_jobs(server)
        elapsed = time.monotonic() - started
    finally:
        tcp.close()
    assert 0.3 <= elapsed < 3 and counts["errors"] == 1
    assert (await storage.get_job("j1"))["status"] == "running" and monitoring == []
