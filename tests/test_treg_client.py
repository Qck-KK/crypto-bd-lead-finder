import json

import httpx
import pytest
import respx

from app.treg_client import TregError, idempotency_key, search_people

URL = "https://treg.test/call/treg.people.search"


def _kwargs(**over):
    base = dict(token="tok", company="Example Labs", role="partnerships", company_domain="examplelabs.io",
                limit=10, max_cost_usd=0.15, job_tag="abc123", base_url="https://treg.test")
    base.update(over)
    return base


@respx.mock
async def test_request_shape_and_parsing():
    route = respx.post(URL).mock(return_value=httpx.Response(
        200,
        json={"output": {"people": [{"name": "A"}, "junk"]}, "raw": {"secret": "x"}},
        headers={"X-Treg-Served-By": "apollo", "X-Treg-Cost-Micro": "89000", "X-Treg-Call-Id": "c1"},
    ))
    result = await search_people(**_kwargs())

    req = route.calls.last.request
    assert req.headers["X-Treg-Token"] == "tok"
    assert req.headers["X-Treg-Route-Max-Cost"] == "0.1500"
    assert req.headers["X-Treg-Meta"] == "job=abc123"
    # The key hashes job, endpoint and body: same request = same key (BL-37).
    assert req.headers["Idempotency-Key"] == idempotency_key("abc123", "treg.people.search", json.loads(req.content))
    assert json.loads(req.content) == {"limit": 10, "company_domain": "examplelabs.io"}
    assert result.people == [{"name": "A"}]
    assert result.served_by == "apollo"
    assert result.cost_usd == pytest.approx(0.089)


@respx.mock
async def test_miss_returns_empty_list_and_no_domain_uses_free_text():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    result = await search_people(**_kwargs(role=None, company_domain=None))
    assert result.people == []
    assert json.loads(route.calls.last.request.content) == {"limit": 10, "q": "people at Example Labs"}


@respx.mock
async def test_treg_refusal_raises_without_leaking_token():
    respx.post(URL).mock(return_value=httpx.Response(402, json={"error": "route_max_cost"},
                                                     headers={"X-Treg-Error": "1"}))
    with pytest.raises(TregError) as exc:
        await search_people(**_kwargs())
    assert "route_max_cost" in str(exc.value)
    assert "tok" not in str(exc.value)


async def test_missing_token_raises():
    with pytest.raises(TregError):
        await search_people(**_kwargs(token=""))


@respx.mock
async def test_meta_tag_is_sanitised():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))
    await search_people(**_kwargs(job_tag="a b@c/d"))
    assert route.calls.last.request.headers["X-Treg-Meta"] == "job=a-b-c-d"
