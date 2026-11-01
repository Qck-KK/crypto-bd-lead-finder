# Mainnet checklist

This agent runs on Cardano **Preprod** only. `main.py` refuses any other `NETWORK`, on purpose. Going to
Mainnet means real money for buyers and for us, so every step below is a decision for the owner, not an
automatic switch.

## 1. Before anything touches Mainnet

- [ ] `python scripts/preflight.py` passes on Preprod for both agents (standard and Pro).
- [ ] `scripts/acceptance.py` passes on Preprod for both agents (paid job, price locked, on-chain result
      hash equals the delivered result, compliance scan).
- [ ] `python scripts/preflight.py --target Mainnet` reviewed: every WARN has an owner and a decision.
- [ ] Data providers: treg's terms allow paid resale in a product (§07/§11); confirm the upstream providers
      used on Mainnet (Apollo enrichment in particular) allow it for your use case.
- [ ] Legal: privacy policy and terms URLs ready for the registration's `Legal` field; GDPR basis for
      processing public professional data (legitimate interest assessment), opt-out handling (feedback
      "wrong person" / suppression covers removal from future deliveries).

## 2. Infrastructure

- [ ] A separate Mainnet Payment Service (do not mix with Preprod), on an always-on host, with its own
      PostgreSQL, `ENCRYPTION_KEY` and `ADMIN_KEY`; admin port not public.
- [ ] `BLOCKFROST_API_KEY_MAINNET` only in that Payment Service.
- [ ] New Mainnet selling and purchase hot wallets; `COLLECTION_WALLET_MAINNET_ADDRESS` = a hardware wallet,
      so payments leave the hot wallet. Mnemonics stored offline.
- [ ] Daily backups (`deploy/backup.sh`) running and a restore tested.
- [ ] Monitoring: `/metrics` scraped or `/admin/stats` checked daily; alert on `payments_rejected`, failed
      jobs and spend near `DAILY_SPEND_CAP_USD`.

## 3. Agent configuration

- [ ] Price in Mainnet USDM (`c48cbb3d…5553444d`), set from `scripts/registration_values.py`, covering the
      worst-case cost with margin (preflight checks ×1.5).
- [ ] `EXPECTED_PAYMENT_UNIT` / `EXPECTED_PAYMENT_ATOMIC` match the registered price (payment acceptance
      rejects anything else before work starts).
- [ ] `DAILY_SPEND_CAP_USD`, `TREG_JOB_BUDGET_USD`, `ENRICH_BUDGET_USD` set deliberately; treg org budget
      also capped on treg's side.
- [ ] Agent API key: pay + read only, `NetworkLimit: [Mainnet]`, scoped to the Mainnet selling wallet.
- [ ] `FEEDBACK_SECRET` and `ADMIN_TOKEN` freshly generated for Mainnet.

## 4. Launch (owner only)

- [ ] Remove the Preprod-only guard in `main.py` in a reviewed commit.
- [ ] Register on Mainnet (costs real ADA).
- [ ] Submit the Sokosumi Mainnet whitelist form.
- [ ] First Mainnet sale bought by the owner, verified by hand (escrow, delivery, result hash, payout to the
      collection wallet after unlock).
