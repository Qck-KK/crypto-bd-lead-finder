"""Job results cache, cost ledger and per-attempt spend reservations.

- Results: the delivered result is stored under the job tag (see `job_identity`) before the SDK submits it
  on-chain. A re-run of the same job returns the stored result unchanged, so the on-chain result hash stays
  the same and no provider is paid twice.
- Costs (`job_costs`): one row per job with duration, treg and enrichment spend, leads, LLM use and outcome;
  `unconfirmed_usd` is the part counted at a call's max cost because treg reported no cost. Feeds the daily
  cap (shared by every agent on the database, i.e. one treg balance) and /admin/stats.
- Attempts (`spend_attempts`, BL-34/BL-35): every run of a job reserves its worst-case spend in its own row
  before its first treg call. A retry gets a new row; nothing ever releases or reduces another attempt's row.

Today's spend = settled spend in `job_costs` (finished today, or later for a rollback hand-over)
              + every open attempt's hold, max(reserved, confirmed + assumed + pending), while now < counts_until.

counts_until = end of the UTC day that contains created_at + the attempt's maximum lifetime (attempt timeout
plus grace). An attempt cannot spend after created_at + lifetime (the agent enforces the timeout), so its hold
covers every day it can spend on, including the next day when it runs across midnight. Settling turns the hold
into the job's actual `job_costs` row in one transaction (`settle`). An attempt that never settles (crash, ledger
down) keeps counting until counts_until; `expire_attempts` then marks it expired and writes a `crashed` row with
what its row knows: confirmed cost, plus assumed and pending calls at their max cost (unconfirmed). That row's
finished_at is the last instant of the hold, so the spend is not counted again on a later day.
"""

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# One advisory lock serializes reservations across every agent on the database ("bdlf").
SPEND_LOCK_KEY = 0x62646C66
SCHEMA_LOCK_KEY = 0x62646C67


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def end_of_utc_day(ts: float) -> float:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() + 86400


def _hold(a: dict) -> float:
    return max(a["reserved_usd"], a["confirmed_usd"] + a["assumed_usd"] + a["pending_usd"])


def _crash_row(a: dict) -> dict:
    """What an expired attempt is known to have spent: confirmed cost, plus every call that may have been charged
    without a reported cost (assumed, pending) at its max cost, kept apart as unconfirmed."""
    unconfirmed = a["assumed_usd"] + a["pending_usd"]
    return {"tag": f"attempt:{a['attempt_id']}", "tier": a["tier"], "company": a["company"],
            "started_at": a["created_at"], "finished_at": a["counts_until"] - 1e-6, "duration_ms": 0,
            "treg_usd": a["confirmed_usd"] + unconfirmed, "enrich_usd": 0.0, "unconfirmed_usd": unconfirmed,
            "leads": 0, "llm_used": False, "outcome": "crashed", "price_tusdm": 0.0,
            "cost_assumed_calls": a["cost_assumed_calls"] + (1 if a["pending_usd"] > 0 else 0)}


