import masumi.server
from masumi import Config

from app.masumi_compat import V2Payment


def _payment(**kw):
    return V2Payment(agent_identifier="agent-1", config=Config(payment_service_url="https://p.test/api/v1",
                                                               payment_api_key="k"),
                     network="Preprod", identifier_from_purchaser="abcdef1234567890",
                     input_data={"company": "Example"}, **kw)


def test_v2_payload_has_source_type_and_index(monkeypatch):
    monkeypatch.delenv("PAYMENT_SOURCE_TYPE", raising=False)
    monkeypatch.setenv("PAYMENT_SOURCE_INDEX", "0")
    payload = _payment().build_payload()
    assert payload["paymentSourceType"] == "Web3CardanoV2"
    assert payload["supportedPaymentSourceIndex"] == 0
    assert "paymentType" not in payload
    assert payload["inputHash"] and payload["identifierFromPurchaser"] == "abcdef1234567890"
    assert payload["payByTime"] < payload["submitResultTime"]


def test_v1_mode_omits_index(monkeypatch):
    monkeypatch.setenv("PAYMENT_SOURCE_TYPE", "Web3CardanoV1")
    payload = _payment().build_payload()
    assert payload["paymentSourceType"] == "Web3CardanoV1"
    assert "supportedPaymentSourceIndex" not in payload


def test_installed_into_sdk_server():
    assert masumi.server.Payment is V2Payment
