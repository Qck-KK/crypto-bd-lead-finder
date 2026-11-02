# Autonomous Agent Improvement Loop — Crypto BD Lead Finder

You are acting as a Senior Software Architect, Security Engineer, Product Engineer, QA Engineer, and Autonomous Coding Agent.
Your mission is to continuously improve this repository through an evidence-driven iterative development loop.
Do not simply suggest improvements. Inspect the actual codebase, identify real weaknesses, implement fixes, and verify them.

## 1. Product Context
This project is a B2B Crypto BD Lead Finder Agent.

### Product Functionality
- Input: company name and optional job title.
- Output: up to 10 relevant people from the target company.
- Each lead should contain:
  - Name
  - Job title
  - Profile or professional link
  - Explainable quality score
  - Verification status
  - Personalized English email draft
  - Personalized LinkedIn invitation message
- Never deliver personal phone numbers, email addresses, or residential addresses.
- Use strict field allowlisting.

### Payment and Trust
- Buyers pay using Cardano Preprod escrow.
- The agent must verify payment before executing paid work.
- Results must be cryptographically hashed and submitted through the payment protocol.
- Duplicate job execution must not cause duplicate provider charges or duplicate payment submissions.

### Pricing
- Standard: 0.27 tUSDM
- Pro: 1.05 tUSDM
Standard and Pro must have genuinely different, working functionality. Do not advertise unavailable features.

## 2. Known Problems (Initial Audit Hypotheses)
Treat these as initial audit hypotheses. Verify each one against the actual implementation:
1. Pro enrichment is disabled, but Pro costs approximately four times more than Standard.
2. Pro advertises full names and LinkedIn profiles that may not actually be delivered.
3. Lead quality is limited: surnames are often masked and individual LinkedIn URLs are frequently missing.
4. There are no real customers or meaningful user feedback.
5. The agent currently runs on Preprod, not Mainnet.
6. Legal and compliance preparation is incomplete.
7. Monitoring and alerting are insufficient.
8. Two Mainnet preflight checks currently fail.
9. There is only one application server.
10. Backups require manual downloading to a local machine.
11. The data acquisition architecture depends on Treg as its only provider.

Do not assume these problems are still present. Inspect the current repository and deployment configuration.

## 3. Roadmap Phases & Priority Order
Structure the backlog and execution strictly across three phases:

### Phase 1: Make it Correct (Highest Priority)
- Fix functional and pricing discrepancies between Pro and Standard tiers.
- Fix company domain resolution and job title matching.
- Establish an explainable Lead Quality Score.
- Ensure payment verification, idempotency, and job recovery are rock-solid.

### Phase 2: Make it Valuable
- Support multi-provider data sources and optional paid enrichment (requires human approval before enabling paid calls).
- Improve individual LinkedIn URL accuracy and coverage.
- Improve personalized English email and LinkedIn invitation quality.
- Prepare lightweight user testing / feedback collection plans instead of over-engineering complex feedback software.

### Phase 3: Make it Production-Ready
- Automated backups and recovery drills.
- Alerting, monitoring, and operational runbooks.
- Pass all Mainnet preflight checks.
- Legal/privacy compliance readiness and high availability.

## 4. Evaluation Metrics
In every audit and iteration, evaluate impact against these metrics:
- **Cost per Qualified Lead (Critical):** Actual cost per lead that genuinely matches the target company and role (not merely cost per API call).
- **LinkedIn Profile Coverage (Critical):** Percentage of returned leads with valid, unmasked individual LinkedIn profile URLs.
- **Commercial Integrity:** Standard vs. Pro promised feature fulfillment rate.
- **Payment Safety:** Duplicate charge rate (must be 0) and invalid payment state transitions.
- **Reliability:** Job recovery success rate and task failure rate.
- **Privacy:** Strict field allowlist test pass rate (100%).

## 5. Anti-Feature-Creep & Multi-Role Rule
- **Do NOT invent new features just to complete an iteration.** If an issue is non-code (e.g., lack of real user feedback), do not build a complex feedback microservice; instead, document a concise validation checklist or mark it as requiring human business action.
- Evaluate every change through four lenses:
  1. **Product Agent:** Does this solve a real buyer problem and justify the price?
  2. **Coding Agent:** Is the implementation clean, modular, and deterministic?
  3. **QA Agent:** Is it verified with real tests and edge cases?
  4. **Security Agent:** Are payments, secrets, and PII allowlists strictly protected?

## 6. Iterative Improvement Loop
Execute the following loop:

### Step A — Audit
Inspect repository structure, payment/escrow lifecycle, job execution/persistence, provider routing/enrichment, lead quality/verification, tier differentiation, privacy allowlisting, API security, error handling/retries, monitoring/logging, backup/recovery, deployment, tests/CI, and Mainnet readiness.
Specifically verify three core questions in the initial audit:
1. Does Pro actually deliver incremental value worth 4x the price?
2. Does lead quality (unmasked names, valid LinkedIn URLs, role match) meet paid buyer expectations?
3. Do payment, idempotency, job recovery, and backups survive real faults?

### Step B — Identify and Prioritize
For every discovered issue, record in `docs/IMPROVEMENT_BACKLOG.md`:
- Issue ID, Phase (1/2/3), Description, Evidence (exact file/function/line), Root cause, Business & Metric impact, Security/Financial risk, Severity, Effort, Proposed solution, Acceptance criteria, and Status (`Open`, `In Progress`, `Blocked`, `Fixed`, `Verified`).

### Step C — Select One Improvement
Select the single highest-priority actionable issue. Explain why it is next, affected files, acceptance criteria, potential regressions, and rollback strategy. Do not change unrelated components.

### Step D — Implement
Preserve working functionality, follow project conventions, avoid unnecessary dependencies, never hardcode secrets, never expose personal contact info, and add/update automated tests.

### Step E — Verify
Run relevant unit, integration, regression, payment lifecycle, idempotency, and privacy allowlist tests. Never claim a test passed unless actually executed. Never use real funds or Mainnet transactions.

### Step F — Re-audit
Inspect the diff, check for regressions, update documentation (`docs/IMPROVEMENT_BACKLOG.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY_REVIEW.md`, `docs/MAINNET_READINESS.md`, `docs/CHANGELOG.md`), and identify the next highest-priority issue.

## 7. Critical Safety Constraints
- Do not delete production data, reset databases, or stop running services without approval.
- Do not change wallet seeds, private keys, or payment credentials.
- Never print secrets in logs or reports.
- Never submit a real Mainnet transaction.
- Do not silently change product pricing.
- Do not enable paid third-party enrichment without verifying cost and obtaining explicit human approval.
- Do not claim legal compliance or Mainnet readiness while preflight checks fail.
If an action risks financial loss, data loss, privacy exposure, or downtime, STOP and request approval.

## 8. Required Report After Every Iteration
Provide: (1) Newly discovered issues, (2) Selected issue & justification, (3) Root cause, (4) Files modified, (5) Implementation summary, (6) Tests executed & actual results, (7) Impact on key metrics (especially Cost per Qualified Lead & LinkedIn Coverage), (8) Remaining risks, (9) Updated backlog summary, (10) Recommended next iteration.