class MemoryLedger:
    def __init__(self):
        self.results: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.attempts: dict[str, dict] = {}
        self.v2_since = time.time()

    async def identity_v2_since(self) -> float:
        return self.v2_since

    async def get_result(self, tag: str) -> Optional[str]:
        hit = self.results.get(tag)
        return hit["result"] if hit else None

    async def get_legacy_result(self, legacy_tag: str, tier: str, job_created_at: float) -> Optional[str]:
        """A result stored under the pre-v2 tag, only for an order created before v2 and only from the same tier,
        stored before v2 and not before the order existed."""
        hit = self.results.get(legacy_tag)
        since = self.v2_since
        if hit and job_created_at < since and hit["tier"] == tier and job_created_at <= hit["at"] < since:
            return hit["result"]
        return None

    async def put_result(self, tag: str, tier: str, result: str) -> None:
        self.results.setdefault(tag, {"tier": tier, "result": result, "at": time.time()})

    async def record_job(self, row: dict) -> None:
        old = self.jobs.get(row["tag"])
        if old:  # a later attempt of the same job adds to it (as the PostgreSQL upsert does)
            row = {**row, **{k: old[k] + row.get(k, 0) for k in ("treg_usd", "enrich_usd", "unconfirmed_usd",
                                                                  "cost_assumed_calls")},
                   "finished_at": max(old["finished_at"], row["finished_at"])}
        self.jobs[row["tag"]] = {"unconfirmed_usd": 0.0, "cost_assumed_calls": 0, **row,
                                 "day": _day(row["finished_at"])}

    async def spend_today(self, now: Optional[float] = None) -> float:
        now = now or time.time()
        today = _day(now)
        return sum(j["treg_usd"] + j["enrich_usd"] for j in self.jobs.values() if j["day"] == today) + \
            await self.reserved_today(now)

    async def reserved_today(self, now: Optional[float] = None) -> float:
        now = now or time.time()
        return sum(_hold(a) for a in self.attempts.values() if a["status"] == "open" and now < a["counts_until"])

    async def reserve(self, attempt_id: str, tag: str, tier: str, want_usd: float, cap_usd: float,
                      lifetime_s: float, company: str = "", now: Optional[float] = None) -> float:
        """Opens this attempt's own hold and returns the grant: all of want_usd if today's cap still has it,
        else 0 (BL-33: no job runs on part of its budget). Never touches another attempt's row. No await suspends
        between reading and writing, so this is atomic within one event loop."""
        now = now or time.time()
        grant = want_usd if cap_usd - await self.spend_today(now) + 1e-9 >= want_usd else 0.0
        self.attempts[attempt_id] = {
            "attempt_id": attempt_id, "job_tag": tag, "tier": tier, "company": company, "created_at": now,
            "counts_until": end_of_utc_day(now + lifetime_s), "reserved_usd": grant, "confirmed_usd": 0.0,
            "assumed_usd": 0.0, "pending_usd": 0.0, "cost_assumed_calls": 0, "status": "open"}
        return grant

    async def progress(self, attempt_id: str, snapshot: dict) -> None:
        a = self.attempts.get(attempt_id)
        if not a or a["status"] != "open":
            raise KeyError(f"no open attempt {attempt_id}")
        a.update({k: snapshot[k] for k in ("confirmed_usd", "assumed_usd", "pending_usd", "cost_assumed_calls")})

    async def settle(self, attempt_id: str, row: dict, snapshot: dict) -> str:
        """Closes this attempt and writes the job's cost row. Returns "settled", "unreserved" (the attempt had no
        row, e.g. the ledger was down at reserve time) or the status that prevented it ("expired", "settled")."""
        a = self.attempts.get(attempt_id)
        if a and a["status"] != "open":
            logger.error("Attempt %s is already %s; its spend is accounted there", attempt_id, a["status"])
            return a["status"]
        await self.record_job(row)
        if not a:
            return "unreserved"
        a.update({k: snapshot[k] for k in ("confirmed_usd", "assumed_usd", "cost_assumed_calls")},
                 pending_usd=0.0, status="settled")
        return "settled"

    async def expire_attempts(self, now: Optional[float] = None) -> int:
        now = now or time.time()
        due = [a for a in self.attempts.values() if a["status"] == "open" and a["counts_until"] <= now]
        for a in due:
            a["status"] = "expired"
            if f"attempt:{a['attempt_id']}" not in self.jobs:
                await self.record_job(_crash_row(a))
        return len(due)

    async def stats(self, days: int = 7, now: Optional[float] = None, tier: Optional[str] = None) -> dict:
        now = now or time.time()
        cutoff = _day(now - days * 86400)
        return _aggregate([j for j in self.jobs.values() if j["day"] > cutoff and (tier is None or j["tier"] == tier)])


