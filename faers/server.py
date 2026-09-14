"""
OpenFDA FAERS MCP Server - tool definitions and entry point.

Wraps the OpenFDA Drug Adverse Event API (https://api.fda.gov/drug/event.json)
as fifteen pharmacovigilance tools. Run with `faers-mcp` or `python -m faers`.

Read docs/PLAN.md for the evidence behind the design. The changes that alter
answers relative to the original single-file server:

  * Clauses are joined with " AND ", not "+AND+". Passed through httpx's params
    dict the "+" arrived at openFDA as %2B, so every multi-clause query returned
    404 and was reported to the caller as "No results found for this query."
  * No drugcharacterization clause is emitted. It filters the report, not the
    matched drug, so it never meant "suspect only". Role basis is now explicit.
  * Case records are projected to compact cards by default. limit=10 on the raw
    records measured 724 KB (~181,000 tokens).
  * Date buckets are keyed "time", not "term"; reading "term" made every yearly
    trend come back empty.
  * The single SIGNAL DETECTED banner is replaced by separately named criteria.

Interface conventions:

  * Tool arguments are flat (drug_name, events, ...), not wrapped in a params
    object, so clients that send flat JSON validate.
  * Tools return dicts. FastMCP publishes an outputSchema and sends the dict as
    structuredContent alongside the JSON text.
  * Failures raise ToolError carrying a {code, reason, recovery} payload, so
    the MCP result is marked isError rather than looking like a success.
  * A date window or raw_filter is applied to EVERY cell of a contingency table,
    including the grand total N, or to none.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from faers import __version__
from faers.client import MAX_SKIP, get_client
from faers.errors import FaersError, InvalidQuery, NotComputable
from faers.fields import describe
from faers.projection import FIELDS_OMITTED, project, verify_suspect
from faers.query import (
    DISCLAIMER,
    F_PT,
    F_REPORT_ID,
    F_SUBSTANCE,
    ROLE_ANY,
    ROLE_LABELS,
    ROLE_SUSPECT_VERIFIED,
    combine,
    date_clause,
    drug_clause,
    event_clause,
    normalise_substance,
    phrase,
    term_clause,
    validate_raw_query,
)
from faers.screen import MODE_DRUG, MODE_EVENT, run_screen
from faers.stats import build_cells, disproportionality, evaluate_criteria, mantel_haenszel
from faers.strata import (
    AGE,
    describe_stratifiers,
    get_stratifier,
    stratified_totals,
)

mcp = FastMCP(
    "faers_mcp",
    instructions=(
        "Pharmacovigilance tools over the openFDA FAERS drug/event endpoint. Counts are "
        "any-role and not de-duplicated; see each payload's disclaimer. For a signal "
        "work-up use the faers_signal_workup prompt. The first faers_ebgm call for a "
        "FAERS release builds a background table (~100 s); call faers_warm_cache first "
        "or raise the client timeout."
    ),
)
# FastMCP 1.x reports the mcp library's version in serverInfo unless told otherwise.
mcp._mcp_server.version = __version__

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

# ─────────────────────────────────────────────
# Shared argument types
# ─────────────────────────────────────────────
DrugName = Annotated[
    str,
    Field(description="Active substance name, e.g. 'EMPAGLIFLOZIN'.", min_length=1, max_length=200),
]
Events = Annotated[
    Optional[list[str]],
    Field(description="MedDRA preferred terms, e.g. ['PANCREATITIS']. Case-insensitive."),
]
DateFrom = Annotated[
    Optional[str],
    Field(description="Start of a receivedate window, YYYYMMDD. Must be paired with date_to.", pattern=r"^\d{8}$"),
]
DateTo = Annotated[
    Optional[str],
    Field(description="End of a receivedate window, YYYYMMDD. Must be paired with date_from.", pattern=r"^\d{8}$"),
]
RawFilter = Annotated[
    Optional[str],
    Field(
        description=(
            "Extra Lucene clause ANDed onto the preset query, e.g. 'patient.patientsex:2'. "
            "Applied to every marginal of a contingency table. See faers_describe_fields."
        )
    ),
]
RoleBasis = Literal["any", "suspect_verified"]
StratifyBy = Literal["sex", "age", "year", "age_sex"]
EbgmStratifyBy = Literal["sex", "age", "year", "age_sex"]
ScreenMode = Literal["drug", "event"]
ScreenSort = Literal["ror_lower", "ror", "prr", "count"]
FieldCategory = Literal["drug", "reaction", "seriousness", "patient", "report"]

ROLE_BASIS_DESC = (
    "'any' counts every report naming the drug in any role. 'suspect_verified' is only "
    "computable where records are in hand (faers_search_cases, faers_raw_search)."
)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def fail(exc: FaersError) -> None:
    """Surface a structured failure as an MCP error result, not a success."""
    raise ToolError(json.dumps(exc.to_dict()["error"]))


def restriction(date_from: Optional[str], date_to: Optional[str], raw_filter: Optional[str]) -> Optional[str]:
    """The window + raw filter, as one clause applied uniformly to every cell."""
    parts = [date_clause(date_from, date_to)]
    if raw_filter:
        parts.append(f"({validate_raw_query(raw_filter)})")
    return combine(*parts) or None


def window_note(date_from: Optional[str], date_to: Optional[str], raw_filter: Optional[str]) -> dict:
    return {
        "date_window": date_clause(date_from, date_to) or "none (all dates)",
        "raw_filter": raw_filter,
    }


def _filters(serious_only: bool = False, fatal_only: bool = False) -> list[str]:
    clauses = []
    if serious_only:
        clauses.append("serious:1")
    if fatal_only:
        clauses.append("seriousnessdeath:1")
    return clauses


def _require_any_basis(role_basis: str, tool: str) -> None:
    """Count-based tools cannot honour a suspect basis. Say so, don't fake it."""
    if role_basis == ROLE_SUSPECT_VERIFIED:
        raise NotComputable(
            reason=(
                f"{tool} counts reports server-side, and openFDA cannot scope a count "
                "to one drug's role within a report."
            ),
            recovery=(
                "Use role_basis='any' and read the role_basis_note, or call "
                "faers_search_cases, which verifies the suspect role per record."
            ),
        )


