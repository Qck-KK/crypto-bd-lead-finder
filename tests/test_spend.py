"""BL-13: the daily spend cap holds under concurrency; every treg call is metered; missing cost headers count
at the call's max cost; reservations survive a crash until the end of the UTC day and are reused by a retry."""

import asyncio
import dataclasses
import json
import os
import time
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app import agent as agent_module
from app import drafts as drafts_module
from app import ledger as ledger_module
from app.agent import _apollo_probe, _job_tag, _spend_ceiling, process_job
from app.ledger import MemoryLedger
from app.resolver import ResolvedCompany
from app.router import find_leads
from app.spend import JobBudget, SpendCapReached
from app.treg_client import APOLLO_MAX_COST_USD, TregError, search_apollo, search_people
from conftest import FakeGemini

APOLLO_URL = "https://treg.test/call/apollo.people.search"
ROUTED_URL = "https://treg.test/call/treg.people.search"
INPUT = {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"}
FREE = {"X-Treg-Cost-Micro": "0"}

requires_pg = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))


def _apollo(headers=FREE):
    """One Jane at acme.io (fewer than MIN_LEADS_BEFORE_PAID, so the routed search runs too)."""
    def handler(request):
        if json.loads(request.content)["q_organization_domains_list"] != ["acme.io"]:
            return httpx.Response(200, json={"people": []}, headers=headers)
        return httpx.Response(200, json={"people": [
            {"id": "p1", "first_name": "Jane", "last_name_obfuscated": "S***h", "title": "Head of BD",
             "organization": {"name": "Acme Labs"}}]}, headers=headers)
    return handler


def _resolved(verified=True):
    return ResolvedCompany(name="Acme", given_domain="acme.io", domain="acme.io", verified=verified)


def _row(tag, treg=0.0, enrich=0.0, assumed=0, now=None):
    now = now or time.time()
    return {"tag": tag, "tier": "standard", "company": "Acme", "started_at": now - 1, "finished_at": now,
            "duration_ms": 1000, "treg_usd": treg, "enrich_usd": enrich, "leads": 1, "llm_used": True,
            "outcome": "delivered", "price_tusdm": 0.27, "cost_assumed_calls": assumed}


def _kwargs(**over):
    base = dict(token="tok", company="Acme", role="BD", company_domain="acme.io", limit=10, max_cost_usd=0.15,
                job_tag="t", base_url="https://treg.test")
    base.update(over)
    return base


# --- cost headers: missing or invalid = the call's max cost --------------------------------------------------

@pytest.mark.parametrize("header,cost,assumed", [
    (None, 0.15, True), ("", 0.15, True), ("abc", 0.15, True), ("1.5", 0.15, True), ("-1", 0.15, True),
    ("0", 0.0, False), ("52000", 0.052, False), ("150000", 0.15, False)])
@respx.mock
async def test_cost_header_is_parsed_fail_safe(header, cost, assumed):
    headers = {} if header is None else {"X-Treg-Cost-Micro": header}
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}, headers=headers))
    result = await search_people(**_kwargs())
    assert result.cost_usd == pytest.approx(cost) and result.cost_assumed is assumed


@respx.mock
async def test_apollo_without_cost_header_counts_its_max_cost():
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}))
    result = await search_apollo(token="tok", domain="acme.io", titles=None, per_page=5, job_tag="t",
                                 base_url="https://treg.test")
    assert result.cost_usd == APOLLO_MAX_COST_USD and result.cost_assumed


@pytest.mark.parametrize("response,cost,assumed", [
    (httpx.Response(402, json={"error": "x"}, headers={"X-Treg-Error": "1"}), 0.15, True),
    (httpx.Response(402, json={"error": "x"}, headers={"X-Treg-Error": "1", "X-Treg-Cost-Micro": "0"}), 0.0, False),
    (httpx.Response(200, text="not json", headers={"X-Treg-Cost-Micro": "3000"}), 0.003, False),
    (httpx.ConnectTimeout("slow"), 0.15, True)])
