"""Pro is only sold while its enrichment is on (BL-01): settings, /availability, /start_job, preflight."""

import dataclasses
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import preflight  # noqa: E402

START = {"identifier_from_purchaser": "abcdef1234567890", "input_data": {"company": "Example Labs"}}


@pytest.mark.parametrize("tier, budget, token, people", [
    ("pro", 0.0, "t", 0), ("pro", 0.025, "t", 0), ("pro", 0.026, "t", 1), ("pro", 0.08, "t", 3),
    ("pro", 0.30, "t", 10), ("pro", 0.30, "", 0), ("standard", 0.30, "t", 0)])
def test_enrich_people_per_job(settings, tier, budget, token, people):
    s = dataclasses.replace(settings, tier=tier, enrich_budget_usd=budget, treg_token=token)
    assert s.enrich_people_per_job == people and s.enrichment_enabled == (people > 0)


def test_sale_blocked_only_for_unenriched_pro(settings):
    assert settings.sale_blocked_reason() is None
    pro = dataclasses.replace(settings, tier="pro")
    assert "enrichment is off" in pro.sale_blocked_reason()
    assert dataclasses.replace(pro, enrich_budget_usd=0.05).sale_blocked_reason() is None
    assert dataclasses.replace(pro, pro_allow_without_enrichment=True).sale_blocked_reason() is None


def test_enrichment_is_off_unless_configured(monkeypatch):
    from app.settings import Settings

    monkeypatch.delenv("ENRICH_BUDGET_USD", raising=False)
    monkeypatch.setenv("AGENT_TIER", "pro")
    monkeypatch.setenv("TREG_TOKEN", "t")
    assert Settings.from_env().enrich_budget_usd == 0.0 and not Settings.from_env().enrichment_enabled


async def _app_client(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from main import create_app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://agent")


async def test_unenriched_pro_is_not_sold(env, payment_mock, monkeypatch):
    monkeypatch.delenv("ENRICH_BUDGET_USD", raising=False)
    async with await _app_client(monkeypatch, AGENT_TIER="pro") as c:
        av = (await c.get("/availability")).json()
        assert av["status"] == "unavailable" and "enrichment is off" in av["message"]
        r = await c.post("/start_job", json=START)
        assert r.status_code == 503 and "Not accepting jobs" in r.json()["detail"]
        assert payment_mock["callback"] is None  # no payment request, no monitoring
        notice = (await c.get("/input_schema")).json()["input_data"][-1]["data"]["description"]
        assert "Enrichment is currently off" in notice


async def test_unenriched_pro_can_be_allowed_explicitly(env, payment_mock, monkeypatch):
    monkeypatch.delenv("ENRICH_BUDGET_USD", raising=False)
    async with await _app_client(monkeypatch, AGENT_TIER="pro", PRO_ALLOW_WITHOUT_ENRICHMENT="true") as c:
        assert (await c.get("/availability")).json()["status"] == "available"
        assert (await c.post("/start_job", json=START)).status_code == 200


async def test_enriched_pro_and_standard_are_sold(env, payment_mock, monkeypatch):
    async with await _app_client(monkeypatch, AGENT_TIER="pro", ENRICH_BUDGET_USD="0.08") as c:
        assert (await c.get("/availability")).json()["status"] == "available"
        notice = (await c.get("/input_schema")).json()["input_data"][-1]["data"]["description"]
        assert "for up to 3 people per job" in notice
        assert (await c.post("/start_job", json=START)).status_code == 200
    monkeypatch.delenv("ENRICH_BUDGET_USD")
    async with await _app_client(monkeypatch, AGENT_TIER="standard") as c:
        assert (await c.get("/availability")).json()["status"] == "available"
        assert (await c.post("/start_job", json=START)).status_code == 200


def test_preflight_fails_unenriched_pro(monkeypatch):
    for k in ("PRICE_TUSDM", "ENRICH_BUDGET_USD", "PRO_ALLOW_WITHOUT_ENRICHMENT"):
        monkeypatch.delenv(k, raising=False)
    rows = {n: (s, d) for s, n, d in preflight.check_economics("pro", "Preprod")}
    assert rows["Pro enrichment enabled"][0] == "FAIL" and "refuses new jobs" in rows["Pro enrichment enabled"][1]

    monkeypatch.setenv("PRO_ALLOW_WITHOUT_ENRICHMENT", "true")
    rows = {n: (s, d) for s, n, d in preflight.check_economics("pro", "Mainnet")}
    assert rows["Pro enrichment enabled"] == ("FAIL", "ENRICH_BUDGET_USD=0.0; still sold: PRO_ALLOW_WITHOUT_ENRICHMENT is set")

    monkeypatch.setenv("ENRICH_BUDGET_USD", "0.1")
    rows = {n: s for s, n, _ in preflight.check_economics("pro", "Preprod")}
    assert "Pro enrichment enabled" not in rows and rows["enrichment budget vs price"] == "PASS"
    assert rows["enrichment on Preprod"] == "WARN"
    assert not any(s == "FAIL" for s, _, _ in preflight.check_economics("standard", "Preprod"))
