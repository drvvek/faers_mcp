"""The bulk signal screen, and the call-count claim that justifies it."""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from faers import server as m
from conftest import Recorder, make_transport, error_of
from faers import client as client_module
from faers.screen import MarginalCache, run_screen

PT_FIELD = "patient.reaction.reactionmeddrapt.exact"
SUBSTANCE_FIELD = "patient.drug.activesubstance.activesubstancename.exact"

# Drug mode: a drug reporting five PTs. Three appear in the global marginal
# table, two do not - those two need a per-term fallback lookup.
DRUG_PTS = [("NAUSEA", 100), ("PANCREATITIS", 60), ("RASH", 40), ("RARE PT A", 9), ("RARE PT B", 5)]
GLOBAL_PTS = [("NAUSEA", 500_000), ("PANCREATITIS", 52_226), ("RASH", 300_000)]

# Event mode: an event reported with three substances, one of them absent from
# the global substance table.
EVENT_DRUGS = [("METFORMIN", 200), ("EMPAGLIFLOZIN", 60), ("RARE DRUG", 4)]
GLOBAL_DRUGS = [("METFORMIN", 500_000), ("EMPAGLIFLOZIN", 67_251)]

FALLBACK_TOTALS = {"RARE PT A": 900, "RARE PT B": 400, "RARE DRUG": 800}

DRUG_TOTAL = 67_251
EVENT_TOTAL = 52_226
GRAND_TOTAL = 20_692_690


def fake_openfda(request: httpx.Request) -> httpx.Response:
    """A scripted openFDA that distinguishes the calls the screen makes."""
    params = request.url.params
    search = params.get("search")
    count = params.get("count")

    def rows(pairs):
        return httpx.Response(200, json={
            "meta": {"last_updated": "2026-09-01"},
            "results": [{"term": t, "count": c} for t, c in pairs],
        })

    def total(n):
        return httpx.Response(200, json={
            "meta": {"last_updated": "2026-09-01", "results": {"total": n}}, "results": [],
        })

    if count == PT_FIELD:
        # With a search it is the index drug's events; without, the global table.
        return rows(DRUG_PTS if search else GLOBAL_PTS)
    if count == SUBSTANCE_FIELD:
        return rows(EVENT_DRUGS if search else GLOBAL_DRUGS)

    if search is None:  # grand total N
        return total(GRAND_TOTAL)

    for name, n in FALLBACK_TOTALS.items():
        if f'"{name}"' in search:
            return total(n)

    # The index term's own total.
    return total(EVENT_TOTAL if "reactionmeddrapt" in search else DRUG_TOTAL)


@pytest.fixture
def screen_env(monkeypatch, tmp_path, recorder):
    """Isolated cache directory plus a scripted transport."""
    monkeypatch.setenv("FAERS_CACHE_DIR", str(tmp_path))
    c = client_module.OpenFdaClient(
        transport=make_transport(fake_openfda, recorder), api_key="k"
    )
    monkeypatch.setattr(client_module, "_client", c)
    monkeypatch.setattr(m, "get_client", lambda: c)
    return c, recorder, tmp_path


