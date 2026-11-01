"""Builds the job result: a Markdown report followed by the same data as JSON.

The result string is what the Masumi Payment Service hashes and submits on-chain,
and what Sokosumi shows to the buyer. It never contains our costs or provider credentials.
"""

import json
from collections import Counter
from typing import Optional

from .settings import DATA_SOURCE, USAGE_NOTICE

FOOTER = f"Data source: {DATA_SOURCE}. {USAGE_NOTICE}"


def _outreach(item) -> dict:
    # Plain strings are accepted for callers that only have an email draft.
    return item if isinstance(item, dict) else {"subject": "", "email": item, "linkedin_note": ""}


def build_payload(company: str, role: Optional[str], leads: list[dict], drafts: list,
                  resolution: Optional[dict] = None, search: Optional[list[dict]] = None,
                  dropped_low_score: int = 0, tier: str = "standard", feedback_url: Optional[str] = None,
                  suppressed: int = 0) -> dict:
    labels = Counter(lead.get("quality", "") for lead in leads)
    verified = Counter((lead.get("verification") or {}).get("status", "") for lead in leads)
    return {
        "query": {"company": company, "role": role or None},
        "tier": tier,
        "resolution": resolution or {},
        "summary": {"delivered": len(leads), "high": labels["High"], "medium": labels["Medium"],
                    "low": labels["Low"], "verified": verified["Verified"], "likely": verified["Likely"],
                    "filtered_low_quality": dropped_low_score, "suppressed_by_feedback": suppressed},
        "count": len(leads),
        "leads": [{**lead, "outreach": _outreach(d), "outreach_draft": _outreach(d)["email"]}
                  for lead, d in zip(leads, drafts)],
        "search": search or [],
        "feedback_url": feedback_url,
        "data_source": DATA_SOURCE,
        "usage_notice": USAGE_NOTICE,
    }


def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _link(url: str) -> str:
    if not url:
        return "n/a"
    label = "LinkedIn" if "linkedin.com/in/" in url else url.split("://", 1)[-1]
    return f"[{_md(label)}]({url})"


def render_markdown(payload: dict) -> str:
    q, res, summary, leads = payload["query"], payload["resolution"], payload["summary"], payload["leads"]
    target = f"{q['role']} at {q['company']}" if q.get("role") else q["company"]
    title = "BD leads (Pro)" if payload.get("tier") == "pro" else "BD leads"
    lines = [f"# {title}: {_md(target)}", ""]

    domain = res.get("domain")
    if domain:
        state = "confirmed" if res.get("verified") else "as given, not confirmed"
        lines.append(f"- **Company domain:** {_md(domain)} ({state})")
    lines.append(f"- **Leads delivered:** {summary['delivered']} "
                 f"(High {summary['high']} · Medium {summary['medium']} · Low {summary['low']})")
    if leads and "verification" in leads[0]:
        lines.append(f"- **Verification:** {summary.get('verified', 0)} verified · {summary.get('likely', 0)} likely "
                     f"· {summary['delivered'] - summary.get('verified', 0) - summary.get('likely', 0)} unverified")
    if payload.get("feedback_url"):
        lines.append(f"- **Rate these leads:** [feedback form]({payload['feedback_url']})")
    lines.append("")

    if not leads:
        lines += [
            "## No leads found",
            "",
            "No public professional profiles matched this company and role. Things that usually help:",
            "- Enter the company's website domain (for example `chain.link`).",
            "- Use a broader role (for example `BD` instead of a full job title), or leave the role empty.",
            "- Check the spelling of the company name.",
            "",
        ]
    else:
        lines += ["## Overview", "", "| # | Name | Title | Quality | Verified | Profile |",
                  "|---|---|---|---|---|---|"]
        for i, lead in enumerate(leads, 1):
            status = (lead.get("verification") or {}).get("status", "n/a")
            lines.append(f"| {i} | {_md(lead['name'])} | {_md(lead['title']) or 'n/a'} | "
                         f"{lead.get('quality', '')} {lead.get('quality_score', '')} | {status} | "
                         f"{_link(lead['profile_url'])} |")
        lines += ["", "## How to use these leads", ""]
        if any("*" in lead["name"] for lead in leads):
            lines.append("- Names with `***` are partly masked by the data provider. Search LinkedIn for the first "
                         "name, title and company to find the person.")
        lines += [
            "- Check the person's current role before you reach out; public data can be out of date.",
            "- The drafts are starting points: add one specific, verifiable reason for contacting this person.",
            "- Use these contacts for professional B2B outreach only, and honour opt-out requests.",
            "",
            "## Leads",
            "",
        ]
        for i, lead in enumerate(leads, 1):
            lines.append(f"### {i}. {_md(lead['name'])}")
            lines.append(f"- **Title:** {_md(lead['title']) or 'n/a'}")
            lines.append(f"- **Company:** {_md(lead['company'])}")
            lines.append(f"- **Profile:** {_link(lead['profile_url'])}")
            if "quality_score" in lead:
                why = "; ".join(lead.get("match_reasons") or [])
                lines.append(f"- **Quality:** {lead['quality_score']}/100 ({lead['quality']})" + (f": {why}" if why else ""))
            if lead.get("verification"):
                v = lead["verification"]
                lines.append(f"- **Verification:** {v['status']}" + (f" ({'; '.join(v['notes'])})" if v["notes"] else ""))
            lines.append(f"- **Source:** {_md(lead['source'])}")
            lines.append("")
            o = lead["outreach"]
            if o.get("subject"):
                lines.append(f"**Email subject:** {_md(o['subject'])}")
                lines.append("")
            lines.append("> " + o["email"].replace("\n", "\n> "))
            lines.append("")
            if o.get("linkedin_note"):
                lines.append(f"**LinkedIn note:** {_md(o['linkedin_note'])}")
                lines.append("")

    notes = [res["note"]] if res.get("note") else []
    for step in payload.get("search", []):
        status = step.get("status", "ok")
        if status == "ok":
            notes.append(f"{step['provider']} on {step['query']}: {step['rows']} found, {step['kept']} kept")
        else:
            notes.append(f"{step['provider']} on {step['query']}: {status}")
    if summary.get("filtered_low_quality"):
        notes.append(f"{summary['filtered_low_quality']} more matches were left out for low quality.")
    if summary.get("suppressed_by_feedback"):
        notes.append(f"{summary['suppressed_by_feedback']} people were left out because earlier buyers reported "
                     "them as gone or wrong.")
    if notes:
        lines += ["## Search notes", ""] + [f"- {_md(n)}" for n in notes] + [""]
    if payload.get("feedback_url"):
        lines += ["## Feedback", "",
                  f"Tell us which leads were useful: [rate this delivery]({payload['feedback_url']}). "
                  "People you mark as gone or wrong are never delivered again.", ""]
    return "\n".join(lines)


def build_result(company: str, role: Optional[str], leads: list[dict], drafts: list,
                 resolution: Optional[dict] = None, search: Optional[list[dict]] = None,
                 dropped_low_score: int = 0, tier: str = "standard", feedback_url: Optional[str] = None,
                 suppressed: int = 0) -> str:
    payload = build_payload(company, role, leads, drafts, resolution, search, dropped_low_score, tier,
                            feedback_url, suppressed)
    return (
        render_markdown(payload)
        + "\n## Data (JSON)\n\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n```\n\n---\n"
        + FOOTER
        + "\n"
    )
