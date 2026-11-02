"""Data provider router: free sources first, paid ones only when needed, within a per-job budget.

Order:
  1. apollo.people.search on the resolved domain (free on treg, filters by title on its side)
  2. treg.people.search by company_domain (routed, usually cents) if fewer than
     MIN_LEADS_BEFORE_PAID leads so far
  3. treg.people.search by name if the domain could not be confirmed and leads are still short

Every row goes through the compliance filter, then gets a quality score. The same person found by
two providers is merged and the better-scoring record kept (a full name beats a masked one).

Spend: every call, Apollo included, is sent only if the job's daily-cap grant (`JobBudget`) covers its max
cost, and its cost is charged to that grant. TREG_JOB_BUDGET_USD additionally caps the routed calls.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .compliance import filter_people, role_terms, row_signals
from .resolver import ResolvedCompany
from .scoring import rate, score_parts
from .settings import Settings
from .spend import JobBudget
from .treg_client import APOLLO_MAX_COST_USD, TregError, search_apollo, search_people

logger = logging.getLogger(__name__)

FETCH_ROWS = 25


@dataclass
class SearchStep:
    provider: str
    query: str
    rows: int = 0
    kept: int = 0
    cost_usd: float = 0.0
    error: str = ""


@dataclass
class RouteResult:
    leads: list[dict]
    steps: list[SearchStep] = field(default_factory=list)
    spent_usd: float = 0.0  # routed calls only (TREG_JOB_BUDGET_USD); the JobBudget holds every call
    dropped_low_score: int = 0
    suppressed: int = 0


def person_key(lead: dict) -> tuple:
    """First name + first/last letter of the last name + company, so "Jane S***h" meets "Jane Smith"."""
    parts = re.findall(r"[\w*]+", lead["name"].lower())
    first = parts[0] if parts else ""
    last = parts[-1].replace("*", "") if len(parts) > 1 else ""
    company = re.sub(r"[^a-z0-9]", "", lead["company"].lower())
    return first, last[:1], last[-1:], company


def person_hash(lead: dict) -> str:
    """Stored instead of names (feedback suppression): one-way, not reversible to a name."""
    return hashlib.sha256("|".join(person_key(lead)).encode()).hexdigest()[:32]


def public_view(lead: dict) -> dict:
    """Drop internal keys (leading underscore) before anything leaves the agent."""
    return {k: v for k, v in lead.items() if not k.startswith("_")}


class _Pool:
    def __init__(self, company: str, role: Optional[str], suppressed: frozenset = frozenset()):
        self.company, self.role, self.suppressed = company, role, suppressed
        self.by_person: dict[tuple, dict] = {}
        self.suppressed_hits = 0

    def add(self, rows: list[dict], provider: str, domain: Optional[str], evidence: str) -> int:
        kept = filter_people(rows, provider=provider, company=self.company, company_domain=domain, role=self.role,
                             with_rows=True)
        for lead, row in kept:
            if person_hash(lead) in self.suppressed:
                self.suppressed_hits += 1
                continue
            score, reasons, parts = score_parts(lead, self.role, evidence)
            signals = {**row_signals(row), "providers": [provider or "?"], "evidence": evidence}
            scored = {**lead, "_signals": signals}
            rate(scored, score, reasons, parts)
            key = person_key(lead)
            old = self.by_person.get(key)
            if old:
                # Same person from another provider: keep the better record, remember every source.
                providers = sorted(set(old["_signals"]["providers"]) | {provider or "?"})
                best, other = (scored, old) if score > old["quality_score"] else (old, scored)
                merged = {k: best["_signals"].get(k) or other["_signals"].get(k) for k in best["_signals"]}
                best["_signals"] = {**merged, "providers": providers}
                self.by_person[key] = best
            else:
                self.by_person[key] = scored
        return len(kept)

    def count(self, min_score: int) -> int:
        return sum(1 for lead in self.by_person.values() if lead["quality_score"] >= min_score)

    def best(self, limit: int, min_score: int) -> tuple[list[dict], int]:
        good = [lead for lead in self.by_person.values() if lead["quality_score"] >= min_score]
        good.sort(key=lambda lead: -lead["quality_score"])  # stable: provider order breaks ties
        return good[:limit], len(self.by_person) - len(good)


async def find_leads(settings: Settings, company: str, role: Optional[str], resolved: ResolvedCompany,
                     tag: str, suppressed: frozenset = frozenset(), budget: Optional[JobBudget] = None) -> RouteResult:
    """suppressed: person_hash values buyers reported as gone or wrong; never delivered again.
    budget: the job's share of the daily cap; None = unmetered (tools and tests only)."""
    budget = budget if budget is not None else JobBudget()
    pool = _Pool(company, role, suppressed)
    result = RouteResult(leads=[])
    succeeded = False

    def enough() -> bool:
        return pool.count(settings.min_lead_score) >= settings.min_leads_before_paid

    # "domain" only when the provider confirmed the domain belongs to this company (BL-04).
    domain_evidence = "domain" if resolved.verified else "given domain"

    if resolved.domain:
        step = SearchStep("apollo", resolved.domain)
        if not budget.covers(APOLLO_MAX_COST_USD):
            step.error = "daily spend budget used up"
        else:
            try:
                found = await search_apollo(token=settings.treg_token, domain=resolved.domain,
                                            titles=role_terms(role) or None, per_page=FETCH_ROWS,
                                            job_tag=f"{tag}-ap", base_url=settings.treg_base_url,
                                            timeout_s=settings.treg_timeout_s)
                step.rows, step.cost_usd = len(found.people), budget.charge_call(found)
                step.kept = pool.add(found.people, found.served_by, resolved.domain, domain_evidence)
                succeeded = True
            except TregError as e:
                step.error, step.cost_usd = str(e), budget.charge_call(e)
        result.steps.append(step)

    paid = []
    if resolved.domain:
        paid.append(("domain", resolved.domain))
    if not resolved.verified:
        paid.append(("name", None))

    for i, (mode, domain) in enumerate(paid):
        if enough():
            break
        remaining = min(settings.job_budget_usd - result.spent_usd, budget.remaining)
        step = SearchStep(f"treg routed ({mode})", domain or company)
        if remaining < 0.001:
            step.error = "job budget used up" if budget.remaining >= 0.001 else "daily spend budget used up"
            result.steps.append(step)
            break
        try:
            found = await search_people(
                token=settings.treg_token, company=company, role=role, company_domain=domain,
                limit=FETCH_ROWS if (domain and role) else settings.max_people,
                max_cost_usd=min(settings.treg_max_cost_usd, remaining), job_tag=f"{tag}-r{i}",
                base_url=settings.treg_base_url, timeout_s=settings.treg_timeout_s)
            step.rows, step.cost_usd = len(found.people), budget.charge_call(found)
            step.provider = f"treg routed ({mode}) via {found.served_by or '?'}"
            step.kept = pool.add(found.people, found.served_by, domain, domain_evidence if domain else "name")
            succeeded = True
        except TregError as e:
            step.error, step.cost_usd = str(e), budget.charge_call(e)
        result.spent_usd += step.cost_usd
        result.steps.append(step)

    for s in result.steps:
        logger.info("search %s %s: rows=%d kept=%d cost=%.4f %s", s.provider, s.query, s.rows, s.kept, s.cost_usd,
                    s.error)
    if not succeeded and result.steps:
        # Every provider failed or could not be afforded: fail the job (the buyer can get a refund) instead of
        # delivering nothing.
        raise TregError("all data providers failed: " + "; ".join(s.error for s in result.steps if s.error))

    result.leads, result.dropped_low_score = pool.best(settings.max_people, settings.min_lead_score)
    result.suppressed = pool.suppressed_hits
    return result
