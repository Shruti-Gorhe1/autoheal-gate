"""Security guards shared by execution, audit, and observability paths."""
from __future__ import annotations

import re
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password|authorization|webhook[_-]?secret)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9+/=_\-.]+")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[ps]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

# Commands that can turn the validation workspace into a network/control-plane
# escape hatch. Normal test commands such as python/pytest/git/echo remain
# supported. This is deliberately conservative rather than pretending that
# shell execution is a security boundary.
_DANGEROUS_COMMANDS = re.compile(
    r"(?i)(?:^|[;&|]\s*)"
    r"(?:curl|wget|certutil|bitsadmin|invoke-webrequest|iwr|irm|start-bitstransfer)\b"
    r"|\b(?:git\s+push|git\s+remote\s+set-url|gh\s+(?:auth|repo|pr))\b"
    r"|\b(?:powershell|pwsh)\s+.*(?:download|invoke-webrequest|start-bitstransfer)\b"
    r"|\b(?:rm\s+-rf|rmdir\s+/s|del\s+/f\s+/s)\b"
    r"|\b(?:nc|ncat|netcat)\b"
)


def redact_secrets(value: Any) -> Any:
    """Recursively redact values likely to contain credentials."""
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if _SECRET_KEY_RE.search(str(k)) else redact_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(v) for v in value)
    if isinstance(value, str):
        value = _BEARER_RE.sub("[REDACTED]", value)
        value = _GITHUB_TOKEN_RE.sub("[REDACTED]", value)
        value = _AWS_KEY_RE.sub("[REDACTED]", value)
        return value
    return value


def safe_error(value: Any, limit: int = 1000) -> str:
    text = str(redact_secrets(value))
    return text[:limit]


def validate_execution_command(command: str | None) -> tuple[bool, str | None]:
    """Reject obvious network/control-plane/destructive commands.

    This protects against accidental command injection without claiming that
    shell execution is a sandbox. Docker/container isolation remains the
    stronger boundary for untrusted repositories.
    """
    if not command or not command.strip():
        return True, None
    if _DANGEROUS_COMMANDS.search(command):
        return False, "The execution command contains a blocked network, credential, or destructive operation."
    return True, None
