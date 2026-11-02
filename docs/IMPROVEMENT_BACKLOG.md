# Improvement backlog

Audit of 2026-11-02 (AGENT_LOOP.md Step A + B). No application code was changed for this audit.
Evidence is from the repository at `a97ca3e` plus the uncommitted backup changes, the test suite, and
read-only checks on the InstaBox deployment (preflight, aggregate counts over stored results; no
person data was printed).

Status values: `Open`, `In Progress`, `Blocked`, `Fixed`, `Verified`.
Severity: Critical / High / Medium / Low. Effort: S (< half a day), M (1–2 days), L (> 2 days).

## Baseline measurements

| Metric | Value | How measured |
|---|---|---|
| Jobs on the deployment | 8, all `completed` (6 standard, 2 Pro), 0 failed | `agent_jobs`, `agent_jobs_pro` status counts |
| Stored results analysed | 2 (Uniswap Labs / standard, Coinbase / Pro); older jobs predate the result cache | `job_results` |
| Leads in those results | 13 | parsed JSON block of each result |
| Masked names | 10 / 13 (77 %) | `*` in name |
| Individual LinkedIn coverage | **0 / 13 (0 %)** | `linkedin.com/in/` or `/pub/` |
| Profile link = company homepage | 13 / 13 | profile URL not LinkedIn |
| Rated "High" quality | 9 / 13 (8 of 10 in the Pro job, all masked, none with LinkedIn) | `quality` |
| Role match | 13 / 13 | title reason in `match_reasons` |
| Provider spend | $0.0005 total (standard job), $0 (Pro job) | `job_costs` |
| Cost per qualified lead* | undefined: 0 qualified leads | *qualified = right company and role, unmasked name, individual LinkedIn |
| Pro promise fulfilment | full names 2/10, LinkedIn 0/10, LLM drafts 0/10 (template fallback), verification 10/10 | Coinbase Pro result + `job_costs.llm_used = false` |
| Duplicate charges / submissions observed | 0 | 8 jobs; no duplicate rows |
| Test suite | 96 passed (incl. 5 PostgreSQL tests on a throwaway cluster) | `pytest -q`, 2026-11-02 |
| Mainnet preflight | 2 FAIL, 7 WARN on both agents | `scripts/preflight.py --target Mainnet` in each container |
| Preprod preflight | 0 FAIL, 0 WARN on both agents | same, `--target Preprod` |

Two results are a small sample; the numbers show the pattern, not a rate to rely on.

## Core questions

1. **Does Pro deliver incremental value worth 4x?** No. With `ENRICH_BUDGET_USD=0` (confirmed in the
   `agent-pro` container) the only Pro-specific code path (`app/agent.py:247-255`) enriches nothing.
   Pro output differs from Standard only in the report title and an "apollo enrich: 0 leads" search note. Its 1.05 tUSDM price is derived entirely from
   the enrichment cost it does not incur (`app/pricing.py:26`). See BL-01.
2. **Does lead quality meet paid-buyer expectations?** No. 0 % individual LinkedIn, 77 % masked names,
   every profile link is the company homepage, yet most leads are labelled High and Verified. Role and
   company matching are good on this sample. See BL-02, BL-03, BL-20.
3. **Do payment, idempotency, recovery and backups survive real faults?** Partly. The designs are sound
   and unit/integration tested (mocks + PostgreSQL), but: the amount check compares our own requested
   funds, not what was paid (BL-08; fixed in code in iteration 3: the Payment Service matches the locked
   amount before `FundsLocked`, and identity fields are now required); recovery did not look at an in-flight result submission (BL-09; fixed in code in iteration 4);
   no fault has been injected on the live stack; a restore drill passed once (2026-11-02) and off-host
   copies are manual (BL-25).

## Known problems from AGENT_LOOP.md, verified

| # | Hypothesis | Verdict | Backlog |
|---|---|---|---|
| 1 | Pro enrichment disabled, Pro costs ~4x | Confirmed | BL-01 |
| 2 | Pro advertises full names and LinkedIn it may not deliver | Confirmed, including the on-chain registry description and the Standard upsell text | BL-01 |
| 3 | Masked surnames, missing LinkedIn | Confirmed: 77 % masked, 0 % LinkedIn | BL-03, BL-20 |
| 4 | No real customers or feedback | Confirmed: 8 jobs, all test purchases; 0 feedback responses | BL-22 |
| 5 | Preprod, not Mainnet | Confirmed, by design (`main.py:121-122`) | BL-27 |
| 6 | Legal and compliance incomplete | Confirmed: no privacy policy or terms; preflight WARN "legal" | BL-28 |
| 7 | Monitoring and alerting insufficient | Confirmed: endpoints exist, nothing scrapes or alerts | BL-24 |
| 8 | Two Mainnet preflight checks fail | Confirmed: "agent registered on Mainnet" and "code guard" — both intentional until launch | BL-27 |
| 9 | One application server | Confirmed: DB, Payment Service and both agents on one InstaBox machine | BL-26 |
| 10 | Backups require manual download | Partly fixed: daily on-host dumps now automated (uncommitted); off-host copy still manual | BL-25 |
| 11 | Treg is the only provider | Confirmed: every data call goes through `app/treg_client.py` | BL-21 |

