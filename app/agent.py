"""Job handler: input -> domain resolver -> provider router (via treg) -> compliance filter + scoring
-> (Pro) enrichment -> verification -> personalized outreach -> report with a feedback link."""

import hashlib
import logging
from typing import Any, Optional

from . import feedback
from .compliance import normalize_domain
from .drafts import TONES, OutreachContext, write_outreach
from .enrich import enrich_leads
from .report import FOOTER, build_result
from .resolver import resolve_company
from .router import find_leads, person_hash, public_view
from .scoring import quality_label, score_lead
from .settings import MAX_PEOPLE_HARD_CAP, Settings
from .treg_client import search_apollo
from .verify import apply_verification, verify_lead

logger = logging.getLogger(__name__)

_WHAT_YOU_GET = {
    "standard": (
        f"Up to {MAX_PEOPLE_HARD_CAP} people, ranked by a quality score and checked against the company's domain, "
        "each with name, title, company, profile link, and a personalized email (with subject) and LinkedIn note. "
        "Public professional data only: no personal phone numbers, emails or addresses. Some names may be partly "
        "masked by the data provider; the Pro agent adds full names and LinkedIn profiles."
    ),
    "pro": (
        f"Up to {MAX_PEOPLE_HARD_CAP} people, ranked by a quality score, with full names and LinkedIn profiles "
        "where available, a verification status (employer, current role, data freshness), and a personalized "
        "email (with subject) and LinkedIn note for each. Public professional data only: no personal phone "
        "numbers, emails or addresses."
    ),
}


def input_schema(tier: str = "standard") -> dict:
    return {"input_data": [
        {
            "id": "company",
            "type": "text",
            "name": "Company name",
            "data": {
                "placeholder": "e.g. Chainlink Labs",
                "description": "The company whose team you want to reach. Use its usual name, e.g. 'Chainlink Labs', "
                               "'Coinbase', 'Uniswap Labs'.",
            },
            "validations": [{"validation": "min", "value": "2"}, {"validation": "max", "value": "100"}],
        },
        {
            "id": "company_domain",
            "type": "text",
            "name": "Company website domain (recommended)",
            "data": {
                "placeholder": "e.g. chain.link",
                "description": (
                    "Strongly recommended: the website the company uses, without https:// (e.g. chain.link, "
                    "coinbase.com). People are then looked up at that exact company. Without it the agent "
                    "guesses the domain from the name and may find fewer people."
                ),
            },
            "validations": [{"validation": "optional", "value": "true"}, {"validation": "max", "value": "100"}],
        },
        {
            "id": "role",
            "type": "text",
            "name": "Target role (optional)",
            "data": {
                "placeholder": "e.g. BD, partnerships, recruiter",
                "description": "One function or title. BD, partnerships and recruiter include common variants "
                               "(e.g. 'Head of Business Development', 'Ecosystem Partnerships', 'Talent Acquisition'). "
                               "Leave empty for the most senior people found.",
            },
            "validations": [{"validation": "optional", "value": "true"}, {"validation": "max", "value": "80"}],
        },
        {
            "id": "pitch",
            "type": "textarea",
            "name": "Your offer in one sentence (recommended)",
            "data": {
                "placeholder": "e.g. a cross-chain analytics dashboard for oracle node operators",
                "description": "What you want to talk about. The emails and LinkedIn notes are written around it.",
            },
            "validations": [{"validation": "optional", "value": "true"}, {"validation": "max", "value": "300"}],
        },
        {
            "id": "sender_name",
            "type": "text",
            "name": "Your name (optional)",
            "data": {"placeholder": "e.g. Alex Kim", "description": "Used to sign the emails."},
            "validations": [{"validation": "optional", "value": "true"}, {"validation": "max", "value": "60"}],
        },
        {
            "id": "sender_company",
            "type": "text",
            "name": "Your company (optional)",
            "data": {"placeholder": "e.g. NodeMetrics", "description": "Used in the emails and subject lines."},
            "validations": [{"validation": "optional", "value": "true"}, {"validation": "max", "value": "80"}],
        },
        {
            "id": "tone",
            "type": "option",
            "name": "Tone (optional)",
            "data": {"values": list(TONES), "description": "Friendly (default), formal, or concise."},
            "validations": [{"validation": "optional", "value": "true"}, {"validation": "min", "value": "1"},
                            {"validation": "max", "value": "1"}],
        },
        {
            "id": "notice",
            "type": "none",
            "name": "What you get",
            "data": {"description": f"{_WHAT_YOU_GET['pro' if tier == 'pro' else 'standard']} {FOOTER}"},
            # Display-only; masumi treats fields without "optional" as required.
            "validations": [{"validation": "optional", "value": "true"}],
        },
    ]}


