"""Pro enrichment, lead verification, buyer feedback (all external APIs mocked)."""

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app import drafts as drafts_module
from app import feedback
from app.agent import process_job
from app.enrich import enrich_leads, needs_enrichment
from app.pricing import estimate
from app.verify import apply_verification, verify_lead
from conftest import FakeGemini

APOLLO_URL = "https://treg.test/call/apollo.people.search"
ENRICH_URL = "https://treg.test/call/apollo.people.bulk_enrich"
ROUTED_URL = "https://treg.test/call/treg.people.search"
RECENT = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()


def _extract_json(result):
    return json.loads(result.split("```json\n", 1)[1].split("\n```", 1)[0])


def _apollo_row(pid, first, title):
    return {"id": pid, "first_name": first, "last_name_obfuscated": "S***h", "title": title,
            "last_refreshed_at": RECENT, "organization": {"name": "Acme Labs"}}


def _enriched(pid, first, last):
    # Shape of treg's apollo.people.enrich example, with the personal data it can carry.
    return {"id": pid, "first_name": first, "last_name": last, "name": f"{first} {last}",
            "linkedin_url": f"http://www.linkedin.com/in/{first.lower()}-{last.lower()}", "title": "Head of BD",
            "email": f"{first.lower()}@acme.io", "personal_emails": [f"{first.lower()}@gmail.com"],
            "street_address": "1 Main Street", "city": "Berlin", "formatted_address": "1 Main Street, Berlin",
            "twitter_url": "https://twitter.com/x", "phone_numbers": [{"raw_number": "+49 30 1234567"}],
            "organization": {"name": "Acme Labs", "primary_domain": "acme.io", "phone": "+49 30 7654321",
                             "street_address": "HQ Road 5"},
            "employment_history": [{"current": True, "title": "Head of BD", "organization_name": "Acme Labs"}]}


FREE = {"X-Treg-Cost-Micro": "0"}  # Apollo's search is free: treg reports a zero cost


def _apollo_handler(request):
    body = json.loads(request.content)
    if body["q_organization_domains_list"] != ["acme.io"]:
        return httpx.Response(200, json={"people": []}, headers=FREE)
    return httpx.Response(200, json={"people": [_apollo_row("p1", "Jane", "Head of BD"),
                                                _apollo_row("p2", "Ann", "BD Manager"),
                                                _apollo_row("p3", "Max", "Business Development Lead"),
                                                _apollo_row("p4", "Ola", "BD Lead"),
                                                _apollo_row("p5", "Kim", "Partnerships Lead")]}, headers=FREE)


# --- enrichment ---------------------------------------------------------------

