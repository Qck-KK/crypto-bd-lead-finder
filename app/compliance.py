"""Data-minimisation filter: turns provider-native rows into public professional records only.

This is an allowlist, not a blocklist. A lead leaves the agent with exactly these fields:
name, title, company, profile_url (LinkedIn profile or the company's own website), source.
Everything else a provider returns (emails, phone numbers, addresses, socials, raw payloads)
is dropped, and email- or phone-like text is scrubbed from the fields that are kept.
"""

import logging
import re
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from .settings import DATA_SOURCE, MAX_PEOPLE_HARD_CAP

logger = logging.getLogger(__name__)

PUBLIC_FIELDS = ("name", "title", "company", "profile_url", "source")

# Optional label in front ("mobile:", "email ") goes with the value.
_LABEL = r"(?:\b(?:mobile|cell|phone|tel|whatsapp|e-?mail|mail|contact)\b\s*[:.]?\s*)?"
_EMAIL = re.compile(_LABEL + r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE)
_PHONE = re.compile(_LABEL + r"\+?\(?\d[\d\s().-]{5,}\d", re.IGNORECASE)

_NAME_KEYS = ("full_name", "fullName", "name", "person_name", "display_name")
_FIRST_KEYS = ("first_name", "firstName", "given_name")
# Apollo's free search masks last names ("S***h"); that is what it returns, so it is what we show.
_LAST_KEYS = ("last_name", "lastName", "family_name", "last_name_obfuscated")
_TITLE_KEYS = ("title", "job_title", "jobTitle", "position", "current_title", "role", "headline")
_COMPANY_KEYS = ("company", "company_name", "companyName", "organization_name", "organization",
                 "current_company", "employer")
_LINKEDIN_KEYS = ("linkedin_url", "linkedinUrl", "linkedin", "linkedin_profile_url",
                  "linkedin_profile", "profile_url", "profileUrl", "url")
_COMPANY_SITE_KEYS = ("website_url", "website", "domain", "primary_domain")


def scrub_contact_text(value: str) -> str:
    """Remove anything that looks like an email address or phone number."""
    value = _EMAIL.sub("", value)

    def _phone(m: re.Match) -> str:
        return "" if sum(c.isdigit() for c in m.group(0)) >= 7 else m.group(0)

    value = _PHONE.sub(_phone, value)
    return re.sub(r"\s{2,}", " ", value).strip(" ,;|-")


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("title")
    if not isinstance(value, str):
        return ""
    return scrub_contact_text(value.strip())[:200]


def _first(row: dict, keys: Iterable[str]) -> str:
    for key in keys:
        text = _text(row.get(key))
        if text:
            return text
    return ""


def normalize_domain(domain: Optional[str]) -> str:
    if not domain:
        return ""
    domain = domain.strip().lower()
    if "://" in domain:
        domain = urlparse(domain).hostname or ""
    domain = domain.split("/")[0]
    return domain.removeprefix("www.")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _as_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    url = value.strip()
    if "://" not in url:
        url = "https://" + url
    return url


def is_linkedin_profile(url: str) -> bool:
    parsed = urlparse(url)
    host = _host(url)
    return (
        parsed.scheme in ("http", "https")
        and (host == "linkedin.com" or host.endswith(".linkedin.com"))
        and (parsed.path.startswith("/in/") or parsed.path.startswith("/pub/"))
    )


def is_company_site(url: str, company_domain: str) -> bool:
    if not company_domain:
        return False
    parsed = urlparse(url)
    host = _host(url)
    return parsed.scheme in ("http", "https") and (host == company_domain or host.endswith("." + company_domain))


def _safe_url(url: str) -> bool:
    # No embedded addresses, credentials or phone links.
    return bool(url) and "@" not in url and not url.lower().startswith(("mailto:", "tel:"))


def pick_profile_url(row: dict, company_domain: str) -> str:
    for key in _LINKEDIN_KEYS:
        url = _as_url(row.get(key))
        if _safe_url(url) and is_linkedin_profile(url):
            return _strip_query(url)

    org = row.get("organization") if isinstance(row.get("organization"), dict) else {}
    domain = company_domain or normalize_domain(_first_str(org, _COMPANY_SITE_KEYS) or _first_str(row, ("company_domain",)))
    for source in (row, org):
        for key in _COMPANY_SITE_KEYS:
            url = _as_url(source.get(key))
            if _safe_url(url) and is_company_site(url, domain):
                return _strip_query(url)
    if domain:
        return f"https://{domain}"
    return ""