def _crude_vs_adjusted(crude: dict, adjusted: dict) -> dict:
    """Say plainly whether adjusting changed the answer."""
    if not (crude.get("valid") and adjusted.get("valid") and adjusted.get("ror")):
        return {"note": "Crude and adjusted estimates are not both computable."}

    c = crude["ror"]["value"]
    a = adjusted["ror"]["value"]
    change = (a - c) / c * 100 if c else None
    material = change is not None and abs(change) >= 10

    return {
        "crude_ror": c,
        "adjusted_ror": a,
        "percent_change": round(change, 1) if change is not None else None,
        "confounding_material": material,
        "interpretation": (
            f"Adjusting moved the ROR by {change:+.1f}%, which is material - the crude "
            "estimate was confounded by this factor."
            if material
            else f"Adjusting moved the ROR by {change:+.1f}%. This factor was not an "
            "important confounder here."
        )
        if change is not None
        else "No comparison available.",
    }


async def _progress(ctx: Optional[Context], done: float, total: float, message: str) -> None:
    """Report progress when a client is listening; never let it break a tool."""
    if ctx is None:
        return
    try:
        await ctx.report_progress(done, total, message)
    except Exception:  # noqa: BLE001 - outside a request there is no channel
        pass


COUNT_SEMANTICS = (
    "openFDA counts every value present in the matching reports; the search clause "
    "selects reports, not array elements. A count over a drug-filtered search therefore "
    "includes values contributed by other drugs on the same report."
)

CRITERIA_DEFINITIONS = {
    "ema_ror": "ROR lower 95% CI > 1 and a >= 3",
    "evans_prr": "PRR >= 2 and chi-square >= 4 and a >= 3",
}


# ─────────────────────────────────────────────
# TOOL 1: faers_search_cases
# ─────────────────────────────────────────────
@mcp.tool(name="faers_search_cases", annotations={"title": "Search FAERS Case Reports", **READ_ONLY})
async def faers_search_cases(
    drug_name: DrugName,
    events: Events = None,
    serious_only: Annotated[bool, Field(description="Restrict to serious cases (serious=1).")] = False,
    fatal_only: Annotated[bool, Field(description="Restrict to fatal cases (seriousnessdeath=1).")] = False,
    role_basis: Annotated[
        RoleBasis,
        Field(description="'suspect_verified' checks each returned record for the drug in a suspect role."),
    ] = ROLE_ANY,
    full: Annotated[
        bool,
        Field(description="Return complete ICSR records (~72 KB each) instead of compact cards."),
    ] = False,
    sort: Annotated[Optional[str], Field(description="openFDA sort, e.g. 'receivedate:desc'.")] = "receivedate:desc",
    limit: Annotated[int, Field(description="Records per page.", ge=1, le=100)] = 10,
    skip: Annotated[int, Field(description=f"Pagination offset (openFDA ceiling {MAX_SKIP}).", ge=0)] = 0,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
) -> dict[str, Any]:
    """Search FAERS for individual case safety reports (ICSRs).

    Returns compact triage cards by default: report id, dates, seriousness flags,
    suspect drugs, reactions and outcomes. Pass full=true for complete records.
    """
    try:
        clauses = [drug_clause(drug_name, role_basis)]
        if events:
            clauses.append(event_clause(events))
        clauses.extend(_filters(serious_only, fatal_only))
        query = combine(*clauses, restriction(date_from, date_to, raw_filter))
        data = await get_client().fetch(search=query, limit=limit, skip=skip, sort=sort)
    except FaersError as exc:
        fail(exc)

    total = data.get("meta", {}).get("results", {}).get("total", 0)
    records = data.get("results", []) or []

    verified_in_page = None
    if role_basis == ROLE_SUSPECT_VERIFIED:
        records = [r for r in records if verify_suspect(r, drug_name)]
        verified_in_page = len(records)

    output: dict[str, Any] = {
        "ok": True,
        "drug": normalise_substance(drug_name),
        "events": events,
        "filters": {
            "serious_only": serious_only,
            "fatal_only": fatal_only,
            **window_note(date_from, date_to, raw_filter),
        },
        "role_basis": role_basis,
        "role_basis_note": ROLE_LABELS[role_basis],
        "meta": {"total_any_role": total, "returned": len(records), "skip": skip, "limit": limit, "sort": sort},
        "effective_query": query,
        "results": project(records, drug_name, full=full),
        "disclaimer": DISCLAIMER,
    }
    if verified_in_page is not None:
        output["meta"]["suspect_verified_in_page"] = verified_in_page
        output["meta"]["note"] = (
            "total_any_role counts reports in any drug role; suspect verification is "
            "applied only to the records on this page."
        )
    if not full:
        output["fields_omitted"] = FIELDS_OMITTED
    return output


# ─────────────────────────────────────────────
# TOOL 2: faers_case_counts
# ─────────────────────────────────────────────
@mcp.tool(name="faers_case_counts", annotations={"title": "FAERS Case Counts", **READ_ONLY})
async def faers_case_counts(
    drug_name: DrugName,
    events: Events = None,
    role_basis: Annotated[RoleBasis, Field(description=ROLE_BASIS_DESC)] = ROLE_ANY,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
) -> dict[str, Any]:
    """Total, serious and fatal case counts for a drug, optionally by event.

    All counts are issued concurrently as a single round trip.
    """
    try:
        _require_any_basis(role_basis, "faers_case_counts")
        clauses = [drug_clause(drug_name, role_basis)]
        if events:
            clauses.append(event_clause(events))
        query = combine(*clauses, restriction(date_from, date_to, raw_filter))
        total, serious, fatal = await get_client().totals(
            [query, combine(query, "serious:1"), combine(query, "seriousnessdeath:1")]
        )
    except FaersError as exc:
        fail(exc)

    def pct(part: int) -> Optional[float]:
        return round(100 * part / total, 1) if total else None

    return {
        "ok": True,
        "drug": normalise_substance(drug_name),
        "events": events,
        "filters": window_note(date_from, date_to, raw_filter),
        "role_basis": role_basis,
        "role_basis_note": ROLE_LABELS[role_basis],
        "counts": {"total": total, "serious": serious, "fatal": fatal},
        "percentages": {"serious": pct(serious), "fatal": pct(fatal)},
        "effective_query": query,
        "disclaimer": DISCLAIMER,
    }


