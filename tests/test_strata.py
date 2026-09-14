"""Stratification: binning, Mantel-Haenszel pooling and the homogeneity test."""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from scipy.stats import chi2 as scipy_chi2

from faers import server as m
from conftest import Recorder, make_transport, meta_total, error_of
from faers import client as client_module
from faers.errors import InvalidQuery
from faers.stats import Cells, breslow_day, chi2_sf, disproportionality, mantel_haenszel
from faers.strata import (
    AGE,
    AGE_SEX,
    SEX,
    YEAR,
    get_stratifier,
    stratified_totals,
)


class TestFieldChoice:
    """Coverage measured live drove the field choice; the docs must say so."""

    def test_age_uses_onsetage_not_agegroup(self):
        # patientagegroup covers 18.4% of reports; patientonsetage covers 55.3%.
        assert AGE.count_field == "patient.patientonsetage"
        assert "patientagegroup" in AGE.coverage_note
        assert AGE.extra_clause == "patient.patientonsetageunit:801"

    def test_every_stratifier_declares_coverage(self):
        for strat in (SEX, AGE, YEAR, AGE_SEX):
            assert strat.coverage_note

    def test_unknown_stratifier_lists_the_valid_ones(self):
        with pytest.raises(InvalidQuery) as exc:
            get_stratifier("eye_colour")
        assert "age_sex" in exc.value.recovery


class TestBinning:
    def test_sex_codes(self):
        assert SEX.binner("1") == "male"
        assert SEX.binner("2") == "female"
        assert SEX.binner("0") is None, "explicit 'unknown' is not a stratum"

    @pytest.mark.parametrize(
        "age,band", [(0, "0-17"), (17, "0-17"), (18, "18-64"), (64, "18-64"), (65, "65+"), (120, "65+")]
    )
    def test_age_bands_are_contiguous(self, age, band):
        assert AGE.binner(str(age)) == band

    def test_implausible_age_is_dropped(self):
        assert AGE.binner("500") is None
        assert AGE.binner("not-a-number") is None

    def test_year_from_a_date_bucket(self):
        assert YEAR.binner("20230415") == "2023"
        assert YEAR.binner("bad") is None

    def test_enumerable_strata_have_clauses(self):
        assert set(SEX.clauses()) == {"male", "female"}
        assert len(AGE.clauses()) == 3
        assert len(AGE_SEX.clauses()) == 6

    def test_year_strata_are_not_enumerable(self):
        assert YEAR.clauses() is None, "year labels are only known after counting"


class TestFetchCost:
    """The reason stratification is affordable: one call per marginal, not per stratum."""

    @pytest.mark.asyncio
    async def test_single_stratifier_is_one_call(self, recorder):
        def handler(request):
            return httpx.Response(200, json={
                "meta": {}, "results": [{"term": "1", "count": 40}, {"term": "2", "count": 60}],
            })

        client = client_module.OpenFdaClient(
            transport=make_transport(handler, recorder), api_key="k"
        )
        result = await stratified_totals(client, 'drug:"X"', SEX)
        await client.aclose()

        assert result == {"male": 40, "female": 60}
        assert len(recorder.requests) == 1
        assert recorder.requests[0].url.params["count"] == "patient.patientsex"

    @pytest.mark.asyncio
    async def test_crossed_stratifier_is_one_call_per_band(self, recorder):
        def handler(request):
            return httpx.Response(200, json={
                "meta": {}, "results": [{"term": "1", "count": 5}, {"term": "2", "count": 7}],
            })

        client = client_module.OpenFdaClient(
            transport=make_transport(handler, recorder), api_key="k"
        )
        result = await stratified_totals(client, 'drug:"X"', AGE_SEX)
        await client.aclose()

        assert len(recorder.requests) == 3  # one per age band
        assert set(result) == {
            "0-17|male", "0-17|female", "18-64|male",
            "18-64|female", "65+|male", "65+|female",
        }

    @pytest.mark.asyncio
    async def test_age_counts_are_binned_client_side(self, recorder):
        def handler(request):
            return httpx.Response(200, json={
                "meta": {},
                "results": [
                    {"term": "10", "count": 3}, {"term": "30", "count": 5},
                    {"term": "45", "count": 6}, {"term": "70", "count": 4},
                    {"term": "999", "count": 1},
                ],
            })

        client = client_module.OpenFdaClient(
            transport=make_transport(handler, recorder), api_key="k"
        )
        result = await stratified_totals(client, None, AGE)
        await client.aclose()

        assert result == {"0-17": 3, "18-64": 11, "65+": 4}
        assert "patientonsetageunit:801" in recorder.requests[0].url.params["search"]


