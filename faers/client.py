"""Shared openFDA HTTP client.

Replaces the previous pattern of opening a fresh httpx.AsyncClient per call
(which happened 4-7 times per tool, sequentially). One client, one connection
pool, concurrent fan-out via gather(), retry with backoff, and hard enforcement
of openFDA's documented ceilings.

The API key travels in an HTTP Basic auth header (key as username, empty
password), not in the query string, so it stays out of URLs, proxy logs and
crash traces. This is the scheme used by the companion AEMS Chrome extension,
from which this server's calculations were originally derived.
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
from typing import Any, Iterable, Optional

import httpx

from .errors import PaginationLimitReached, RateLimited, UpstreamError

BASE_URL = "https://api.fda.gov/drug/event.json"

# openFDA ceilings.
MAX_SKIP = 25_000
MAX_LIMIT = 1_000
# limit >= 1000 is rejected outright without a key (verified: API_KEY_MISSING).
MAX_LIMIT_KEYLESS = 999

EMPTY = {"meta": {"results": {"total": 0, "skip": 0, "limit": 0}}, "results": []}


class OpenFdaClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.environ.get("OPENFDA_API_KEY", "")).strip()
        self.max_retries = max_retries
        headers = {"User-Agent": "faers-mcp/2.0"}
        if self.api_key:
            token = base64.b64encode(f"{self.api_key}:".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        # `transport` exists so tests can drive the client from recorded fixtures.
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers, transport=transport)

    @property
    def max_limit(self) -> int:
        return MAX_LIMIT if self.api_key else MAX_LIMIT_KEYLESS

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OpenFdaClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- core ----------------------------------------------------------------

    async def fetch(
        self,
        search: Optional[str] = None,
        count: Optional[str] = None,
        limit: Optional[int] = None,
        skip: int = 0,
        sort: Optional[str] = None,
    ) -> dict:
        """One openFDA call. Returns the parsed body, or EMPTY on a 404.

        openFDA uses 404 to mean "nothing matched", which is not an error
        condition for us.
        """
        if skip > MAX_SKIP:
            raise PaginationLimitReached(
                reason=f"skip={skip} exceeds openFDA's ceiling of {MAX_SKIP}.",
                recovery=(
                    "openFDA cannot page beyond 25,000 records. Narrow the query with "
                    "date_from/date_to, or aggregate with a count field instead of "
                    "paging through records."
                ),
                skip=skip,
                max_skip=MAX_SKIP,
            )

        if limit is not None and limit > self.max_limit:
            raise UpstreamError(
                reason=f"limit={limit} exceeds the maximum of {self.max_limit}.",
                recovery=(
                    "Lower the limit."
                    if self.api_key
                    else "Lower the limit to 999, or set OPENFDA_API_KEY to allow 1000."
                ),
            )

        params: dict[str, Any] = {}
        if search:
            params["search"] = search
        if count:
            params["count"] = count
        if limit is not None:
            params["limit"] = limit
        if skip:
            params["skip"] = skip
        if sort:
            params["sort"] = sort

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(BASE_URL, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    raise UpstreamError(
                        reason=f"Network failure after {self.max_retries + 1} attempts: {exc}",
                        recovery="Retry shortly, or narrow the query if it is very large.",
                    ) from exc
                await self._backoff(attempt)
                continue

            if response.status_code == 404:
                return dict(EMPTY)

            if response.status_code == 429:
                if attempt == self.max_retries:
                    raise RateLimited(
                        reason="openFDA rate limit exceeded.",
                        recovery=(
                            "Wait and retry."
                            if self.api_key
                            else "Set OPENFDA_API_KEY for 120,000 requests/day instead of 1,000."
                        ),
                    )
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            if response.status_code >= 500:
                if attempt == self.max_retries:
                    raise UpstreamError(
                        reason=f"openFDA returned {response.status_code} repeatedly.",
                        recovery="The API is having trouble; retry in a few minutes.",
                    )
                await self._backoff(attempt)
                continue

            if response.status_code == 400:
                raise UpstreamError(
                    reason=f"openFDA rejected the query: {response.text[:300]}",
                    recovery=(
                        "Check field paths with faers_describe_fields, and confirm the "
                        "query balances quotes and parentheses."
                    ),
                    effective_query=search,
                )

            if response.status_code != 200:
                raise UpstreamError(
                    reason=f"Unexpected status {response.status_code}: {response.text[:300]}",
                    recovery="Retry, or simplify the query.",
                )

            return response.json()

        raise UpstreamError(  # pragma: no cover - loop always returns or raises
            reason=f"Request failed: {last_exc}",
            recovery="Retry shortly.",
        )

    async def _backoff(self, attempt: int, retry_after: Optional[str] = None) -> None:
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), 60.0))
                return
            except (TypeError, ValueError):
                pass
        # Exponential with jitter, so concurrent fan-out does not retry in lockstep.
        await asyncio.sleep((2 ** attempt) + random.random())

    # -- convenience ---------------------------------------------------------

    async def total(self, search: Optional[str] = None) -> int:
        """Just the matching-record count. limit=1 is the cheapest way to ask."""
        data = await self.fetch(search=search, limit=1)
        return int(data.get("meta", {}).get("results", {}).get("total", 0) or 0)

    async def totals(self, searches: Iterable[Optional[str]]) -> list[int]:
        """Concurrent counts. This is what turns 4-7 sequential calls into one wait."""
        return list(await asyncio.gather(*(self.total(s) for s in searches)))

    async def counts(
        self, search: Optional[str], count_field: str, limit: int
    ) -> list[dict]:
        data = await self.fetch(search=search, count=count_field, limit=limit)
        return data.get("results", []) or []

    async def last_updated(self) -> Optional[str]:
        data = await self.fetch(limit=1)
        return data.get("meta", {}).get("last_updated")


# --- module-level singleton --------------------------------------------------

_client: Optional[OpenFdaClient] = None


def get_client() -> OpenFdaClient:
    global _client
    if _client is None:
        _client = OpenFdaClient()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
