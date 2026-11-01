"""Runtime settings, read from environment variables (.env in local development)."""

import os
from dataclasses import dataclass

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
    # enrichment (full names, LinkedIn) within enrich_budget_usd per job.
    tier: str = "standard"
    enrich_budget_usd: float = 0.30
    # Feedback links: HMAC secret and the public URL buyers open.
    feedback_secret: str = ""
    public_url: str = ""
    # Circuit breaker: paid provider spend (router + enrichment) per UTC day, summed over every agent
    # that shares the database (they draw on the same treg balance).
    daily_spend_cap_usd: float = 1.0

    @property
    def is_pro(self) -> bool:
        return self.tier == "pro"

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
            enrich_budget_usd=_float("ENRICH_BUDGET_USD", 0.30),
            feedback_secret=os.getenv("FEEDBACK_SECRET", ""),
            public_url=os.getenv("AGENT_PUBLIC_URL", "").rstrip("/"),
            daily_spend_cap_usd=_float("DAILY_SPEND_CAP_USD", 1.0),
        )
