"""Date windows, raw filters, raw search and the field catalogue."""

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
    def install(handler, api_key: str = "") -> Recorder:
        c = client_module.OpenFdaClient(
            transport=make_transport(handler, recorder), api_key=api_key
        )
        monkeypatch.setattr(client_module, "_client", c)
        monkeypatch.setattr(m, "get_client", lambda: c)
        return recorder

    return install


class TestDateWindow:
    @pytest.mark.asyncio
    async def test_window_reaches_the_query(self, stub):
        recorder = stub(lambda r: httpx.Response(200, json=meta_total(5)))
        out = await m.faers_case_counts(
            drug_name="X", date_from="20230101", date_to="20231231"
        )

        assert "receivedate:[20230101 TO 20231231]" in out["effective_query"]
        assert out["filters"]["date_window"] == "receivedate:[20230101 TO 20231231]"

    @pytest.mark.asyncio
    async def test_window_applies_to_the_grand_total_too(self, stub):
        """A 2x2 restricted on three cells but not N is not a contingency table."""
        recorder = stub(lambda r: httpx.Response(200, json=meta_total(50)))
        await m.faers_disproportionality(
            drug_name="X", events=["Y"], date_from="20230101", date_to="20231231"
        )

        searches = [r.url.params.get("search") for r in recorder.requests]
        assert len(searches) == 4
        assert all("receivedate:[20230101 TO 20231231]" in (s or "") for s in searches), (
            "every marginal, including N, must carry the window"
        )

    @pytest.mark.asyncio
    async def test_half_open_window_rejected(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(1)))
        with pytest.raises(ToolError) as exc:
            await m.faers_case_counts(
                drug_name="X", date_from="20230101"
            )
        err = error_of(exc)
        assert err["code"] == "invalid_query"

    @pytest.mark.asyncio
    async def test_no_window_means_no_clause(self, stub):
        recorder = stub(lambda r: httpx.Response(200, json=meta_total(5)))
        await m.faers_disproportionality(drug_name="X", events=["Y"])

        assert recorder.requests[0].url.params.get("search") is None, (
            "unrestricted N must be a bare count"
        )


class TestRawFilter:
    @pytest.mark.asyncio
    async def test_filter_is_anded_onto_the_preset(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(5)))
        out = await m.faers_case_counts(
            drug_name="X", raw_filter="patient.patientsex:2"
        )

        assert "AND (patient.patientsex:2)" in out["effective_query"]

    @pytest.mark.asyncio
    async def test_filter_stratifies_all_four_marginals(self, stub):
        recorder = stub(lambda r: httpx.Response(200, json=meta_total(50)))
        out = await m.faers_disproportionality(
            drug_name="X", events=["Y"], raw_filter="patient.patientsex:2"
        )

        searches = [r.url.params.get("search") for r in recorder.requests]
        assert all("patient.patientsex:2" in (s or "") for s in searches)
        assert "restriction_note" in out

    @pytest.mark.asyncio
    async def test_malformed_filter_rejected_before_the_call(self, stub):
        recorder = stub(lambda r: httpx.Response(200, json=meta_total(1)))
        with pytest.raises(ToolError) as exc:
            await m.faers_top_events(
                drug_name="X", raw_filter='patient.patientsex:"2'
            )
        err = error_of(exc)
        assert not recorder.requests

    @pytest.mark.asyncio
    async def test_plus_joined_filter_rejected(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(1)))
        with pytest.raises(ToolError) as exc:
            await m.faers_top_events(
                drug_name="X", raw_filter="serious:1+AND+patient.patientsex:2"
            )
        err = error_of(exc)
        assert "literal plus" in err["recovery"]


class TestRawSearch:
    @pytest.mark.asyncio
    async def test_passthrough_with_compact_projection(self, stub, icsr):
        stub(lambda r: httpx.Response(200, json={
            "meta": {"results": {"total": 1}}, "results": [icsr]
        }))
        out = await m.faers_raw_search(
            search="serious:1 AND patient.patientsex:2", limit=1
        )

        assert out["ok"] is True
        assert out["effective_query"] == "serious:1 AND patient.patientsex:2"
        assert "fields_omitted" in out
        assert len(json.dumps(out["results"])) < len(json.dumps([icsr]))

    @pytest.mark.asyncio
    async def test_count_mode_returns_rows_not_records(self, stub):
        stub(lambda r: httpx.Response(200, json={
            "meta": {}, "results": [{"term": "NAUSEA", "count": 10}]
        }))
        out = await m.faers_raw_search(
            search="serious:1", count="patient.reaction.reactionmeddrapt.exact"
        )

        assert out["results"] == [{"term": "NAUSEA", "count": 10}]
        assert "count_semantics" in out
        assert "fields_omitted" not in out

    @pytest.mark.asyncio
    async def test_unbalanced_query_never_leaves_the_process(self, stub):
        recorder = stub(lambda r: httpx.Response(200, json=meta_total(1)))
        with pytest.raises(ToolError) as exc:
            await m.faers_raw_search(search='(serious:1')
        err = error_of(exc)
        assert not recorder.requests

    @pytest.mark.asyncio
    async def test_skip_ceiling_enforced(self, stub):
        stub(lambda r: httpx.Response(200, json=meta_total(1)))
        with pytest.raises(ToolError) as exc:
            await m.faers_raw_search(search="serious:1", skip=30_000)
        assert error_of(exc)["code"] == "pagination_limit_reached"

    @pytest.mark.asyncio
    async def test_suspect_verification_available(self, stub):
        record = {
            "safetyreportid": "1",
            "patient": {"drug": [{
                "drugcharacterization": "1",
                "activesubstance": {"activesubstancename": "METFORMIN"},
            }], "reaction": []},
        }
        stub(lambda r: httpx.Response(200, json={"meta": {"results": {"total": 1}}, "results": [record]}))
        out = await m.faers_raw_search(
            search="serious:1", drug_name="METFORMIN"
        )

        assert out["results"][0]["suspect_verified"] is True


class TestDescribeFields:
    @pytest.mark.asyncio
    async def test_full_catalogue(self):
        out = await m.faers_describe_fields()

        assert set(out["categories"]) == {"drug", "reaction", "seriousness", "patient", "report"}
        assert out["case_sensitivity"]

    @pytest.mark.asyncio
    async def test_single_category(self):
        out = await m.faers_describe_fields(category="reaction")
        assert set(out["categories"]) == {"reaction"}

    @pytest.mark.asyncio
    async def test_unknown_category_lists_the_valid_ones(self):
        out = await m.faers_describe_fields(category="nonsense")
        assert "available_categories" in out

    @pytest.mark.asyncio
    async def test_documents_the_traps_that_produce_wrong_answers(self):
        out = await m.faers_describe_fields()
        blob = json.dumps(out)

        assert "Filters the REPORT, not the matched drug" in blob
        assert "case-SENSITIVE" in blob and "case-INSENSITIVE" in blob
        assert any("literal plus" in tip for tip in out["search_tips"])
        assert "keyed 'time'" in blob

    @pytest.mark.asyncio
    async def test_count_paths_carry_exact_where_appropriate(self):
        out = await m.faers_describe_fields(category="reaction")
        paths = {f["search_path"]: f["count_path"] for f in out["categories"]["reaction"]}

        assert paths["patient.reaction.reactionmeddrapt"].endswith(".exact")
        assert not paths["patient.reaction.reactionoutcome"].endswith(".exact")
