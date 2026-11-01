"""Lead quality score (0-100): how useful a lead is for B2B outreach, with the reasons.

| Signal            | Points                                                        |
|-------------------|---------------------------------------------------------------|
| Role fit          | 35 exact role phrase, 22 synonym, 25 no role asked, 0 no title |
| Seniority         | 20 head/VP/director/C-level/founder, 12 lead/manager/senior, 5 |
| Name              | 15 full name, 5 masked by the provider ("S***h")               |
| Profile link      | 20 LinkedIn profile, 6 company website, 0 none                |
| Company evidence  | 10 matched by domain, 6 by name, 0 none                        |

High >= 70, Medium >= 45, otherwise Low.
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


def quality_label(score: int) -> str:
    return "High" if score >= 70 else "Medium" if score >= 45 else "Low"


def score_lead(lead: dict, role: Optional[str], company_evidence: str) -> tuple[int, list[str]]:
    """company_evidence: "domain", "name" or "none"."""
    reasons: list[str] = []
    score = 0
    title = lead.get("title") or ""

    if not title:
        reasons.append("no job title")
    elif not role:
        score += 25
    else:
        rank = role_rank(title, role)
        if rank == 0:
            score += 35
            reasons.append(f"title matches '{role}'")
        elif rank is not None:
            score += 22
            reasons.append(f"title is related to '{role}'")

    if title:
        level = seniority(title)
        score += {"senior": 20, "mid": 12, "other": 5}[level]
        if level == "senior":
            reasons.append("senior role")

    if "*" in lead.get("name", ""):
        score += 5
        reasons.append("last name masked by the provider")
    else:
        score += 15

    url = lead.get("profile_url") or ""
    if is_linkedin_profile(url):
        score += 20
        reasons.append("LinkedIn profile")
    elif url:
        score += 6
        reasons.append("company website only")

    score += {"domain": 10, "name": 6}.get(company_evidence, 0)
    if company_evidence == "domain":
        reasons.append("company confirmed by domain")

    return min(score, 100), reasons