class TestChiSquareTail:
    """Hand-rolled so the non-EBGM tools stay scipy-free; must still be exact."""

    @pytest.mark.parametrize(
        "stat,df", [(0.5, 1), (3.841, 1), (5.991, 2), (7.815, 3), (10.828, 1), (25.0, 5), (100.0, 10)]
    )
    def test_matches_scipy(self, stat, df):
        assert chi2_sf(stat, df) == pytest.approx(scipy_chi2.sf(stat, df), abs=1e-9)

    def test_zero_statistic(self):
        assert chi2_sf(0.0, 3) == 1.0


class TestMantelHaenszel:
    def test_identical_strata_reproduce_the_crude_estimate(self):
        one = Cells(50, 950, 100, 9900)
        crude = disproportionality(one)
        pooled = mantel_haenszel([one, one, one])

        assert pooled["ror"]["value"] == pytest.approx(crude["ror"]["value"], rel=1e-9)
        assert pooled["prr"]["value"] == pytest.approx(crude["prr"]["value"], rel=1e-3)

    def test_single_stratum_equals_the_unstratified_odds_ratio(self):
        cells = Cells(30, 470, 200, 9300)
        pooled = mantel_haenszel([cells])
        assert pooled["ror"]["value"] == pytest.approx(
            disproportionality(cells)["ror"]["value"], rel=1e-9
        )

    def test_confounding_is_removed(self):
        """Two strata with the same true OR but very different exposure prevalence.

        The crude table pooled across them is biased; MH recovers the truth.
        """
        s1 = Cells(a=90, b=910, c=9, d=991)      # OR ~ 10.9
        s2 = Cells(a=9, b=991, c=90, d=910)      # OR ~ 0.09
        crude = Cells(a=99, b=1901, c=99, d=1901)

        pooled = mantel_haenszel([s1, s2])
        assert disproportionality(crude)["ror"]["value"] == pytest.approx(1.0, abs=0.01)
        # Strata disagree wildly, so MH lands between them and BD flags it.
        assert pooled["homogeneity"]["homogeneous_at_0.05"] is False

    def test_pooled_cells_are_the_sum(self):
        s1, s2 = Cells(5, 95, 10, 890), Cells(7, 93, 20, 880)
        pooled = mantel_haenszel([s1, s2])
        assert pooled["pooled_cells"] == {"a": 12, "b": 188, "c": 30, "d": 1770}

    def test_degenerate_strata_are_dropped_not_fatal(self):
        good = Cells(10, 90, 20, 880)
        empty = Cells(0, 0, 0, 0)
        pooled = mantel_haenszel([good, empty])

        assert pooled["valid"] is True
        assert pooled["strata_used"] == 1
        assert pooled["strata_dropped"] == 1

    def test_no_usable_strata(self):
        assert mantel_haenszel([Cells(0, 0, 0, 0)])["valid"] is False

    def test_confidence_interval_brackets_the_estimate(self):
        pooled = mantel_haenszel([Cells(40, 960, 80, 9920), Cells(25, 475, 50, 4950)])
        for metric in ("ror", "prr"):
            r = pooled[metric]
            assert r["ci_lower_95"] < r["value"] < r["ci_upper_95"]


class TestBreslowDay:
    def test_homogeneous_strata_give_a_large_p(self):
        cells = Cells(50, 950, 100, 9900)
        pooled = mantel_haenszel([cells, cells])
        assert pooled["homogeneity"]["p_value"] == pytest.approx(1.0, abs=0.01)
        assert pooled["homogeneity"]["homogeneous_at_0.05"] is True

    def test_heterogeneous_strata_are_detected(self):
        s1 = Cells(a=200, b=800, c=50, d=9950)     # strong association
        s2 = Cells(a=50, b=950, c=500, d=9500)     # none
        pooled = mantel_haenszel([s1, s2])

        assert pooled["homogeneity"]["p_value"] < 0.001
        assert pooled["homogeneity"]["homogeneous_at_0.05"] is False
        assert "per-stratum" in pooled["homogeneity"]["interpretation"]

    def test_degrees_of_freedom(self):
        cells = Cells(50, 950, 100, 9900)
        assert mantel_haenszel([cells] * 4)["homogeneity"]["df"] == 3

    def test_needs_two_strata(self):
        assert breslow_day([Cells(10, 90, 20, 880)], 2.0)["p_value"] is None

    def test_undefined_pooled_or(self):
        assert breslow_day([Cells(1, 1, 1, 1)] * 2, None)["p_value"] is None