INPUT_SCHEMA = input_schema("standard")


def _clean(value: Any, max_len: int) -> Optional[str]:
    if isinstance(value, list):  # option fields may arrive as a one-item list
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())[:max_len]
    return value or None


def _job_tag(identifier_from_purchaser: str, input_data: dict) -> str:
    seed = identifier_from_purchaser + "|" + repr(sorted(input_data.items()))
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _apollo_probe(settings: Settings, tag: str):
    """Resolver probe: organisation names Apollo returns for a domain (free, no titles)."""
    async def probe(domain: str) -> list[str]:
        found = await search_apollo(token=settings.treg_token, domain=domain, titles=None, per_page=5,
                                    job_tag=f"{tag}-probe-{domain}", base_url=settings.treg_base_url,
                                    timeout_s=settings.treg_timeout_s)
        names = []
        for row in found.people:
            org = row.get("organization")
            name = org.get("name") if isinstance(org, dict) else row.get("company") or row.get("organization_name")
            if isinstance(name, str):
                names.append(name)
        return names
    return probe


async def process_job(identifier_from_purchaser: str, input_data: dict, settings: Optional[Settings] = None) -> str:
    settings = settings or Settings.from_env()
    company = _clean(input_data.get("company"), 100)
    if not company:
        raise ValueError("company is required")
    role = _clean(input_data.get("role"), 80)
    domain = normalize_domain(_clean(input_data.get("company_domain"), 100)) or None
    tone = _clean(input_data.get("tone"), 20)
    ctx = OutreachContext(pitch=_clean(input_data.get("pitch"), 300),
                          sender_name=_clean(input_data.get("sender_name"), 60),
                          sender_company=_clean(input_data.get("sender_company"), 80),
                          tone=tone.lower() if tone and tone.lower() in TONES else "friendly")
    tag = _job_tag(identifier_from_purchaser, input_data)
    store = feedback.get_store()

    try:
        suppressed = await store.suppressed_hashes()
    except Exception as e:  # feedback storage must never block a paid job
        logger.warning("Suppression list unavailable: %s", type(e).__name__)
        suppressed = frozenset()

    resolved = await resolve_company(company, domain, _apollo_probe(settings, tag) if settings.treg_token else None)
    routed = await find_leads(settings, company, role, resolved, tag, suppressed=suppressed)
    leads = routed.leads

    enrichment = None
    if settings.is_pro and leads and settings.treg_token:
        enrichment = await enrich_leads(settings, leads, company, resolved.domain or "", tag)
        for lead in leads:
            if lead.get("_signals", {}).get("enriched"):
                score, reasons = score_lead(public_view(lead), role, lead["_signals"].get("evidence", "none"))
                lead.update(quality_score=score, quality=quality_label(score), match_reasons=reasons)

    for lead in leads:
        apply_verification(lead, verify_lead(lead, resolved.domain, resolved.verified))
    leads.sort(key=lambda lead: -lead["quality_score"])

    outreach, used_llm = await write_outreach(leads, ctx, api_key=settings.gemini_api_key,
                                              model=settings.gemini_model)
    logger.info("Delivering %d leads (tier %s, LLM outreach: %s, router spend %.4f USD, enrich %s)", len(leads),
                settings.tier, used_llm, routed.spent_usd, enrichment)

    link = feedback.feedback_link(settings.public_url, tag, settings.feedback_secret)
    if link:
        try:
            await store.remember_delivery(tag, company, [{"hash": person_hash(lead), "title": lead["title"]}
                                                         for lead in leads])
        except Exception as e:
            logger.warning("Could not record delivery for feedback: %s", type(e).__name__)
            link = None

    resolution = {"given_domain": resolved.given_domain, "domain": resolved.domain, "verified": resolved.verified,
                  "note": resolved.note}
    # Buyers see what was searched, not our costs or provider error details.
    search = [{"provider": s.provider, "query": s.query, "rows": s.rows, "kept": s.kept,
               "status": "ok" if not s.error else "skipped" if "budget" in s.error else "unavailable"}
              for s in routed.steps]
    if enrichment is not None:
        search.append({"provider": "apollo enrich", "query": f"{enrichment['enriched']} leads", "rows": 0,
                       "kept": enrichment["enriched"], "status": enrichment["error"] or "ok"})
    return build_result(company, role, [public_view(lead) for lead in leads], outreach, resolution, search,
                        routed.dropped_low_score, tier=settings.tier, feedback_url=link,
                        suppressed=routed.suppressed)
