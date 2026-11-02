"""Job handler: input -> domain resolver -> provider router (via treg) -> compliance filter + scoring
-> (Pro) enrichment -> verification -> personalized outreach -> report with a feedback link."""

import asyncio
import dataclasses
import logging
import time
import uuid
from typing import Any, Optional

from . import feedback
from .compliance import normalize_domain
from .drafts import TONES, OutreachContext, write_outreach
from .enrich import enrich_leads, needs_enrichment
from .job_identity import JobIdentity, current_identity, legacy_tag
from .ledger import get_ledger
from .logs import job_tag_var
from .pricing import estimate
from .report import FOOTER, build_result
from .resolver import candidate_domains, resolve_company
from .router import find_leads, person_hash, public_view
from .scoring import rate, score_parts
from .settings import MAX_PEOPLE_HARD_CAP, Settings
from .spend import JobBudget
from .treg_client import APOLLO_MAX_COST_USD, search_apollo
from .verify import apply_verification, verify_lead

logger = logging.getLogger(__name__)

# An attempt's hold counts until the end of the UTC day containing created_at + ATTEMPT_TIMEOUT_S + this grace:
# time for a timed-out attempt to settle after it was cancelled.
ATTEMPT_GRACE_S = 600

_NO_CONTACTS = "Public professional data only: no personal phone numbers, emails or addresses."
_AS_PROVIDED = ("Some names may be partly masked by the data provider, and people without a public profile link "
                "to the company website.")


def what_you_get(tier: str = "standard", enrich_people: int = 0) -> str:
    """Buyer-facing description of the output. Promises enrichment only when it is actually on."""
    base = (f"Up to {MAX_PEOPLE_HARD_CAP} people, ranked by a quality score and checked against the company's "
            "domain, each with name, title, company, profile link, a verification status (employer, current role, "
            "data freshness), and a personalized email (with subject) and LinkedIn note.")
    if tier != "pro":
        return f"{base} {_AS_PROVIDED} {_NO_CONTACTS}"
    if enrich_people:
        return (f"{base} Pro completes masked names and missing LinkedIn profiles for up to {enrich_people} people "
                f"per job where the provider has them. {_NO_CONTACTS}")
    return f"{base} Enrichment is currently off on this agent. {_AS_PROVIDED} {_NO_CONTACTS}"


def input_schema(tier: str = "standard", enrich_people: int = 0) -> dict:
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
            "data": {"description": f"{what_you_get(tier, enrich_people)} {FOOTER}"},
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


_job_tag = legacy_tag  # pre-v2 tag; kept for tools that look up old results


def _apollo_probe(settings: Settings, tag: str, budget: JobBudget):
    """Resolver probe: organisation names Apollo returns for a domain (free, no titles). Metered like any
    treg call (`JobBudget.run`); a refused or failed probe raises, which the resolver treats as no answer."""
    async def probe(domain: str) -> list[str]:
        found = await budget.run(APOLLO_MAX_COST_USD, lambda: search_apollo(
            token=settings.treg_token, domain=domain, titles=None, per_page=5, job_tag=f"{tag}-probe-{domain}",
            base_url=settings.treg_base_url, timeout_s=settings.treg_timeout_s,
            trust_refusals=settings.trust_uncharged_refusals))
        names = []
        for row in found.people:
            org = row.get("organization")
            name = org.get("name") if isinstance(org, dict) else row.get("company") or row.get("organization_name")
            if isinstance(name, str):
                names.append(name)
        return names
    return probe


async def stored_result(ledger, identity: JobIdentity) -> Optional[str]:
    """The result already delivered for this order. Results stored before the v2 identity are found by the old tag
    only for an order created before v2, from the same tier, stored after that order was created (BL-36)."""
    found = await ledger.get_result(identity.tag)
    if found is None and identity.payment_id and identity.job_created_at is not None:
        found = await ledger.get_legacy_result(identity.legacy_tag, identity.tier, identity.job_created_at)
        if found is not None:
            logger.info("Returning a result stored before the job identity upgrade")
    return found


async def process_job(identifier_from_purchaser: str, input_data: dict, settings: Optional[Settings] = None) -> str:
    """MIP-003 job handler. Idempotent per order: a re-run of the same payment returns the stored result."""
    settings = settings or Settings.from_env()
    identity = await current_identity(identifier_from_purchaser, input_data, settings.tier)
    tag = identity.tag
    token = job_tag_var.set(tag)
    ledger = get_ledger()
    try:
        try:
            cached = await stored_result(ledger, identity)
        except Exception as e:  # cache problems must not block a paid job; it then runs normally
            logger.warning("Result cache unavailable: %s", type(e).__name__)
            cached = None
        if cached is not None:
            logger.info("Re-run of a delivered job: returning the stored result (same on-chain hash)")
            return cached

        started = time.time()
        metrics = {"leads": 0, "llm_used": False}
        attempt_id = uuid.uuid4().hex  # this run's own spend reservation; a retry gets a new one

        async def progress(snapshot: dict) -> None:
            await ledger.progress(attempt_id, snapshot)

        budget = JobBudget(granted=0.0, progress=progress)  # nothing may be spent until _run_job has reserved
        try:
            # The hold of this attempt counts until a deadline derived from this limit (see ledger.reserve).
            async with asyncio.timeout(settings.attempt_timeout_s):
                result = await _run_job(input_data, settings, tag, metrics, budget, attempt_id)
        except Exception:
            await _record(ledger, settings, tag, attempt_id, input_data, started, metrics, budget, "failed")
            raise
        try:
            await ledger.put_result(tag, settings.tier, result)
        except Exception as e:
            logger.warning("Could not store the result for re-runs: %s", type(e).__name__)
        await _record(ledger, settings, tag, attempt_id, input_data, started, metrics, budget,
                      "delivered" if metrics["leads"] else "empty")
        return result
    finally:
        job_tag_var.reset(token)