# ─────────────────────────────────────────────
# TOOL 3: faers_disproportionality
# ─────────────────────────────────────────────
@mcp.tool(name="faers_disproportionality", annotations={"title": "ROR / PRR from FAERS", **READ_ONLY})
async def faers_disproportionality(
    drug_name: DrugName,
    events: Annotated[list[str], Field(description="One or more MedDRA preferred terms.", min_length=1)],
    role_basis: Annotated[RoleBasis, Field(description=ROLE_BASIS_DESC)] = ROLE_ANY,
    stratify_by: Annotated[
        Optional[StratifyBy],
        Field(
            description=(
                "Adjust for a confounder by Mantel-Haenszel pooling. Reports crude and adjusted "
                "side by side with a Breslow-Day homogeneity test. Adds 4 calls per outer band."
            )
        ),
    ] = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
) -> dict[str, Any]:
    """Build the 2x2 contingency table and compute ROR, PRR and chi-square.

    Any date window or raw_filter is applied to all four marginals including the
    grand total N, so the table stays internally consistent. Screening criteria
    are reported individually by name rather than collapsed into a verdict.
    """
    try:
        _require_any_basis(role_basis, "faers_disproportionality")
        restrict = restriction(date_from, date_to, raw_filter)
        drug_q = combine(drug_clause(drug_name, role_basis), restrict)
        event_q = combine(event_clause(events), restrict)
        combo_q = combine(drug_clause(drug_name, role_basis), event_clause(events), restrict)

        client = get_client()
        grand_total, drug_total, event_total, a = await client.totals([restrict, drug_q, event_q, combo_q])

        stratified = None
        if stratify_by:
            strat = get_stratifier(stratify_by)
            n_k, drug_k, event_k, a_k = await asyncio.gather(
                stratified_totals(client, restrict, strat),
                stratified_totals(client, drug_q, strat),
                stratified_totals(client, event_q, strat),
                stratified_totals(client, combo_q, strat),
            )
            stratified = (strat, n_k, drug_k, event_k, a_k)
    except FaersError as exc:
        fail(exc)

    cells, flags = build_cells(a, drug_total, event_total, grand_total)
    result = disproportionality(cells, flags)
    criteria = evaluate_criteria(result)

    payload: dict[str, Any] = {
        "ok": True,
        "drug": normalise_substance(drug_name),
        "events": events,
        "filters": window_note(date_from, date_to, raw_filter),
        "role_basis": role_basis,
        "role_basis_note": ROLE_LABELS[role_basis],
        "marginals": {
            "grand_total_N": grand_total,
            "drug_total": drug_total,
            "event_total": event_total,
            "drug_and_event": a,
        },
        **result,
        "criteria_met": criteria,
        "effective_query": {"grand_total": restrict, "drug": drug_q, "event": event_q, "combined": combo_q},
        "disclaimer": DISCLAIMER,
    }

    if stratified:
        strat, n_k, drug_k, event_k, a_k = stratified
        rows, strata_cells = [], []
        for label in sorted(n_k):
            s_cells, s_flags = build_cells(a_k.get(label, 0), drug_k.get(label, 0), event_k.get(label, 0), n_k[label])
            strata_cells.append(s_cells)
            s_result = disproportionality(s_cells, s_flags)
            rows.append(
                {
                    "stratum": label,
                    "cells": s_cells.as_dict(),
                    "ror": s_result["ror"]["value"] if s_result["valid"] else None,
                    "ror_ci_lower_95": s_result["ror"]["ci_lower_95"] if s_result["valid"] else None,
                    "flags": s_result["flags"],
                }
            )
        adjusted = mantel_haenszel(strata_cells)
        covered = sum(n_k.values())
        payload["stratified"] = {
            "stratify_by": strat.name,
            "description": strat.description,
            "coverage": {
                "reports_in_strata": covered,
                "crude_total_N": grand_total,
                "percent_of_database": round(100 * covered / grand_total, 1) if grand_total else None,
                "note": (
                    "Reports missing the stratifying field cannot enter a stratum and are "
                    "excluded, so the adjusted estimate describes a smaller population than "
                    "the crude one. " + strat.coverage_note
                ),
            },
            "per_stratum": rows,
            "adjusted": adjusted,
            "comparison": _crude_vs_adjusted(result, adjusted),
        }

    if restrict:
        payload["restriction_note"] = (
            "The window/filter was applied to all four marginals including N, so the "
            "table is internally consistent and describes only the restricted stratum."
        )
    if len(events) > 1:
        payload["grouped_term_note"] = (
            "Multiple PTs were combined with OR. Cell 'a' counts reports carrying any "
            "of them, so this is a grouped-term analysis, not a single-PT analysis."
        )
    return payload


# ─────────────────────────────────────────────
# TOOL 4: faers_count_by_field
# ─────────────────────────────────────────────
@mcp.tool(name="faers_count_by_field", annotations={"title": "Aggregate FAERS by Field", **READ_ONLY})
async def faers_count_by_field(
    drug_name: DrugName,
    count_field: Annotated[
        str,
        Field(
            description=(
                "FAERS field to aggregate, e.g. 'patient.reaction.reactionmeddrapt.exact'. "
                "See faers_describe_fields for the catalogue."
            ),
            min_length=1,
        ),
    ],
    events: Events = None,
    top_n: Annotated[int, Field(description="Number of values to return.", ge=1, le=100)] = 10,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
) -> dict[str, Any]:
    """Aggregate FAERS reports for a drug by any field (SQL GROUP BY equivalent)."""
    try:
        clauses = [drug_clause(drug_name)]
        if events:
            clauses.append(event_clause(events))
        query = combine(*clauses, restriction(date_from, date_to, raw_filter))
        results = await get_client().counts(query, count_field, top_n)
    except FaersError as exc:
        fail(exc)

    return {
        "ok": True,
        "drug": normalise_substance(drug_name),
        "events": events,
        "filters": window_note(date_from, date_to, raw_filter),
        "count_field": count_field,
        "returned": len(results),
        "results": results[:top_n],
        "count_semantics": COUNT_SEMANTICS,
        "effective_query": query,
        "disclaimer": DISCLAIMER,
    }


# ─────────────────────────────────────────────
# TOOL 5: faers_top_events
# ─────────────────────────────────────────────
@mcp.tool(name="faers_top_events", annotations={"title": "Top Adverse Events for a Drug", **READ_ONLY})
async def faers_top_events(
    drug_name: DrugName,
    serious_only: Annotated[bool, Field(description="Restrict to serious cases.")] = False,
    top_n: Annotated[int, Field(description="Number of PTs to return.", ge=1, le=100)] = 20,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
) -> dict[str, Any]:
    """Top MedDRA preferred terms reported with a drug.

    Convenience form of faers_count_by_field with the reaction field preset.
    """
    try:
        clauses = [drug_clause(drug_name), *_filters(serious_only)]
        query = combine(*clauses, restriction(date_from, date_to, raw_filter))
        results = await get_client().counts(query, F_PT, top_n)
    except FaersError as exc:
        fail(exc)

    return {
        "ok": True,
        "drug": normalise_substance(drug_name),
        "serious_only": serious_only,
        "filters": window_note(date_from, date_to, raw_filter),
        "top_events": results[:top_n],
        "count_semantics": COUNT_SEMANTICS,
        "effective_query": query,
        "disclaimer": DISCLAIMER,
    }