class TestCallEconomics:
    """The whole point: ~500 calls collapse to a handful."""

    @pytest.mark.asyncio
    async def test_global_marginals_replace_per_term_lookups(self, screen_env):
        client, recorder, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                  cache=MarginalCache(tmp))

        # 3 opening calls (N, drug total, drug's PTs) + 1 global + 2 fallbacks.
        assert result["audit"]["api_calls"] == 6
        assert result["audit"]["fallback_calls"] == 2
        assert len(recorder.requests) == 6

        # The extension would have issued one call per screened term, plus setup.
        assert result["audit"]["api_calls"] < len(DRUG_PTS) + 2

    @pytest.mark.asyncio
    async def test_cache_removes_the_global_call_on_a_second_run(self, screen_env):
        client, recorder, tmp = screen_env
        cache = MarginalCache(tmp)

        first = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0, cache=cache)
        assert first["audit"]["global_marginals_from_cache"] is False

        before = len(recorder.requests)
        second = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0, cache=cache)

        assert second["audit"]["global_marginals_from_cache"] is True
        assert len(recorder.requests) - before == 5, "the global call should be skipped"

    @pytest.mark.asyncio
    async def test_cache_is_keyed_on_data_currency(self, tmp_path):
        cache = MarginalCache(tmp_path)
        cache.put("f", "all", "2026-09-01", {"A": 1})

        assert cache.get("f", "all", "2026-09-01") == {"A": 1}
        assert cache.get("f", "all", "2026-12-01") is None, "a FAERS refresh must invalidate"
        assert cache.get("f", "receivedate:[x TO y]", "2026-09-01") is None, "a window is a different table"

    @pytest.mark.asyncio
    async def test_fallback_is_capped(self, screen_env):
        client, recorder, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                  max_fallback_calls=1, cache=MarginalCache(tmp))

        assert result["audit"]["fallback_calls"] == 1
        assert "fallback_truncated" in result["audit"]
        unavailable = [r for r in result["rows"] if "marginal_unavailable" in r.get("flags", [])]
        assert len(unavailable) == 1


class TestArithmetic:
    @pytest.mark.asyncio
    async def test_cells_match_a_hand_computed_2x2(self, screen_env):
        client, _, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                  cache=MarginalCache(tmp))

        row = next(r for r in result["rows"] if r["event"] == "PANCREATITIS")
        assert row["contingency_table"] == {
            "a": 60,
            "b": DRUG_TOTAL - 60,
            "c": 52_226 - 60,
            "d": GRAND_TOTAL - DRUG_TOTAL - 52_226 + 60,
        }

    @pytest.mark.asyncio
    async def test_event_mode_swaps_the_marginals(self, screen_env):
        """In event mode the fixed marginal is a+c (the event), not a+b (the drug)."""
        client, _, tmp = screen_env
        result = await run_screen(client, "event", "PANCREATITIS", top_n=5, min_cases=0,
                                  cache=MarginalCache(tmp))

        assert result["mode"] == "event"
        assert all("drug" in row for row in result["rows"])

        row = next(r for r in result["rows"] if r["drug"] == "EMPAGLIFLOZIN")
        assert row["contingency_table"] == {
            "a": 60,
            "b": DRUG_TOTAL - 60,          # per-row marginal, from the global drug table
            "c": EVENT_TOTAL - 60,         # fixed index marginal
            "d": GRAND_TOTAL - DRUG_TOTAL - EVENT_TOTAL + 60,
        }

    @pytest.mark.asyncio
    async def test_both_modes_agree_on_the_same_pair(self, screen_env):
        """The 2x2 for EMPAGLIFLOZIN x PANCREATITIS must not depend on the direction."""
        client, _, tmp = screen_env
        by_drug = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                   cache=MarginalCache(tmp))
        by_event = await run_screen(client, "event", "PANCREATITIS", top_n=5, min_cases=0,
                                    cache=MarginalCache(tmp))

        drug_row = next(r for r in by_drug["rows"] if r["event"] == "PANCREATITIS")
        event_row = next(r for r in by_event["rows"] if r["drug"] == "EMPAGLIFLOZIN")

        assert drug_row["contingency_table"] == event_row["contingency_table"]
        assert drug_row["ror"] == event_row["ror"]

    @pytest.mark.asyncio
    async def test_rows_carry_named_criteria_not_a_verdict(self, screen_env):
        client, _, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                  cache=MarginalCache(tmp))

        row = next(r for r in result["rows"] if r["event"] == "PANCREATITIS")
        assert set(row["criteria_met"]) == {"ema_ror", "evans_prr"}
        assert "signal_detected" not in row


