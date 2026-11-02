# Mainnet readiness (initial draft)

**Status on 2026-11-02: not ready.** Both agents fail 2 Mainnet preflight checks and carry 7 warnings, and
Phase 1 correctness issues are open. This document does not claim legal compliance. The step-by-step
launch list stays in `MAINNET_CHECKLIST.md`; this file tracks the evidence. Issue IDs refer to
`docs/IMPROVEMENT_BACKLOG.md`.

## Preflight (`scripts/preflight.py --target Mainnet`, run in each container on 2026-11-02)

| Check | standard | Pro | Note |
|---|---|---|---|
| agent registered on Mainnet | FAIL | FAIL | Not registered — intended until launch |
| code guard (`main.py:121-122`) | FAIL | FAIL | Refuses `NETWORK` other than Preprod — lift only in a reviewed commit |
| API key limited to target network | WARN | WARN | Key is Preprod-only (correct for now) |
| Sokosumi whitelist | WARN | WARN | Owner decision |
| Mainnet wallets | WARN | WARN | New wallets, collection wallet on hardware |
| Blockfrost Mainnet key | WARN | WARN | Payment Service only |
| legal | WARN | WARN | Privacy policy and terms URLs missing (BL-28) |
| data providers | WARN | WARN | treg / upstream terms for paid resale (BL-21) |
| real-money test | WARN | WARN | First Mainnet sale by hand |

Preprod preflight: 0 FAIL, 0 WARN on both agents (deployed code). Since iteration 1 (BL-01, not deployed yet),
Pro preflight FAILs "Pro enrichment enabled" while `ENRICH_BUDGET_USD` is 0, on both targets.

## Gates before any Mainnet step

| Gate | Status | Backlog |
|---|---|---|
| Pro delivers what it advertises, or is not sold | Fixed in code, not deployed; enrichment budget is the owner's decision | BL-01 |
| Quality labels honest (masked / no LinkedIn never High) | Fixed in code, not deployed | BL-02, BL-03 |
| Payment amount guarantee documented and enforced | Fixed in code, not deployed (verified against Payment Service 0.30.0 source; 0.29 to confirm) | BL-08 |
| Recovery never resubmits | Fixed in code, not deployed; no fault injected on the live stack yet | BL-09 |
| Spend cap holds under concurrency | Open | BL-13 |
| Gemini timeout | Fixed in code, not deployed (45 s, `GEMINI_TIMEOUT_S`) | BL-10 |
| At least 3 real users tested the output | Open | BL-22 |
| Alerting on availability, rejected payments, spend | Open | BL-24 |
| Daily backups + off-host copy + restore drill | In progress (on-host daily and one drill done) | BL-25 |
| Separate Mainnet Payment Service, keys, wallets | Not started | `MAINNET_CHECKLIST.md` §2 |
| Legal: privacy policy, terms, GDPR basis | Not started | BL-28 |
| CI running the test suite | Not started | BL-29 |
| Deploy from a commit, no drift | Not started | BL-30 |

## Owner-only decisions
Pro enrichment budget or Pro withdrawal (BL-01); Mainnet price; lifting the Preprod guard; Mainnet
registration and whitelist form; legal documents; provider terms for resale.
