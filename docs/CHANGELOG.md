# Changelog

## Unreleased

### Iteration 11 — BL-33, BL-15 and the deployment review (2026-11-02)
- Before the SDK creates a payment request, `/start_job` now checks today's shared data budget (BL-33):
  - when what is left (settled spend + open holds, both agents) cannot cover one job's worst case ($0.22
    standard), `/availability` reports unavailable and `/start_job` answers 503 until 00:00 UTC;
  - it does the same when the ledger cannot be read;
  - unpaid orders hold no budget.
- Rate limits on `/start_job` (BL-15), all with Retry-After:
  - 10 per client and 30 per agent per hour (429);
  - at most 30 orders awaiting payment (429).
  - The client is the peer address unless `RATE_LIMIT_CLIENT_HEADER` names a trusted proxy header.
- `/admin/stats` → `start_job_gate`, `/metrics` → `bdlf_start_job_total{result=…}`.
- `docs/DEPLOY_REVIEW.md`: behaviour changes, migration (tested in place on an `a97ca3e` database), env vars,
  read-only pre-deploy checks, deploy, verification and rollback steps.

### Iteration 10 — BL-34 to BL-37: attempt-level spend holds, midnight-safe expiry, order identity (2026-11-02)
- Every run of a job (attempt) has its own spend reservation (`spend_attempts`). A retry gets a new one, and
  settling closes only its own, so a crashed attempt's possible spend is never released by its retry
  (treg replays the retry's calls at $0).
- Each treg call's max cost is written to the ledger before the call is sent; the call is not sent if that
  write fails. Confirmed cost and assumed (max-cost) amounts are kept apart.
- Open holds count until the end of the UTC day containing start + `ATTEMPT_TIMEOUT_S` (default 1800 s)
  + 10 min, also after midnight. A run is cancelled at `ATTEMPT_TIMEOUT_S` (the job fails, refundable).
  Unsettled holds then expire into `crashed` cost rows; this runs at startup and every 10 min, exactly once
  across both agents.
- Job identity = agent, tier and the order's payment id (from the SDK job record, verified by the payment
  check) + purchaser identifier + input. Standard and Pro orders, or two payments with the same identifier
  and input, no longer share results, holds or treg keys (also fixes BL-14).
- Results stored under the old tag are used only for orders created before the upgrade, from the same tier.
- treg idempotency keys hash the request body; the max cost is floored to the 4 decimals sent; a 502's
  `charged_micro` counts. Free treg refusals count $0 only with `TREG_TRUST_UNCHARGED_REFUSALS=true`.
- New: `PAUSE_NEW_JOBS`; `docs/ROLLBACK.md`, `scripts/ledger_rollback_prep.py`. New `job_costs` column
  `unconfirmed_usd`; new tables `spend_attempts`, `ledger_meta`; the migration is serialized across agents.
- Payment verification, escrow, refunds and the payment state machine are unchanged.

### Iteration 9 — BL-13: the daily spend cap holds under concurrency (2026-11-02)
- A job reserves its worst-case treg spend before its first call: domain probes and the Apollo search at $0.01
  each, the routed budget, and the Pro enrichment budget when on. It gets what the cap still allows. On
  PostgreSQL the reservation is serialized across both agents with an advisory lock (new table
  `spend_reservations`).
- Every treg call is metered against that grant, the free Apollo search and domain probes included. A call is
  sent only if its max cost fits, and its cost is deducted. The actual spend replaces the reservation when the
  job finishes.
- A missing or invalid `X-Treg-Cost-Micro` header now counts as the call's max cost (was $0), also for failed
  calls and timeouts; `"0"` stays zero. Counted per job as `cost_assumed_calls` (new `job_costs` column) and
  shown in `/metrics`.
- A crashed job's reservation counts until the end of the UTC day; a retry of the same job reuses it.
- With the cap used up a job now makes no treg call and fails (refundable) instead of delivering an
  Apollo-only result. `/admin/stats` adds `reserved_today_usd`; `spend_today_usd` includes it.
- Payment verification, escrow, refunds and the job state machine are unchanged.

### Iteration 8 — BL-10: the Gemini drafting call is bounded (2026-11-02)
- The one drafting call per job now times out after `GEMINI_TIMEOUT_S` (default 45 s); google-genai sets no
  timeout of its own, so a hanging call used to keep a paid job `running` until its result deadline. On
  timeout the pending call is cancelled and every lead gets the template drafts; the job completes normally.
- Transport errors (connection refused/reset from httpx or aiohttp) also fall back to templates instead of
  failing the job; before, only API errors and malformed responses did.
- The fallback log line names the model and, for API errors, the status code.

