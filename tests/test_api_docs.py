"""BL-17: FastAPI's documentation routes are off unless API_DOCS_ENABLED=true; MIP-003 endpoints are unaffected."""

import httpx
import pytest

DOCS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
MIP003 = {("/start_job", "POST"), ("/status", "GET"), ("/availability", "GET"), ("/input_schema", "GET"),
          ("/provide_input", "POST"), ("/demo", "GET")}


@pytest.fixture(params=["standard", "pro"])
def tier_env(request, env, payment_mock, monkeypatch):
    monkeypatch.delenv("API_DOCS_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_TIER", request.param)
    return request.param


def _routes(app):
    return {(r.path, m) for r in app.router.routes for m in (getattr(r, "methods", None) or ())}


def _paths(app):
    return {getattr(r, "path", None) for r in app.router.routes}


async def _get_all(app, paths):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as c:
        return {p: await c.get(p) for p in paths}


@pytest.mark.parametrize("value", [None, "false", "0", "no", ""])
async def test_docs_are_off_by_default(tier_env, monkeypatch, value):
    from main import create_app

    if value is not None:
        monkeypatch.setenv("API_DOCS_ENABLED", value)
    app = create_app()
    assert not _paths(app) & set(DOCS)  # not registered at all
    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None
    assert MIP003 <= _routes(app)
    answers = await _get_all(app, DOCS)
    assert all(r.status_code == 404 for r in answers.values()), {p: r.status_code for p, r in answers.items()}


async def test_docs_can_be_turned_on(tier_env, monkeypatch):
    from main import create_app

    monkeypatch.setenv("API_DOCS_ENABLED", "true")
    app = create_app()
    assert set(DOCS) <= _paths(app) and MIP003 <= _routes(app)
    answers = await _get_all(app, DOCS)
    assert all(r.status_code == 200 for r in answers.values())
    assert {p for p, _ in MIP003} <= set(answers["/openapi.json"].json()["paths"])


async def test_mip003_endpoints_answer_the_same_with_docs_on_or_off(tier_env, monkeypatch):
    from main import create_app

    seen = []
    for value in ("false", "true"):
        monkeypatch.setenv("API_DOCS_ENABLED", value)
        app = create_app()
        answers = await _get_all(app, ("/availability", "/input_schema", "/demo"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as c:
            status = await c.get("/status", params={"job_id": "nope"})
            start = await c.post("/start_job", json={"identifier_from_purchaser": "abcdef1234567890",
                                                     "input_data": {"company": "Acme Labs"}})
        seen.append(({p: (r.status_code, r.json()) for p, r in answers.items()}, status.status_code,
                     start.status_code, sorted(_routes(app) - {(p, m) for p in DOCS for m in ("GET", "HEAD")})))
    assert seen[0] == seen[1]
    assert seen[0][0]["/input_schema"][0] == 200 and seen[0][1] == 404
    # Standard sells; Pro without enrichment refuses before any payment request (BL-01), as before.
    assert seen[0][2] == (200 if tier_env == "standard" else 503)
