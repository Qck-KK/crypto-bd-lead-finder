# Changelog

## Unreleased

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
