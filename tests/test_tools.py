"""Tool-level behaviour, driven from fixtures through a mock transport."""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from faers import server as m
from conftest import Recorder, make_transport, meta_total, error_of
from faers import client as client_module


@pytest.fixture
def stub(monkeypatch, recorder):
    """Point the tools at a scripted transport instead of the network."""

    def install(handler, api_key: str = "") -> Recorder:
        c = client_module.OpenFdaClient(
            transport=make_transport(handler, recorder), api_key=api_key
        )
        monkeypatch.setattr(client_module, "_client", c)
        monkeypatch.setattr(m, "get_client", lambda: c)
        return recorder

    return install


def date_histogram(rows):
    """openFDA keys date buckets as 'time', not 'term'."""
    return {"meta": {}, "results": [{"time": t, "count": n} for t, n in rows]}


class TestTimeTrend:
    """Date counts key the bucket as 'time'. Reading 'term' returned an empty trend."""

    @pytest.mark.asyncio
    async def test_time_keyed_buckets_are_aggregated(self, stub):
        stub(lambda r: httpx.Response(200, json=date_histogram([
            ("20230104", 2), ("20230715", 3), ("20240101", 5),
        ])))
        out = await m.faers_time_trend(drug_name="X")

        assert out["yearly_trend"] == [{"year": "2023", "count": 5}, {"year": "2024", "count": 5}]
        assert out["total_reports"] == 10

    @pytest.mark.asyncio
    async def test_term_keyed_buckets_still_work(self, stub):
        stub(lambda r: httpx.Response(200, json={
            "meta": {}, "results": [{"term": "20230104", "count": 2}]
        }))
        out = await m.faers_time_trend(drug_name="X")
        assert out["yearly_trend"] == [{"year": "2023", "count": 2}]

    @pytest.mark.asyncio
    async def test_respects_the_keyless_limit_ceiling(self, stub):
        recorder = stub(lambda r: httpx.Response(200, json=date_histogram([("20230104", 1)])))
        await m.faers_time_trend(drug_name="X")

        assert recorder.requests[0].url.params["limit"] == "999"


class TestSearchCases:
    @pytest.mark.asyncio
    async def test_compact_by_default_and_declares_omissions(self, stub, icsr):
        stub(lambda r: httpx.Response(200, json={
            "meta": {"results": {"total": 1}}, "results": [icsr]
        }))
        out = await m.faers_search_cases(drug_name="EMPAGLIFLOZIN", limit=1
        )

        assert "fields_omitted" in out
        assert len(json.dumps(out["results"])) < len(json.dumps([icsr]))

    @pytest.mark.asyncio
    async def test_full_returns_the_record_and_no_omission_note(self, stub, icsr):
        stub(lambda r: httpx.Response(200, json={
            "meta": {"results": {"total": 1}}, "results": [icsr]
        }))
        out = await m.faers_search_cases(drug_name="EMPAGLIFLOZIN", limit=1, full=True
        )

        assert out["results"] == [icsr]
        assert "fields_omitted" not in out

    @pytest.mark.asyncio
    async def test_suspect_verified_filters_the_page(self, stub):
        concomitant_only = {
            "safetyreportid": "1",
            "patient": {"drug": [{
                "drugcharacterization": "2",
                "activesubstance": {"activesubstancename": "EMPAGLIFLOZIN"},
            }], "reaction": []},
        }
        stub(lambda r: httpx.Response(200, json={
            "meta": {"results": {"total": 1}}, "results": [concomitant_only]
        }))
        out = await m.faers_search_cases(
            drug_name="EMPAGLIFLOZIN", limit=1, role_basis="suspect_verified"
        )

        assert out["meta"]["total_any_role"] == 1
        assert out["meta"]["suspect_verified_in_page"] == 0
        assert out["results"] == []

    @pytest.mark.asyncio
    async def test_sort_and_query_are_reported(self, stub, icsr):
        recorder = stub(lambda r: httpx.Response(200, json={
            "meta": {"results": {"total": 1}}, "results": [icsr]
        }))
        out = await m.faers_search_cases(
            drug_name="EMPAGLIFLOZIN", events=["PANCREATITIS"], limit=1
        )

        assert recorder.requests[0].url.params["sort"] == "receivedate:desc"
        assert " AND " in out["effective_query"]
        assert "+AND+" not in out["effective_query"]