@respx.mock
async def test_pro_job_enriches_filters_personal_data_and_verifies(settings, monkeypatch):
    respx.post(APOLLO_URL).mock(side_effect=_apollo_handler)
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    enrich = respx.post(url__startswith=ENRICH_URL).mock(return_value=httpx.Response(
        200, json={"matches": [_enriched("p1", "Jane", "Smith"), _enriched("p2", "Ann", "Stone"),
                               _enriched("p9", "Zed", "Unknown")]},
        headers={"X-Treg-Cost-Micro": "52000"}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))

    pro = dataclasses.replace(settings, tier="pro", enrich_budget_usd=0.30)
    result = await process_job("buyer", {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"}, pro)
    data = _extract_json(result)
    assert data["enrichment"] == {"enabled": True, "candidates": 5, "enriched": 2, "status": "ok"}
    assert "**Enrichment:** 2 of 5 leads with a masked name or no LinkedIn profile completed" in result

    req = enrich.calls.last.request
    assert "reveal_personal_emails=false" in str(req.url) and "reveal_phone_number=false" in str(req.url)
    assert json.loads(req.content)["details"][0] == {"id": "p1"}

    names = {lead["name"] for lead in data["leads"]}
    assert {"Jane Smith", "Ann Stone"} <= names and "Zed Unknown" not in names
    jane = next(lead for lead in data["leads"] if lead["name"] == "Jane Smith")
    assert jane["profile_url"] == "http://www.linkedin.com/in/jane-smith"
    assert jane["verification"]["status"] == "Verified" and jane["verification"]["checks"]["profile"]
    assert "+ apollo enrich" in jane["source"]
    for private in ("@acme.io", "gmail", "Main Street", "Berlin", "twitter", "1234567", "7654321", "HQ Road"):
        assert private not in result, private
    assert data["tier"] == "pro" and result.startswith("# BD leads (Pro)")


@respx.mock
async def test_pro_without_budget_says_enrichment_is_off_and_pays_nothing(settings, monkeypatch):
    respx.post(APOLLO_URL).mock(side_effect=_apollo_handler)
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    enrich = respx.post(url__startswith=ENRICH_URL).mock(return_value=httpx.Response(200, json={"matches": []}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))

    pro = dataclasses.replace(settings, tier="pro")  # default budget: 0, enrichment off
    result = await process_job("buyer", {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"}, pro)
    data = _extract_json(result)
    assert not enrich.called
    assert data["enrichment"] == {"enabled": False, "candidates": 5, "enriched": 0, "status": "off"}
    assert "**Enrichment:** off on this agent" in result
    assert not any("enrich" in step["provider"] for step in data["search"])


@respx.mock
async def test_pro_enrichment_skipped_when_daily_cap_is_used_up(settings, monkeypatch):
    respx.post(APOLLO_URL).mock(side_effect=_apollo_handler)
    enrich = respx.post(url__startswith=ENRICH_URL).mock(return_value=httpx.Response(200, json={"matches": []}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))

    # 0.02 left today: the (free) Apollo calls fit, one enrichment match (~0.026) does not.
    pro = dataclasses.replace(settings, tier="pro", enrich_budget_usd=0.30, daily_spend_cap_usd=0.02)
    data = _extract_json(await process_job("buyer", {"company": "Acme Labs", "role": "BD",
                                                     "company_domain": "acme.io"}, pro))
    assert not enrich.called
    assert data["enrichment"]["status"] == "daily spend cap reached" and data["enrichment"]["enriched"] == 0


@respx.mock
async def test_pro_enrichment_is_limited_to_what_the_reservation_has_left(settings, monkeypatch, _fresh_ledger):
    respx.post(APOLLO_URL).mock(side_effect=_apollo_handler)
    enrich = respx.post(url__startswith=ENRICH_URL).mock(return_value=httpx.Response(
        200, json={"matches": []}, headers={"X-Treg-Cost-Micro": "52000"}))
    monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))

    pro = dataclasses.replace(settings, tier="pro", enrich_budget_usd=0.30, daily_spend_cap_usd=0.06)
    await process_job("buyer", {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"}, pro)
    req = enrich.calls.last.request
    assert len(json.loads(req.content)["details"]) == 2  # 0.06 granted, Apollo free: 2 x 0.026 fit
    assert req.headers["X-Treg-Route-Max-Cost"] == "0.0570"
    row = next(iter(_fresh_ledger.jobs.values()))
    assert row["enrich_usd"] == pytest.approx(0.052) and await _fresh_ledger.spend_today() <= 0.06 + 1e-9


async def test_standard_report_has_no_enrichment_line(settings):
    with respx.mock:
        respx.post(APOLLO_URL).mock(side_effect=_apollo_handler)
        respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
        result = await process_job("buyer", {"company": "Acme Labs", "role": "BD", "company_domain": "acme.io"},
                                   dataclasses.replace(settings, gemini_api_key=""))
    assert _extract_json(result)["enrichment"] is None and "Enrichment:" not in result


async def test_enrichment_budget_limits_people_and_errors_are_soft(settings):
    leads = [{"name": f"P{i} S***h", "title": "BD", "company": "Acme", "profile_url": "https://acme.io",
              "source": "treg.to", "_signals": {"id": f"id{i}"}} for i in range(10)]
    assert all(needs_enrichment(lead) for lead in leads)
    with respx.mock:
        route = respx.post(url__startswith=ENRICH_URL).mock(return_value=httpx.Response(
            402, json={"error": "insufficient_balance"}, headers={"X-Treg-Error": "1"}))
        out = await enrich_leads(dataclasses.replace(settings, enrich_budget_usd=0.08), leads, "Acme", "acme.io", "t")
    assert len(json.loads(route.calls.last.request.content)["details"]) == 3  # 0.08 / 0.026
    # The 402 carried no cost header, so the failed call counts at its max cost (BL-13).
    assert out == {"enriched": 0, "spent_usd": 0.08, "cost_assumed": True, "error": "unavailable"}
    assert leads[0]["name"] == "P0 S***h"


def test_pro_price_covers_enrichment():
    std, pro = estimate("standard"), estimate("pro")
    assert pro["enrich_usd"] == 0.26 and pro["price_tusdm"] > std["price_tusdm"]
    assert pro["price_tusdm"] == 1.05


# --- verification -------------------------------------------------------------

def _vlead(**signals):
    return {"name": "Jane Smith", "title": "BD", "company": "Acme", "profile_url": "https://acme.io",
            "quality_score": 60, "quality": "Medium", "_signals": {"providers": ["apollo"], **signals}}


def test_verification_statuses():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    v = verify_lead(_vlead(evidence="domain", refreshed_at="2026-09-01T00:00:00Z"), "acme.io", True, now)
    assert v["status"] == "Verified" and v["checks"]["current"] is True
    assert "record refreshed 61 days ago" in v["notes"]

    stale = verify_lead(_vlead(evidence="domain", refreshed_at="2023-01-01T00:00:00Z", current=False),
                        "acme.io", True, now)
    assert stale["status"] == "Unverified" and stale["checks"]["fresh"] is False

    by_name = verify_lead(_vlead(evidence="name"), "acme.io", False, now)
    assert by_name["status"] == "Unverified" and not by_name["checks"]["company"]

    lead = _vlead(evidence="domain", refreshed_at="2026-09-01T00:00:00Z")
    apply_verification(lead, verify_lead(lead, "acme.io", True, now))
    assert lead["quality_score"] == 65


# --- feedback -----------------------------------------------------------------

@pytest.fixture
async def fb_client(env, payment_mock, monkeypatch):
    monkeypatch.setenv("FEEDBACK_SECRET", "test-secret")
    monkeypatch.setenv("FEEDBACK_ADMIN_TOKEN", "admin-1")
    monkeypatch.setenv("AGENT_PUBLIC_URL", "https://agent.test")
    feedback._hits.clear()
    from main import create_app

    store = feedback.MemoryFeedbackStore()
    app = create_app(feedback_store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://agent.test") as c:
        yield c, store


def test_signature_and_link():
    assert feedback.sign("ref1", "s") == feedback.sign("ref1", "s") != feedback.sign("ref2", "s")
    assert feedback.feedback_link("https://a.test", "ref1", "") is None
    assert feedback.feedback_link("https://a.test", "ref1", "s").startswith("https://a.test/feedback?ref=ref1&t=")


async def test_feedback_form_submit_suppress_and_summary(fb_client, monkeypatch):
    client, store = fb_client
    s = dataclasses.replace(__import__("app.settings", fromlist=["Settings"]).Settings.from_env(),
                            treg_base_url="https://treg.test")
    with respx.mock(assert_all_called=False) as router:
        router.route(host="agent.test").pass_through()
        router.post(APOLLO_URL).mock(side_effect=_apollo_handler)
        router.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
        monkeypatch.setattr(drafts_module.genai, "Client", lambda api_key: FakeGemini(text="{}"))
        first = _extract_json(await process_job("b1", {"company": "Acme Labs", "role": "BD",
                                                       "company_domain": "acme.io"}, s))
        url = first["feedback_url"]
        assert url and url.startswith("https://agent.test/feedback?ref=")

        page = await client.get(url.replace("https://agent.test", ""))
        assert page.status_code == 200 and "How useful were these leads?" in page.text
        assert first["leads"][0]["name"] not in page.text  # the form shows titles only

        ref, token = url.split("ref=")[1].split("&t=")
        bad = await client.post("/feedback", json={"ref": ref, "t": "0" * 32, "rating": 5})
        assert bad.status_code == 403
        r = await client.post("/feedback", content=f"ref={ref}&t={token}&rating=2&lead_0=left_company&lead_1=useful"
                              "&comment=thanks", headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200 and "Thank you" in r.text

        gone = first["leads"][0]["name"]
        second = _extract_json(await process_job("b2", {"company": "Acme Labs", "role": "BD",
                                                        "company_domain": "acme.io"}, s))
        assert gone not in [lead["name"] for lead in second["leads"]]
        assert second["summary"]["suppressed_by_feedback"] == 1

    assert (await client.get("/feedback/summary")).status_code == 403
    summary = (await client.get("/feedback/summary", headers={"x-admin-token": "admin-1"})).json()
    assert summary["responses"] == 1 and summary["avg_rating"] == 2 and summary["suppressed_people"] == 1


async def test_feedback_rejects_bad_rating_and_unknown_job(fb_client):
    client, store = fb_client
    await store.remember_delivery("r1", "Acme", [{"hash": "h", "title": "BD"}])
    t = feedback.sign("r1", "test-secret")
    assert (await client.post("/feedback", json={"ref": "r1", "t": t, "rating": 9})).status_code == 400
    t2 = feedback.sign("nope", "test-secret")
    assert (await client.post("/feedback", json={"ref": "nope", "t": t2, "rating": 3})).status_code == 404
    ok = await client.post("/feedback", json={"ref": "r1", "t": t, "rating": 4, "labels": {"0": "wrong_person"}})
    assert ok.json() == {"status": "ok"}
    # Resubmitting does not count the same report twice.
    await client.post("/feedback", json={"ref": "r1", "t": t, "rating": 4, "labels": {"0": "wrong_person"}})
    assert store.suppressed["h"]["reports"] == 1