@respx.mock
async def test_failed_call_carries_its_possible_cost(response, cost, assumed):
    route = respx.post(ROUTED_URL)
    route.mock(side_effect=response) if isinstance(response, Exception) else route.mock(return_value=response)
    with pytest.raises(TregError) as err:
        await search_people(**_kwargs())
    assert err.value.cost_usd == pytest.approx(cost) and err.value.cost_assumed is assumed


# --- the job budget meters every call ------------------------------------------------------------------------

@respx.mock
async def test_router_skips_apollo_when_the_grant_cannot_cover_it(settings):
    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo())
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}},
                                                                     headers=FREE))
    budget = JobBudget(0.005)
    res = await find_leads(settings, "Acme", "BD", _resolved(), "t", budget=budget)
    assert not apollo.called and res.steps[0].error == "daily spend budget used up"
    # The routed search fits in what is left, capped at it.
    assert routed.calls.last.request.headers["X-Treg-Route-Max-Cost"] == "0.0050"


@respx.mock
async def test_router_fails_the_job_when_the_grant_covers_nothing(settings):
    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo())
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    with pytest.raises(TregError, match="daily spend budget used up"):
        await find_leads(settings, "Acme", "BD", _resolved(), "t", budget=JobBudget(0.0005))
    assert not apollo.called and not routed.called


@respx.mock
async def test_router_charges_every_call_to_the_job_budget(settings):
    respx.post(APOLLO_URL).mock(side_effect=_apollo({"X-Treg-Cost-Micro": "10000"}))
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}},
                                                                     headers={"X-Treg-Cost-Micro": "20000"}))
    budget = JobBudget(1.0)
    res = await find_leads(settings, "Acme", "BD", _resolved(), "t", budget=budget)
    assert budget.treg_usd == pytest.approx(0.03) and budget.cost_assumed_calls == 0
    # TREG_JOB_BUDGET_USD caps the routed calls only; the Apollo cost comes out of the grant.
    assert routed.calls.last.request.headers["X-Treg-Route-Max-Cost"] == "0.1500"
    assert res.spent_usd == pytest.approx(0.02)


@respx.mock
async def test_router_counts_failed_calls_at_their_max_cost(settings):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(500, json={}))
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(402, json={"error": "x"}, headers={"X-Treg-Error": "1"}))
    budget = JobBudget(1.0)
    with pytest.raises(TregError):
        await find_leads(settings, "Acme", "BD", _resolved(), "t", budget=budget)
    assert budget.treg_usd == pytest.approx(0.16) and budget.cost_assumed_calls == 2


@respx.mock
async def test_probe_is_metered_and_refused_when_the_grant_is_used_up(settings):
    apollo = respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}))
    with pytest.raises(SpendCapReached):
        await _apollo_probe(settings, "t", JobBudget(0.0))("acme.io")
    assert not apollo.called

    budget = JobBudget(0.015)
    await _apollo_probe(settings, "t", budget)("acme.io")  # no cost header: counts 0.01
    assert budget.treg_usd == pytest.approx(0.01) and budget.cost_assumed_calls == 1
    with pytest.raises(SpendCapReached):
        await _apollo_probe(settings, "t", budget)("acme.com")  # 0.005 left < 0.01
    assert apollo.call_count == 1


def test_spend_ceiling_covers_every_probe_the_apollo_search_and_the_budgets(settings):
    # acme.io + 3 guessed domains, plus the Apollo search, at 0.01 each; routed 0.15.
    assert _spend_ceiling(settings, "Acme Labs", "acme.io") == pytest.approx(0.20)
    pro = dataclasses.replace(settings, tier="pro", enrich_budget_usd=0.30)
    assert _spend_ceiling(pro, "Acme Labs", "acme.io") == pytest.approx(0.50)
    assert _spend_ceiling(dataclasses.replace(settings, tier="pro"), "Acme Labs", "acme.io") == pytest.approx(0.20)


# --- reservations (in-memory ledger) -------------------------------------------------------------------------

