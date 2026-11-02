# Security review (initial draft)

Date: 2026-11-02. Scope: this repository and the Preprod InstaBox deployment. Method: code reading,
the test suite (96 passed), read-only checks on the server. No penetration test, no load test. Issue IDs
refer to `docs/IMPROVEMENT_BACKLOG.md`.

## Assets

| Asset | Where | Protection today |
|---|---|---|
| Wallet keys (selling, purchasing) | Payment Service DB, encrypted with `ENCRYPTION_KEY` | Key in server `deploy/.env` (mode 600); plaintext `seed.log` deleted by the owner (checked 2026-11-02) |
| Payment API key, treg token, Gemini key, feedback secret, admin token | `deploy/agent*.env` (mode 600), local `.env` | git-ignored; never printed by preflight (length checks only) |
| Escrowed buyer funds | Cardano escrow contract via Payment Service | Work starts only on `FundsLocked` plus our checks (see Payments) |
| Delivered lead data (names, titles) | `job_results`, `/status` responses, backups | Allowlist; backups mode 600 |
| treg balance (real money) | treg account | Per-call cost header, per-job budget, daily cap |

## Findings

### Payments and escrow
- **Work only after funds are locked** — `app/server.py:65-87` checks the record before calling the SDK
  handler; failures and unreadable status reject (fail closed). Good.
- **Amount guarantee (BL-08, fixed in code 2026-11-02).** The Payment Service sets `FundsLocked` only after
  comparing the on-chain escrow output with the request's `RequestedFunds` (native tokens exactly, ADA at
  least, same token set) and the datum; a mismatch becomes `FundsOrDatumInvalid`
  (`src/services/transactions/tx-sync/tx/initial-payment.ts`, `packages/payment-core/src/payment-amounts.ts`,
  read in the local 0.30.0 checkout). So `RequestedFunds` on a `FundsLocked` record is the locked amount. Our
  check binds it to the job and now rejects records without `blockchainIdentifier` or `inputHash` (and jobs
  without an input hash); the non-existent `PaidFunds` fallback is gone. Residual: the deployed service is
  0.29 and the shallow 0.30.0 clone has no history to confirm 0.29 has the same check.
- **Duplicate execution** — SDK fires the callback once per payment instance; re-runs return the stored result
  (`app/agent.py:165-172`); treg calls carry idempotency keys. A crash during result submission no longer
  leads to a second submit attempt: recovery treats `NextAction.requestedAction` `SubmitResultRequested` /
  `SubmitResultInitiated` as submitted and completes the job from the stored result (BL-09, fixed in code).
  The Payment Service itself also refuses a second submit unless the next action is `WaitingForExternalAction`.
- **Cache key** — purchaser identifier + input, not the payment (BL-14, Low): two purchases that reuse both
  share a result.

### Privacy and data minimisation
- Allowlist of five public fields (`app/compliance.py:18,176-191`); contact-like text scrubbed from names,
  titles and drafts; tests include rows stuffed with private data. Enrichment requests disable personal
  emails and phones (`app/treg_client.py:116`). Good.
- Only name, title, company go to Gemini (`app/drafts.py:100-113`). Buyer's pitch is free text sent to the
  model; it only affects that buyer's own drafts.
- SDK log lines with inputs/results are redacted by prefix (`app/logs.py:16-26`). Redaction depends on SDK
  message wording; a masumi upgrade could silently bypass it (BL-31).
- Feedback stores person hashes and titles, not names. One buyer's report suppresses a person for all buyers
  (BL-18, Medium — integrity, not confidentiality).

### API surface
- `/start_job` is public by protocol design but has no rate limit; each call creates a Payment Service record
  and a 12 h polling task (BL-15, Medium — availability).
- `/start_job` errors return `str(e)`, which can include Payment Service response text (BL-16, Low).
- `/docs` and `/openapi.json` are public (BL-17, Low).
- `/admin/stats`, `/metrics`, `/feedback/summary` require `x-admin-token`, compared in constant time
  (`app/server.py:119-120`). Good.
- Feedback links are HMAC-signed (`app/feedback.py:32-39`); HTML output is escaped; body size capped.
  In-memory rate limit keyed by client IP, likely the proxy's (BL-18).

