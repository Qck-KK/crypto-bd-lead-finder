"""`identifier_from_purchaser` validation (BL-16), shared by every place that sends it to the Payment Service.

The Payment Service 0.29 accepts 14 to 26 characters (zod `min(14).max(26)`) that pass `validateHexString`:
hexadecimal digits only, and an even number of them. A value it would refuse is refused here with HTTP 400, before
any payment request is made, so a buyer's typo never reaches the Payment Service or comes back as its error text.
"""

import re
from typing import Any, Optional

MIN_LENGTH, MAX_LENGTH = 14, 26
_HEX = re.compile(r"[0-9a-fA-F]+")
MESSAGE = (f"identifier_from_purchaser must be a hexadecimal string of {MIN_LENGTH} to {MAX_LENGTH} characters "
           "with an even length")


class PurchaserIdentifierError(ValueError):
    """The purchaser identifier would be refused by the Payment Service. The message is safe to show the buyer."""

    def __init__(self) -> None:
        super().__init__(MESSAGE)


def purchaser_identifier_problem(value: Any) -> Optional[str]:
    """Why `value` is not an identifier the Payment Service accepts, or None if it is."""
    if (not isinstance(value, str) or not MIN_LENGTH <= len(value) <= MAX_LENGTH or len(value) % 2
            or not _HEX.fullmatch(value)):
        return MESSAGE
    return None


def validate_purchaser_identifier(value: Any) -> str:
    """Return `value` if the Payment Service accepts it, else raise PurchaserIdentifierError."""
    if purchaser_identifier_problem(value):
        raise PurchaserIdentifierError()
    return value
