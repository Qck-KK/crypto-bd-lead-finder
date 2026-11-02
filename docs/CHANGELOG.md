# Changelog

## Unreleased

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
