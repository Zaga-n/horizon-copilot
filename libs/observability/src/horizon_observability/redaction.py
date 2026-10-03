"""The one redaction policy for rendered log text: credentials and tokens become a stable marker."""

import re

REDACTED = "[REDACTED]"
MAX_TEXT_CHARS = 2_000
TRUNCATED = "...[truncated]"

_SECRET_KEYS = (
    r"password|passwd|pwd|secret|token|access_token|refresh_token|id_token|api[_-]?key|apikey"
    r"|client_secret|private_key|authorization|cookie|session|sig|signature"
    r"|x-amz-signature|x-amz-credential|x-amz-security-token"
)
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Header and bearer values, e.g. "Authorization: Bearer abc" or "Bearer abc".
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), rf"\1 {REDACTED}"),
    # key=value / key: value / "key": "value", including URL query parameters and libpq conninfo.
    (
        re.compile(rf"""(?i)(["']?\b(?:{_SECRET_KEYS})\b["']?\s*[=:]\s*)(["']?)[^\s&,;"'}}]+\2"""),
        rf"\1{REDACTED}",
    ),
    # Credentials embedded in a URL: scheme://user:password@host.
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@"), rf"\1{REDACTED}@"),
    # Well-known token shapes: JWTs, provider keys, AWS access key IDs, GitHub tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), REDACTED),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
)


def redact_text(text: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Mask credentials in free text, then bound its length; never a reversible transform."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    if len(text) > max_chars:
        return text[: max_chars - len(TRUNCATED)] + TRUNCATED
    return text
