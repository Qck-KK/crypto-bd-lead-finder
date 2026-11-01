import json

from google.genai import errors

from app.drafts import LINKEDIN_NOTE_MAX, OutreachContext, build_prompt, region_of, write_outreach
from conftest import FakeGemini

LEADS = [
    {"name": "Alice Nakamoto", "title": "Head Business Development Banking and Capital Markets EMEA",
     "company": "Example Labs", "profile_url": "", "source": "treg.to"},
    {"name": "Bob Lee", "title": "", "company": "Example Labs", "profile_url": "", "source": "treg.to"},
]
CTX = OutreachContext(pitch="our L2 bridge", sender_name="Alex Kim", sender_company="NodeMetrics", tone="formal")


def _resp(items):
    return FakeGemini(text=json.dumps({"drafts": items}))


async def test_one_call_with_personalization_context():
    fake = _resp([{"index": 0, "subject": "Capital markets x L2", "email": "Hi Alice", "linkedin_note": "Hi"},
                  {"index": 1, "subject": "Quick idea", "email": "Hi Bob", "linkedin_note": "Hi Bob"}])
    out, used_llm = await write_outreach(LEADS, CTX, api_key="k", model="gemini-2.5-flash", client=fake)
    assert used_llm and [o["email"] for o in out] == ["Hi Alice", "Hi Bob"]
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["model"] == "gemini-2.5-flash" and call["config"].response_mime_type == "application/json"
    prompt = call["contents"]
    for expected in ("our L2 bridge", "Alex Kim", "NodeMetrics", "formal", "EMEA", "senior"):
        assert expected in prompt
    assert "profile_url" not in prompt


def test_region_and_prompt_fields():
    assert region_of("Head of BD, APAC") == "APAC"
    assert region_of("Business Development") is None
    people = json.loads(build_prompt(LEADS, OutreachContext()).split("\n", 3)[-1])
    assert set(people[0]) == {"index", "name", "title", "company", "seniority", "region"}


async def test_contact_details_scrubbed_and_lengths_enforced():
    fake = _resp([{"index": 0, "subject": "S" * 90, "email": "Hi Alice, call me at +1 415 555 0199 or a@b.io",
                   "linkedin_note": "Hi Alice. " + "word " * 100}])
    out, _ = await write_outreach(LEADS[:1], CTX, api_key="k", model="m", client=fake)
    assert "555" not in out[0]["email"] and "@" not in out[0]["email"]
    assert len(out[0]["subject"]) <= 60
    assert len(out[0]["linkedin_note"]) <= LINKEDIN_NOTE_MAX


async def test_api_error_falls_back_to_signed_templates():
    fake = FakeGemini(exc=errors.APIError(429, {"error": {"message": "quota"}}))
    out, used_llm = await write_outreach(LEADS, CTX, api_key="k", model="m", client=fake)
    assert not used_llm
    assert out[0]["email"].startswith("Hi Alice,") and out[0]["email"].endswith("Alex Kim, NodeMetrics")
    assert "our L2 bridge" in out[0]["linkedin_note"] and len(out[0]["linkedin_note"]) <= LINKEDIN_NOTE_MAX
    assert out[1]["email"].startswith("Hi Bob,")


async def test_incomplete_entries_and_bad_json_fall_back():
    fake = _resp([{"index": 1, "subject": "x", "email": "Hi Bob", "linkedin_note": "n"},
                  {"index": 0, "subject": "missing email"}])
    out, _ = await write_outreach(LEADS, CTX, api_key="k", model="m", client=fake)
    assert out[0]["email"].startswith("Hi Alice,") and out[1]["email"] == "Hi Bob"

    out, used_llm = await write_outreach(LEADS, CTX, api_key="k", model="m", client=FakeGemini(text="not json"))
    assert not used_llm and len(out) == 2


async def test_no_key_uses_templates_without_calling():
    out, used_llm = await write_outreach(LEADS, OutreachContext(), api_key="", model="m")
    assert not used_llm and len(out) == 2 and "Best," not in out[0]["email"]
