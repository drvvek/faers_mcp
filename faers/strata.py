"""Stratification variables for confounder-adjusted disproportionality.

Crude ROR/PRR/EBGM compare a drug against the whole database, so anything that
predicts both exposure and reporting - age, sex, reporting era - confounds them.
Stratifying and pooling removes that, which is why FDA's MGPS stratifies.

Field choice is driven by coverage, measured against the live API:

    patient.patientsex          87.9% of reports
    patient.patientonsetage     55.3% (with unit = years)
    patient.patientagegroup     18.4%   <- rejected: discards 82% of the data
    receivedate                  ~100%

`patientagegroup` is the obvious-looking age field and the wrong one. Age bands
are derived from `patientonsetage` instead, restricted to unit 801 (years).

Cost
----
Each marginal is ONE call per stratifier, not one per stratum, because openFDA
can count on the stratifying field itself and the bins are formed client-side:

    sex   count=patient.patientsex
    age   count=patient.patientonsetage  (+ unit filter), binned here
    year  count=receivedate              (complete histogram), binned here

A crossed stratifier (age_sex) costs one call per outer band.

Enumerating strata
------------------
Mantel-Haenszel only needs the counts, so it works with any stratifier. Stratified
EBGM additionally needs each stratum's whole marginal distribution, which means
knowing the strata up front. Sex, age and age_sex are fixed. Year is not: FAERS
spans 38 calendar years, so `resolve_strata` measures them and prunes the
negligible tail - 1986-2003 hold 385 reports between them (0.002%) and would cost
two calls each. The resolving call doubles as the stratum-size measurement.
`max_strata` guards against fragmenting the data past the point of estimating
anything from it.

Coverage
--------
Reports missing the stratifying field cannot enter any stratum and are dropped.
That is standard, but it changes the population being described, so every
stratified result reports its coverage against the crude total.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional

from .errors import InvalidQuery
from .query import combine

YEARS_UNIT = "patient.patientonsetageunit:801"

AGE_BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 17, "0-17"),
    (18, 64, "18-64"),
    (65, 120, "65+"),
)


def _bin_sex(term: str) -> Optional[str]:
    # "0" is an explicit "unknown" code; it is not a stratum.
    return {"1": "male", "2": "female"}.get(str(term).strip())


def _bin_age(term: str) -> Optional[str]:
    try:
        age = float(term)
    except (TypeError, ValueError):
        return None
    for lo, hi, label in AGE_BANDS:
        if lo <= age <= hi:
            return label
    return None


def _bin_year(term: str) -> Optional[str]:
    text = str(term)
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else None


@dataclass(frozen=True)
class Stratifier:
    """How to partition FAERS reports, and what it costs to do so."""

    name: str
    count_field: str
    binner: Callable[[str], Optional[str]]
    description: str
    coverage_note: str
    extra_clause: Optional[str] = None
    # Outer clauses turn one stratifier into a cross: each is counted separately.
    outer: tuple[tuple[str, str], ...] = field(default=())

    @property
    def calls_per_marginal(self) -> int:
        return max(1, len(self.outer))

    def clauses(self) -> Optional[dict[str, str]]:
        """Explicit search clause per stratum, when the strata are enumerable.

        Returns None for open-ended stratifiers such as year, whose labels are
        only known once the data has been counted. Mantel-Haenszel does not need
        this; stratified EBGM does, because it must count each stratum's whole
        marginal distribution.
        """
        if self.name == "sex":
            return {"male": "patient.patientsex:1", "female": "patient.patientsex:2"}
        if self.name == "age":
            return {
                label: combine(YEARS_UNIT, f"patient.patientonsetage:[{lo} TO {hi}]")
                for lo, hi, label in AGE_BANDS
            }
        if self.name == "age_sex":
            return {
                f"{band}|{sex_label}": combine(band_clause, sex_clause)
                for band, band_clause in self.outer
                for sex_label, sex_clause in (
                    ("male", "patient.patientsex:1"),
                    ("female", "patient.patientsex:2"),
                )
            }
        return None


SEX = Stratifier(
    name="sex",
    count_field="patient.patientsex",
    binner=_bin_sex,
    description="Male / female. Reports coded 0 (unknown) or missing are excluded.",
    coverage_note="patient.patientsex is populated on ~88% of reports.",
)

AGE = Stratifier(
    name="age",
    count_field="patient.patientonsetage",
    binner=_bin_age,
    description="Age bands 0-17 / 18-64 / 65+, from patientonsetage in years.",
    coverage_note=(
        "patientonsetage in years is populated on ~55% of reports. "
        "patientagegroup was not used: it covers only ~18%."
    ),
    extra_clause=YEARS_UNIT,
)

YEAR = Stratifier(
    name="year",
    count_field="receivedate",
    binner=_bin_year,
    description="Calendar year of receivedate.",
    coverage_note="receivedate is present on essentially every report.",
)

AGE_SEX = Stratifier(
    name="age_sex",
    count_field="patient.patientsex",
    binner=_bin_sex,
    description="Age band crossed with sex. Costs one call per age band.",
    coverage_note=(
        "Requires both patientonsetage (years) and patientsex, so coverage is "
        "lower than either alone - check the reported figure."
    ),
    outer=tuple(
        (label, f"{YEARS_UNIT} AND patient.patientonsetage:[{lo} TO {hi}]")
        for lo, hi, label in AGE_BANDS
    ),
)

STRATIFIERS: dict[str, Stratifier] = {s.name: s for s in (SEX, AGE, YEAR, AGE_SEX)}


def get_stratifier(name: str) -> Stratifier:
    key = (name or "").strip().lower()
    if key not in STRATIFIERS:
        raise InvalidQuery(
            reason=f"Unknown stratify_by {name!r}.",
            recovery=f"Use one of: {', '.join(sorted(STRATIFIERS))}.",
        )
    return STRATIFIERS[key]


async def stratified_totals(client, base_query: Optional[str], strat: Stratifier) -> dict[str, int]:
    """Report counts per stratum for one query, in one call per outer band.

    This is the trick that keeps stratification affordable: counting ON the
    stratifying field returns every stratum at once, so a stratified 2x2 costs
    four calls rather than four per stratum.
    """

    async def band(outer_label: str, outer_clause: Optional[str]) -> dict[str, int]:
        query = combine(base_query, strat.extra_clause, outer_clause)
        rows = await client.counts(query or None, strat.count_field, client.max_limit)
        out: dict[str, int] = {}
        for row in rows:
            label = strat.binner(row.get("time") or row.get("term"))
            if label is None:
                continue
            key = f"{outer_label}|{label}" if outer_label else label
            out[key] = out.get(key, 0) + int(row.get("count", 0))
        return out

    bands = strat.outer or (("", None),)
    parts = await asyncio.gather(*(band(label, clause) for label, clause in bands))

    merged: dict[str, int] = {}
    for part in parts:
        merged.update(part)
    return merged


# Years below this share of the window's reports are dropped: FAERS carries a
# tail of pre-2004 records (1986-2003 hold 385 reports in total, 0.002%) that
# would each cost two calls to stratify and contribute nothing.
MIN_YEAR_SHARE = 0.001

# A guard against fragmenting the data into strata too thin to estimate from.
DEFAULT_MAX_STRATA = 30


async def resolve_strata(
    client,
    strat: Stratifier,
    window_clause: Optional[str] = None,
    min_share: float = MIN_YEAR_SHARE,
    max_strata: int = DEFAULT_MAX_STRATA,
) -> tuple[dict[str, str], dict]:
    """Determine this stratifier's strata and their search clauses.

    Static stratifiers (sex, age, age_sex) know their strata in advance and cost
    nothing. Year does not: FAERS spans 38 calendar years, so the labels come
    from the data, and negligible years are pruned before they cost two calls
    each. Returns (clauses, provenance).
    """
    static = strat.clauses()
    if static is not None:
        return static, {"resolution": "static", "strata": len(static), "resolve_calls": 0}

    if strat.name != "year":  # pragma: no cover - no other dynamic stratifier yet
        raise InvalidQuery(
            reason=f"Stratifier {strat.name!r} has no strata resolver.",
            recovery="Use sex, age, age_sex or year.",
        )

    rows = await client.counts(window_clause, strat.count_field, client.max_limit)
    totals: dict[str, int] = {}
    for row in rows:
        label = strat.binner(row.get("time") or row.get("term"))
        if label:
            totals[label] = totals.get(label, 0) + int(row.get("count", 0))

    if not totals:
        raise InvalidQuery(
            reason="No years found in the selected window.",
            recovery="Widen or remove date_from/date_to.",
        )

    grand = sum(totals.values())
    kept = sorted(y for y, n in totals.items() if n / grand >= min_share)
    dropped = sorted(set(totals) - set(kept))

    if len(kept) > max_strata:
        raise InvalidQuery(
            reason=(
                f"{len(kept)} year strata exceeds the limit of {max_strata}. Thin strata "
                "give unstable estimates and cost two calls each."
            ),
            recovery=(
                "Narrow the analysis with date_from/date_to, or raise max_strata if you "
                "genuinely want every year."
            ),
        )

    return (
        {y: f"receivedate:[{y}0101 TO {y}1231]" for y in kept},
        {
            "resolution": "measured from the data",
            "strata": len(kept),
            "resolve_calls": 1,
            "years_kept": kept,
            "years_dropped": dropped,
            "reports_dropped": sum(totals[y] for y in dropped),
            "drop_rule": f"years holding less than {min_share:.1%} of reports in the window",
            # The resolving call already measured every stratum's size, so the
            # caller need not ask again.
            "stratum_sizes": {y: totals[y] for y in kept},
        },
    )


async def stratum_field_counts(
    client,
    count_field: str,
    clauses: dict[str, str],
    limit: Optional[int] = None,
) -> dict[str, dict[str, int]]:
    """For each stratum, the whole distribution of `count_field` within it.

    One call per stratum. Used to build stratified expected counts, where every
    drug's and every event's marginal is needed inside each stratum.
    """

    async def one(label: str, clause: str) -> tuple[str, dict[str, int]]:
        rows = await client.counts(clause, count_field, limit or client.max_limit)
        return label, {str(r["term"]).upper(): int(r["count"]) for r in rows}

    results = await asyncio.gather(*(one(k, v) for k, v in clauses.items()))
    return dict(results)


def describe_stratifiers() -> dict:
    def strata_count(s: Stratifier) -> str:
        static = s.clauses()
        return str(len(static)) if static else "measured from the data (year)"

    return {
        name: {
            "description": s.description,
            "coverage": s.coverage_note,
            "strata": strata_count(s),
            "calls_per_marginal": s.calls_per_marginal,
            "note": (
                "Mantel-Haenszel (faers_disproportionality) costs "
                f"{4 * s.calls_per_marginal} calls. Stratified EBGM additionally "
                "builds per-stratum marginals at ~2 calls per stratum, cached."
            ),
        }
        for name, s in sorted(STRATIFIERS.items())
    }
