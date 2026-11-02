"""Daily spend cap (BL-13) with per-attempt reservations (BL-34), UTC-midnight-safe expiry (BL-35) and fail-safe
treg costs (BL-37): every treg call is metered and written ahead; a retry never releases an earlier attempt's
hold; holds count until counts_until; unknown costs count at the (floored) max cost."""

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
from app.agent import ATTEMPT_GRACE_S, _apollo_probe, _spend_ceiling, process_job
from app.job_identity import job_tag
from app.ledger import MemoryLedger, end_of_utc_day
from app.resolver import ResolvedCompany
from app.router import find_leads
from app.spend import JobBudget, SpendCapReached, SpendLedgerUnavailable
from app.treg_client import (APOLLO_MAX_COST_USD, TregError, TregSearchResult, floor_cost, idempotency_key,
                             search_apollo, search_people)
from conftest import FakeGemini

APOLLO_URL = "https://treg.test/call/apollo.people.search"
ROUTED_URL = "https://treg.test/call/treg.people.search"
INPUT = {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"}
FREE = {"X-Treg-Cost-Micro": "0"}
PAID = {"X-Treg-Cost-Micro": "150000"}
LIFETIME = 1800 + ATTEMPT_GRACE_S

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


def _empty(headers=FREE):
    return httpx.Response(200, json={"output": {"people": []}}, headers=headers)


def _resolved(domain="acme.io", verified=True):
    return ResolvedCompany(name="Acme", given_domain=domain, domain=domain, verified=verified)


def _tag(buyer="buyer"):
    return job_tag("standard", "", None, buyer, INPUT)


def _row(tag, treg=0.0, now=None, outcome="delivered"):
    now = now or time.time()
    return {"tag": tag, "tier": "standard", "company": "Acme", "started_at": now - 1, "finished_at": now,
            "duration_ms": 1000, "treg_usd": treg, "enrich_usd": 0.0, "unconfirmed_usd": 0.0, "leads": 1,
            "llm_used": True, "outcome": outcome, "price_tusdm": 0.27, "cost_assumed_calls": 0}


def _snap(confirmed=0.0, assumed=0.0, pending=0.0, calls=0):
    return {"confirmed_usd": confirmed, "assumed_usd": assumed, "pending_usd": pending, "cost_assumed_calls": calls}


def _open(led):
    return [a for a in led.attempts.values() if a["status"] == "open"]


def _kwargs(**over):
    base = dict(token="tok", company="Acme", role="BD", company_domain="acme.io", limit=10, max_cost_usd=0.15,
                job_tag="t", base_url="https://treg.test")
    base.update(over)
    return base


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


async def _crash_in_drafting(monkeypatch):
    async def crash(*args, **kwargs):
        raise asyncio.CancelledError()  # shutdown mid-job: not an Exception, so nothing is settled
    monkeypatch.setattr(agent_module, "write_outreach", crash)


# --- treg costs: missing/invalid header = max cost; 502 charged_micro; floored max -----------------------------

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
    # 502 route_failed bills charged_micro (treg docs): taken from the body, the larger of body and header.
    (httpx.Response(502, json={"error": "route_failed", "charged_micro": 40000}, headers={"X-Treg-Error": "1"}),
     0.04, False),
    (httpx.Response(502, json={"detail": {"charged_micro": "40000"}}, headers={"X-Treg-Cost-Micro": "10000"}),
     0.04, False),
    (httpx.Response(502, json={"charged_micro": 10000}, headers={"X-Treg-Cost-Micro": "40000"}), 0.04, False),
    (httpx.Response(502, json={"charged_micro": -5}), 0.15, True),
    (httpx.Response(502, json={"charged_micro": True}), 0.15, True),
    (httpx.ConnectTimeout("slow"), 0.15, True)])
@respx.mock
async def test_failed_call_carries_its_possible_cost(response, cost, assumed):
    route = respx.post(ROUTED_URL)
    route.mock(side_effect=response) if isinstance(response, Exception) else route.mock(return_value=response)
    with pytest.raises(TregError) as err:
        await search_people(**_kwargs())
    assert err.value.cost_usd == pytest.approx(cost) and err.value.cost_assumed is assumed


@pytest.mark.parametrize("max_cost,header", [(0.01999, "0.0199"), (0.0199, "0.0199"), (0.15, "0.1500"),
                                             (0.00995, "0.0099"), (0.057, "0.0570")])
@respx.mock
async def test_max_cost_is_floored_and_assumed_at_exactly_what_was_sent(max_cost, header):
    route = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    result = await search_people(**_kwargs(max_cost_usd=max_cost))
    assert route.calls.last.request.headers["X-Treg-Route-Max-Cost"] == header
    assert result.cost_usd == float(header) == floor_cost(max_cost) and result.cost_assumed


