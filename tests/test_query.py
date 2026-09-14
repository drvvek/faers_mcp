"""Query construction, including the regression that made the server return nothing."""

from __future__ import annotations

import httpx
import pytest

from faers.errors import InvalidQuery
from faers.query import (
    F_PT,
    F_SUBSTANCE,
    ROLE_ANY,
    approximate_terms,
    term_clause,
    combine,
    date_clause,
    drug_clause,
    escape_phrase,
    event_clause,
    normalise_pt,
    normalise_substance,
    validate_raw_query,
)


class TestPlusJoinRegression:
    """The defect that made every tool return 'No results found for this query.'

    Clauses were joined with '+AND+'. Passed through httpx's params dict the '+'
    is percent-encoded to %2B, so openFDA received a literal plus rather than the
    AND operator and answered 404. Verified against the live API 2026-09-05:

        build_drug_query() as written   -> 404 'No matches found!'
        same query, space-joined        -> 200, 67,027 records
    """

    def test_clauses_are_space_joined(self):
        q = combine('a:"1"', 'b:"2"')
        assert q == 'a:"1" AND b:"2"'
        assert "+AND+" not in q

    def test_multi_event_clause_uses_spaces(self):
        q = event_clause(["PANCREATITIS", "NAUSEA"])
        assert q == (
            '(patient.reaction.reactionmeddrapt.exact:"PANCREATITIS"'
            ' OR patient.reaction.reactionmeddrapt.exact:"NAUSEA")'
        )
        assert "+OR+" not in q

    def test_plus_survives_url_encoding_as_literal_plus(self):
        """Pin the encoding behaviour that caused the bug, so it cannot return."""
        bad = httpx.Request(
            "GET", "https://api.fda.gov/drug/event.json", params={"search": 'a:"1"+AND+b:"2"'}
        )
        assert "%2B" in str(bad.url)

        good = httpx.Request(
            "GET", "https://api.fda.gov/drug/event.json", params={"search": combine('a:"1"', 'b:"2"')}
        )
        assert "%2B" not in str(good.url)

    def test_raw_query_rejects_plus_joining(self):
        with pytest.raises(InvalidQuery) as exc:
            validate_raw_query('a:"1"+AND+b:"2"')
        assert "literal plus" in exc.value.recovery


class TestRoleBasis:
    """drugcharacterization must never appear: it filters the report, not the drug.

    Verified against the live API:
        ASPIRIN any role                    547,048
        ASPIRIN AND drugcharacterization:1  545,046  (99.6% retained)
        ASPIRIN AND drugcharacterization:2  486,912  (89.0% retained)
    The filtered counts sum to ~189% of the total, so the clauses match
    independently and 'suspect only' was never enforced.
    """

    def test_no_drugcharacterization_clause_is_emitted(self):
        assert "drugcharacterization" not in drug_clause("METFORMIN", ROLE_ANY)

    def test_unknown_role_basis_rejected(self):
        with pytest.raises(InvalidQuery):
            drug_clause("METFORMIN", "suspect_only")


class TestCaseNormalisation:
    """Substance .exact is case-sensitive; PT .exact is not. Both verified live."""

    def test_substance_is_uppercased(self):
        assert normalise_substance(" empagliflozin ") == "EMPAGLIFLOZIN"
        assert 'exact:"EMPAGLIFLOZIN"' in drug_clause("empagliflozin")

    def test_pt_casing_is_preserved(self):
        assert normalise_pt("  Acute kidney injury ") == "Acute kidney injury"
        assert 'exact:"Acute kidney injury"' in event_clause(["Acute kidney injury"])


class TestEscaping:
    def test_quotes_and_backslashes_escaped(self):
        assert escape_phrase('AB"C') == 'AB\\"C'
        assert escape_phrase("AB\\C") == "AB\\\\C"

    def test_injection_attempt_is_neutralised(self):
        clause = drug_clause('X" OR serious:1 OR "')
        assert clause.count('\\"') == 2

    def test_names_with_lucene_specials_are_quoted_not_broken(self):
        clause = drug_clause("amoxicillin (as trihydrate)")
        assert clause == (
            'patient.drug.activesubstance.activesubstancename.exact:'
            '"AMOXICILLIN (AS TRIHYDRATE)"'
        )


