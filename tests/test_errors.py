"""BL-16: buyer-facing errors carry no internals, upstream failures are 502/503, bad purchaser identifiers are 400
before any Payment Service request. A real HTTP fake of the Payment Service runs on 127.0.0.1, so V2Payment's own
request and parsing code is what is tested."""

import logging
import re

import httpx
import pytest
from aiohttp import web
from fastapi import HTTPException
from masumi import Config, InMemoryJobStorage

from app.errors import PaymentServiceError, PaymentServiceUnavailable, classify
from app.identifiers import MESSAGE, PurchaserIdentifierError, purchaser_identifier_problem
from app.logs import sanitize
from app.masumi_compat import V2Payment

API_KEY = "pk-SENTINEL-api-key-7f3a9c"
UPSTREAM_TOKEN = "leaked-upstream-token-91b2"
DB_PASSWORD = "dbpass-SENTINEL-55e1"
# Text the fake Payment Service puts in its answers; none of it may reach the buyer.
UPSTREAM = f"UPSTREAM-SENTINEL PrismaClientKnownRequestError at postgres:5432 token={UPSTREAM_TOKEN}"
LEAKS = ("UPSTREAM-SENTINEL", "Prisma", "postgres:5432", UPSTREAM_TOKEN, DB_PASSWORD, API_KEY, "Traceback",
         "nginx", "172.18.", "Internal server error:")
GOOD_ID = "0a1b2c3d4e5f60718293"
INPUT = {"company": "Acme Labs", "company_domain": "acme.io"}
GOOD_DATA = {"blockchainIdentifier": "b" * 40, "payByTime": "1793598064738", "submitResultTime": "1793641264738",
             "unlockTime": "1793662864738", "externalDisputeUnlockTime": "1793684464738"}


@pytest.fixture
async def fake_ps():
    """A Payment Service on 127.0.0.1. `mode` picks its answer; `requests` counts POST /payment/ calls."""
    state = {"mode": "ok", "requests": 0}

    async def payment(request):
        state["requests"] += 1
        state["token"] = request.headers.get("token")
        mode = state["mode"]
        if mode == "400":
            return web.json_response({"status": "error", "error": {"message": f"identifierFromPurchaser; {UPSTREAM}"}},
                                     status=400)
        if mode == "401":
            return web.json_response({"status": "error", "error": {"message": UPSTREAM}}, status=401)
        if mode == "500":
            return web.json_response({"status": "error", "error": {"message": UPSTREAM}}, status=500)
        if mode == "html":
            return web.Response(text=f"<html>bad gateway nginx/1.25 upstream 172.18.0.5 {UPSTREAM}</html>",
                                content_type="text/html")
        if mode == "nodata":
            return web.json_response({"status": "success", "note": UPSTREAM})
        if mode == "badtime":
            return web.json_response({"status": "success", "data": {**GOOD_DATA, "payByTime": UPSTREAM}})
        return web.json_response({"status": "success", "data": GOOD_DATA})

    app = web.Application()
    app.router.add_post("/api/v1/payment/", payment)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state["url"] = f"http://127.0.0.1:{runner.addresses[0][1]}/api/v1"
    yield state
    await runner.cleanup()


def _closed_port_url():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/api/v1"


class BrokenStorage(InMemoryJobStorage):
    async def create_job(self, job_id, job_data):
        raise OSError(f"Connect call failed ('172.18.0.2', 5432) postgresql://masumi:{DB_PASSWORD}@postgres:5432/x")

    async def get_job(self, job_id):
        raise OSError(f"connection lost postgresql://masumi:{DB_PASSWORD}@postgres:5432/x")


@pytest.fixture
def agent_env(env, monkeypatch, fake_ps):
    monkeypatch.setenv("PAYMENT_SERVICE_URL", fake_ps["url"])
    monkeypatch.setenv("PAYMENT_API_KEY", API_KEY)

    async def no_monitoring(self, callback=None, interval_seconds=10):
        return None

    monkeypatch.setattr(V2Payment, "start_status_monitoring", no_monitoring)
    return fake_ps


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://agent")


async def _post(storage=None, identifier=GOOD_ID, **extra):
    from main import create_app

    async with _client(create_app(job_storage=storage or InMemoryJobStorage())) as c:
        return await c.post("/start_job", json={"identifier_from_purchaser": identifier, "input_data": INPUT, **extra})


def _assert_generic(r, status, caplog, cause):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"detail", "error_id"}
    assert re.fullmatch(r"[0-9a-f]{16}", body["error_id"])
    for leak in LEAKS:
        assert leak not in r.text, leak
    # The id is in the server log next to the cause, so a buyer's report can be traced.
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "app.errors"]
    assert any(f"error_id={body['error_id']}" in line and cause in line for line in lines), lines
    # Nothing logged anywhere carries the API key or the database password; upstream secrets are redacted.
    assert API_KEY not in caplog.text and DB_PASSWORD not in caplog.text and UPSTREAM_TOKEN not in caplog.text
    return body


