"""Disproportionality maths, checked against the recorded known pair."""

from __future__ import annotations

import pytest

from faers.stats import Cells, build_cells, disproportionality, evaluate_criteria


class TestKnownPair:
    """EMPAGLIFLOZIN x PANCREATITIS, marginals recorded from the live API.

    Expected values computed independently:
        a = 575, b = 66,676, c = 51,651, d = 20,573,788
        ROR = (575 x 20,573,788) / (66,676 x 51,651) = 3.435
        PRR = (575/67,251) / (51,651/20,625,439)     = 3.414
    """

    def test_cells(self, known_pair):
        cells, flags = build_cells(**{
            "a": known_pair["a"],
            "drug_total": known_pair["drug_total"],
            "event_total": known_pair["event_total"],
            "grand_total": known_pair["grand_total"],
        })
        assert cells.a == 575
        assert cells.b == 66_676
        assert cells.c == 51_651
        assert cells.d == 20_573_788
        assert cells.n == known_pair["grand_total"]
        assert flags == []

    def test_ror_and_prr(self, known_pair):
        cells, flags = build_cells(
            known_pair["a"], known_pair["drug_total"],
            known_pair["event_total"], known_pair["grand_total"],
        )
        result = disproportionality(cells, flags)

        assert result["valid"] is True
        assert result["flags"] == []
        assert result["ror"]["value"] == pytest.approx(3.435, abs=0.01)
        assert result["prr"]["value"] == pytest.approx(3.414, abs=0.01)
        assert result["ror"]["ci_lower_95"] < result["ror"]["value"] < result["ror"]["ci_upper_95"]

    def test_criteria_are_named_not_a_single_verdict(self, known_pair):
        cells, flags = build_cells(
            known_pair["a"], known_pair["drug_total"],
            known_pair["event_total"], known_pair["grand_total"],
        )
        criteria = evaluate_criteria(disproportionality(cells, flags))

        assert set(criteria) == {"definitions", "ema_ror", "evans_prr"}
        assert "signal_detected" not in criteria
        assert criteria["ema_ror"] is True
        assert criteria["evans_prr"] is True


class TestConditionalHaldane:
    """The 0.5 correction fires only when a zero cell would leave a metric undefined."""

    def test_not_applied_when_no_cell_is_zero(self):
        result = disproportionality(Cells(10, 90, 100, 9800))
        assert "haldane_anscombe_0.5" not in result["flags"]
        assert result["ror"]["value"] == pytest.approx((10 * 9800) / (90 * 100), abs=0.001)

    def test_applied_when_a_cell_is_zero(self):
        result = disproportionality(Cells(0, 100, 50, 9850))
        assert result["valid"] is True
        assert "haldane_anscombe_0.5" in result["flags"]
        assert result["ror"]["value"] is not None

    def test_zero_cell_no_longer_refuses_to_compute(self):
        """The previous implementation errored out on any zero cell."""
        assert disproportionality(Cells(3, 0, 40, 9957))["valid"] is True


class TestInvalidTables:
    def test_negative_cell_is_invalid_and_yields_no_metric(self):
        cells, flags = build_cells(a=100, drug_total=50, event_total=200, grand_total=10_000)
        result = disproportionality(cells, flags)

        assert result["valid"] is False
        assert "invalid_cells" in result["flags"]
        assert result["ror"] is None and result["prr"] is None

    def test_grand_total_inconsistency_flagged(self):
        _, flags = build_cells(a=10, drug_total=500, event_total=500, grand_total=100)
        assert "grand_total_inconsistent" in flags

    def test_criteria_not_evaluated_for_invalid_table(self):
        cells, flags = build_cells(a=100, drug_total=50, event_total=200, grand_total=10_000)
        criteria = evaluate_criteria(disproportionality(cells, flags))
        assert criteria["ema_ror"] is None
        assert criteria["evans_prr"] is None


class TestCriteriaBoundaries:
    def test_ema_ror_needs_three_cases(self):
        """A strong ratio on two cases must not pass the EMA criterion."""
        cells = Cells(2, 10, 20, 100_000)
        criteria = evaluate_criteria(disproportionality(cells))
        assert criteria["ema_ror"] is False

    def test_evans_prr_needs_chi_square_too(self):
        result = disproportionality(Cells(3, 3, 3, 20))
        criteria = evaluate_criteria(result)
        if result["chi_square"] is not None and result["chi_square"] < 4:
            assert criteria["evans_prr"] is False

    def test_no_signal_when_ror_ci_crosses_one(self):
        result = disproportionality(Cells(5, 1000, 5000, 1_000_000))
        criteria = evaluate_criteria(result)
        assert (result["ror"]["ci_lower_95"] > 1) == criteria["ema_ror"]
