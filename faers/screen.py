"""Bulk disproportionality screen.

The companion AEMS Chrome extension (the browser tool this server was derived
from) screens a drug by fetching its top-500 PTs and then issuing
one count call *per event* to get that event's database-wide total - roughly 502
calls throttled at 300 ms, several minutes per run. In a chat tool that is
unusable and would spend half a keyless daily quota on a single question.

Those database-wide marginals are all obtainable in ONE call:

    ?count=patient.reaction.reactionmeddrapt.exact&limit=999

which returns the global top-999 PTs with their counts - exactly the per-event
total the extension asks for one at a time. Measured for EMPAGLIFLOZIN:

    global marginals cached in 1 call    999
    drug's top PTs                       500
    covered by that single call          444/500 = 88.8%
    per-PT fallback calls needed          56
    TOTAL                                 58 calls   (vs ~502)

The table is cached on meta.last_updated, so repeat screens for other drugs skip
the global call entirely.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import time
from typing import Optional

from .client import OpenFdaClient
from .errors import FaersError, InvalidQuery
from .query import (
    F_PT,
    F_SUBSTANCE,
    combine,
    date_clause,
    normalise_substance,
    phrase,
    term_clause,
    validate_raw_query,
)
from .stats import build_cells, disproportionality, evaluate_criteria

MODE_DRUG = "drug"
MODE_EVENT = "event"

# Cap concurrent fallback lookups so a screen cannot hammer the API.
FALLBACK_CONCURRENCY = 5


def cache_dir() -> pathlib.Path:
    override = os.environ.get("FAERS_CACHE_DIR")
    path = pathlib.Path(override) if override else pathlib.Path.home() / ".faers_mcp_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


class MarginalCache:
    """Disk cache for the global marginal tables.

    Keyed on the count field, the date window and openFDA's own
    meta.last_updated, so a FAERS refresh invalidates it automatically.
    """

    def __init__(self, directory: Optional[pathlib.Path] = None) -> None:
        self.directory = directory or cache_dir()

    def _path(self, field: str, window: str, last_updated: str) -> pathlib.Path:
        digest = hashlib.sha256(f"{field}|{window}|{last_updated}".encode()).hexdigest()[:16]
        return self.directory / f"marginals_{digest}.json"

    def get(self, field: str, window: str, last_updated: str) -> Optional[dict[str, int]]:
        path = self._path(field, window, last_updated)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())["marginals"]
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def put(self, field: str, window: str, last_updated: str, marginals: dict[str, int]) -> None:
        try:
            self._path(field, window, last_updated).write_text(
                json.dumps(
                    {
                        "field": field,
                        "window": window,
                        "last_updated": last_updated,
                        "cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "marginals": marginals,
                    }
                )
            )
        except OSError:
            pass  # A cache miss is survivable; a crash is not.


async def global_marginals(
    client: OpenFdaClient,
    count_field: str,
    window_clause: Optional[str],
    last_updated: str,
    cache: Optional[MarginalCache] = None,
) -> tuple[dict[str, int], bool]:
    """Database-wide totals per term, in one call. Returns (table, was_cached)."""
    cache = cache or MarginalCache()
    window = window_clause or "all"

    cached = cache.get(count_field, window, last_updated)
    if cached is not None:
        return cached, True

    rows = await client.counts(window_clause, count_field, client.max_limit)
    table = {str(r["term"]).upper(): int(r["count"]) for r in rows if r.get("term") is not None}
    cache.put(count_field, window, last_updated, table)
    return table, False


async def run_screen(
    client: OpenFdaClient,
    mode: str,
    term: str,
    top_n: int = 100,
    return_n: int = 25,
    min_cases: int = 3,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    raw_filter: Optional[str] = None,
    max_fallback_calls: int = 60,
    signals_only: bool = False,
    sort_by: str = "ror_lower",
    cache: Optional[MarginalCache] = None,
) -> dict:
    """Screen one drug against its events, or one event against its drugs.

    The 2x2 for every row shares one role basis (any role) and one restriction -
    the date window and any raw_filter - applied to the index query, the grand
    total N, the global marginals and every fallback lookup alike. Restricting
    three cells but not the fourth would not be a contingency table.
    """
    if mode not in (MODE_DRUG, MODE_EVENT):
        raise InvalidQuery(reason=f"Unknown mode {mode!r}.", recovery="Use mode='drug' or mode='event'.")

    parts = [date_clause(date_from, date_to)]
    if raw_filter:
        parts.append(f"({validate_raw_query(raw_filter)})")
    # Named `window` throughout for brevity; it is the whole restriction.
    window = combine(*parts) or None

    if mode == MODE_DRUG:
        index_clause, index_approx = term_clause(F_SUBSTANCE, normalise_substance(term))
        item_field = F_PT
        item_label = "event"
    else:
        index_clause, index_approx = term_clause(F_PT, term.strip())
        item_field = F_SUBSTANCE
        item_label = "drug"

    index_query = combine(index_clause, window)
    calls = {"total": 0, "fallback": 0, "global_cached": False}

    # Grand total N, the index total, this index's item counts, and the API's
    # own data-currency stamp - all concurrently.
    meta_task = client.fetch(search=window, limit=1)
    index_total_task = client.total(index_query)
    items_task = client.counts(index_query, item_field, min(top_n, client.max_limit))
    meta, index_total, item_rows = await asyncio.gather(meta_task, index_total_task, items_task)
    calls["total"] += 3

    grand_total = int(meta.get("meta", {}).get("results", {}).get("total", 0) or 0)
    last_updated = meta.get("meta", {}).get("last_updated") or "unknown"

    if not item_rows:
        return {
            "ok": True,
            "mode": mode,
            "term": term,
            "rows": [],
            "note": f"No {item_label}s found for {term!r} in the selected window.",
            "audit": _audit(term, mode, window, last_updated, grand_total, index_total, calls, 0),
        }

    counts = {str(r["term"]).upper(): int(r["count"]) for r in item_rows}

    marginals, was_cached = await global_marginals(client, item_field, window, last_updated, cache)
    calls["global_cached"] = was_cached
    if not was_cached:
        calls["total"] += 1

    missing = [name for name in counts if name not in marginals]
    truncated_fallback = False
    if len(missing) > max_fallback_calls:
        missing, truncated_fallback = missing[:max_fallback_calls], True

    approximate: set[str] = set()
    if missing:
        semaphore = asyncio.Semaphore(FALLBACK_CONCURRENCY)

        async def lookup(name: str) -> tuple[str, Optional[int], bool]:
            """One term's marginal. A failure here must not sink the whole screen."""
            async with semaphore:
                clause, is_approx = term_clause(item_field, name)
                try:
                    return name, await client.total(combine(clause, window)), is_approx
                except FaersError:
                    return name, None, is_approx

        for name, total, is_approx in await asyncio.gather(*(lookup(n) for n in missing)):
            if total is not None:
                marginals[name] = total
                if is_approx:
                    approximate.add(name)
        calls["fallback"] = len(missing)
        calls["total"] += len(missing)

    rows = []
    for name, a in counts.items():
        item_total = marginals.get(name)
        if item_total is None:
            rows.append(
                {
                    item_label: name,
                    "count": a,
                    "valid": False,
                    "flags": ["marginal_unavailable"],
                    "note": "Database-wide total not retrieved; no metric computed.",
                }
            )
            continue

        if mode == MODE_DRUG:
            drug_total, event_total = index_total, item_total
        else:
            drug_total, event_total = item_total, index_total

        cells, flags = build_cells(a, drug_total, event_total, grand_total)
        result = disproportionality(cells, flags)
        criteria = evaluate_criteria(result)

        flags = list(result["flags"])
        if name in approximate:
            flags.append("approximate_marginal")

        row = {
            item_label: name,
            "count": a,
            "valid": result["valid"],
            "flags": flags,
            "contingency_table": result["contingency_table"],
        }
        if result["valid"]:
            row.update(
                {
                    "ror": result["ror"]["value"],
                    "ror_lower_95": result["ror"]["ci_lower_95"],
                    "prr": result["prr"]["value"],
                    "prr_lower_95": result["prr"]["ci_lower_95"],
                    "chi_square": result["chi_square"],
                    "criteria_met": {k: v for k, v in criteria.items() if k != "definitions"},
                }
            )
        rows.append(row)

    screened = len(rows)
    rows = [r for r in rows if r["count"] >= min_cases]
    if signals_only:
        rows = [r for r in rows if r.get("criteria_met", {}).get("ema_ror")]

    key = {
        "ror_lower": lambda r: r.get("ror_lower_95") or -1,
        "ror": lambda r: r.get("ror") or -1,
        "prr": lambda r: r.get("prr") or -1,
        "count": lambda r: r["count"],
    }.get(sort_by, lambda r: r.get("ror_lower_95") or -1)
    rows.sort(key=key, reverse=True)

    returned = rows[:return_n]
    audit = _audit(term, mode, window, last_updated, grand_total, index_total, calls, screened)
    audit["rows_after_filters"] = len(rows)
    audit["rows_returned"] = len(returned)
    audit["min_cases"] = min_cases
    audit["sort_by"] = sort_by
    if index_approx:
        audit["index_term_approximate"] = (
            f"{term!r} contains a character openFDA cannot match exactly; a tokenised "
            "phrase match was used and is slightly over-inclusive."
        )
    if approximate:
        audit["approximate_marginals"] = sorted(approximate)
    if truncated_fallback:
        audit["fallback_truncated"] = (
            f"More than {max_fallback_calls} terms were absent from the global marginal "
            "table; the remainder are reported with flag 'marginal_unavailable'."
        )

    return {"ok": True, "mode": mode, "term": term, "rows": returned, "audit": audit}


def _audit(
    term: str,
    mode: str,
    window: Optional[str],
    last_updated: str,
    grand_total: int,
    index_total: int,
    calls: dict,
    screened: int,
) -> dict:
    """Provenance block, mirroring the CSV audit trail the AEMS export writes."""
    from . import __version__

    return {
        "tool_version": __version__,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": mode,
        "query_term": term,
        "restriction": window or "none (all dates, no filter)",
        "faers_last_updated": last_updated,
        "grand_total_N": grand_total,
        "index_total": index_total,
        "terms_screened": screened,
        "api_calls": calls["total"],
        "fallback_calls": calls["fallback"],
        "global_marginals_from_cache": calls["global_cached"],
        "method": (
            "Any-role marginals; conditional Haldane-Anscombe 0.5 applied only to "
            "zero-cell tables; criteria evaluated per named convention."
        ),
    }