def _aggregate(rows: list[dict]) -> dict:
    by = defaultdict(lambda: {"jobs": 0, "delivered": 0, "empty": 0, "failed": 0, "crashed_attempts": 0, "leads": 0,
                              "treg_usd": 0.0, "enrich_usd": 0.0, "unconfirmed_usd": 0.0, "llm_fallbacks": 0,
                              "duration_ms": 0, "revenue_tusdm": 0.0, "cost_assumed_calls": 0})
    for r in rows:
        b = by[(r["day"], r["tier"])]
        b["treg_usd"] += r["treg_usd"]
        b["enrich_usd"] += r["enrich_usd"]
        b["unconfirmed_usd"] += r.get("unconfirmed_usd") or 0.0
        b["cost_assumed_calls"] += r.get("cost_assumed_calls") or 0
        if r["outcome"] == "crashed":  # an attempt's cost, not a job
            b["crashed_attempts"] += 1
            continue
        b["jobs"] += 1
        b[{"delivered": "delivered", "empty": "empty"}.get(r["outcome"], "failed")] += 1
        b["leads"] += r["leads"]
        b["llm_fallbacks"] += 0 if r["llm_used"] or not r["leads"] else 1
        b["duration_ms"] += r["duration_ms"]
        b["revenue_tusdm"] += r["price_tusdm"] if r["outcome"] != "failed" else 0.0
    out = []
    for (day, tier), b in sorted(by.items()):
        n = max(b["jobs"], 1)
        out.append({"day": day, "tier": tier, **{k: round(v, 4) if isinstance(v, float) else v for k, v in b.items()},
                    "avg_leads": round(b["leads"] / n, 2), "avg_duration_ms": round(b["duration_ms"] / n),
                    "cost_usd": round(b["treg_usd"] + b["enrich_usd"], 4)})
    return {"days": out, "totals": {
        "jobs": sum(d["jobs"] for d in out), "cost_usd": round(sum(d["cost_usd"] for d in out), 4),
        "revenue_tusdm": round(sum(d["revenue_tusdm"] for d in out), 4),
        "cost_assumed_calls": sum(d["cost_assumed_calls"] for d in out),
        "unconfirmed_usd": round(sum(d["unconfirmed_usd"] for d in out), 4),
        "crashed_attempts": sum(d["crashed_attempts"] for d in out)}}


_TODAY = "date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'"
_OPEN_HOLD = "GREATEST(reserved_usd, confirmed_usd + assumed_usd + pending_usd)"
_OPEN_NOW = "status = 'open' AND counts_until > now()"
_SPEND_TODAY_SQL = (f"SELECT (SELECT coalesce(sum(treg_usd + enrich_usd), 0) FROM job_costs WHERE finished_at >= "
                    f"{_TODAY}) + (SELECT coalesce(sum({_OPEN_HOLD}), 0) FROM spend_attempts WHERE {_OPEN_NOW})")
_ATTEMPT_COLS = ("attempt_id, job_tag, tier, company, extract(epoch FROM created_at)::float8 AS created_at, "
                 "extract(epoch FROM counts_until)::float8 AS counts_until, reserved_usd, confirmed_usd, "
                 "assumed_usd, pending_usd, cost_assumed_calls")


