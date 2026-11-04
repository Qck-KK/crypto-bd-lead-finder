"""Logging: job tag on every line, optional JSON output, and redaction of personal data.

The masumi SDK logs job input, the full result ("Escaped Output for hashing") and payment payloads
at INFO. Results contain people's names, so those messages are cut to a short prefix before any
handler sees them. LOG_FORMAT=json writes one JSON object per line for log shippers.
"""

import json
import logging
import os
import re
from contextvars import ContextVar
from typing import Iterable

job_tag_var: ContextVar[str] = ContextVar("job_tag", default="-")

# Message prefixes from the SDK that are followed by job input, results or payment details.
_REDACT_PREFIXES = ("Escaped Output for hashing", "Input data for job", "Payment Details",
                    "Payment request payload prepared", "Full payment response")

# Credentials that can end up inside error text (BL-16):
# - `label: value` / `label=value` pairs whose label ends in a credential word, prefixed or not (token, api_key,
#   PAYMENT_API_KEY, client_secret, Authorization: Basic ...), the value including an auth scheme word;
# - bearer and basic credentials without a label (a basic credential only when it looks like Base64, so the word
#   "basic" in prose stays);
# - the password of a connection URL (postgresql://user:password@host).
_CREDENTIAL_WORDS = (r"token|api[_-]?key|authorization|password|passwd|secret|admin[_-]?key|access[_-]?key|"
                     r"private[_-]?key")
_CREDENTIAL_PATTERNS = (
    (re.compile(r"(?i)\b([A-Za-z0-9_-]*?(?:" + _CREDENTIAL_WORDS + r"))\b(\s*[\"']?\s*[:=]\s*[\"']?)"
                r"(?:(?:bearer|basic|digest|token)\s+)?[^\s\"',;}&]+"), r"\1\2[redacted]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(basic\s+)(?=(?-i:[A-Za-z0-9+/]*[A-Z0-9+/=]))[A-Za-z0-9+/]{8,}={0,2}"), r"\1[redacted]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]*@"), r"\1[redacted]@"),
)
# Environment variables whose values must never appear in a log line, whatever the text around them.
_SECRET_ENV = ("PAYMENT_API_KEY", "ADMIN_TOKEN", "FEEDBACK_ADMIN_TOKEN", "FEEDBACK_SECRET", "TREG_TOKEN",
               "GEMINI_API_KEY", "ADMIN_KEY")


def redact_credentials(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with known secret values and credential-looking pairs replaced by [redacted]."""
    for value in (*secrets, *(os.getenv(name, "") for name in _SECRET_ENV)):
        if value and len(value) >= 6:
            text = text.replace(value, "[redacted]")
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize(text: object, limit: int = 300, secrets: Iterable[str] = ()) -> str:
    """Text safe for one log line: credentials redacted (before cutting, so no half secret survives), control
    characters flattened (no forged log lines) and at most `limit` characters."""
    out = re.sub(r"[\x00-\x1f\x7f]+", " ", redact_credentials(str(text), secrets)).strip()
    return out if len(out) <= limit else out[:limit] + "...[truncated]"


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.job = job_tag_var.get()
        msg = record.getMessage()
        if msg.startswith(_REDACT_PREFIXES):
            record.msg, record.args = msg.split(":", 1)[0] + ": [redacted]", ()
            return True
        clean = redact_credentials(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        if record.exc_info and not record.exc_text:
            # Formatters print exc_text when it is set; a traceback repeats the exception's message.
            record.exc_text = redact_credentials(logging.Formatter().formatException(record.exc_info))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname, "logger": record.name,
               "job": getattr(record, "job", "-"), "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = record.exc_text or self.formatException(record.exc_info)
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
    """Redact on the loggers themselves too: the SDK adds its own handlers that bypass ours, and uvicorn logs
    unhandled request exceptions (with traceback) on `uvicorn.error` through its own handler. A filter on the logger
    survives uvicorn's logging setup (dictConfig replaces handlers, not filters). `uvicorn.access` is left alone:
    its formatter reads the record's arguments, and its lines carry no exception text."""
    f = RedactFilter()
    for name in ("", "masumi", "masumi.server", "masumi.payment", "masumi.helper_functions", "masumi.job_manager",
                 "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(x, RedactFilter) for x in logger.filters):
            logger.addFilter(f)
        for h in logger.handlers:
            if not any(isinstance(x, RedactFilter) for x in h.filters):
                h.addFilter(f)
