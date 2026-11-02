import json

import httpx
import pytest
import respx

from app import drafts as drafts_module
from app.agent import INPUT_SCHEMA, input_schema, process_job
from conftest import LEAKY_ROW, PRIVATE_STRINGS, FakeGemini, mock_apollo_empty

TREG_URL = "https://treg.test/call/treg.people.search"


def _extract_json(result: str) -> dict:
    return json.loads(result.split("```json\n", 1)[1].split("\n```", 1)[0])


@respx.mock
async def test_full_job_output(settings, monkeypatch):
    rows = ([LEAKY_ROW]
            + [{"name": f"Engineer {i}", "job_title": "Software Engineer"} for i in range(5)]
            + [{"name": f"Person {i}", "job_title": "Business Development Lead", "email": f"p{i}@gmail.com"}
               for i in range(15)])
    mock_apollo_empty(respx)
    route = respx.post(TREG_URL).mock(return_value=httpx.Response(
        200, json={"output": {"people": rows}}, headers={"X-Treg-Served-By": "apollo"}))
    fake = FakeGemini(text=json.dumps({"drafts": [
        {"index": i, "subject": f"Subject {i}", "email": f"Draft {i}", "linkedin_note": f"Note {i}"}
        for i in range(10)]}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: fake)

    result = await process_job("purchaser-1", {"company": "  Example   Labs ", "role": "BD",
                                               "company_domain": "https://www.ExampleLabs.io/about"}, settings)

    body = json.loads(route.calls.last.request.content)
    assert body["company_domain"] == "examplelabs.io"
    assert body == {"company_domain": "examplelabs.io", "limit": 25}

    data = _extract_json(result)
    assert data["count"] == 10 and len(data["leads"]) == 10
    assert not any(lead["name"].startswith("Engineer") for lead in data["leads"])  # role filter
    assert data["leads"][0]["outreach_draft"] == "Draft 0"
    assert data["leads"][0]["outreach"] == {"subject": "Subject 0", "email": "Draft 0", "linkedin_note": "Note 0"}
    assert set(data["leads"][0]) == {"name", "title", "company", "profile_url", "source", "outreach_draft",
                                     "outreach", "quality_score", "quality", "match_reasons", "verification"}
    assert data["leads"][0]["verification"]["status"] in ("Verified", "Likely", "Unverified")
    assert "_signals" not in result and data["tier"] == "standard"
    assert data["data_source"] == "treg.to"
    assert data["usage_notice"] == "For B2B professional outreach only."

    for private in PRIVATE_STRINGS + ["@gmail.com"]:
        assert private not in result, private
    assert result.rstrip().endswith("Data source: treg.to. For B2B professional outreach only.")
    assert "Jev" not in result and "TypeSafe" not in result


@respx.mock
async def test_no_results(settings, monkeypatch):
    mock_apollo_empty(respx)
    respx.post(TREG_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: pytest.fail("LLM must not be called"))
    result = await process_job("p", {"company": "Nobody Inc"}, settings)
    assert "## No leads found" in result and "website domain" in result
    assert _extract_json(result)["count"] == 0


async def test_company_required(settings):
    with pytest.raises(ValueError):
        await process_job("p", {"company": "   "}, settings)


def test_input_schema_mentions_source_and_limit():
    ids = [f["id"] for f in INPUT_SCHEMA["input_data"]]
    assert ids == ["company", "company_domain", "role", "pitch", "sender_name", "sender_company", "tone", "notice"]
    notice = INPUT_SCHEMA["input_data"][-1]["data"]["description"]
    assert "treg.to" in notice and "B2B professional outreach only" in notice and "10" in notice
    assert "quality score" in notice and "masked" in notice and "verification" in notice
    # The standard agent cannot know whether Pro enrichment is on, so it makes no Pro promise.
    assert "Pro" not in notice
    off = input_schema("pro")["input_data"][-1]["data"]["description"]
    assert "Enrichment is currently off" in off and "masked" in off and "LinkedIn profiles" not in off
    on = input_schema("pro", 4)["input_data"][-1]["data"]["description"]
    assert "masked names and missing LinkedIn profiles for up to 4 people" in on and "currently off" not in on
    tone = INPUT_SCHEMA["input_data"][6]
    assert tone["type"] == "option" and tone["data"]["values"] == ["friendly", "formal", "concise"]
    domain_field = INPUT_SCHEMA["input_data"][1]
    assert "recommended" in domain_field["name"] and "Without it" in domain_field["data"]["description"]