class TestStructuredFailures:
    @pytest.mark.asyncio
    async def test_suspect_basis_refused_on_count_tools(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(1)))
        with pytest.raises(ToolError) as exc:
            await m.faers_disproportionality(
                drug_name="X", events=["Y"], role_basis="suspect_verified"
            )
        err = error_of(exc)
        assert err["code"] == "not_computable"
        assert "faers_search_cases" in err["recovery"]

    @pytest.mark.asyncio
    async def test_skip_ceiling_surfaces_as_structured_error(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(1)))
        with pytest.raises(ToolError) as exc:
            await m.faers_search_cases(
                drug_name="X", skip=25_001
            )
        err = error_of(exc)
        assert err["code"] == "pagination_limit_reached"
        assert "date_from" in err["recovery"]

    @pytest.mark.asyncio
    async def test_missing_report_returns_recovery_advice(self, stub):
        stub(lambda r: httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}))
        with pytest.raises(ToolError) as exc:
            await m.faers_get_report(safetyreportid="99999999")
        err = error_of(exc)
        assert "safetyreportversion" in err["recovery"]


class TestReportIdPattern:
    r"""The old pattern was ^\d{7}-\d{1,2}$ and rejected every real id.

    Validation now lives in the tool's input schema, so it is checked there and
    through FastMCP's argument validation rather than on a direct call.
    """

    def _pattern(self):
        import asyncio
        tools = asyncio.run(m.mcp.list_tools())
        schema = {t.name: t for t in tools}["faers_get_report"].inputSchema
        return schema["properties"]["safetyreportid"]["pattern"]

    @pytest.mark.parametrize("rid", ["10084081", "10193585", "26702571", "1234567-1"])
    def test_real_ids_accepted_by_the_schema(self, rid):
        import re
        assert re.fullmatch(self._pattern(), rid)

    @pytest.mark.parametrize("bad", ["", "abc", "12345", "1234567-123"])
    def test_malformed_ids_rejected_by_the_schema(self, bad):
        import re
        assert not re.fullmatch(self._pattern(), bad)

    @pytest.mark.asyncio
    async def test_malformed_id_rejected_at_the_mcp_boundary(self, stub):
        """FastMCP validates arguments against the schema before the tool runs."""
        recorder = stub(lambda r: httpx.Response(200, json={}))
        with pytest.raises(ToolError):
            await m.mcp.call_tool("faers_get_report", {"safetyreportid": "abc"})
        assert not recorder.requests

    @pytest.mark.asyncio
    async def test_hyphenated_id_falls_back_to_base(self, stub, icsr):
        def handler(request):
            sid = request.url.params.get("search", "")
            if '"1234567"' in sid:
                return httpx.Response(200, json={"meta": {}, "results": [icsr]})
            return httpx.Response(404, json={})

        recorder = stub(handler)
        out = await m.faers_get_report(safetyreportid="1234567-1")

        assert out["ok"] is True
        assert out["matched_on"] == "base id without version suffix"
        assert len(recorder.requests) == 2


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_outcome_breakdown_is_one_round_trip(self, stub):
        def handler(request):
            if request.url.params.get("count"):
                return httpx.Response(200, json={"meta": {}, "results": [{"term": "1", "count": 5}]})
            return httpx.Response(200, json=meta_total(3))

        recorder = stub(handler)
        out = await m.faers_outcome_breakdown(drug_name="X")

        # 1 outcome aggregation + 6 seriousness criteria, all gathered.
        assert len(recorder.requests) == 7
        assert len(out["seriousness_criteria"]) == 6

    @pytest.mark.asyncio
    async def test_disproportionality_fetches_four_marginals(self, stub):
        recorder = stub(lambda r: httpx.Response(200, json=meta_total(100)))
        await m.faers_disproportionality(drug_name="X", events=["Y"])

        assert len(recorder.requests) == 4
        assert any(r.url.params.get("search") is None for r in recorder.requests), (
            "grand total N must be an unfiltered count"
        )


class TestLabelling:
    @pytest.mark.asyncio
    async def test_every_payload_carries_the_disclaimer(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(10)))
        out = await m.faers_case_counts(drug_name="X")

        assert "not de-duplicated" in out["disclaimer"]
        assert "any role" in out["role_basis_note"]

    @pytest.mark.asyncio
    async def test_coreported_tool_does_not_claim_co_suspect(self, stub):
        stub(lambda r: httpx.Response(200, json={
            "meta": {}, "results": [{"term": "ASPIRIN", "count": 10}]
        }))
        out = await m.faers_coreported_drugs(drug_name="X")

        assert "top_coreported_substances" in out
        assert "concomitant" in out["interpretation_note"]

    @pytest.mark.asyncio
    async def test_grouped_terms_are_flagged(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(500)))
        out = await m.faers_disproportionality(drug_name="X", events=["A", "B"]
        )

        assert "grouped_term_note" in out
