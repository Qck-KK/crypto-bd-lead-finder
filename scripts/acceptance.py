"""Payment acceptance test: one real paid job end to end, Preprod only.

Run where the Payment Service is reachable, with its admin key (to buy from the purchasing wallet):

    ADMIN_KEY=... python scripts/acceptance.py --agent-url https://<agent> --company "Coinbase" \\
        --domain coinbase.com --role recruiter

Checks: availability and schema; MIP-003 start_job fields; input hash (MIP-004) matches; the purchase
locks exactly the registered price; the job completes; the on-chain result hash equals the hash of
the delivered result; the result passes the compliance scan. Spends test tokens only and refuses any
network other than Preprod.
"""

import argparse
import json
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from masumi.helper_functions import create_masumi_input_hash, create_masumi_output_hash  # noqa: E402

# The SDK logs the full result (people's names) at INFO while hashing; keep this script's output clean.
logging.disable(logging.INFO)

START_FIELDS = ("id", "blockchainIdentifier", "payByTime", "submitResultTime", "unlockTime",
                "externalDisputeUnlockTime", "agentIdentifier", "sellerVKey", "identifierFromPurchaser", "input_hash")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class Result:
    def __init__(self):
        self.rows = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append(("PASS" if ok else "FAIL", name, detail))
        print(f"{'PASS' if ok else 'FAIL':<5} {name}" + (f"  ({detail})" if detail else ""), flush=True)
        return ok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent-url", required=True)
    p.add_argument("--payment-url", default=os.getenv("PAYMENT_SERVICE_URL", "http://localhost:3001/api/v1"))
    p.add_argument("--network", default="Preprod")
    p.add_argument("--company", required=True)
    p.add_argument("--domain")
    p.add_argument("--role")
    p.add_argument("--timeout-min", type=float, default=20)
    a = p.parse_args()
    if a.network != "Preprod":
        print("Refusing: this test spends real funds on any network other than Preprod.")
        return 2
    admin = os.getenv("ADMIN_KEY", "")
    if not admin:
        print("Set ADMIN_KEY (Payment Service admin key) in the environment.")
        return 2

    agent, pay = a.agent_url.rstrip("/"), a.payment_url.rstrip("/")
    r = Result()
    inp = {"company": a.company, **({"company_domain": a.domain} if a.domain else {}), **({"role": a.role} if a.role else {})}
    ident = secrets.token_hex(10)
    with httpx.Client(timeout=60) as c:
        av = c.get(f"{agent}/availability").json()
        r.check(av.get("status") == "available", "agent available", av.get("message", ""))
        r.check(c.get(f"{agent}/input_schema").status_code == 200, "input schema served")

        job = c.post(f"{agent}/start_job", json={"identifier_from_purchaser": ident, "input_data": inp}).json()
        if not r.check(all(k in job for k in START_FIELDS), "start_job returns all MIP-003 fields"):
            return 1
        r.check(job["input_hash"] == create_masumi_input_hash(inp, ident), "input hash matches MIP-004")

        purchase = {k: job[k] for k in ("blockchainIdentifier", "agentIdentifier")}
        purchase.update(network="Preprod", paymentSourceType="Web3CardanoV2", supportedPaymentSourceIndex=0,
                        inputHash=job["input_hash"], sellerVkey=job["sellerVKey"],
                        identifierFromPurchaser=ident,
                        **{k: str(job[k]) for k in ("unlockTime", "externalDisputeUnlockTime", "submitResultTime",
                                                    "payByTime")})
        pr = c.post(f"{pay}/purchase/", headers={"token": admin}, json=purchase).json()
        if not r.check(pr.get("status") == "success", "purchase created", str(pr.get("error") or "")):
            return 1

        deadline, status = time.time() + a.timeout_min * 60, {}
        while time.time() < deadline:
            status = c.get(f"{agent}/status", params={"job_id": job["id"]}).json()
            if status.get("status") in ("completed", "failed"):
                break
            time.sleep(15)
        if not r.check(status.get("status") == "completed", "job completed", status.get("status", "timeout")):
            return 1
        result = status["result"]

        payment = {}
        for _ in range(20):  # the result hash appears once the submit transaction is seen
            payment = c.post(f"{pay}/payment/resolve-blockchain-identifier", headers={"token": admin},
                             json={"blockchainIdentifier": job["blockchainIdentifier"], "network": "Preprod"}
                             ).json().get("data") or {}
            if payment.get("resultHash"):
                break
            time.sleep(15)
        funds = payment.get("RequestedFunds") or []
        r.check(bool(funds), "registered price locked", json.dumps(funds))
        r.check(payment.get("resultHash") == create_masumi_output_hash(result, ident),
                "on-chain result hash = hash of delivered result", payment.get("onChainState", ""))
        r.check(not EMAIL.search(result), "no email addresses in the result")
        r.check(result.rstrip().endswith("For B2B professional outreach only."), "data source and usage notice")
    fails = sum(1 for s, _, _ in r.rows if s == "FAIL")
    print(f"\nacceptance: {len(r.rows) - fails} passed, {fails} failed (job {job['id']})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