@respx.mock
async def test_a_max_cost_that_floors_to_zero_is_not_sent():
    route = respx.post(ROUTED_URL).mock(return_value=_empty())
    with pytest.raises(TregError, match="not sent") as err:
        await search_people(**_kwargs(max_cost_usd=0.00009))
    assert not route.called and err.value.cost_usd == 0


@pytest.mark.parametrize("trust,response,cost", [
    (False, httpx.Response(402, json={"error": "route_max_cost"}, headers={"X-Treg-Error": "1"}), 0.15),
    (True, httpx.Response(402, json={"error": "route_max_cost"}, headers={"X-Treg-Error": "1"}), 0.0),
    (True, httpx.Response(409, json={"error": "in_progress"}, headers={"X-Treg-Error": "1"}), 0.0),
    (True, httpx.Response(402, json={"error": "something_else"}, headers={"X-Treg-Error": "1"}), 0.15),
    (True, httpx.Response(402, json={"error": "route_max_cost"}), 0.15),  # not treg's own refusal
    (True, httpx.Response(502, json={"error": "route_failed"}, headers={"X-Treg-Error": "1"}), 0.15)])
@respx.mock
async def test_free_refusals_count_zero_only_when_trusted(trust, response, cost):
    respx.post(ROUTED_URL).mock(return_value=response)
    with pytest.raises(TregError) as err:
        await search_people(**_kwargs(trust_refusals=trust))
    assert err.value.cost_usd == pytest.approx(cost) and err.value.cost_assumed is (cost > 0)


# --- idempotency keys follow the request body (M3) -----------------------------------------------------------

def test_idempotency_key_is_stable_per_request_and_changes_with_the_body():
    body = {"q_organization_domains_list": ["acme.io"], "per_page": 25}
    assert idempotency_key("t-ap", "apollo.people.search", body) == \
        idempotency_key("t-ap", "apollo.people.search", {"per_page": 25, "q_organization_domains_list": ["acme.io"]})
    assert idempotency_key("t-ap", "apollo.people.search", body) != \
        idempotency_key("t-ap", "apollo.people.search", {**body, "q_organization_domains_list": ["acmelabs.com"]})
    assert idempotency_key("t-ap", "apollo.people.search", body) != idempotency_key("u-ap", "apollo.people.search", body)
    assert len(idempotency_key("t" * 200, "x", body)) == 45


@respx.mock
async def test_a_retry_with_a_changed_request_does_not_reuse_the_key(settings):
    apollo = respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}, headers=FREE))
    respx.post(ROUTED_URL).mock(return_value=_empty())
    await find_leads(settings, "Acme", "BD", _resolved("acme.io"), "job", budget=JobBudget(1.0))
    await find_leads(settings, "Acme", "BD", _resolved("acme.io"), "job", budget=JobBudget(1.0))
    await find_leads(settings, "Acme", "BD", _resolved("acmelabs.com"), "job", budget=JobBudget(1.0))
    keys = [c.request.headers["Idempotency-Key"] for c in apollo.calls]
    assert keys[0] == keys[1]  # same request: treg replays it at no cost
    assert keys[2] != keys[0]  # the resolution changed the body: a new key, not treg's 422 for a reused one


# --- the attempt budget meters every call ---------------------------------------------------------------------

@respx.mock
async def test_router_skips_apollo_when_the_grant_cannot_cover_it(settings):
    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo())
    routed = respx.post(ROUTED_URL).mock(return_value=_empty())
    res = await find_leads(settings, "Acme", "BD", _resolved(), "t", budget=JobBudget(0.005))
    assert not apollo.called and res.steps[0].error == "daily spend budget used up"
    assert routed.calls.last.request.headers["X-Treg-Route-Max-Cost"] == "0.0050"


@respx.mock
async def test_router_fails_the_job_when_the_grant_covers_nothing(settings):
    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo())
    routed = respx.post(ROUTED_URL).mock(return_value=_empty())
    with pytest.raises(TregError, match="daily spend budget used up"):
        await find_leads(settings, "Acme", "BD", _resolved(), "t", budget=JobBudget(0.0005))
    assert not apollo.called and not routed.called


@respx.mock
async def test_router_charges_every_call_to_the_attempt_budget(settings):
    respx.post(APOLLO_URL).mock(side_effect=_apollo({"X-Treg-Cost-Micro": "10000"}))
    routed = respx.post(ROUTED_URL).mock(return_value=_empty({"X-Treg-Cost-Micro": "20000"}))
    budget = JobBudget(1.0)
    res = await find_leads(settings, "Acme", "BD", _resolved(), "t", budget=budget)
    assert budget.treg_usd == pytest.approx(0.03) and budget.cost_assumed_calls == 0
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
    assert budget.confirmed["treg"] == 0 and budget.assumed["treg"] == pytest.approx(0.16)