### Iteration 7 — BL-06: name matching rejects look-alike companies (2026-11-02)
- A lead found by company name now needs the same core company name as requested, after removing legal and
  generic words (Inc, Labs, Foundation, Global, .io, …). Before, any company whose name *contained* the
  requested words matched, so "Link" accepted "Link Metal" and "Uniswap" accepted "Uniswap Ventures".
- Spacing variants still match ("Chain Link" = "Chainlink"), keeping word order ("Link Chain" no longer does).
- A link on the requested company's own domain still counts as a match, unchanged.
- The resolver and the enrichment check use the same comparison (`same_company`), which now also ignores
  "Global", "International", "HQ" and domain endings.

### Iteration 6 — BL-05: a failed domain probe is not cached (2026-11-02)
- When the domain probe raises (treg error, timeout), the unconfirmed resolution is used for that job only and
  not cached; the next job for the company probes again. Before, it was cached for 24 h, so one provider
  hiccup pushed every job for that company onto the unconfirmed-domain / name-search path for a day.
- Completed resolutions (confirmed, or genuinely not found) are still cached for 24 h.
- The resolver note now says when the data provider did not answer, instead of "could not confirm".

### Iteration 5 — BL-04: "confirmed by domain" only for a confirmed domain (2026-11-02)
- Leads found by searching a domain the data provider could not confirm now carry evidence `given domain`:
  6 company points (as for a name match) instead of 10, and the reason "company matched by name (domain given
  but not confirmed)" instead of "company confirmed by domain". Leads from a confirmed domain are unchanged.
- Every lead now has a company-evidence reason (also "company matched by name" / "company not confirmed").
- Apollo's domain search still counts as "current role" for an unconfirmed domain (it only returns current
  staff); the employment check's "employer confirmed" still requires a confirmed domain, as before.

### Iteration 4 — BL-09: recovery never resubmits a result in flight (2026-11-02)
- After a restart, a running job whose payment's `NextAction.requestedAction` is `SubmitResultRequested` or
  `SubmitResultInitiated` is treated as submitted (as `ResultSubmitted` already was): it is marked completed
  from the stored result, not re-run and not submitted again. Before, it re-ran and called submit-result,
  which the Payment Service refuses in that state, so the job ended `failed` although its result was being
  put on-chain.
- A `RefundRequested` payment with a submission in progress counts as submitted, not refunded.
- Source references for the action names and the submit-result guard in `app/job_store.py`.

### Iteration 3 — BL-08: payment check binds the escrow guarantee to the job (2026-11-02)
- Documented what `FundsLocked` guarantees: the Payment Service compares the on-chain locked amount with the
  request's `RequestedFunds` before setting it (source references in `app/server.py` `check_payment`).
- A payment record without `blockchainIdentifier` or `inputHash`, or a job without an input hash, is now
  rejected (was accepted). The `PaidFunds` fallback (a field the Payment Service does not return) is removed;
  malformed amounts count as 0.
- README "Payment acceptance" wording corrected.

### Iteration 2 — BL-02 + BL-03: honest quality labels and profile links (2026-11-02)
- A lead with a masked name and no individual LinkedIn profile is rated at most Medium (reason stated).
- Every lead carries `score_breakdown` (points per component, incl. the employment check ±5) and a reason
  for every component; the report lists the points.
- `profile_type` per lead (`linkedin` / `company_website` / `none`); the report says "company site only"
  instead of presenting the company homepage as a profile, and its summary counts full names and LinkedIn
  profiles ("Identifiable").
- "Verification" is now labelled "Employment check" in the report (status values unchanged).

### Iteration 1 — BL-01: Pro is only sold with enrichment on (2026-11-02)
- Enrichment is off unless `ENRICH_BUDGET_USD` covers at least one match (~$0.026); the default is now 0
  (was 0.30 when unset).
- Pro without enrichment reports `unavailable` on `/availability` and answers `/start_job` with 503; paid jobs
  already in progress still run. `PRO_ALLOW_WITHOUT_ENRICHMENT=true` overrides this for test deployments.
- Input form: promises enrichment only when it is on; the standard agent no longer advertises Pro.
- Reports: Pro reports carry an **Enrichment** line and an `enrichment` JSON block saying what happened; the
  misleading "apollo enrich … ok" search note is removed.
- `scripts/preflight.py`: FAIL "Pro enrichment enabled" while enrichment is off.
  `scripts/registration_values.py`: warns before registering an unenriched Pro.

### Baseline
- `e3e6749` daily database backups in a compose service (restore tested).
- `6bd594b` audit: backlog, security review, Mainnet readiness drafts.
