"""Personalized outreach via the Gemini API (google-genai SDK).

Per lead: an email subject, an email body and a LinkedIn connection note (<= 280 characters).
Personalization uses only what the agent knows: the lead's title, seniority and region (from the
title), the company, and the buyer's offer, name, company and chosen tone. Only those public
fields are sent to the model, in one request for all leads. Facts are never invented. If the
model call fails, each lead gets a template so a paid job still delivers.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from google import genai
from google.genai import errors, types

from .compliance import scrub_contact_text
from .scoring import seniority

logger = logging.getLogger(__name__)

LINKEDIN_NOTE_MAX = 280  # LinkedIn allows 300 characters in a connection note; keep a margin.
TONES = ("friendly", "formal", "concise")

_REGIONS = {"emea": "EMEA", "apac": "APAC", "latam": "LATAM", "americas": "the Americas", "europe": "Europe",
            "asia": "Asia", "north america": "North America", "us": "the US", "uk": "the UK", "dach": "DACH"}


@dataclass(frozen=True)
class OutreachContext:
    pitch: Optional[str] = None
    sender_name: Optional[str] = None
    sender_company: Optional[str] = None
    tone: str = "friendly"


SYSTEM_INSTRUCTION = (
    "You write personalized B2B outreach for business development in the crypto and Web3 industry. "
    "For each person write: a subject line (max 60 characters), an email body (60 to 110 words) and a "
    "LinkedIn connection note (max 280 characters). Address the person by first name. Make one specific, "
    "relevant link between their role (title, seniority, region) and the sender's offer, and adapt the ask to "
    "their seniority: strategic outcomes for executives and heads, practical next steps for managers and "
    "individual contributors. End the email with a short, low-pressure call to action. Sign the email with the "
    "sender's name and company if given, otherwise without a signature. Follow the requested tone. "
    "Never invent facts about the person or their company beyond what is given. Never include email "
    "addresses, phone numbers or links. Return JSON only."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "subject": {"type": "string"},
                    "email": {"type": "string"},
                    "linkedin_note": {"type": "string"},
                },
                "required": ["index", "subject", "email", "linkedin_note"],
            },
        }
    },
    "required": ["drafts"],
}


def region_of(title: str) -> Optional[str]:
    t = f" {re.sub(r'[^a-z]+', ' ', title.lower())} "
    return next((label for key, label in _REGIONS.items() if f" {key} " in t), None)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[: end + 1] if end > limit // 2 else cut[: cut.rfind(" ")].rstrip(",;:") + "…").strip()


def template_outreach(lead: dict, ctx: OutreachContext) -> dict:
    first = lead["name"].split()[0]
    role = f" as {lead['title']}" if lead.get("title") else ""
    about = ctx.pitch.strip().rstrip(".") if ctx.pitch else "a partnership idea that may be relevant to your team"
    sender = ", ".join(p for p in (ctx.sender_name, ctx.sender_company) if p)
    sign = f"\n\nBest,\n{sender}" if sender else ""
    email = (f"Hi {first}, I came across your work{role} at {lead['company']}. I'm reaching out about {about}. "
             f"Would you be open to a 15-minute call in the next couple of weeks to see if there's a fit?{sign}")
    note = _clip(f"Hi {first}, I work on {about} and would value connecting with you{role} at {lead['company']}.",
                 LINKEDIN_NOTE_MAX)
    return {"subject": _clip(f"{lead['company']} x {ctx.sender_company or 'quick idea'}", 60), "email": email,
            "linkedin_note": note}


def build_prompt(leads: list[dict], ctx: OutreachContext) -> str:
    people = [
        {"index": i, "name": lead["name"], "title": lead.get("title", ""), "company": lead["company"],
         "seniority": seniority(lead.get("title", "")), "region": region_of(lead.get("title", ""))}
        for i, lead in enumerate(leads)
    ]
    sender = {
        "offer": ctx.pitch or "No specific offer given; keep the ask general (exploring a partnership).",
        "name": ctx.sender_name or None,
        "company": ctx.sender_company or None,
        "tone": ctx.tone if ctx.tone in TONES else "friendly",
    }
    return (f"Sender: {json.dumps(sender, ensure_ascii=False)}\n\n"
            f"Write one entry per person below. Use the same index.\n{json.dumps(people, ensure_ascii=False)}")


def parse_outreach(text: str, count: int) -> dict[int, dict]:
    data = json.loads(text)
    out: dict[int, dict] = {}
    for item in data.get("drafts", []):
        idx = item.get("index")
        parts = {k: item.get(k) for k in ("subject", "email", "linkedin_note")}
        if not (isinstance(idx, int) and 0 <= idx < count and all(isinstance(v, str) and v.strip()
                                                                    for v in parts.values())):
            continue
        # The model is told not to, but never let it reintroduce contact details.
        clean = {k: scrub_contact_text(v.strip()) if k != "email" else
                 "\n".join(scrub_contact_text(line) for line in v.strip().splitlines()) for k, v in parts.items()}
        clean["subject"] = _clip(clean["subject"], 60)
        clean["linkedin_note"] = _clip(clean["linkedin_note"], LINKEDIN_NOTE_MAX)
        out[idx] = clean
    return out


async def write_outreach(
    leads: list[dict],
    ctx: OutreachContext,
    *,
    api_key: str,
    model: str,
    client: Optional[Any] = None,
) -> tuple[list[dict], bool]:
    """Return (outreach dicts aligned with leads, used_llm)."""
    if not leads:
        return [], False

    drafts: dict[int, dict] = {}
    used_llm = False
    if api_key or client is not None:
        try:
            client = client or genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model=model,
                contents=build_prompt(leads, ctx),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_json_schema=RESPONSE_SCHEMA,
                    # Headroom for models that think before answering (2.5 Flash does by default).
                    max_output_tokens=16384,
                ),
            )
            drafts = parse_outreach(response.text or "", len(leads))
            used_llm = bool(drafts)
        except (errors.APIError, ValueError, KeyError, TypeError, AttributeError) as e:
            logger.warning("Gemini drafting failed, using templates: %s", type(e).__name__)
    else:
        logger.warning("GEMINI_API_KEY is not set, using template drafts")

    return [drafts.get(i) or template_outreach(lead, ctx) for i, lead in enumerate(leads)], used_llm
