import asyncio
import inspect
import json
from types import SimpleNamespace

import httpx
from google.genai import errors

from app.drafts import (DEFAULT_TIMEOUT_S, LINKEDIN_NOTE_MAX, OutreachContext, build_prompt, region_of,
                        write_outreach)
from app.settings import Settings
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


class _HangingGemini:
    """A model call that never returns (the SDK has no timeout of its own)."""

    def __init__(self):
        self.started = self.cancelled = False
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, **kwargs):
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_hanging_model_call_times_out_to_templates(caplog):
    fake = _HangingGemini()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    out, used_llm = await asyncio.wait_for(
        write_outreach(LEADS, CTX, api_key="k", model="m", client=fake, timeout_s=0.05), timeout=5)
    assert loop.time() - t0 < 2
    assert fake.started and fake.cancelled  # the pending call is cancelled, not leaked
    assert not used_llm and out[0]["email"].startswith("Hi Alice,") and out[1]["email"].startswith("Hi Bob,")
    assert "timed out" in caplog.text


async def test_transport_error_falls_back_to_templates():
    fake = FakeGemini(exc=httpx.ConnectError("connection refused"))
    out, used_llm = await write_outreach(LEADS, CTX, api_key="k", model="m", client=fake)
    assert not used_llm and len(out) == 2 and out[0]["email"].startswith("Hi Alice,")


def test_default_timeout_and_setting(monkeypatch):
    assert inspect.signature(write_outreach).parameters["timeout_s"].default == DEFAULT_TIMEOUT_S == 45.0
    monkeypatch.delenv("GEMINI_TIMEOUT_S", raising=False)
    assert Settings.from_env().gemini_timeout_s == 45.0
    monkeypatch.setenv("GEMINI_TIMEOUT_S", "12.5")
    assert Settings.from_env().gemini_timeout_s == 12.5


async def test_no_key_uses_templates_without_calling():
    out, used_llm = await write_outreach(LEADS, OutreachContext(), api_key="", model="m")
    assert not used_llm and len(out) == 2 and "Best," not in out[0]["email"]


# --- the fallback is visible (draft_source) ------------------------------------------------------------------

async def test_every_draft_says_where_it_came_from(caplog):
    out, _ = await write_outreach(LEADS, CTX, api_key="k", model="m", client=_resp(
        [{"index": 0, "subject": "S", "email": "Hi Alice", "linkedin_note": "Hi"}]))
    assert [o["draft_source"] for o in out] == ["llm", "template_fallback"]  # the model skipped Bob
    assert "usable drafts for 1 of 2 leads" in caplog.text

    caplog.clear()
    out, used_llm = await write_outreach(LEADS, CTX, api_key="k", model="m",
                                         client=FakeGemini(exc=errors.APIError(503, {"error": {"message": "x"}})))
    assert not used_llm and [o["draft_source"] for o in out] == ["template_fallback"] * 2
    assert "Gemini drafting failed (model m)" in caplog.text
    assert any(r.levelname == "WARNING" for r in caplog.records)

    out, _ = await write_outreach(LEADS, CTX, api_key="", model="m")
    assert [o["draft_source"] for o in out] == ["template", "template"]


def test_report_shows_the_fallback_and_keeps_the_outreach_object_unchanged():
    from app.report import build_result

    leads = [{**lead, "quality_score": 50, "quality": "Medium"} for lead in LEADS]
    drafts = [{"subject": "S", "email": "Hi Alice", "linkedin_note": "N", "draft_source": "llm"},
              {"subject": "T", "email": "Hi Bob", "linkedin_note": "M", "draft_source": "template_fallback"}]
    result = build_result("Example Labs", None, leads, drafts)
    data = json.loads(result.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert [lead["draft_source"] for lead in data["leads"]] == ["llm", "template_fallback"]
    assert data["leads"][1]["outreach"] == {"subject": "T", "email": "Hi Bob", "linkedin_note": "M"}
    assert data["summary"]["draft_fallbacks"] == 1
    assert "**Outreach drafts:** 1 of 2 outreach drafts are generic templates" in result
    assert result.count("*Template draft: AI drafting was unavailable for this lead.*") == 1

    off = build_result("Example Labs", None, leads, [{**d, "draft_source": "template"} for d in drafts])
    assert "AI drafting is off on this agent" in off and "Template draft:" not in off
    legacy = build_result("Example Labs", None, leads, ["Hi Alice", "Hi Bob"])  # plain strings: no source
    assert "draft_source" not in legacy and "Outreach drafts:" not in legacy


async def test_a_job_whose_drafting_fails_tells_the_buyer(settings, monkeypatch, caplog):
    import respx as _respx

    from app import drafts as drafts_module
    from app.agent import process_job

    with _respx.mock:
        _respx.post("https://treg.test/call/apollo.people.search").mock(return_value=httpx.Response(200, json={
            "people": [{"first_name": "Jane", "last_name": "Doe", "title": "Head of BD",
                        "organization": {"name": "Acme"}}]}))
        _respx.post("https://treg.test/call/treg.people.search").mock(
            return_value=httpx.Response(200, json={"output": {"people": []}}))
        monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(exc=TimeoutError()))
        result = await process_job("p-fallback", {"company": "Acme", "role": "BD", "company_domain": "acme.io"},
                                   settings)
    data = json.loads(result.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert data["leads"][0]["draft_source"] == "template_fallback" and data["summary"]["draft_fallbacks"] == 1
    assert "AI drafting service did not answer" in result
    assert "timed out" in caplog.text or "failed" in caplog.text
