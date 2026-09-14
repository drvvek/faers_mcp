"""Disproportionality statistics.

Two behaviours ported from the companion AEMS Chrome extension - the browser
tool this server was derived from - because it gets them right:

* Conditional Haldane-Anscombe. The 0.5 correction is applied only when a zero
  cell would leave ROR/PRR or their CIs undefined. Tables with no zero cell are
  left exactly as reported (signal_detection.js:404). The previous MCP simply
  refused to compute whenever any cell was zero.

* Validity decided before computing, so a failed marginal can never silently
  become a fabricated ROR (signal_detection.js:391).

One behaviour deliberately *not* ported: the extension's dashboard builds a 2x2
whose cell `a` is suspect-verified but whose cell `b` comes from an unscoped
drug query (dashboard.js:1015). Mixing bases inflates `b` with concomitant-only
reports and biases ROR downward. Here every cell shares one role basis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

Z = 1.959963984540054  # two-sided 95%


@dataclass(frozen=True)
class Cells:
    a: int  # drug and event
    b: int  # drug, other events
    c: int  # event, other drugs
    d: int  # neither

    @property
    def n(self) -> int:
        return self.a + self.b + self.c + self.d

    def as_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "c": self.c, "d": self.d}


def build_cells(a: int, drug_total: int, event_total: int, grand_total: int) -> tuple[Cells, list[str]]:
    """Derive the 2x2 from four marginals, reporting any inconsistency.

    All four inputs must be measured on the same role basis and the same date
    window, or the table is meaningless.
    """
    flags: list[str] = []
    b = drug_total - a
    c = event_total - a
    d = grand_total - (a + b + c)

    if b < 0 or c < 0 or d < 0:
        flags.append("invalid_cells")
    if grand_total < a + b + c:
        flags.append("grand_total_inconsistent")

    return Cells(a=a, b=b, c=c, d=d), flags


def _chi_square(cells: Cells) -> Optional[float]:
    """Uncorrected 2x2 chi-square, as used by the Evans PRR criterion."""
    a, b, c, d = cells.a, cells.b, cells.c, cells.d
    n = cells.n
    denom = (a + b) * (c + d) * (a + c) * (b + d)
    if denom == 0 or n == 0:
        return None
    return n * ((a * d - b * c) ** 2) / denom


def disproportionality(cells: Cells, flags: Optional[list[str]] = None) -> dict:
    """Compute ROR, PRR and chi-square with 95% intervals.

    Returns a payload whose `valid` field is authoritative: when it is False the
    metrics are None, never a placeholder number.
    """
    flags = list(flags or [])
    a, b, c, d = cells.a, cells.b, cells.c, cells.d

    if any(x < 0 for x in (a, b, c, d)):
        if "invalid_cells" not in flags:
            flags.append("invalid_cells")
        return {
            "valid": False,
            "flags": flags,
            "contingency_table": cells.as_dict(),
            "ror": None,
            "prr": None,
            "chi_square": None,
            "note": "One or more contingency cells is negative; no metric is computable.",
        }

    needs_correction = 0 in (a, b, c, d)
    if needs_correction:
        flags.append("haldane_anscombe_0.5")
    ca, cb, cc, cd = (
        (a + 0.5, b + 0.5, c + 0.5, d + 0.5) if needs_correction else (a, b, c, d)
    )

    try:
        ror = (ca * cd) / (cb * cc)
        ror_se = math.sqrt(1 / ca + 1 / cb + 1 / cc + 1 / cd)
        ror_lower = math.exp(math.log(ror) - Z * ror_se)
        ror_upper = math.exp(math.log(ror) + Z * ror_se)

        prr = (ca / (ca + cb)) / (cc / (cc + cd))
        prr_se = math.sqrt(1 / ca - 1 / (ca + cb) + 1 / cc - 1 / (cc + cd))
        prr_lower = math.exp(math.log(prr) - Z * prr_se)
        prr_upper = math.exp(math.log(prr) + Z * prr_se)
    except (ValueError, ZeroDivisionError):
        flags.append("undefined_metric")
        return {
            "valid": False,
            "flags": flags,
            "contingency_table": cells.as_dict(),
            "ror": None,
            "prr": None,
            "chi_square": None,
            "note": "The contingency table does not admit a defined ROR/PRR.",
        }

    finite = all(math.isfinite(v) for v in (ror, ror_lower, ror_upper, prr, prr_lower, prr_upper))
    if not finite:
        flags.append("undefined_metric")
        return {
            "valid": False,
            "flags": flags,
            "contingency_table": cells.as_dict(),
            "ror": None,
            "prr": None,
            "chi_square": None,
            "note": "A metric evaluated to a non-finite value.",
        }

    chi2 = _chi_square(cells)

    return {
        "valid": True,
        "flags": flags,
        "contingency_table": cells.as_dict(),
        "n_cases": a,
        "ror": {
            "value": round(ror, 3),
            "ci_lower_95": round(ror_lower, 3),
            "ci_upper_95": round(ror_upper, 3),
        },
        "prr": {
            "value": round(prr, 3),
            "ci_lower_95": round(prr_lower, 3),
            "ci_upper_95": round(prr_upper, 3),
        },
        "chi_square": round(chi2, 3) if chi2 is not None else None,
    }


# --- named criteria ----------------------------------------------------------
#
# The previous implementation printed a single "SIGNAL DETECTED" banner whose
# rule (ror_lower > 1 and a >= 3) contradicted its own docstring. Each criterion
# now stands on its own name so the reader knows which convention was applied.

CRITERIA_DEFINITIONS = {
    "ema_ror": "ROR lower 95% CI > 1 and a >= 3",
    "evans_prr": "PRR >= 2 and chi-square >= 4 and a >= 3",
}


def evaluate_criteria(result: dict) -> dict:
    """Evaluate the named screening criteria. Never returns a single verdict."""
    if not result.get("valid"):
        return {
            "definitions": CRITERIA_DEFINITIONS,
            "ema_ror": None,
            "evans_prr": None,
            "note": "Criteria not evaluated: the contingency table is not valid.",
        }

    a = result["n_cases"]
    ror_lower = result["ror"]["ci_lower_95"]
    prr = result["prr"]["value"]
    chi2 = result["chi_square"]

    return {
        "definitions": CRITERIA_DEFINITIONS,
        "ema_ror": bool(ror_lower > 1 and a >= 3),
        "evans_prr": bool(prr >= 2 and chi2 is not None and chi2 >= 4 and a >= 3),
    }


# ─────────────────────────────────────────────
# Stratified (Mantel-Haenszel) estimation
# ─────────────────────────────────────────────
#
# Crude ROR/PRR compare a drug against the whole database, so any factor that
# predicts both exposure and reporting confounds them. Pooling stratum-specific
# tables with Mantel-Haenszel weights removes that, and the Breslow-Day test
# then says whether the strata disagreed enough for it to have mattered.


def mantel_haenszel(strata: list[Cells]) -> dict:
    """Pool stratum-specific 2x2 tables.

    Returns MH-pooled ROR (Robins-Breslow-Greenland variance) and PRR
    (Greenland-Robins variance), plus a Breslow-Day test of whether the
    stratum-specific odds ratios are homogeneous.
    """
    usable = [
        s
        for s in strata
        if s.n > 0 and min(s.a, s.b, s.c, s.d) >= 0 and (s.a + s.b) > 0 and (s.c + s.d) > 0
    ]
    if not usable:
        return {
            "valid": False,
            "flags": ["no_usable_strata"],
            "note": "No stratum had a well-formed 2x2 table.",
        }

    # --- MH odds ratio, with the Robins-Breslow-Greenland variance ------------
    r_sum = s_sum = 0.0
    pr = ps_qr = qs = 0.0
    for k in usable:
        n = float(k.n)
        r = k.a * k.d / n
        s = k.b * k.c / n
        p = (k.a + k.d) / n
        q = (k.b + k.c) / n
        r_sum += r
        s_sum += s
        pr += p * r
        ps_qr += p * s + q * r
        qs += q * s

    if r_sum <= 0 or s_sum <= 0:
        ror = ror_lo = ror_hi = None
        ror_flags = ["mh_ror_undefined"]
    else:
        ror = r_sum / s_sum
        var_log = pr / (2 * r_sum**2) + ps_qr / (2 * r_sum * s_sum) + qs / (2 * s_sum**2)
        se = math.sqrt(var_log) if var_log > 0 else 0.0
        ror_lo = math.exp(math.log(ror) - Z * se)
        ror_hi = math.exp(math.log(ror) + Z * se)
        ror_flags = []

    # --- MH proportional reporting ratio, Greenland-Robins variance -----------
    num = den = 0.0
    var_num = 0.0
    for k in usable:
        n = float(k.n)
        n1, n0 = k.a + k.b, k.c + k.d
        num += k.a * n0 / n
        den += k.c * n1 / n
        var_num += (n1 * n0 * (k.a + k.c) - k.a * k.c * n) / (n * n)

    if num <= 0 or den <= 0:
        prr = prr_lo = prr_hi = None
        prr_flags = ["mh_prr_undefined"]
    else:
        prr = num / den
        var_log_prr = var_num / (num * den)
        se_prr = math.sqrt(var_log_prr) if var_log_prr > 0 else 0.0
        prr_lo = math.exp(math.log(prr) - Z * se_prr)
        prr_hi = math.exp(math.log(prr) + Z * se_prr)
        prr_flags = []

    pooled = Cells(
        a=sum(k.a for k in usable),
        b=sum(k.b for k in usable),
        c=sum(k.c for k in usable),
        d=sum(k.d for k in usable),
    )

    return {
        "valid": ror is not None,
        "flags": ror_flags + prr_flags,
        "strata_used": len(usable),
        "strata_dropped": len(strata) - len(usable),
        "pooled_cells": pooled.as_dict(),
        "n_cases": pooled.a,
        "ror": (
            None
            if ror is None
            else {
                "value": round(ror, 3),
                "ci_lower_95": round(ror_lo, 3),
                "ci_upper_95": round(ror_hi, 3),
                "method": "Mantel-Haenszel, Robins-Breslow-Greenland variance",
            }
        ),
        "prr": (
            None
            if prr is None
            else {
                "value": round(prr, 3),
                "ci_lower_95": round(prr_lo, 3),
                "ci_upper_95": round(prr_hi, 3),
                "method": "Mantel-Haenszel, Greenland-Robins variance",
            }
        ),
        "homogeneity": breslow_day(usable, ror),
    }


def _expected_a(cells: Cells, psi: float) -> Optional[float]:
    """Expected `a` under a common odds ratio, holding the margins fixed."""
    n1 = cells.a + cells.b
    n0 = cells.c + cells.d
    m1 = cells.a + cells.c

    lo = max(0.0, float(m1 - n0))
    hi = min(float(n1), float(m1))
    if hi <= lo:
        return None

    if abs(psi - 1.0) < 1e-12:  # the quadratic degenerates to the null expectation
        return min(max(n1 * m1 / float(cells.n), lo), hi)

    qa = psi - 1.0
    qb = -(psi * (n1 + m1) + (n0 - m1))
    qc = psi * n1 * m1

    disc = qb * qb - 4 * qa * qc
    if disc < 0:
        return None
    root = math.sqrt(disc)
    for candidate in ((-qb - root) / (2 * qa), (-qb + root) / (2 * qa)):
        if lo - 1e-9 <= candidate <= hi + 1e-9:
            return min(max(candidate, lo), hi)
    return None


def breslow_day(strata: list[Cells], psi: Optional[float]) -> dict:
    """Test whether the stratum-specific odds ratios are homogeneous.

    A small p-value means the effect genuinely differs across strata, so the
    pooled estimate is hiding something and the per-stratum rows should be read
    instead.
    """
    if psi is None or psi <= 0 or len(strata) < 2:
        return {
            "test": "Breslow-Day",
            "statistic": None,
            "df": max(0, len(strata) - 1),
            "p_value": None,
            "note": "Needs a defined pooled odds ratio and at least two strata.",
        }

    statistic = 0.0
    used = 0
    for k in strata:
        expected = _expected_a(k, psi)
        if expected is None:
            continue
        n1, n0 = k.a + k.b, k.c + k.d
        m1 = k.a + k.c
        terms = [expected, n1 - expected, m1 - expected, n0 - m1 + expected]
        if any(t <= 1e-9 for t in terms):
            continue
        variance = 1.0 / sum(1.0 / t for t in terms)
        statistic += (k.a - expected) ** 2 / variance
        used += 1

    df = used - 1
    if df < 1:
        return {
            "test": "Breslow-Day",
            "statistic": None,
            "df": max(df, 0),
            "p_value": None,
            "note": "Too few evaluable strata to test homogeneity.",
        }

    p_value = chi2_sf(statistic, df)
    return {
        "test": "Breslow-Day",
        "statistic": round(statistic, 3),
        "df": df,
        "p_value": round(p_value, 5),
        "homogeneous_at_0.05": bool(p_value >= 0.05),
        "interpretation": (
            "Stratum-specific odds ratios are consistent; the pooled estimate "
            "summarises them fairly."
            if p_value >= 0.05
            else "Stratum-specific odds ratios differ; read the per-stratum rows "
            "rather than the pooled estimate."
        ),
    }


def chi2_sf(statistic: float, df: int) -> float:
    """Upper tail of the chi-square distribution.

    Written out rather than taken from scipy so the twelve non-EBGM tools keep
    working without numpy/scipy installed.
    """
    if statistic <= 0:
        return 1.0
    a = df / 2.0
    x = statistic / 2.0
    log_prefactor = -x + a * math.log(x) - math.lgamma(a)

    if x < a + 1.0:
        # Series expansion for the regularised lower incomplete gamma.
        term = 1.0 / a
        total = term
        n = a
        for _ in range(1000):
            n += 1.0
            term *= x / n
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return max(0.0, min(1.0, 1.0 - total * math.exp(log_prefactor)))

    # Continued fraction (Lentz) for the regularised upper incomplete gamma.
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return max(0.0, min(1.0, h * math.exp(log_prefactor)))
