"""Client for treg people search (https://treg.to): the routed `treg.people.search` and Apollo's
free `apollo.people.search`.

treg relays provider-native rows, so field names differ by provider. This module only fetches;
`compliance.py` decides what may leave the agent.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

ENDPOINT_ID = "treg.people.search"
APOLLO_ENDPOINT_ID = "apollo.people.search"
APOLLO_ENRICH_ENDPOINT_ID = "apollo.people.bulk_enrich"
APOLLO_ENRICH_USD_PER_MATCH = 0.026
# Apollo's search is documented free on treg; the cap only guards against a pricing change.
APOLLO_MAX_COST_USD = 0.01

_META_SAFE = re.compile(r"[^A-Za-z0-9._:-]")
_COST_MICRO = re.compile(r"[0-9]{1,12}")

logger = logging.getLogger(__name__)


class TregError(Exception):
    """treg refused the call or the upstream provider failed.

    cost_usd: what the failed call may have cost (its max cost unless treg reported a valid cost).
    """

    def __init__(self, message: str = "", *, cost_usd: float = 0.0, cost_assumed: bool = False):
        super().__init__(message)
        self.cost_usd, self.cost_assumed = cost_usd, cost_assumed


@dataclass
class TregSearchResult:
    people: list[dict[str, Any]]
    served_by: str = ""
    cost_usd: float = 0.0
    cost_assumed: bool = False  # no valid X-Treg-Cost-Micro: cost_usd is the call's max cost
    call_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def build_query(company: str, role: Optional[str]) -> str:
    return f"{role} at {company}" if role else f"people at {company}"


def _meta_value(value: str) -> str:
    # X-Treg-Meta values: letters, digits and . _ - : only, max 128 chars.
    return _META_SAFE.sub("-", value)[:128] or "unknown"


async def search_people(
    *,
    token: str,
    company: str,
    role: Optional[str],
    company_domain: Optional[str],
    limit: int,
    max_cost_usd: float,
    job_tag: str,
    base_url: str = "https://treg.to",
    timeout_s: float = 60.0,
    client: Optional[httpx.AsyncClient] = None,
) -> TregSearchResult:
    if not token:
        raise TregError("TREG_TOKEN is not set")

    # With a domain, send the domain alone. treg passes each provider only the fields it accepts, so
    # with `title` or `q` present a title-only or keyword provider can answer and ignore the company
    # (it returned BD people at "Link Metal" for chain.link). The role is filtered locally instead.
    body: dict[str, Any] = {"limit": limit}
    if company_domain:
        body["company_domain"] = company_domain
    else:
        body["q"] = build_query(company, role)
    return await _call(ENDPOINT_ID, body, token=token, max_cost_usd=max_cost_usd, job_tag=job_tag,
                       base_url=base_url, timeout_s=timeout_s, client=client)


async def search_apollo(
    *,
    token: str,
    domain: str,
    titles: Optional[list[str]],
    per_page: int,
    job_tag: str,
    base_url: str = "https://treg.to",
    timeout_s: float = 60.0,
    client: Optional[httpx.AsyncClient] = None,
) -> TregSearchResult:
    """Apollo people search by company domain and titles. Free on treg; returns no contact data
    (last names come obfuscated, e.g. "S***h", and there are no profile links)."""
    if not token:
        raise TregError("TREG_TOKEN is not set")
    body: dict[str, Any] = {"q_organization_domains_list": [domain], "per_page": per_page}
    if titles:
        body["person_titles"] = titles
    result = await _call(APOLLO_ENDPOINT_ID, body, token=token, max_cost_usd=APOLLO_MAX_COST_USD, job_tag=job_tag,
                         base_url=base_url, timeout_s=timeout_s, client=client)
    result.served_by = result.served_by or "apollo"
    return result


async def enrich_apollo(
    *,
    token: str,
    apollo_ids: list[str],
    max_cost_usd: float,
    job_tag: str,
    base_url: str = "https://treg.to",
    timeout_s: float = 60.0,
    client: Optional[httpx.AsyncClient] = None,
) -> TregSearchResult:
    """Apollo bulk enrichment by Apollo person id (max 10). ~$0.026 per match on treg.

    Personal emails and phone numbers are explicitly not requested; whatever else comes back
    (work email, address, socials) is removed by the compliance allowlist.
    """
    if not token:
        raise TregError("TREG_TOKEN is not set")
    body = {"details": [{"id": i} for i in apollo_ids[:10]]}
    return await _call(f"{APOLLO_ENRICH_ENDPOINT_ID}?reveal_personal_emails=false&reveal_phone_number=false", body,
                       token=token, max_cost_usd=max_cost_usd, job_tag=job_tag, base_url=base_url,
                       timeout_s=timeout_s, client=client)


async def _call(
    endpoint_id: str,
    body: dict[str, Any],
    *,
    token: str,
    max_cost_usd: float,
    job_tag: str,
    base_url: str,
    timeout_s: float,
    client: Optional[httpx.AsyncClient],
) -> TregSearchResult:
    tag = _meta_value(job_tag)
    headers = {
        "X-Treg-Token": token,
        "X-Treg-Route-Max-Cost": f"{max_cost_usd:.4f}",
        "X-Treg-Meta": f"job={tag}",
        # A retry of the same job returns the stored answer instead of paying twice.
        "Idempotency-Key": f"bdlf-{tag}",
    }

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout_s)
    try:
        resp = await client.post(f"{base_url}/call/{endpoint_id}", json=body, headers=headers)
    except httpx.HTTPError as e:
        # The request may have reached treg (timeout, reset), so it may have been charged.
        _warn_assumed(endpoint_id, max_cost_usd, "no response")
        raise TregError(f"treg request failed: {type(e).__name__}", cost_usd=max_cost_usd, cost_assumed=True) from e
    finally:
        if owns_client:
            await client.aclose()

    cost_usd, assumed = call_cost(resp, max_cost_usd)
    if assumed:
        _warn_assumed(endpoint_id, max_cost_usd, f"HTTP {resp.status_code} without a valid cost header")
    if resp.headers.get("X-Treg-Error") == "1" or resp.status_code >= 400:
        raise TregError(f"treg returned HTTP {resp.status_code}: {_error_summary(resp)}", cost_usd=cost_usd,
                        cost_assumed=assumed)

    try:
        payload = resp.json()
    except ValueError as e:
        raise TregError("treg returned a non-JSON body", cost_usd=cost_usd, cost_assumed=assumed) from e

    # Routed endpoints wrap rows as {output: {people}}; direct provider endpoints may relay the body as-is.
    output = payload.get("output", payload) if isinstance(payload, dict) else None
    people = (output.get("people") or output.get("matches")) if isinstance(output, dict) else None
    if not isinstance(people, list):
        people = []

    total = output.get("total_entries") if isinstance(output, dict) else None
    return TregSearchResult(
        people=[p for p in people if isinstance(p, dict)],
        served_by=resp.headers.get("X-Treg-Served-By", ""),
        cost_usd=cost_usd,
        cost_assumed=assumed,
        call_id=resp.headers.get("X-Treg-Call-Id", ""),
        extra={"total_entries": total} if total is not None else {},
    )


def call_cost(resp: httpx.Response, max_cost_usd: float) -> tuple[float, bool]:
    """(cost in USD, assumed). Without a valid X-Treg-Cost-Micro (missing, negative, fractional, garbage) the
    call counts at the max cost we allowed for it, so spend is never undercounted. "0" is a real zero."""
    raw = (resp.headers.get("X-Treg-Cost-Micro") or "").strip()
    if _COST_MICRO.fullmatch(raw):
        return int(raw) / 1_000_000, False
    return max_cost_usd, True


def _warn_assumed(endpoint_id: str, max_cost_usd: float, why: str) -> None:
    logger.warning("treg %s: %s; counting its max cost %.4f USD", endpoint_id.split("?")[0], why, max_cost_usd)


def _error_summary(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or "")[:200]
    return ""
