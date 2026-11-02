# Deployment preflight: a97ca3e → 5db6646 (2026-11-02)

- **Scope:** read-only checks on the server, run 2026-11-02 ~13:50 UTC, plus a code review of the gate and the
  execution-time ledger. The plan is in `docs/DEPLOY_REVIEW.md`.
- **On the server, nothing was:** written, restarted, deployed or called on a paid API.
- **Database queries:** ran in read-only transactions (`default_transaction_read_only=on`, confirmed `on`).
- **Output:** counts, names and short hashes only. No env values, IP addresses or job contents were printed.
  For the config comparison (item 6), 20 non-secret variables were read on the server and only equal/differs
  was printed.

**Verdict (updated after iteration 12): no blocker left in code; deployment still not approved.**
- **B1:** fixed in code, not deployed.
- **D1–D3:** decided by the owner.
- **Open before a deploy:** the owner's treg settings (D3), and the deploy-time steps at the end of this file.

## B1 — FIXED in code (iteration 12, not deployed)

### B1 Unpaid orders never expire while the agent runs, so the unpaid-order limit can stop all sales
- **Evidence (code):**
  - masumi SDK 1.2.0 `payment.py` `start_status_monitoring` never looks at `pay_by_time`. An order that is
    never paid keeps status `awaiting_payment` and its monitoring task polls every 10 s indefinitely.
  - Only a restart expires it (`app/job_store.py` `recover_jobs`, deadline passed → failed).
  - BL-15's `MAX_AWAITING_PAYMENT` (30) counts every `awaiting_payment` job (`app/job_gate.py`
    `StartJobGate.unpaid_jobs`), expired ones included.
- **Failure:** 30 orders started and never paid make every later `/start_job` return 429 until the next
  restart, also after their pay-by time. On Preprod this costs an abuser nothing.
- **Evidence (server):** 0 orders awaiting payment today, so this is not triggered yet.
- **Fix (code, no payment change):** count only `awaiting_payment` jobs whose `pay_by_time` is still in the
  future, and add a regression test (30 expired unpaid orders must not block a new one).
- **Optional:** expire such jobs periodically, as `recover_jobs` already does at startup. That touches the job
  lifecycle, so it is the owner's call.
- **Fix (iteration 12):** `StartJobGate.unpaid_jobs` counts only `awaiting_payment` orders whose `pay_by_time`
  is still ahead.
  - **Trusted source:** that value is the Payment Service's `payByTime` from the payment request response,
    stored by the SDK, not derived by us. Milliseconds and seconds are both read; a missing or unreadable value
    counts (fail closed).
  - **Concurrency:** requests admitted but still being created by the SDK count too, under a lock, and are
    released when the request ends, including when order creation fails.
  - **Untouched:** no payment, escrow, refund or state-machine code.
  - **Tests** (`tests/test_admission.py`):
    - pay-by boundary (exactly now = passed; +1 s = open; seconds; strings; unreadable);
    - 30 expired orders do not block, 30 open ones do;
    - an order frees its slot when its pay-by passes;
    - 5 concurrent requests against a limit of 2 admit exactly 2;
    - a failed creation gives its slot back;
    - expired orders through the real SDK path do not block.
  - **Mutation checks:** counting expired orders, ignoring in-flight admissions, never releasing — all caught.
- **Remaining:** expired unpaid orders still keep their SDK monitoring task (polling every 10 s) until the next
  restart. That is SDK behaviour and a resource cost only; tracked as W10.

## Decisions (owner, 2026-11-02)

- **D1:** `START_JOB_PER_CLIENT_PER_HOUR=30` for both agents (now the code default, so no server env change);
  the total stays 30/h per agent; forwarded IP headers are not trusted. This is a provisional setting for the
  proxy environment, not yet checked against real `/start_job` traffic.
- **D2:** Pro stays hidden. `PRO_ALLOW_WITHOUT_ENRICHMENT` is not set; the production configuration is not
  changed for testing.
- **D3:** the owner handles the treg daily cap and auto top-up by hand; the agent and Claude do not touch the
  treg account.
- **BL-33:** no partial execution.

  | Budget at execution | Behaviour |
  |---|---|
  | Whole budget | Runs |
  | Part of the budget | No treg call; the job fails (refundable) |
  | Zero | No treg call; the job fails (refundable) |
  | Ledger unreadable | Fails closed |

  Implemented in iteration 12.

