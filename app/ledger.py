"""Job results cache and cost ledger.

- Results: the delivered result is stored under the job tag (purchaser identifier + input hash)
  before the SDK submits it on-chain. A re-run of the same job (crash, restart, retry) returns the
  stored result unchanged, so the on-chain result hash stays the same and no provider is paid twice.
- Ledger: one row per job with duration, treg and enrichment spend, leads, LLM use and outcome.
  Feeds the daily spend cap (all agents sharing the database, i.e. one treg balance) and
  /admin/stats (per agent).
- Reservations (BL-13): a running job holds its worst-case spend so concurrent jobs, in either agent,
  cannot both spend the same remainder of the cap. Today's spend = settled jobs + open reservations.
  `settle` replaces the reservation with the job's row in one transaction. A job that never settles
  (crash, cancellation) keeps its reservation counted until the end of the UTC day; a retry of the same
  job tag reuses it instead of reserving again.
"""

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class MemoryLedger:
    def __init__(self):
        self.results: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.reservations: dict[str, dict] = {}

    async def get_result(self, tag: str) -> Optional[str]:
        hit = self.results.get(tag)
        return hit["result"] if hit else None

    async def put_result(self, tag: str, tier: str, result: str) -> None:
        self.results.setdefault(tag, {"tier": tier, "result": result, "at": time.time()})

    async def record_job(self, row: dict) -> None:
        self.jobs[row["tag"]] = {**row, "day": _day(row["finished_at"])}

    async def spend_today(self, now: Optional[float] = None) -> float:
        today = _day(now or time.time())
        return sum(j["treg_usd"] + j["enrich_usd"] for j in self.jobs.values() if j["day"] == today) + \
            await self.reserved_today(now)

    async def reserved_today(self, now: Optional[float] = None) -> float:
        today = _day(now or time.time())
        return sum(r["usd"] for r in self.reservations.values() if r["day"] == today)

    async def reserve(self, tag: str, tier: str, want_usd: float, cap_usd: float,
                      now: Optional[float] = None) -> float:
        """Grants up to want_usd of today's cap to this job and holds it; returns the grant.

        No await suspends between reading and writing, so this is atomic within one event loop."""
        now = now or time.time()
        held = self.reservations.get(tag)
        held_usd = held["usd"] if held and held["day"] == _day(now) else 0.0
        grant = held_usd + max(0.0, min(want_usd - held_usd, cap_usd - await self.spend_today(now)))
        if grant > 0:
            self.reservations[tag] = {"tier": tier, "usd": grant, "day": _day(now)}
        return grant

    async def settle(self, row: dict) -> None:
        """Records the finished job and releases its reservation."""
        await self.record_job(row)
        self.reservations.pop(row["tag"], None)

    async def stats(self, days: int = 7, now: Optional[float] = None, tier: Optional[str] = None) -> dict:
        now = now or time.time()
        cutoff = _day(now - days * 86400)
        return _aggregate([j for j in self.jobs.values() if j["day"] > cutoff and (tier is None or j["tier"] == tier)])


def _aggregate(rows: list[dict]) -> dict:
    by = defaultdict(lambda: {"jobs": 0, "delivered": 0, "empty": 0, "failed": 0, "leads": 0, "treg_usd": 0.0,
                              "enrich_usd": 0.0, "llm_fallbacks": 0, "duration_ms": 0, "revenue_tusdm": 0.0,
                              "cost_assumed_calls": 0})
    for r in rows:
        b = by[(r["day"], r["tier"])]
        b["jobs"] += 1
        b[{"delivered": "delivered", "empty": "empty"}.get(r["outcome"], "failed")] += 1
        b["leads"] += r["leads"]
        b["treg_usd"] += r["treg_usd"]
        b["enrich_usd"] += r["enrich_usd"]
        b["llm_fallbacks"] += 0 if r["llm_used"] or not r["leads"] else 1
        b["duration_ms"] += r["duration_ms"]
        b["cost_assumed_calls"] += r.get("cost_assumed_calls") or 0
        b["revenue_tusdm"] += r["price_tusdm"] if r["outcome"] != "failed" else 0.0
    out = []
    for (day, tier), b in sorted(by.items()):
        n = b["jobs"]
        out.append({"day": day, "tier": tier, **{k: round(v, 4) if isinstance(v, float) else v for k, v in b.items()},
                    "avg_leads": round(b["leads"] / n, 2), "avg_duration_ms": round(b["duration_ms"] / n),
                    "cost_usd": round(b["treg_usd"] + b["enrich_usd"], 4)})
    return {"days": out, "totals": {
        "jobs": sum(d["jobs"] for d in out), "cost_usd": round(sum(d["cost_usd"] for d in out), 4),
        "revenue_tusdm": round(sum(d["revenue_tusdm"] for d in out), 4),
        "cost_assumed_calls": sum(d["cost_assumed_calls"] for d in out)}}


