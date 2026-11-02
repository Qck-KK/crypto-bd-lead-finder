"""Lead verification from evidence we already have; no extra requests, no scraping.

Checks (each true / false / unknown):
- company:  employer confirmed by the resolved domain (domain search or the org's domain)
- current:  provider marks the position as current (Apollo search only returns current staff)
- fresh:    provider refreshed the record within FRESH_DAYS
- sources:  found by two or more providers
- profile:  a LinkedIn profile URL is present

Status: "Verified" = company + current + fresh; "Likely" = company and (current or fresh);
otherwise "Unverified". Verified adds 5 to the quality score, Unverified subtracts 5.
"""

from datetime import datetime, timezone
from typing import Optional

from .compliance import is_linkedin_profile, normalize_domain
from .scoring import rate

FRESH_DAYS = 365
# Providers whose search only returns people currently employed at the searched company.
_CURRENT_BY_SEARCH = ("apollo",)


def _age_days(value: Optional[str], now: datetime) -> Optional[int]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (now - dt).days)


def verify_lead(lead: dict, resolved_domain: Optional[str], verified_domain: bool,
                now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    s = lead.get("_signals", {})
    providers = s.get("providers") or []
    org_domain = normalize_domain(s.get("org_domain"))

    company = (s.get("evidence") == "domain" and verified_domain) or (
        bool(org_domain) and bool(resolved_domain) and org_domain == normalize_domain(resolved_domain))
    current = s.get("current")
    # Apollo's domain search returns only current staff, whether or not we could confirm the domain.
    if current is None and any(p.startswith(_CURRENT_BY_SEARCH) for p in providers) and s.get("evidence") in (
            "domain", "given domain"):
        current = True
    age = _age_days(s.get("refreshed_at"), now)
    fresh = None if age is None else age <= FRESH_DAYS

    checks = {
        "company": bool(company),
        "current": current,
        "fresh": fresh,
        "sources": len(set(providers)) >= 2,
        "profile": is_linkedin_profile(lead.get("profile_url") or ""),
    }
    if checks["company"] and current and fresh:
        status = "Verified"
    elif checks["company"] and (current or fresh):
        status = "Likely"
    else:
        status = "Unverified"

    notes = []
    if checks["company"]:
        notes.append("employer confirmed by domain")
    if current:
        notes.append("current role per provider")
    if age is not None:
        notes.append(f"record refreshed {age} days ago")
    if checks["sources"]:
        notes.append(f"found by {len(set(providers))} sources")
    return {"status": status, "checks": checks, "notes": notes}


def apply_verification(lead: dict, verification: dict) -> None:
    """Attach the verification and adjust the score: +5 Verified, -5 Unverified."""
    delta = {"Verified": 5, "Unverified": -5}.get(verification["status"], 0)
    rate(lead, max(0, min(100, lead["quality_score"] + delta)), lead.get("match_reasons") or [],
         {**(lead.get("score_breakdown") or {}), "employment_check": delta})
    lead["verification"] = verification