async def test_reservations_grant_what_the_cap_has_left_in_order():
    led = MemoryLedger()
    grants = [await led.reserve(t, "standard", 0.2, 0.5) for t in "abcd"]
    assert grants == pytest.approx([0.2, 0.2, 0.1, 0.0])
    assert "d" not in led.reservations  # nothing granted, nothing held
    assert await led.spend_today() == pytest.approx(0.5) and await led.reserved_today() == pytest.approx(0.5)

    await led.settle(_row("a", treg=0.05))  # actual spend replaces the reservation
    assert "a" not in led.reservations and led.jobs["a"]["treg_usd"] == 0.05
    assert await led.spend_today() == pytest.approx(0.35)
    assert await led.reserve("e", "standard", 0.2, 0.5) == pytest.approx(0.15)


async def test_a_retry_reuses_its_reservation_instead_of_reserving_again():
    led = MemoryLedger()
    assert await led.reserve("a", "standard", 0.2, 0.2) == pytest.approx(0.2)
    assert await led.reserve("a", "standard", 0.2, 0.2) == pytest.approx(0.2)  # same job: same hold
    assert await led.spend_today() == pytest.approx(0.2)
    assert await led.reserve("b", "standard", 0.2, 0.2) == 0.0

    # A short grant is topped up on retry when the cap has room again, never doubled.
    assert await led.reserve("x", "standard", 0.2, 0.3) == pytest.approx(0.1)
    assert await led.reserve("x", "standard", 0.2, 0.5) == pytest.approx(0.2)
    assert await led.spend_today() == pytest.approx(0.4)


async def test_an_unsettled_reservation_counts_until_the_end_of_the_utc_day():
    led = MemoryLedger()
    evening = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc).timestamp()
    await led.reserve("crashed", "standard", 0.2, 1.0, now=evening)
    assert await led.spend_today(now=evening + 3500) == pytest.approx(0.2)  # 23:58, still held
    assert await led.spend_today(now=evening + 3700) == 0.0  # next UTC day
    # A retry on the next day reserves afresh against that day's cap.
    assert await led.reserve("crashed", "standard", 0.2, 0.1, now=evening + 3700) == pytest.approx(0.1)


# --- process_job --------------------------------------------------------------------------------------------

@respx.mock
async def test_two_concurrent_jobs_cannot_both_spend_a_one_job_cap(settings, gemini, _fresh_ledger):
    """Acceptance: cap = one job budget, two jobs in flight; before BL-13 both read the same remainder."""
    in_flight, peak = 0, 0

    async def slow_routed(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, json={"output": {"people": []}}, headers={"X-Treg-Cost-Micro": "150000"})

    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo())
    routed = respx.post(ROUTED_URL).mock(side_effect=slow_routed)
    capped = dataclasses.replace(settings, daily_spend_cap_usd=settings.job_budget_usd)
    out = await asyncio.gather(process_job("buyer-a", INPUT, capped), process_job("buyer-b", INPUT, capped),
                               return_exceptions=True)

    delivered = [r for r in out if isinstance(r, str)]
    failed = [r for r in out if isinstance(r, TregError)]
    assert len(delivered) == 1 and len(failed) == 1 and "daily spend budget used up" in str(failed[0])
    assert routed.call_count == 1 and apollo.call_count == 2  # one job's probe + search; the other made none
    assert await _fresh_ledger.spend_today() <= capped.daily_spend_cap_usd + 1e-9
    assert sum(r["treg_usd"] for r in _fresh_ledger.jobs.values()) == pytest.approx(0.15)
    assert not _fresh_ledger.reservations


@respx.mock
async def test_jobs_that_fit_the_cap_still_run_in_parallel(settings, gemini, _fresh_ledger):
    in_flight, peak = 0, 0

    async def slow_routed(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, json={"output": {"people": []}}, headers={"X-Treg-Cost-Micro": "20000"})

    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    routed = respx.post(ROUTED_URL).mock(side_effect=slow_routed)
    roomy = dataclasses.replace(settings, daily_spend_cap_usd=2 * _spend_ceiling(settings, "Acme Labs", "acme.io"))
    out = await asyncio.gather(process_job("buyer-a", INPUT, roomy), process_job("buyer-b", INPUT, roomy))
    assert all(isinstance(r, str) for r in out) and routed.call_count == 2 and peak == 2
    assert await _fresh_ledger.spend_today() == pytest.approx(0.04)


