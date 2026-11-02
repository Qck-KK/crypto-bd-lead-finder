"""Lead quality score (0-100): how useful a lead is for B2B outreach, with the reasons.

| Signal            | Points                                                        |
|-------------------|---------------------------------------------------------------|
| Role fit          | 35 exact role phrase, 22 synonym, 25 no role asked, 0 no title |
| Seniority         | 20 head/VP/director/C-level/founder, 12 lead/manager/senior, 5 |
| Name              | 15 full name, 5 masked by the provider ("S***h")               |
| Profile link      | 20 LinkedIn profile, 6 company website, 0 none                |
| Company evidence  | 10 matched by domain, 6 by name, 0 none                        |

High >= 70, Medium >= 45, otherwise Low. A lead the buyer cannot identify (masked name and no individual
profile) is rated at most Medium, whatever its score. Each lead carries its points per component
(`score_breakdown`) and the reasons.
"""

import re
from typing import Optional

from .compliance import is_linkedin_profile, role_rank

_SENIOR = ("chief", "ceo", "coo", "cto", "cmo", "cbo", "cro", "founder", "cofounder", "co founder", "president",
           "vp", "vice president", "head", "director", "general manager", "managing partner", "general partner")
_MID = ("lead", "principal", "manager", "senior", "sr", "staff")


def _words(text: str) -> str:
    return f" {re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()} "


def seniority(title: str) -> str:
    t = _words(title)
    if any(f" {w} " in t for w in _SENIOR):
        return "senior"
    if any(f" {w} " in t for w in _MID):
        return "mid"
    return "other"


CAP_REASON = "rated at most Medium: masked name and no individual profile"


def quality_label(score: int) -> str:
    return "High" if score >= 70 else "Medium" if score >= 45 else "Low"


def is_identifiable(lead: dict) -> bool:
    """The buyer can tell who this is: a full name or an individual LinkedIn profile."""
    return "*" not in lead.get("name", "") or is_linkedin_profile(lead.get("profile_url") or "")


def rate(lead: dict, score: int, reasons: list[str], breakdown: dict) -> None:
    """Set score, label, reasons and breakdown on a lead; unidentifiable leads are never High."""
    reasons = [r for r in reasons if r != CAP_REASON]
    label = quality_label(score)
    if label == "High" and not is_identifiable(lead):
        label = "Medium"
        reasons.append(CAP_REASON)
    lead.update(quality_score=score, quality=label, match_reasons=reasons, score_breakdown=breakdown)


def score_parts(lead: dict, role: Optional[str], company_evidence: str) -> tuple[int, list[str], dict]:
    """(score, reasons, points per component). company_evidence: "domain", "name" or "none"."""
    reasons: list[str] = []
    parts = {"role": 0, "seniority": 0, "name": 0, "profile": 0, "company": 0}
    title = lead.get("title") or ""

    if not title:
        reasons.append("no job title")
    elif not role:
        parts["role"] = 25
    else:
        rank = role_rank(title, role)
        if rank == 0:
            parts["role"] = 35
            reasons.append(f"title matches '{role}'")
        elif rank is not None:
            parts["role"] = 22
            reasons.append(f"title is related to '{role}'")

    if title:
        level = seniority(title)
        parts["seniority"] = {"senior": 20, "mid": 12, "other": 5}[level]
        if level == "senior":
            reasons.append("senior role")

    if "*" in lead.get("name", ""):
        parts["name"] = 5
        reasons.append("last name masked by the provider")
    else:
        parts["name"] = 15
        reasons.append("full name")

    url = lead.get("profile_url") or ""
    if is_linkedin_profile(url):
        parts["profile"] = 20
        reasons.append("LinkedIn profile")
    elif url:
        parts["profile"] = 6
        reasons.append("no individual profile (company website only)")
    else:
        reasons.append("no profile link")

    parts["company"] = {"domain": 10, "name": 6}.get(company_evidence, 0)
    if company_evidence == "domain":
        reasons.append("company confirmed by domain")

    return min(sum(parts.values()), 100), reasons, parts


def score_lead(lead: dict, role: Optional[str], company_evidence: str) -> tuple[int, list[str]]:
    score, reasons, _ = score_parts(lead, role, company_evidence)
    return score, reasons
