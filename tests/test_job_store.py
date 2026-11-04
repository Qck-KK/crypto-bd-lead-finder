"""Job persistence and restart recovery.

Recovery tests use the SDK's in-memory storage (state survives because the same storage object is
handed to a new server). The PostgreSQL tests run only when TEST_DATABASE_URL points at a scratch
database, e.g. postgresql://user:pass@localhost:5433/agent_jobs_test.
"""

import asyncio
import json
import os
import time

import httpx
import pytest
import respx
from masumi import Config, InMemoryJobStorage, MasumiAgentServer

from app import drafts as drafts_module
from app.agent import INPUT_SCHEMA, process_job
from app.job_store import PostgresJobStorage, recover_jobs
from conftest import CHAIN_ID, FakeGemini, mock_apollo_empty

TREG_URL = "https://treg.test/call/treg.people.search"


def _server(storage):
    return MasumiAgentServer(
        config=Config(payment_service_url="https://payment.test/api/v1", payment_api_key="k"),
        agent_identifier="test-agent", network="Preprod", job_storage=storage,
        start_job_handler=process_job, input_schema_handler=INPUT_SCHEMA, seller_vkey="test-vkey",
    )


def _job(job_id, status, pay_by_ms, submit_ms, payment_id=CHAIN_ID):
    return {"job_id": job_id, "status": status, "payment_id": payment_id,
            "identifier_from_purchaser": "abcdef1234567890", "input_data": {"company": "Example Labs"},
            "pay_by_time": pay_by_ms, "submit_result_time": submit_ms, "agent_identifier": "test-agent",
            "seller_vkey": "test-vkey", "input_hash": "h", "result": None, "error": None}


async def _wait_status(server, job_id, statuses=("completed", "failed")):
    for _ in range(200):
        job = await server.job_manager.get_job(job_id)
        if job["status"] in statuses:
            return job
        await asyncio.sleep(0.01)
    return job


@respx.mock
async def test_restart_resumes_running_job_and_expires_stale_ones(env, payment_mock, monkeypatch):
    now = time.time()
    future, past = int((now + 3600) * 1000), int((now - 60) * 1000)
    storage = InMemoryJobStorage()
    await storage.create_job("running-1", _job("running-1", "running", past, future))
    await storage.create_job("waiting-1", _job("waiting-1", "awaiting_payment", future, future, payment_id="pid2"))
    await storage.create_job("stale-1", _job("stale-1", "awaiting_payment", past, future, payment_id="pid3"))
    await storage.create_job("done-1", {**_job("done-1", "completed", past, past), "result": "old"})

    mock_apollo_empty(respx)
    treg = respx.post(TREG_URL).mock(return_value=httpx.Response(200, json={"output": {"people": [
        {"name": "Alice Nakamoto", "title": "Head of BD", "email": "alice@gmail.com"}]}}))
    monkeypatch.setattr(drafts_module.genai, "Client",
                        lambda api_key: FakeGemini(text=json.dumps({"drafts": [{"index": 0, "draft": "Hi Alice"}]})))

    # "Restart": a fresh server on the same storage.
    server = _server(storage)
    counts = await recover_jobs(server, now=now)
    assert counts == {"resumed": 2, "expired": 1, "completed": 0, "refunded": 0, "reconcile": 0, "errors": 0}
    assert (await storage.get_job("stale-1"))["status"] == "failed"
    assert (await storage.get_job("done-1"))["result"] == "old"
    assert not treg.called  # nothing runs before escrow is confirmed again

    # The Payment Service reports FundsLocked for the running job: it runs and delivers.
    callbacks = payment_mock["callbacks"]
    assert set(callbacks) == {CHAIN_ID, "pid2"}
    await callbacks[CHAIN_ID]({"blockchainIdentifier": CHAIN_ID})
    job = await _wait_status(server, "running-1")
    assert job["status"] == "completed"
    assert "Hi Alice" in job["result"] and "gmail" not in job["result"]
    assert payment_mock["submitted"] == (CHAIN_ID, job["result"])
    assert (await storage.get_job("waiting-1"))["status"] == "awaiting_payment"


async def test_recovery_ignores_jobs_without_payment_id(env, payment_mock):
    storage = InMemoryJobStorage()
    await storage.create_job("x", _job("x", "running", None, None, payment_id=None))
    counts = await recover_jobs(_server(storage))
    assert counts == {"resumed": 0, "expired": 1, "completed": 0, "refunded": 0, "reconcile": 0, "errors": 0}


class _FakePgStorage(PostgresJobStorage):
    """PostgresJobStorage with the database swapped for memory, to test the app wiring."""

    def __init__(self):
        super().__init__("postgresql://unused")
        self.mem = InMemoryJobStorage()
        self.events = []

    async def connect(self):
        self.events.append("connect")

    async def close(self):
        self.events.append("close")

    async def create_job(self, job_id, job_data):
        await self.mem.create_job(job_id, job_data)

    async def get_job(self, job_id):
        return await self.mem.get_job(job_id)

    async def update_job(self, job_id, updates):
        await self.mem.update_job(job_id, updates)

    async def delete_job(self, job_id):
        await self.mem.delete_job(job_id)

    async def list_jobs(self, status=None):
        return await self.mem.list_jobs(status)


