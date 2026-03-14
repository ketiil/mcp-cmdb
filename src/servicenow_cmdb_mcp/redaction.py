"""Credential pattern redaction for script bodies."""

from __future__ import annotations

import re

REDACTION_PLACEHOLDER = "[REDACTED — credential pattern detected]"

# Compiled patterns for credential detection in ServiceNow script bodies
_PATTERNS: list[re.Pattern[str]] = [
    # gs.getProperty with key/token/secret/password in the property name
    re.compile(
        r"""gs\.getProperty\(\s*['"][^'"]*(?:key|token|secret|password|credential|api_key)[^'"]*['"]\s*\)""",
        re.IGNORECASE,
    ),
    # Hardcoded passwords/secrets in string assignments
    re.compile(
        r"""(?:password|passwd|pwd|secret|api_key|apikey|token|auth_token|access_token)\s*[:=]\s*['"][^'"]{4,}['"]""",
        re.IGNORECASE,
    ),
    # Bearer token headers
    re.compile(
        r"""['"]Bearer\s+[A-Za-z0-9\-._~+/]+=*['"]""",
    ),
    # Authorization header values
    re.compile(
        r"""['"]Basic\s+[A-Za-z0-9+/]+=*['"]""",
    ),
    # Connection strings with embedded passwords
    re.compile(
        r"""(?:jdbc|mongodb|mysql|postgres|sqlserver)://[^'"\s]*:[^@'"\s]+@[^'"\s]+""",
        re.IGNORECASE,
    ),
    # Base64-encoded credential blocks (long base64 strings in assignments to credential-like vars)
    re.compile(
        r"""(?:password|secret|token|credential|key)\s*[:=]\s*['"][A-Za-z0-9+/]{40,}=*['"]""",
        re.IGNORECASE,
    ),
]


def redact_credentials(text: str) -> str:
    """Scrub credential patterns from a script body.

    Replaces any matched credential pattern with a redaction placeholder.
    This runs on all script bodies before they are returned to the LLM.

    Args:
        text: Raw script body text from ServiceNow.

    Returns:
        Text with credential patterns replaced by redaction placeholders.
    """
    result = text
    for pattern in _PATTERNS:
        result = pattern.sub(REDACTION_PLACEHOLDER, result)
    return result