## Phase 1 — Make it correct (prioritised)

### BL-01 Pro tier sells features that are switched off
- **Phase:** 1 · **Severity:** Critical · **Effort:** S (honest interim) / M (enrichment on) · **Status:** Fixed in code
  (iteration 1, 2026-11-02; not deployed yet). Owner decision on enabling paid enrichment still open: on Preprod
  it stays off, so Pro is not sold.
- **Resolution (iteration 1):** `Settings.enrich_people_per_job` / `enrichment_enabled` (budget for ≥ 1 match and
  a treg token) is the single source of truth; the default budget is now 0. The input form promises enrichment
  only when it is on and the Standard form makes no Pro promise (`app/agent.py` `what_you_get`). Each Pro report
  has an **Enrichment** line and an `enrichment` JSON block (off / `n of m` completed / not needed / skipped by the
  daily cap / provider unavailable); the old "apollo enrich … ok" search note is gone. While enrichment is off, Pro
  `/availability` reports `unavailable` and `/start_job` answers 503 (`app/server.py` `add_sale_guard`), unless
  `PRO_ALLOW_WITHOUT_ENRICHMENT=true`. Preflight FAILs "Pro enrichment enabled" in both cases;
  `scripts/registration_values.py` warns before registering an unenriched Pro. Tests: `tests/test_tiers.py`,
  `tests/test_upsell.py`, `tests/test_agent.py`. Remaining: the on-chain registry description still mentions full
  names and LinkedIn (changes only by re-registration; the listing is hidden while Pro is unavailable).
- **Description:** Pro (1.05 tUSDM) delivers the same leads as Standard (0.27). Its advertised difference
  — full names and LinkedIn profiles — comes only from enrichment, which is off on the deployment.
- **Evidence:** `agent-pro` env `ENRICH_BUDGET_USD=0`; `app/enrich.py:27-28` (`affordable = 0` → no calls);
  `app/agent.py:34-39` (Pro schema text), `app/agent.py:31-32` (Standard text: "the Pro agent adds full names
  and LinkedIn profiles"), `scripts/registration_values.py:25-27,39` (registered description and `enrichment`
  tag), `app/pricing.py:26` (price includes $0.26 enrichment), `app/agent.py:282-284` (report says
  "apollo enrich … 0 leads, ok"). Live Coinbase Pro result: 8/10 masked, 0/10 LinkedIn.
- **Root cause:** Enrichment was turned off for Preprod (test-token buyers would spend real treg balance), but
  price, descriptions and registry entry were left as if it were on. Nothing ties the claims to the setting;
  preflight passes with enrichment off (`scripts/preflight.py:53-57`).
- **Business & metric impact:** Commercial integrity ≈ 0 % on the Pro-only promises; a buyer comparing both
  tiers sees no difference. Reputational risk with judges or first users.
- **Security/financial risk:** Misrepresentation; on Mainnet this would be selling a feature not delivered.
- **Proposed solution:** Owner picks one: (a) enable enrichment with an approved per-job budget (≈ $0.026 per
  person, ≤ $0.26 per job) and a daily cap; (b) keep it off and stop selling Pro until it is on (Pro
  `/availability` reports unavailable, so the registry hides it); (c) deregister and re-register Pro with an
  honest description and price. Independent of the choice, in code: derive every claim (schema text, Standard
  upsell, report search notes) from whether enrichment is actually enabled, and make preflight FAIL for Pro
  when its differentiator is disabled.
- **Acceptance criteria:** With `ENRICH_BUDGET_USD=0`, no text served by either agent promises full names or
  LinkedIn from Pro, Pro preflight reports FAIL, and the report does not show enrichment as "ok". With a
  budget set, a mocked Pro job enriches masked leads and the result shows the gain. Tests cover both.

### BL-02 Quality score and verification rate unusable leads as High / Verified
- **Phase:** 1 · **Severity:** High · **Effort:** S · **Status:** Fixed in code (iteration 2, 2026-11-02; not deployed)
- **Resolution (iteration 2):** `app/scoring.py` `score_parts` returns points per component and a reason for each
  (incl. "full name", "no individual profile (company website only)", "no profile link"); `rate()` caps the label
  at Medium when the name is masked and there is no individual LinkedIn profile, with the reason "rated at most
  Medium: …". The cap is re-applied after verification (`app/verify.py`), which adds `employment_check` ±5 to the
  breakdown. The report calls the check "Employment check" and lists the points per lead. Effect on the live
  Coinbase Pro sample: its 8 High leads (all masked, no LinkedIn) would be Medium. Tests: `tests/test_quality.py`.
- **Description:** A masked lead with only the company homepage scores 81/100 (High) and is "Verified".
- **Evidence:** `app/scoring.py:52-82`: exact role 35 + senior 20 + masked name 5 + company site 6 + domain 10
  = 76, `app/verify.py:59-60,80` adds +5 for Verified (employer + current + fresh — nothing about reachability).
  Coinbase Pro result: 8 High, 9 Verified, all masked, none with LinkedIn.
