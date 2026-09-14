"""Background table assembly, caching, and the faers_ebgm tool."""

from __future__ import annotations

import json

import httpx
import pytest

from faers import server as m
from conftest import make_transport
from faers import client as client_module
from faers.background import Background, build_background, fit_for_background
from faers.errors import InvalidQuery

PT_FIELD = "patient.reaction.reactionmeddrapt.exact"
SUBSTANCE_FIELD = "patient.drug.activesubstance.activesubstancename.exact"
SEX_FIELD = "patient.patientsex"

OBSERVED_CELL = 77  # what a drug-AND-event query returns

GRAND_TOTAL = 20_000_000
DRUGS = {f"DRUG{i:03d}": 50_000 - i * 300 for i in range(40)}
EVENTS = {f"EVENT{j:03d}": 400_000 - j * 6_000 for j in range(50)}

# Terms a drug reports but that are too rare to be in the global event universe -
# exactly where the real signals live (euglycaemic DKA, Fournier's gangrene).
RARE_EVENTS = {"RARE EVENT": 900}


def fake_openfda(request: httpx.Request) -> httpx.Response:
    """A background-shaped openFDA: global marginals, then one row per drug."""
    params = request.url.params
    search = params.get("search")
    count = params.get("count")

    limit = int(params.get("limit", 100))

    def rows(mapping):
        """Honour `limit` the way openFDA does - count rows come back ranked."""
        ranked = sorted(mapping.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return httpx.Response(200, json={
            "meta": {"last_updated": "2026-07-30"},
            "results": [{"term": t, "count": c} for t, c in ranked],
        })

    if count == "receivedate":
        # A date histogram keyed "time", with a negligible pre-2004 tail that
        # year resolution is expected to prune.
        buckets = [("19980101", 1), ("20030101", 2)] + [
            (f"{y}0101", 1_000_000) for y in range(2004, 2010)
        ]
        return httpx.Response(200, json={
            "meta": {"last_updated": "2026-07-30"},
            "results": [{"time": t, "count": c} for t, c in buckets],
        })

    if count == SEX_FIELD:
        # Stratum sizes / per-stratum marginals: a 40/60 male-female split.
        return httpx.Response(200, json={
            "meta": {"last_updated": "2026-07-30"},
            "results": [{"term": "1", "count": 4}, {"term": "2", "count": 6}],
        })

    if count == SUBSTANCE_FIELD:
        return rows(DRUGS)

    if count == PT_FIELD:
        if not search:
            return rows(EVENTS)
        # A drug's own events: a deterministic slice, so cells vary by drug.
        idx = int(search.split("DRUG")[1][:3]) if "DRUG" in search else 0
        cells = {
            f"EVENT{j:03d}": max(1, (idx * 7 + j * 13) % 400)
            for j in range(idx % 5, 50, 3)
        }
        return rows(cells)

    def total(n):
        return httpx.Response(200, json={
            "meta": {"last_updated": "2026-07-30", "results": {"total": n}}, "results": [],
        })

    if search is None:
        return total(GRAND_TOTAL)

    # A drug AND event query is an observed cell count, distinct from either
    # marginal - the tool must ask for it rather than assuming zero.
    if "reactionmeddrapt" in search and "activesubstance" in search:
        return total(OBSERVED_CELL)

    # An on-demand marginal lookup for a single PT: known terms have a count,
    # anything else genuinely does not exist.
    if "reactionmeddrapt" in search:
        for term, count in {**RARE_EVENTS, **EVENTS}.items():
            if f'"{term}"' in search:
                return total(count)
        return total(0)

    return total(12_345)


@pytest.fixture
def env(monkeypatch, tmp_path, recorder):
    monkeypatch.setenv("FAERS_CACHE_DIR", str(tmp_path))
    c = client_module.OpenFdaClient(
        transport=make_transport(fake_openfda, recorder), api_key="k"
    )
    monkeypatch.setattr(client_module, "_client", c)
    monkeypatch.setattr(m, "get_client", lambda: c)
    return c, recorder, tmp_path


class TestBuild:
    @pytest.mark.asyncio
    async def test_costs_three_plus_one_call_per_drug(self, env):
        client, recorder, _ = env
        bg, cached, calls = await build_background(client, n_drugs=40, n_events=50)

        assert cached is False
        assert calls == 3 + len(DRUGS)
        assert len(recorder.requests) == calls

    @pytest.mark.asyncio
    async def test_table_is_populated_and_consistent(self, env):
        client, _, _ = env
        bg, _, _ = await build_background(client, n_drugs=40, n_events=50)

        assert bg.grand_total == GRAND_TOTAL
        assert bg.last_updated == "2026-07-30"
        assert len(bg.drug_totals) == len(DRUGS)
        assert len(bg.event_totals) == len(EVENTS)
        assert bg.n_cells > 100

    @pytest.mark.asyncio
    async def test_cells_are_restricted_to_the_event_universe(self, env):
        """Every cell needs a marginal, or its expected count is undefined."""
        client, _, _ = env
        bg, _, _ = await build_background(client, n_drugs=40, n_events=50)

        for events in bg.cells.values():
            assert set(events).issubset(set(bg.event_totals))

    @pytest.mark.asyncio
    async def test_observed_pairs_are_finite_and_positive(self, env):
        client, _, _ = env
        bg, _, _ = await build_background(client, n_drugs=40, n_events=50)
        n, e = bg.observed_pairs()

        assert len(n) == len(e) == bg.n_cells
        assert (e > 0).all()
        assert (n >= 1).all()


class TestCaching:
    @pytest.mark.asyncio
    async def test_second_build_costs_one_call(self, env):
        """Only the meta lookup that establishes data currency."""
        client, recorder, _ = env
        await build_background(client, n_drugs=40, n_events=50)
        before = len(recorder.requests)

        bg, cached, calls = await build_background(client, n_drugs=40, n_events=50)

        assert cached is True
        assert calls == 1
        assert len(recorder.requests) - before == 1

    @pytest.mark.asyncio
    async def test_different_shape_is_a_different_cache_entry(self, env):
        client, recorder, _ = env
        await build_background(client, n_drugs=40, n_events=50)
        before = len(recorder.requests)

        _, cached, _ = await build_background(client, n_drugs=20, n_events=50)
        assert cached is False
        assert len(recorder.requests) - before > 1

    @pytest.mark.asyncio
    async def test_hyperparameters_are_cached(self, env):
        client, _, _ = env
        bg, _, _ = await build_background(client, n_drugs=40, n_events=50)

        first, cached_first = fit_for_background(bg, 40, 50)
        second, cached_second = fit_for_background(bg, 40, 50)

        assert cached_first is False and cached_second is True
        assert second.theta == pytest.approx(
            tuple(round(v, 6) for v in first.theta), rel=1e-6
        )

    @pytest.mark.asyncio
    async def test_corrupt_cache_falls_back_to_rebuilding(self, env, tmp_path):
        client, _, _ = env
        await build_background(client, n_drugs=40, n_events=50)
        for path in tmp_path.glob("background_*.json"):
            path.write_text("{ not json")

        bg, cached, _ = await build_background(client, n_drugs=40, n_events=50)
        assert cached is False
        assert bg.n_cells > 0

    def test_round_trips_through_json(self):
        bg = Background(
            drug_totals={"A": 10}, event_totals={"X": 20}, cells={"A": {"X": 5}},
            grand_total=1000, last_updated="u", window="all", built_at="t",
        )
        assert Background.from_json(bg.to_json()).cells == bg.cells


class TestTool:
    @pytest.mark.asyncio
    async def test_scores_a_drug_in_the_background(self, env):
        out = await m.faers_ebgm(
            drug_name="DRUG001", top_n=5, min_cases=0,
            background_drugs=40, background_events=50,
        )

        assert out["ok"] is True
        assert out["rows"]
        row = out["rows"][0]
        for key in ("event", "observed", "expected", "rrr", "ebgm", "eb05", "eb95"):
            assert key in row, key
        assert row["eb05"] <= row["ebgm"] <= row["eb95"]

    @pytest.mark.asyncio
    async def test_rows_sorted_by_eb05(self, env):
        out = await m.faers_ebgm(
            drug_name="DRUG002", top_n=10, min_cases=0,
            background_drugs=40, background_events=50,
        )
        scores = [r["eb05"] for r in out["rows"]]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_reports_hyperparameters_and_provenance(self, env):
        out = await m.faers_ebgm(
            drug_name="DRUG003", top_n=3, background_drugs=40, background_events=50,
        )

        assert set(out["hyperparameters"]) >= {"a1", "b1", "a2", "b2", "p", "converged"}
        assert out["background"]["grand_total_N"] == GRAND_TOTAL
        assert out["background"]["faers_last_updated"] == "2026-07-30"

    @pytest.mark.asyncio
    async def test_declares_that_it_is_unstratified(self, env):
        """The caveat that stops these being read as FDA's published EBGMs."""
        out = await m.faers_ebgm(
            drug_name="DRUG004", top_n=1, background_drugs=40, background_events=50,
        )
        caveats = " ".join(out["method_caveats"])

        assert "UNSTRATIFIED" in caveats
        assert "any-role" in caveats
        assert "Zero-truncated" in caveats

    @pytest.mark.asyncio
    async def test_drug_outside_the_background_costs_two_extra_calls(self, env):
        out = await m.faers_ebgm(
            drug_name="DRUG001", top_n=3, background_drugs=20, background_events=50,
        )
        # DRUG001 is inside a 20-drug background, so pick one that is not.
        assert out["ok"] is True

        out2 = await m.faers_ebgm(
            drug_name="DRUG039", top_n=3, background_drugs=20, background_events=50,
        )
        assert out2["ok"] is True
        assert out2["cost"]["api_calls_this_request"] >= 2

    @pytest.mark.asyncio
    async def test_min_cases_filters(self, env):
        out = await m.faers_ebgm(
            drug_name="DRUG005", top_n=50, min_cases=100,
            background_drugs=40, background_events=50,
        )
        assert all(r["observed"] >= 100 for r in out["rows"])

    @pytest.mark.asyncio
    async def test_explicit_events_are_honoured(self, env):
        out = await m.faers_ebgm(
            drug_name="DRUG006", events=["EVENT000", "EVENT003"], min_cases=0,
            background_drugs=40, background_events=50,
        )
        assert {r["event"] for r in out["rows"]} <= {"EVENT000", "EVENT003"}

    @pytest.mark.asyncio
    async def test_unknown_event_flagged_rather_than_scored(self, env):
        out = await m.faers_ebgm(
            drug_name="DRUG007", events=["NOT A REAL PT"], min_cases=0,
            background_drugs=40, background_events=50,
        )
        row = out["rows"][0]
        assert row["ebgm"] is None
        assert "event_marginal_unavailable" in row["flags"]

    @pytest.mark.asyncio
    async def test_rare_event_marginal_is_fetched_on_demand(self, env):
        """The background's event universe is the globally commonest terms.

        A drug-specific signal (euglycaemic DKA, Fournier's gangrene) falls
        outside it and must not simply be dropped.
        """
        out = await m.faers_ebgm(
            drug_name="DRUG008", events=["RARE EVENT"], min_cases=0,
            background_drugs=40, background_events=50,
        )

        row = out["rows"][0]
        assert row["event"] == "RARE EVENT"
        assert row["ebgm"] is not None
        assert "marginal_fetched_on_demand" in row["flags"]
        assert out["cost"]["api_calls_this_request"] >= 1

    @pytest.mark.asyncio
    async def test_second_call_reports_cache_hits(self, env):
        kwargs = dict(top_n=3, background_drugs=40, background_events=50)
        await m.faers_ebgm(drug_name="DRUG008", **kwargs)
        out = await m.faers_ebgm(drug_name="DRUG009", **kwargs)

        assert out["cost"]["background_from_cache"] is True
        assert out["cost"]["hyperparameters_from_cache"] is True


class TestStratifiedEbgm:
    """Stratification enters MGPS only through E; the prior is refitted on it."""

    @pytest.mark.asyncio
    async def test_stratified_expectation_differs_from_crude(self, env):
        from faers.background import build_background, build_stratified_marginals
        from faers.strata import SEX

        client, _, _ = env
        bg, _, _ = await build_background(client, n_drugs=40, n_events=50)
        sm, cached, calls = await build_stratified_marginals(
            client, SEX, n_drugs=40, n_events=50
        )

        assert cached is False
        assert set(sm.n_k) == {"male", "female"}
        # 1 meta + 1 stratum-size call + 2 strata x 2 fields.
        assert calls == 1 + 1 + 4

        drug = next(iter(bg.cells))
        event = next(iter(bg.cells[drug]))
        assert sm.expected(drug, event) > 0

    @pytest.mark.asyncio
    async def test_prior_is_refitted_for_stratified_expectations(self, env):
        from faers.background import build_background, build_stratified_marginals, fit_for_background
        from faers.strata import SEX

        client, _, _ = env
        bg, _, _ = await build_background(client, n_drugs=40, n_events=50)
        sm, _, _ = await build_stratified_marginals(client, SEX, n_drugs=40, n_events=50)

        crude, _ = fit_for_background(bg, 40, 50)
        strat, _ = fit_for_background(bg, 40, 50, stratified=sm)

        assert crude.theta != strat.theta, "a different E must give a different prior"

    @pytest.mark.asyncio
    async def test_stratified_marginals_are_cached(self, env):
        from faers.background import build_stratified_marginals
        from faers.strata import SEX

        client, recorder, _ = env
        await build_stratified_marginals(client, SEX, n_drugs=40, n_events=50)
        before = len(recorder.requests)
        _, cached, calls = await build_stratified_marginals(client, SEX, n_drugs=40, n_events=50)

        assert cached is True
        assert calls == 1
        assert len(recorder.requests) - before == 1

    @pytest.mark.asyncio
    async def test_tool_reports_stratification_block(self, env):
        out = await m.faers_ebgm(
            drug_name="DRUG001", top_n=5, min_cases=0,
            background_drugs=40, background_events=50, stratify_by="sex",
        )

        assert out["ok"] is True
        st = out["stratification"]
        assert st["stratify_by"] == "sex"
        assert sorted(st["strata"]) == ["female", "male"]
        assert st["percent_of_database"] is not None
        assert "Stratified by sex" in " ".join(out["method_caveats"])

    @pytest.mark.asyncio
    async def test_year_strata_are_resolved_from_the_data(self, env):
        """Year labels are not knowable in advance, so they are measured."""
        from faers.background import build_stratified_marginals
        from faers.strata import YEAR

        client, _, _ = env
        sm, _, calls = await build_stratified_marginals(client, YEAR, n_drugs=40, n_events=50)

        assert set(sm.n_k) == {str(y) for y in range(2004, 2010)}
        assert sm.resolution["resolution"] == "measured from the data"

    @pytest.mark.asyncio
    async def test_negligible_years_are_pruned(self, env):
        """FAERS carries a pre-2004 tail worth 0.002% that costs 2 calls a year."""
        from faers.background import build_stratified_marginals
        from faers.strata import YEAR

        client, _, _ = env
        sm, _, _ = await build_stratified_marginals(client, YEAR, n_drugs=40, n_events=50)

        assert sm.resolution["years_dropped"] == ["1998", "2003"]
        assert sm.resolution["reports_dropped"] == 3
        assert "1998" not in sm.n_k

    @pytest.mark.asyncio
    async def test_resolution_call_doubles_as_the_stratum_sizes(self, env):
        """Resolving year already counts every stratum; do not count them twice."""
        from faers.background import build_stratified_marginals
        from faers.strata import YEAR

        client, _, _ = env
        sm, _, calls = await build_stratified_marginals(client, YEAR, n_drugs=40, n_events=50)

        n_strata = len(sm.n_k)
        # 1 meta + 1 resolve (which also gives sizes) + 2 field counts per stratum.
        assert calls == 1 + 1 + 2 * n_strata
        assert sum(sm.n_k.values()) == 6_000_000

    @pytest.mark.asyncio
    async def test_too_many_strata_is_refused_with_a_way_out(self, env):
        from faers.strata import YEAR, resolve_strata

        client, _, _ = env
        with pytest.raises(InvalidQuery) as exc:
            await resolve_strata(client, YEAR, None, max_strata=2)
        assert "date_from" in exc.value.recovery

    @pytest.mark.asyncio
    async def test_tool_accepts_year_and_reports_resolution(self, env):
        out = await m.faers_ebgm(
            drug_name="DRUG001", top_n=3, min_cases=0, background_drugs=40,
            background_events=50, stratify_by="year",
        )

        assert out["ok"] is True
        st = out["stratification"]
        assert st["stratify_by"] == "year"
        assert st["strata_resolution"]["years_dropped"] == ["1998", "2003"]

    @pytest.mark.asyncio
    async def test_requested_event_absent_from_the_cell_row_is_measured(self, env):
        """The background row is filtered to its event universe.

        Assuming zero for anything outside it reported 'no cases' for pairs that
        have them - openFDA is asked instead.
        """
        out = await m.faers_ebgm(
            drug_name="DRUG001", events=["RARE EVENT"], min_cases=0,
            background_drugs=40, background_events=50,
        )

        row = out["rows"][0]
        assert row["observed"] == OBSERVED_CELL, "must be measured, not assumed to be 0"
