"""Contract tests against the live openFDA API.

These pin undocumented behaviour the server depends on. They are skipped by
default; run them with `pytest -m live` when you want to know whether openFDA
has changed under you. A failure here is a signal that openFDA's undocumented
behaviour shifted, not necessarily a bug in this repo.
"""

from __future__ import annotations

import pytest

from faers.client import OpenFdaClient
from faers.query import F_PT, F_SUBSTANCE, combine, phrase

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


@pytest.fixture
async def client():
    c = OpenFdaClient()
    try:
        yield c
    finally:
        await c.aclose()


async def test_substance_exact_is_case_sensitive(client):
    """Uppercasing drug_name is required, not cosmetic."""
    upper = await client.total(phrase(F_SUBSTANCE, "EMPAGLIFLOZIN"))
    mixed = await client.total(phrase(F_SUBSTANCE, "Empagliflozin"))

    assert upper > 0
    assert mixed == 0, "if this passes, substance .exact became case-insensitive"


async def test_pt_exact_is_case_insensitive(client):
    """Why no dual-casing search strategy is needed for MedDRA terms."""
    upper = await client.total(phrase(F_PT, "ACUTE KIDNEY INJURY"))
    title = await client.total(phrase(F_PT, "Acute kidney injury"))

    assert upper > 0
    assert upper == title, "if this fails, PT .exact became case-sensitive"


async def test_drugcharacterization_does_not_scope_to_the_matched_drug(client):
    """The reason 'suspect_only' was removed.

    If openFDA ever makes this clause drug-scoped, the two filtered counts would
    partition the total instead of overlapping, and a real suspect-only basis
    would become available.
    """
    aspirin = phrase(F_SUBSTANCE, "ASPIRIN")
    any_role = await client.total(aspirin)
    suspect = await client.total(combine(aspirin, "patient.drug.drugcharacterization:1"))
    concomitant = await client.total(combine(aspirin, "patient.drug.drugcharacterization:2"))

    assert suspect + concomitant > any_role * 1.5, (
        "clauses no longer overlap; drugcharacterization may now be drug-scoped"
    )


async def test_space_joined_and_actually_works(client):
    """The regression that made every tool return nothing."""
    drug = phrase(F_SUBSTANCE, "EMPAGLIFLOZIN")
    event = phrase(F_PT, "PANCREATITIS")

    combined = await client.total(combine(drug, event))
    assert combined > 0
    assert combined < await client.total(drug)


async def test_plus_joined_and_returns_nothing(client):
    """Documents the failure mode, so nobody reintroduces '+AND+'."""
    drug = phrase(F_SUBSTANCE, "EMPAGLIFLOZIN")
    event = phrase(F_PT, "PANCREATITIS")

    assert await client.total(f"{drug}+AND+{event}") == 0


async def test_receivedate_count_ignores_limit(client):
    """faers_time_trend relies on getting a complete histogram, not a top-N."""
    drug = phrase(F_SUBSTANCE, "EMPAGLIFLOZIN")
    rows = await client.counts(drug, "receivedate", 100)
    total = await client.total(drug)

    assert len(rows) > 100, "limit no longer ignored for date counts"
    assert sum(r["count"] for r in rows) == total, "histogram is no longer complete"


async def test_date_counts_are_keyed_time_not_term(client):
    """Reading 'term' on a date histogram silently produced an empty trend."""
    rows = await client.counts(phrase(F_SUBSTANCE, "EMPAGLIFLOZIN"), "receivedate", 50)

    assert rows
    assert "time" in rows[0], "date buckets are no longer keyed 'time'"
    assert "term" not in rows[0]


async def test_non_date_counts_are_keyed_term(client):
    rows = await client.counts(phrase(F_SUBSTANCE, "EMPAGLIFLOZIN"), F_PT, 5)

    assert rows
    assert "term" in rows[0]


async def test_caret_terms_cannot_be_matched_exactly(client):
    """FAERS stores apostrophes as '^' and openFDA rejects the character in a search.

    The count API returns CROHN^S DISEASE happily; feeding it back into a query
    is a BAD_REQUEST however it is escaped. If this test ever fails, openFDA has
    fixed the asymmetry and term_clause() can drop its fallback.
    """
    from faers.errors import UpstreamError

    with pytest.raises(UpstreamError):
        await client.total(phrase(F_PT, "CROHN^S DISEASE"))


async def test_relaxed_phrase_fallback_matches_caret_terms(client):
    """The fallback is over-inclusive, but only slightly - measured +0.08% here."""
    from faers.query import term_clause

    clause, approximate = term_clause(F_PT, "CROHN^S DISEASE")
    assert approximate is True

    approx_total = await client.total(clause)
    rows = await client.counts(None, F_PT, 999)
    exact_total = next(r["count"] for r in rows if r["term"] == "CROHN^S DISEASE")

    assert approx_total >= exact_total
    assert (approx_total - exact_total) / exact_total < 0.05, "fallback drifted badly"


async def test_safetyreportid_is_plain_digits(client):
    """The pattern must accept unhyphenated ids; it previously rejected all of them."""
    import re

    data = await client.fetch(search=phrase(F_SUBSTANCE, "EMPAGLIFLOZIN"), limit=5)
    ids = [r["safetyreportid"] for r in data["results"]]

    assert ids
    assert all(re.fullmatch(r"\d{6,10}(-\d{1,2})?", i) for i in ids)
    assert any("-" not in i for i in ids), "expected plain unhyphenated ids"
