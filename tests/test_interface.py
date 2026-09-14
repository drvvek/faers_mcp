"""The MCP contract: what a client sees on the wire, not what Python sees.

These go through FastMCP's own request handlers so that argument validation,
error marking and structured content are exercised exactly as a client would.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mcp import types
from mcp.server.fastmcp.exceptions import ToolError

from conftest import Recorder, error_of, make_transport, meta_total
from faers import client as client_module
from faers import server as m
from faers.strata import AGE


async def call_on_the_wire(name: str, arguments: dict) -> types.CallToolResult:
    """Dispatch through the low-level handler, so isError / structuredContent are real."""
    req = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments)
    )
    handler = m.mcp._mcp_server.request_handlers[types.CallToolRequest]
    return (await handler(req)).root


@pytest.fixture
def stub(monkeypatch, recorder):
    def install(handler, api_key: str = "k") -> Recorder:
        c = client_module.OpenFdaClient(transport=make_transport(handler, recorder), api_key=api_key)
        monkeypatch.setattr(client_module, "_client", c)
        monkeypatch.setattr(m, "get_client", lambda: c)
        return recorder

    return install


class TestFlatArguments:
    """Critical #1: no `params` wrapper. Clients send drug_name at the top level."""

    @pytest.mark.asyncio
    async def test_no_tool_wraps_its_arguments(self):
        for tool in await m.mcp.list_tools():
            props = tool.inputSchema.get("properties", {})
            assert "params" not in props, tool.name
            assert "$ref" not in json.dumps(tool.inputSchema), tool.name

    @pytest.mark.asyncio
    async def test_flat_json_validates_and_runs(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(42)))
        res = await call_on_the_wire("faers_case_counts", {"drug_name": "X"})

        assert res.isError is False
        assert res.structuredContent["counts"]["total"] == 42

    @pytest.mark.asyncio
    async def test_wrapped_json_is_rejected(self, stub):
        """The old shape must not silently work either - the schema is the contract."""
        stub(lambda r: httpx.Response(200, json=meta_total(1)))
        res = await call_on_the_wire("faers_case_counts", {"params": {"drug_name": "X"}})
        assert res.isError is True

    @pytest.mark.asyncio
    async def test_describe_fields_needs_no_arguments(self):
        res = await call_on_the_wire("faers_describe_fields", {})
        assert res.isError is False
        assert "categories" in res.structuredContent

    @pytest.mark.asyncio
    async def test_enums_are_enforced_by_the_schema(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(1)))
        res = await call_on_the_wire("faers_disproportionality", {"drug_name": "X", "events": ["Y"], "stratify_by": "zodiac"})
        assert res.isError is True

    @pytest.mark.asyncio
    async def test_date_pattern_is_in_the_schema(self):
        tool = {t.name: t for t in await m.mcp.list_tools()}["faers_case_counts"]
        options = tool.inputSchema["properties"]["date_from"]["anyOf"]
        assert any(o.get("pattern") == r"^\d{8}$" for o in options)


class TestErrorsAreErrors:
    """Critical #2: a failure must be an MCP error result, not a successful text blob."""

    @pytest.mark.asyncio
    async def test_structured_failure_sets_is_error(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(1)))
        res = await call_on_the_wire("faers_search_cases", {"drug_name": "X", "skip": 30_000})

        assert res.isError is True
        text = res.content[0].text
        assert "pagination_limit_reached" in text
        assert "date_from" in text, "the recovery advice must survive onto the wire"

    @pytest.mark.asyncio
    async def test_direct_call_raises_tool_error_with_the_payload(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(1)))
        with pytest.raises(ToolError) as exc:
            await m.faers_case_counts(drug_name="X", role_basis="suspect_verified")
        err = error_of(exc)
        assert err["code"] == "not_computable"
        assert "faers_search_cases" in err["recovery"]

    @pytest.mark.asyncio
    async def test_no_success_payload_ever_carries_ok_false(self, stub):
        """`ok: false` as a success result was the old contract; it must be gone."""
        stub(lambda r: httpx.Response(404, json={}))
        res = await call_on_the_wire("faers_get_report", {"safetyreportid": "99999999"})
        assert res.isError is True


class TestStructuredContent:
    @pytest.mark.asyncio
    async def test_every_tool_declares_an_output_schema(self):
        for tool in await m.mcp.list_tools():
            assert tool.outputSchema is not None, tool.name

    @pytest.mark.asyncio
    async def test_structured_and_text_content_agree(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(7)))
        res = await call_on_the_wire("faers_case_counts", {"drug_name": "X"})
        assert json.loads(res.content[0].text) == res.structuredContent