### Spend controls
- Per-call max-cost header, per-job budget, daily cap that fails closed when the ledger is unreadable.
- Since BL-13 and BL-34 to BL-37 (fixed in code, not deployed):
  - **Reservation per attempt:** every attempt (run) of a job reserves its worst case in its own
    `spend_attempts` row before its first treg call. On PostgreSQL this is serialized across both agents
    (`app/ledger.py` `reserve`, advisory lock). A retry gets a new row; nothing releases another attempt's row.
  - **Metering:**
    - every treg call, including Apollo and the domain probes, must fit in the attempt's grant;
    - its max cost is written ahead to the ledger, and the call is not sent if that write fails
      (`app/spend.py`);
    - confirmed cost is kept apart from assumed (max-cost) amounts.
  - **Unknown costs:**
    - a missing or invalid cost header, an error response without one, a timeout or a cancelled call counts
      at the call's max cost, floored to the 4 decimals sent;
    - a 502's `charged_micro` counts when it is larger than the header.
  - **Hold lifetime:**
    - open holds count until the end of the UTC day containing start + `ATTEMPT_TIMEOUT_S` + 10 min, also
      across midnight;
    - the attempt timeout guarantees a run cannot spend after that;
    - expiry records a crash row with confirmed and unconfirmed amounts.
  - **Job identity:** includes tier and the verified payment id, so orders share no result, hold or treg key.
- Remaining bound: the cap holds on our accounting. That it is a hard bound depends on treg never charging
  more than the max cost sent (see "Open questions for treg"). It is not claimed as a strict hard cap.
- With the cap used up, paid jobs fail and wait for a refund instead of being refused before payment (BL-33,
  Low).

### Open questions for treg (must be confirmed before calling the cap a hard bound)
Status 2026-11-02: answered from treg's published source where possible, in `docs/TREG_BILLING.md`.
- **Hard bound:** `X-Treg-Route-Max-Cost` is **not** a hard bound on the final charge (pre-reservation guard
  only).
- **Free:** Apollo search is free.
- **Bulk enrichment:** holds one unit and bills per match.
- **Still open:** the credit rate and the deployed-vs-source questions.
1. **Direct Apollo endpoints:** on `apollo.people.search` and `apollo.people.bulk_enrich`, can the final
   charge exceed what treg reserved against `X-Treg-Route-Max-Cost`, or is that a hard upper bound on the
   charge?
2. **Apollo search price:** what does `apollo.people.search` cost per call, and can it change without notice?
3. **Bulk enrichment:** how is the `apollo.people.bulk_enrich` reservation sized (per id sent, per expected
   match, or for 10 matches)? Is billing per match, at what price, and is a request refused when its max cost
   is below the reservation?
4. **Cost header coverage:** is `X-Treg-Cost-Micro` present on every 2xx, on every non-2xx (provider 4xx/5xx,
   402, 409, 422, 429, 502), and on idempotent replays?
5. **502 `route_failed`:** where is `charged_micro` (top level or under `detail`), and does
   `X-Treg-Cost-Micro` equal it when both are present?
6. **Free refusals:** are 402 `route_max_cost`, 402 `insufficient_balance`, 429 `share_busy`, 409 (in flight)
   and 422 (key reused) never charged? Only then should `TREG_TRUST_UNCHARGED_REFUSALS` be turned on.
7. **Formats:** what precision does `X-Treg-Route-Max-Cost` accept, and what is the maximum length and allowed
   character set of `Idempotency-Key` (we send `bdlf-` + 40 hex characters)?

Facts from treg's guide (skill.md, read 2026-11-02), not yet verified against the live service:
- the max cost refuses a call whose reservation would exceed it (402, nothing charged);
- replays cost 0 and return `X-Treg-Cost-Micro: 0`; keys last 24 h;
- a duplicate in flight gets 409; a reused key with a different body gets 422;
- failed calls are free, except that 502 `route_failed` bills `charged_micro`; timeouts may be charged.

### Infrastructure
- Payment Service bound to `127.0.0.1:3001`; agents public on 8080/8081 behind InstaBox HTTPS.
- Containers: agent runs as non-root `agent` (`Dockerfile`); backup runs as uid 1000.
- Backups contain the encrypted wallet keys; they must be stored apart from `ENCRYPTION_KEY`. Off-host copy
  on the owner's PC since 2026-11-02 (BL-25).
- Single host, no CI, deployment by file copy (BL-26, BL-29, BL-30).
- External LLM call without timeout can hold paid jobs (BL-10, Medium — availability).

## Not reviewed yet
- Payment Service 0.29 itself and the V2 escrow contract (only consumed).
- Dependency vulnerabilities (no `pip-audit` run).
- InstaBox platform isolation and its proxy's header handling.

## Next security steps (in order)
1. ~~BL-08~~ fixed in code; confirm the amount check in the deployed 0.29 Payment Service source.
2. ~~BL-09~~ fixed in code: recovery treats a submit-in-progress as submitted.
3. ~~BL-13~~ fixed in code: reservations, every call metered, missing cost header = max cost.
4. ~~BL-15~~ fixed in code (rate limits and unpaid-order cap); ~~BL-33~~ fixed in code (budget check before
   payment). Still open: BL-16 / BL-17 (generic error messages, hide docs).
5. Run `pip-audit` and record the result here.
