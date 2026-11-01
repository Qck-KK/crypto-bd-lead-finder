import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import preflight  # noqa: E402


def test_economics_pass_for_registered_prices(monkeypatch):
    for k in ("PRICE_TUSDM", "ENRICH_BUDGET_USD", "DAILY_SPEND_CAP_USD"):
        monkeypatch.delenv(k, raising=False)
    assert all(s == "PASS" for s, _, _ in preflight.check_economics("standard", "Preprod"))
    pro = preflight.check_economics("pro", "Preprod")
    assert pro[0][0] == "PASS"


def test_underpriced_agent_fails(monkeypatch):
    monkeypatch.setenv("PRICE_TUSDM", "0.05")
    assert preflight.check_economics("standard", "Preprod")[0][0] == "FAIL"


def test_mainnet_target_always_lists_owner_decisions():
    rows = preflight.check_mainnet_manual("Mainnet")
    assert any(s == "FAIL" and "code guard" in n for s, n, _ in rows)
    assert any("whitelist" in n.lower() for _, n, _ in rows)
    assert preflight.check_mainnet_manual("Preprod") == []


def test_missing_secrets_fail(monkeypatch):
    for k in ("PAYMENT_API_KEY", "FEEDBACK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    rows = {n: s for s, n, _ in preflight.check_secrets()}
    assert rows["PAYMENT_API_KEY set"] == "FAIL" and rows["FEEDBACK_SECRET set"] == "FAIL"