@respx.mock
async def test_probe_is_metered_and_refused_when_the_grant_is_used_up(settings):
    apollo = respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}))
    with pytest.raises(SpendCapReached):
        await _apollo_probe(settings, "t", JobBudget(0.0))("acme.io")
    assert not apollo.called
    budget = JobBudget(0.015)
    await _apollo_probe(settings, "t", budget)("acme.io")  # no cost header: counts 0.01 (assumed)
    assert budget.treg_usd == pytest.approx(0.01) and budget.cost_assumed_calls == 1
    with pytest.raises(SpendCapReached):
        await _apollo_probe(settings, "t", budget)("acme.com")  # 0.005 left < 0.01
    assert apollo.call_count == 1


def test_spend_ceiling_covers_every_probe_the_apollo_search_and_the_budgets(settings):
    assert _spend_ceiling(settings, "Acme Labs", "acme.io") == pytest.approx(0.20)
    pro = dataclasses.replace(settings, tier="pro", enrich_budget_usd=0.30)
    assert _spend_ceiling(pro, "Acme Labs", "acme.io") == pytest.approx(0.50)


async def test_a_call_is_not_sent_unless_its_pending_cost_was_written_first():
    sent = []

    async def broken(snapshot):
        raise RuntimeError("db down")

    async def call():
        sent.append(1)
        return TregSearchResult(people=[], cost_usd=0.0)

    budget = JobBudget(1.0, progress=broken)
    with pytest.raises(SpendLedgerUnavailable):
        await budget.run(0.15, call)
    assert not sent and budget.pending == 0 and budget.spent == 0


async def test_a_failed_post_call_write_leaves_the_call_pending_in_the_ledger():
    rows = []

    async def flaky(snapshot):
        if snapshot["pending_usd"] == 0:
            raise RuntimeError("write lost")  # the write after the call
        rows.append(snapshot)

    async def call():
        return TregSearchResult(people=[], cost_usd=0.15)

    budget = JobBudget(1.0, progress=flaky)
    await budget.run(0.15, call)
    assert budget.progress_failed and budget.confirmed["treg"] == pytest.approx(0.15)
    # What the ledger has: the call as pending at its max cost, never as a known actual cost.
    assert rows[-1] == _snap(pending=0.15)


async def test_a_call_cancelled_mid_flight_stays_charged_at_its_max_cost():
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.sleep(3600)

    budget = JobBudget(1.0)
    task = asyncio.create_task(budget.run(0.15, hang))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.assumed["treg"] == pytest.approx(0.15) and budget.cost_assumed_calls == 1 and budget.pending == 0


# --- attempt ledger (in memory) -------------------------------------------------------------------------------

async def test_every_attempt_gets_its_own_hold_in_order():
    led = MemoryLedger()
    grants = [await led.reserve(f"a{i}", f"tag{i}", "standard", 0.2, 0.5, LIFETIME) for i in range(4)]
    assert grants == pytest.approx([0.2, 0.2, 0.1, 0.0])
    assert await led.spend_today() == pytest.approx(0.5) and await led.reserved_today() == pytest.approx(0.5)
    assert await led.settle("a0", _row("tag0", treg=0.05), _snap(confirmed=0.05)) == "settled"
    assert led.attempts["a0"]["status"] == "settled" and await led.spend_today() == pytest.approx(0.35)
    assert await led.reserve("a4", "tag4", "standard", 0.2, 0.5, LIFETIME) == pytest.approx(0.15)


async def test_a_retry_never_releases_or_reduces_the_previous_attempts_hold():
    led = MemoryLedger()
    assert await led.reserve("first", "T", "standard", 0.2, 0.5, LIFETIME) == pytest.approx(0.2)
    await led.progress("first", _snap(confirmed=0.15))  # charged, then crashed before settling
    assert await led.reserve("retry", "T", "standard", 0.2, 0.5, LIFETIME) == pytest.approx(0.2)  # its own hold
    await led.settle("retry", _row("T", treg=0.0), _snap())  # treg replayed the calls at $0
    first = led.attempts["first"]
    assert first["status"] == "open" and first["reserved_usd"] == pytest.approx(0.2)
    assert await led.spend_today() == pytest.approx(0.2)  # the crashed attempt still counts in full
    assert await led.reserve("other", "U", "standard", 0.5, 0.5, LIFETIME) == pytest.approx(0.3)