# ─────────────────────────────────────────────
# TOOL 6: faers_get_report
# ─────────────────────────────────────────────
@mcp.tool(name="faers_get_report", annotations={"title": "Get Full FAERS Report by ID", **READ_ONLY})
async def faers_get_report(
    safetyreportid: Annotated[
        str,
        Field(
            description=(
                "FDA Safety Report ID. Usually a plain 8-digit number such as 10084081. "
                "A hyphenated form (1234567-1) is accepted and both forms are tried."
            ),
            pattern=r"^\d{6,10}(-\d{1,2})?$",
        ),
    ],
) -> dict[str, Any]:
    """Retrieve one complete ICSR by Safety Report ID.

    The only tool that returns an unprojected record; a single ICSR averages ~72 KB.
    """
    raw = safetyreportid.strip()
    candidates = [raw]
    if "-" in raw:
        candidates.append(raw.split("-", 1)[0])

    try:
        client = get_client()
        for candidate in candidates:
            data = await client.fetch(search=phrase(F_REPORT_ID, candidate), limit=1)
            results = data.get("results") or []
            if results:
                return {
                    "ok": True,
                    "safetyreportid": candidate,
                    "matched_on": "as supplied" if candidate == raw else "base id without version suffix",
                    "report": results[0],
                }
    except FaersError as exc:
        fail(exc)

    fail(
        InvalidQuery(
            reason=f"No FAERS report found for id {raw!r} (tried: {', '.join(candidates)}).",
            recovery=(
                "Confirm the id from a faers_search_cases result. FAERS ids in this "
                "endpoint are typically plain 8-digit numbers; the version lives in the "
                "separate safetyreportversion field."
            ),
        )
    )
    return {}  # unreachable; keeps type checkers content


# ─────────────────────────────────────────────
# TOOL 7: faers_demographic_profile
# ─────────────────────────────────────────────
SEX_MAP = {"0": "Unknown", "1": "Male", "2": "Female"}
AGE_GROUP_MAP = {"1": "Neonate", "2": "Infant", "3": "Child", "4": "Adolescent", "5": "Adult", "6": "Elderly"}
REPORTER_MAP = {"1": "Physician", "2": "Pharmacist", "3": "Other HCP", "4": "Lawyer", "5": "Consumer/Non-HCP"}


@mcp.tool(name="faers_demographic_profile", annotations={"title": "FAERS Demographic Profile", **READ_ONLY})
async def faers_demographic_profile(
    drug_name: DrugName,
    events: Events = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
) -> dict[str, Any]:
    """Sex, age, reporter qualification and country breakdown for a drug.

    Age is reported two ways: the coded patientagegroup (populated on ~18% of
    reports) and bands derived from patientonsetage in years (~55%), the same
    bands the stratifiers use. Each carries its own coverage against the total.
    """
    try:
        clauses = [drug_clause(drug_name)]
        if events:
            clauses.append(event_clause(events))
        query = combine(*clauses, restriction(date_from, date_to, raw_filter))

        client = get_client()
        total, sex, age_group, onset, reporter, country = await asyncio.gather(
            client.total(query),
            client.counts(query, "patient.patientsex", 10),
            client.counts(query, "patient.patientagegroup", 10),
            stratified_totals(client, query, AGE),
            client.counts(query, "primarysource.qualification", 10),
            # occurcountry is a text field: counting it without .exact began returning
            # openFDA 500s in September 2026, on every query. Every other text field
            # here already counts on .exact.
            client.counts(query, "occurcountry.exact", 15),
        )
    except FaersError as exc:
        fail(exc)

    def decode(rows: list[dict], mapping: dict) -> list[dict]:
        return [{"label": mapping.get(str(r["term"]), str(r["term"])), "count": r["count"]} for r in rows]

    def coverage(rows_total: int) -> Optional[float]:
        return round(100 * rows_total / total, 1) if total else None

    sex_rows = decode(sex, SEX_MAP)
    age_group_rows = decode(age_group, AGE_GROUP_MAP)
    onset_rows = [{"label": band, "count": onset[band]} for band in ("0-17", "18-64", "65+") if band in onset]

    return {
        "ok": True,
        "drug": normalise_substance(drug_name),
        "events": events,
        "filters": window_note(date_from, date_to, raw_filter),
        "total_reports": total,
        "sex_distribution": sex_rows,
        "sex_coverage_percent": coverage(sum(r["count"] for r in sex_rows)),
        "age_bands_from_onset_age": onset_rows,
        "age_bands_coverage_percent": coverage(sum(r["count"] for r in onset_rows)),
        "age_group_coded": age_group_rows,
        "age_group_coded_coverage_percent": coverage(sum(r["count"] for r in age_group_rows)),
        "age_note": (
            "patientagegroup is sparsely populated (~18% of reports database-wide); "
            "age_bands_from_onset_age uses patientonsetage in years (~55%) and matches "
            "the bands used by stratify_by='age'."
        ),
        "reporter_qualification": decode(reporter, REPORTER_MAP),
        "top_countries": country[:15],
        "count_semantics": COUNT_SEMANTICS,
        "effective_query": query,
        "disclaimer": DISCLAIMER,
    }


# ─────────────────────────────────────────────
# TOOL 8: faers_outcome_breakdown
# ─────────────────────────────────────────────
OUTCOME_MAP = {
    "1": "Recovered/Resolved",
    "2": "Recovering/Resolving",
    "3": "Not Recovered/Not Resolved",
    "4": "Recovered with Sequelae",
    "5": "Fatal",
    "6": "Unknown",
}

SERIOUSNESS_FIELDS = {
    "seriousnessdeath": "Death",
    "seriousnesshospitalization": "Hospitalization",
    "seriousnesslifethreatening": "Life-threatening",
    "seriousnessdisabling": "Disability",
    "seriousnesscongenitalanomali": "Congenital anomaly",
    "seriousnessother": "Medically significant",
}


