"""Apollo-first search: domain guessing, title synonyms, fallback to the routed search."""

import json

import httpx
import respx

from app import drafts as drafts_module
from app.agent import process_job
from app.compliance import guess_domains, role_terms, to_public_lead
from conftest import FakeGemini

APOLLO_URL = "https://treg.test/call/apollo.people.search"
ROUTED_URL = "https://treg.test/call/treg.people.search"


def _apollo_row(first, title, org="Chainlink Labs"):
    # Shape of treg's apollo.people.search example (free search: masked last name, no contact data).
    return {"id": "x", "first_name": first, "last_name_obfuscated": "S***h", "title": title,
            "has_email": True, "has_direct_phone": "Yes", "organization": {"name": org, "has_phone": True}}


def _extract_json(result):
    return json.loads(result.split("```json\n", 1)[1].split("\n```", 1)[0])


def test_guess_domains_tries_given_then_name_based():
    assert guess_domains("Chainlink Labs", "https://www.chain.link/x") == [
        "chain.link", "chainlinklabs.com", "chainlink.com", "chainlinklabs.io", "chainlink.io"]
    assert guess_domains("Uniswap")[:2] == ["uniswap.com", "uniswap.io"]


def test_role_terms_expand_bd():
    terms = role_terms("BD")
    assert terms[0] == "bd" and "business development" in terms and "market development" in terms
    assert role_terms(None) == []


def test_apollo_row_maps_to_public_fields_only():
    lead = to_public_lead(_apollo_row("Jane", "Business Development Lead"), provider="apollo",
                          company_fallback="X", company_domain="chainlinklabs.com")
    assert lead == {"name": "Jane S***h", "title": "Business Development Lead", "company": "Chainlink Labs",
                    "profile_url": "https://chainlinklabs.com", "source": "treg.to (apollo)"}


def _chainlink_apollo(calls):
    """Apollo mock: nothing under chain.link; Chainlink Labs under chainlinklabs.com."""
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["q_organization_domains_list"] != ["chainlinklabs.com"]:
            return httpx.Response(200, json={"people": [], "total_entries": 0})
        if "person_titles" not in body:  # resolver probe
            return httpx.Response(200, json={"people": [_apollo_row("Zed", "CEO")], "total_entries": 500})
        assert "business development" in body["person_titles"]
        return httpx.Response(200, json={"people": [
            _apollo_row("Jane", "Business Development Lead"),
            _apollo_row("Ann", "Head of BD, EMEA"),
            _apollo_row("Bob", "Software Engineer"),               # off-role
            _apollo_row("Cid", "BD Manager", org="Link Metal Co"),  # other company
        ], "total_entries": 4})
    return handler


@respx.mock
async def test_resolves_domain_then_searches_apollo(settings, monkeypatch):
    calls = []
    respx.post(APOLLO_URL).mock(side_effect=_chainlink_apollo(calls))
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(
        text=json.dumps({"drafts": [{"index": 0, "draft": "Hi Jane"}, {"index": 1, "draft": "Hi Ann"}]})))

    result = await process_job("p1", {"company": "Chainlink Labs", "role": "business development",
                                      "company_domain": "chain.link"}, settings)
    data = _extract_json(result)
    assert [lead["name"] for lead in data["leads"]] == ["Jane S***h", "Ann S***h"]
    assert data["leads"][0]["quality"] in ("High", "Medium") and data["leads"][0]["match_reasons"]
    assert [c["q_organization_domains_list"][0] for c in calls] == ["chain.link", "chainlinklabs.com",
                                                                    "chainlinklabs.com"]
    assert data["resolution"] == {"given_domain": "chain.link", "domain": "chainlinklabs.com", "verified": True,
                                  "note": "chain.link is not indexed by the data provider; used chainlinklabs.com."}
    # Only 2 leads < MIN_LEADS_BEFORE_PAID (5): the paid routed search tries the verified domain, not the name.
    assert routed.call_count == 1
    assert json.loads(routed.calls.last.request.content) == {"limit": 25, "company_domain": "chainlinklabs.com"}
    assert "has_email" not in result and "Yes" not in result
    assert "## Overview" in result and "## Search notes" in result and "partly masked" in result


@respx.mock
async def test_enough_free_leads_skip_paid_search(settings, monkeypatch):
    import dataclasses

    respx.post(APOLLO_URL).mock(side_effect=_chainlink_apollo([]))
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))
    await process_job("p1b", {"company": "Chainlink Labs", "role": "BD", "company_domain": "chain.link"},
                      dataclasses.replace(settings, min_leads_before_paid=2))
    assert not routed.called


@respx.mock
async def test_falls_back_to_routed_search_when_apollo_finds_nobody(settings, monkeypatch):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": [], "total_entries": 0}))
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": [
        {"name": "Dee Lee", "title": "Partnerships Lead", "company": "Acme"}]}}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="not json"))

    result = await process_job("p2", {"company": "Acme", "role": "partnerships"}, settings)
    assert routed.called
    assert _extract_json(result)["leads"][0]["name"] == "Dee Lee"


@respx.mock
async def test_apollo_error_falls_back(settings, monkeypatch):
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(403, json={"error": "no apollo"},
                                                            headers={"X-Treg-Error": "1"}))
    routed = respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))
    await process_job("p3", {"company": "Acme"}, settings)
    assert routed.called