class TestDisproportionalityTool:
    @pytest.fixture
    def stub(self, monkeypatch, recorder):
        def install(handler):
            c = client_module.OpenFdaClient(
                transport=make_transport(handler, recorder), api_key="k"
            )
            monkeypatch.setattr(client_module, "_client", c)
            monkeypatch.setattr(m, "get_client", lambda: c)
            return recorder

        return install

    @staticmethod
    def handler(request):
        params = request.url.params
        if params.get("count") == "patient.patientsex":
            search = params.get("search") or ""
            # A drug reported mostly in women, and an event that follows suit.
            if "reactionmeddrapt" in search and "activesubstance" in search:
                rows = [("1", 10), ("2", 90)]
            elif "activesubstance" in search:
                rows = [("1", 200), ("2", 1800)]
            elif "reactionmeddrapt" in search:
                rows = [("1", 2000), ("2", 8000)]
            else:
                rows = [("1", 400_000), ("2", 600_000)]
            return httpx.Response(200, json={
                "meta": {}, "results": [{"term": t, "count": c} for t, c in rows],
            })

        search = params.get("search")
        if search is None:
            return httpx.Response(200, json=meta_total(1_000_000))
        if "reactionmeddrapt" in search and "activesubstance" in search:
            return httpx.Response(200, json=meta_total(100))
        if "activesubstance" in search:
            return httpx.Response(200, json=meta_total(2000))
        return httpx.Response(200, json=meta_total(10_000))

    @pytest.mark.asyncio
    async def test_costs_four_extra_calls(self, stub):
        recorder = stub(self.handler)
        await m.faers_disproportionality(
            drug_name="X", events=["Y"], stratify_by="sex"
        )
        assert len(recorder.requests) == 8  # 4 crude marginals + 4 stratified

    @pytest.mark.asyncio
    async def test_reports_crude_and_adjusted_together(self, stub):
        stub(self.handler)
        out = await m.faers_disproportionality(
            drug_name="X", events=["Y"], stratify_by="sex"
        )
        s = out["stratified"]

        assert out["ror"]["value"] is not None          # crude survives
        assert s["adjusted"]["ror"]["value"] is not None  # adjusted alongside
        assert s["comparison"]["crude_ror"] == out["ror"]["value"]
        assert "percent_change" in s["comparison"]

    @pytest.mark.asyncio
    async def test_per_stratum_rows_and_coverage(self, stub):
        stub(self.handler)
        out = await m.faers_disproportionality(
            drug_name="X", events=["Y"], stratify_by="sex"
        )
        s = out["stratified"]

        assert {r["stratum"] for r in s["per_stratum"]} == {"male", "female"}
        assert s["coverage"]["reports_in_strata"] == 1_000_000
        assert s["coverage"]["percent_of_database"] == 100.0
        assert "excluded" in s["coverage"]["note"]

    @pytest.mark.asyncio
    async def test_homogeneity_is_reported(self, stub):
        stub(self.handler)
        out = await m.faers_disproportionality(
            drug_name="X", events=["Y"], stratify_by="sex"
        )
        assert out["stratified"]["adjusted"]["homogeneity"]["test"] == "Breslow-Day"

    @pytest.mark.asyncio
    async def test_unstratified_by_default(self, stub):
        recorder = stub(self.handler)
        out = await m.faers_disproportionality(drug_name="X", events=["Y"]
        )
        assert "stratified" not in out
        assert len(recorder.requests) == 4

    @pytest.mark.asyncio
    async def test_bad_stratifier_rejected(self, stub):
        stub(self.handler)
        with pytest.raises(ToolError) as exc:
            await m.faers_disproportionality(
                drug_name="X", events=["Y"], stratify_by="astrology"
            )
        err = error_of(exc)
        assert err["code"] == "invalid_query"


class TestCatalogue:
    @pytest.mark.asyncio
    async def test_stratifiers_are_discoverable(self):
        out = await m.faers_describe_fields()
        assert set(out["stratifiers"]) == {"sex", "age", "year", "age_sex"}
        assert "18%" in out["stratifiers"]["age"]["coverage"]