- **Root cause:** Score weights reward role and seniority (55 points) far more than whether the buyer can
  identify and reach the person (masked name and missing profile cost only 10 + 14 points). "Verified" checks
  employment, but the label reads as "verified lead".
- **Business & metric impact:** Buyers are told most leads are High quality when none are directly actionable;
  undermines the "explainable score" requirement.
- **Security/financial risk:** None directly; trust and refund risk.
- **Proposed solution:** Add an explicit "contactability" dimension (unmasked name + individual LinkedIn), cap
  the label at Medium when the name is masked and there is no individual profile, and rename the verification
  status to what it checks ("Employment verified"). Keep the score breakdown in `match_reasons`.
- **Acceptance criteria:** A masked lead without LinkedIn can never be High; reasons list every scored component;
  unit tests for the boundary cases; report wording updated.

### BL-03 "Profile" is the company homepage, presented as a person's profile
- **Phase:** 1 · **Severity:** High · **Effort:** S · **Status:** Fixed in code (iteration 2, 2026-11-02; not deployed)
- **Resolution (iteration 2):** each lead in the JSON has `profile_type` (`linkedin` / `company_website` / `none`);
  the report shows "company site only: …" instead of a bare link, explains it under "How to use", and the summary
  has an **Identifiable** line (`n of m with a full name · n of m with an individual LinkedIn profile`, also
  `with_full_name` / `with_linkedin` in the JSON summary). The allowlisted fields are unchanged.
- **Description:** When there is no individual profile, `profile_url` falls back to `https://<company domain>`
  and the report lists it under **Profile**.
- **Evidence:** `app/compliance.py:123-124`; `app/report.py:51,91,108`; live results: 13/13 leads have the
  company homepage as their profile.
- **Root cause:** Fallback chosen so every lead has a link; the field name and report label were not changed.
- **Business & metric impact:** LinkedIn coverage 0 % is hidden behind a link that looks like a profile.
- **Security/financial risk:** None.
- **Proposed solution:** Keep the allowlisted field, but report it as "Company website (no individual profile
  found)" and count coverage explicitly in the summary (`n of m with LinkedIn`).
- **Acceptance criteria:** Report and JSON distinguish individual profile from company site; summary shows the
  LinkedIn coverage; compliance allowlist tests still pass.

### BL-08 Payment amount check compares our own request, not the payment
- **Phase:** 1 · **Severity:** High · **Effort:** M (needs Payment Service investigation) · **Status:** Fixed in code
  (iteration 3, 2026-11-02; not deployed)
- **Resolution (iteration 3):** Guarantee established from the Payment Service source (local checkout
  `masumi-node/masumi-payment-service`, release 0.30.0): `updateInitialPaymentTransaction`
  (`src/services/transactions/tx-sync/tx/initial-payment.ts`) sets `FundsLocked` only if
  `checkPaymentAmountsMatch` (`packages/payment-core/src/payment-amounts.ts`) finds every requested native
  token locked in exactly the requested amount, ADA at least as requested, the same token set, and a datum that
  matches seller, times and no result hash; otherwise the record becomes `FundsOrDatumInvalid` and waits for
  manual action. Spoofed outputs without the buyer as input are ignored. So `RequestedFunds` on a `FundsLocked`
  record *is* the locked amount, and there is no `PaidFunds` field (the resolve endpoint includes only
  `RequestedFunds`, `WithdrawnForSeller`, `WithdrawnForBuyer`). `app/server.py` `check_payment` now rejects a
  missing `blockchainIdentifier` or `inputHash` (or a job without an input hash), drops the `PaidFunds`
  fallback and treats malformed amounts as 0; the docstring cites the source. README wording corrected. Tests:
  `tests/test_reliability.py` (missing ids, `FundsOrDatumInvalid`, malformed/short/non-list funds, a job
  without input hash, an end-to-end rejection with no provider call). Residual: the deployment runs 0.29; the
  local clone is shallow, so 0.29 having the same check is likely but not confirmed from source.
- **Description:** `check_payment` sums `RequestedFunds` (falls back to `PaidFunds`). The live record has no
  `PaidFunds`; `RequestedFunds` is the amount our own `/payment` call asked for. The check therefore confirms
  our configuration, not what the buyer locked. Missing `blockchainIdentifier` or `inputHash` also pass.
- **Evidence:** `app/server.py:44,50,52-55`; live `resolve-blockchain-identifier` record (2026-11-02): keys
  include `RequestedFunds` (`270000` tUSDM) and no `PaidFunds`.
- **Root cause:** Field semantics assumed, not verified against Payment Service 0.29.
- **Business & metric impact:** Payment safety relies entirely on the Payment Service's `FundsLocked` state;
  README claims more than the code checks.
- **Security/financial risk:** If the Payment Service ever reports `FundsLocked` for an underpaid escrow, work
  runs unpaid. Unverified whether 0.29 / the V2 contract can do that.
- **Proposed solution:** Confirm from the Payment Service source what `FundsLocked` guarantees about amount and
  asset; if available, compare the on-chain/locked amount; make missing identity fields a rejection; correct
  README wording.
- **Acceptance criteria:** Documented guarantee with a source reference; tests with a record that lacks
  `blockchainIdentifier`/`inputHash` (rejected) and with a lower locked amount (rejected, if the field exists).

