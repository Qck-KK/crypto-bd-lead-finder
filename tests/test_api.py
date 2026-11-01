"""MIP-003 endpoints through the masumi SDK, with the Payment Service, treg and Gemini mocked."""

import asyncio
import json

import httpx
import pytest
import respx

from app import drafts as drafts_module
from conftest import CHAIN_ID, FakeGemini, mock_apollo_empty

TREG_URL = "https://treg.test/call/treg.people.search"


@pytest.fixture
async def client(env, payment_mock):
    from main import create_app

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as c:
        yield c


async def test_static_endpoints(client):
    r = await client.get("/availability")
    assert r.status_code == 200 and r.json()["status"] == "available" and r.json()["type"] == "masumi-agent"

    r = await client.get("/input_schema")
    assert r.status_code == 200
    assert [f["id"] for f in r.json()["input_data"]][0] == "company"

    r = await client.get("/demo")
    assert r.status_code == 200 and "B2B professional outreach only" in r.json()["output"]["result"]

    r = await client.get("/status", params={"job_id": "nope"})
    assert r.status_code == 404


async def test_invalid_input_rejected_before_payment(client, payment_mock):
    r = await client.post("/start_job", json={"identifier_from_purchaser": "abcdef1234567890", "input_data": {}})
    assert r.status_code == 400
    assert payment_mock["callback"] is None


@respx.mock(assert_all_called=False)
async def test_pay_then_run_then_deliver(client, payment_mock, monkeypatch, respx_mock):
    respx_mock.route(host="agent").pass_through()
    apollo = mock_apollo_empty(respx_mock)
    treg = respx_mock.post(TREG_URL).mock(return_value=httpx.Response(200, json={"output": {"people": [
        {"name": "Alice Nakamoto", "title": "Head of BD", "phone": "+1 415 555 0199",
         "linkedin_url": "https://linkedin.com/in/alice"}]}}))
    monkeypatch.setattr(drafts_module.genai, "Client",
                        lambda api_key: FakeGemini(text=json.dumps({"drafts": [{"index": 0, "draft": "Hi Alice"}]})))

    r = await client.post("/start_job", json={"identifier_from_purchaser": "abcdef1234567890",
                                              "input_data": {"company": "Example Labs", "role": "BD"}})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["blockchainIdentifier"] == CHAIN_ID and job["agentIdentifier"] == "test-agent"
    assert job["input_hash"]

    # Escrow first: nothing runs and nothing is paid to treg until the payment is confirmed.
    r = await client.get("/status", params={"job_id": job["id"]})
    assert r.json()["status"] == "awaiting_payment"
    assert not treg.called and not apollo.called

    await payment_mock["callback"]({"blockchainIdentifier": CHAIN_ID})
    for _ in range(100):
        r = await client.get("/status", params={"job_id": job["id"]})
        if r.json()["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)

    assert r.json()["status"] == "completed", r.json()
    result = r.json()["result"]
    assert "Hi Alice" in result and "555" not in result
    assert payment_mock["submitted"] == (CHAIN_ID, result)
