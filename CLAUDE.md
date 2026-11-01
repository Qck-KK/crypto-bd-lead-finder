# CLAUDE.md

Crypto BD Lead Finder: a paid Masumi (MIP-003) agent for Sokosumi on Cardano **Preprod**. Python 3.12 +
FastAPI via the `masumi` SDK; people data from treg.to (Apollo first), outreach via Gemini (`google-genai`).
Two registered agents from one codebase: standard and Pro (`AGENT_TIER=pro`). Current state and history:
`HANDOFF.md` (local, not committed).

## Owner's rules (always)

- **Preprod only.** No Mainnet registration, no Mainnet funds, no Sokosumi whitelist form, no lifting the
  Preprod guard in `main.py` without the owner's explicit OK.
- **No real money.** Test ADA / tUSDM only. treg calls cost real balance: Apollo search is free, routed search
  costs cents, enrichment ~$0.026 per person. Enrichment is tested with mocks only; Pro runs with
  `ENRICH_BUDGET_USD=0` until the owner sets a budget.
- **Secrets:** never print, copy into chat, commit or log keys, tokens, mnemonics or `.env` contents. Check
  that a variable is set by its length, not its value. Secrets live in `.env` (local), `deploy/.env`,
  `deploy/agent.env`, `deploy/agent-pro.env` (server) and `masumi-node/.secrets/` (local), all git-ignored.
- **Data compliance:** deliver public professional fields only (name, title, company, LinkedIn or company site,
  source); never personal phone, email or address, even if a provider returns them; max 10 people; data source
  and "For B2B professional outreach only." at the end of descriptions and results.
- **Ask first** before installing global software or services, changing firewall/system settings, and before
  any step that spends real money or touches Mainnet.
- Commit or push only when the owner asks.

## Working on the code

- Env: `C:\Users\zhang\anaconda3\envs\bd-lead-finder\python.exe` (conda, Python 3.12). Do not install a global
  Python.
- Tests: `python -m pytest -q` (all external APIs mocked; `BDLF_NO_DOTENV=1` keeps the real `.env` out).
  PostgreSQL tests run when `TEST_DATABASE_URL` is set (local Postgres: conda env `masumi-pg`, port 5433; start
  `pg_ctl` with `Start-Process ... -WindowStyle Hidden` and no `-Wait`, or the shell hangs).
- Keep the compliance allowlist (`app/compliance.py`) the only way data reaches a result; add a test for any new
  provider shape, including a row stuffed with private data.
- `masumi==1.2.0` is pinned and `app/masumi_compat.py` / `app/server.py` use SDK internals
  (`_handle_payment_confirmed`, `_payment_instances`). `fastapi==0.162.0` is pinned (newer removed
  `add_event_handler`; we wrap `lifespan` instead).
- Payment Service is **0.29 with Web3CardanoV2**: payments need `supportedPaymentSourceIndex`, registrations put
  prices in `supportedPaymentSources[].pricing` (no `decimals`), descriptions max 250 chars.

## Deploying (InstaBox)

- SSH certificates last **1 hour**. If SSH says `Permission denied (publickey)`, ask the owner to run
  `npx instabox@latest ssh m_agqzoe52hlullr222yb5ovrswm` on this machine, then batch the server work.
- Server repo: `/data/workspace/crypto-bd-lead-finder`, compose in `deploy/`.
- Docker Hub rate-limits base image pulls; for code-only changes layer onto the built image:
  `printf "FROM deploy-agent:latest\nCOPY app ./app\nCOPY scripts ./scripts\nCOPY main.py .\n" > /tmp/Dockerfile.codeonly`,
  `DOCKER_BUILDKIT=0 docker build -q -f /tmp/Dockerfile.codeonly -t deploy-agent:latest .`, then
  `docker compose up -d --no-build --no-deps --force-recreate agent agent-pro`.
- After deploying: `docker compose exec -T agent python scripts/preflight.py` (and `agent-pro`), then optionally
  `scripts/acceptance.py` (paid, Preprod test tokens; needs `ADMIN_KEY` from `deploy/.env` passed with `-e`,
  never echoed).