### BL-09 Recovery may resubmit a result that is already being submitted
- **Phase:** 1 · **Severity:** Medium · **Effort:** S · **Status:** Fixed in code (iteration 4, 2026-11-02; not deployed)
- **Resolution (iteration 4):** Verified against the Payment Service source (local 0.30.0 checkout): the action
  names are `SubmitResultRequested` / `SubmitResultInitiated` (`prisma/schema.prisma`, enum `PaymentAction`),
  and `src/routes/api/payments/submit-result/index.ts` accepts a submit only while the next action is
  `WaitingForExternalAction`. So the old behaviour was not a duplicate on-chain submission but a needless
  re-run followed by a refused submit (404), which left the job `failed` while its result was being put
  on-chain. `app/job_store.py` `_payment_state` now reads `NextAction.requestedAction` too; `recover_jobs`
  treats a submit-in-progress like `ResultSubmitted` (completed from the stored result, no re-run, no
  submit, no monitoring), also when the on-chain state is `RefundRequested`. Tests in
  `tests/test_reliability.py`: FundsLocked/RefundRequested × Requested/Initiated → completed with no submit;
  FundsLocked with `WaitingForExternalAction`, no `NextAction` or a malformed one → resumed;
  RefundRequested with no submission → failed. The 4 in-flight cases fail on the previous code.
- **Description:** After a crash between "submit result requested" and the on-chain `ResultSubmitted`, recovery
  sees `FundsLocked`, reruns (cached result) and calls submit again.
- **Evidence:** `app/job_store.py:156-172` checks only `onChainState`, not `NextAction.requestedAction`.
- **Root cause:** Only final on-chain states are treated as "result submitted".
- **Business & metric impact:** Possible duplicate submission attempt (payment-safety metric).
- **Security/financial risk:** Low (same hash), but an invalid state transition or extra Cardano fees.
- **Proposed solution:** Treat `NextAction` submit-in-progress states as submitted; mark the job completed from
  the stored result and keep monitoring.
- **Acceptance criteria:** Recovery test with `FundsLocked` + submit-requested next action does not call
  `complete_payment` again.

### BL-32 Recovery does not classify the V2 states `WithdrawAuthorized` / `RefundAuthorized`
- **Phase:** 1 · **Severity:** Low · **Effort:** S · **Status:** Open (found in iteration 4)
- **Evidence:** Payment Service `prisma/schema.prisma` enum `OnChainState` and the V2 datum decoder
  (`src/utils/converter/string-datum-convert/index.ts`, states 4 and 5) include `WithdrawAuthorized` and
  `RefundAuthorized`; `app/job_store.py` `_RESULT_ON_CHAIN` / `_REFUNDED` know neither, so a running job in
  one of them is resumed (monitoring re-attached; the SDK only runs on `FundsLocked`, so it would sit `running`
  until the result deadline instead of being closed).
- **Root cause:** State sets written for the V1 contract.
- **Impact:** A job stays `running` for hours after a restart instead of completed/failed; no extra charge.
- **Proposed solution:** Confirm from the contract what each state implies about a submitted result, then add
  them to the matching set.
- **Acceptance criteria:** Recovery test per state with the expected final job status.