@mcp.tool(name="faers_outcome_breakdown", annotations={"title": "FAERS Outcome Breakdown", **READ_ONLY})
async def faers_outcome_breakdown(
    drug_name: DrugName,
    events: Events = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
) -> dict[str, Any]:
    """Reaction outcomes and seriousness criteria for a drug, in one concurrent round trip."""
    try:
        clauses = [drug_clause(drug_name)]
        if events:
            clauses.append(event_clause(events))
        query = combine(*clauses, restriction(date_from, date_to, raw_filter))

        client = get_client()
        outcomes_task = client.counts(query, "patient.reaction.reactionoutcome", 10)
        serious_tasks = [client.total(combine(query, f"{field}:1")) for field in SERIOUSNESS_FIELDS]
        outcomes, *serious_counts = await asyncio.gather(outcomes_task, *serious_tasks)
    except FaersError as exc:
        fail(exc)

    return {
        "ok": True,
        "drug": normalise_substance(drug_name),
        "events": events,
        "filters": window_note(date_from, date_to, raw_filter),
        "outcome_distribution": [
            {"label": OUTCOME_MAP.get(str(r["term"]), str(r["term"])), "count": r["count"]} for r in outcomes
        ],
        "seriousness_criteria": [
            {"criterion": label, "count": count} for label, count in zip(SERIOUSNESS_FIELDS.values(), serious_counts)
        ],
        "count_semantics": COUNT_SEMANTICS,
        "effective_query": query,
        "disclaimer": DISCLAIMER,
    }


# ─────────────────────────────────────────────
# TOOL 9: faers_time_trend
# ─────────────────────────────────────────────
@mcp.tool(name="faers_time_trend", annotations={"title": "FAERS Yearly Reporting Trend", **READ_ONLY})
async def faers_time_trend(
    drug_name: DrugName,
    events: Events = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
) -> dict[str, Any]:
    """Yearly reporting trend for a drug, aggregated from receivedate.

    count=receivedate returns a complete date histogram irrespective of limit, so
    the yearly rollup is exact. Date buckets arrive keyed "time", not "term".
    """
    try:
        clauses = [drug_clause(drug_name)]
        if events:
            clauses.append(event_clause(events))
        query = combine(*clauses, restriction(date_from, date_to, raw_filter))
        client = get_client()
        rows = await client.counts(query, "receivedate", client.max_limit)
    except FaersError as exc:
        fail(exc)

    yearly: dict[str, int] = {}
    for row in rows:
        bucket = str(row.get("time") or row.get("term") or "")
        if len(bucket) >= 4:
            yearly[bucket[:4]] = yearly.get(bucket[:4], 0) + int(row.get("count", 0))

    trend = [{"year": y, "count": c} for y, c in sorted(yearly.items())]
    return {
        "ok": True,
        "drug": normalise_substance(drug_name),
        "events": events,
        "filters": window_note(date_from, date_to, raw_filter),
        "yearly_trend": trend,
        "total_years_with_data": len(trend),
        "total_reports": sum(yearly.values()),
        "effective_query": query,
        "disclaimer": DISCLAIMER,
    }


# ─────────────────────────────────────────────
# TOOL 10: faers_coreported_drugs
# ─────────────────────────────────────────────
@mcp.tool(name="faers_coreported_drugs", annotations={"title": "Co-reported Substances in FAERS", **READ_ONLY})
async def faers_coreported_drugs(
    drug_name: DrugName,
    events: Events = None,
    top_n: Annotated[int, Field(description="Number of co-reported substances.", ge=1, le=50)] = 15,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
) -> dict[str, Any]:
    """Substances most often appearing on the same reports as an index drug.

    Co-REPORTED at any role, not co-suspect: openFDA cannot restrict a count to
    substances carried as suspect, so the list is dominated by concomitant medication.
    """
    try:
        clauses = [drug_clause(drug_name)]
        if events:
            clauses.append(event_clause(events))
        query = combine(*clauses, restriction(date_from, date_to, raw_filter))
        rows = await get_client().counts(query, F_SUBSTANCE, top_n + 5)
    except FaersError as exc:
        fail(exc)

    target = normalise_substance(drug_name)
    filtered = [r for r in rows if str(r.get("term", "")).upper() != target][:top_n]
    return {
        "ok": True,
        "index_drug": target,
        "events": events,
        "filters": window_note(date_from, date_to, raw_filter),
        "top_coreported_substances": filtered,
        "interpretation_note": (
            "Co-reported at any role. openFDA cannot restrict a count to substances "
            "carried as suspect, so this list is dominated by concomitant medication."
        ),
        "count_semantics": COUNT_SEMANTICS,
        "effective_query": query,
        "disclaimer": DISCLAIMER,
    }


# ─────────────────────────────────────────────
# TOOL 11: faers_signal_screen
# ─────────────────────────────────────────────
@mcp.tool(name="faers_signal_screen", annotations={"title": "Bulk Disproportionality Screen", **READ_ONLY})
async def faers_signal_screen(
    term: Annotated[str, Field(description="Active substance (mode='drug') or MedDRA PT (mode='event').", min_length=1)],
    mode: Annotated[ScreenMode, Field(description="Screen a drug against its events, or an event against its drugs.")] = MODE_DRUG,
    top_n: Annotated[int, Field(description="How many terms to screen.", ge=1, le=500)] = 100,
    return_n: Annotated[int, Field(description="How many rows to return.", ge=1, le=200)] = 25,
    min_cases: Annotated[int, Field(description="Minimum case count for a row to be reported.", ge=0)] = 3,
    signals_only: Annotated[bool, Field(description="Return only rows meeting the EMA ROR criterion.")] = False,
    sort_by: Annotated[ScreenSort, Field(description="Statistic to sort rows by.")] = "ror_lower",
    max_fallback_calls: Annotated[
        int,
        Field(description="Cap on per-term lookups for terms absent from the cached global marginal table.", ge=0, le=300),
    ] = 60,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
    ctx: Optional[Context] = None,
) -> dict[str, Any]:
    """Screen a drug against all its reported events, or an event against all its drugs.

    ROR, PRR and chi-square for every term with named criteria. The database-wide
    marginals come from a single cached count call rather than one call per term,
    so a 200-term screen costs ~13 calls cold and ~10 warm. Any date window or
    raw_filter is applied to every marginal.
    """
    await _progress(ctx, 0, 3, "fetching index counts")
    try:
        result = await run_screen(
            get_client(),
            mode=mode,
            term=term,
            top_n=top_n,
            return_n=return_n,
            min_cases=min_cases,
            date_from=date_from,
            date_to=date_to,
            raw_filter=raw_filter,
            max_fallback_calls=max_fallback_calls,
            signals_only=signals_only,
            sort_by=sort_by,
        )
    except FaersError as exc:
        fail(exc)
    await _progress(ctx, 3, 3, "done")

    result["criteria_definitions"] = CRITERIA_DEFINITIONS
    result["filters"] = window_note(date_from, date_to, raw_filter)
    result["disclaimer"] = DISCLAIMER
    return result


