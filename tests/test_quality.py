"""Domain resolver, lead scoring and provider router."""

import dataclasses

import httpx
import pytest
import respx

from app.resolver import candidate_domains, registrable_root, resolve_company
from app.router import find_leads, person_key
from app.report import build_result
from app.scoring import CAP_REASON, quality_label, rate, score_lead, score_parts, seniority
from app.treg_client import TregError
from app.verify import apply_verification, verify_lead

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
    assert "did not answer" in r.note


async def test_resolver_does_not_cache_a_failed_probe():
    calls = []

    async def probe(domain):
        calls.append(domain)
        if len(calls) == 1:
            raise TregError("hiccup")
        return ["Acme"] if domain == "acme.io" else []

    first = await resolve_company("Acme", "acme.io", probe, now=1000)
    assert (first.verified, calls) == (False, ["acme.io"])
    again = await resolve_company("Acme", "acme.io", probe, now=1001)  # probes again, not the cached failure
    assert (again.domain, again.verified, calls) == ("acme.io", True, ["acme.io", "acme.io"])
    cached = await resolve_company("Acme", "acme.io", probe, now=1002)  # a completed resolution is cached
    assert cached is again and len(calls) == 2


async def test_resolver_does_not_cache_a_failed_probe_without_domain():
    calls = []

    async def probe(domain):
        calls.append(domain)
        raise RuntimeError("timeout")

    r = await resolve_company("Acme", None, probe, now=1000)
    assert not r.verified and "did not answer" in r.note and "name matching" in r.note
    await resolve_company("Acme", None, probe, now=1001)
    assert len(calls) == 2


async def test_resolver_caches_a_genuine_not_found():
    calls = []

    async def probe(domain):
        calls.append(domain)
        return []

    r = await resolve_company("Acme", "acme.io", probe, now=1000)
    assert not r.verified and "Could not confirm acme.io" in r.note
    n = len(calls)
    assert await resolve_company("Acme", "acme.io", probe, now=1001) is r and len(calls) == n


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


def test_breakdown_adds_up_and_every_component_has_a_reason():
    score, reasons, parts = score_parts(_lead("Jane S***h", "Head of BD", "https://acme.io"), "BD", "domain")
    assert parts == {"role": 35, "seniority": 20, "name": 5, "profile": 6, "company": 10}
    assert score == sum(parts.values()) == 76
    assert {"title matches 'BD'", "senior role", "last name masked by the provider",
            "no individual profile (company website only)", "company confirmed by domain"} <= set(reasons)
    _, reasons, _ = score_parts(_lead(url=""), None, "none")
    assert "full name" in reasons and "no profile link" in reasons


@pytest.mark.parametrize("evidence, points, reason", [
    ("domain", 10, "company confirmed by domain"),
    ("given domain", 6, "company matched by name (domain given but not confirmed)"),
    ("name", 6, "company matched by name"),
    ("none", 0, "company not confirmed")])
def test_company_evidence_points_and_reason(evidence, points, reason):
    _, reasons, parts = score_parts(_lead(), "BD", evidence)
    assert parts["company"] == points and reason in reasons
    assert (evidence == "domain") == ("company confirmed by domain" in reasons)


@pytest.mark.parametrize("name, url, label", [
    ("Jane S***h", "https://acme.io", "Medium"),                       # masked, company site: capped
    ("Jane S***h", "", "Medium"),                                      # masked, no link: capped
    ("Jane S***h", "https://www.linkedin.com/in/jane", "High"),        # masked but own profile
    ("Jane Smith", "https://acme.io", "High"),                         # full name, no profile
    ("Jane Smith", "https://www.linkedin.com/in/jane", "High")])
def test_unidentifiable_leads_are_never_high(name, url, label):
    lead = _lead(name, "Head of Business Development", url)
    score, reasons, parts = score_parts(lead, "business development", "domain")
    assert score >= 70
    rate(lead, score, reasons, parts)
    assert lead["quality"] == label and lead["quality_score"] == score
    assert (CAP_REASON in lead["match_reasons"]) == (label == "Medium")


def test_cap_survives_verification_without_duplicate_reasons():
    lead = _lead("Jane S***h", "Head of Business Development", "https://acme.io")
    rate(lead, *score_parts(lead, "business development", "domain"))
    lead["_signals"] = {"providers": ["apollo"], "evidence": "domain", "refreshed_at": "2026-10-01T00:00:00Z"}
    apply_verification(lead, verify_lead(lead, "acme.io", True))
    assert lead["verification"]["status"] == "Verified" and lead["quality_score"] == 81
    assert lead["quality"] == "Medium" and lead["match_reasons"].count(CAP_REASON) == 1
    assert lead["score_breakdown"]["employment_check"] == 5


def test_report_separates_individual_profiles_from_company_sites():
    leads = []
    for name, url in (("Jane S***h", "https://acme.io"), ("Max Mustermann", "https://www.linkedin.com/in/max"),
                      ("Ann S***e", "")):
        lead = _lead(name, "Head of BD", url)
        rate(lead, *score_parts(lead, "BD", "domain"))
        leads.append(lead)
    result = build_result("Acme", "BD", leads, ["Hi"] * 3)
    data = __import__("json").loads(result.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert [lead["profile_type"] for lead in data["leads"]] == ["company_website", "linkedin", "none"]
    assert data["summary"]["with_linkedin"] == 1 and data["summary"]["with_full_name"] == 1
    assert "**Identifiable:** 1 of 3 with a full name · 1 of 3 with an individual LinkedIn profile" in result
    assert "- **Profile:** company site only: [acme.io](https://acme.io)" in result
    assert "- **Profile:** [LinkedIn](https://www.linkedin.com/in/max)" in result
    assert "*Company site only* means" in result and "| Employment |" in result
    assert "- **Points:** role 35 · seniority 20 · name 5 · profile 6 · company 10" in result


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


APOLLO_JANE = {"people": [{"first_name": "Jane", "last_name_obfuscated": "S***h", "title": "Head of BD",
                           "organization": {"name": "Acme"}}]}


@pytest.mark.parametrize("verified, evidence, company_points", [(True, "domain", 10), (False, "given domain", 6)])
@respx.mock
async def test_router_claims_domain_evidence_only_for_a_confirmed_domain(settings, verified, evidence,
                                                                          company_points):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json=APOLLO_JANE))
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    res = await find_leads(dataclasses.replace(settings, min_leads_before_paid=1), "Acme", "BD",
                           _resolved(verified=verified), "t6")
    lead = res.leads[0]
    assert lead["_signals"]["evidence"] == evidence
    assert lead["score_breakdown"]["company"] == company_points
    assert ("company confirmed by domain" in lead["match_reasons"]) == verified
    # Verification agrees: the employer is only confirmed by a confirmed domain; Apollo still implies current.
    v = verify_lead(lead, "acme.io", verified)
    assert v["checks"]["company"] == verified and v["checks"]["current"] is True
    assert ("employer confirmed by domain" in v["notes"]) == verified


@respx.mock
async def test_router_unconfirmed_domain_routed_search_is_not_domain_evidence(settings):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": []}))
    respx.post(ROUTED_URL).mock(side_effect=[
        httpx.Response(200, json={"output": {"people": [{"name": "Max Mustermann", "title": "Head of BD",
                                                          "company": "Acme"}]}}),
        httpx.Response(200, json={"output": {"people": []}})])
    res = await find_leads(settings, "Acme", "BD", _resolved(verified=False), "t7")
    assert [lead["_signals"]["evidence"] for lead in res.leads] == ["given domain"]
    assert "company confirmed by domain" not in res.leads[0]["match_reasons"]


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
