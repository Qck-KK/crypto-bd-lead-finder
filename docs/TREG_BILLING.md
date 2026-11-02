# treg billing: what we know and what is still open (roadmap step 1)

Read on 2026-11-02 from treg's public guide ([treg.to/skill.md](https://treg.to/skill.md)) and its open-source
server ([github.com/superdesigndev/treg](https://github.com/superdesigndev/treg), branch `main`). Read only;
no treg call was made.

- **Source versus service:** the open-source `main` branch is not necessarily the code running at treg.to, so
  "source" below means "how the published code behaves", not a guarantee from treg.
- **Quotes:** they were read through a summarizing fetch tool; check the exact wording at the linked files
  before quoting them to anyone.

Evidence classes:
- **Source:** in treg's published code or its architecture docs (`docs/context/...`).
- **Docs:** in treg's public guide or API doc only.
- **Unknown:** neither says; needs treg or a live check.

## Answers

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Is `X-Treg-Route-Max-Cost` a hard bound on a direct call's final charge? | **No.** It is checked against the *reservation estimate* (plus margin) before the hold; over it, the call is refused with 402 `route_max_cost` and nothing is charged. Except for AnyAPI, "the ceiling remains a pre-reservation guard: provider-reported settlement can still exceed the estimate the guard checked". `settle: usage` rows "may exceed the reserve"; the overrun is taken from the balance and listed by `reconcile.async_task_settlement`. | Source: `docs/context/architecture/money.md`, `application/call/reserve.py` (`_platform_reserve`) |
| 1b | Routed `treg.people.search` | The route ceiling (default $1) skips every candidate whose *catalog price* would exceed what is left; each child gets the unspent part. Settlement per child is still provider-reported, so the same caveat applies. | Source: `application/call/route.py` |
| 2 | Apollo search price | `apollo.people.search`: `cost: type: free, value: 0, unit: call`, "documented free — 0 credits, because no contact data is returned. Counts against the plan's request limits only". A free endpoint reserves 0. | Source: `src/treg/catalog/apollo.yaml`, `application/call/resolve.py` (`_platform_estimate_micro`) |
| 3 | Bulk enrichment reservation and billing | Billed **per person matched** (1 credit baseline; unmatched rows free; +8 credits per mobile, which we never request). The endpoint is `per_success` without a `reserve`/`quantity` rule, and `details` is not an entity key, so the hold is **one unit** whatever the batch size. Settlement then charges per match, above the hold and possibly above our max cost. The USD price per Apollo credit comes from the catalog's `credit_rates` and was not found. | Source: `apollo.yaml`, `resolve.py`; the credit rate is Unknown |
| 4 | Is `X-Treg-Cost-Micro` always present? | "Metered responses carry it"; "a reserved call that fails before a provider answer carries an explicit `0`"; replays send `0` plus `X-Treg-Original-Cost-Micro`. **Missing** on own-key/tool calls and on refusals such as `provider_paused` and `provider_capacity_unavailable`. On a routed response, the published route code reads the children's header but sets none itself. | Docs: `docs/context/interface/api.md`; Source: `route.py`. Whether the routed response carries it is Unknown |
| 5 | 502 `route_failed` and `charged_micro` | `charged_micro` sits **under `detail`**, and `run_routed` sets it to `0` on failure: "A routed call that fails charges the caller nothing". A 200 miss is billed (each attempt settles at its own price); `not_found` charges nothing. | Source: `route.py` |
| 6 | Uncharged refusals | 402 `route_max_cost`: not charged. 402 `insufficient_balance`: refused before the relay. 429 `share_busy`, `provider_rate_limited`, `money_admission_timeout`: not charged. 503 `provider_capacity_unavailable`: no hold. 502 `response_buffer_limit`: not charged. Provider 5xx and caller 4xx are released (free) unless the provider reports a charge; a timeout or connect error releases as `call_failed_*`. **409** (in flight) and **422** (key reused): the charge is not stated. | Docs: `api.md`; Source: `money.md` |
| 7 | Formats | Max-cost header: a USD amount (non-numeric → 400); precision not stated. Idempotency-Key: TTL 24 h; replay free; 409 while in flight; 422 for a different request; length and charset not stated. Responses spooled to disk are not stored for replay, so a retry is charged again. | Docs: `api.md`; the 24 h TTL is from `docs/IDEMPOTENT-CALLS-PLAN.md` |

**Also found:** treg has a **per-team daily platform cap**.
- It is `Org.daily_cap_micro`, set with `PATCH /orgs/{id}/settings`.
- It is enforced at reserve time (`_enforce_platform_daily_cap`).
- The shipped default is 0, meaning no limit.
- treg's own budgets "are SOFT" (overshoot bounded by concurrency × per-call estimate).

Source: `money.md`.

## What this means for us

1. **No hard cap is claimable from our side.** Our ledger is correct on what treg *reports*, but a direct
   call can settle above the max cost we send, and our cap would then be overrun by the difference. The only
   hard money bound is the **treg prepaid balance**. A reserve needs funds; an overrun at settlement still
   comes out of the balance, and then the next reserve is refused.
2. **Apollo search** is free and reserves nothing. Counting it at $0.01 when no cost header comes is safe and
   only over-counts.
3. **Bulk enrichment is the real risk.**
   - **Per match, above the hold:** it is billed per match above a one-unit hold, so the final charge is
     bounded only by matches × credit price. We send at most the number of ids our budget covers at
     $0.026/match.
   - **Credit rate:** if treg's Apollo credit rate is above $0.026, a full batch can cost more than we
     reserved.
   - **Before turning Pro enrichment on:** confirm the credit rate, and send one id per call or keep the
     batch small.
4. **Routed search:**
   - The route ceiling keeps candidates within our max cost by *catalog price*.
   - A routed failure (`route_failed`) charges nothing.
   - A 200 miss is billed.
   - If routed responses carry no `X-Treg-Cost-Micro`, every routed call counts at our max cost. That is
     safe, but it may make the cap bite early (check A below).
5. **Refusal trust:** 402/429/503 refusals are documented as free, so `TREG_TRUST_UNCHARGED_REFUSALS` would
   be correct for them. Keep it off until a live check confirms the deployed service matches; 409 and 422 are
   not documented either way.
6. **Our parsing:**
   - **`charged_micro`:** we read it top level and under `detail`, which matches the source.
   - **Idempotency key:** our 45-character hex key has no documented limit to violate.

## Recommended treg-side protection (owner action: account settings)

- **Daily cap:** set the team's daily cap in treg (`Org.daily_cap_micro`) to our `DAILY_SPEND_CAP_USD`. It is
  a second, server-side gate on new reservations across both agents.
- **Prepaid balance:** keep it small and auto top-up off (`maybe_schedule_autotopup` exists). The balance is
  the only bound that also stops overruns.

## Still open (ask treg, or a live check)

**Questions for treg:**
1. Is the open-source `main` what runs at treg.to, for billing (`money.md`, `reserve.py`, `settle.py`,
   `route.py`)?
2. ~~What is the USD price per Apollo credit~~: $0.026 in the catalog (check D). Is it stable, and are
   catalog price changes announced?
3. For `apollo.people.bulk_enrich`, is the hold really one unit for a 10-id batch? What bounds the final
   charge: matches × credit price, or our `X-Treg-Route-Max-Cost`?
4. Does a routed `treg.people.search` response (hit, 200 miss, `route_failed`) carry `X-Treg-Cost-Micro`?
5. Are 409 (in flight) and 422 (key reused) ever charged?
6. Max length and charset of `Idempotency-Key`; accepted precision of `X-Treg-Route-Max-Cost`.

**Live checks that need the owner's approval:**
- **A. Done 2026-11-02**, read-only transaction on the server's `agent_jobs` database, aggregates only.
  - **Scope:** 2 job cost rows, both on 2026-11-01: one standard, one Pro.
  - **Standard job:** one routed `treg.people.search` that answered (25 rows) and recorded
    `treg_usd = 0.0005`, i.e. `X-Treg-Cost-Micro: 500`.
  - **Pro job:** no routed call (Apollo only), `treg_usd = 0`.
  - **Proves:** the deployed service sent a valid, non-zero `X-Treg-Cost-Micro` on at least one routed
    response that answered.
  - **Does not prove:** coverage for routed misses, failures or other providers. One sample.
  - **Pricing note:** $0.0005 for 25 rows is far below the `COST_TREG_PER_ROW_USD` = $0.0089 pricing
    assumption.
- **B. Done 2026-11-02:** one `apollo.people.search` sent exactly as the agent sends it (max cost `0.0100`,
  `per_page` 1, domain chain.link, which Apollo does not index, so 0 rows).
  - **Response:** HTTP 200 with `X-Treg-Cost-Micro: 0`.
  - **Ledger** (`GET /calls/<id>`): reserve 0, settle 0, `cost_charged_micro` 0.
  - **Confirms:** Apollo search is free on the deployed service and reports an explicit `0` (one sample), so
    our client counts it at $0, not at the assumed $0.01.
  - **Balance:** not read. treg's API doc says the balance route can trigger an auto top-up, and the
    per-call ledger is the better evidence.
- **C. Done 2026-11-02**, read-only GETs.
  - **Team:** our treg team (role owner).
  - **No daily cap:** `GET /orgs/<team id>/settings` has `daily_cap_micro: 0` and `platform_default_micro: 0`, so
    treg enforces **no daily cap at all** for this team.
  - **Member usage:** `GET /usage/me` showed `used_today` 0 (1 after check B) and member cap `-1` (unlimited).
  - **Not visible:** auto top-up and the balance appear in neither response; check the dashboard.
- **D. Done 2026-11-02** (open catalog route, no token).
  - **bulk_enrich:** `GET /catalog/endpoints/apollo.people.bulk_enrich` has
    `cost: per_success, 1 credit per record, usd 0.026` ("Basic-plan rate", `credit_rates_usd.apollo`), billed
    from the provider-reported `credits_consumed`; observed hit rate 0.99. This matches our
    `APOLLO_ENRICH_USD_PER_MATCH`.
  - **search:** `apollo.people.search` has `cost: free, usd 0.0`, verified 2026-08-29.

## Consequences of the live checks

- **Bulk enrichment** is bounded by what we send: final charge ≤ ids sent × $0.026, and we send at most the
  number of ids the attempt budget covers at $0.026. This holds while:
  - treg's Apollo credit rate stays $0.026;
  - mobiles are never revealed (we never request them);
  - Apollo bills only matched people.

  The one-unit hold means treg's own max-cost check does not enforce this; our id count does.
  **Before enabling Pro enrichment,** re-read the catalog price.
- **No treg-side cap is set.** Recommended owner action in treg (an account setting, not done here):
  - set the team's daily cap, e.g. `daily_cap_micro = 1000000` for $1.00 = our `DAILY_SPEND_CAP_USD`;
  - confirm in the dashboard that auto top-up is off and the balance is small.
