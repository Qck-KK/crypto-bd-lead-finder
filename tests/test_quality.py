"""Domain resolver, lead scoring and provider router."""

import dataclasses

import httpx
import pytest
import respx

from app.resolver import candidate_domains, registrable_root, resolve_company
from app.router import find_leads, person_key
from app.scoring import quality_label, score_lead, seniority
from app.treg_client import TregError

APOLLO_URL = "https://treg.test/call/apollo.people.search"
ROUTED_URL = "https://treg.test/call/treg.people.search"


# --- resolver ---------------------------------------------------------------

def test_registrable_root_and_candidates():
    assert registrable_root("careers.chain.link") == "chain.link"
    assert registrable_root("jobs.example.co.uk") == "example.co.uk"
    assert candidate_domains("Chainlink Labs", "https://careers.chain.link/jobs")[:3] == [
        "careers.chain.link", "chain.link", "chainlinklabs.com"]


async def test_resolver_accepts_only_matching_org_and_caches():
    seen = []

    async def probe(domain):
        seen.append(domain)
        return {"chainlink.com": ["Chainlink Plumbing Inc"], "chainlinklabs.com": ["Chainlink Labs"]}.get(domain, [])

    r = await resolve_company("Chainlink Labs", None, probe, now=1000)
    assert (r.domain, r.verified) == ("chainlinklabs.com", True)
    assert "found the company under chainlinklabs.com" in r.note
    again = await resolve_company("chainlink labs", None, probe, now=2000)
    assert again is r and seen == ["chainlinklabs.com"]  # first guess hit; cached afterwards


async def test_resolver_confirms_given_domain():
    async def probe(domain):
        return ["Uniswap"] if domain == "uniswap.org" else []

    r = await resolve_company("Uniswap Labs", "uniswap.org", probe)
    assert (r.domain, r.verified, r.note) == ("uniswap.org", True, "Confirmed uniswap.org with the data provider.")


async def test_resolver_rejects_look_alike_and_keeps_given_domain():
    async def probe(domain):
        return ["Chainlink Plumbing Inc"]

    r = await resolve_company("Chainlink Labs", "chain.link", probe)
    assert (r.domain, r.verified) == ("chain.link", False)
    assert "Could not confirm chain.link" in r.note


async def test_resolver_survives_probe_errors():
    async def probe(domain):
        raise TregError("down")

    r = await resolve_company("Acme", "acme.io", probe)
    assert (r.domain, r.verified) == ("acme.io", False)


# --- scoring ----------------------------------------------------------------

def _lead(name="Jane Smith", title="Head of Business Development", url="https://www.linkedin.com/in/jane"):
    return {"name": name, "title": title, "company": "Acme", "profile_url": url, "source": "treg.to"}


def test_scores_reward_role_fit_seniority_full_name_and_linkedin():
    best, reasons = score_lead(_lead(), "business development", "domain")
    assert best == 100 and quality_label(best) == "High"
    assert "senior role" in reasons and "LinkedIn profile" in reasons

    masked, reasons = score_lead(_lead("Jane S***h", "Business Development", "https://acme.io"), "BD", "domain")
    assert quality_label(masked) == "Medium" and "last name masked by the provider" in reasons

    untitled, _ = score_lead(_lead("Jo Bloggs", "", "https://acme.io"), None, "none")
    assert untitled < 30 and quality_label(untitled) == "Low"


def test_seniority_levels():
    assert seniority("VP, Partnerships") == "senior"
    assert seniority("Co-Founder") == "senior"
    assert seniority("Business Development Manager") == "mid"
    assert seniority("Analyst") == "other"
    assert seniority("Talent Acquisition Partner") == "other"
    assert seniority("Managing Partner") == "senior"


# --- router -----------------------------------------------------------------

def test_masked_and_full_names_share_a_person_key():
    assert person_key({"name": "Jane S***h", "company": "Acme Labs"}) == person_key(
        {"name": "Jane Smith", "company": "Acme Labs"})
    assert person_key({"name": "Jane Stone", "company": "Acme Labs"}) != person_key(
        {"name": "Jane Smith", "company": "Acme Labs"})


def _resolved(domain="acme.io", verified=True):
    from app.resolver import ResolvedCompany
    return ResolvedCompany(name="Acme", given_domain=domain, domain=domain, verified=verified)


@respx.mock
async def test_router_merges_providers_and_keeps_the_better_record(settings):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": [
        {"first_name": "Jane", "last_name_obfuscated": "S***h", "title": "Head of BD",
         "organization": {"name": "Acme"}}]}))
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": [
        {"name": "Jane Smith", "title": "Head of BD", "company": "Acme",
         "linkedin_url": "https://www.linkedin.com/in/jane-smith", "email": "jane@gmail.com"}]}},
        headers={"X-Treg-Cost-Micro": "20000", "X-Treg-Served-By": "prov"}))

    res = await find_leads(settings, "Acme", "BD", _resolved(), "t1")
    assert [lead["name"] for lead in res.leads] == ["Jane Smith"]
    assert res.leads[0]["profile_url"] == "https://www.linkedin.com/in/jane-smith"
    assert "email" not in res.leads[0]
    assert res.spent_usd == pytest.approx(0.02)
    assert [s.provider for s in res.steps] == ["apollo", "treg routed (domain) via prov"]


@respx.mock
async def test_router_respects_budget(settings):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}))
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}},
                                                                     headers={"X-Treg-Cost-Micro": "150000"}))
    res = await find_leads(settings, "Acme", None, _resolved(verified=False), "t2")
    # The domain search spent the whole 0.15 budget, so the name search is skipped.
    assert routed.call_count == 1
    assert res.steps[-1].error == "job budget used up"
    assert routed.calls.last.request.headers["X-Treg-Route-Max-Cost"] == "0.1500"


@respx.mock
async def test_router_caps_each_call_by_remaining_budget(settings):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}))
    routed = respx.post(ROUTED_URL).mock(side_effect=[
        httpx.Response(200, json={"output": {"people": []}}, headers={"X-Treg-Cost-Micro": "100000"}),
        httpx.Response(200, json={"output": {"people": []}}, headers={"X-Treg-Cost-Micro": "0"}),
    ])
    await find_leads(settings, "Acme", None, _resolved(verified=False), "t3")
    assert routed.calls[1].request.headers["X-Treg-Route-Max-Cost"] == "0.0500"


@respx.mock
async def test_router_fails_the_job_when_every_provider_fails(settings):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(500, json={"error": "x"}))
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(402, json={"error": "insufficient_balance"},
                                                            headers={"X-Treg-Error": "1"}))
    with pytest.raises(TregError):
        await find_leads(dataclasses.replace(settings), "Acme", None, _resolved(), "t4")


@respx.mock
async def test_low_scores_are_left_out(settings):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}))
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": [
        {"name": "No Title Person"}, {"name": "Real Person", "title": "Head of Partnerships"}]}}))
    res = await find_leads(dataclasses.replace(settings, min_leads_before_paid=1), "Acme", None,
                           _resolved(verified=True), "t5")
    assert [lead["name"] for lead in res.leads] == ["Real Person"]
    assert res.dropped_low_score == 1
