"""Company domain resolver: turns what the buyer typed into the domain data providers know.

Providers index a company under its primary domain, which is often not the one people type
(Apollo knows Chainlink Labs as chainlinklabs.com, not chain.link). Candidates, in order:
the given domain, its registrable root (careers.chain.link -> chain.link), then domains guessed
from the name. Each is checked with a probe (by default Apollo's free people search) and accepted
only if the people found there work at a company whose name matches. Results are cached.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .compliance import guess_domains, normalize_domain, same_company

logger = logging.getLogger(__name__)

# Second-level suffixes where the registrable domain has three labels (example.co.uk).
_TWO_PART_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.jp", "com.cn", "com.sg", "com.hk",
                      "co.kr", "com.br", "co.in", "co.za", "com.tr", "com.mx"}

CACHE_TTL_S = 24 * 3600

# probe(domain) -> organisation names of the people found at that domain (empty if none)
Probe = Callable[[str], Awaitable[list[str]]]


@dataclass
class ResolvedCompany:
    name: str
    given_domain: Optional[str]
    domain: Optional[str]            # verified domain, or the given one when nothing verified
    verified: bool
    tried: list[str] = field(default_factory=list)
    note: str = ""


def registrable_root(domain: str) -> str:
    labels = domain.split(".")
    if len(labels) <= 2:
        return domain
    keep = 3 if ".".join(labels[-2:]) in _TWO_PART_SUFFIXES else 2
    return ".".join(labels[-keep:])


def candidate_domains(company: str, given: Optional[str], max_guesses: int = 4) -> list[str]:
    given = normalize_domain(given)
    first = [given, registrable_root(given)] if given else []
    return list(dict.fromkeys(d for d in first + guess_domains(company, None, max_guesses) if d))


_cache: dict[tuple[str, str], tuple[float, ResolvedCompany]] = {}


def clear_cache() -> None:
    _cache.clear()


async def resolve_company(company: str, given_domain: Optional[str], probe: Optional[Probe],
                          now: Optional[float] = None) -> ResolvedCompany:
    now = time.time() if now is None else now
    given = normalize_domain(given_domain) or None
    key = (company.lower().strip(), given or "")
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]

    result = ResolvedCompany(name=company, given_domain=given, domain=given, verified=False)
    probe_failed = False
    if probe is not None:
        for candidate in candidate_domains(company, given):
            result.tried.append(candidate)
            try:
                orgs = await probe(candidate)
            except Exception as e:  # a probe failure must not fail a paid job
                logger.warning("Domain probe failed for %s: %s", candidate, type(e).__name__)
                probe_failed = True
                break
            # Strict match: a domain is only accepted if its people work at this very company.
            if any(same_company(org, company) for org in orgs):
                result.domain, result.verified = candidate, True
                break

    if result.verified and given and result.domain == given:
        result.note = f"Confirmed {given} with the data provider."
    elif result.verified and given:
        result.note = f"{given} is not indexed by the data provider; used {result.domain}."
    elif result.verified and not given:
        result.note = f"No domain given; found the company under {result.domain}."
    elif probe_failed and given:
        result.note = f"The data provider did not answer, so {given} could not be confirmed; searched it as given."
    elif probe_failed:
        result.note = "No domain given and the data provider did not answer; results rely on name matching."
    elif given:
        result.note = f"Could not confirm {given} with the data provider; searched it as given."
    else:
        result.note = "No domain given and none could be confirmed; results rely on name matching."

    # A failed probe says nothing about the company: do not let one provider hiccup pin it to the
    # unconfirmed (name-search, possibly paid) path for a day. Only completed resolutions are cached.
    if probe_failed and not result.verified:
        logger.info("Resolved %r (given %s) -> %s unverified after a probe error; not cached", company, given,
                    result.domain)
        return result
    _cache[key] = (now, result)
    logger.info("Resolved %r (given %s) -> %s verified=%s tried=%s", company, given, result.domain, result.verified,
                result.tried)
    return result
