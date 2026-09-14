"""Client behaviour: 404, 429, 5xx, ceilings, auth and concurrency."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from conftest import Recorder, load, make_transport, meta_total
from faers.client import MAX_SKIP, OpenFdaClient
from faers.errors import PaginationLimitReached, RateLimited, UpstreamError


def client_with(handler, recorder: Recorder, **kwargs) -> OpenFdaClient:
    return OpenFdaClient(transport=make_transport(handler, recorder), **kwargs)


@pytest.mark.asyncio
async def test_404_is_an_empty_result_not_an_error(recorder):
    """openFDA uses 404 for 'nothing matched'. That is data, not a failure."""
    fixture = load("not_found_404.json")
    assert fixture["status"] == 404

    async with client_with(
        lambda r: httpx.Response(404, json=fixture["body"]), recorder, api_key=""
    ) as client:
        assert await client.total('x:"NOTAREALDRUGXYZ"') == 0


@pytest.mark.asyncio
async def test_429_retries_then_succeeds(recorder):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=meta_total(42))

    async with client_with(handler, recorder, api_key="", max_retries=3) as client:
        assert await client.total("anything") == 42
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_429_eventually_raises_structured_error(recorder):
    async with client_with(
        lambda r: httpx.Response(429, headers={"Retry-After": "0"}), recorder,
        api_key="", max_retries=1,
    ) as client:
        with pytest.raises(RateLimited) as exc:
            await client.total("anything")
    assert "OPENFDA_API_KEY" in exc.value.recovery
    assert exc.value.to_dict()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_500_retries_then_raises(recorder):
    async with client_with(
        lambda r: httpx.Response(503, text="upstream down"), recorder,
        api_key="", max_retries=1,
    ) as client:
        with pytest.raises(UpstreamError):
            await client.total("anything")
    assert len(recorder.requests) == 2


@pytest.mark.asyncio
async def test_400_is_not_retried_and_reports_the_query(recorder):
    async with client_with(
        lambda r: httpx.Response(400, text="bad syntax"), recorder, api_key="",
    ) as client:
        with pytest.raises(UpstreamError) as exc:
            await client.total('broken:"')
    assert len(recorder.requests) == 1
    assert exc.value.extra["effective_query"] == 'broken:"'


@pytest.mark.asyncio
async def test_skip_ceiling_is_refused_before_the_call(recorder):
    async with client_with(lambda r: httpx.Response(200, json=meta_total(1)), recorder) as client:
        with pytest.raises(PaginationLimitReached) as exc:
            await client.fetch(search="x", skip=MAX_SKIP + 1)
    assert not recorder.requests, "no request should be sent once the ceiling is known"
    assert exc.value.extra["max_skip"] == MAX_SKIP


@pytest.mark.asyncio
async def test_keyless_limit_ceiling_is_999(recorder):
    """Verified live: limit >= 1000 without a key fails with API_KEY_MISSING."""
    async with client_with(lambda r: httpx.Response(200, json=meta_total(1)), recorder, api_key="") as client:
        assert client.max_limit == 999
        with pytest.raises(UpstreamError) as exc:
            await client.fetch(count="x", limit=1000)
    assert "OPENFDA_API_KEY" in exc.value.recovery


@pytest.mark.asyncio
async def test_keyed_limit_ceiling_is_1000(recorder):
    async with client_with(lambda r: httpx.Response(200, json=meta_total(1)), recorder, api_key="k") as client:
        assert client.max_limit == 1000
        await client.fetch(count="x", limit=1000)


@pytest.mark.asyncio
async def test_api_key_travels_in_the_auth_header_not_the_url(recorder):
    """The key must never reach a URL, proxy log or crash trace."""
    async with client_with(lambda r: httpx.Response(200, json=meta_total(0)), recorder, api_key="SECRET") as client:
        await client.total("x")

    request = recorder.requests[0]
    assert "SECRET" not in str(request.url)
    assert "api_key" not in str(request.url)
    expected = base64.b64encode(b"SECRET:").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_totals_are_issued_concurrently(recorder):
    """Four marginals must be one round trip, not four sequential waits."""
    in_flight = {"now": 0, "peak": 0}

    async def handler_async():
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(0.02)
        in_flight["now"] -= 1

    class ConcurrentTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            recorder.requests.append(request)
            await handler_async()
            return httpx.Response(200, json=meta_total(7), request=request)

    async with OpenFdaClient(transport=ConcurrentTransport(), api_key="") as client:
        totals = await client.totals([None, "a", "b", "c"])

    assert totals == [7, 7, 7, 7]
    assert in_flight["peak"] == 4, "all four marginals should be in flight together"