async def _record(ledger, settings: Settings, tag: str, attempt_id: str, input_data: dict, started: float,
                  metrics: dict, budget: JobBudget, outcome: str) -> None:
    """Settles this attempt: its spend replaces its own hold (never another attempt's). Never raises (a paid job
    must not fail here); if the ledger is down the hold stays open and counts until its counts_until."""
    now = time.time()
    row = {"tag": tag, "tier": settings.tier, "company": str(input_data.get("company") or "")[:100],
           "started_at": started, "finished_at": now, "duration_ms": int((now - started) * 1000),
           "price_tusdm": estimate(settings.tier)["price_tusdm"], "outcome": outcome, **metrics, **budget.totals()}
    logger.info("job finished outcome=%s leads=%d treg_usd=%.4f (unconfirmed %.4f) enrich_usd=%.4f "
                "cost_assumed_calls=%d duration_ms=%d", outcome, metrics["leads"], row["treg_usd"],
                row["unconfirmed_usd"], row["enrich_usd"], row["cost_assumed_calls"], row["duration_ms"])
    try:
        await ledger.settle(attempt_id, row, budget.snapshot())
    except Exception as e:
        logger.warning("Could not write the cost ledger: %s", type(e).__name__)


def _spend_ceiling(settings: Settings, company: str, domain: Optional[str]) -> float:
    """Worst-case treg spend of one job: every domain probe and the Apollo search at their max cost (free today,
    but metered), the routed budget, and (Pro) the enrichment budget."""
    apollo_calls = len(candidate_domains(company, domain)) + 1
    enrich = settings.enrich_budget_usd if settings.enrichment_enabled else 0.0
    return round(apollo_calls * APOLLO_MAX_COST_USD + settings.job_budget_usd + enrich, 6)


async def _reserve(settings: Settings, tag: str, attempt_id: str, want_usd: float, company: str) -> float:
    try:
        granted = await get_ledger().reserve(attempt_id, tag, settings.tier, want_usd, settings.daily_spend_cap_usd,
                                             lifetime_s=settings.attempt_timeout_s + ATTEMPT_GRACE_S, company=company)
    except Exception as e:
        # Without the ledger we cannot prove we are under the cap, so no treg call is made.
        logger.warning("Spend ledger unavailable, no treg calls for this job: %s", type(e).__name__)
        return 0.0
    if granted + 1e-9 < want_usd:
        logger.warning("Daily spend cap %.2f USD: reserved %.4f of %.4f USD for this job",
                       settings.daily_spend_cap_usd, granted, want_usd)
    return granted


async def _run_job(input_data: dict, settings: Settings, tag: str, metrics: dict, budget: JobBudget,
                   attempt_id: str) -> str:
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
    store = feedback.get_store()

    try:
        suppressed = await store.suppressed_hashes()
    except Exception as e:  # feedback storage must never block a paid job
        logger.warning("Suppression list unavailable: %s", type(e).__name__)
        suppressed = frozenset()

    # Reserve before the first treg call (the resolver probe). Every attempt has its own hold.
    budget.granted = await _reserve(settings, tag, attempt_id, _spend_ceiling(settings, company, domain), company)
    resolved = await resolve_company(company, domain,
                                     _apollo_probe(settings, tag, budget) if settings.treg_token else None)
    routed = await find_leads(settings, company, role, resolved, tag, suppressed=suppressed, budget=budget)
    leads = routed.leads

    # What the report says about enrichment (Pro only); never claims work that did not happen.
    enrichment = None
    if settings.is_pro:
        candidates = sum(needs_enrichment(lead) for lead in leads)
        enrichment = {"enabled": settings.enrichment_enabled, "candidates": candidates, "enriched": 0,
                      "status": "off" if not settings.enrichment_enabled else "not needed" if not candidates else "ok"}
        if settings.enrichment_enabled and candidates:
            enrich_settings = dataclasses.replace(
                settings, enrich_budget_usd=min(settings.enrich_budget_usd, budget.remaining))
            if enrich_settings.enrich_people_per_job == 0:
                enrichment["status"] = "daily spend cap reached"
            else:
                done = await enrich_leads(enrich_settings, leads, company, resolved.domain or "", tag, budget=budget)
                enrichment.update(enriched=done["enriched"], status=done["error"] or "ok")
        for lead in leads:
            if lead.get("_signals", {}).get("enriched"):
                rate(lead, *score_parts(public_view(lead), role, lead["_signals"].get("evidence", "none")))

    for lead in leads:
        apply_verification(lead, verify_lead(lead, resolved.domain, resolved.verified))
    leads.sort(key=lambda lead: -lead["quality_score"])

    outreach, used_llm = await write_outreach(leads, ctx, api_key=settings.gemini_api_key,
                                              model=settings.gemini_model, timeout_s=settings.gemini_timeout_s)
    metrics.update(leads=len(leads), llm_used=used_llm)
    logger.info("Delivering %d leads (tier %s, LLM outreach: %s, treg spend %.4f USD, enrich %s)", len(leads),
                settings.tier, used_llm, budget.treg_usd, enrichment)

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
    return build_result(company, role, [public_view(lead) for lead in leads], outreach, resolution, search,
                        routed.dropped_low_score, tier=settings.tier, feedback_url=link,
                        suppressed=routed.suppressed, enrichment=enrichment)
