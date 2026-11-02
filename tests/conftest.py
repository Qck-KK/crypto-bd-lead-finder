import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Keep real keys out of tests: main.py skips .env when this is set, and stray vars are dropped.
os.environ["BDLF_NO_DOTENV"] = "1"
for _var in ("TREG_TOKEN", "GEMINI_API_KEY", "PAYMENT_API_KEY", "AGENT_DATABASE_URL"):
    os.environ.pop(_var, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import masumi.server  # noqa: E402
from app import masumi_compat  # noqa: E402
from app.settings import Settings  # noqa: E402

# Same as main.py; payment_mock then patches V2Payment, the class the app really uses.
masumi_compat.install()

CHAIN_ID = "block_chain_id_123"
APOLLO_URL = "https://treg.test/call/apollo.people.search"


def mock_apollo_empty(router):
    """Apollo finds nobody, so the job goes on to the routed treg search (the path these tests cover)."""
    import httpx

    return router.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": [], "total_entries": 0}))

# A provider row stuffed with private contact data that must never reach the output.
LEAKY_ROW = {
    "first_name": "Alice",
    "last_name": "Nakamoto",
    "title": "Head of BD | mobile +1 (415) 555-0199",
    "organization": {"name": "Example Labs", "website_url": "https://examplelabs.io"},
    "linkedin_url": "https://www.linkedin.com/in/alice-nakamoto?trk=public",
    "email": "alice.private@gmail.com",
    "personal_emails": ["alice@protonmail.com"],
    "work_email": "alice@examplelabs.io",
    "phone": "+1 415 555 0199",
    "mobile_phone": "+44 7700 900123",
    "phone_numbers": [{"raw_number": "+1-415-555-0199"}],
    "home_address": "1 Infinite Loop, Cupertino, CA",
    "street_address": "742 Evergreen Terrace",
    "city": "Springfield",
    "twitter_url": "https://twitter.com/alice",
    "facebook_url": "https://facebook.com/alice",
}

PRIVATE_STRINGS = [
    "555-0199", "555 0199", "7700 900123", "alice.private@gmail.com", "alice@protonmail.com",
    "alice@examplelabs.io", "Infinite Loop", "Evergreen", "Springfield", "twitter.com", "facebook.com",
    "mobile +1",
]


@pytest.fixture(autouse=True)
def _fresh_resolver_cache():
    from app.resolver import clear_cache

    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _fresh_ledger(monkeypatch):
    from app import ledger

    fresh = ledger.MemoryLedger()
    ledger.set_ledger(fresh)
    yield fresh
    ledger.set_ledger(ledger.MemoryLedger())


@pytest.fixture(autouse=True)
def _healthy(monkeypatch):
    from app.server import Health

    async def check(self):
        return ("available", "ok")

    monkeypatch.setattr(Health, "check", check)


@pytest.fixture
def settings():
    return Settings(
        treg_token="test-treg-token",
        treg_base_url="https://treg.test",
        treg_max_cost_usd=0.15,
        treg_timeout_s=5,
        gemini_api_key="test-gemini-key",
        gemini_model="gemini-2.5-flash",
        max_people=10,
    )


@pytest.fixture
def env(monkeypatch):
    values = {
        "PAYMENT_SERVICE_URL": "https://payment.test/api/v1",
        "PAYMENT_API_KEY": "test-payment-key",
        "SELLER_VKEY": "test-vkey",
        "AGENT_IDENTIFIER": "test-agent",
        "NETWORK": "Preprod",
        "TREG_TOKEN": "test-treg-token",
        "TREG_BASE_URL": "https://treg.test",
        "GEMINI_API_KEY": "test-gemini-key",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def payment_mock(monkeypatch):
    """Mocks the Masumi Payment Service calls. `callbacks` maps each monitored payment id to its callback.

    `record` is what /payment/resolve-blockchain-identifier returns (a valid FundsLocked payment for
    the test agent at the standard price); tests change it to simulate bad payments or other states.
    """
    from app.pricing import TUSDM_UNIT

    state = {"callback": None, "callbacks": {}, "submitted": None, "status_calls": 0,
             "record": {"onChainState": "FundsLocked", "agentIdentifier": "test-agent",
                        "RequestedFunds": [{"unit": TUSDM_UNIT, "amount": "270000"}]}}

    async def check_payment_status_by_identifier(self, blockchain_identifier):
        state["status_calls"] += 1
        if isinstance(state["record"], Exception):
            raise state["record"]
        # Like the Payment Service: the record carries the input hash this payment was created with.
        return {"status": "success", "data": {"inputHash": getattr(self, "input_hash", None), **state["record"],
                                              "blockchainIdentifier": blockchain_identifier}}

    monkeypatch.setattr(masumi.server.Payment, "check_payment_status_by_identifier",
                        check_payment_status_by_identifier)

    async def create_payment_request(self, metadata=None):
        return {"data": {"blockchainIdentifier": CHAIN_ID, "payByTime": "1700000000000",
                         "submitResultTime": "1700000100000", "unlockTime": "1700000200000",
                         "externalDisputeUnlockTime": "1700000300000", "sellerVKey": "test-vkey"}}

    async def start_status_monitoring(self, callback=None, interval_seconds=10):
        state["callback"] = callback
        for payment_id in self.payment_ids:
            state["callbacks"][payment_id] = callback

    async def complete_payment(self, blockchain_identifier, job_output):
        state["submitted"] = (blockchain_identifier, job_output)
        return {"status": "success"}

    monkeypatch.setattr(masumi.server.Payment, "create_payment_request", create_payment_request)
    monkeypatch.setattr(masumi.server.Payment, "start_status_monitoring", start_status_monitoring)
    monkeypatch.setattr(masumi.server.Payment, "complete_payment", complete_payment)
    return state


class FakeGemini:
    """Stands in for google.genai.Client: client.aio.models.generate_content(...)."""

    def __init__(self, text=None, exc=None):
        self.calls = []
        self._text, self._exc = text, exc
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        return SimpleNamespace(text=self._text)
