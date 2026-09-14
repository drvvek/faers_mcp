"""MGPS/EBGM: parameter recovery, shrinkage behaviour and numerical edges."""

from __future__ import annotations

import math

import numpy as np
import pytest

from faers.ebgm import (
    DEFAULT_START,
    Hyperparameters,
    ebgm_for_cell,
    expected_count,
    fit_hyperparameters,
    negative_log_likelihood,
)


def simulate(true, n_cells=20000, seed=11):
    """Draw counts from a known gamma-mixture prior, as the model assumes."""
    rng = np.random.default_rng(seed)
    e = np.exp(rng.normal(-0.5, 1.6, n_cells))
    from_c1 = rng.random(n_cells) < true["p"]
    lam = np.where(
        from_c1,
        rng.gamma(true["a1"], 1 / true["b1"], n_cells),
        rng.gamma(true["a2"], 1 / true["b2"], n_cells),
    )
    n = rng.poisson(lam * e)
    keep = n >= 1  # openFDA only reports co-occurring pairs
    return n[keep], e[keep]


@pytest.fixture(scope="module")
def recovered():
    """Fit against data generated from known hyperparameters."""
    true = {"a1": 2.0, "b1": 2.0, "a2": 4.0, "b2": 0.5, "p": 0.85}
    n, e = simulate(true)
    return true, fit_hyperparameters(n, e, truncated=True)


class TestParameterRecovery:
    """The only way to know the likelihood is right without an external oracle."""

    def test_converges(self, recovered):
        _, hyper = recovered
        assert hyper.converged is True
        assert hyper.n_cells > 5_000

    @pytest.mark.parametrize("name", ["a1", "b1", "a2", "b2", "p"])
    def test_each_parameter_recovered(self, recovered, name):
        true, hyper = recovered
        fitted = getattr(hyper, name)
        assert fitted == pytest.approx(true[name], rel=0.20), (
            f"{name}: fitted {fitted:.4f} vs true {true[name]}"
        )

    def test_prior_means_recovered_tightly(self, recovered):
        """The component means are the well-identified quantities."""
        true, h = recovered
        assert h.a1 / h.b1 == pytest.approx(true["a1"] / true["b1"], rel=0.10)
        assert h.a2 / h.b2 == pytest.approx(true["a2"] / true["b2"], rel=0.10)

    def test_components_are_ordered(self, recovered):
        """A gamma mixture is exchangeable; the fit must impose a stable order."""
        _, h = recovered
        assert h.a1 / h.b1 <= h.a2 / h.b2

    def test_fitted_beats_the_starting_point(self, recovered):
        true, h = recovered
        n, e = simulate(true)
        assert negative_log_likelihood(h.theta, n, e) < negative_log_likelihood(
            DEFAULT_START, n, e
        )


class TestShrinkage:
    """The property that distinguishes EBGM from a raw ratio."""

    @pytest.fixture
    def hyper(self, recovered):
        return recovered[1]

    def test_sparse_evidence_is_shrunk_hard(self, hyper):
        r = ebgm_for_cell(1, 0.01, hyper)
        assert r["rrr"] == 100.0
        assert r["ebgm"] < 20, "a single case on E=0.01 must not keep a ratio of 100"

    def test_strong_evidence_is_barely_shrunk(self, hyper):
        r = ebgm_for_cell(2000, 100.0, hyper)
        assert r["ebgm"] == pytest.approx(r["rrr"], rel=0.02)

    def test_shrinkage_decreases_monotonically_with_evidence(self, hyper):
        """Same ratio of 30, increasing evidence: EBGM must approach the ratio."""
        retained = [
            ebgm_for_cell(n, e, hyper)["ebgm"] / 30.0
            for n, e in [(3, 0.1), (30, 1.0), (300, 10.0), (3000, 100.0)]
        ]
        assert retained == sorted(retained), retained
        assert retained[0] < 0.6
        assert retained[-1] > 0.95

    def test_interval_brackets_the_point_estimate(self, hyper):
        for n, e in [(1, 0.01), (3, 1.0), (30, 1.0), (300, 10.0), (2000, 100.0)]:
            r = ebgm_for_cell(n, e, hyper)
            assert r["eb05"] <= r["ebgm"] <= r["eb95"], (n, e, r)

    def test_interval_narrows_as_evidence_grows(self, hyper):
        wide = ebgm_for_cell(3, 0.1, hyper)
        tight = ebgm_for_cell(3000, 100.0, hyper)
        assert (wide["eb95"] - wide["eb05"]) > (tight["eb95"] - tight["eb05"])

    def test_ratio_of_one_stays_near_one(self, hyper):
        r = ebgm_for_cell(500, 500.0, hyper)
        assert r["ebgm"] == pytest.approx(1.0, abs=0.15)


