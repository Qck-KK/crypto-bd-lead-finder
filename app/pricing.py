"""Per-job cost estimate and the Preprod listing price derived from it.

Defaults: treg ~$0.0089 per returned row (figure from treg's people-search listing) x 10 rows,
Gemini 2.5 Flash on the free tier = $0. Price = cost x markup, rounded up to 0.01.
Every number can be overridden with the env vars named below.
"""

import math
import os

# Preprod test stablecoin (policy id + asset name), from the Masumi registration docs.
TUSDM_UNIT = "16a55b2a349361ff88c03788f93e1e966e5d689605d044fef722ddde0014df10745553444d"
TUSDM_DECIMALS = 6


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def estimate(tier: str = "standard") -> dict:
    """Worst-case cost: the routed fallback search, plus (Pro) enriching all 10 leads at ~$0.026 each."""
    rows = _f("COST_ROWS_PER_JOB", 10)
    treg_per_row = _f("COST_TREG_PER_ROW_USD", 0.0089)
    llm = _f("COST_LLM_PER_JOB_USD", 0.0)
    enrich = _f("COST_ENRICH_PER_JOB_USD", 10 * 0.026) if tier == "pro" else 0.0
    markup = _f("PRICE_MARKUP", 3.0)
    cost = rows * treg_per_row + llm + enrich
    suggested = math.ceil(round(cost * markup * 100, 6)) / 100
    price = _f("PRICE_TUSDM", suggested)
    return {
        "tier": tier,
        "treg_usd": round(rows * treg_per_row, 4),
        "enrich_usd": round(enrich, 4),
        "llm_usd": llm,
        "cost_usd": round(cost, 4),
        "markup": markup,
        "suggested_price_tusdm": suggested,
        "price_tusdm": price,
        "price_atomic": str(round(price * 10**TUSDM_DECIMALS)),
        "unit": TUSDM_UNIT,
    }