async def test_an_open_hold_keeps_counting_after_midnight():
    led = MemoryLedger()
    evening = _utc(2026, 11, 2, 23, 59, 50)
    await led.reserve("late", "A", "standard", 0.2, 0.3, LIFETIME, now=evening)
    after = evening + 15  # 00:00:05 next UTC day
    assert await led.spend_today(now=after) == pytest.approx(0.2)
    assert await led.reserve("early", "B", "standard", 0.2, 0.3, LIFETIME, now=after) == pytest.approx(0.1)
    # A settles after midnight: its actual spend counts on the new day.
    await led.settle("late", _row("A", treg=0.15, now=after + 30), _snap(confirmed=0.15))
    assert await led.spend_today(now=after + 31) == pytest.approx(0.15 + 0.1)


async def test_counts_until_covers_the_attempts_lifetime_and_expiry_records_the_crash_once():
    led = MemoryLedger()
    evening = _utc(2026, 11, 2, 23, 50)  # created 10 min before midnight: lifetime reaches into Nov 3
    await led.reserve("crashed", "A", "standard", 0.2, 1.0, LIFETIME, now=evening)
    await led.progress("crashed", _snap(confirmed=0.04, assumed=0.01, pending=0.15, calls=1))
    assert led.attempts["crashed"]["counts_until"] == _utc(2026, 11, 4)
    assert await led.spend_today(now=_utc(2026, 11, 3, 23, 59)) == pytest.approx(0.2)  # held all of Nov 3
    assert await led.expire_attempts(now=_utc(2026, 11, 3, 23, 59)) == 0
    assert await led.expire_attempts(now=_utc(2026, 11, 4, 0, 1)) == 1
    assert await led.expire_attempts(now=_utc(2026, 11, 4, 0, 2)) == 0  # idempotent
    row = led.jobs["attempt:crashed"]
    assert row["outcome"] == "crashed" and row["day"] == "2026-11-03"  # not counted again on Nov 4
    assert row["treg_usd"] == pytest.approx(0.20) and row["unconfirmed_usd"] == pytest.approx(0.16)
    assert row["cost_assumed_calls"] == 2 and await led.spend_today(now=_utc(2026, 11, 4, 0, 3)) == 0
    # A settle that arrives after expiry is refused; the crash record stays the only record.
    assert await led.settle("crashed", _row("A", treg=0.05), _snap(confirmed=0.05)) == "expired"
    assert "A" not in led.jobs
    stats = await led.stats(7, now=_utc(2026, 11, 4, 1))
    assert stats["totals"]["crashed_attempts"] == 1 and stats["totals"]["jobs"] == 0


# --- process_job ----------------------------------------------------------------------------------------------

@respx.mock
async def test_two_concurrent_jobs_cannot_both_spend_a_one_job_cap(settings, gemini, _fresh_ledger):
    async def slow_routed(request):
        await asyncio.sleep(0.05)
        return _empty(PAID)

    apollo = respx.post(APOLLO_URL).mock(side_effect=_apollo())
    routed = respx.post(ROUTED_URL).mock(side_effect=slow_routed)
    capped = dataclasses.replace(settings, daily_spend_cap_usd=settings.job_budget_usd)
    out = await asyncio.gather(process_job("buyer-a", INPUT, capped), process_job("buyer-b", INPUT, capped),
                               return_exceptions=True)
    delivered = [r for r in out if isinstance(r, str)]
    failed = [r for r in out if isinstance(r, TregError)]
    assert len(delivered) == 1 and len(failed) == 1 and "daily spend budget used up" in str(failed[0])
    assert routed.call_count == 1 and apollo.call_count == 2
    assert await _fresh_ledger.spend_today() <= capped.daily_spend_cap_usd + 1e-9
    assert not _open(_fresh_ledger)


