"""Deployment review: upgrading a database written by the deployed version (a97ca3e) in place."""

import os
import time
from urllib.parse import urlencode, urlparse, urlunparse

import pytest

from app.job_identity import legacy_tag

requires_pg = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
SCHEMA = "bl_upgrade"

# app/ledger.py SCHEMA at a97ca3e, the version running on the server.
A97CA3E_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_results (tag TEXT PRIMARY KEY, tier TEXT NOT NULL, result TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS job_costs (tag TEXT PRIMARY KEY, tier TEXT NOT NULL, company TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ NOT NULL, duration_ms INT NOT NULL,
    treg_usd DOUBLE PRECISION NOT NULL, enrich_usd DOUBLE PRECISION NOT NULL, leads INT NOT NULL,
    llm_used BOOLEAN NOT NULL, outcome TEXT NOT NULL, price_tusdm DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS job_costs_finished_idx ON job_costs (finished_at);
"""
# a97ca3e's spend_today
OLD_SPEND_TODAY = ("SELECT coalesce(sum(treg_usd + enrich_usd), 0) FROM job_costs WHERE finished_at >= "
                   "date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'")


def _dsn_in_schema() -> str:
    url = urlparse(os.environ["TEST_DATABASE_URL"])
    return urlunparse(url._replace(query=urlencode({"search_path": SCHEMA})))


@requires_pg
async def test_the_new_ledger_upgrades_a_database_of_the_deployed_version_in_place():
    import asyncpg

    from app.ledger import PostgresLedger

    admin = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    try:
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA}")
        await admin.execute(f"SET search_path = {SCHEMA}")
        await admin.execute(A97CA3E_SCHEMA)
        order_created = time.time() - 600
        old_tag = legacy_tag("buyer", {"company": "Acme"})
        await admin.execute("INSERT INTO job_costs VALUES ('old-job', 'standard', 'Acme', now(), now(), 1000, 0.3, 0, "
                            "3, true, 'delivered', 0.27)")
        await admin.execute("INSERT INTO job_results (tag, tier, result, created_at) VALUES ($1, 'standard', 'OLD', "
                            "to_timestamp($2))", old_tag, order_created + 60)

        for _ in range(2):  # the second start must find everything in place
            led = PostgresLedger(_dsn_in_schema())
            try:
                await led.ping()
                assert await led.spend_today() == pytest.approx(0.3)  # old settled spend still counts
                since = await led.identity_v2_since()
                # A pre-upgrade order in flight finds its old-tag result; a new order would not.
                assert await led.get_legacy_result(old_tag, "standard", order_created) == "OLD"
                assert await led.get_legacy_result(old_tag, "standard", since + 1) is None
            finally:
                await led.close()

        cols = {r["column_name"] for r in await admin.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = 'job_costs'",
            SCHEMA)}
        assert {"cost_assumed_calls", "unconfirmed_usd"} <= cols
        assert await admin.fetchval("SELECT count(*) FROM ledger_meta") == 1

        # The old version's INSERT (explicit columns) still works on the upgraded table, and its spend query
        # sees the new version's rows: rollback stays possible.
        await admin.execute("INSERT INTO job_costs (tag, tier, company, started_at, finished_at, duration_ms, treg_usd, "
                            "enrich_usd, leads, llm_used, outcome, price_tusdm) VALUES ('old-again', 'pro', 'B', "
                            "now(), now(), 1, 0.1, 0, 1, true, 'delivered', 1.05)")
        led = PostgresLedger(_dsn_in_schema())
        try:
            await led.reserve("upgrade-attempt", "T", "standard", 0.2, 5.0, 2400)
            assert await led.spend_today() == pytest.approx(0.6)
            await led.hand_over_open_holds()
            assert float(await admin.fetchval(OLD_SPEND_TODAY)) == pytest.approx(0.6)
        finally:
            await led.close()
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.close()