# ─────────────────────────────────────────────
# TOOL 12: faers_raw_search
# ─────────────────────────────────────────────
@mcp.tool(name="faers_raw_search", annotations={"title": "Raw openFDA Query", **READ_ONLY})
async def faers_raw_search(
    search: Annotated[
        str,
        Field(
            description=(
                "Raw Lucene query, e.g. 'patient.patientsex:2 AND serious:1'. Join clauses with "
                "spaces (\" AND \", \" OR \"), never \"+AND+\"."
            ),
            min_length=1,
        ),
    ],
    count: Annotated[
        Optional[str],
        Field(description="Field to aggregate by. When set, results are {term, count} rows instead of records."),
    ] = None,
    drug_name: Annotated[
        Optional[str],
        Field(
            description=(
                "Optional substance to annotate on each returned card as suspect_verified "
                "(true when carried in a suspect role on that record). Annotates only; does "
                "not filter. Use faers_search_cases with role_basis='suspect_verified' to filter."
            )
        ),
    ] = None,
    full: Annotated[bool, Field(description="Return complete records rather than compact cards.")] = False,
    sort: Annotated[Optional[str], Field(description="e.g. 'receivedate:desc'.")] = None,
    limit: Annotated[int, Field(description="Records or count rows to return.", ge=1, le=1000)] = 10,
    skip: Annotated[int, Field(description=f"Pagination offset (openFDA ceiling {MAX_SKIP}).", ge=0)] = 0,
) -> dict[str, Any]:
    """Escape hatch: run an arbitrary Lucene query against the FAERS endpoint.

    The query is checked for balanced quotes and parentheses and for the "+AND+"
    mistake before being sent; the same compact projection and ceilings apply.
    """
    try:
        query = validate_raw_query(search)
        data = await get_client().fetch(search=query, count=count, limit=limit, skip=skip, sort=sort)
    except FaersError as exc:
        fail(exc)

    meta = data.get("meta", {}).get("results", {})
    rows = data.get("results", []) or []
    output: dict[str, Any] = {
        "ok": True,
        "effective_query": query,
        "count_field": count,
        "meta": {"total": meta.get("total"), "returned": len(rows), "skip": skip, "limit": limit},
        "disclaimer": DISCLAIMER,
    }
    if count:
        output["results"] = rows
        output["count_semantics"] = COUNT_SEMANTICS
    else:
        output["results"] = project(rows, drug_name, full=full)
        if not full:
            output["fields_omitted"] = FIELDS_OMITTED
    return output


# ─────────────────────────────────────────────
# TOOL 13: faers_describe_fields
# ─────────────────────────────────────────────
@mcp.tool(name="faers_describe_fields", annotations={"title": "FAERS Field Catalogue", **READ_ONLY})
async def faers_describe_fields(
    category: Annotated[Optional[FieldCategory], Field(description="Narrow to one category.")] = None,
) -> dict[str, Any]:
    """Searchable FAERS field paths with coded values and traps, plus the available stratifiers.

    Consult before writing a raw_filter or count_field rather than guessing a path.
    """
    return {
        "ok": True,
        "server_version": __version__,
        **describe(category),
        "stratifiers": describe_stratifiers(),
    }


# ─────────────────────────────────────────────
# TOOL 14: faers_ebgm
# ─────────────────────────────────────────────
try:  # numpy/scipy carry the MGPS fit; the other tools must not depend on them.
    from faers.background import build_background, build_stratified_marginals, fit_for_background
    from faers.ebgm import ebgm_for_cell, expected_count

    EBGM_AVAILABLE = True
    EBGM_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - exercised only without scipy
    EBGM_AVAILABLE = False
    EBGM_IMPORT_ERROR = str(exc)

BACKGROUND_DRUGS_DESC = (
    "Substances in the background table the prior is fitted on. Building it costs "
    "3 + this many calls, once per FAERS release, then it is cached."
)


def _require_ebgm() -> None:
    if not EBGM_AVAILABLE:
        raise NotComputable(
            reason=f"numpy/scipy are required for EBGM but could not be imported: {EBGM_IMPORT_ERROR}",
            recovery="pip install numpy scipy (they are in pyproject.toml), then retry.",
        )