# --- the seven audited scenarios ---------------------------------------------------------------------------------

@pytest.mark.parametrize("identifier", ["abc", "abcdef1234567", "abcdef123456789", "ab" * 14, "zz" * 8,
                                        "0x" + "ab" * 7, " " + "ab" * 7, "ab" * 7 + "\n", "", 1234567890123456,
                                        None, ["ab" * 7]])
async def test_bad_identifier_is_400_and_never_reaches_the_payment_service(agent_env, caplog, identifier):
    r = await _post(identifier=identifier)
    assert r.status_code == 400 and r.json() == {"detail": MESSAGE}
    assert agent_env["requests"] == 0
    assert API_KEY not in caplog.text


@pytest.mark.parametrize("mode, status, cause", [
    ("400", 502, "PaymentServiceError"),
    ("401", 502, "PaymentServiceError"),
    ("500", 503, "PaymentServiceUnavailable"),
    ("html", 502, "PaymentServiceError"),
    ("nodata", 502, "PaymentServiceError"),
    ("badtime", 502, "PaymentServiceError"),
])
async def test_payment_service_failures_are_5xx_without_internals(agent_env, caplog, mode, status, cause):
    caplog.set_level(logging.INFO)
    agent_env["mode"] = mode
    r = await _post()
    body = _assert_generic(r, status, caplog, cause)
    assert agent_env["requests"] == 1 and agent_env["token"] == API_KEY  # the key was sent, never shown
    assert body["detail"] in ("Payment service error", "Payment service temporarily unavailable; try again later")
    if status == 503:
        assert r.headers["Retry-After"] == "30"


async def test_payment_service_unreachable_is_503(agent_env, monkeypatch, caplog):
    monkeypatch.setenv("PAYMENT_SERVICE_URL", _closed_port_url())
    r = await _post()
    _assert_generic(r, 503, caplog, "PaymentServiceUnavailable")


async def test_job_storage_failure_is_500_without_internals(agent_env, caplog):
    r = await _post(storage=BrokenStorage())
    body = _assert_generic(r, 500, caplog, "OSError")
    assert body["detail"] == "Internal server error"


async def test_status_failure_is_generic_500(agent_env, caplog):
    from main import create_app

    async with _client(create_app(job_storage=BrokenStorage())) as c:
        r = await c.get("/status", params={"job_id": "x"})
    _assert_generic(r, 500, caplog, "OSError")


async def test_unhandled_exception_outside_the_sdk_is_generic_500(agent_env, caplog):
    from main import create_app

    app = create_app()

    @app.get("/boom")
    async def boom():
        raise RuntimeError(f"secret detail {UPSTREAM}")

    async with _client(app) as c:
        r = await c.get("/boom")
    _assert_generic(r, 500, caplog, "RuntimeError")


async def test_good_request_still_creates_the_order(agent_env):
    r = await _post()
    assert r.status_code == 200, r.text
    assert r.json()["blockchainIdentifier"] == "b" * 40 and r.json()["identifierFromPurchaser"] == GOOD_ID


# --- statuses below 500 are unchanged --------------------------------------------------------------------------

async def test_4xx_answers_are_preserved(agent_env, monkeypatch):
    from main import create_app

    async with _client(create_app()) as c:
        r = await c.get("/status", params={"job_id": "nope"})
        assert r.status_code == 404 and r.json() == {"detail": "Job not found"}
        r = await c.post("/start_job", json={"input_data": INPUT})  # identifier missing: the SDK's validation
        assert r.status_code == 422 and "error_id" not in r.json()
        r = await c.post("/start_job", content=b"not json", headers={"content-type": "application/json"})
        assert r.status_code == 422
    assert agent_env["requests"] == 0

    monkeypatch.setenv("START_JOB_PER_HOUR", "0")
    async with _client(create_app()) as c:
        r = await c.post("/start_job", json={"identifier_from_purchaser": GOOD_ID, "input_data": INPUT})
    assert r.status_code == 429 and "error_id" not in r.json() and r.headers["Retry-After"]

    monkeypatch.setenv("PAUSE_NEW_JOBS", "true")
    async with _client(create_app()) as c:
        r = await c.post("/start_job", json={"identifier_from_purchaser": GOOD_ID, "input_data": INPUT})
    assert r.status_code == 503 and r.json()["detail"].startswith("Not accepting jobs") and "error_id" not in r.json()
    assert agent_env["requests"] == 0