@respx.mock
async def test_jobs_that_fit_the_cap_still_run_in_parallel(settings, gemini, _fresh_ledger):
    in_flight, peak = 0, 0

    async def slow_routed(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _empty({"X-Treg-Cost-Micro": "20000"})

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
    respx.post(ROUTED_URL).mock(return_value=_empty({"X-Treg-Cost-Micro": "20000"}))
    await process_job("buyer", INPUT, settings)
    row = _fresh_ledger.jobs[_tag()]
    assert row["treg_usd"] == pytest.approx(0.04) and row["cost_assumed_calls"] == assumed
    assert row["unconfirmed_usd"] == pytest.approx(0.01 * assumed)


@respx.mock
async def test_crash_after_a_paid_call_then_a_zero_cost_replay_retry(settings, gemini, _fresh_ledger, monkeypatch):
    """H1 regression: treg replays the retry's calls at $0, so the retry must not release the first hold."""
    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    routed = respx.post(ROUTED_URL).mock(side_effect=[_empty(PAID), _empty(FREE)])  # charged, then replayed
    ceiling = _spend_ceiling(settings, "Acme Labs", "acme.io")
    capped = dataclasses.replace(settings, daily_spend_cap_usd=2 * ceiling + 0.05)

    await _crash_in_drafting(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await process_job("buyer", INPUT, capped)
    (first,) = _open(_fresh_ledger)
    assert first["confirmed_usd"] == pytest.approx(0.15)  # written ahead of the crash
    monkeypatch.undo()
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))

    assert "Jane" in await process_job("buyer", INPUT, capped)  # recovery re-runs the same job tag
    keys = [c.request.headers["Idempotency-Key"] for c in routed.calls]
    assert keys[0] == keys[1]  # same request, same key: treg's free replay is preserved
    assert _fresh_ledger.jobs[_tag()]["treg_usd"] == 0  # the retry itself paid nothing
    assert first["status"] == "open"  # never released by the retry
    assert await _fresh_ledger.spend_today() == pytest.approx(ceiling)  # the first hold, in full
    # Another job only gets what the cap leaves next to the crashed attempt's hold.
    assert await _fresh_ledger.reserve("x", "X", "standard", 1.0, capped.daily_spend_cap_usd, LIFETIME) == \
        pytest.approx(ceiling + 0.05)

    # At expiry the first attempt's known charge is recorded: the books show the $0.15 really spent.
    await _fresh_ledger.expire_attempts(now=first["counts_until"])
    crash = _fresh_ledger.jobs[f"attempt:{first['attempt_id']}"]
    assert crash["treg_usd"] == pytest.approx(0.15) and crash["unconfirmed_usd"] == 0


@respx.mock
async def test_repeated_crashes_each_keep_their_own_hold(settings, gemini, _fresh_ledger, monkeypatch):
    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    respx.post(ROUTED_URL).mock(side_effect=[_empty(PAID)] + [_empty(FREE)] * 5)
    ceiling = _spend_ceiling(settings, "Acme Labs", "acme.io")
    await _crash_in_drafting(monkeypatch)
    for _ in range(3):
        with pytest.raises(asyncio.CancelledError):
            await process_job("buyer", INPUT, settings)
    assert len(_open(_fresh_ledger)) == 3 and await _fresh_ledger.spend_today() == pytest.approx(3 * ceiling)

    # With a cap that the three holds use up, a fourth attempt gets nothing: no treg call, the job fails.
    tight = dataclasses.replace(settings, daily_spend_cap_usd=3 * ceiling)
    calls = respx.calls.call_count
    with pytest.raises(TregError, match="daily spend budget used up"):
        await process_job("buyer", INPUT, tight)
    assert respx.calls.call_count == calls and len(_open(_fresh_ledger)) == 3
    monkeypatch.undo()
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))
    await process_job("buyer", INPUT, settings)
    assert len(_open(_fresh_ledger)) == 3  # the success settled only its own attempt

    until = max(a["counts_until"] for a in _fresh_ledger.attempts.values())
    assert await _fresh_ledger.expire_attempts(now=until) == 3
    crashes = [j for j in _fresh_ledger.jobs.values() if j["outcome"] == "crashed"]
    assert len(crashes) == 3 and sum(j["treg_usd"] for j in crashes) == pytest.approx(0.15)


@respx.mock
async def test_a_crash_during_a_treg_call_keeps_that_call_unconfirmed(settings, gemini, _fresh_ledger):
    started = asyncio.Event()

    async def hang(request):
        started.set()
        await asyncio.sleep(3600)

    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    respx.post(ROUTED_URL).mock(side_effect=hang)
    task = asyncio.create_task(process_job("buyer", INPUT, settings))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (a,) = _open(_fresh_ledger)
    assert a["confirmed_usd"] == 0 and a["assumed_usd"] + a["pending_usd"] == pytest.approx(0.15)
    await _fresh_ledger.expire_attempts(now=a["counts_until"])
    crash = _fresh_ledger.jobs[f"attempt:{a['attempt_id']}"]
    assert crash["unconfirmed_usd"] == pytest.approx(0.15) and crash["treg_usd"] == pytest.approx(0.15)