@pytest.mark.parametrize("apollo_headers,assumed", [({"X-Treg-Cost-Micro": "10000"}, 0), ({}, 2)])
@respx.mock
async def test_probe_and_apollo_costs_are_recorded(settings, gemini, _fresh_ledger, apollo_headers, assumed):
    respx.post(APOLLO_URL).mock(side_effect=_apollo(apollo_headers))
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}},
                                                            headers={"X-Treg-Cost-Micro": "20000"}))
    await process_job("buyer", INPUT, settings)
    row = _fresh_ledger.jobs[_job_tag("buyer", INPUT)]
    # probe acme.io (0.01) + Apollo search (0.01) + routed (0.02)
    assert row["treg_usd"] == pytest.approx(0.04) and row["cost_assumed_calls"] == assumed
    stats = await _fresh_ledger.stats(1)
    assert stats["totals"]["cost_assumed_calls"] == assumed


@respx.mock
async def test_a_failing_settle_does_not_fail_the_paid_job(settings, gemini):
    class NoSettle(MemoryLedger):
        async def settle(self, row):
            raise RuntimeError("db down")

    led = NoSettle()
    ledger_module.set_ledger(led)
    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}, headers=FREE))
    result = await process_job("buyer", INPUT, settings)
    assert "Jane" in result
    # Not settled: the reservation keeps counting (fail closed) until the end of the UTC day.
    assert await led.spend_today() == pytest.approx(_spend_ceiling(settings, "Acme Labs", "acme.io"))


@respx.mock
async def test_a_crashed_job_keeps_its_reservation_and_its_retry_reuses_it(settings, gemini, _fresh_ledger,
                                                                           monkeypatch):
    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}},
                                                                     headers={"X-Treg-Cost-Micro": "20000"}))
    ceiling = _spend_ceiling(settings, "Acme Labs", "acme.io")
    capped = dataclasses.replace(settings, daily_spend_cap_usd=ceiling)  # room for exactly one job

    async def crash(*args, **kwargs):
        raise asyncio.CancelledError()  # shutdown mid-job: not an Exception, so nothing is settled

    monkeypatch.setattr(agent_module, "write_outreach", crash)
    with pytest.raises(asyncio.CancelledError):
        await process_job("buyer", INPUT, capped)
    tag = _job_tag("buyer", INPUT)
    assert _fresh_ledger.reservations[tag]["usd"] == pytest.approx(ceiling) and tag not in _fresh_ledger.jobs
    monkeypatch.undo()
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))

    with pytest.raises(TregError):  # another job finds the cap held by the crashed one
        await process_job("other-buyer", INPUT, capped)
    calls = routed.call_count

    result = await process_job("buyer", INPUT, capped)  # recovery re-runs the same job tag
    assert "Jane" in result and routed.call_count == calls + 1
    assert tag not in _fresh_ledger.reservations and _fresh_ledger.jobs[tag]["outcome"] == "delivered"


