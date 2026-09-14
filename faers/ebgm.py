"""Multi-item Gamma Poisson Shrinker (MGPS) and the Empirical Bayes Geometric Mean.

Implements DuMouchel's empirical Bayes model directly against scipy rather than
wrapping vigipy (GPL-3.0, hard-pinned deps, not on PyPI) or rpy2/openEBGM (needs
a full R runtime). The maths here is closed-form apart from one 5-parameter MLE
and one root-find, both of which scipy handles natively.

Model
-----
Each drug-event cell has an observed count N and an expected count
E = (n_drug * n_event) / n_total. The relative reporting ratio lambda = N/E is
given a two-component gamma mixture prior:

    pi(lambda) = P * Gamma(a1, b1) + (1 - P) * Gamma(a2, b2)

Marginally, N is then a mixture of two negative binomials, which is what the
likelihood below maximises. The posterior for a cell is again a gamma mixture,
and EBGM is the geometric mean of that posterior - i.e. 2 ** E[log2 lambda].

Shrinkage is the entire point: a cell with N=3, E=0.1 has a raw ratio of 30 but
almost no evidence behind it, and EBGM pulls it back toward 1. ROR and PRR do
not do this, which is why they produce long tails of spurious "signals" on
sparse cells.

Zero truncation
---------------
openFDA's count API reports only pairs that actually co-occur, so the table
never contains the N=0 cells. Fitting the untruncated likelihood to such data
biases the prior badly. The likelihood here is therefore zero-truncated by
default - the same choice openEBGM makes for processRaw() output. Pass
truncated=False only if you have supplied a genuinely complete rectangle.

What this is not
----------------
FDA's own MGPS stratifies by age, sex and report year to control confounding.
Doing that here would multiply the API calls by the number of strata, so this is
an UNSTRATIFIED fit over an any-role, non-deduplicated background. Values will
not reproduce FDA's published EBGMs and are labelled accordingly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Optional, Sequence

import numpy as np
from scipy.optimize import brentq, minimize
from scipy.special import digamma, gammaln, logsumexp
from scipy.stats import gamma as gamma_dist

LN2 = math.log(2.0)

# openEBGM's default starting point, and a reasonable one: component 1 is the
# large "noise" component near lambda = 1, component 2 the smaller signal one.
DEFAULT_START = (0.2, 0.1, 2.0, 4.0, 1.0 / 3.0)


@dataclass(frozen=True)
class Hyperparameters:
    a1: float
    b1: float
    a2: float
    b2: float
    p: float
    log_likelihood: float
    n_cells: int
    converged: bool
    truncated: bool

    def as_dict(self) -> dict:
        d = asdict(self)
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}

    @property
    def theta(self) -> tuple[float, float, float, float, float]:
        return self.a1, self.b1, self.a2, self.b2, self.p


# --- likelihood --------------------------------------------------------------


def _log_nb(n: np.ndarray, e: np.ndarray, alpha: float, beta: float) -> np.ndarray:
    """log P(N = n) for a Poisson(lambda*E) with lambda ~ Gamma(alpha, beta).

    Marginally negative binomial with success probability beta / (beta + E).
    """
    log_p = math.log(beta) - np.log(beta + e)
    log_q = np.log(e) - np.log(beta + e)
    return (
        gammaln(alpha + n)
        - gammaln(alpha)
        - gammaln(n + 1.0)
        + alpha * log_p
        + n * log_q
    )


def _log_mixture(n: np.ndarray, e: np.ndarray, theta: Sequence[float]) -> np.ndarray:
    a1, b1, a2, b2, p = theta
    comp = np.vstack([_log_nb(n, e, a1, b1), _log_nb(n, e, a2, b2)])
    weights = np.array([[math.log(max(p, 1e-300))], [math.log(max(1.0 - p, 1e-300))]])
    return logsumexp(comp + weights, axis=0)


def _unpack(raw: np.ndarray) -> tuple[float, float, float, float, float]:
    """Map the unconstrained optimiser vector onto the constrained parameters."""
    a1, b1, a2, b2 = np.exp(np.clip(raw[:4], -30.0, 30.0))
    p = 1.0 / (1.0 + math.exp(-float(np.clip(raw[4], -30.0, 30.0))))
    return float(a1), float(b1), float(a2), float(b2), p


def _pack(theta: Sequence[float]) -> np.ndarray:
    a1, b1, a2, b2, p = theta
    p = min(max(p, 1e-6), 1 - 1e-6)
    return np.array([math.log(a1), math.log(b1), math.log(a2), math.log(b2), math.log(p / (1 - p))])


def negative_log_likelihood(
    theta: Sequence[float], n: np.ndarray, e: np.ndarray, truncated: bool = True
) -> float:
    ll = _log_mixture(n, e, theta)
    if truncated:
        zeros = np.zeros_like(n)
        log_f0 = _log_mixture(zeros, e, theta)
        # log(1 - f0), guarded against f0 -> 1.
        survival = np.log(-np.expm1(np.minimum(log_f0, -1e-12)))
        ll = ll - survival
    total = float(np.sum(ll))
    return -total if np.isfinite(total) else 1e300


def fit_hyperparameters(
    counts: Sequence[float],
    expected: Sequence[float],
    start: Sequence[float] = DEFAULT_START,
    truncated: bool = True,
) -> Hyperparameters:
    """Maximum-likelihood fit of the five prior parameters over the whole table."""
    n = np.asarray(counts, dtype=float)
    e = np.asarray(expected, dtype=float)

    keep = np.isfinite(n) & np.isfinite(e) & (e > 0)
    n, e = n[keep], e[keep]
    if n.size < 20:
        raise ValueError(
            f"Only {n.size} usable cells; an empirical Bayes prior cannot be fitted "
            "from a table this small."
        )

    result = minimize(
        lambda raw: negative_log_likelihood(_unpack(raw), n, e, truncated),
        _pack(start),
        method="Nelder-Mead",
        options={"maxiter": 8000, "xatol": 1e-8, "fatol": 1e-8},
    )

    a1, b1, a2, b2, p = _unpack(result.x)

    # Identifiability: the components are exchangeable, so fix an order. By
    # convention component 1 is the one with the smaller prior mean.
    if (a1 / b1) > (a2 / b2):
        a1, b1, a2, b2, p = a2, b2, a1, b1, 1.0 - p

    return Hyperparameters(
        a1=a1,
        b1=b1,
        a2=a2,
        b2=b2,
        p=p,
        log_likelihood=-float(result.fun),
        n_cells=int(n.size),
        converged=bool(result.success),
        truncated=truncated,
    )


# --- posterior ---------------------------------------------------------------


def _posterior_weight(n: float, e: float, theta: Sequence[float]) -> float:
    """Q_n: posterior probability that the cell came from component 1."""
    a1, b1, a2, b2, p = theta
    n_arr, e_arr = np.array([n], dtype=float), np.array([e], dtype=float)
    log1 = float(_log_nb(n_arr, e_arr, a1, b1)[0]) + math.log(max(p, 1e-300))
    log2 = float(_log_nb(n_arr, e_arr, a2, b2)[0]) + math.log(max(1.0 - p, 1e-300))
    top = max(log1, log2)
    w1, w2 = math.exp(log1 - top), math.exp(log2 - top)
    return w1 / (w1 + w2)


def _posterior_cdf(x: float, n: float, e: float, theta: Sequence[float], q: float) -> float:
    a1, b1, a2, b2, _ = theta
    return q * gamma_dist.cdf(x, a=a1 + n, scale=1.0 / (b1 + e)) + (1 - q) * gamma_dist.cdf(
        x, a=a2 + n, scale=1.0 / (b2 + e)
    )


def _posterior_quantile(prob: float, n: float, e: float, theta: Sequence[float], q: float) -> float:
    """Invert the posterior mixture CDF. The mixture has no closed-form inverse.

    The mixture quantile is bracketed by the two component quantiles, but when
    one component carries essentially all the posterior weight the bracket
    collapses and the CDF is flat to machine precision at both ends - so the
    endpoints are checked before handing anything to brentq.
    """
    a1, b1, a2, b2, _ = theta
    q1 = gamma_dist.ppf(prob, a=a1 + n, scale=1.0 / (b1 + e))
    q2 = gamma_dist.ppf(prob, a=a2 + n, scale=1.0 / (b2 + e))
    if not (np.isfinite(q1) and np.isfinite(q2)):
        return float("nan")

    lo, hi = float(min(q1, q2)), float(max(q1, q2))
    if math.isclose(lo, hi, rel_tol=1e-12, abs_tol=1e-300):
        return lo

    f_lo = _posterior_cdf(lo, n, e, theta, q) - prob
    f_hi = _posterior_cdf(hi, n, e, theta, q) - prob
    # The CDF is monotone, so a non-straddling bracket means the root sits at
    # (or numerically indistinguishably close to) whichever endpoint is nearer.
    if f_lo >= 0.0:
        return lo
    if f_hi <= 0.0:
        return hi

    return float(brentq(lambda x: _posterior_cdf(x, n, e, theta, q) - prob, lo, hi, xtol=1e-12))


def ebgm_for_cell(
    n: float,
    e: float,
    hyper: Hyperparameters,
    lower: float = 0.05,
    upper: float = 0.95,
) -> dict:
    """EBGM with a credibility interval for one drug-event cell."""
    if e <= 0:
        return {"ebgm": None, "eb05": None, "eb95": None, "expected": e, "note": "expected count is zero"}

    theta = hyper.theta
    a1, b1, a2, b2, _ = theta
    q = _posterior_weight(n, e, theta)

    # E[log2 lambda] under the posterior gamma mixture.
    e_log2 = (
        q * (digamma(a1 + n) - math.log(b1 + e))
        + (1 - q) * (digamma(a2 + n) - math.log(b2 + e))
    ) / LN2

    return {
        "ebgm": round(float(2.0**e_log2), 3),
        "eb05": round(_posterior_quantile(lower, n, e, theta, q), 3),
        "eb95": round(_posterior_quantile(upper, n, e, theta, q), 3),
        "expected": round(float(e), 3),
        "observed": int(n),
        "rrr": round(float(n / e), 3),  # unshrunk relative reporting ratio
        "posterior_weight_component1": round(q, 4),
    }


def expected_count(drug_total: int, event_total: int, grand_total: int) -> float:
    """E = (n_drug * n_event) / N, the independence baseline."""
    if grand_total <= 0:
        return 0.0
    return (drug_total * event_total) / grand_total