class TestFiltersAndOrdering:
    @pytest.mark.asyncio
    async def test_min_cases_excludes_sparse_terms(self, screen_env):
        client, _, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=10,
                                  cache=MarginalCache(tmp))

        assert all(r["count"] >= 10 for r in result["rows"])
        assert "RARE PT B" not in [r["event"] for r in result["rows"]]

    @pytest.mark.asyncio
    async def test_sorted_by_ror_lower_descending(self, screen_env):
        client, _, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                  cache=MarginalCache(tmp))

        lowers = [r.get("ror_lower_95") or -1 for r in result["rows"]]
        assert lowers == sorted(lowers, reverse=True)

    @pytest.mark.asyncio
    async def test_sort_by_count(self, screen_env):
        client, _, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                  sort_by="count", cache=MarginalCache(tmp))

        counts = [r["count"] for r in result["rows"]]
        assert counts == sorted(counts, reverse=True)

    @pytest.mark.asyncio
    async def test_return_n_truncates_after_sorting(self, screen_env):
        client, _, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, return_n=2,
                                  min_cases=0, cache=MarginalCache(tmp))

        assert len(result["rows"]) == 2
        assert result["audit"]["rows_returned"] == 2

    @pytest.mark.asyncio
    async def test_signals_only(self, screen_env):
        client, _, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                  signals_only=True, cache=MarginalCache(tmp))

        assert all(r["criteria_met"]["ema_ror"] for r in result["rows"])


class TestAudit:
    @pytest.mark.asyncio
    async def test_audit_records_provenance(self, screen_env):
        client, _, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                  cache=MarginalCache(tmp))
        audit = result["audit"]

        for key in ("tool_version", "generated_utc", "query_term", "restriction",
                    "faers_last_updated", "grand_total_N", "api_calls", "method"):
            assert key in audit, key
        assert audit["faers_last_updated"] == "2026-09-01"
        assert audit["restriction"] == "none (all dates, no filter)"

    @pytest.mark.asyncio
    async def test_date_window_recorded_and_applied(self, screen_env):
        client, recorder, tmp = screen_env
        result = await run_screen(client, "drug", "EMPAGLIFLOZIN", top_n=5, min_cases=0,
                                  date_from="20230101", date_to="20231231",
                                  cache=MarginalCache(tmp))

        assert "20230101" in result["audit"]["restriction"]
        searches = [r.url.params.get("search") for r in recorder.requests]
        assert all("receivedate:[20230101 TO 20231231]" in (s or "") for s in searches), (
            "the window must reach the grand total and the global marginals too"
        )


class TestTool:
    @pytest.mark.asyncio
    async def test_tool_wraps_the_screen(self, screen_env):
        out = await m.faers_signal_screen(
            term="EMPAGLIFLOZIN", top_n=5, min_cases=0
        )

        assert out["ok"] is True
        assert out["rows"]
        assert "criteria_definitions" in out
        assert "not de-duplicated" in out["disclaimer"]

    @pytest.mark.asyncio
    async def test_bad_mode_rejected(self, screen_env):
        """Rejected by the schema at the MCP boundary, and by run_screen on a direct call."""
        with pytest.raises(ToolError) as exc:
            await m.faers_signal_screen(mode="sideways", term="X")
        assert error_of(exc)["code"] == "invalid_query"

        with pytest.raises(ToolError):
            await m.mcp.call_tool("faers_signal_screen", {"mode": "sideways", "term": "X"})

    @pytest.mark.asyncio
    async def test_no_terms_returns_a_clean_empty_result(self, monkeypatch, tmp_path, recorder):
        monkeypatch.setenv("FAERS_CACHE_DIR", str(tmp_path))

        def empty(request):
            if request.url.params.get("count"):
                return httpx.Response(200, json={"meta": {"last_updated": "x"}, "results": []})
            return httpx.Response(200, json={"meta": {"last_updated": "x", "results": {"total": 0}}, "results": []})

        c = client_module.OpenFdaClient(transport=make_transport(empty, recorder), api_key="k")
        monkeypatch.setattr(m, "get_client", lambda: c)

        out = await m.faers_signal_screen(term="NOSUCHDRUG")
        assert out["ok"] is True
        assert out["rows"] == []
        assert "No events found" in out["note"]
