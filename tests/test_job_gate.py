"""Checks before a payment request is created: daily budget (BL-33) and rate / unpaid-order limits (BL-15)."""

import asyncio
import dataclasses
import time
from types import SimpleNamespace

import httpx
import pytest

from app.job_gate import StartJobGate, max_spend_ceiling
from app.ledger import MemoryLedger
from app.settings import Settings

START = {"identifier_from_purchaser": "abcdef1234567890", "input_data": {"company": "Example Labs"}}


def _cost_row(tag, usd):
    now = time.time()
    return {"tag": tag, "tier": "standard", "company": "x", "started_at": now, "finished_at": now, "duration_ms": 1,
            "treg_usd": usd, "enrich_usd": 0.0, "leads": 1, "llm_used": True, "outcome": "delivered",
            "price_tusdm": 0.27}


async def _client(monkeypatch, led=None, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from main import create_app

    app = create_app(job_ledger=led or MemoryLedger())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent")


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _request(host="1.2.3.4", headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers or {})


def test_max_spend_ceiling_covers_the_largest_job(settings):
    # 6 domain candidates + the Apollo search at $0.01, plus the routed budget (and Pro enrichment when on).
    assert max_spend_ceiling(settings) == pytest.approx(0.22)
    pro = dataclasses.replace(settings, tier="pro", enrich_budget_usd=0.30)
    assert max_spend_ceiling(pro) == pytest.approx(0.52)
    assert max_spend_ceiling(dataclasses.replace(settings, tier="pro")) == pytest.approx(0.22)  # enrichment off


# --- BL-33: no sale without budget for the job -----------------------------------------------------------

async def test_a_used_up_budget_stops_sales_before_any_payment_request(env, payment_mock, monkeypatch):
    led = MemoryLedger()
    await led.record_job(_cost_row("spent", 0.85))  # $0.15 left < $0.22 per job
    async with await _client(monkeypatch, led) as c:
        av = (await c.get("/availability")).json()
        r = await c.post("/start_job", json=START)
    assert av["status"] == "unavailable" and "data budget is used up" in av["message"]
    assert r.status_code == 503 and "after 00:00 UTC" in r.json()["detail"]
    assert int(r.headers["Retry-After"]) >= 60
    assert "0.85" not in r.text and "0.15" not in r.text  # no spend figures to buyers
    assert payment_mock["callback"] is None  # no payment request, no monitoring


async def test_open_holds_of_the_other_agent_count_against_the_budget(env, payment_mock, monkeypatch):
    led = MemoryLedger()
    await led.reserve("pro-attempt", "T", "pro", 0.8, 1.0, 2400)  # a running Pro job holds $0.80
    async with await _client(monkeypatch, led) as c:
        assert (await c.post("/start_job", json=START)).status_code == 503


async def test_a_budget_that_covers_one_job_sells(env, payment_mock, monkeypatch):
    led = MemoryLedger()
    await led.record_job(_cost_row("spent", 0.78))  # $0.22 left = one job
    async with await _client(monkeypatch, led) as c:
        assert (await c.get("/availability")).json()["status"] == "available"
        assert (await c.post("/start_job", json=START)).status_code == 200


async def test_an_unreadable_ledger_stops_sales(env, payment_mock, monkeypatch):
    class Broken(MemoryLedger):
        async def spend_today(self, now=None):
            raise RuntimeError("db down")

    async with await _client(monkeypatch, Broken()) as c:
        av = (await c.get("/availability")).json()
        r = await c.post("/start_job", json=START)
    assert av["status"] == "unavailable" and "spend ledger is unavailable" in av["message"]
    assert r.status_code == 503 and payment_mock["callback"] is None


async def test_the_budget_check_is_cached_briefly(settings, _fresh_ledger):
    clock = _Clock()
    gate = StartJobGate(dataclasses.replace(settings, daily_spend_cap_usd=1.0), clock=clock, budget_ttl_s=15)
    assert await gate.budget_problem() is None
    await _fresh_ledger.record_job(_cost_row("spent", 0.9))
    assert await gate.budget_problem() is None  # cached
    clock.t += 16
    assert await gate.budget_problem() == "today's data budget is used up"


# --- BL-15: rate limits and unpaid orders ----------------------------------------------------------------

async def test_the_per_client_limit_refuses_with_retry_after(env, payment_mock, monkeypatch):
    async with await _client(monkeypatch, START_JOB_PER_CLIENT_PER_HOUR="2") as c:
        codes = [(await c.post("/start_job", json=START)).status_code for _ in range(3)]
        r = await c.post("/start_job", json=START)
    assert codes == [200, 200, 429] and r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 3600


async def test_the_client_header_is_only_trusted_when_configured(env, payment_mock, monkeypatch):
    async with await _client(monkeypatch, START_JOB_PER_CLIENT_PER_HOUR="1") as c:
        assert (await c.post("/start_job", json=START, headers={"X-Forwarded-For": "9.9.9.1"})).status_code == 200
        # A spoofed header does not make a new client when no header is configured.
        assert (await c.post("/start_job", json=START, headers={"X-Forwarded-For": "9.9.9.2"})).status_code == 429
    async with await _client(monkeypatch, START_JOB_PER_CLIENT_PER_HOUR="1",
                             RATE_LIMIT_CLIENT_HEADER="X-Forwarded-For") as c:
        assert (await c.post("/start_job", json=START, headers={"X-Forwarded-For": "9.9.9.1, 10.0.0.1"})).status_code \
            == 200
        assert (await c.post("/start_job", json=START, headers={"X-Forwarded-For": "9.9.9.2"})).status_code == 200
        assert (await c.post("/start_job", json=START, headers={"X-Forwarded-For": "9.9.9.1"})).status_code == 429


async def test_the_total_limit_holds_across_clients(env, payment_mock, monkeypatch):
    async with await _client(monkeypatch, START_JOB_PER_HOUR="3", RATE_LIMIT_CLIENT_HEADER="X-Client") as c:
        codes = [(await c.post("/start_job", json=START, headers={"X-Client": f"c{i}"})).status_code
                 for i in range(4)]
    assert codes == [200, 200, 200, 429]


async def test_the_window_slides(settings):
    clock = _Clock()
    gate = StartJobGate(dataclasses.replace(settings, start_job_per_client_per_hour=1), clock=clock)
    assert await gate.check(_request()) is None
    assert (await gate.check(_request())).status_code == 429
    clock.t += 3599
    assert (await gate.check(_request())).status_code == 429
    clock.t += 2
    assert await gate.check(_request()) is None


async def test_unpaid_orders_are_capped_and_refusals_use_no_rate_slot(env, payment_mock, monkeypatch):
    async with await _client(monkeypatch, MAX_AWAITING_PAYMENT="2", START_JOB_PER_CLIENT_PER_HOUR="3") as c:
        answers = [await c.post("/start_job", json=START) for _ in range(4)]
    # Two orders await payment; the next two are refused for that, without using the client's 3 slots
    # (if a refusal used one, the fourth would hit the rate limit instead).
    assert [a.status_code for a in answers] == [200, 200, 429, 429]
    assert all("unpaid" in a.json()["detail"] for a in answers[2:])


async def test_concurrent_requests_cannot_overshoot_the_limit(settings):
    gate = StartJobGate(dataclasses.replace(settings, start_job_per_client_per_hour=2), clock=_Clock())

    async def slow_unpaid():
        await asyncio.sleep(0.01)  # an await between the limit check and the decision
        return 0

    gate.unpaid_jobs = slow_unpaid
    out = await asyncio.gather(*[gate.check(_request()) for _ in range(5)])
    assert sum(r is None for r in out) == 2 and all(r.status_code == 429 for r in out if r is not None)


async def test_negative_limits_mean_unlimited(settings):
    gate = StartJobGate(dataclasses.replace(settings, start_job_per_client_per_hour=-1, start_job_per_hour=-1,
                                            max_awaiting_payment=-1), clock=_Clock())
    assert all([await gate.check(_request()) is None for _ in range(50)])


async def test_gate_counters_reach_metrics(env, payment_mock, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "adm-1")
    async with await _client(monkeypatch, START_JOB_PER_CLIENT_PER_HOUR="1") as c:
        await c.post("/start_job", json=START)
        await c.post("/start_job", json=START)
        stats = (await c.get("/admin/stats", headers={"x-admin-token": "adm-1"})).json()
        metrics = (await c.get("/metrics", headers={"x-admin-token": "adm-1"})).text
    assert stats["start_job_gate"] == {"rate_limited": 1, "unpaid_limit": 0, "budget_refused": 0, "accepted": 1}
    assert 'bdlf_start_job_total{tier="standard",result="rate_limited"} 1' in metrics


def test_limits_come_from_env(monkeypatch):
    for k, v in {"START_JOB_PER_CLIENT_PER_HOUR": "5", "START_JOB_PER_HOUR": "-1", "MAX_AWAITING_PAYMENT": "7",
                 "RATE_LIMIT_CLIENT_HEADER": " X-Forwarded-For "}.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert (s.start_job_per_client_per_hour, s.start_job_per_hour, s.max_awaiting_payment,
            s.rate_limit_client_header) == (5, -1, 7, "X-Forwarded-For")