def _first_str(row: dict, keys: Iterable[str]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _strip_query(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


# Providers nest person fields, e.g. aiark: {"profile": {"full_name", "title"}, "link": {"linkedin"},
# "company": {"name", "domain"}}. Lifting these containers is safe because only allowlisted keys are read.
_NESTED = ("profile", "person", "link", "links", "social", "socials", "current_position", "position", "employment")
# Lists of jobs, e.g. harvestapi: {"currentPositions": [{"title", "companyName", "current"}]}; the current one is used.
_POSITION_LISTS = ("currentPositions", "current_positions", "positions", "experiences", "experience",
                   "employment_history")
_ORG_KEYS = ("organization", "company", "current_company", "employer")


def _current_position(items: list) -> Optional[dict]:
    jobs = [j for j in items if isinstance(j, dict)]
    return next((j for j in jobs if j.get("current") is True), jobs[0] if jobs else None)


def flatten_row(row: dict) -> dict:
    flat = dict(row)
    for key in _NESTED:
        inner = row.get(key)
        if isinstance(inner, dict):
            for k, v in inner.items():
                flat.setdefault(k, v)
    for key in _POSITION_LISTS:
        if isinstance(row.get(key), list):
            job = _current_position(row[key])
            if job:
                for k, v in job.items():
                    flat.setdefault(k, v)
                break
    for key in _ORG_KEYS:
        if isinstance(row.get(key), dict):
            flat.setdefault("organization", row[key])
            break
    return flat


def to_public_lead(row: dict, *, provider: str, company_fallback: str, company_domain: str) -> Optional[dict]:
    """Return a lead with only PUBLIC_FIELDS, or None if the row has no usable name."""
    row = flatten_row(row)
    name = _first(row, _NAME_KEYS)
    if not name:
        name = " ".join(p for p in (_first(row, _FIRST_KEYS), _first(row, _LAST_KEYS)) if p)
    if not name:
        return None
    source = f"{DATA_SOURCE} ({provider})" if provider else DATA_SOURCE
    return {
        "name": name,
        "title": _first(row, _TITLE_KEYS),
        "company": _first(row, _COMPANY_KEYS) or company_fallback,
        "profile_url": pick_profile_url(row, normalize_domain(company_domain)),
        "source": source,
    }


_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "limited", "co", "corp", "corporation", "company", "gmbh", "ag", "sa",
                     "plc", "bv", "labs", "lab", "foundation", "group", "holdings", "technologies", "technology",
                     "the", "network", "protocol"}


# Words that do not change which company is meant when comparing names ("Coinbase Global" is Coinbase,
# "Gate.io" is Gate). Only used for matching, not for domain guesses.
_MATCH_ONLY_SUFFIXES = {"global", "international", "hq", "io", "com", "xyz", "org"}


def _core_name_tokens(name: str) -> list[str]:
    """Name tokens in order, without legal/generic suffixes (all tokens if nothing else is left)."""
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    core = [t for t in tokens if t not in _COMPANY_SUFFIXES and t not in _MATCH_ONLY_SUFFIXES]
    return core or tokens


def same_company(a: str, b: str) -> bool:
    """Strict: same core name tokens ("Chainlink Labs" == "chainlink" == "Chain Link", but not
    "Chainlink Plumbing" or "Link Metal")."""
    ta, tb = _core_name_tokens(a), _core_name_tokens(b)
    # "chainlink" vs "chain link": compare with spaces removed, word order kept.
    return bool(ta) and (set(ta) == set(tb) or "".join(ta) == "".join(tb))


def company_matches(lead_company: str, wanted: str, wanted_domain: str = "", lead_url: str = "") -> bool:
    """True if the lead's company is the requested one: a link on the requested domain, or the same core
    name (same_company). Extra name words are not accepted: "Link" must not match "Link Metal"."""
    # The bare https://<wanted_domain> is our own fallback, not evidence from the provider.
    if wanted_domain and lead_url and lead_url != f"https://{wanted_domain}" and is_company_site(lead_url, wanted_domain):
        return True
    return same_company(wanted, lead_company)


# Ordered from closest to loosest; leads are ranked by the first phrase their title contains.
# Bare "ecosystem" or "integrations" also hit marketing, media and engineering titles, so only
# explicit phrases are used.
_BD = ("business development", "bd", "biz dev", "bizdev", "market development", "partnerships", "partnership",
       "strategic partnerships", "ecosystem development", "ecosystem growth", "ecosystem partnerships", "alliances",
       "commercial")
# No bare "partner": it also means "Talent Acquisition Partner" and "HR Business Partner".
_PARTNERSHIPS = ("partnerships", "partnership", "alliances", "ecosystem partnerships", "business development", "bd",
                 "channel partnerships", "partner manager", "partner success", "partner lead",
                 "strategic partner")
_RECRUITING = ("recruiter", "recruiting", "recruitment", "talent", "talent acquisition", "sourcer", "people partner",
               "hiring")
_ROLE_SYNONYMS = {
    "bd": _BD, "business development": _BD, "biz dev": _BD,
    "partnerships": _PARTNERSHIPS, "partnership": _PARTNERSHIPS,
    "recruiter": _RECRUITING, "recruiting": _RECRUITING, "recruitment": _RECRUITING, "talent": _RECRUITING,
}


def _norm_words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def role_terms(role: Optional[str]) -> list[str]:
    """Title phrases for a role: the role itself plus BD / partnerships / recruiting synonyms."""
    if not role:
        return []
    role_l = _norm_words(role)
    return list(dict.fromkeys([role_l, *_ROLE_SYNONYMS.get(role_l, ())]))


def role_rank(title: str, role: Optional[str]) -> Optional[int]:
    """Index of the closest role phrase in the title (0 = the role itself), or None if it does not fit."""
    if not role:
        return 0
    title_l = f" {_norm_words(title)} "
    terms = role_terms(role)
    for i, term in enumerate(terms):
        if f" {term} " in title_l:
            return i
    if all(f" {w} " in title_l for w in _norm_words(role).split()):
        return len(terms)
    return None


def role_matches(title: str, role: Optional[str]) -> bool:
    """True if the job title fits the requested role (phrase match against role_terms)."""
    return role_rank(title, role) is not None


def guess_domains(company: str, given: Optional[str] = None, max_guesses: int = 4) -> list[str]:
    """Domains to try for a company: the given one first, then guesses from the name.

    Data providers index a company under its primary domain, which is not always the one people type
    (Apollo knows Chainlink Labs as chainlinklabs.com, not chain.link).
    """
    tokens = re.findall(r"[a-z0-9]+", company.lower())
    core = [t for t in tokens if t not in _COMPANY_SUFFIXES] or tokens
    stems = list(dict.fromkeys(["".join(tokens), "".join(core)]))
    guesses = [f"{s}.{tld}" for tld in ("com", "io", "xyz", "org") for s in stems if s]
    first = [normalize_domain(given)] if given else []
    out = list(dict.fromkeys(d for d in first + guesses if d))
    return out[: len(first) + max_guesses]


def row_signals(row: dict) -> dict:
    """Non-personal facts about a row, used for verification and enrichment, never delivered as-is:
    provider record id, last refresh date, whether the position is marked current, the org's domain."""
    flat = flatten_row(row)
    org = flat.get("organization") if isinstance(flat.get("organization"), dict) else {}
    refreshed = flat.get("last_refreshed_at") or flat.get("updated_at") or flat.get("last_updated")
    current = flat.get("current") if isinstance(flat.get("current"), bool) else None
    return {
        "id": row.get("id") if isinstance(row.get("id"), str) else None,
        "refreshed_at": refreshed if isinstance(refreshed, str) else None,
        "current": current,
        "org_domain": normalize_domain(_first_str(org, ("primary_domain", "domain", "website_url", "website"))),
    }


def filter_people(
    rows: Iterable[dict],
    *,
    provider: str,
    company: str,
    company_domain: Optional[str],
    limit: int = MAX_PEOPLE_HARD_CAP,
    role: Optional[str] = None,
    with_rows: bool = False,
) -> list:
    """Public leads that match the company and role, closest titles first.

    with_rows=True returns (lead, source_row) pairs so callers can read row_signals; the row
    itself must never be delivered.
    """
    limit = max(0, min(limit, MAX_PEOPLE_HARD_CAP))
    ranked: list[tuple[int, dict, dict]] = []
    seen: set[tuple[str, str]] = set()
    mismatched = off_role = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        lead = to_public_lead(row, provider=provider, company_fallback=company, company_domain=company_domain or "")
        if not lead:
            # Field names only, never values: enough to extend the mapping for a new provider shape.
            logger.info("Dropped a %s row without a usable name; fields: %s", provider or "?", sorted(row)[:30])
            continue
        # Keyword-matching providers return people at look-alike companies ("Link Metal" for "Chainlink").
        if not company_matches(lead["company"], company, normalize_domain(company_domain), lead["profile_url"]):
            mismatched += 1
            continue
        rank = role_rank(lead["title"], role)
        if rank is None:
            off_role += 1
            continue
        key = (lead["name"].lower(), lead["company"].lower())
        if key in seen:
            continue
        seen.add(key)
        ranked.append((rank, lead, row))
    if mismatched or off_role:
        logger.info("Dropped %d rows from other companies than %r and %d not matching role %r",
                    mismatched, company, off_role, role)
    # Closest titles first; the sort is stable, so the provider's order breaks ties.
    ranked.sort(key=lambda item: item[0])
    if with_rows:
        return [(lead, row) for _, lead, row in ranked[:limit]]
    return [lead for _, lead, _ in ranked[:limit]]