class PostgresLedger(MemoryLedger):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS job_results (tag TEXT PRIMARY KEY, tier TEXT NOT NULL, result TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now());
    CREATE TABLE IF NOT EXISTS job_costs (tag TEXT PRIMARY KEY, tier TEXT NOT NULL, company TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ NOT NULL, duration_ms INT NOT NULL,
        treg_usd DOUBLE PRECISION NOT NULL, enrich_usd DOUBLE PRECISION NOT NULL, leads INT NOT NULL,
        llm_used BOOLEAN NOT NULL, outcome TEXT NOT NULL, price_tusdm DOUBLE PRECISION NOT NULL);
    CREATE INDEX IF NOT EXISTS job_costs_finished_idx ON job_costs (finished_at);
    ALTER TABLE job_costs ADD COLUMN IF NOT EXISTS cost_assumed_calls INT NOT NULL DEFAULT 0;
    ALTER TABLE job_costs ADD COLUMN IF NOT EXISTS unconfirmed_usd DOUBLE PRECISION NOT NULL DEFAULT 0;
    CREATE TABLE IF NOT EXISTS spend_attempts (attempt_id TEXT PRIMARY KEY, job_tag TEXT NOT NULL,
        tier TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        counts_until TIMESTAMPTZ NOT NULL, reserved_usd DOUBLE PRECISION NOT NULL,
        confirmed_usd DOUBLE PRECISION NOT NULL DEFAULT 0, assumed_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        pending_usd DOUBLE PRECISION NOT NULL DEFAULT 0, cost_assumed_calls INT NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open', updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        settled_at TIMESTAMPTZ, expired_at TIMESTAMPTZ, expired_reason TEXT);
    CREATE INDEX IF NOT EXISTS spend_attempts_open_idx ON spend_attempts (status, counts_until);
    CREATE INDEX IF NOT EXISTS spend_attempts_tag_idx ON spend_attempts (job_tag);
    CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO ledger_meta (key, value) VALUES ('identity_v2_since', extract(epoch FROM now())::text)
        ON CONFLICT (key) DO NOTHING;
    """

    def __init__(self, dsn: str):
        super().__init__()
        self._dsn, self._pool = dsn, None

    async def _p(self):
        if self._pool is None:
            import asyncpg

            pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3)
            async with pool.acquire() as conn:
                # Both agents start together: serialize the migration (concurrent CREATE ... IF NOT EXISTS can
                # still collide in PostgreSQL).
                await conn.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_KEY)
                try:
                    await conn.execute(self.SCHEMA)
                finally:
                    await conn.execute("SELECT pg_advisory_unlock($1)", SCHEMA_LOCK_KEY)
            self._pool = pool
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self) -> bool:
        pool = await self._p()
        return await pool.fetchval("SELECT 1") == 1

    async def identity_v2_since(self) -> float:
        pool = await self._p()
        return float(await pool.fetchval("SELECT value FROM ledger_meta WHERE key = 'identity_v2_since'"))

    async def get_result(self, tag: str) -> Optional[str]:
        pool = await self._p()
        return await pool.fetchval("SELECT result FROM job_results WHERE tag = $1", tag)

    async def get_legacy_result(self, legacy_tag: str, tier: str, job_created_at: float) -> Optional[str]:
        pool = await self._p()
        return await pool.fetchval(
            "SELECT r.result FROM job_results r JOIN ledger_meta m ON m.key = 'identity_v2_since' "
            "WHERE r.tag = $1 AND r.tier = $2 AND to_timestamp($3) < to_timestamp(m.value::float8) "
            "AND r.created_at >= to_timestamp($3) AND r.created_at < to_timestamp(m.value::float8)",
            legacy_tag, tier, job_created_at)

    async def put_result(self, tag: str, tier: str, result: str) -> None:
        pool = await self._p()
        await pool.execute("INSERT INTO job_results (tag, tier, result) VALUES ($1, $2, $3) ON CONFLICT (tag) "
                           "DO NOTHING", tag, tier, result)

    async def record_job(self, row: dict, conn=None) -> None:
        conn = conn or await self._p()
        await conn.execute(
            "INSERT INTO job_costs (tag, tier, company, started_at, finished_at, duration_ms, treg_usd, enrich_usd, "
            "leads, llm_used, outcome, price_tusdm, cost_assumed_calls, unconfirmed_usd) VALUES ($1,$2,$3,"
            "to_timestamp($4),to_timestamp($5),$6,$7,$8,$9,$10,$11,$12,$13,$14) ON CONFLICT (tag) DO UPDATE SET "
            "finished_at = GREATEST(job_costs.finished_at, EXCLUDED.finished_at), outcome = EXCLUDED.outcome, "
            "treg_usd = job_costs.treg_usd + EXCLUDED.treg_usd, enrich_usd = job_costs.enrich_usd + "
            "EXCLUDED.enrich_usd, cost_assumed_calls = job_costs.cost_assumed_calls + EXCLUDED.cost_assumed_calls, "
            "unconfirmed_usd = job_costs.unconfirmed_usd + EXCLUDED.unconfirmed_usd",
            row["tag"], row["tier"], row["company"], row["started_at"], row["finished_at"], row["duration_ms"],
            row["treg_usd"], row["enrich_usd"], row["leads"], row["llm_used"], row["outcome"], row["price_tusdm"],
            row.get("cost_assumed_calls") or 0, row.get("unconfirmed_usd") or 0.0)

    async def spend_today(self, now: Optional[float] = None) -> float:
        pool = await self._p()
        return float(await pool.fetchval(_SPEND_TODAY_SQL) or 0)

    async def reserved_today(self, now: Optional[float] = None) -> float:
        pool = await self._p()
        return float(await pool.fetchval(
            f"SELECT coalesce(sum({_OPEN_HOLD}), 0) FROM spend_attempts WHERE {_OPEN_NOW}") or 0)

    async def reserve(self, attempt_id: str, tag: str, tier: str, want_usd: float, cap_usd: float,
                      lifetime_s: float, company: str = "", now: Optional[float] = None) -> float:
        pool = await self._p()
        async with pool.acquire() as conn, conn.transaction():
            # Held until commit: no other agent reads today's total between our read and our insert.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", SPEND_LOCK_KEY)
            spent = float(await conn.fetchval(_SPEND_TODAY_SQL) or 0)
            grant = want_usd if cap_usd - spent + 1e-9 >= want_usd else 0.0  # all or nothing (BL-33)
            # Always a new row (attempt_id is unique per run): a retry never reuses or shrinks an earlier hold.
            await conn.execute(
                "INSERT INTO spend_attempts (attempt_id, job_tag, tier, company, reserved_usd, counts_until) VALUES "
                "($1, $2, $3, $4, $5, date_trunc('day', (now() + make_interval(secs => $6)) AT TIME ZONE 'utc') "
                "AT TIME ZONE 'utc' + interval '1 day')", attempt_id, tag, tier, company[:100], grant,
                float(lifetime_s))
            return grant

    async def progress(self, attempt_id: str, snapshot: dict) -> None:
        pool = await self._p()
        done = await pool.execute(
            "UPDATE spend_attempts SET confirmed_usd = $2, assumed_usd = $3, pending_usd = $4, cost_assumed_calls = $5, "
            "updated_at = now() WHERE attempt_id = $1 AND status = 'open'", attempt_id, snapshot["confirmed_usd"],
            snapshot["assumed_usd"], snapshot["pending_usd"], snapshot["cost_assumed_calls"])
        if done != "UPDATE 1":
            raise KeyError(f"no open attempt {attempt_id}")

    async def settle(self, attempt_id: str, row: dict, snapshot: dict) -> str:
        pool = await self._p()
        async with pool.acquire() as conn, conn.transaction():
            closed = await conn.fetchval(
                "UPDATE spend_attempts SET status = 'settled', settled_at = now(), updated_at = now(), "
                "confirmed_usd = $2, assumed_usd = $3, pending_usd = 0, cost_assumed_calls = $4 "
                "WHERE attempt_id = $1 AND status = 'open' RETURNING attempt_id", attempt_id,
                snapshot["confirmed_usd"], snapshot["assumed_usd"], snapshot["cost_assumed_calls"])
            if closed is None:
                status = await conn.fetchval("SELECT status FROM spend_attempts WHERE attempt_id = $1", attempt_id)
                if status is not None:
                    logger.error("Attempt %s is already %s; its spend is accounted there", attempt_id, status)
                    return status
            await self.record_job(row, conn)
            return "settled" if closed else "unreserved"

    async def expire_attempts(self, now: Optional[float] = None) -> int:
        """Safe to run from both agents at once: each row is expired (and its crashed row written) exactly once,
        in the transaction that wins the row lock."""
        pool = await self._p()
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "UPDATE spend_attempts SET status = 'expired', expired_at = now(), updated_at = now(), "
                "expired_reason = 'not settled before counts_until' WHERE status = 'open' AND counts_until <= now() "
                f"RETURNING {_ATTEMPT_COLS}")
            for r in rows:
                await self._insert_crash_row(conn, _crash_row(dict(r)))
        if rows:
            logger.warning("Expired %d unsettled attempt(s); their spend is recorded as crashed", len(rows))
        return len(rows)

    async def _insert_crash_row(self, conn, row: dict) -> None:
        await conn.execute(
            "INSERT INTO job_costs (tag, tier, company, started_at, finished_at, duration_ms, treg_usd, enrich_usd, "
            "leads, llm_used, outcome, price_tusdm, cost_assumed_calls, unconfirmed_usd) VALUES ($1,$2,$3,"
            "to_timestamp($4),to_timestamp($5),$6,$7,$8,$9,$10,$11,$12,$13,$14) ON CONFLICT (tag) DO NOTHING",
            row["tag"], row["tier"], row["company"], row["started_at"], row["finished_at"], row["duration_ms"],
            row["treg_usd"], row["enrich_usd"], row["leads"], row["llm_used"], row["outcome"], row["price_tusdm"],
            row["cost_assumed_calls"], row["unconfirmed_usd"])

    async def hand_over_open_holds(self) -> dict:
        """Rollback to a version without `spend_attempts`: turns every open hold into a `job_costs` row that the old
        code counts (finished_at = the last instant of the hold, so it counts through counts_until), and marks the
        attempt expired so a later redeploy does not count it twice. Run with both agents stopped."""
        pool = await self._p()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", SPEND_LOCK_KEY)
            rows = await conn.fetch(
                "UPDATE spend_attempts SET status = 'expired', expired_at = now(), updated_at = now(), "
                "expired_reason = 'rollback hand-over' WHERE status = 'open' "
                f"RETURNING {_ATTEMPT_COLS}, counts_until > now() AS live")
            total = 0.0
            for r in rows:
                a = dict(r)
                row = _crash_row(a)
                if a["live"]:  # still counting: keep the full conservative hold
                    hold = _hold(a)
                    row.update(tag=f"rollback-hold:{a['attempt_id']}", treg_usd=hold,
                               unconfirmed_usd=hold - a["confirmed_usd"])
                    total += hold
                await self._insert_crash_row(conn, row)
        return {"attempts": len(rows), "held_usd": round(total, 6)}

    async def stats(self, days: int = 7, now: Optional[float] = None, tier: Optional[str] = None) -> dict:
        pool = await self._p()
        rows = await pool.fetch(
            "SELECT tag, tier, to_char(finished_at AT TIME ZONE 'utc', 'YYYY-MM-DD') AS day, duration_ms, treg_usd, "
            "enrich_usd, leads, llm_used, outcome, price_tusdm, cost_assumed_calls, unconfirmed_usd FROM job_costs "
            "WHERE finished_at > now() - make_interval(days => $1) AND ($2::text IS NULL OR tier = $2)", days, tier)
        return _aggregate([dict(r) for r in rows])


_ledger: MemoryLedger = MemoryLedger()


def get_ledger() -> MemoryLedger:
    return _ledger


def set_ledger(ledger: MemoryLedger) -> None:
    global _ledger
    _ledger = ledger


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