class TestNumericalEdges:
    @pytest.fixture
    def hyper(self, recovered):
        return recovered[1]

    def test_zero_expected_is_reported_not_divided_by(self, hyper):
        r = ebgm_for_cell(5, 0.0, hyper)
        assert r["ebgm"] is None
        assert "expected count is zero" in r["note"]

    def test_zero_observed_is_computable(self, hyper):
        r = ebgm_for_cell(0, 5.0, hyper)
        assert r["ebgm"] is not None
        assert r["ebgm"] < 1.0, "no reports against a positive expectation is below 1"

    def test_extreme_cell_does_not_break_the_quantile_bracket(self, hyper):
        """A dominated posterior component collapses the brentq bracket."""
        for n, e in [(1, 1e-6), (100_000, 1.0), (0, 1e-6), (5, 1e8)]:
            r = ebgm_for_cell(n, e, hyper)
            assert r["ebgm"] is not None
            assert math.isfinite(r["eb05"]) and math.isfinite(r["eb95"])

    def test_posterior_weight_is_a_probability(self, hyper):
        for n, e in [(1, 0.01), (50, 1.0), (5000, 10.0)]:
            w = ebgm_for_cell(n, e, hyper)["posterior_weight_component1"]
            assert 0.0 <= w <= 1.0

    def test_too_few_cells_refuses_to_fit(self):
        with pytest.raises(ValueError, match="cannot be fitted"):
            fit_hyperparameters([1, 2, 3], [0.5, 0.5, 0.5])

    def test_cells_with_zero_expected_are_dropped_before_fitting(self, recovered):
        true, _ = recovered
        n, e = simulate(true, n_cells=8000)
        n2 = np.concatenate([n, [5, 5]])
        e2 = np.concatenate([e, [0.0, -1.0]])
        assert fit_hyperparameters(n2, e2).n_cells == len(n)


class TestExpectedCount:
    def test_independence_baseline(self):
        assert expected_count(1000, 2000, 1_000_000) == pytest.approx(2.0)

    def test_zero_grand_total_is_safe(self):
        assert expected_count(10, 10, 0) == 0.0


class TestTruncation:
    def test_truncated_and_untruncated_likelihoods_differ(self, recovered):
        true, h = recovered
        n, e = simulate(true, n_cells=5000)
        assert negative_log_likelihood(h.theta, n, e, truncated=True) != pytest.approx(
            negative_log_likelihood(h.theta, n, e, truncated=False)
        )

    def test_untruncated_fit_on_truncated_data_is_biased(self, recovered):
        """Why truncated=True is the default for openFDA-shaped data."""
        true, correct = recovered
        n, e = simulate(true)
        wrong = fit_hyperparameters(n, e, truncated=False)

        correct_err = abs(correct.a1 / correct.b1 - true["a1"] / true["b1"])
        wrong_err = abs(wrong.a1 / wrong.b1 - true["a1"] / true["b1"])
        assert correct_err < wrong_err

    def test_flag_is_recorded(self, recovered):
        assert recovered[1].as_dict()["truncated"] is True


class TestSerialisation:
    def test_round_trips_through_the_cache_format(self, recovered):
        _, h = recovered
        restored = Hyperparameters(**h.as_dict())
        assert restored.theta == pytest.approx(
            tuple(round(v, 6) for v in h.theta), rel=1e-6
        )
