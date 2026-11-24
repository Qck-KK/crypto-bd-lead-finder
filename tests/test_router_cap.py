"""Regression: the 10-person cap applies after scoring and suppression, not to each provider's raw rows.

Before the fix, `_Pool.add` let `filter_people` keep only its default 10 rows per provider call (ordered by role
match, then provider order), so senior people the provider listed at positions 11-15 were dropped unscored, and
suppressed people took slots that other candidates could have filled.
"""

import dataclasses

import httpx
import respx

from app.compliance import filter_people
from app.resolver import ResolvedCompany
from app.router import find_leads, person_hash

APOLLO_URL = "https://treg.test/call/apollo.people.search"
ROUTED_URL = "https://treg.test/call/treg.people.search"
FIRST = ["Anna", "Ben", "Cara", "Dan", "Eva", "Finn", "Gina", "Hugo", "Ida", "Jon", "Kai", "Lea", "Max", "Nia",
         "Oli", "Pia", "Quin", "Rita", "Sam", "Tom", "Uma", "Vic", "Wes", "Xia", "Yan"]


def _row(i: int, title: str, linkedin: bool = True) -> dict:
    row = {"first_name": FIRST[i], "last_name": f"Person{i}", "title": title,
           "organization": {"name": "Acme", "website_url": "https://acme.io"}}
    if linkedin:
        row["linkedin_url"] = f"https://www.linkedin.com/in/{FIRST[i].lower()}-{i}"
    return row


def _resolved() -> ResolvedCompany:
    return ResolvedCompany(name="Acme", given_domain="acme.io", domain="acme.io", verified=True)


def _mock(rows: list[dict]) -> None:
    respx.post(APOLLO_URL).mock(return_value=httpx.Response(200, json={"people": rows}))
    respx.post(ROUTED_URL).mock(return_value=httpx.Response(200, json={"output": {"people": []}}))


SENIOR = ["CEO", "VP Engineering", "Head of Product", "Chief Technology Officer", "Director of Operations"]


@respx.mock
async def test_without_role_seniors_listed_after_position_10_are_delivered(settings):
    rows = ([_row(i, "Software Engineer") for i in range(10)]
            + [_row(10 + i, t) for i, t in enumerate(SENIOR)]
            + [_row(15 + i, "Software Engineer") for i in range(10)])
    _mock(rows)
    res = await find_leads(dataclasses.replace(settings, min_leads_before_paid=1), "Acme", None, _resolved(), "c1")
    titles = [lead["title"] for lead in res.leads]
    assert len(res.leads) == 10
    assert titles[:5] == SENIOR  # 90 points each, ahead of the 75-point engineers
    assert titles[5:] == ["Software Engineer"] * 5
    assert res.steps[0].rows == 25 and res.steps[0].kept == 25


@respx.mock
async def test_with_role_better_scored_synonym_matches_beat_weak_exact_matches(settings):
    # Exact role phrase but junior and no individual profile: 35 + 5 + 15 + 6 + 10 = 71.
    weak = [_row(i, "BD Analyst", linkedin=False) for i in range(10)]
    # Synonym ("partnerships"), senior, LinkedIn profile: 22 + 20 + 15 + 20 + 10 = 87.
    strong = [_row(10 + i, "VP Partnerships") for i in range(5)]
    _mock(weak + strong)
    res = await find_leads(dataclasses.replace(settings, min_leads_before_paid=1), "Acme", "BD", _resolved(), "c2")
    assert len(res.leads) == 10
    assert [lead["quality_score"] for lead in res.leads] == [87] * 5 + [71] * 5
    assert {lead["title"] for lead in res.leads[:5]} == {"VP Partnerships"}


@respx.mock
async def test_suppressed_people_do_not_take_slots_from_remaining_candidates(settings):
    rows = [_row(i, "Head of BD") for i in range(14)]
    _mock(rows)
    first = await find_leads(dataclasses.replace(settings, min_leads_before_paid=1), "Acme", "BD", _resolved(), "c3")
    suppressed = frozenset(person_hash(lead) for lead in first.leads[:3])
    res = await find_leads(dataclasses.replace(settings, min_leads_before_paid=1), "Acme", "BD", _resolved(), "c4",
                           suppressed=suppressed)
    assert res.suppressed == 3
    assert len(res.leads) == 10  # 14 candidates - 3 suppressed = 11 left, so the cap still fills
    assert not suppressed & {person_hash(lead) for lead in res.leads}


@respx.mock
async def test_the_hard_cap_still_holds_whatever_max_people_says(settings):
    _mock([_row(i, "Head of BD") for i in range(25)])
    res = await find_leads(dataclasses.replace(settings, min_leads_before_paid=1, max_people=50), "Acme", "BD",
                           _resolved(), "c5")
    assert len(res.leads) == 10


def test_filter_people_default_limit_unchanged_and_none_keeps_all():
    rows = [_row(i, "Head of BD") for i in range(25)]
    assert len(filter_people(rows, provider="x", company="Acme", company_domain="acme.io")) == 10
    assert len(filter_people(rows, provider="x", company="Acme", company_domain="acme.io", limit=None)) == 25