async def test_app_lifespan_connects_and_recovers(env, payment_mock):
    from main import create_app

    storage = _FakePgStorage()
    future = int((time.time() + 3600) * 1000)
    await storage.create_job("w", _job("w", "awaiting_payment", future, future, payment_id="pid9"))
    app = create_app(job_storage=storage)

    async with app.router.lifespan_context(app):
        assert storage.events == ["connect"]
        assert set(payment_mock["callbacks"]) == {"pid9"}
    assert storage.events == ["connect", "close"]


requires_pg = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


@requires_pg
async def test_postgres_storage_roundtrip():
    storage = PostgresJobStorage(os.environ["TEST_DATABASE_URL"])
    await storage.connect()
    try:
        pool = await storage._p()
        await pool.execute("DELETE FROM agent_jobs WHERE job_id LIKE 'test-%'")
        await storage.create_job("test-1", {"job_id": "test-1", "status": "awaiting_payment", "input_data": {"a": 1}})
        await storage.update_job("test-1", {"status": "running"})
        assert (await storage.get_job("test-1"))["status"] == "running"
        assert (await storage.get_job("test-1"))["input_data"] == {"a": 1}
        assert [j["job_id"] for j in await storage.list_jobs("running") if j["job_id"].startswith("test-")] == ["test-1"]
        with pytest.raises(ValueError):
            await storage.update_job("test-missing", {"status": "x"})
        await storage.delete_job("test-1")
        assert await storage.get_job("test-1") is None
    finally:
        await storage.close()


@requires_pg
async def test_postgres_state_survives_new_storage_instance():
    dsn = os.environ["TEST_DATABASE_URL"]
    first = PostgresJobStorage(dsn)
    await first.create_job("test-2", {"job_id": "test-2", "status": "running"})
    await first.close()
    second = PostgresJobStorage(dsn)
    try:
        assert (await second.get_job("test-2"))["status"] == "running"
        await second.delete_job("test-2")
    finally:
        await second.close()


@requires_pg
async def test_tier_tables_are_separate():
    dsn = os.environ["TEST_DATABASE_URL"]
    std, pro = PostgresJobStorage(dsn), PostgresJobStorage(dsn, table="agent_jobs_pro")
    try:
        await std.create_job("test-tier", {"job_id": "test-tier", "status": "running"})
        assert await pro.get_job("test-tier") is None
        await std.delete_job("test-tier")
    finally:
        await std.close()
        await pro.close()
    with pytest.raises(ValueError):
        PostgresJobStorage(dsn, table="jobs; DROP TABLE x")


@requires_pg
async def test_postgres_feedback_store():
    from app.feedback import PostgresFeedbackStore

    store = PostgresFeedbackStore(os.environ["TEST_DATABASE_URL"])
    try:
        pool = await store._p()
        await pool.execute("DELETE FROM feedback WHERE ref LIKE 'test-%'")
        await pool.execute("DELETE FROM feedback_deliveries WHERE ref LIKE 'test-%'")
        await pool.execute("DELETE FROM suppressed_people WHERE hash LIKE 'test-%'")
        await store.remember_delivery("test-r", "Acme", [{"hash": "test-h0", "title": "BD"},
                                                        {"hash": "test-h1", "title": "CTO"}])
        assert (await store.get_delivery("test-r"))["leads"][1]["title"] == "CTO"
        await store.save_feedback("test-r", 4, {0: "left_company", 1: "useful"}, "ok")
        await store.save_feedback("test-r", 5, {0: "left_company"}, "again")  # no double count
        assert "test-h0" in await store.suppressed_hashes() and "test-h1" not in await store.suppressed_hashes()
        assert await pool.fetchval("SELECT reports FROM suppressed_people WHERE hash = 'test-h0'") == 1
        summary = await store.summary()
        assert summary["responses"] >= 1 and summary["suppressed_people"] >= 1
    finally:
        await store.close()


@requires_pg
async def test_postgres_ledger():
    import time as _t

    from app.ledger import PostgresLedger

    led = PostgresLedger(os.environ["TEST_DATABASE_URL"])
    try:
        pool = await led._p()
        await pool.execute("DELETE FROM job_results WHERE tag LIKE 'test-%'")
        await pool.execute("DELETE FROM job_costs WHERE tag LIKE 'test-%'")
        await led.put_result("test-1", "standard", "R1")
        await led.put_result("test-1", "standard", "R2")  # first result wins: same on-chain hash
        assert await led.get_result("test-1") == "R1"
        before = await led.spend_today()
        now = _t.time()
        row = {"tag": "test-1", "tier": "pro", "company": "Acme", "started_at": now - 2, "finished_at": now,
               "duration_ms": 2000, "treg_usd": 0.05, "enrich_usd": 0.1, "leads": 3, "llm_used": True,
               "outcome": "delivered", "price_tusdm": 1.05}
        await led.record_job(row)
        assert abs(await led.spend_today() - before - 0.15) < 1e-9
        assert any(d["tier"] == "pro" for d in (await led.stats(1))["days"])
        assert await led.ping()
    finally:
        await led.close()
