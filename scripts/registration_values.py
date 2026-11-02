"""Print the values to enter in the Masumi Payment Service admin form (AI Agents -> Register AI Agent).

    python scripts/registration_values.py

Reads AGENT_PUBLIC_URL, PRICE_TUSDM and the COST_* overrides from .env. Sends nothing.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

from app.pricing import estimate  # noqa: E402
from app.report import FOOTER  # noqa: E402
from app.settings import Settings  # noqa: E402

# The registry caps the description at 250 characters.
DESCRIPTION = (
    "Finds up to 10 people at a company by role and drafts English outreach. Public professional data only "
    "(name, title, company, LinkedIn/company site); no personal contacts. " + FOOTER
)
DESCRIPTION_PRO = (
    "Pro: up to 10 verified people at a company by role, with full names and LinkedIn, plus personalized "
    "email and LinkedIn notes. Public professional data only; no personal contacts. " + FOOTER
)
NAMES = {"standard": "Crypto BD Lead Finder", "pro": "Crypto BD Lead Finder Pro"}


def main() -> None:
    tier = "pro" if os.getenv("AGENT_TIER", "standard").lower() == "pro" else "standard"
    e = estimate(tier)
    print(f"Name:          {NAMES[tier]}")
    print(f"API URL:       {os.getenv('AGENT_PUBLIC_URL', '<your public https URL>')}")
    print(f"Description:   {DESCRIPTION_PRO if tier == 'pro' else DESCRIPTION}")
    print(f"Capability:    crypto-bd-lead-finder{'-pro' if tier == 'pro' else ''} / 1.0.0")
    print("Tags:          bd, leads, crypto, web3, outreach, people-search" + (", enrichment" if tier == "pro" else ""))
    print(f"Price:         {e['price_tusdm']} tUSDM  (atomic amount {e['price_atomic']}, 6 decimals)")
    print(f"Payment unit:  {e['unit']}")
    print()
    print(f"Cost estimate (worst case): treg ${e['treg_usd']} + enrich ${e['enrich_usd']} + LLM ${e['llm_usd']} "
          f"= ${e['cost_usd']} per job; x{e['markup']} = {e['suggested_price_tusdm']} suggested")
    if tier == "pro" and not Settings.from_env().enrichment_enabled:
        print("\nWARNING: enrichment is off (ENRICH_BUDGET_USD). This description and price assume it is on; "
              "do not register Pro until it is, or the listing promises what the agent does not deliver.")


if __name__ == "__main__":
    main()
