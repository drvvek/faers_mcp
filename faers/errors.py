"""Structured errors.

Every failure returns an object saying *why* it failed and *how* to recover,
rather than a bare {"error": "..."} string. The model can act on `recovery`;
a human can act on `reason`.
"""

from __future__ import annotations

from typing import Any


class FaersError(Exception):
    """Base for all recoverable, caller-facing failures."""

    code = "faers_error"

    def __init__(self, reason: str, recovery: str, **extra: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.recovery = recovery
        self.extra = extra

    def to_dict(self) -> dict:
        payload = {
            "ok": False,
            "error": {"code": self.code, "reason": self.reason, "recovery": self.recovery},
        }
        if self.extra:
            payload["error"]["context"] = self.extra
        return payload


class InvalidQuery(FaersError):
    code = "invalid_query"


class PaginationLimitReached(FaersError):
    """openFDA refuses skip > 25,000 — no amount of retrying helps."""

    code = "pagination_limit_reached"


class RateLimited(FaersError):
    code = "rate_limited"


class UpstreamError(FaersError):
    code = "upstream_error"


class NotComputable(FaersError):
    """The request is well-formed but the answer cannot be derived from openFDA.

    Used for the suspect-role basis on count tools: see docs/PLAN.md section 3.
    """

    code = "not_computable"
