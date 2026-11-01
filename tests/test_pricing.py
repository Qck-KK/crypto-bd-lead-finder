from app.pricing import TUSDM_UNIT, estimate


def test_default_price_is_about_three_times_cost(monkeypatch):
    for k in ("COST_ROWS_PER_JOB", "COST_TREG_PER_ROW_USD", "COST_LLM_PER_JOB_USD", "PRICE_MARKUP", "PRICE_TUSDM"):
        monkeypatch.delenv(k, raising=False)
    e = estimate()
    assert e["cost_usd"] == 0.089 and e["llm_usd"] == 0.0
    assert e["price_tusdm"] == 0.27
    assert e["price_atomic"] == "270000"
    assert e["unit"] == TUSDM_UNIT


def test_registry_description_fits_and_carries_notice():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from registration_values import DESCRIPTION

    assert len(DESCRIPTION) <= 250
    assert "treg.to" in DESCRIPTION and "B2B professional outreach only" in DESCRIPTION


def test_price_override(monkeypatch):
    monkeypatch.setenv("PRICE_TUSDM", "0.5")
    assert estimate()["price_atomic"] == "500000"
