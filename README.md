# Crypto BD Lead Finder

A paid agent for [Sokosumi](https://preprod.sokosumi.com) on the Masumi Network (Cardano **Preprod**).

You give it a company name and, optionally, a target role (recruiter, BD, partnerships…). It finds up to
10 people at that company through treg's people search and writes a short English outreach draft for each.
The buyer pays into Masumi escrow first; the job runs only after the payment is confirmed on-chain.

**Data source: treg.to. For B2B professional outreach only.**

## What it returns

A Markdown report followed by the same data as JSON. The report has a summary (resolved company
domain, number of leads by quality), an overview table, usage guidance, one section per lead with its
draft, and search notes. Each lead has exactly these fields:

| Field | Content |
|---|---|
| `name` | Full name (some providers mask the last name, e.g. `Jane S***h`) |
| `title` | Job title |
| `company` | Company |
| `profile_url` | Public LinkedIn profile (`linkedin.com/in/…`) or the company's own website |
| `profile_type` | `linkedin`, `company_website` (no individual profile found) or `none`; the report says "company site only" |
| `source` | `treg.to (<provider>)` |
| `quality_score`, `quality`, `match_reasons`, `score_breakdown` | 0–100 score, High/Medium/Low, why, and the points per component (see below) |
| `outreach_draft` | 50–90 word English draft |

The report ends with the data source and the B2B-only notice. It never contains our costs.

## Reliability

| Area | What happens |
|---|---|
| Persistence | Jobs (`agent_jobs`, `agent_jobs_pro`), results, cost ledger and feedback live in PostgreSQL. The compose `backup` service runs `deploy/backup.sh` on start and every 24 h: both databases, 7 dumps each, in `BACKUP_HOST_DIR` (default `/data/workspace/backups`). Copy them off the server; the Payment Service dump holds wallet keys encrypted with `ENCRYPTION_KEY`, so never store it next to the key. |
| Idempotency | The result is stored under the job tag (purchaser identifier + input) before the SDK submits it on-chain. A re-run returns it unchanged: same on-chain hash, no provider paid twice. treg calls also carry idempotency keys. |
| Recovery | On start: jobs awaiting payment get monitoring back; running jobs whose result is already on-chain, or whose submission the Payment Service is already processing (`NextAction` `SubmitResultRequested`/`Initiated`), are marked completed from the stored result (never resubmitted); refunded ones fail; locked ones run again. |
| Payment acceptance | Work starts only on `FundsLocked`, which the Payment Service sets only after matching the on-chain escrow against the requested amount and token (else `FundsOrDatumInvalid`). Before any work the record is checked against this job: same payment id, this agent, this input hash (all must be present), expected token, at least the registered price. Otherwise the job fails and nothing is delivered. If the status cannot be read (3 tries), the job fails closed. |
| Spend control | `DAILY_SPEND_CAP_USD` (default 1.0) caps treg spend per UTC day across all agents on the database (one treg balance). Each job reserves its worst case (domain probes and Apollo search at $0.01 each, `TREG_JOB_BUDGET_USD`, Pro `ENRICH_BUDGET_USD`) before its first call; concurrent jobs and both agents share the cap through that reservation. Every treg call, including the free Apollo ones, must fit in the job's grant, and its cost is deducted; a missing or invalid cost header counts as the call's max cost. A crashed job's reservation counts until the end of the UTC day and is reused by its retry. If the ledger cannot be read, or the cap is used up, no treg call is made and the job fails (refundable). |
| Monitoring | `/availability` reports `unavailable` when the Payment Service or database is down (the registry then hides the agent). `/admin/stats` (JSON, ledger per agent) and `/metrics` (Prometheus text) need `x-admin-token`. Logs carry the job tag, `LOG_FORMAT=json` is available, and SDK lines with results or inputs are redacted. |
| Launch checks | `scripts/preflight.py` (read-only, `--target Mainnet` for a readiness review), `scripts/acceptance.py` (paid end-to-end test, Preprod only), `MAINNET_CHECKLIST.md`. |

## Tiers, outreach, verification and feedback

Two agents run from the same code and are registered separately (Masumi prices are fixed per registration):

| | Crypto BD Lead Finder | Crypto BD Lead Finder Pro (`AGENT_TIER=pro`) |
|---|---|---|
| Price (Preprod) | 0.27 tUSDM | 1.05 tUSDM (worst-case cost incl. enrichment × 3) |
| Leads | up to 10, scored and verified | same, plus **enrichment**: full names and LinkedIn profiles |
| Outreach | email subject, email body, LinkedIn note | same |

- **Enrichment** (`app/enrich.py`, Pro): Apollo bulk enrichment by Apollo id for leads that are masked or have no
  LinkedIn profile, best first, ≤ `ENRICH_BUDGET_USD` per job (~$0.026 per person). Personal emails and phone
  numbers are not requested; anything else the provider returns (work email, address, socials) is removed by
  the allowlist. A record is used only if it is the same first name at the same company. On Preprod buyers pay
  with test tokens while enrichment costs real treg balance, so `ENRICH_BUDGET_USD=0` (off) is the default.
- **Pro is only sold with enrichment on.** Without a budget for at least one match, Pro would deliver the
  standard results at the Pro price, so it reports `unavailable` on `/availability` (the registry hides it),
  answers `/start_job` with 503, and preflight fails. Jobs already paid still run. The input form and the
  report describe enrichment as it is: promised only when on, and each Pro report says what it did (off,
  `n of m` completed, or why it was skipped). `PRO_ALLOW_WITHOUT_ENRICHMENT=true` keeps selling an
  unenriched Pro, for test deployments only.
- **Personalized outreach** (`app/drafts.py`): per lead a subject (≤ 60 chars), a 60–110 word email and a
  LinkedIn connection note (≤ 280 chars), from the lead's title, seniority and region plus the buyer's offer,
  name, company and tone (`friendly`, `formal`, `concise`). One Gemini call per job, bounded by
  `GEMINI_TIMEOUT_S` (default 45 s); templates if it fails or times out.
- **Verification** (`app/verify.py`): no extra requests. Checks employer confirmed by domain, current role,
  record refreshed within 365 days, found by 2+ sources, LinkedIn present. Verified (+5 score), Likely, or
  Unverified (−5).
- **Buyer feedback** (`app/feedback.py`): every report links to `/feedback?ref=…&t=<HMAC>` (needs
  `FEEDBACK_SECRET` and `AGENT_PUBLIC_URL`). Buyers rate 1–5 and mark leads useful / not useful / left the
  company / wrong person. People marked gone or wrong are never delivered again. Stored: person hashes and
  titles, no names. `GET /feedback/summary` with header `x-admin-token: $FEEDBACK_ADMIN_TOKEN`.

## Search and quality

| Step | Module | What it does |
|---|---|---|
| Domain resolver | `app/resolver.py` | Given domain, its root (`careers.chain.link` → `chain.link`), then guesses from the name. Each is checked with a free Apollo probe and accepted only if the people there work at a company with the same name. Cached for 24 h, except when a probe failed (the next job probes again). |
| Provider router | `app/router.py` | Apollo people search (free) on the resolved domain. Paid routed treg search only if fewer than `MIN_LEADS_BEFORE_PAID` leads: by domain, then by name if the domain was not confirmed. Total spend per job ≤ `TREG_JOB_BUDGET_USD`. If every provider fails, the job fails (so the buyer can be refunded) instead of delivering nothing. |
| Compliance filter | `app/compliance.py` | Allowlisted fields only; wrong-company and off-role people dropped. |
| Quality score | `app/scoring.py` | Role fit (35), seniority (20), full name (15), LinkedIn (20) or company site (6), company evidence (10 for a domain the provider confirmed; 6 for a name match or a given domain that could not be confirmed). High ≥ 70, Medium ≥ 45; a lead with a masked name and no individual profile is never High. The report's summary counts full names and LinkedIn profiles, and "Employment check" (was "Verification") names what is checked. Leads below `MIN_LEAD_SCORE` (35) are not delivered. The same person from two providers is merged, keeping the better record. |

## Data compliance

`app/compliance.py` is an **allowlist**: only the fields above are copied out of a provider's row. Everything
else (work or personal emails, mobile and phone numbers, home or street addresses, social accounts, the raw
provider payload) is dropped, even when treg returns it. Email- and phone-like text is also scrubbed from
names, titles and the LLM drafts. URLs are kept only if they are a LinkedIn profile or on the company's own
domain. The maximum of 10 people is hard-coded (`MAX_PEOPLE` can only lower it).

Only name, title and company are sent to the LLM.

## Architecture

```
main.py              masumi SDK (create_masumi_app): /start_job /status /availability /input_schema /provide_input /demo
app/agent.py         job handler + MIP-003 input schema
app/resolver.py      company domain resolver
app/router.py        data provider router, per-job budget, merge
app/scoring.py       lead quality score
app/treg_client.py   treg calls: apollo.people.search and treg.people.search (cost cap, idempotency key)
app/compliance.py    allowlist filter, company/role match, 10-person cap
app/drafts.py        Gemini via google-genai (one call per job, template fallback)
app/report.py        Markdown + JSON result
app/pricing.py       cost estimate and Preprod price
```

Payment flow (handled by the masumi SDK): `/start_job` validates input, creates a payment request in your
Masumi Payment Service and returns `blockchainIdentifier`, `payByTime`, `submitResultTime`, `unlockTime`,
`externalDisputeUnlockTime`, `agentIdentifier`, `sellerVKey` and `input_hash`. The SDK polls the Payment
Service; when funds are locked in escrow it runs the job and submits the result hash with
`/payment/submit-result`. Funds unlock to the seller after `unlockTime` unless the buyer disputes.

## Cost and price

| Item | Per job |
|---|---|
| treg people search: 10 rows × ~$0.0089 | ~$0.089 |
| Gemini (`GEMINI_MODEL`, free tier) | $0 |
| **Cost** | **~$0.089** |
| **Preprod price (×3, rounded up)** | **0.27 tUSDM** |

In practice most jobs are answered by Apollo's free search ($0 on treg), so the estimate above is the
worst case of the routed fallback. treg bills that fallback per returned row and routes across providers;
misses at some providers are billed too.
`TREG_MAX_COST_USD` (default $0.15) is sent as `X-Treg-Route-Max-Cost`, so one job never spends more than
that on treg. Change the inputs with `COST_TREG_PER_ROW_USD`, `COST_LLM_PER_JOB_USD`, `PRICE_MARKUP`, or set
`PRICE_TUSDM` directly. The price is fixed on-chain at registration;
`python scripts/registration_values.py` prints it in the atomic units the registration form needs.

## Run locally

Python 3.12 in its own conda environment:

```powershell
conda create -n bd-lead-finder python=3.12 -y
conda activate bd-lead-finder
pip install -r requirements-dev.txt
copy .env.example .env      # then fill in the keys (see below)
```

```powershell
pytest                                  # all external APIs are mocked; no keys needed
python main.py --standalone --input "{\"company\": \"Chainlink Labs\", \"role\": \"partnerships\"}"
python main.py                          # API on http://localhost:8080 (needs the Payment Service)
```

`--standalone` runs one job without payments. It does call treg (costs ~$0.09) and Gemini.

### Keys

| Variable | Where to get it |
|---|---|
| `TREG_TOKEN` | [treg.to/app](https://treg.to/app): sign in, create an org token. New teams get $1 free credit. |
| `GEMINI_API_KEY` | [Google AI Studio → API keys](https://aistudio.google.com/apikey) |
| `GEMINI_MODEL` | Default `gemini-2.5-flash`; any Gemini model id works |
| `PAYMENT_API_KEY`, `SELLER_VKEY`, `AGENT_IDENTIFIER` | Your Masumi Payment Service admin (below) |

Gemini notes: the free tier has rate limits, and Google may use free-tier prompts to improve its products
(only name, title and company of public profiles are sent). The Gemini API is not available in every region.
If Gemini fails, the job still delivers with neutral template drafts.

### Docker

```bash
docker build -t crypto-bd-lead-finder .
docker run --env-file .env -p 8080:8080 crypto-bd-lead-finder
```

If the Payment Service runs on the host, set `PAYMENT_SERVICE_URL=http://host.docker.internal:3001/api/v1`.

### Jobs survive restarts

The masumi SDK keeps jobs in memory by default. With `AGENT_DATABASE_URL` set, jobs are stored in
PostgreSQL (`app/job_store.py`). On startup the agent re-attaches payment monitoring for jobs still
awaiting payment or running; a job whose escrow is locked runs again (treg's idempotency key prevents
paying twice for the same search), and jobs whose deadline passed while offline are marked failed.

## Deploy on a server (Docker Compose)

`deploy/docker-compose.yml` runs PostgreSQL, the Masumi Payment Service (0.29.0, as in the official
quickstart), the standard agent (`agent`, port 8080) and the Pro agent (`agent-pro`, port 8081) on one host,
all with `restart: unless-stopped`.

```bash
cd deploy
cp .env.example .env              # POSTGRES_PASSWORD, ENCRYPTION_KEY, ADMIN_KEY (openssl rand -hex 32), Blockfrost key
cp agent.env.example agent.env    # TREG_TOKEN, GEMINI_API_KEY; PAYMENT_API_KEY and SELLER_VKEY after seeding
cp agent.env.example agent-pro.env  # same keys, the Pro AGENT_IDENTIFIER and AGENT_PUBLIC_URL, ENRICH_BUDGET_USD=0
mkdir -p -m 700 /data/workspace/backups  # as the host user (uid 1000) that the backup service runs as
docker compose up -d --build
docker compose logs payment-setup  # the generated wallet mnemonics are printed here once: store them offline
```

Only the agent ports (8080 standard, 8081 Pro) should be reachable from the internet; put HTTPS in front of them. The
Payment Service binds to `127.0.0.1:3001`; reach the admin with
`ssh -L 3001:localhost:3001 <server>` and open `http://localhost:3001/admin`.

## Masumi Payment Service (Preprod)

You run your own Payment Service (the "Masumi Node"). It holds your wallets and talks to Cardano through
Blockfrost.

1. **Blockfrost key**: create a free *Cardano Preprod* project at [blockfrost.io](https://blockfrost.io).
2. **Install**, either way:
   - Docker: clone [masumi-services-dev-quickstart](https://github.com/masumi-network/masumi-services-dev-quickstart),
     `cp .env.example .env`, set `ADMIN_KEY` (15+ chars), `ENCRYPTION_KEY` (32+ chars) and
     `BLOCKFROST_API_KEY_PREPROD`, then `docker compose up -d`.
   - Manual: [masumi-payment-service](https://github.com/masumi-network/masumi-payment-service) needs
     Node 24 and pnpm 10 (`npx pnpm@10.30.2 …`) and PostgreSQL 15. Check out the latest release tag, fill `.env`
     (`DATABASE_URL`, `ENCRYPTION_KEY`, `ADMIN_KEY`, `BLOCKFROST_API_KEY_PREPROD`), then run
     `pnpm install`, `pnpm run prisma:migrate`, `pnpm run prisma:seed`, `pnpm run build`, `pnpm start`.
3. Open `http://localhost:3001/admin` and log in with `ADMIN_KEY`. **Store the generated wallet mnemonics
   offline**; never put them in this repo.
4. **Fund the Preprod wallets** with free test ADA: copy the *selling* and *purchase* wallet addresses from
   *Wallets* and request funds from the [Cardano testnet faucet](https://docs.cardano.org/cardano-testnets/tools/faucet)
   (network: Preprod) or the Masumi faucet linked from the Masumi docs. The purchase wallet also needs tUSDM to
   buy jobs.
5. Copy into this repo's `.env`:
   - `SELLER_VKEY`: *Wallets* → selling wallet → vKey
   - `PAYMENT_API_KEY`: *API Keys* → copy or create a key
   - `PAYMENT_SERVICE_URL=http://localhost:3001/api/v1`

## Register on Preprod and appear on Sokosumi

1. Start the agent (`python main.py`) and make it reachable over **public HTTPS** (for example a Cloudflare
   or ngrok tunnel to port 8080). Put the URL in `AGENT_PUBLIC_URL` and check `<url>/availability`.
2. Run `python scripts/registration_values.py`. It prints the name, description, API URL, tags and price.
3. In the admin: *AI Agents* → *+ Register AI Agent*. Paste the values. **Price in tUSDM**; Sokosumi lists
   only USDM/tUSDM-priced agents. Preprod tUSDM unit:
   `16a55b2a349361ff88c03788f93e1e966e5d689605d044fef722ddde0014df10745553444d`.
4. Registration mints an NFT and costs a little test ADA from the selling wallet. On Preprod it takes about
   5–15 minutes. Then copy the `agentIdentifier` from the *AI Agents* table into `AGENT_IDENTIFIER` and restart.
5. The agent then appears automatically at
   [preprod.sokosumi.com/agents](https://preprod.sokosumi.com/agents). (Mainnet listing needs a separate
   whitelist approval; this build refuses to start with `NETWORK` other than `Preprod`.)

To change the price later you deregister (burns the NFT) and register again.

### Payment Service 0.29+ (Web3CardanoV2)

masumi SDK 1.2.0 still creates V1-style payment requests. `app/masumi_compat.py` sends
`paymentSourceType: Web3CardanoV2` and `supportedPaymentSourceIndex` (env `PAYMENT_SOURCE_TYPE`,
`PAYMENT_SOURCE_INDEX`, default the first source). V2 registrations put the price in
`supportedPaymentSources[].pricing` (`{"pricingType": "Fixed", "fixed": [{"asset": <tUSDM unit>, "amount": "270000"}]}`),
not in `AgentPricing`, and the registry description is limited to 250 characters.

### Lead quality

Provider rows come in different shapes (flat, nested `profile`/`link`/`company`, or `currentPositions` lists);
`app/compliance.py` reads the allowlisted fields from all of them. Keyword-matching providers can return
people at look-alike companies, so a lead whose company does not match the requested one is dropped. A match
needs a link on the requested company's own domain or the same core name after removing legal and generic words
("Chainlink Labs" = "Chainlink" = "Chain Link"); extra words are not accepted ("Link" ≠ "Link Metal").

Search order:

1. **Apollo people search** (`apollo.people.search` on treg: free, no contact data). Tried with the domain
   the buyer entered, then with domains guessed from the company name (`chainlinklabs.com`, `chainlink.com`,
   `.io`…), because Apollo indexes a company under its primary domain. The role is sent as title phrases
   (with BD, partnerships and recruiting synonyms) and checked again locally; closest titles come first.
   Apollo's free search masks last names (`Jane S***h`) and has no profile links, so these leads point to
   the company website.
2. **Routed treg search** only if fewer than `MIN_LEADS_BEFORE_PAID` (default 5) leads reach `MIN_LEAD_SCORE`:
   first by domain, then by name if the domain was not confirmed. With a domain only `company_domain` is sent,
   so only providers that search by domain can answer; the role is matched locally.

**Enter the company domain** (the input form marks it as recommended). Without it the search relies on the
domain guesses and on name search, and results can be few or empty after the company filter.

## End-to-end test on Preprod

1. `POST <agent>/start_job` with `{"identifier_from_purchaser": "<14–26 hex chars>", "input_data": {"company": "…"}}`.
2. Create the purchase in your Payment Service (`POST /api/v1/purchase`, Swagger at `http://localhost:3001/docs`)
   with the values from step 1, plus `paymentSourceType: "Web3CardanoV2"` and `supportedPaymentSourceIndex: 0`.
   Or buy the agent from preprod.sokosumi.com.
3. Poll `GET <agent>/status?job_id=<id>`: `awaiting_payment` → `running` → `completed` with the report.
4. In the admin, the payment moves to *ResultSubmitted*, and later the funds unlock to the seller.

## Security

- All secrets live only in `.env` (git-ignored) or the Payment Service's own `.env`.
- Never commit wallet mnemonics, `.env` files or Blockfrost keys.
- This project is for Preprod only. Do not use it on Mainnet without a review.
