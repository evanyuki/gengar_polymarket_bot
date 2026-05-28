"""Security helpers for log-safe bot output.

Never print private keys, auth tokens, or full headers. These helpers are
small and dependency-free so they can be used from exception paths.
"""

from __future__ import annotations

import re

_PRIVATE_KEY_RE = re.compile(r"0x[a-fA-F0-9]{64}")
_AUTH_RE = re.compile(
    r"(?i)(authorization|x-api-key|polymarket-key|key|private[_-]?key)\s*[:=]\s*([^\s,;}]+)"
)


def redact_secret(value: object) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    if len(text) <= 10:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def sanitize_exception_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = _PRIVATE_KEY_RE.sub(lambda m: redact_secret(m.group(0)), text)
    text = _AUTH_RE.sub(lambda m: f"{m.group(1)}=***", text)
    return text