@mcp.tool(name="faers_ebgm", annotations={"title": "EBGM / MGPS Bayesian Signal Scores", **READ_ONLY})
async def faers_ebgm(
    drug_name: DrugName,
    events: Annotated[Optional[list[str]], Field(description="Specific MedDRA PTs. Omit to score the drug's most-reported events.")] = None,
    top_n: Annotated[int, Field(description="Events to score when `events` is omitted.", ge=1, le=200)] = 25,
    min_cases: Annotated[int, Field(description="Minimum observed count for a row.", ge=0)] = 3,
    stratify_by: Annotated[
        Optional[EbgmStratifyBy],
        Field(
            description=(
                "Adjust expected counts for a confounder. Stratification enters MGPS only through E, "
                "so the prior is refitted. ~2 calls per stratum, cached. 'year' resolves its strata "
                "from the data and prunes negligible years."
            )
        ),
    ] = None,
    background_drugs: Annotated[int, Field(description=BACKGROUND_DRUGS_DESC, ge=5, le=500)] = 100,
    background_events: Annotated[int, Field(description="Terms in the background table.", ge=10, le=1000)] = 999,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    raw_filter: RawFilter = None,
    ctx: Optional[Context] = None,
) -> dict[str, Any]:
    """Empirical Bayes signal scores (EBGM with EB05/EB95) via the Gamma-Poisson Shrinker.

    EBGM shrinks the observed-to-expected ratio toward 1 in proportion to how
    little evidence supports it. EB05 is the conventional screening statistic;
    EB05 > 2 is the usual threshold. The prior is fitted across a drug x event
    background table, so the FIRST call for a FAERS release builds and caches
    that table (~100 s, 3 + background_drugs calls). Call faers_warm_cache
    first, or raise the client timeout. Later calls take a few seconds.
    """
    try:
        _require_ebgm()
        client = get_client()

        await _progress(ctx, 0, 4, "loading background table")
        background, from_cache, build_calls = await build_background(
            client,
            n_drugs=background_drugs,
            n_events=background_events,
            date_from=date_from,
            date_to=date_to,
        )

        strat = strat_marginals = strat_cached = None
        if stratify_by:
            await _progress(ctx, 1, 4, f"building {stratify_by} marginals")
            strat = get_stratifier(stratify_by)
            strat_marginals, strat_cached, strat_calls = await build_stratified_marginals(
                client,
                strat,
                date_from=date_from,
                date_to=date_to,
                n_drugs=background_drugs,
                n_events=background_events,
            )
            build_calls += strat_calls

        await _progress(ctx, 2, 4, "fitting prior")
        hyper, hyper_cached = fit_for_background(
            background, background_drugs, background_events, stratified=strat_marginals
        )

        drug = normalise_substance(drug_name)
        restrict = restriction(date_from, date_to, raw_filter)
        drug_query = combine(drug_clause(drug_name), restrict)

        await _progress(ctx, 3, 4, "scoring")
        extra_calls = 0
        drug_total = background.drug_totals.get(drug)
        counts = background.cells.get(drug)
        if drug_total is None or counts is None:
            drug_total, rows = await asyncio.gather(
                client.total(drug_query),
                client.counts(drug_query, F_PT, min(max(top_n, 50), client.max_limit)),
            )
            counts = {str(r["term"]).upper(): int(r["count"]) for r in rows}
            extra_calls = 2

        if events:
            wanted = [e.strip().upper() for e in events]
            # The background's cell rows are restricted to its event universe, so an
            # explicitly requested event may be absent while genuinely having cases.
            unknown = [t for t in wanted if t not in counts]
            if unknown:
                observed = await asyncio.gather(
                    *(client.total(combine(drug_query, term_clause(F_PT, t)[0])) for t in unknown)
                )
                counts = {**counts, **dict(zip(unknown, observed))}
                extra_calls += len(unknown)
        else:
            wanted = [t for t, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)][:top_n]
        wanted = [t for t in wanted if counts.get(t, 0) >= min_cases]

        event_totals = dict(background.event_totals)
        missing_marginals = [t for t in wanted if t not in event_totals]
        if missing_marginals:
            fetched = await asyncio.gather(
                *(client.total(combine(term_clause(F_PT, t)[0], restrict)) for t in missing_marginals)
            )
            event_totals.update(dict(zip(missing_marginals, fetched)))
            extra_calls += len(missing_marginals)

        if strat_marginals:
            if drug not in next(iter(strat_marginals.drug_k.values()), {}):
                per_stratum_drug = await stratified_totals(client, drug_query, strat)
                for stratum, value in per_stratum_drug.items():
                    strat_marginals.drug_k.setdefault(stratum, {})[drug] = value
                extra_calls += strat.calls_per_marginal
            needed = [t for t in wanted if any(t not in table for table in strat_marginals.event_k.values())]
            if needed:
                fetched = await asyncio.gather(
                    *(stratified_totals(client, combine(term_clause(F_PT, t)[0], restrict), strat) for t in needed)
                )
                for term_, per_stratum in zip(needed, fetched):
                    for stratum, value in per_stratum.items():
                        strat_marginals.event_k.setdefault(stratum, {})[term_] = value
                extra_calls += len(needed) * strat.calls_per_marginal
    except FaersError as exc:
        fail(exc)
    except ValueError as exc:
        fail(NotComputable(reason=str(exc), recovery="Increase background_drugs so the prior has enough cells to fit."))

    rows_out = []
    for term_ in wanted:
        observed = counts.get(term_, 0)
        event_total = event_totals.get(term_)
        if not event_total:
            rows_out.append(
                {
                    "event": term_,
                    "observed": observed,
                    "ebgm": None,
                    "flags": ["event_marginal_unavailable"],
                    "note": "No database-wide count could be obtained for this term.",
                }
            )
            continue
        expected = (
            strat_marginals.expected(drug, term_)
            if strat_marginals
            else expected_count(drug_total, event_total, background.grand_total)
        )
        scores = ebgm_for_cell(observed, expected, hyper)
        flags = ["marginal_fetched_on_demand"] if term_ in missing_marginals else []
        rows_out.append({"event": term_, **scores, "flags": flags, "eb05_exceeds_2": bool((scores["eb05"] or 0) > 2)})

    rows_out.sort(key=lambda r: (r.get("eb05") if r.get("eb05") is not None else -1), reverse=True)
    await _progress(ctx, 4, 4, "done")

    payload: dict[str, Any] = {
        "ok": True,
        "drug": drug,
        "filters": window_note(date_from, date_to, raw_filter),
        "rows": rows_out,
        "hyperparameters": hyper.as_dict(),
        "interpretation": {
            "ebgm": "Shrunk observed/expected reporting ratio. 1.0 means as expected.",
            "eb05": "5th percentile of the posterior. The conventional screening statistic; EB05 > 2 is the usual threshold.",
            "rrr": "Unshrunk observed/expected, shown for comparison.",
            "why_shrinkage": (
                "EBGM discounts ratios built on few cases, so sparse cells stop "
                "outranking well-evidenced ones the way raw ROR/PRR lets them."
            ),
        },
        "stratification": (
            {
                "stratify_by": strat.name,
                "description": strat.description,
                "strata": sorted(strat_marginals.n_k),
                "reports_in_strata": strat_marginals.coverage,
                "percent_of_database": (
                    round(100 * strat_marginals.coverage / background.grand_total, 1) if background.grand_total else None
                ),
                "coverage_note": (
                    "Reports missing the stratifying field contribute nothing to the "
                    "stratified expectation. " + strat.coverage_note
                ),
                "method": "E = sum_k (n_drug,k * n_event,k) / n_k; the prior is refitted on these expectations.",
                "marginals_from_cache": strat_cached,
                "strata_resolution": strat_marginals.resolution,
            }
            if strat_marginals
            else None
        ),
        "method_caveats": [
            (
                f"Stratified by {strat.name}. These values still will not reproduce FDA's "
                "published EBGMs: the background is truncated and the counts are not de-duplicated."
                if strat_marginals
                else "UNSTRATIFIED fit. FDA's MGPS stratifies by age, sex and report year; "
                "pass stratify_by to adjust. These values will not reproduce FDA's published EBGMs."
            ),
            "Expected counts use any-role marginals - openFDA cannot scope a count to one drug's role within a report.",
            "The prior is fitted on a truncated background, not all of FAERS.",
            "Zero-truncated likelihood: openFDA reports only co-occurring pairs.",
        ],
        "background": background.provenance(),
        "cost": {
            "background_from_cache": from_cache,
            "hyperparameters_from_cache": hyper_cached,
            "api_calls_this_request": build_calls + extra_calls,
        },
        "disclaimer": DISCLAIMER,
    }
    if background.n_cells < 1000:
        payload["background_warning"] = (
            f"The prior was fitted on only {background.n_cells} cells. Empirical Bayes borrows "
            "strength across the table, so a small background gives an unreliable prior - raise background_drugs."
        )
    return payload


