"""Runtime settings, read from environment variables (.env in local development)."""

import math
import os
from dataclasses import dataclass
from typing import Optional

from .treg_client import APOLLO_ENRICH_USD_PER_MATCH

# Hard compliance cap: never return more people than this, whatever the env says.
MAX_PEOPLE_HARD_CAP = 10

DATA_SOURCE = "treg.to"
USAGE_NOTICE = "For B2B professional outreach only."


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    treg_token: str
    treg_base_url: str
    treg_max_cost_usd: float
    treg_timeout_s: float
    gemini_api_key: str
    gemini_model: str
    max_people: int
    # Router: total treg spend per job, minimum score to deliver, and how many leads the free
    # providers must find before paid ones are skipped.
    job_budget_usd: float = 0.15
    min_lead_score: int = 35
    min_leads_before_paid: int = 5
    # "standard" or "pro". Pro is registered as its own agent with its own price and adds
    # enrichment (full names, LinkedIn) within enrich_budget_usd per job. Enrichment costs real treg
    # balance, so it is off (0) unless a budget is set.
    tier: str = "standard"
    enrich_budget_usd: float = 0.0
    # Pro without enrichment is the standard product at the Pro price, so it is not sold (it reports
    # unavailable and refuses new jobs) unless this is set, e.g. on a test deployment.
    pro_allow_without_enrichment: bool = False
    # Feedback links: HMAC secret and the public URL buyers open.
    feedback_secret: str = ""
    public_url: str = ""
    # Circuit breaker: paid provider spend (router + enrichment) per UTC day, summed over every agent
    # that shares the database (they draw on the same treg balance).
    daily_spend_cap_usd: float = 1.0
    # Upper bound for the Gemini drafting call; on timeout the job delivers template drafts.
    gemini_timeout_s: float = 45.0
    # Hard limit for one run of a job. Its spend hold counts until the end of the UTC day containing
    # start + this limit (+ grace), so a run cannot spend after its hold stops counting.
    attempt_timeout_s: float = 1800.0
    # Count treg refusals its docs call free (402 route_max_cost, 409, 422, ...) as $0 instead of their max
    # cost. Off until treg confirms the semantics.
    trust_uncharged_refusals: bool = False
    # Operator switch: refuse new jobs (/start_job 503, /availability unavailable), e.g. before a rollback.
    pause_new_jobs: bool = False
    # /start_job limits before a payment request is created (BL-15), per agent process; -1 = no limit.
    start_job_per_client_per_hour: int = 10
    start_job_per_hour: int = 30
    max_awaiting_payment: int = 30
    # Header whose first entry identifies the client behind a proxy (e.g. X-Forwarded-For); empty = peer address.
    rate_limit_client_header: str = ""

    @property
    def is_pro(self) -> bool:
        return self.tier == "pro"

    @property
    def enrich_people_per_job(self) -> int:
        """How many leads Pro can enrich per job (0 = enrichment off). Needs a treg token and a budget
        for at least one match."""
        if not (self.is_pro and self.treg_token):
            return 0
        return min(MAX_PEOPLE_HARD_CAP, math.floor(self.enrich_budget_usd / APOLLO_ENRICH_USD_PER_MATCH + 1e-9))

    @property
    def enrichment_enabled(self) -> bool:
        return self.enrich_people_per_job > 0

    def sale_blocked_reason(self) -> Optional[str]:
        """Why this agent must not take new jobs, or None."""
        if self.pause_new_jobs:
            return "paused by the operator (PAUSE_NEW_JOBS)"
        if self.is_pro and not self.enrichment_enabled and not self.pro_allow_without_enrichment:
            return "Pro enrichment is off (ENRICH_BUDGET_USD), so Pro would deliver the standard results"
        return None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            treg_token=os.getenv("TREG_TOKEN", ""),
            treg_base_url=os.getenv("TREG_BASE_URL", "https://treg.to").rstrip("/"),
            treg_max_cost_usd=_float("TREG_MAX_COST_USD", 0.15),
            treg_timeout_s=_float("TREG_TIMEOUT_S", 60.0),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            max_people=min(_int("MAX_PEOPLE", MAX_PEOPLE_HARD_CAP), MAX_PEOPLE_HARD_CAP),
            job_budget_usd=_float("TREG_JOB_BUDGET_USD", 0.15),
            # 35 drops people with neither a title nor a LinkedIn profile (at most 31 points).
            min_lead_score=_int("MIN_LEAD_SCORE", 35),
            min_leads_before_paid=_int("MIN_LEADS_BEFORE_PAID", 5),
            tier="pro" if os.getenv("AGENT_TIER", "standard").strip().lower() == "pro" else "standard",
            enrich_budget_usd=_float("ENRICH_BUDGET_USD", 0.0),
            pro_allow_without_enrichment=_bool("PRO_ALLOW_WITHOUT_ENRICHMENT"),
            feedback_secret=os.getenv("FEEDBACK_SECRET", ""),
            public_url=os.getenv("AGENT_PUBLIC_URL", "").rstrip("/"),
            daily_spend_cap_usd=_float("DAILY_SPEND_CAP_USD", 1.0),
            gemini_timeout_s=_float("GEMINI_TIMEOUT_S", 45.0),
            attempt_timeout_s=max(60.0, _float("ATTEMPT_TIMEOUT_S", 1800.0)),
            trust_uncharged_refusals=_bool("TREG_TRUST_UNCHARGED_REFUSALS"),
            pause_new_jobs=_bool("PAUSE_NEW_JOBS"),
            start_job_per_client_per_hour=_int("START_JOB_PER_CLIENT_PER_HOUR", 10),
            start_job_per_hour=_int("START_JOB_PER_HOUR", 30),
            max_awaiting_payment=_int("MAX_AWAITING_PAYMENT", 30),
            rate_limit_client_header=os.getenv("RATE_LIMIT_CLIENT_HEADER", "").strip(),
        )
