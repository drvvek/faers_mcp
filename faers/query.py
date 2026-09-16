"""Query construction for the openFDA drug/event endpoint.

Three rules this module exists to enforce:

1. Clauses are joined with a literal space-delimited " AND " / " OR ", never
   "+AND+". The "+" convention only works in a hand-written URL where "+" means
   space. We pass the query through httpx's params dict, which percent-encodes
   "+" to %2B, so openFDA would receive a literal plus and 404 with
   "No matches found!". Verified against the live API on 2026-09-05.

2. Substance names are uppercased; MedDRA PTs are left alone.
   activesubstancename.exact is case-SENSITIVE ("Empagliflozin" -> 0 hits).
   reactionmeddrapt.exact is case-INSENSITIVE. Both verified against the live
   API; tests/test_query.py pins the behaviour because it is undocumented.

3. Caller-supplied values are escaped before interpolation.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .errors import InvalidQuery

# --- field paths -------------------------------------------------------------

F_SUBSTANCE = "patient.drug.activesubstance.activesubstancename.exact"
F_PT = "patient.reaction.reactionmeddrapt.exact"
F_RECEIVEDATE = "receivedate"
F_REPORT_ID = "safetyreportid"

# --- role basis --------------------------------------------------------------
#
# "suspect_only" as previously implemented did not work. The clause
# `drugcharacterization:1` is evaluated against the whole report, not against
# the matched drug, because openFDA flattens patient.drug[]. Verified:
#   ASPIRIN any role                     547,048
#   ASPIRIN AND drugcharacterization:1   545,046  (99.6% retained)
#   ASPIRIN AND drugcharacterization:2   486,912  (89.0% retained)
# The filtered counts sum to ~189% of the total, i.e. the clauses match
# independently.

ROLE_ANY = "any"
ROLE_SUSPECT_VERIFIED = "suspect_verified"

ROLE_LABELS = {
    ROLE_ANY: (
        "any role (suspect, concomitant or interacting) - openFDA cannot scope a "
        "count to one drug's role within a report"
    ),
    ROLE_SUSPECT_VERIFIED: (
        "suspect only, verified per record by checking drugcharacterization on the "
        "matched drug object"
    ),
}

DISCLAIMER = (
    "openFDA counts are not de-duplicated and do not match FAERS Public Dashboard "
    "case counts. These are reporting-rate comparisons, not incidence, and cannot "
    "support causal inference."
)

# --- escaping ----------------------------------------------------------------

_PHRASE_ESCAPE = {'\\': '\\\\', '"': '\\"'}
_TERM_SPECIALS = '+-&|!(){}[]^"~*?:\\/'


def escape_phrase(value: str) -> str:
    """Escape a value destined for the inside of a quoted phrase."""
    return "".join(_PHRASE_ESCAPE.get(ch, ch) for ch in value)


def escape_term(value: str) -> str:
    """Escape a value destined for an unquoted Lucene term."""
    out = []
    for ch in value:
        if ch in _TERM_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def phrase(field: str, value: str) -> str:
    return f'{field}:"{escape_phrase(value)}"'


# --- unqueryable characters --------------------------------------------------
#
# FAERS stores apostrophes as a caret: CROHN^S DISEASE, PARKINSON^S DISEASE,
# FOURNIER^S GANGRENE. The count API returns those terms happily, but feeding
# one back into a search is rejected with BAD_REQUEST - quoted, backslash-
# escaped, or wildcarded alike. Verified against the live API:
#
#   .exact:"CROHN^S DISEASE"      -> BAD_REQUEST
#   .exact:"CROHN\^S DISEASE"     -> BAD_REQUEST
#   .exact:"CROHN?S DISEASE"      -> BAD_REQUEST
#   .exact:"CROHNS DISEASE"       -> NOT_FOUND
#   (non-exact):"CROHN S DISEASE" -> 58,021   <- the only thing that works
#
# The fallback drops .exact, so the phrase is matched on tokens and is slightly
# over-inclusive: it also matches longer PTs containing the same token run.
# Measured overshoot: CROHN^S DISEASE +0.08%, PARKINSON^S DISEASE +1.36%.
# Callers are told, via the approximate flag, so the payload can say so.

UNQUERYABLE_CHARS = "^'"


def needs_relaxed_match(value: str) -> bool:
    return any(ch in value for ch in UNQUERYABLE_CHARS)


def term_clause(exact_field: str, value: str) -> tuple[str, bool]:
    """Build a clause for one term. Returns (clause, is_approximate)."""
    value = value.strip()
    if not needs_relaxed_match(value):
        return phrase(exact_field, value), False

    base = exact_field[: -len(".exact")] if exact_field.endswith(".exact") else exact_field
    relaxed = value
    for ch in UNQUERYABLE_CHARS:
        relaxed = relaxed.replace(ch, " ")
    return phrase(base, " ".join(relaxed.split())), True


# --- clause builders ---------------------------------------------------------


def normalise_substance(name: str) -> str:
    """Substance .exact is case-sensitive and stored uppercase."""
    return name.strip().upper()


def normalise_pt(term: str) -> str:
    """PT .exact is case-insensitive; preserve caller casing, just trim."""
    return term.strip()


def drug_clause(drug_name: str, role_basis: str = ROLE_ANY) -> str:
    """Build the drug clause.

    No drugcharacterization clause is ever emitted: it does not do what its name
    suggests (see module docstring). Suspect scoping happens
    per-record in projection.verify_suspect() on the tools that hold records.
    """
    if role_basis not in ROLE_LABELS:
        raise InvalidQuery(
            reason=f"Unknown role_basis {role_basis!r}.",
            recovery=f"Use one of: {', '.join(ROLE_LABELS)}.",
        )
    clause, _ = term_clause(F_SUBSTANCE, normalise_substance(drug_name))
    return clause


def approximate_terms(events: Iterable[str]) -> list[str]:
    """Terms that can only be matched approximately (see UNQUERYABLE_CHARS)."""
    return [e.strip() for e in events if e and e.strip() and needs_relaxed_match(e)]


def event_clause(events: Iterable[str]) -> str:
    terms = [term_clause(F_PT, normalise_pt(e))[0] for e in events if e and e.strip()]
    if not terms:
        raise InvalidQuery(
            reason="No usable MedDRA PT terms were supplied.",
            recovery="Pass at least one non-empty term, e.g. ['PANCREATITIS'].",
        )
    if len(terms) == 1:
        return terms[0]
    return "(" + " OR ".join(terms) + ")"


def date_clause(date_from: Optional[str], date_to: Optional[str]) -> Optional[str]:
    """Build a receivedate range clause from two YYYYMMDD strings.

    Either both or neither. A half-open range is rejected rather than silently
    widened, because a date filter must apply to every cell of a 2x2 or none of
    them.
    """
    if not date_from and not date_to:
        return None
    if bool(date_from) != bool(date_to):
        raise InvalidQuery(
            reason="Only one end of the date range was supplied.",
            recovery="Pass both date_from and date_to as YYYYMMDD, or neither.",
        )
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if not (value and len(value) == 8 and value.isdigit()):
            raise InvalidQuery(
                reason=f"{label}={value!r} is not an 8-digit YYYYMMDD date.",
                recovery="Use YYYYMMDD, e.g. 20230101.",
            )
    if date_from > date_to:  # type: ignore[operator]
        raise InvalidQuery(
            reason=f"date_from ({date_from}) is after date_to ({date_to}).",
            recovery="Swap the two values.",
        )
    return f"{F_RECEIVEDATE}:[{date_from} TO {date_to}]"


def combine(*clauses: Optional[str]) -> str:
    parts = [c for c in clauses if c]
    return " AND ".join(parts)


# --- raw query validation ----------------------------------------------------


def validate_raw_query(search: str) -> str:
    """Sanity-check a caller-supplied Lucene string before sending it upstream.

    Catches the two failures that produce a confusing 404 rather than a useful
    error: unbalanced quotes and unbalanced parentheses.
    """
    if not search or not search.strip():
        raise InvalidQuery(
            reason="Empty search string.",
            recovery='Pass a Lucene query, e.g. \'patient.patientsex:2 AND serious:1\'.',
        )

    if search.count('"') % 2:
        raise InvalidQuery(
            reason="Unbalanced double quotes in the search string.",
            recovery="Every phrase needs an opening and closing quote.",
            search=search,
        )

    depth = 0
    in_quotes = False
    for ch in search:
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    raise InvalidQuery(
                        reason="Closing parenthesis with no matching opener.",
                        recovery="Check the parentheses in the search string.",
                        search=search,
                    )
    if depth:
        raise InvalidQuery(
            reason=f"{depth} unclosed parenthesis/es in the search string.",
            recovery="Balance the parentheses.",
            search=search,
        )

    if "+AND+" in search or "+OR+" in search or "+NOT+" in search:
        raise InvalidQuery(
            reason="The search string uses '+AND+' style joining.",
            recovery=(
                "Join clauses with plain spaces: 'a AND b'. The '+' form only works "
                "in a hand-written URL and arrives at openFDA as a literal plus."
            ),
            search=search,
        )

    return search.strip()
