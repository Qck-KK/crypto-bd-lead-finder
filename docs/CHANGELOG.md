# Changelog

## Unreleased

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