@respx.mock
async def test_a_cached_rerun_and_an_invalid_input_reserve_nothing(settings, gemini, _fresh_ledger, monkeypatch):
    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}, headers=FREE))
    first = await process_job("buyer", INPUT, settings)

    calls = []
    original = _fresh_ledger.reserve

    async def spy(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(_fresh_ledger, "reserve", spy)
    assert await process_job("buyer", INPUT, settings) == first
    with pytest.raises(ValueError):
        await process_job("buyer", {"company": ""}, settings)
    assert calls == [] and not _fresh_ledger.reservations


# --- PostgreSQL: two agents, one database ---------------------------------------------------------------------

@requires_pg
async def test_postgres_reservations_hold_the_cap_across_two_agents():
    from app.ledger import PostgresLedger

    dsn = os.environ["TEST_DATABASE_URL"]
    standard, pro = PostgresLedger(dsn), PostgresLedger(dsn)  # two containers, two pools
    try:
        pool = await standard._p()
        await pool.execute("DELETE FROM spend_reservations WHERE tag LIKE 'test-bl13-%'")
        await pool.execute("DELETE FROM job_costs WHERE tag LIKE 'test-bl13-%'")
        await pro._p()
        base = await standard.spend_today()
        cap = base + 0.5

        grants = await asyncio.gather(*[(pro if i % 2 else standard).reserve(f"test-bl13-{i}", "standard", 0.15, cap)
                                        for i in range(8)])
        assert sum(grants) == pytest.approx(0.5)
        assert sorted(round(g, 4) for g in grants) == [0.0] * 4 + [0.05, 0.15, 0.15, 0.15]
        assert await pro.spend_today() == pytest.approx(cap)
        assert await standard.reserved_today() >= 0.5 - 1e-9

        held = {f"test-bl13-{i}": g for i, g in enumerate(grants) if g > 0}
        tag, grant = next((t, g) for t, g in held.items() if g == pytest.approx(0.15))
        # A retry of the same job reuses its hold; nothing more is reserved.
        assert await pro.reserve(tag, "standard", 0.15, cap) == pytest.approx(grant)
        assert await standard.spend_today() == pytest.approx(cap)

        # Settling swaps the reservation for the actual spend in one transaction.
        await standard.settle(_row(tag, treg=0.04, assumed=2))
        assert await pool.fetchval("SELECT count(*) FROM spend_reservations WHERE tag = $1", tag) == 0
        assert await pool.fetchval("SELECT cost_assumed_calls FROM job_costs WHERE tag = $1", tag) == 2
        assert await standard.spend_today() == pytest.approx(cap - grant + 0.04)
        assert (await standard.stats(1))["totals"]["cost_assumed_calls"] >= 2

        # A reservation from a previous UTC day no longer counts, and a retry reserves afresh.
        old_tag, old = next((t, g) for t, g in held.items() if t != tag)
        await pool.execute("UPDATE spend_reservations SET created_at = now() - interval '1 day' WHERE tag = $1",
                           old_tag)
        assert await standard.spend_today() == pytest.approx(cap - grant + 0.04 - old)
        assert await pro.reserve(old_tag, "standard", 0.15, cap) == pytest.approx(min(0.15, grant - 0.04 + old))
    finally:
        pool = await standard._p()
        await pool.execute("DELETE FROM spend_reservations WHERE tag LIKE 'test-bl13-%'")
        await pool.execute("DELETE FROM job_costs WHERE tag LIKE 'test-bl13-%'")
        await standard.close()
        await pro.close()


@requires_pg
async def test_postgres_reserve_waits_for_the_other_agents_reservation():
    """Deterministic version of the race: the other agent holds the spend lock mid-reservation."""
    import asyncpg

    from app.ledger import SPEND_LOCK_KEY, PostgresLedger

    dsn = os.environ["TEST_DATABASE_URL"]
    led = PostgresLedger(dsn)
    other = await asyncpg.connect(dsn)
    try:
        pool = await led._p()
        await pool.execute("DELETE FROM spend_reservations WHERE tag LIKE 'test-bl13-%'")
        cap = await led.spend_today() + 0.15  # room for one job

        tx = other.transaction()
        await tx.start()
        await other.execute("SELECT pg_advisory_xact_lock($1)", SPEND_LOCK_KEY)
        waiter = asyncio.create_task(led.reserve("test-bl13-waiter", "standard", 0.15, cap))
        await asyncio.sleep(0.3)
        assert not waiter.done()  # blocked: it must not read the total while the other agent reserves
        await other.execute("INSERT INTO spend_reservations (tag, tier, reserved_usd) "
                            "VALUES ('test-bl13-holder', 'pro', 0.15)")
        await tx.commit()
        assert await asyncio.wait_for(waiter, 5) == 0.0  # it saw the other agent's hold
    finally:
        await pool.execute("DELETE FROM spend_reservations WHERE tag LIKE 'test-bl13-%'")
        await other.close()
        await led.close()


@requires_pg
async def test_postgres_schema_upgrade_is_idempotent():
    from app.ledger import PostgresLedger

    dsn = os.environ["TEST_DATABASE_URL"]
    for _ in range(2):  # an existing database (pre-BL-13 job_costs) gets the new column and table once
        led = PostgresLedger(dsn)
        try:
            assert await led.ping()
            await led.record_job({k: v for k, v in _row("test-bl13-legacy").items() if k != "cost_assumed_calls"})
        finally:
            pool = await led._p()
            await pool.execute("DELETE FROM job_costs WHERE tag = 'test-bl13-legacy'")
            await led.close()
