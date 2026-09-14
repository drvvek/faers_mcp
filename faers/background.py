"""The drug x event background table that MGPS is fitted against.

EBGM cannot be computed for one pair in isolation: the gamma mixture prior is
estimated across a whole table, and that shared prior is what supplies the
shrinkage. So a background has to be assembled once and reused.

Cost is 3 + n_drugs calls:

    1  global substance counts   -> drug marginals n_i.
    1  global PT counts          -> event marginals n_.j
    1  grand total               -> N
    D  one count per drug        -> the cells n_ij

For n_drugs=100 that is 103 calls, cached on openFDA's meta.last_updated so a
FAERS refresh invalidates it. Fitted hyperparameters are cached alongside, so
the expensive part happens once per data release rather than once per question.

Two honest limitations, both surfaced in the payload:

* The background covers the top D substances and top E terms, not all of FAERS.
  A truncated background gives a slightly different prior than the full table.
* Each drug's cell row is itself capped at the count API's 1000-value ceiling,
  so a very promiscuous drug's rarest events are missing from the table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .client import OpenFdaClient
from .ebgm import Hyperparameters, expected_count, fit_hyperparameters
from .errors import FaersError
from .query import F_PT, F_SUBSTANCE, combine, date_clause, term_clause
from .screen import cache_dir

BUILD_CONCURRENCY = 5


@dataclass
class StratifiedMarginals:
    """Per-stratum marginals, enough to compute a stratified expected count.

    DuMouchel's stratified MGPS leaves the observed count N alone and changes
    only the expectation:

        E_ij = sum_k (n_i.k * n_.jk) / n_..k

    so stratification costs 2K+1 extra calls (one substance count and one event
    count inside each stratum, plus the stratum sizes) and nothing else changes.
    """

    strat_name: str
    n_k: dict[str, int]
    drug_k: dict[str, dict[str, int]]
    event_k: dict[str, dict[str, int]]
    resolution: dict = field(default_factory=dict)

    def expected(self, drug: str, event: str) -> float:
        total = 0.0
        for stratum, size in self.n_k.items():
            if size <= 0:
                continue
            n_drug = self.drug_k.get(stratum, {}).get(drug, 0)
            n_event = self.event_k.get(stratum, {}).get(event, 0)
            if n_drug and n_event:
                total += (n_drug * n_event) / size
        return total

    @property
    def coverage(self) -> int:
        return sum(self.n_k.values())

    def to_json(self) -> str:
        return json.dumps(
            {
                "strat_name": self.strat_name,
                "n_k": self.n_k,
                "drug_k": self.drug_k,
                "event_k": self.event_k,
                "resolution": self.resolution,
            }
        )

    @classmethod
    def from_json(cls, blob: str) -> "StratifiedMarginals":
        return cls(**json.loads(blob))


@dataclass
class Background:
    drug_totals: dict[str, int]
    event_totals: dict[str, int]
    cells: dict[str, dict[str, int]]
    grand_total: int
    last_updated: str
    window: str
    built_at: str
    partial_drugs: list[str] = field(default_factory=list)

    def observed_pairs(
        self, stratified: Optional[StratifiedMarginals] = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """(N, E) over every observed cell, for the likelihood.

        With `stratified`, the expectation is the sum of stratum-specific
        expectations; the observed counts are unchanged.
        """
        counts, expected = [], []
        for drug, events in self.cells.items():
            drug_total = self.drug_totals.get(drug)
            if not drug_total:
                continue
            for event, n in events.items():
                event_total = self.event_totals.get(event)
                if not event_total:
                    continue
                e = (
                    stratified.expected(drug, event)
                    if stratified
                    else expected_count(drug_total, event_total, self.grand_total)
                )
                if e > 0:
                    counts.append(n)
                    expected.append(e)
        return np.asarray(counts, dtype=float), np.asarray(expected, dtype=float)

    @property
    def n_cells(self) -> int:
        return sum(len(v) for v in self.cells.values())

    def provenance(self) -> dict:
        return {
            "drugs_in_background": len(self.drug_totals),
            "events_in_background": len(self.event_totals),
            "observed_cells": self.n_cells,
            "grand_total_N": self.grand_total,
            "faers_last_updated": self.last_updated,
            "date_window": self.window,
            "built_at": self.built_at,
            "truncation_note": (
                "Background covers the most-reported substances and terms, not all of "
                "FAERS; the prior is fitted on that subset."
            ),
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "drug_totals": self.drug_totals,
                "event_totals": self.event_totals,
                "cells": self.cells,
                "grand_total": self.grand_total,
                "last_updated": self.last_updated,
                "window": self.window,
                "built_at": self.built_at,
                "partial_drugs": self.partial_drugs,
            }
        )

    @classmethod
    def from_json(cls, blob: str) -> "Background":
        d = json.loads(blob)
        return cls(**d)


def _key(n_drugs: int, n_events: int, window: str, last_updated: str) -> str:
    return hashlib.sha256(f"{n_drugs}|{n_events}|{window}|{last_updated}".encode()).hexdigest()[:16]


def _background_path(key: str):
    return cache_dir() / f"background_{key}.json"


def _hyper_path(key: str):
    return cache_dir() / f"hyper_{key}.json"


async def build_background(
    client: OpenFdaClient,
    n_drugs: int = 100,
    n_events: int = 999,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    use_cache: bool = True,
) -> tuple[Background, bool, int]:
    """Assemble (or load) the background table. Returns (table, from_cache, calls)."""
    window_clause = date_clause(date_from, date_to)
    window = window_clause or "all"

    meta = await client.fetch(search=window_clause, limit=1)
    calls = 1
    grand_total = int(meta.get("meta", {}).get("results", {}).get("total", 0) or 0)
    last_updated = meta.get("meta", {}).get("last_updated") or "unknown"

    key = _key(n_drugs, n_events, window, last_updated)
    path = _background_path(key)
    if use_cache and path.exists():
        try:
            return Background.from_json(path.read_text()), True, calls
        except (json.JSONDecodeError, TypeError, KeyError, OSError):
            pass

    drug_rows, event_rows = await asyncio.gather(
        client.counts(window_clause, F_SUBSTANCE, min(n_drugs, client.max_limit)),
        client.counts(window_clause, F_PT, min(n_events, client.max_limit)),
    )
    calls += 2

    drug_totals = {str(r["term"]).upper(): int(r["count"]) for r in drug_rows}
    event_totals = {str(r["term"]).upper(): int(r["count"]) for r in event_rows}

    semaphore = asyncio.Semaphore(BUILD_CONCURRENCY)
    partial: list[str] = []

    async def row_for(drug: str) -> tuple[str, dict[str, int]]:
        async with semaphore:
            clause, _ = term_clause(F_SUBSTANCE, drug)
            try:
                rows = await client.counts(
                    combine(clause, window_clause), F_PT, client.max_limit
                )
            except FaersError:
                return drug, {}
            if len(rows) >= client.max_limit:
                partial.append(drug)
            # Keep only events that are in the background's event universe, so
            # every cell has a usable marginal.
            return drug, {
                str(r["term"]).upper(): int(r["count"])
                for r in rows
                if str(r["term"]).upper() in event_totals
            }

    results = await asyncio.gather(*(row_for(d) for d in drug_totals))
    calls += len(drug_totals)

    background = Background(
        drug_totals=drug_totals,
        event_totals=event_totals,
        cells={d: c for d, c in results if c},
        grand_total=grand_total,
        last_updated=last_updated,
        window=window,
        built_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        partial_drugs=sorted(partial),
    )

    try:
        path.write_text(background.to_json())
    except OSError:
        pass

    return background, False, calls


async def build_stratified_marginals(
    client: OpenFdaClient,
    strat,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    n_drugs: int = 100,
    n_events: int = 999,
    use_cache: bool = True,
) -> tuple[StratifiedMarginals, bool, int]:
    """Per-stratum drug and event marginals. Returns (marginals, from_cache, calls)."""
    from .strata import resolve_strata, stratified_totals, stratum_field_counts

    window_clause = date_clause(date_from, date_to)
    window = window_clause or "all"

    meta = await client.fetch(search=window_clause, limit=1)
    calls = 1
    last_updated = meta.get("meta", {}).get("last_updated") or "unknown"

    key = _key(n_drugs, n_events, f"{window}|strat={strat.name}", last_updated)
    path = cache_dir() / f"strata_{key}.json"
    if use_cache and path.exists():
        try:
            return StratifiedMarginals.from_json(path.read_text()), True, calls
        except (json.JSONDecodeError, TypeError, KeyError, OSError):
            pass

    # Year strata are not known in advance; resolving them also measures each
    # stratum's size, so that count is not repeated.
    clauses, resolution = await resolve_strata(client, strat, window_clause)
    calls += resolution["resolve_calls"]

    sizes = resolution.pop("stratum_sizes", None)
    if sizes is None:
        n_k = await stratified_totals(client, window_clause, strat)
        calls += strat.calls_per_marginal
    else:
        n_k = sizes

    drug_k, event_k = await asyncio.gather(
        stratum_field_counts(client, F_SUBSTANCE, clauses, limit=min(n_drugs, client.max_limit)),
        stratum_field_counts(client, F_PT, clauses, limit=min(n_events, client.max_limit)),
    )
    calls += 2 * len(clauses)

    marginals = StratifiedMarginals(
        strat_name=strat.name,
        n_k=n_k,
        drug_k=drug_k,
        event_k=event_k,
        resolution=resolution,
    )
    try:
        path.write_text(marginals.to_json())
    except OSError:
        pass

    return marginals, False, calls


def fit_for_background(
    background: Background,
    n_drugs: int,
    n_events: int,
    use_cache: bool = True,
    stratified: Optional[StratifiedMarginals] = None,
) -> tuple[Hyperparameters, bool]:
    """Fit (or load) the MGPS hyperparameters for a background. Returns (hyper, cached).

    A stratified expectation changes the likelihood, so it gets its own cache
    entry - the prior fitted against crude E is not valid for stratified E.
    """
    window = background.window
    if stratified:
        window = f"{window}|strat={stratified.strat_name}"
    key = _key(n_drugs, n_events, window, background.last_updated)
    path = _hyper_path(key)

    if use_cache and path.exists():
        try:
            return Hyperparameters(**json.loads(path.read_text())), True
        except (json.JSONDecodeError, TypeError, KeyError, OSError):
            pass

    counts, expected = background.observed_pairs(stratified)
    hyper = fit_hyperparameters(counts, expected, truncated=True)

    try:
        path.write_text(json.dumps(hyper.as_dict()))
    except OSError:
        pass

    return hyper, False
