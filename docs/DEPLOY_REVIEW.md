# Deployment review: a97ca3e → current main (roadmap step 4)

Reviewed 2026-11-02. The server runs `a97ca3e`; this upgrade brings iterations 1–11 (BL-01 to BL-37, BL-15,
BL-33). Nothing here has been run on the server yet; every step needs the owner's go-ahead and a fresh SSH
certificate.

## What changes for buyers and operators

| Change | Effect |
|---|---|
| BL-01 | **Pro reports unavailable and refuses new jobs** while `ENRICH_BUDGET_USD` is 0, as it is on the server. The registry and Sokosumi hide it. `PRO_ALLOW_WITHOUT_ENRICHMENT=true` keeps selling it for tests (preflight still fails). |
| BL-02/03/04/06 | Reports: unidentifiable leads capped at Medium, `score_breakdown`, `profile_type`, stricter company matching. The JSON gains keys; none are removed. |
| BL-10 | Gemini call bounded at 45 s, then template drafts. |
| BL-13, BL-34 to BL-37 | Spend reservations per attempt. A job with no grant makes no treg call and fails (refundable). Runs are cancelled after `ATTEMPT_TIMEOUT_S` (30 min). |
| BL-33 | `/availability` unavailable and `/start_job` 503 while today's shared budget is below one job's worst case ($0.22 standard). |
| BL-15 | `/start_job` 429 above 10 per client per hour, 30 per agent per hour, or 30 orders awaiting payment. |
| BL-36 | Job identity includes tier and payment id. Results stored by the old version are used only for orders created before the upgrade. |

## Database migration

- **Additive only**, run by each agent at startup, serialized by an advisory lock:
  - `job_costs` gets `cost_assumed_calls` and `unconfirmed_usd` (defaults 0);
  - new tables `spend_attempts` and `ledger_meta`;
  - `ledger_meta.identity_v2_since` is set once, on the first start.
- **Not touched:** the job tables (`agent_jobs`, `agent_jobs_pro`) and the Payment Service database. Nothing
  is dropped and nothing is backfilled.
- **Tested:** `tests/test_upgrade.py` builds a database with `a97ca3e`'s schema and rows and starts the new
  ledger twice. It checks that:
  - the old spend still counts;
  - a pre-upgrade order finds its old result, and a new order does not;
  - the old version's `INSERT` and spend query still work on the upgraded tables.

## Environment variables

- **Required variables:** unchanged.
- **New, all optional with safe defaults:**

  | Variable | Default |
  |---|---|
  | `GEMINI_TIMEOUT_S` | 45 |
  | `PRO_ALLOW_WITHOUT_ENRICHMENT` | false |
  | `ATTEMPT_TIMEOUT_S` | 1800 |
  | `TREG_TRUST_UNCHARGED_REFUSALS` | false |
  | `PAUSE_NEW_JOBS` | false |
  | `START_JOB_PER_CLIENT_PER_HOUR` | 10 |
  | `START_JOB_PER_HOUR` | 30 |
  | `MAX_AWAITING_PAYMENT` | 30 |
  | `RATE_LIMIT_CLIENT_HEADER` | empty |

- **Changed default:** `ENRICH_BUDGET_USD` falls back to 0 when unset (was 0.30). The server's
  `agent-pro.env` sets 0 explicitly.
- **Per-client limit behind the proxy:** behind the InstaBox proxy the connecting address may be the proxy's,
  and orders relayed by Sokosumi share its address. The per-client limit (10/h) may then act as a total of
  10/h per agent.
  - **Before deploying,** check the client addresses in the logs (pre-deploy check 3).
  - If they are all one address, set `START_JOB_PER_CLIENT_PER_HOUR` equal to `START_JOB_PER_HOUR`.
  - If the proxy forwards the real address, set `RATE_LIMIT_CLIENT_HEADER`.

## Both containers together

- **No new dependencies:** `requirements.txt` and `Dockerfile` are unchanged since `a97ca3e`, so the
  code-only image from `CLAUDE.md` applies.
- **One command recreates both agents.** They are seconds apart; the schema lock handles the concurrent
  migration.
- **The old version has no pause switch,** so drain by checking that no job is running or awaiting payment
  (pre-deploy check 1).
  - **Orders still awaiting payment at the switch:** they run under the new version with v2 identity, which
    is correct.
  - **Jobs still running at the switch:** they are re-run by recovery. treg keys changed format, so their
    searches would be charged again (cents). That is why we drain first.

## Pre-deploy checks (read only)

