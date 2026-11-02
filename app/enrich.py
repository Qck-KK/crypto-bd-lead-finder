"""Optional enrichment (Pro tier): full names and LinkedIn profiles for leads that lack them.

Uses Apollo bulk enrichment through treg (~$0.026 per matched person), only for leads that came
from Apollo with an id and are masked or have no LinkedIn profile, best-scored first, within
ENRICH_BUDGET_USD per job. The enriched record goes through the same compliance allowlist, so
emails, phone numbers and addresses in the provider response are dropped.
"""

import logging
import math

from .compliance import is_linkedin_profile, row_signals, same_company, to_public_lead
from .settings import Settings
from .treg_client import APOLLO_ENRICH_USD_PER_MATCH, TregError, enrich_apollo

logger = logging.getLogger(__name__)


def needs_enrichment(lead: dict) -> bool:
    return bool(lead.get("_signals", {}).get("id")) and (
        "*" in lead["name"] or not is_linkedin_profile(lead.get("profile_url") or ""))


async def enrich_leads(settings: Settings, leads: list[dict], company: str, domain: str, tag: str) -> dict:
    """Updates leads in place. Returns {"enriched": n, "spent_usd": x, "cost_assumed": bool, "error": str}.

    spent_usd is the call's max cost when treg reported no valid cost, also for a failed call."""
    todo = [lead for lead in leads if needs_enrichment(lead)]
    affordable = math.floor(settings.enrich_budget_usd / APOLLO_ENRICH_USD_PER_MATCH + 1e-9)
    todo = todo[: min(10, affordable)]
    outcome = {"enriched": 0, "spent_usd": 0.0, "cost_assumed": False, "error": ""}
    if not todo:
        return outcome

    by_id = {lead["_signals"]["id"]: lead for lead in todo}
    try:
        found = await enrich_apollo(token=settings.treg_token, apollo_ids=list(by_id),
                                    max_cost_usd=min(settings.enrich_budget_usd,
                                                     len(by_id) * APOLLO_ENRICH_USD_PER_MATCH + 0.005),
                                    job_tag=f"{tag}-enrich", base_url=settings.treg_base_url,
                                    timeout_s=settings.treg_timeout_s)
    except TregError as e:
        # Enrichment is an upgrade: without it the job still delivers the search results.
        logger.warning("Enrichment failed: %s", e)
        outcome.update(error="unavailable", spent_usd=e.cost_usd, cost_assumed=e.cost_assumed)
        return outcome
    outcome.update(spent_usd=found.cost_usd, cost_assumed=found.cost_assumed)

    for row in found.people:
        person = row.get("person") if isinstance(row.get("person"), dict) else row
        lead = by_id.get(person.get("id"))
        if lead is None:
            continue
        public = to_public_lead(person, provider="apollo enrich", company_fallback=company, company_domain=domain)
        # Only accept the record if it is the same person at the same company.
        if not public or not same_company(public["company"], company) or \
                public["name"].split()[0].lower() != lead["name"].split()[0].lower():
            continue
        signals = row_signals(person)
        lead["name"] = public["name"]
        if is_linkedin_profile(public["profile_url"]):
            lead["profile_url"] = public["profile_url"]
        if public["title"]:
            lead["title"] = public["title"]
        lead["source"] = f"{lead['source']} + apollo enrich"
        lead["_signals"] = {**lead["_signals"],
                            **{k: v for k, v in signals.items() if v not in (None, "") and k != "id"},
                            "enriched": True}
        outcome["enriched"] += 1
    logger.info("Enriched %d of %d leads for %.4f USD", outcome["enriched"], len(by_id), outcome["spent_usd"])
    return outcome
