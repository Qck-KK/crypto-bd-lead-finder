# Architecture

Details and module table: `README.md` → Architecture. This file records the decisions that shape the code.

## Job flow
`/start_job` (masumi SDK) → payment request (`app/masumi_compat.py`) → escrow `FundsLocked` → payment check
(`app/server.py`) → `app/agent.process_job`: result cache → domain resolver → provider router → compliance
allowlist + score → (Pro) enrichment → verification → Gemini outreach → report (`app/report.py`) → SDK submits
the result hash.

## Tier gating (BL-01)
- One codebase, two registered agents (`AGENT_TIER`). The only Pro-specific step is enrichment.
- `Settings.enrich_people_per_job` is the single source of truth for whether Pro enriches: a treg token and an
  `ENRICH_BUDGET_USD` that covers at least one match. Everything buyer-facing derives from it: the input-form
  notice, the report's **Enrichment** line, `/availability`, the `/start_job` guard, preflight.
- `Settings.sale_blocked_reason()` decides whether the agent may take new jobs. When it returns a reason,
  `/availability` reports `unavailable` (the registry hides the agent) and `add_sale_guard` returns 503 on
  `POST /start_job`, before any payment request. Jobs already paid are not affected, so the guard never strands
  escrowed funds.
- The standard agent runs in another container and cannot see Pro's configuration, so it makes no Pro claims.

## Lead quality (BL-02, BL-03)
- `app/scoring.py` is the only place that turns a lead into a score and label: `score_parts` (points and
  reasons per component) and `rate` (label, with the identifiability cap). The router, the enrichment rescoring
  and `apply_verification` all go through `rate`, so the cap cannot be bypassed by a later score change.
- "Identifiable" = full name or individual LinkedIn profile; without either a lead is never High.
- `profile_type` is derived in the report from `profile_url`; the compliance allowlist is unchanged.

## Money safety
- Paid provider calls: per-call max cost header, per-job budget, daily cap across both agents (shared ledger).
- Paid enrichment stays off on Preprod by default: test-token buyers must not spend real treg balance.
