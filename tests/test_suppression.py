"""Feedback suppression: 2+ distinct orders, a TTL, full names compared in full (memory and PostgreSQL stores)."""

import os
import time

import pytest

from app.feedback import MemoryFeedbackStore
from app.router import person_full_hash, person_hash
from app.suppression import SUPPRESS_MIN_REPORTS, SUPPRESS_TTL_DAYS, Report, Suppression

requires_pg = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def _lead(name, company="Acme"):
    return {"name": name, "company": company}


def _delivered(name):
    lead = _lead(name)
    return {"hash": person_hash(lead), "full_hash": person_full_hash(lead), "title": "BD"}


def _hit(sup: Suppression, name: str) -> bool:
    lead = _lead(name)
    return sup.matches(person_hash(lead), person_full_hash(lead))


def test_defaults():
    assert SUPPRESS_MIN_REPORTS == 2 and SUPPRESS_TTL_DAYS == 180


def test_one_order_is_not_enough_two_distinct_orders_are():
    h, f = person_hash(_lead("Wei Zhang")), person_full_hash(_lead("Wei Zhang"))
    assert not _hit(Suppression([Report("o1", h, f)]), "Wei Zhang")
    assert not _hit(Suppression([Report("o1", h, f), Report("o1", h, f)]), "Wei Zhang")  # same order twice
    assert _hit(Suppression([Report("o1", h, f), Report("o2", h, f)]), "Wei Zhang")
    assert _hit(Suppression([Report("o1", h, f)], min_reports=1), "Wei Zhang")


def test_full_name_reports_do_not_hit_a_colliding_full_name():
    reports = [Report(o, person_hash(_lead("Wei Zhang")), person_full_hash(_lead("Wei Zhang"))) for o in ("o1", "o2")]
    sup = Suppression(reports)
    assert _hit(sup, "Wei Zhang")
    assert not _hit(sup, "Wei Zheng")   # same coarse key, different person
    assert _hit(sup, "Wei Z***g")       # a masked candidate can only be compared coarsely


def test_masked_reports_hit_every_name_with_the_coarse_key():
    masked = _lead("Li W***g")
    sup = Suppression([Report(o, person_hash(masked), None) for o in ("o1", "o2")])
    assert _hit(sup, "Li Wang") and _hit(sup, "Li W***g")
    assert _hit(sup, "Li Wong")  # W...g fits both: all a masked report can tell
    assert not _hit(sup, "Li Wall") and not _hit(sup, "Lu Wang")
    assert sup.active_count() == 1 and Suppression().active_count() == 0


def test_a_full_and_a_masked_report_add_up():
    full, masked = _lead("Jane Smith"), _lead("Jane S***h")
    sup = Suppression([Report("o1", person_hash(full), person_full_hash(full)), Report("o2", person_hash(masked), None)])
    assert _hit(sup, "Jane Smith") and not _hit(sup, "Jane Stone")


async def test_memory_store_counts_distinct_orders_and_expires():
    store = MemoryFeedbackStore()
    for ref in ("o1", "o2"):
        await store.remember_delivery(ref, "Acme", [_delivered("Wei Zhang"), _delivered("Li Wang")])
    await store.save_feedback("o1", 2, {0: "left_company"}, "")
    await store.save_feedback("o1", 2, {0: "left_company"}, "")  # resubmitted: still one report
    assert not _hit(await store.suppression(), "Wei Zhang")
    await store.save_feedback("o2", 2, {0: "wrong_person", 1: "useful"}, "")
    sup = await store.suppression()
    assert _hit(sup, "Wei Zhang") and not _hit(sup, "Wei Zheng") and not _hit(sup, "Li Wang")
    later = time.time() + (SUPPRESS_TTL_DAYS + 1) * 86400
    assert not _hit(await store.suppression(now=later), "Wei Zhang")
    assert (await store.summary())["suppressed_people"] == 1


async def test_memory_store_reads_rows_of_the_old_format():
    store = MemoryFeedbackStore()
    h = person_hash(_lead("Wei Zhang"))
    store.suppressed[h] = {"reason": "left_company", "reports": 1, "updated_at": time.time()}
    assert not _hit(await store.suppression(), "Wei Zhang")  # one old report alone no longer suppresses
    store.suppressed[h]["reports"] = 2
    assert _hit(await store.suppression(), "Wei Zhang")
    assert _hit(await store.suppression(), "Wei Zheng")  # old rows carry the coarse hash only
    store.suppressed[h]["updated_at"] = time.time() - (SUPPRESS_TTL_DAYS + 1) * 86400
    assert not _hit(await store.suppression(), "Wei Zhang")


async def test_deliveries_without_full_hash_still_work():
    store = MemoryFeedbackStore()
    for ref in ("o1", "o2"):
        await store.remember_delivery(ref, "Acme", [{"hash": person_hash(_lead("Wei Zhang")), "title": "BD"}])
        await store.save_feedback(ref, 1, {0: "left_company"}, "")
    assert _hit(await store.suppression(), "Wei Zhang")


