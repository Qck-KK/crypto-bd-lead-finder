"""Logging: job tag on every line, optional JSON output, and redaction of personal data.

The masumi SDK logs job input, the full result ("Escaped Output for hashing") and payment payloads
at INFO. Results contain people's names, so those messages are cut to a short prefix before any
handler sees them. LOG_FORMAT=json writes one JSON object per line for log shippers.
"""

import json
import logging
import os
from contextvars import ContextVar

job_tag_var: ContextVar[str] = ContextVar("job_tag", default="-")

# Message prefixes from the SDK that are followed by job input, results or payment details.
_REDACT_PREFIXES = ("Escaped Output for hashing", "Input data for job", "Payment Details",
                    "Payment request payload prepared", "Full payment response")


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.job = job_tag_var.get()
        msg = record.getMessage()
        if msg.startswith(_REDACT_PREFIXES):
            record.msg, record.args = msg.split(":", 1)[0] + ": [redacted]", ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname, "logger": record.name,
               "job": getattr(record, "job", "-"), "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO")
    handler = logging.StreamHandler()
    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(job)s]: %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    install_redaction()


def install_redaction() -> None:
    """Redact on the loggers themselves too: the SDK adds its own handlers that bypass ours."""
    f = RedactFilter()
    for name in ("", "masumi", "masumi.server", "masumi.payment", "masumi.helper_functions", "masumi.job_manager"):
        logger = logging.getLogger(name)
        if not any(isinstance(x, RedactFilter) for x in logger.filters):
            logger.addFilter(f)
        for h in logger.handlers:
            if not any(isinstance(x, RedactFilter) for x in h.filters):
                h.addFilter(f)