# ─────────────────────────────────────────────
# TOOL 15: faers_warm_cache
# ─────────────────────────────────────────────
@mcp.tool(name="faers_warm_cache", annotations={"title": "Build / Inspect the EBGM Caches", **READ_ONLY})
async def faers_warm_cache(
    background_drugs: Annotated[int, Field(description=BACKGROUND_DRUGS_DESC, ge=5, le=500)] = 100,
    background_events: Annotated[int, Field(description="Terms in the background table.", ge=10, le=1000)] = 999,
    stratify_by: Annotated[
        Optional[EbgmStratifyBy],
        Field(description="Also build the per-stratum marginals and fit the stratified prior."),
    ] = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    ctx: Optional[Context] = None,
) -> dict[str, Any]:
    """Build the EBGM background table and fit the prior ahead of time.

    The first faers_ebgm call for a FAERS release takes ~100 s and 100+ API
    calls, which exceeds many MCP client timeouts. Calling this tool first moves
    that cost to a turn that expects it; subsequent faers_ebgm calls take a few
    seconds. Reports whether each cache was already warm.
    """
    try:
        _require_ebgm()
        client = get_client()
        await _progress(ctx, 0, 3, "background table")
        background, bg_cached, calls = await build_background(
            client, n_drugs=background_drugs, n_events=background_events, date_from=date_from, date_to=date_to
        )
        strat_marginals = strat_cached = None
        if stratify_by:
            await _progress(ctx, 1, 3, f"{stratify_by} marginals")
            strat = get_stratifier(stratify_by)
            strat_marginals, strat_cached, strat_calls = await build_stratified_marginals(
                client, strat, date_from=date_from, date_to=date_to, n_drugs=background_drugs, n_events=background_events
            )
            calls += strat_calls
        await _progress(ctx, 2, 3, "fitting prior")
        hyper, hyper_cached = fit_for_background(
            background, background_drugs, background_events, stratified=strat_marginals
        )
    except FaersError as exc:
        fail(exc)
    except ValueError as exc:
        fail(NotComputable(reason=str(exc), recovery="Increase background_drugs so the prior has enough cells to fit."))
    await _progress(ctx, 3, 3, "done")

    return {
        "ok": True,
        "background": {**background.provenance(), "was_cached": bg_cached},
        "stratified_marginals": (
            {"stratify_by": stratify_by, "strata": sorted(strat_marginals.n_k), "was_cached": strat_cached}
            if strat_marginals
            else None
        ),
        "hyperparameters": {**hyper.as_dict(), "was_cached": hyper_cached},
        "api_calls_this_request": calls,
        "note": "faers_ebgm calls with the same background_drugs / background_events / stratify_by / window are now fast.",
    }


# ─────────────────────────────────────────────
# PROMPT: faers_signal_workup
# ─────────────────────────────────────────────
@mcp.prompt(name="faers_signal_workup", description="Structured signal work-up for one drug, or one drug-event pair.")
def faers_signal_workup(
    drug_name: str,
    event: Optional[str] = None,
) -> str:
    """A fixed sequence of tool calls for a defensible FAERS signal assessment."""
    pair = f"{drug_name} and {event}" if event else drug_name
    steps = [
        f"Run a FAERS signal work-up for {pair}. Use only the faers_* tools, in this order, "
        "and quote the numbers each returns rather than estimating.",
        "",
        "1. faers_case_counts - total, serious and fatal counts. Note the role_basis_note: counts are any-role.",
        "2. faers_top_events (or faers_signal_screen with mode='drug', top_n=100) - what is most reported, "
        "with ROR/PRR and the named criteria. Do not report a single 'signal detected' verdict; "
        "report ema_ror and evans_prr separately.",
    ]
    if event:
        steps += [
            f"3. faers_disproportionality for {event} - the crude 2x2, then again with stratify_by='age' "
            "and stratify_by='sex'. Report crude and Mantel-Haenszel adjusted ROR side by side, the "
            "Breslow-Day p-value, and the stated coverage of each stratifier.",
            f"4. faers_ebgm with events=['{event}'], then with stratify_by='year'. Report EBGM and EB05, "
            "and whether EB05 exceeds 2. Call faers_warm_cache first if the background is not built.",
            f"5. faers_time_trend for {event} - is reporting rising, and does any spike coincide with a "
            "label change or regulatory communication?",
            f"6. faers_search_cases with events=['{event}'], role_basis='suspect_verified', limit=10 - "
            "how many of the top cases actually carry the drug as suspect?",
        ]
    else:
        steps += [
            "3. For the top three events by ROR lower bound, faers_disproportionality with stratify_by='age' "
            "and 'sex'. Report crude vs adjusted and the Breslow-Day result.",
            "4. faers_ebgm with stratify_by='year' for the same events. Report EBGM and EB05.",
            "5. faers_time_trend for the drug overall.",
        ]
    steps += [
        "",
        "Finish with the disclaimer every payload carries: openFDA counts are not de-duplicated, "
        "do not match the FAERS Public Dashboard, are reporting-rate comparisons rather than "
        "incidence, and cannot support causal inference. State any 'approximate_marginal' or "
        "'marginal_unavailable' flags encountered.",
    ]
    return "\n".join(steps)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> None:
    """Console entry point: `faers-mcp [--transport stdio|http] [--host] [--port]`.

    stdio is the default and what MCP clients spawn. Streamable HTTP
    serves one shared process on a URL; it carries the API key's quota and has no
    auth of its own, so it defaults to loopback and warns when bound elsewhere.
    Environment variables FAERS_MCP_TRANSPORT / FAERS_MCP_HOST / FAERS_MCP_PORT
    set the defaults so a config file need not pass flags.
    """
    import argparse
    import logging
    import os

    # httpx logs every request at INFO; on stdio that is stderr noise for the client.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(prog="faers-mcp", description="openFDA FAERS pharmacovigilance MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("FAERS_MCP_TRANSPORT", "stdio"),
        help="stdio for desktop clients (default); http for a shared Streamable HTTP endpoint.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("FAERS_MCP_HOST", "127.0.0.1"),
        help="Bind address for http (default 127.0.0.1 - do not expose publicly).",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("FAERS_MCP_PORT", "8010")), help="Port for http (default 8010).")
    parser.add_argument("--version", action="version", version=f"faers-mcp {__version__}")
    args = parser.parse_args(argv)

    if args.transport == "http":
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(
                f"WARNING: binding to {args.host}. The HTTP transport has no authentication; "
                "anyone who can reach /mcp spends this key's openFDA quota. Keep it on a "
                "private network or behind an authenticating proxy.",
                file=sys.stderr,
            )
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