```bash
D=/c/Users/zhang/.config/instabox/ssh; M=m_agqzoe52hlullr222yb5ovrswm; S="ssh -p 2222 -i $D/id_ed25519 -o CertificateFile=$D/$M-cert.pub -o UserKnownHostsFile=$D/known_hosts -o IdentitiesOnly=yes $M@ssh.instabox.sh"
```
1. **Jobs in progress per agent** (read-only transaction):
   ```bash
   $S "cd /data/workspace/crypto-bd-lead-finder/deploy && docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -X -q -U masumi -d agent_jobs -c \"SELECT 'standard' AS agent, status, count(*) FROM agent_jobs GROUP BY status UNION ALL SELECT 'pro', status, count(*) FROM agent_jobs_pro GROUP BY status ORDER BY 1, 2\""
   ```
2. **Env variable names only, no values:**
   ```bash
   $S "cd /data/workspace/crypto-bd-lead-finder/deploy && for f in agent.env agent-pro.env; do echo == \$f; sed -n 's/^\([A-Z_]*\)=.*/\1/p' \$f | sort; done"
   ```
3. **How many distinct client addresses called `/start_job`** (a count, not the addresses):
   ```bash
   $S "cd /data/workspace/crypto-bd-lead-finder/deploy && docker compose logs --since 168h agent agent-pro 2>/dev/null | grep -oE '[0-9a-f.:]+ - \"POST /start_job' | awk '{print \$1}' | sed 's/:[0-9]*$//' | sort -u | wc -l"
   ```
4. **Latest backup present:**
   ```bash
   $S "ls -la /data/workspace/backups | tail -4"
   ```
5. **Owner, in treg:** team daily cap set (`docs/TREG_BILLING.md`), auto top-up off.

## Deploy steps (only after the checks pass and the owner approves)

0. **Keep the running image** for rollback:
   ```bash
   $S 'docker tag deploy-agent:latest deploy-agent:a97ca3e'
   ```
1. **Backup now:**
   ```bash
   $S "cd /data/workspace/crypto-bd-lead-finder/deploy && docker compose exec -T backup sh /deploy/backup.sh"
   ```
2. **Upload exactly the commit being deployed** (app code only; `deploy/` already matches `e3e6749`):
   ```bash
   git archive --format=tar HEAD app scripts main.py | $S "tar -x -C /data/workspace/crypto-bd-lead-finder && echo $(git rev-parse HEAD) > /data/workspace/crypto-bd-lead-finder/DEPLOYED_COMMIT"
   ```
3. **Code-only image and both agents together** (from `CLAUDE.md`):
   ```bash
   $S 'cd /data/workspace/crypto-bd-lead-finder && printf "FROM deploy-agent:latest\nCOPY app ./app\nCOPY scripts ./scripts\nCOPY main.py .\n" > /tmp/Dockerfile.codeonly && DOCKER_BUILDKIT=0 docker build -q -f /tmp/Dockerfile.codeonly -t deploy-agent:latest . && cd deploy && docker compose up -d --no-build --no-deps --force-recreate agent agent-pro'
   ```

## Post-deploy verification

- **Preflight on both agents:**
  - standard: 0 FAIL;
  - Pro: FAIL "Pro enrichment enabled", which is intended while enrichment is off.
- **Availability:** `/availability` on 8080 is `available`; on 8081 `unavailable` with the enrichment reason.
- **Admin stats:** `/admin/stats` (admin token) has `reserved_today_usd`, `start_job_gate` and
  `ledger.totals.unconfirmed_usd`.
- **Tables:** in a read-only transaction, `spend_attempts` exists and is empty, and `ledger_meta` has one row.
- **Logs:** agent logs show no errors at startup; the attempt-expiry task runs silently.
- **Then:** the Preprod integration test (roadmap step 5) with test tokens.

## Rollback

`docs/ROLLBACK.md`:
1. `PAUSE_NEW_JOBS=true`.
2. Drain.
3. Stop both agents.
4. `scripts/ledger_rollback_prep.py --apply`.
5. Redeploy the previous commit with the same steps.

Step 0 kept the previous image. Rolling it back: `docker tag deploy-agent:a97ca3e deploy-agent:latest`,
re-upload `git archive a97ca3e app scripts main.py`, then recreate both agents.

## Decisions for the owner before deploying

1. **Pro:** hidden while enrichment is off (intended, BL-01), or `PRO_ALLOW_WITHOUT_ENRICHMENT=true` for the
   integration test.
2. **Rate limit:** client identification, depending on check 3.
3. **treg:** team daily cap and auto top-up.