# One advisory lock serializes reservations across every agent on the database ("bdlf").
SPEND_LOCK_KEY = 0x62646C66
_TODAY = "date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'"
_SPEND_TODAY_SQL = (f"SELECT (SELECT coalesce(sum(treg_usd + enrich_usd), 0) FROM job_costs WHERE finished_at >= "
                    f"{_TODAY}) + (SELECT coalesce(sum(reserved_usd), 0) FROM spend_reservations WHERE created_at "
                    f">= {_TODAY})")


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
    CREATE TABLE IF NOT EXISTS spend_reservations (tag TEXT PRIMARY KEY, tier TEXT NOT NULL,
        reserved_usd DOUBLE PRECISION NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
    """

    def __init__(self, dsn: str):
        super().__init__()
        self._dsn, self._pool = dsn, None

    async def _p(self):
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3)
            async with self._pool.acquire() as conn:
                await conn.execute(self.SCHEMA)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self) -> bool:
        pool = await self._p()
        return await pool.fetchval("SELECT 1") == 1

    async def get_result(self, tag: str) -> Optional[str]:
        pool = await self._p()
        return await pool.fetchval("SELECT result FROM job_results WHERE tag = $1", tag)

    async def put_result(self, tag: str, tier: str, result: str) -> None:
        pool = await self._p()
        await pool.execute("INSERT INTO job_results (tag, tier, result) VALUES ($1, $2, $3) ON CONFLICT (tag) "
                           "DO NOTHING", tag, tier, result)

    async def record_job(self, row: dict, conn=None) -> None:
        conn = conn or await self._p()
        await conn.execute(
            "INSERT INTO job_costs (tag, tier, company, started_at, finished_at, duration_ms, treg_usd, enrich_usd, "
            "leads, llm_used, outcome, price_tusdm, cost_assumed_calls) VALUES ($1,$2,$3,to_timestamp($4),"
            "to_timestamp($5),$6,$7,$8,$9,$10,$11,$12,$13) ON CONFLICT (tag) DO UPDATE SET finished_at = "
            "EXCLUDED.finished_at, outcome = EXCLUDED.outcome, treg_usd = job_costs.treg_usd + EXCLUDED.treg_usd, "
            "enrich_usd = job_costs.enrich_usd + EXCLUDED.enrich_usd, cost_assumed_calls = "
            "job_costs.cost_assumed_calls + EXCLUDED.cost_assumed_calls",
            row["tag"], row["tier"], row["company"], row["started_at"], row["finished_at"], row["duration_ms"],
            row["treg_usd"], row["enrich_usd"], row["leads"], row["llm_used"], row["outcome"], row["price_tusdm"],
            row.get("cost_assumed_calls") or 0)

    async def spend_today(self, now: Optional[float] = None) -> float:
        pool = await self._p()
        return float(await pool.fetchval(_SPEND_TODAY_SQL) or 0)

    async def reserved_today(self, now: Optional[float] = None) -> float:
        pool = await self._p()
        return float(await pool.fetchval(
            f"SELECT coalesce(sum(reserved_usd), 0) FROM spend_reservations WHERE created_at >= {_TODAY}") or 0)

    async def reserve(self, tag: str, tier: str, want_usd: float, cap_usd: float,
                      now: Optional[float] = None) -> float:
        pool = await self._p()
        async with pool.acquire() as conn, conn.transaction():
            # Held until commit: no other agent reads today's total between our read and our insert.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", SPEND_LOCK_KEY)
            held = float(await conn.fetchval(
                f"SELECT reserved_usd FROM spend_reservations WHERE tag = $1 AND created_at >= {_TODAY}", tag) or 0)
            spent = float(await conn.fetchval(_SPEND_TODAY_SQL) or 0)
            grant = held + max(0.0, min(want_usd - held, cap_usd - spent))
            if grant > 0:
                await conn.execute(
                    "INSERT INTO spend_reservations (tag, tier, reserved_usd) VALUES ($1, $2, $3) ON CONFLICT (tag) "
                    "DO UPDATE SET tier = EXCLUDED.tier, reserved_usd = EXCLUDED.reserved_usd, created_at = now()",
                    tag, tier, grant)
            return grant

    async def settle(self, row: dict) -> None:
        pool = await self._p()
        async with pool.acquire() as conn, conn.transaction():
            await self.record_job(row, conn)
            await conn.execute("DELETE FROM spend_reservations WHERE tag = $1", row["tag"])

    async def stats(self, days: int = 7, now: Optional[float] = None, tier: Optional[str] = None) -> dict:
        pool = await self._p()
        rows = await pool.fetch(
            "SELECT tag, tier, to_char(finished_at AT TIME ZONE 'utc', 'YYYY-MM-DD') AS day, duration_ms, treg_usd, "
            "enrich_usd, leads, llm_used, outcome, price_tusdm, cost_assumed_calls FROM job_costs WHERE finished_at > now() - "
            "make_interval(days => $1) AND ($2::text IS NULL OR tier = $2)", days, tier)
        return _aggregate([dict(r) for r in rows])


_ledger: MemoryLedger = MemoryLedger()


def get_ledger() -> MemoryLedger:
    return _ledger


def set_ledger(ledger: MemoryLedger) -> None:
    global _ledger
    _ledger = ledger


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