class TestScreenRawFilter:
    """Critical #3: raw_filter was advertised and then dropped."""

    @pytest.mark.asyncio
    async def test_filter_reaches_every_marginal(self, monkeypatch, tmp_path, stub):
        monkeypatch.setenv("FAERS_CACHE_DIR", str(tmp_path))

        def handler(request):
            if request.url.params.get("count"):
                return httpx.Response(200, json={
                    "meta": {"last_updated": "u"},
                    "results": [{"term": "NAUSEA", "count": 5}],
                })
            return httpx.Response(200, json={"meta": {"last_updated": "u", "results": {"total": 1000}}, "results": []})

        recorder = stub(handler)
        out = await m.faers_signal_screen(term="X", top_n=5, min_cases=0, raw_filter="patient.patientsex:2")

        searches = [r.url.params.get("search") or "" for r in recorder.requests]
        assert searches, "no calls made"
        assert all("patient.patientsex:2" in s for s in searches), searches
        assert "patient.patientsex:2" in out["audit"]["restriction"]
        assert out["filters"]["raw_filter"] == "patient.patientsex:2"

    @pytest.mark.asyncio
    async def test_malformed_filter_rejected_before_any_call(self, monkeypatch, tmp_path, stub):
        monkeypatch.setenv("FAERS_CACHE_DIR", str(tmp_path))
        recorder = stub(lambda r: httpx.Response(200, json=meta_total(1)))
        with pytest.raises(ToolError):
            await m.faers_signal_screen(term="X", raw_filter="serious:1+AND+x:1")
        assert not recorder.requests


class TestDemographicAge:
    """patientagegroup covers ~18% of reports; onset-age bands cover ~55%."""

    @pytest.mark.asyncio
    async def test_reports_both_age_views_with_coverage(self, stub):
        def handler(request):
            count = request.url.params.get("count")
            if count == "patient.patientagegroup":
                rows = [{"term": "5", "count": 18}]
            elif count == AGE.count_field:
                rows = [{"term": "30", "count": 40}, {"term": "70", "count": 15}]
            elif count:
                rows = [{"term": "1", "count": 50}]
            else:
                return httpx.Response(200, json=meta_total(100))
            return httpx.Response(200, json={"meta": {}, "results": rows})

        recorder = stub(handler)
        out = await m.faers_demographic_profile(drug_name="X")

        assert out["total_reports"] == 100
        assert out["age_group_coded_coverage_percent"] == 18.0
        assert out["age_bands_from_onset_age"] == [{"label": "18-64", "count": 40}, {"label": "65+", "count": 15}]
        assert out["age_bands_coverage_percent"] == 55.0
        assert "stratify_by='age'" in out["age_note"]
        counted = [r.url.params.get("count") for r in recorder.requests if r.url.params.get("count")]
        assert "occurcountry.exact" in counted
        assert "occurcountry" not in counted


class TestServerInfo:
    def test_handshake_reports_faers_mcp_version(self):
        """FastMCP 1.x otherwise falls back to the mcp library version."""
        from faers import __version__

        opts = m.mcp._mcp_server.create_initialization_options()
        assert opts.server_version == __version__


class TestWarmCache:
    @pytest.mark.asyncio
    async def test_reports_cache_state(self, monkeypatch, tmp_path, stub):
        monkeypatch.setenv("FAERS_CACHE_DIR", str(tmp_path))
        from test_background import fake_openfda

        stub(fake_openfda)
        first = await m.faers_warm_cache(background_drugs=40, background_events=50)
        second = await m.faers_warm_cache(background_drugs=40, background_events=50)

        assert first["background"]["was_cached"] is False
        assert first["hyperparameters"]["was_cached"] is False
        assert second["background"]["was_cached"] is True
        assert second["hyperparameters"]["was_cached"] is True
        assert second["api_calls_this_request"] == 1


class TestPrompt:
    @pytest.mark.asyncio
    async def test_workup_prompt_is_registered_and_renders(self):
        names = [p.name for p in await m.mcp.list_prompts()]
        assert "faers_signal_workup" in names

        text = m.faers_signal_workup("EMPAGLIFLOZIN", "PANCREATITIS")
        for tool in ("faers_case_counts", "faers_disproportionality", "faers_ebgm", "faers_time_trend", "faers_search_cases"):
            assert tool in text
        assert "not de-duplicated" in text
        assert "signal detected" in text.lower()  # explicitly told not to emit one

    def test_drug_only_variant(self):
        text = m.faers_signal_workup("METFORMIN")
        assert "faers_signal_screen" in text
        assert "METFORMIN" in text


class TestRawSearchDrugName:
    @pytest.mark.asyncio
    async def test_annotates_without_filtering(self, stub):
        """The description says annotate-only; the behaviour must match it."""
        concomitant = {
            "safetyreportid": "1",
            "patient": {"drug": [{"drugcharacterization": "2", "activesubstance": {"activesubstancename": "X"}}], "reaction": []},
        }
        stub(lambda r: httpx.Response(200, json={"meta": {"results": {"total": 1}}, "results": [concomitant]}))
        out = await m.faers_raw_search(search="serious:1", drug_name="X")

        assert len(out["results"]) == 1, "must not filter"
        assert out["results"][0]["suspect_verified"] is False
