# Rollback runbook: per-attempt spend holds and v2 job identity (BL-34 to BL-37)

Use this to return from the BL-34 version (or later) to a version without `spend_attempts` and the v2 job
identity, such as the deployed `a97ca3e` or BL-13 `4fd86b5`. Do not deploy the old version over running jobs.

## Why the old code cannot simply take over

- **Spend holds:** the old code counts today's spend from `job_costs` only. It does not see open
  `spend_attempts` holds, including those of crashed attempts whose treg spend may be unrecorded, so it would
  hand that budget out again.
- **Job identity:** v2 jobs key their stored result and treg idempotency keys by the v2 tag (tier + payment id).
  The old code computes the old tag, so for a v2 job that is still awaiting payment or running it would:
  - miss the stored result and run the job again;
  - send different idempotency keys, so treg charges again;
  - possibly produce a different result than the one being submitted on-chain.
- **What the old code can serve:** finished v2 jobs (`completed`, `failed`) only need their stored status and
  result, which the old code reads unchanged.

## Steps

1. **Stop new orders.** Set `PAUSE_NEW_JOBS=true` in `deploy/agent.env` and `deploy/agent-pro.env` and
   recreate both agents (`docker compose up -d --no-build --no-deps --force-recreate agent agent-pro`).
   `/availability` then reports unavailable, so the registry hides the agents, and `/start_job` answers 503
   before any payment request is created. Jobs already paid keep running.
2. **Drain.** Wait until both agents have no job `awaiting_payment`, `awaiting_input` or `running`
   (`/admin/stats` → `jobs_by_status`). Unpaid orders expire at their pay-by time; running jobs finish or
   hit `ATTEMPT_TIMEOUT_S` (30 min).
3. **Stop both agents:** `docker compose stop agent agent-pro`.
4. **Check, then hand over the open holds:**
   ```
   docker compose run --rm --no-deps -T agent python scripts/ledger_rollback_prep.py
   docker compose run --rm --no-deps -T agent python scripts/ledger_rollback_prep.py --apply
   ```
   - The script refuses while any job is unfinished.
   - With `--apply`, it turns every open hold into a `job_costs` row the old code counts, worth the full
     hold: `max(reserved, confirmed + assumed + pending)`. The row's `finished_at` is the last instant of
     the hold, so it keeps counting until the hold's `counts_until` (at most the end of the next UTC day).
   - It also marks those attempts `expired`, so a later redeploy does not count them twice.
   - It prints only counts and amounts.
5. **Deploy the old version** and run `scripts/preflight.py` in both containers.

## What stays in the database (all additive, ignored by the old code)

- **New tables:** `spend_attempts` and `ledger_meta`; nothing is dropped.
- **New `job_costs` columns:** `cost_assumed_calls` and `unconfirmed_usd`. The old code's `INSERT` names its
  columns, so the defaults apply.
- **Cache rows:** `job_results` rows under v2 tags are never looked up by the old code.

## Rolling forward again

Deploy the new version normally (with no running jobs).
- **Holds:** the handed-over holds are `expired` attempts plus `rollback-hold:` rows in `job_costs`, which
  count until their `finished_at` day, so nothing is counted twice or forgotten.
- **Identity:** `ledger_meta.identity_v2_since` keeps its first value. Results stored by the old code in
  between have a later `created_at`, so the legacy fallback ignores them, and orders taken by the old code
  run normally under v2.