### BL-04 Unconfirmed domains are scored as "company confirmed by domain"
- **Phase:** 1 · **Severity:** Medium · **Effort:** S · **Status:** Fixed in code (iteration 5, 2026-11-02; not deployed)
- **Resolution (iteration 5):** `app/router.py` `find_leads` passes `"domain"` only when `resolved.verified`,
  otherwise `"given domain"` (Apollo step and routed domain step). `app/scoring.py` `COMPANY_POINTS` /
  `COMPANY_REASONS`: confirmed domain 10 points "company confirmed by domain"; given-but-unconfirmed domain 6
  points "company matched by name (domain given but not confirmed)" (the compliance filter still required a
  name match or a link on the company's own site); name 6 "company matched by name"; none 0 "company not
  confirmed". `app/verify.py` keeps inferring "current" from Apollo's domain search for both domain kinds;
  "employer confirmed" was already gated on a confirmed domain. Tests in `tests/test_quality.py`: evidence
  table (4 cases), router with confirmed vs. unconfirmed domain (evidence, points, reason, employment check),
  routed domain search on an unconfirmed domain. Effect: an unconfirmed-domain lead loses 4 points; a lead at
  the High boundary (70–73) can drop to Medium.
- **Evidence:** `app/router.py:124,154` pass evidence `"domain"` whenever a domain was searched, also when
  `resolved.verified` is false; `app/scoring.py:80-82` then adds 10 points and the reason.
- **Root cause:** Evidence reflects the search mode, not whether the domain was confirmed.
- **Impact:** Wrong explanation and +10 points on unconfirmed matches.
- **Proposed solution:** Pass `"domain"` only for confirmed domains, otherwise `"given domain"` with fewer points.
- **Acceptance criteria:** Unit test: unconfirmed domain → no "confirmed by domain" reason.

### BL-05 A failed domain probe is cached for 24 hours
- **Phase:** 1 · **Severity:** Medium · **Effort:** S · **Status:** Fixed in code (iteration 6, 2026-11-02; not deployed)
- **Resolution (iteration 6):** `app/resolver.py` `resolve_company` records whether a probe raised; such a
  resolution is returned for the current job but not cached, so the next job probes again. Confirmed and
  genuine not-found results are still cached for 24 h (probes are free, but they add latency to each job). The
  note says "the data provider did not answer" instead of "could not confirm". No short negative TTL was added
  (not needed for the hiccup case; can follow if stale not-found results show up). Tests in
  `tests/test_quality.py`: failure then success with a given domain (second call probes and verifies, third is
  cached), failure without a domain (not cached), genuine not-found (cached). The two failure tests and the note
  assertion fail on the previous code.
- **Evidence:** `app/resolver.py:73-77` (`break` on exception) then `app/resolver.py:94` caches the unverified
  result for `CACHE_TTL_S` = 24 h.
- **Root cause:** Errors and genuine "not found" are cached alike.
- **Impact:** One treg hiccup degrades every job for that company for a day (name-search fallback, paid search).
- **Proposed solution:** Do not cache when a probe raised; optionally a short TTL for negative results.
- **Acceptance criteria:** Test: probe raises → next call probes again.

### BL-06 Name matching accepts look-alike companies
- **Phase:** 1 · **Severity:** Medium · **Effort:** S · **Status:** Fixed in code (iteration 7, 2026-11-02; not deployed)
- **Resolution (iteration 7):** `app/compliance.py` `company_matches` keeps the domain-link path and otherwise
  delegates to `same_company`, which compares core name tokens in order (`_core_name_tokens`: legal/generic
  suffixes removed, plus match-only words `global`, `international`, `hq`, `io`, `com`, `xyz`, `org`): equal
  token sets, or equal with spaces removed ("Chain Link" = "Chainlink"; the old sorted squash also made
  "Link Chain" equal). Subset matching is gone. The resolver and the enrichment same-company check use the same
  function, so all three agree. Tests in `tests/test_compliance.py`: 7 legitimate variants accepted, 6
  look-alikes rejected ("Link Metal", "Chainlink Plumbing", "Uniswap Ventures", "Link Chain", …), and
  `filter_people` dropping a look-alike while keeping one with a link on the requested domain; 6 of these fail
  on the previous code. Trade-off: a provider spelling with an extra distinctive word ("Kraken Digital Asset
  Exchange" for "Kraken") is now dropped in name-search mode; domain-mode leads are unaffected.
- **Evidence:** `app/compliance.py:216-218`: wanted tokens ⊆ lead tokens, so "Link" matches "Link Metal" and
  "Chainlink" matches "Chainlink Plumbing" (the stricter `same_company` is only used by the resolver).
- **Impact:** Wrong-company leads in name-search mode (no confirmed domain).
- **Proposed solution:** Use strict equality of core tokens unless the extra tokens are known suffixes; keep the
  domain path unchanged.
- **Acceptance criteria:** Tests for look-alikes rejected and legitimate variants ("Chainlink Labs") accepted.

### BL-07 Role matching only understands BD, partnerships and recruiting
- **Phase:** 1 · **Severity:** Medium · **Effort:** M · **Status:** Open
- **Evidence:** `app/compliance.py:227-266`: synonyms only for those families; "CTO" does not match "Chief
  Technology Officer"; "commercial" in the BD list matches "Commercial Counsel".
- **Impact:** Empty or wrong results for other common buyer roles (engineering, marketing, DevRel, C-level).
- **Proposed solution:** Small, tested synonym table for the most common role families plus abbreviation
  expansion; drop ambiguous terms.
- **Acceptance criteria:** Table-driven tests per role family, including negative cases.

### BL-10 Gemini call has no timeout
- **Phase:** 1 · **Severity:** Medium · **Effort:** S · **Status:** Fixed in code (iteration 8, 2026-11-02; not deployed)
- **Resolution (iteration 8):** `app/drafts.py` `write_outreach` wraps `generate_content` in
  `asyncio.wait_for(..., timeout_s)`; `Settings.gemini_timeout_s` (`GEMINI_TIMEOUT_S`, default 45 s) is passed
  from `app/agent.py`. On timeout the pending call is cancelled and every lead gets template drafts
  (`llm_used = false`). The fallback now also catches transport errors (e.g. `httpx.ConnectError`), which before
  escaped the narrow `except` and failed the paid job; `BaseException`s (cancellation) still propagate. Log lines
  name the model (and the API status code). Tests: `tests/test_drafts.py` (never-returning client → templates
  within the bound and the call is cancelled; connection error → templates; default and env setting),
  `tests/test_agent.py` (end-to-end job with a hanging client delivers template drafts and the compliance
  footer). The end-to-end test fails on the previous code. The bound covers the whole call, including any SDK retries.
- **Evidence:** `app/drafts.py:150-161` sets no timeout; google-genai defaults to none
  (`google/genai/_api_client.py`, `timeout_in_seconds = None`).
- **Impact:** A hanging model call keeps a paid job `running` until the result deadline passes.
- **Proposed solution:** Bound the call (e.g. 45 s via `HttpOptions` or `asyncio.wait_for`) and fall back to
  templates.
- **Acceptance criteria:** Test with a never-returning fake client finishes with template drafts.

### BL-11 The Pro job's drafts fell back to templates
- **Phase:** 1 · **Severity:** Medium · **Effort:** S · **Status:** Open (cause unknown)
- **Evidence:** `job_costs` row for the Coinbase Pro job: `llm_used = false`; its 10 drafts are the template.
  Container logs from that time are gone (containers restarted 2026-11-01 21:53 UTC).
- **Impact:** "Personalized" drafts not delivered on that paid job.
- **Proposed solution:** Log the exception class and model id on fallback (already partly), add a counter to
  `/metrics`, run one standalone draft call with the deployed model (`gemini-3.5-flash-lite`) to confirm it works.
- **Acceptance criteria:** Fallback rate visible in `/metrics`; a confirmed working model on the deployment.

### BL-13 Spend cap can be overrun and undercounted
- **Phase:** 1 · **Severity:** Medium · **Effort:** S · **Status:** Fixed in code (iteration 9, 2026-11-02; not deployed)
- **Resolution (iteration 9):**
  - *Reservation:* before its first treg call a job reserves its worst case in the ledger
    (`app/agent.py` `_spend_ceiling`): every domain probe and the Apollo search at their $0.01 max cost,
    `TREG_JOB_BUDGET_USD`, and (Pro, enrichment on) `ENRICH_BUDGET_USD`; $0.20 for a standard job with a domain.
    `ledger.reserve` grants `min(want, cap − today's settled spend − open reservations)`. On PostgreSQL this
    runs in one transaction under `pg_advisory_xact_lock`, so the standard and Pro containers cannot both
    read the same remainder (`app/ledger.py`, table `spend_reservations`).
  - *Metering:* every treg call (probe, Apollo search, routed search, enrichment) is sent only if what is left
    of the grant covers its max cost (`app/spend.py` `JobBudget`), and its cost is deducted. Calls within a
    job run one after another, so a free call gives its share back to the next. `TREG_JOB_BUDGET_USD` still
    caps the routed calls on their own.
  - *Settlement:* `ledger.settle` writes the job's `job_costs` row and deletes the reservation in one
    transaction.
  - *Cost headers:* a missing or invalid `X-Treg-Cost-Micro` (empty, fractional, negative, garbage) counts
    at the max cost sent for that call, as do error responses without one and transport errors. `"0"` is
    zero. Assumed costs are logged and counted (`cost_assumed_calls` per job, in `/admin/stats` and
    `/metrics` as `bdlf_cost_assumed_calls_7d`); open reservations show as `reserved_today_usd`.
  - *Crash:* an unsettled reservation counts until the end of its UTC day. A retry with the same job tag
    reuses it (topped up to the ceiling if the cap allows, never doubled).
  - *Ledger failure:* an unreadable ledger grants 0, so no treg call is made. A failed settle is logged and
    never fails the paid job.
  - *Not touched:* payment verification, escrow, refunds and the job state machine.
- **Behaviour change:** with the cap used up a job makes no treg call at all (Apollo included), so it ends
  `failed` (refundable) instead of delivering an empty Apollo-only result. With little left, the cheaper calls
  still run within it (see BL-33).
- **Tests:** `tests/test_spend.py`.
  - *Concurrency:* two concurrent jobs with cap = one job budget (one routed call, spend ≤ cap); jobs that fit
    still run in parallel.
  - *PostgreSQL:* 8 parallel reservations from two ledgers; `reserve` blocks behind another agent's lock and
    then sees its hold; settle is atomic; a previous day's hold expires; the schema upgrade is idempotent.
  - *Cost headers:* parsing per header value; failed calls carry their cost; Apollo and probe costs are
    recorded.
  - *Crash and retry:* a crash keeps the hold; the retry reuses it while another job is refused.
  - *Reserves nothing:* a cached re-run and an invalid input.
  - *Ledger failures:* settle failure.
  - *Pro:* enrichment is limited to the grant's rest.
  - *Checks:* the concurrency, reuse and crash tests fail with reservations disabled, and the PostgreSQL lock
    test fails without the advisory lock.
- **Still to verify on deployment:** whether treg sends `X-Treg-Cost-Micro: 0` on free Apollo calls. If not,
  each counts $0.01 and `cost_assumed_calls` shows it.
- **Evidence:** `app/agent.py:206-217` reads today's spend before the job and records it only at the end
  (`app/agent.py:185`), so concurrent jobs each see the full remainder; `app/treg_client.py:170` and
  `app/router.py:151` count a missing `X-Treg-Cost-Micro` header as $0.
- **Impact:** Daily cap exceeded by up to (concurrent jobs × job budget); spend invisible if treg omits the header.
- **Proposed solution:** Reserve the job budget in the ledger at start and settle at the end; treat a missing
  cost header as the call's max cost.
- **Acceptance criteria:** Concurrency test with two jobs and a cap of one job budget; test for missing header.

### BL-33 A job sold with the daily cap used up fails after payment
- **Phase:** 1 · **Severity:** Low · **Effort:** S · **Status:** Open (found in iteration 9)
- **Evidence:** since BL-13 a job whose reservation is 0 makes no treg call and fails
  ("daily spend budget used up"); `/availability` still says `available`.
- **Impact:** The buyer pays, gets no result and must wait for the refund window. Rare while the cap
  (default $1.00/day) is far above real spend.
- **Proposed solution:** Report `unavailable` and refuse `/start_job` (as the BL-01 sale guard does) while
  today's remainder is below one job's spend ceiling; re-check per request with a short cache.
- **Acceptance criteria:** With the cap used up, `/availability` is unavailable and `/start_job` returns 503;
  both recover when the UTC day rolls over or spend settles below the cap.

### BL-14 Result cache key ignores the payment; cached forever
- **Phase:** 1 · **Severity:** Low · **Effort:** S · **Status:** Open
- **Evidence:** `app/agent.py:137-139` (tag = purchaser identifier + input), `app/ledger.py:108-111`
  (`ON CONFLICT DO NOTHING`, no expiry). The same tag is the feedback reference (`app/agent.py:267`).
- **Impact:** A new purchase that reuses an identifier and input gets the old result (stale data, someone
  else's drafts if two buyers collide). Sokosumi generates fresh identifiers, so rare in practice.
- **Proposed solution:** Include the blockchain identifier in the tag (available from the job record) or bind
  a cache hit to the same payment id.
- **Acceptance criteria:** Two payments with the same identifier and input produce independent jobs; a re-run of
  one payment still returns its stored result.

## Phase 2 — Make it valuable

### BL-20 LinkedIn coverage 0 %, most names masked
- **Severity:** Critical · **Effort:** M · **Status:** Open (needs approval for paid calls)
- **Evidence:** baseline above; `app/treg_client.py:85-86` (Apollo free search: masked, no links).
- **Root cause:** Free search is the primary source; enrichment is off; the routed fallback rarely returns links.
- **Proposed solution:** Measure cost per qualified lead with enrichment on a fixed sample (owner-approved
  budget), compare with alternative providers, then decide defaults.
- **Acceptance criteria:** Measured LinkedIn coverage and cost per qualified lead on ≥ 5 companies, recorded here.

### BL-21 Single data vendor (treg)
- **Severity:** Medium · **Effort:** L · **Status:** Open
- **Evidence:** all calls in `app/treg_client.py`; one token, one balance, one terms document.
- **Proposed solution:** Provider interface already implied by `router.py`; add a second source only after
  BL-20 shows which gap it must fill.
- **Acceptance criteria:** Router works with treg down for at least one provider path (mocked test).

### BL-22 No real customers or feedback
- **Severity:** High (business) · **Effort:** S (checklist) · **Status:** Open (human action)
- **Evidence:** 8 jobs, all owner/acceptance purchases; `feedback` table 0 rows.
- **Proposed solution:** No new software. A one-page validation plan: 3–5 BD/recruiting users, 3 target
  companies each, fixed questions (would you contact them, would you pay 0.27 / 1.05), results recorded here.
- **Acceptance criteria:** Plan written; results from ≥ 3 users recorded.

### BL-18 One buyer can hide people from every buyer
- **Severity:** Medium · **Effort:** S · **Status:** Open
- **Evidence:** `app/feedback.py:61-65,130-135` (one report suppresses globally); rate limit per client IP
  (`app/feedback.py:169-179`) — behind the InstaBox proxy all clients may share an address.
- **Proposed solution:** Suppress only for the reporting buyer, or globally after ≥ 2 independent reports.
- **Acceptance criteria:** Test: one report does not suppress for other buyers.

### BL-19 Masked-name dedup can merge different people
- **Severity:** Low · **Effort:** S · **Status:** Open
- **Evidence:** `app/router.py:49-55` key = first name + first/last letter of surname + company.
- **Proposed solution:** Prefer provider ids when present; only merge masked with full names on id or title match.

### BL-23 Outreach quality is not measured
- **Severity:** Medium · **Effort:** M · **Status:** Open
- **Evidence:** `tests/test_drafts.py` checks structure and scrubbing, not content; BL-11 fallback went unnoticed.
- **Proposed solution:** Small fixed evaluation set (5 leads × 3 tones) with checks for length, first name,
  pitch mention and no invented facts; run manually before model changes.

## Phase 3 — Make it production-ready

### BL-24 Monitoring and alerting
- **Severity:** Medium · **Effort:** M · **Status:** Open
- **Evidence:** `/metrics` and `/admin/stats` exist (`app/server.py:141-161`) but nothing scrapes them;
  payment counters are in memory and reset on restart (`app/server.py:63`); `LOG_FORMAT` unset; compose sets
  no log rotation (`deploy/docker-compose.yml`).
- **Proposed solution:** External uptime check on `/availability`, a daily stats check, persisted counters,
  Docker log rotation, a short runbook.

### BL-25 Backups and recovery drills
- **Severity:** Medium · **Effort:** S · **Status:** In Progress
- **Evidence:** compose `backup` service deployed 2026-11-01 23:50 UTC (daily, keeps 7); restore drill
  2026-11-02 matched live row counts; committed in `e3e6749`; off-host copy is a manual `scp`.
- **Proposed solution:** Schedule or document the off-host copy; repeat the drill monthly.

### BL-26 Single server
- **Severity:** Medium · **Effort:** L · **Status:** Open
- **Evidence:** one InstaBox machine runs Postgres, Payment Service and both agents; it was suspended once.

### BL-27 Mainnet: 2 preflight FAILs, Preprod guard
- **Severity:** High for launch · **Effort:** L · **Status:** Blocked (owner)
- **Evidence:** preflight `--target Mainnet`: "agent registered on Mainnet: not found", "code guard"
  (`scripts/preflight.py:118`, `main.py:121-122`). Both are intended until the owner launches.
- **Proposed solution:** Follow `docs/MAINNET_READINESS.md` / `MAINNET_CHECKLIST.md`; do not lift the guard
  before Phase 1 is fixed.

### BL-28 Legal and privacy
- **Severity:** High for launch · **Effort:** M · **Status:** Open (human/legal)
- **Evidence:** no privacy policy or terms in the repo; preflight WARN "legal"; treg upstream terms risk
  accepted for Preprod only (HANDOFF).

### BL-15 `/start_job` is open and unthrottled
- **Severity:** Medium · **Effort:** S · **Status:** Open
- **Evidence:** masumi SDK `server.py:186-226`: every call creates a Payment Service record and a monitoring
  task that polls every 10 s until the 12 h pay deadline; no rate limit in front.
- **Proposed solution:** Per-IP rate limit on `/start_job` and a cap on concurrently monitored unpaid jobs.

### BL-16 Error details leak from `/start_job`
- **Severity:** Low · **Effort:** S · **Status:** Open
- **Evidence:** SDK `server.py:250-255` returns `str(e)`; `app/masumi_compat.py:65` puts up to 500 chars of the
  Payment Service response into that message.

### BL-17 Interactive API docs are public
- **Severity:** Low · **Effort:** S · **Status:** Open
- **Evidence:** `GET /docs` and `/openapi.json` return 200 on both public URLs (2026-11-02).

### BL-29 No CI
- **Severity:** Medium · **Effort:** S · **Status:** Open
- **Evidence:** no `.github/` or other CI config; tests run only by hand.

### BL-30 Deployment drift
- **Severity:** Medium · **Effort:** S · **Status:** Open
- **Evidence:** the server copy is not a git checkout (files copied by hand). The four uncommitted files were
  committed on 2026-11-02 (`e3e6749`), so the repo matches what is deployed for `deploy/`; the app code on the
  server is still the image built from `c6d8eba`-era sources.
- **Proposed solution:** Deploy from a tagged commit; record the deployed commit in `/admin/stats`.

### BL-31 Coupling to masumi SDK internals
- **Severity:** Low · **Effort:** M · **Status:** Open
- **Evidence:** `app/server.py`, `app/job_store.py:174` use `_handle_payment_confirmed`, `_payment_instances`;
  `masumi==1.2.0` pinned; FastAPI `on_event` deprecation warnings in the test run.

## Iteration log

| # | Date | Issue | Result |
|---|---|---|---|
| 1 | 2026-11-02 | BL-01 | Fixed in code (`f255fc7`); 112 tests pass (incl. PostgreSQL). Not deployed. |
| 2 | 2026-11-02 | BL-02, BL-03 | Fixed in code (`03cdbdd`); 120 tests pass (incl. PostgreSQL). Not deployed. |
| 3 | 2026-11-02 | BL-08 | Fixed in code (`170f99e`); 140 tests pass (incl. 5 PostgreSQL on a throwaway cluster). Not deployed. |
| 4 | 2026-11-02 | BL-09 | Fixed in code (`debd1e3`); 147 tests pass (incl. PostgreSQL on a throwaway cluster). Not deployed. New: BL-32. |
| 5 | 2026-11-02 | BL-04 | Fixed in code (`c6b0c21`); 154 tests pass (incl. 5 PostgreSQL on a throwaway cluster). Not deployed. BL-05 kept separate (cache logic, not scoring). |
| 6 | 2026-11-02 | BL-05 | Fixed in code (`b39e9f0`); 157 tests pass (incl. PostgreSQL on a throwaway cluster). Not deployed. |
| 7 | 2026-11-02 | BL-06 | Fixed in code (`4d02fca`); 171 tests pass (incl. 5 PostgreSQL on a throwaway cluster). Not deployed. |
| 8 | 2026-11-02 | BL-10 | Fixed in code (`c464d8d`); 175 tests pass (incl. PostgreSQL on a throwaway cluster). Not deployed. |
| 9 | 2026-11-02 | BL-13 | Fixed in code; 209 tests pass (incl. 8 PostgreSQL on a throwaway cluster). Not deployed. New: BL-33. |

## Recommended next iteration (Step C)

BL-33: since BL-13 a job arriving with the daily cap used up makes no treg call and fails, so the buyer pays
and waits for a refund. Reporting `unavailable` while today's remainder is below one job's ceiling would stop
such sales before payment (the BL-01 sale guard is the pattern; no payment code involved). After that:
BL-07 (role synonyms), BL-11 (fallback counter in `/metrics`; the live model check needs the server). BL-32
is a short follow-up once the V2 state semantics are confirmed.