class TestDateClause:
    def test_both_ends_required(self):
        with pytest.raises(InvalidQuery):
            date_clause("20230101", None)

    def test_neither_is_fine(self):
        assert date_clause(None, None) is None

    def test_range_built(self):
        assert date_clause("20230101", "20231231") == "receivedate:[20230101 TO 20231231]"

    def test_reversed_range_rejected(self):
        with pytest.raises(InvalidQuery):
            date_clause("20231231", "20230101")

    def test_bad_format_rejected(self):
        with pytest.raises(InvalidQuery):
            date_clause("2023-01-01", "2023-12-31")


class TestRawQueryValidation:
    def test_unbalanced_quotes(self):
        with pytest.raises(InvalidQuery):
            validate_raw_query('patient.patientsex:"2')

    def test_unbalanced_parens(self):
        with pytest.raises(InvalidQuery):
            validate_raw_query("(serious:1 AND patient.patientsex:2")

    def test_stray_closing_paren(self):
        with pytest.raises(InvalidQuery):
            validate_raw_query("serious:1)")

    def test_paren_inside_quotes_is_not_counted(self):
        assert validate_raw_query('name:"AMOXICILLIN (TRIHYDRATE)"')

    def test_empty_rejected(self):
        with pytest.raises(InvalidQuery):
            validate_raw_query("   ")


class TestEventClause:
    def test_empty_events_rejected(self):
        with pytest.raises(InvalidQuery):
            event_clause([])

    def test_blank_events_rejected(self):
        with pytest.raises(InvalidQuery):
            event_clause(["", "  "])

    def test_single_event_not_parenthesised(self):
        assert event_clause(["NAUSEA"]).startswith("patient.reaction")


class TestUnqueryableCharacters:
    r"""FAERS stores apostrophes as a caret, and openFDA cannot match one exactly.

    Verified against the live API:
        .exact:"CROHN^S DISEASE"      -> BAD_REQUEST
        .exact:"CROHN\^S DISEASE"     -> BAD_REQUEST
        .exact:"CROHN?S DISEASE"      -> BAD_REQUEST
        .exact:"CROHNS DISEASE"       -> NOT_FOUND
        (non-exact):"CROHN S DISEASE" -> 58,021
    """

    def test_ordinary_term_uses_exact(self):
        clause, approximate = term_clause(F_PT, "PANCREATITIS")
        assert clause == 'patient.reaction.reactionmeddrapt.exact:"PANCREATITIS"'
        assert approximate is False

    def test_caret_term_drops_exact_and_is_flagged(self):
        clause, approximate = term_clause(F_PT, "CROHN^S DISEASE")
        assert clause == 'patient.reaction.reactionmeddrapt:"CROHN S DISEASE"'
        assert approximate is True

    def test_apostrophe_is_treated_the_same_way(self):
        """A user typing CROHN'S DISEASE must reach FAERS's CROHN^S DISEASE."""
        clause, approximate = term_clause(F_PT, "CROHN'S DISEASE")
        assert clause == 'patient.reaction.reactionmeddrapt:"CROHN S DISEASE"'
        assert approximate is True

    def test_substance_field_handled_too(self):
        clause, approximate = term_clause(F_SUBSTANCE, "SOME^DRUG")
        assert ".exact" not in clause
        assert approximate is True

    def test_event_clause_routes_caret_terms(self):
        clause = event_clause(["FOURNIER^S GANGRENE"])
        assert ".exact" not in clause
        assert "FOURNIER S GANGRENE" in clause

    def test_approximate_terms_are_reportable(self):
        assert approximate_terms(["PANCREATITIS", "CROHN^S DISEASE"]) == ["CROHN^S DISEASE"]
        assert approximate_terms(["PANCREATITIS"]) == []

    def test_mixed_list_keeps_exact_where_possible(self):
        clause = event_clause(["PANCREATITIS", "CROHN^S DISEASE"])
        assert 'reactionmeddrapt.exact:"PANCREATITIS"' in clause
        assert 'reactionmeddrapt:"CROHN S DISEASE"' in clause