@respx.mock
async def test_a_lost_progress_write_never_turns_into_a_known_cost(settings, gemini, _fresh_ledger, monkeypatch):
    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    respx.post(ROUTED_URL).mock(return_value=_empty(PAID))
    original = _fresh_ledger.progress

    async def lose_after_paid_call(attempt_id, snapshot):
        if snapshot["confirmed_usd"] >= 0.15:
            raise RuntimeError("write lost")
        await original(attempt_id, snapshot)

    monkeypatch.setattr(_fresh_ledger, "progress", lose_after_paid_call)
    await _crash_in_drafting(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await process_job("buyer", INPUT, settings)
    (a,) = _open(_fresh_ledger)
    assert a["confirmed_usd"] == 0 and a["pending_usd"] == pytest.approx(0.15)  # not claimed as actual
    ceiling = _spend_ceiling(settings, "Acme Labs", "acme.io")
    assert await _fresh_ledger.spend_today() == pytest.approx(ceiling)  # the full hold until safe expiry
    await _fresh_ledger.expire_attempts(now=a["counts_until"])
    crash = _fresh_ledger.jobs[f"attempt:{a['attempt_id']}"]
    assert crash["unconfirmed_usd"] == pytest.approx(0.15) and crash["treg_usd"] == pytest.approx(0.15)


@respx.mock
async def test_partial_execution_settles_what_was_spent(settings, gemini, _fresh_ledger, monkeypatch):
    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    respx.post(ROUTED_URL).mock(return_value=_empty(PAID))

    async def broken(*args, **kwargs):
        raise RuntimeError("bug after the search")

    monkeypatch.setattr(agent_module, "write_outreach", broken)
    with pytest.raises(RuntimeError):
        await process_job("buyer", INPUT, settings)
    row = _fresh_ledger.jobs[_tag()]
    assert row["outcome"] == "failed" and row["treg_usd"] == pytest.approx(0.15) and not _open(_fresh_ledger)


@respx.mock
async def test_an_attempt_past_its_time_limit_fails_and_settles(settings, gemini, _fresh_ledger):
    async def hang(request):
        await asyncio.sleep(3600)

    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    respx.post(ROUTED_URL).mock(side_effect=hang)
    with pytest.raises(TimeoutError):
        await process_job("buyer", INPUT, dataclasses.replace(settings, attempt_timeout_s=0.2))
    row = _fresh_ledger.jobs[_tag()]
    assert row["outcome"] == "failed" and row["unconfirmed_usd"] == pytest.approx(0.15) and not _open(_fresh_ledger)


@respx.mock
async def test_a_failing_settle_keeps_the_hold_and_expiry_records_it(settings, gemini):
    class NoSettle(MemoryLedger):
        async def settle(self, attempt_id, row, snapshot):
            raise RuntimeError("db down")

    led = NoSettle()
    ledger_module.set_ledger(led)
    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    respx.post(ROUTED_URL).mock(return_value=_empty({"X-Treg-Cost-Micro": "20000"}))
    assert "Jane" in await process_job("buyer", INPUT, settings)  # the paid job still delivers
    (a,) = _open(led)
    assert await led.spend_today() == pytest.approx(_spend_ceiling(settings, "Acme Labs", "acme.io"))
    await led.expire_attempts(now=a["counts_until"])
    assert led.jobs[f"attempt:{a['attempt_id']}"]["treg_usd"] == pytest.approx(0.02) and not _open(led)


@respx.mock
async def test_a_cached_rerun_and_an_invalid_input_reserve_nothing(settings, gemini, _fresh_ledger):
    respx.post(APOLLO_URL).mock(side_effect=_apollo())
    respx.post(ROUTED_URL).mock(return_value=_empty())
    first = await process_job("buyer", INPUT, settings)
    attempts = len(_fresh_ledger.attempts)
    assert await process_job("buyer", INPUT, settings) == first
    with pytest.raises(ValueError):
        await process_job("buyer", {"company": ""}, settings)
    assert len(_fresh_ledger.attempts) == attempts and not _open(_fresh_ledger)


# --- PostgreSQL: two agents, one database ---------------------------------------------------------------------

async def _pg_pair():
    from app.ledger import PostgresLedger

    dsn = os.environ["TEST_DATABASE_URL"]
    a, b = PostgresLedger(dsn), PostgresLedger(dsn)
    pool = await a._p()
    await b._p()
    await pool.execute("DELETE FROM spend_attempts WHERE job_tag LIKE 'test-bl34-%'")
    await pool.execute("DELETE FROM job_costs WHERE tag LIKE 'test-bl34-%' OR tag LIKE 'attempt:test-bl34-%' "
                       "OR tag LIKE 'rollback-hold:test-bl34-%'")
    return a, b, pool


async def _pg_close(a, b, pool):
    await pool.execute("DELETE FROM spend_attempts WHERE job_tag LIKE 'test-bl34-%'")
    await pool.execute("DELETE FROM job_costs WHERE tag LIKE 'test-bl34-%' OR tag LIKE 'attempt:test-bl34-%' "
                       "OR tag LIKE 'rollback-hold:test-bl34-%'")
    await a.close()
    await b.close()


@requires_pg
async def test_postgres_parallel_attempts_from_two_agents_hold_the_cap():
    standard, pro, pool = await _pg_pair()
    try:
        base = await standard.spend_today()
        cap = base + 0.5
        grants = await asyncio.gather(*[(pro if i % 2 else standard).reserve(
            f"test-bl34-a{i}", "test-bl34-job", "standard", 0.15, cap, LIFETIME) for i in range(8)])
        assert sum(grants) == pytest.approx(0.5)
        assert sorted(round(g, 4) for g in grants) == [0.0] * 4 + [0.05, 0.15, 0.15, 0.15]
        assert await pro.spend_today() == pytest.approx(cap)
    finally:
        await _pg_close(standard, pro, pool)


@requires_pg
async def test_postgres_reserve_waits_for_the_other_agents_reservation():
    import asyncpg

    from app.ledger import SPEND_LOCK_KEY

    led, other_led, pool = await _pg_pair()
    other = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    try:
        cap = await led.spend_today() + 0.15
        tx = other.transaction()
        await tx.start()
        await other.execute("SELECT pg_advisory_xact_lock($1)", SPEND_LOCK_KEY)
        waiter = asyncio.create_task(led.reserve("test-bl34-waiter", "test-bl34-w", "standard", 0.15, cap, LIFETIME))
        await asyncio.sleep(0.3)
        assert not waiter.done()  # blocked: it must not read the total while the other agent reserves
        await other.execute("INSERT INTO spend_attempts (attempt_id, job_tag, tier, reserved_usd, counts_until) "
                            "VALUES ('test-bl34-holder', 'test-bl34-h', 'pro', 0.15, now() + interval '1 hour')")
        await tx.commit()
        assert await asyncio.wait_for(waiter, 5) == 0.0
    finally:
        await other.close()
        await _pg_close(led, other_led, pool)


@requires_pg
async def test_postgres_retry_settlement_never_touches_the_earlier_attempt():
    standard, pro, pool = await _pg_pair()
    try:
        cap = await standard.spend_today() + 0.5
        await standard.reserve("test-bl34-first", "test-bl34-T", "standard", 0.2, cap, LIFETIME)
        await standard.progress("test-bl34-first", _snap(confirmed=0.15))
        await pro.reserve("test-bl34-retry", "test-bl34-T", "standard", 0.2, cap, LIFETIME)
        assert await pro.settle("test-bl34-retry", _row("test-bl34-T"), _snap()) == "settled"
        first = await pool.fetchrow("SELECT status, reserved_usd, confirmed_usd FROM spend_attempts "
                                    "WHERE attempt_id = 'test-bl34-first'")
        assert dict(first) == {"status": "open", "reserved_usd": 0.2, "confirmed_usd": 0.15}
        assert await standard.spend_today() == pytest.approx(cap - 0.3)
        assert await standard.settle("test-bl34-nope", _row("test-bl34-U"), _snap()) == "unreserved"
        with pytest.raises(KeyError):
            await standard.progress("test-bl34-retry", _snap())  # settled: no more writes
    finally:
        await _pg_close(standard, pro, pool)


@requires_pg
async def test_postgres_midnight_holds_and_concurrent_expiry():
    standard, pro, pool = await _pg_pair()
    try:
        base = await standard.spend_today()
        await standard.reserve("test-bl34-late", "test-bl34-late", "standard", 0.2, base + 1, LIFETIME)
        await standard.reserve("test-bl34-old", "test-bl34-old", "standard", 0.1, base + 1, LIFETIME)
        await standard.progress("test-bl34-old", _snap(confirmed=0.04, pending=0.03))
        # 'late': started 23:59:50 yesterday, still running -> counts today. 'old': crashed the day before.
        await pool.execute("UPDATE spend_attempts SET created_at = date_trunc('day', now()) - interval '10 seconds', "
                           "counts_until = date_trunc('day', now()) + interval '1 day' WHERE attempt_id = "
                           "'test-bl34-late'")
        await pool.execute("UPDATE spend_attempts SET created_at = now() - interval '2 days', counts_until = "
                           "date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc' WHERE attempt_id = "
                           "'test-bl34-old'")
        assert await pro.spend_today() == pytest.approx(base + 0.2)
        expired = await asyncio.gather(standard.expire_attempts(), pro.expire_attempts())
        assert sorted(expired) == [0, 1]  # each row expired exactly once
        crash = await pool.fetchrow("SELECT treg_usd, unconfirmed_usd, outcome, finished_at < date_trunc('day', now() "
                                    "AT TIME ZONE 'utc') AT TIME ZONE 'utc' AS before_today FROM job_costs WHERE tag = "
                                    "'attempt:test-bl34-old'")
        assert crash["outcome"] == "crashed" and crash["before_today"]
        assert crash["treg_usd"] == pytest.approx(0.07) and crash["unconfirmed_usd"] == pytest.approx(0.03)
        assert await standard.spend_today() == pytest.approx(base + 0.2)  # not counted again today
        assert await standard.settle("test-bl34-old", _row("test-bl34-old"), _snap()) == "expired"
    finally:
        await _pg_close(standard, pro, pool)


@requires_pg
async def test_postgres_rollback_hand_over_keeps_open_holds_visible_to_the_old_code():
    standard, pro, pool = await _pg_pair()
    old_spend_today = ("SELECT coalesce(sum(treg_usd + enrich_usd), 0) FROM job_costs WHERE finished_at >= "
                       "date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'")  # a97ca3e's query
    try:
        cap = await standard.spend_today() + 1
        await standard.reserve("test-bl34-live", "test-bl34-live", "standard", 0.2, cap, LIFETIME)
        await standard.progress("test-bl34-live", _snap(confirmed=0.05))
        before_new = await standard.spend_today()
        before_old = float(await pool.fetchval(old_spend_today))
        summary = await standard.hand_over_open_holds()
        assert summary["attempts"] >= 1 and summary["held_usd"] >= 0.2
        assert float(await pool.fetchval(old_spend_today)) == pytest.approx(before_old + 0.2)
        assert await standard.spend_today() == pytest.approx(before_new)  # redeploy: no double count
        row = await pool.fetchrow("SELECT treg_usd, unconfirmed_usd, finished_at > now() AS ahead FROM job_costs "
                                  "WHERE tag = 'rollback-hold:test-bl34-live'")
        assert row["treg_usd"] == pytest.approx(0.2) and row["unconfirmed_usd"] == pytest.approx(0.15) and row["ahead"]
    finally:
        await _pg_close(standard, pro, pool)


@requires_pg
async def test_postgres_schema_upgrade_is_idempotent_and_runs_once_per_start():
    from app.ledger import PostgresLedger

    dsn = os.environ["TEST_DATABASE_URL"]
    leds = [PostgresLedger(dsn) for _ in range(3)]
    try:
        await asyncio.gather(*[led._p() for led in leds])  # both agents starting at once
        since = {await led.identity_v2_since() for led in leds}
        assert len(since) == 1  # set once, never moved by later starts
        legacy = {k: v for k, v in _row("test-bl34-legacy").items() if k not in ("cost_assumed_calls",
                                                                                 "unconfirmed_usd")}
        await leds[0].record_job(legacy)
    finally:
        pool = await leds[0]._p()
        await pool.execute("DELETE FROM job_costs WHERE tag = 'test-bl34-legacy'")
        for led in leds:
            await led.close()


@requires_pg
async def test_rollback_prep_refuses_unfinished_jobs_then_hands_over(capsys):
    import importlib.util
    from pathlib import Path

    from app.job_store import PostgresJobStorage

    spec = importlib.util.spec_from_file_location("rollback_prep", Path(__file__).resolve().parents[1] / "scripts"
                                                  / "ledger_rollback_prep.py")
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    dsn = os.environ["TEST_DATABASE_URL"]
    standard, pro, pool = await _pg_pair()
    jobs = PostgresJobStorage(dsn, table="agent_jobs")
    try:
        await jobs.connect()
        await pool.execute("DELETE FROM agent_jobs WHERE job_id LIKE 'test-bl34-%'")
        await jobs.create_job("test-bl34-job", {"status": "running"})
        await standard.reserve("test-bl34-held", "test-bl34-held", "standard", 0.2, 100.0, LIFETIME)
        assert await prep.run(dsn, apply=True) == 2  # refused: a job is still running
        assert "REFUSED" in capsys.readouterr().out
        assert await pool.fetchval("SELECT status FROM spend_attempts WHERE attempt_id = 'test-bl34-held'") == "open"
        await pool.execute("DELETE FROM agent_jobs WHERE job_id LIKE 'test-bl34-%'")
        assert await prep.run(dsn, apply=False) == 0
        assert await pool.fetchval("SELECT status FROM spend_attempts WHERE attempt_id = 'test-bl34-held'") == "open"
        assert await prep.run(dsn, apply=True) == 0
        assert await pool.fetchval("SELECT status FROM spend_attempts WHERE attempt_id = 'test-bl34-held'") == \
            "expired"
        assert await pool.fetchval("SELECT treg_usd FROM job_costs WHERE tag = 'rollback-hold:test-bl34-held'") == \
            pytest.approx(0.2)
    finally:
        await pool.execute("DELETE FROM agent_jobs WHERE job_id LIKE 'test-bl34-%'")
        await jobs.close()
        await _pg_close(standard, pro, pool)