# --- PostgreSQL -------------------------------------------------------------------------------------------------

async def _clean(pool):
    if await pool.fetchval("SELECT to_regclass('suppression_reports') IS NOT NULL"):
        await pool.execute("DELETE FROM suppression_reports WHERE ref LIKE 'test-s%'")
    await pool.execute("DELETE FROM feedback WHERE ref LIKE 'test-s%'")
    await pool.execute("DELETE FROM feedback_deliveries WHERE ref LIKE 'test-s%'")
    await pool.execute("DELETE FROM suppressed_people WHERE hash LIKE 'test-s%' OR hash = $1",
                       person_hash(_lead("Ola Nordmann")))


@requires_pg
async def test_postgres_store_needs_two_orders_compares_full_names_and_expires():
    from app.feedback import PostgresFeedbackStore

    store = PostgresFeedbackStore(os.environ["TEST_DATABASE_URL"])
    try:
        pool = await store._p()
        await _clean(pool)
        for ref in ("test-s1", "test-s2"):
            await store.remember_delivery(ref, "Acme", [_delivered("Wei Zhang"), _delivered("Li Wang")])
        await store.save_feedback("test-s1", 2, {0: "left_company"}, "")
        await store.save_feedback("test-s1", 3, {0: "left_company"}, "")  # resubmitted: still one report
        assert await pool.fetchval("SELECT count(*) FROM suppression_reports WHERE ref = 'test-s1'") == 1
        assert not _hit(await store.suppression(), "Wei Zhang")

        await store.save_feedback("test-s2", 2, {0: "wrong_person", 1: "wrong_person"}, "")
        sup = await store.suppression()
        assert _hit(sup, "Wei Zhang") and not _hit(sup, "Wei Zheng") and not _hit(sup, "Li Wang")

        await store.save_feedback("test-s2", 2, {0: "wrong_person", 1: "useful"}, "")  # takes one mark back
        assert await pool.fetchval("SELECT count(*) FROM suppression_reports WHERE ref = 'test-s2'") == 1

        await pool.execute("UPDATE suppression_reports SET reported_at = now() - make_interval(days => $1) "
                           "WHERE ref = 'test-s1'", SUPPRESS_TTL_DAYS + 1)
        assert not _hit(await store.suppression(), "Wei Zhang")
        await _clean(pool)
    finally:
        await store.close()


@requires_pg
async def test_postgres_store_upgrades_a_database_with_old_suppression_rows():
    """A database written before per-order reports: suppressed_people rows and a feedback row whose suppressing
    mark was already counted there. The new store creates its table in place and does not count that mark twice."""
    import asyncpg

    from app.feedback import PostgresFeedbackStore

    admin = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    old = _lead("Ola Nordmann")
    try:
        await _clean(admin)
        await admin.execute("DROP TABLE IF EXISTS suppression_reports")  # as before this change
        await admin.execute(PostgresFeedbackStore.SCHEMA.split("CREATE TABLE IF NOT EXISTS suppression_reports")[0])
        await admin.execute("INSERT INTO feedback_deliveries (ref, company, leads) VALUES ('test-s-old', 'Acme', $1)",
                            '[{"hash": "%s", "title": "BD"}]' % person_hash(old))
        await admin.execute("INSERT INTO feedback (ref, rating, labels) VALUES ('test-s-old', 1, "
                            "'{\"0\": \"left_company\"}')")
        await admin.execute("INSERT INTO suppressed_people (hash, reason, reports) VALUES ($1, 'left_company', 1)",
                            person_hash(old))
    finally:
        await admin.close()

    for _ in range(2):  # the second start must find everything in place
        store = PostgresFeedbackStore(os.environ["TEST_DATABASE_URL"])
        try:
            pool = await store._p()
            assert not _hit(await store.suppression(), "Ola Nordmann")  # one old report: no longer enough
            # The same old order resubmits: its mark is already in suppressed_people, so no new report.
            await store.save_feedback("test-s-old", 1, {0: "left_company"}, "")
            assert await pool.fetchval("SELECT count(*) FROM suppression_reports WHERE ref = 'test-s-old'") == 0
            assert not _hit(await store.suppression(), "Ola Nordmann")
        finally:
            await store.close()

    store = PostgresFeedbackStore(os.environ["TEST_DATABASE_URL"])
    try:
        pool = await store._p()
        # A new order reports the same person: old report + new report = two orders.
        await store.remember_delivery("test-s-new", "Acme", [_delivered("Ola Nordmann")])
        await store.save_feedback("test-s-new", 1, {0: "wrong_person"}, "")
        assert _hit(await store.suppression(), "Ola Nordmann")
        assert (await store.summary())["suppressed_people"] >= 1
        assert await pool.fetchval("SELECT reports FROM suppressed_people WHERE hash = $1", person_hash(old)) == 1
        await _clean(pool)
    finally:
        await store.close()
