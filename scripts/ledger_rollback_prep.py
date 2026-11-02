"""Prepare a rollback to a version without per-attempt spend holds (anything before BL-34, e.g. a97ca3e).

Runbook (docs/ROLLBACK.md):
  1. Set PAUSE_NEW_JOBS=true for both agents and recreate them: /availability unavailable, /start_job 503.
  2. Wait until no job is awaiting payment or running (pay-by deadlines expire unpaid orders).
  3. Stop both agents:  docker compose stop agent agent-pro
  4. Check, then apply:
       docker compose run --rm --no-deps -T agent python scripts/ledger_rollback_prep.py
       docker compose run --rm --no-deps -T agent python scripts/ledger_rollback_prep.py --apply
  5. Deploy the old version.

Refuses while any job is awaiting payment, awaiting input or running: the old code keys results and treg
idempotency by the old tag, so it would not find a v2 job's stored result and could run it again, paying
treg again. With --apply, every open spend hold becomes a job_costs row that the old code counts until the
hold's counts_until (ledger.hand_over_open_holds), so no possible spend is forgotten by the rollback.
Prints counts and amounts only, never secrets.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ledger import PostgresLedger  # noqa: E402

JOB_TABLES = ("agent_jobs", "agent_jobs_pro")
UNFINISHED = ("awaiting_payment", "awaiting_input", "running")


async def unfinished_jobs(pool) -> dict:
    found = {}
    for table in JOB_TABLES:
        if await pool.fetchval("SELECT to_regclass($1)", table) is None:
            continue
        rows = await pool.fetch(f"SELECT status, count(*) AS n FROM {table} WHERE status = ANY($1::text[]) "
                                "GROUP BY status", list(UNFINISHED))
        for r in rows:
            found[f"{table}.{r['status']}"] = r["n"]
    return found


async def run(dsn: str, apply: bool) -> int:
    led = PostgresLedger(dsn)
    try:
        pool = await led._p()
        busy = await unfinished_jobs(pool)
        held = await led.reserved_today()
        open_n = await pool.fetchval("SELECT count(*) FROM spend_attempts WHERE status = 'open'")
        print(f"open spend attempts: {open_n}, held now: {held:.4f} USD")
        if busy:
            print("REFUSED: unfinished jobs " + ", ".join(f"{k}={v}" for k, v in sorted(busy.items())))
            return 2
        if not apply:
            print("OK to apply (no unfinished jobs). Re-run with --apply.")
            return 0
        summary = await led.hand_over_open_holds()
        print(f"handed over {summary['attempts']} attempt(s), {summary['held_usd']:.4f} USD still held")
        return 0
    finally:
        await led.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the hand-over (default: check only)")
    args = parser.parse_args()
    dsn = os.getenv("AGENT_DATABASE_URL")
    if not dsn:
        sys.exit("AGENT_DATABASE_URL is not set")
    sys.exit(asyncio.run(run(dsn, args.apply)))


if __name__ == "__main__":
    main()