# --- building blocks ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, ok", [("ab" * 7, True), ("AB" * 13, True), ("0123456789abcdefABCDEF", True),
                                       ("ab" * 6, False), ("ab" * 14, False), ("abcdef123456789", False),
                                       ("gh" * 8, False), ("ab" * 7 + " ", False), ("ａｂ" * 7, False),
                                       (12345678901234, False), (None, False), (b"ab" * 7, False)])
def test_identifier_rule_matches_the_payment_service(value, ok):
    assert (purchaser_identifier_problem(value) is None) is ok


async def test_v2_payment_refuses_a_bad_identifier_without_a_request(agent_env):
    payment = V2Payment(agent_identifier="a" * 60, config=Config(payment_service_url=agent_env["url"],
                                                                 payment_api_key=API_KEY),
                        network="Preprod", identifier_from_purchaser="abc", input_data=INPUT)
    with pytest.raises(PurchaserIdentifierError):
        payment.build_payload()
    with pytest.raises(PurchaserIdentifierError):
        await payment.create_payment_request()
    assert agent_env["requests"] == 0


async def test_v2_payment_errors_carry_no_upstream_text(agent_env, caplog):
    payment = V2Payment(agent_identifier="a" * 60, config=Config(payment_service_url=agent_env["url"],
                                                                 payment_api_key=API_KEY),
                        network="Preprod", identifier_from_purchaser=GOOD_ID, input_data=INPUT)
    for mode, exc in (("400", PaymentServiceError), ("500", PaymentServiceUnavailable), ("nodata", PaymentServiceError)):
        agent_env["mode"] = mode
        with pytest.raises(exc) as info:
            await payment.create_payment_request()
        assert "SENTINEL" not in str(info.value) and API_KEY not in str(info.value)
    warnings = [r.getMessage() for r in caplog.records if r.name == "app.masumi_compat"]
    assert warnings and all(len(w) < 320 for w in warnings)
    assert UPSTREAM_TOKEN not in caplog.text and API_KEY not in caplog.text


def test_a_buyer_error_raised_inside_the_sdk_stays_400():
    try:
        try:
            raise PurchaserIdentifierError()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Internal server error: {e}")
    except HTTPException as wrapped:
        assert classify(wrapped)[:2] == (400, MESSAGE)


async def test_uvicorn_traceback_of_an_unhandled_exception_is_redacted(agent_env, capsys):
    """The real path: uvicorn applies its own logging config, Starlette re-raises after our 500 answer, and uvicorn
    logs the traceback on `uvicorn.error` through its own stderr handler."""
    import asyncio

    import uvicorn
    from main import create_app

    app = create_app()

    @app.get("/boom")
    async def boom():
        raise OSError(f"connect failed postgresql://masumi:{DB_PASSWORD}@postgres:5432/x token={UPSTREAM_TOKEN}")

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/boom")
    finally:
        server.should_exit = True
        await task
    err = capsys.readouterr().err
    assert r.status_code == 500 and set(r.json()) == {"detail", "error_id"}
    assert "Exception in ASGI application" in err and "OSError" in err and "[redacted]" in err
    assert DB_PASSWORD not in err and UPSTREAM_TOKEN not in err and API_KEY not in err


def test_basic_and_prefixed_credentials_are_redacted():
    import base64

    basic = base64.b64encode(f"user:{DB_PASSWORD}".encode()).decode()
    texts = [f"Authorization: Basic {basic}", f"{{'Authorization': 'Basic {basic}'}}", f"sent Basic {basic} upstream",
             f"PAYMENT_API_KEY={DB_PASSWORD}", f"client_secret={DB_PASSWORD}", f"x-api-key: {DB_PASSWORD}",
             f'"auth_token": "{DB_PASSWORD}"', f"ACCESS_KEY={DB_PASSWORD}", f"admin_key: {DB_PASSWORD}"]
    for text in texts:
        out = sanitize(text, 500)
        assert DB_PASSWORD not in out and basic not in out and "[redacted]" in out, text.split(":")[0]
    # Ordinary text stays readable.
    for text in ("Basic authentication is required", "max_tokens=5 token_count=3 password_hash=abc",
                 "FEEDBACK_SECRET not set: reports carry no feedback link"):
        assert sanitize(text) == text


def test_sanitize_redacts_credentials_and_flattens_lines():
    text = (f"key {API_KEY} token: abc123 Authorization: Bearer eyJhbGciOi.x.y api_key=zzz "
            f"postgresql://masumi:{DB_PASSWORD}@postgres:5432/db\nFAKE LOG LINE")
    out = sanitize(text, 500, secrets=(API_KEY,))
    for secret in (API_KEY, "abc123", "eyJhbGciOi", "zzz", DB_PASSWORD):
        assert secret not in out
    assert "\n" not in out and "postgresql://[redacted]@postgres:5432/db" in out
    assert sanitize("x" * 1000, 50).endswith("...[truncated]") and len(sanitize("x" * 1000, 50)) < 70
