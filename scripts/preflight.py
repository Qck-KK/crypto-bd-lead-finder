"""Read-only launch check for one agent. Changes nothing, sends no payments.

    python scripts/preflight.py                  # check the current setup (Preprod)
    python scripts/preflight.py --target Mainnet # what would block or worry a Mainnet launch

Reads the agent's env (.env or the container env). Prints PASS / WARN / FAIL per check and exits 1
on any FAIL. Secret values are never printed.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

from app.pricing import TUSDM_DECIMALS, estimate  # noqa: E402
from app.server import expected_payment  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def check_secrets() -> list:
    out = []
    for name, min_len in (("PAYMENT_API_KEY", 16), ("SELLER_VKEY", 56), ("AGENT_IDENTIFIER", 40),
                          ("TREG_TOKEN", 16), ("GEMINI_API_KEY", 16), ("AGENT_DATABASE_URL", 10),
                          ("FEEDBACK_SECRET", 32)):
        v = _env(name)
        out.append((PASS if len(v) >= min_len else FAIL, f"{name} set", "ok" if v else "missing"))
    admin = _env("ADMIN_TOKEN") or _env("FEEDBACK_ADMIN_TOKEN")
    out.append((PASS if len(admin) >= 24 else WARN, "admin token for /admin/stats", "ok" if admin else "missing"))
    return out


def check_economics(tier: str, target: str) -> list:
    e = estimate(tier)
    cap = float(_env("DAILY_SPEND_CAP_USD") or 1.0)
    enrich = float(_env("ENRICH_BUDGET_USD") or (0.30 if tier == "pro" else 0))
    margin = e["price_tusdm"] / e["cost_usd"] if e["cost_usd"] else float("inf")
    out = [
        (PASS if margin >= 1.5 else FAIL, "price covers worst-case cost",
         f"price {e['price_tusdm']} vs cost ${e['cost_usd']} (x{margin:.1f})"),
        (PASS if cap > 0 else WARN, "daily spend cap", f"{cap} USD/day"),
    ]
    if tier == "pro":
        out.append((PASS if enrich <= e["price_tusdm"] * 0.5 else WARN, "enrichment budget vs price",
                    f"{enrich} USD per job"))
        if target == "Preprod" and enrich > 0:
            out.append((WARN, "enrichment on Preprod", "test-token buyers can spend real treg balance"))
    return out


def check_payment_service(client: httpx.Client, target: str, tier: str) -> list:
    url, key, agent_id = _env("PAYMENT_SERVICE_URL").rstrip("/"), _env("PAYMENT_API_KEY"), _env("AGENT_IDENTIFIER")
    out = []
    try:
        ok = client.get(f"{url}/health/").status_code == 200
        out.append((PASS if ok else FAIL, "payment service healthy", url))
    except httpx.HTTPError as e:
        return [(FAIL, "payment service reachable", type(e).__name__)]

    try:
        k = client.get(f"{url}/api-key-status/", headers={"token": key}).json().get("data") or {}
        out.append((PASS if not k.get("canAdmin") else FAIL, "agent API key is not admin", ""))
        nets = k.get("NetworkLimit") or []
        out.append((PASS if nets == [target] else WARN, "API key limited to target network", ",".join(nets)))
        out.append((PASS if k.get("walletScopeEnabled") else WARN, "API key scoped to the selling wallet", ""))
    except (httpx.HTTPError, ValueError) as e:
        out.append((FAIL, "API key status", type(e).__name__))

    try:
        r = client.get(f"{url}/registry/", headers={"token": key},
                       params={"network": target, "filterAgentIdentifier": agent_id}).json()
        assets = (r.get("data") or {}).get("Assets") or []
    except (httpx.HTTPError, ValueError) as e:
        return out + [(FAIL, "registry entry", type(e).__name__)]
    if not assets:
        return out + [(FAIL, f"agent registered on {target}", "not found")]
    a = assets[0]
    out.append((PASS if a.get("state") == "RegistrationConfirmed" else FAIL, "registration confirmed",
                a.get("state", "")))
    public = _env("AGENT_PUBLIC_URL").rstrip("/")
    out.append((PASS if (a.get("apiBaseUrl") or "").rstrip("/") == public else FAIL, "registered URL = public URL",
                a.get("apiBaseUrl", "")))
    unit, amount = expected_payment(target, tier)
    fixed = [f for s in a.get("supportedPaymentSources") or [] for f in ((s.get("pricing") or {}).get("fixed") or [])]
    match = any(f.get("asset") == unit and int(f.get("amount", 0)) == amount for f in fixed)
    out.append((PASS if match else FAIL, "registered price = expected price",
                f"expected {amount / 10**TUSDM_DECIMALS} of {unit[:16]}…"))
    return out


def check_public(client: httpx.Client) -> list:
    public = _env("AGENT_PUBLIC_URL").rstrip("/")
    if not public.startswith("https://"):
        return [(FAIL, "public URL is https", public or "missing")]
    try:
        av = client.get(f"{public}/availability").json()
        schema = client.get(f"{public}/input_schema").status_code
    except (httpx.HTTPError, ValueError) as e:
        return [(FAIL, "public endpoints reachable", type(e).__name__)]
    return [(PASS if av.get("status") == "available" else FAIL, "/availability", av.get("message", "")),
            (PASS if schema == 200 else FAIL, "/input_schema", str(schema))]


def check_mainnet_manual(target: str) -> list:
    if target != "Mainnet":
        return []
    return [
        (FAIL, "code guard", "main.py refuses NETWORK other than Preprod; lifting it is a deliberate change"),
        (WARN, "Sokosumi whitelist", "Mainnet listing needs the whitelist form (owner's decision)"),
        (WARN, "mainnet wallets", "new mainnet selling/purchase wallets; collection wallet on hardware"),
        (WARN, "Blockfrost mainnet key", "set BLOCKFROST_API_KEY_MAINNET in the payment service only"),
        (WARN, "legal", "privacy policy and terms URLs in the registration's Legal field"),
        (WARN, "data providers", "confirm treg / provider terms for paid resale on Mainnet"),
        (WARN, "real-money test", "run scripts/acceptance.py on Preprod; first Mainnet sale by hand"),
    ]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["Preprod", "Mainnet"], default="Preprod")
    args = p.parse_args()
    tier = "pro" if _env("AGENT_TIER").lower() == "pro" else "standard"
    with httpx.Client(timeout=15) as client:
        results = (check_secrets() + check_economics(tier, args.target)
                   + check_payment_service(client, args.target, tier) + check_public(client)
                   + check_mainnet_manual(args.target))
    for status, name, detail in results:
        print(f"{status:<5} {name}" + (f"  ({detail})" if detail else ""))
    fails = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{tier} agent, target {args.target}: {fails} FAIL, {sum(1 for s, _, _ in results if s == WARN)} WARN")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
