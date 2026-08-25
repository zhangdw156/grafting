"""Classify pipeline errors as transient (retryable) or permanent.

Transient errors are caused by external factors (network, rate limits,
credentials, timeouts) and should be retried on the next run. Permanent
errors are logic failures (invalid JSON, schema validation, environment
defects) that will not self-resolve.
"""

from __future__ import annotations

import re
from typing import Literal

ErrorType = Literal["transient", "permanent"]

# Exception class names that indicate transient failures.
_TRANSIENT_EXCEPTION_TYPES: frozenset[str] = frozenset({
    "ConnectionError",
    "TimeoutError",
    "OSError",
    "ConnectError",
    "ReadTimeout",
    "ConnectTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "CredentialUnavailableError",
    "AuthenticationError",
})

# Patterns in error messages that indicate transient failures.
_TRANSIENT_PATTERNS: re.Pattern[str] = re.compile(
    r"429"
    r"|rate.?limit"
    r"|too many requests"
    r"|connection refused"
    r"|timed?\s*out"
    r"|timeout"
    r"|ETIMEDOUT"
    r"|ECONNREFUSED"
    r"|ECONNRESET"
    r"|ENETUNREACH"
    r"|CredentialUnavailable"
    r"|AllEndpointsUnavailable"
    r"|All endpoints are unavailable"
    r"|401"
    r"|Unauthorized"
    r"|AuthenticationError"
    r"|server disconnected"
    r"|connection reset"
    r"|broken pipe",
    re.IGNORECASE,
)


def truncate_stderr(stderr: str, limit: int = 1000) -> str:
    """Keep head and tail of *stderr*, dropping the middle.

    Harness CLIs (Codex, Claude Code) print a lengthy startup banner to
    stderr before the actual error.  A naive ``stderr[:limit]`` discards
    the real error at the tail.  This helper keeps the first *head* chars
    (banner / diagnostics) and the last *tail* chars (actual error),
    joining them with ``" … "`` when truncation occurs.
    """
    if len(stderr) <= limit:
        return stderr
    head = limit // 5          # 20 % for banner
    tail = limit - head - 3    # 80 % for actual error, 3 for separator
    return stderr[:head] + "…\n" + stderr[-tail:]


def classify_error(error: str | Exception) -> ErrorType:
    """Classify an error as ``'transient'`` or ``'permanent'``.

    Accepts either an exception instance or an error message string
    (as stored in pipeline DB ``error`` columns).
    """
    if isinstance(error, Exception):
        # Check exception type hierarchy.
        for cls in type(error).__mro__:
            if cls.__name__ in _TRANSIENT_EXCEPTION_TYPES:
                return "transient"
        error_str = f"{type(error).__name__}: {error}"
    else:
        error_str = error

    # Check exception type name prefix (for DB-stored strings like
    # "ConnectionError: ...").
    colon_idx = error_str.find(":")
    if colon_idx > 0:
        type_name = error_str[:colon_idx].strip().rsplit(".", 1)[-1]
        if type_name in _TRANSIENT_EXCEPTION_TYPES:
            return "transient"

    # Pattern match on the full error string.
    if _TRANSIENT_PATTERNS.search(error_str):
        return "transient"

    return "permanent"


# ---------------------------------------------------------------------------
# Congestion signal detection (subset of transient, used by AIMD limiter)
# ---------------------------------------------------------------------------

_RATE_LIMIT_EXCEPTION_TYPES: frozenset[str] = frozenset({
    "TimeoutError",
    "ReadTimeout",
    "ConnectTimeout",
    "WriteTimeout",
    "PoolTimeout",
})

_RATE_LIMIT_PATTERNS: re.Pattern[str] = re.compile(
    r"429"
    r"|rate.?limit"
    r"|too many requests"
    r"|timed?\s*out"
    r"|timeout"
    r"|ETIMEDOUT",
    re.IGNORECASE,
)


def is_rate_limit_error(error: str | Exception) -> bool:
    """Return ``True`` if *error* is a congestion signal (429 / timeout).

    This is a narrower check than ``classify_error() == 'transient'``:
    connection failures, credential errors, and authentication errors are
    transient but do NOT indicate endpoint congestion.
    """
    if isinstance(error, Exception):
        for cls in type(error).__mro__:
            if cls.__name__ in _RATE_LIMIT_EXCEPTION_TYPES:
                return True
        error_str = f"{type(error).__name__}: {error}"
    else:
        error_str = error

    colon_idx = error_str.find(":")
    if colon_idx > 0:
        type_name = error_str[:colon_idx].strip().rsplit(".", 1)[-1]
        if type_name in _RATE_LIMIT_EXCEPTION_TYPES:
            return True

    return bool(_RATE_LIMIT_PATTERNS.search(error_str))
