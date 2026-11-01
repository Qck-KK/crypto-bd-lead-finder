"""Makes masumi SDK 1.2.0 work with Web3CardanoV2 payment sources (Payment Service 0.29+).

The SDK always sends `paymentType: "Web3CardanoV1"` and no source index. A V2 registration
prices each supported payment source separately, so the Payment Service requires
`paymentSourceType` and `supportedPaymentSourceIndex` on POST /payment. `V2Payment` sends them;
`install()` makes the SDK's server and our recovery code construct it.

Configure with PAYMENT_SOURCE_TYPE (default Web3CardanoV2; Web3CardanoV1 restores SDK behaviour)
and PAYMENT_SOURCE_INDEX (default 0, the first entry of supportedPaymentSources at registration).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import aiohttp
import masumi.server
from masumi import Payment

logger = logging.getLogger(__name__)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class V2Payment(Payment):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.payment_type = os.getenv("PAYMENT_SOURCE_TYPE", "Web3CardanoV2")
        self.source_index = int(os.getenv("PAYMENT_SOURCE_INDEX", "0"))

    def build_payload(self, metadata: Optional[str] = None) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        payload: Dict[str, Any] = {
            "agentIdentifier": self.agent_identifier,
            "network": self.network,
            "paymentSourceType": self.payment_type,
            # Same windows as the SDK: 12 h to pay, 24 h to submit the result.
            "payByTime": _iso(now + timedelta(hours=12)),
            "submitResultTime": _iso(now + timedelta(hours=24)),
            "identifierFromPurchaser": self.identifier_from_purchaser,
        }
        if self.payment_type == "Web3CardanoV2":
            payload["supportedPaymentSourceIndex"] = self.source_index
        if self.input_hash:
            payload["inputHash"] = self.input_hash
        if metadata:
            payload["metadata"] = metadata
        return payload

    async def create_payment_request(self, metadata: Optional[str] = None) -> Dict[str, Any]:
        payload = self.build_payload(metadata)
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.config.payment_service_url}/payment/", headers=self._headers,
                                    json=payload) as response:
                text = await response.text()
                if response.status == 401:
                    raise ValueError("Unauthorized: Invalid API key")
                if response.status == 400:
                    raise ValueError(f"Bad request: {text}")
                if response.status != 200:
                    raise Exception(f"Payment request failed ({response.status}): {text[:500]}")
                result = json.loads(text)
        data = result["data"]
        self.payment_ids.add(data["blockchainIdentifier"])
        result["time_values"] = {k: data[k] for k in ("payByTime", "submitResultTime", "unlockTime",
                                                      "externalDisputeUnlockTime")}
        logger.info("Payment request created: %s...", data["blockchainIdentifier"][:12])
        return result


def install() -> None:
    """Point the SDK's server at V2Payment (it looks up `Payment` in masumi.server at call time)."""
    masumi.server.Payment = V2Payment