The analysis that led to them:

### D1 Rate limit: every request comes from one address
- **Evidence:** agent logs from 2026-11-01 21:53 to 2026-11-02 13:48 UTC (the containers' lifetime):

  | Agent | Requests | Addresses | Busiest address | Private-range requests |
  |---|---|---|---|---|
  | standard | 854 (849 `/availability`, 5 other) | **1** | 100 % | 100 % |
  | Pro | 861 (856 `/availability`, 5 other) | **1** | 100 % | 100 % |

  There were 0 `POST /start_job` requests in the window.
- **Reading:** the only address is private, so the InstaBox proxy (or the Docker gateway) connects for every
  client. The agents cannot see buyers' addresses, so the per-client limit is in effect a total limit. With the
  defaults, that is **10 orders per hour per agent** (not 30).
- **Choose one:**
  - **(a)** Set `START_JOB_PER_CLIENT_PER_HOUR` equal to `START_JOB_PER_HOUR` (e.g. 30) in both env files, and
    rely on the total and unpaid limits.
  - **(b)** First find out whether the proxy forwards the client address (e.g. `X-Forwarded-For`), then set
    `RATE_LIMIT_CLIENT_HEADER`. That needs a request log with headers, a separate check not done here.
  - **(c)** Keep 10/h as the deliberate total for Preprod.
- **Recommendation:** (a) for the deploy, (b) later.

### D2 Pro will be hidden after the deploy
- **Evidence:** `agent-pro.env` sets `ENRICH_BUDGET_USD` (enrichment off on the server) and does not set
  `PRO_ALLOW_WITHOUT_ENRICHMENT`.
- **Effect:** BL-01 then reports Pro unavailable and refuses new Pro jobs.
- **Choose:** that (intended), or `PRO_ALLOW_WITHOUT_ENRICHMENT=true` for the Preprod integration test.

### D3 treg-side protection (owner, in treg)
- **Evidence:** the team has no daily cap in treg (`daily_cap_micro` 0, check C), and auto top-up is unknown.
- **Why it matters:** treg's max cost is not a hard bound on the final charge (`docs/TREG_BILLING.md`).
- **Before more paid traffic:** set the team daily cap, confirm auto top-up is off, and keep the balance small.

## WARN: acceptable for a Preprod deploy, tracked

| # | Finding | Evidence | Handling |
|---|---|---|---|
| W1 | BL-33 checks without reserving, so orders can race past the check (the result is cached 15 s, and the cap can change between `/start_job` and payment). | Code: `app/job_gate.py` | **Resolved (iteration 12):** reservations are all or nothing, under the advisory lock. A paid job without its whole budget makes no treg call and raises `BudgetUnavailable`; the SDK marks it failed, cleans up its monitoring and submits no result. Tests: concurrent paid jobs get only whole or zero grants; on the real SDK path a failed job is not re-run or resubmitted after two restarts. |
| W1b | Refund path for such a job | Code: SDK `_execute_agent_job` (failed + cleanup, no result), `recover_jobs` (failed jobs untouched) | The agent never calls `authorize_refund` and never moves funds, so it cannot refund twice. The buyer's refund is the escrow's: no result hash is submitted, so the payment stays refundable. **Not verified live:** how and when the buyer gets the funds back (Preprod integration test). Speeding it up with `authorize_refund` would change the payment flow and is an owner decision. |
| W2 | A request that passes the gate but is then rejected by the SDK (400 input, Payment Service error) keeps its rate slot. | Code | Conservative; small effect. |
| W3 | The unpaid count can overshoot by the number of simultaneous requests (read before the SDK creates the job). | Code | Small. |
| W4 | Rate-limit windows are in memory and reset when an agent restarts. | Code | Documented. |
| W5 | No `/start_job` traffic in the log window, so the limits cannot be calibrated on real traffic. | Logs | Observe after deploy (`bdlf_start_job_total`). |
| W6 | The latest backup is from 2026-11-01 23:50 UTC (~14 h old at the check). The next daily run is ~10 h later. Only that dump is off-host. | Server listing | Deploy step 1 takes a fresh backup; copy it off-host before deploying. |
| W7 | Disk space was not determined (`df` printed nothing for the paths used). | Server | Check the disk before building the image. |
| W8 | No record of the deployed commit (`DEPLOYED_COMMIT` absent); the image `deploy-agent:latest` was built 16 h ago. | Server | Deploy steps 0 and 2 tag the old image and write `DEPLOYED_COMMIT`. |
| W9 | treg billing questions still open (deployed vs source, routed cost header on misses and failures, 409/422). | `docs/TREG_BILLING.md` | Ask treg; observe in step 5. |
| W10 | Expired unpaid orders keep an SDK monitoring task (one Payment Service status call every 10 s each) until a restart. | masumi 1.2.0 `payment.py` | Resource cost only; the gate no longer counts them (B1). An optional periodic expiry would change the job lifecycle (owner's call). |
| W11 | The limits (30/30/30) are provisional. | D1 | Re-check with `bdlf_start_job_total` and the logs after the deploy. |

## PASS

| # | Check | Evidence |
|---|---|---|
| P1 | No job in progress | `agent_jobs` (standard): 6 completed. `agent_jobs_pro`: 2 completed. 0 awaiting payment, 0 running, 0 past pay-by. Re-check right before the deploy. |
| P2 | Database is the shape the upgrade expects | Public tables: `agent_jobs agent_jobs_pro feedback feedback_deliveries job_costs job_results suppressed_people`. `job_costs` has exactly the `a97ca3e` columns (no `cost_assumed_calls`, `unconfirmed_usd`, `spend_attempts`, `ledger_meta` yet). 2 cost rows, 2 results. `tests/test_upgrade.py` migrates this shape in place. |
| P3 | Env variable names | Both env files have the same 17 names; Pro also has `ENRICH_BUDGET_USD`. No unknown or misspelt names. None of the new variables is set, so code defaults apply. `.env`, `agent.env`, `agent-pro.env` are mode 600. |
| P4 | Deploy files unchanged | `docker-compose.yml` `4593876d5aa4aa41…` and `backup.sh` `dd795f17264c5fc2…` match the repo (`e3e6749`). Containers up: agent, agent-pro, backup, payment-service (healthy), postgres (healthy). |
| P5 | Both agents share one database and one cap | Both have `AGENT_DATABASE_URL` → `postgres:5432/agent_jobs`; Pro has `AGENT_TIER: pro`. `DAILY_SPEND_CAP_USD`, `TREG_JOB_BUDGET_USD`, `TREG_MAX_COST_USD`, `MIN_LEADS_BEFORE_PAID`, `MIN_LEAD_SCORE`, `MAX_PEOPLE`, `GEMINI_MODEL`, `PRICE_MARKUP`: set and **equal** in both. `ENRICH_BUDGET_USD`: Pro only (expected). Rate-limit and attempt variables: unset in both (same defaults). |
| P6 | Backup exists and has a verified restore basis | `agent_jobs-20261101T235052Z.dump` (38 460 B) and `payment-20261101T235052Z.dump` (290 849 B), mode 600. `pg_restore --list` reads both (7 and 59 table-data entries). sha256 `aac79bef…` / `03e93520…` equals the off-host copies, which were restore-tested on 2026-11-02 (pg_restore 15.19, `HANDOFF.md`). |
| P7 | Rate slots are given back when a request is refused | Code: slots are taken before the awaits and undone on the unpaid or budget refusal. Rate-limit refusals take no slot; static refusals come first. Tests in `tests/test_job_gate.py`; mutations caught (no undo, slots taken late). |
| P8 | Execution-time ledger cannot over-grant | Code and tests: `reserve` under the advisory lock inserts one attempt row per run, with grants summing to at most the cap. Tested with two ledgers in parallel and with a lock-holding second connection (`tests/test_spend.py`). |

## Before the deploy (in order)

1. ~~B1~~ fixed in code (iteration 12).
2. ~~D1~~ per-client 30 = total, as the code default.
3. ~~D2~~ Pro stays hidden.
4. **D3, owner:** set the treg team daily cap and check auto top-up.
5. **Owner:** approve the deploy.
6. **At deploy time:**
   - re-run check 1 (no job in progress);
   - check disk space;
   - take a fresh backup and copy it off-host;
   - then follow `docs/DEPLOY_REVIEW.md`.
