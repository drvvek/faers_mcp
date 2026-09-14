"""Static catalogue of FAERS field paths.

Exists so the model stops inventing field names. Every path here has been
exercised against the live endpoint; the notes record the traps that produce a
plausible-looking but wrong answer.

Two rules govern the whole schema:

* `.exact` gives you the untokenised whole value. Without it, a phrase is
  tokenised and "ASPIRIN" also matches "ASPIRIN 81MG EC".
* A `count` returns every value present in the *matching reports*. The search
  clause selects reports, never individual array elements, so counting a field
  under `patient.drug[]` mixes in values contributed by other drugs on the
  same report.
"""

from __future__ import annotations

CASE_SENSITIVITY = {
    "patient.drug.activesubstance.activesubstancename.exact": "case-SENSITIVE - must be uppercase",
    "patient.reaction.reactionmeddrapt.exact": "case-INSENSITIVE - any casing matches",
}

FIELDS: dict[str, list[dict]] = {
    "drug": [
        {
            "path": "patient.drug.activesubstance.activesubstancename",
            "exact": True,
            "description": "Active substance (INN). The preferred drug identifier.",
            "note": "The .exact form is case-sensitive and stored uppercase.",
        },
        {
            "path": "patient.drug.medicinalproduct",
            "exact": True,
            "description": "Trade or product name as reported.",
            "note": "Free text and highly variable; prefer activesubstancename.",
        },
        {
            "path": "patient.drug.drugcharacterization",
            "exact": False,
            "description": "Drug role: 1 suspect, 2 concomitant, 3 interacting.",
            "note": (
                "Filters the REPORT, not the matched drug. 'drug X AND "
                "drugcharacterization:1' means 'X appears somewhere and some drug is "
                "suspect', which is nearly every report. Not usable as a suspect filter."
            ),
        },
        {"path": "patient.drug.drugindication", "exact": True, "description": "Reported indication (MedDRA)."},
        {"path": "patient.drug.drugdosagetext", "exact": False, "description": "Free-text dose."},
        {"path": "patient.drug.actiondrug", "exact": False, "description": "Action taken: 1 withdrawn, 2 dose reduced, 3 dose increased, 4 unchanged, 5 unknown, 6 not applicable."},
        {"path": "patient.drug.drugadministrationroute", "exact": False, "description": "Route of administration code."},
        {"path": "patient.drug.openfda.generic_name", "exact": True, "description": "openFDA-normalised generic name (present only where the product mapped to an FDA label)."},
        {"path": "patient.drug.openfda.brand_name", "exact": True, "description": "openFDA-normalised brand name."},
        {"path": "patient.drug.openfda.pharm_class_epc", "exact": True, "description": "Established pharmacologic class."},
    ],
    "reaction": [
        {
            "path": "patient.reaction.reactionmeddrapt",
            "exact": True,
            "description": "MedDRA Preferred Term for the adverse event.",
            "note": "The .exact form is case-insensitive; no need to try multiple casings.",
        },
        {
            "path": "patient.reaction.reactionoutcome",
            "exact": False,
            "description": "Outcome: 1 recovered, 2 recovering, 3 not recovered, 4 recovered with sequelae, 5 fatal, 6 unknown.",
        },
        {"path": "patient.reaction.reactionmeddraversionpt", "exact": False, "description": "MedDRA version used for coding."},
    ],
    "seriousness": [
        {"path": "serious", "exact": False, "description": "1 serious, 2 non-serious."},
        {"path": "seriousnessdeath", "exact": False, "description": "1 when the case is fatal."},
        {"path": "seriousnesshospitalization", "exact": False, "description": "1 when hospitalisation was involved."},
        {"path": "seriousnesslifethreatening", "exact": False, "description": "1 when life-threatening."},
        {"path": "seriousnessdisabling", "exact": False, "description": "1 when disabling."},
        {"path": "seriousnesscongenitalanomali", "exact": False, "description": "1 for congenital anomaly. Note the truncated spelling."},
        {"path": "seriousnessother", "exact": False, "description": "1 for other medically significant condition."},
    ],
    "patient": [
        {"path": "patient.patientsex", "exact": False, "description": "0 unknown, 1 male, 2 female."},
        {"path": "patient.patientagegroup", "exact": False, "description": "1 neonate, 2 infant, 3 child, 4 adolescent, 5 adult, 6 elderly.", "note": "Sparsely populated; patientonsetage is more often present."},
        {"path": "patient.patientonsetage", "exact": False, "description": "Age at onset, in patientonsetageunit units."},
        {"path": "patient.patientonsetageunit", "exact": False, "description": "801 year, 802 month, 803 week, 804 day, 805 hour."},
        {"path": "patient.patientweight", "exact": False, "description": "Weight in kg."},
    ],
    "report": [
        {"path": "safetyreportid", "exact": False, "description": "Case identifier. Usually a plain 8-digit number.", "note": "The version is a separate field; ids here are not hyphenated."},
        {"path": "safetyreportversion", "exact": False, "description": "Version number of the case."},
        {"path": "receivedate", "exact": False, "description": "Date FDA received the most recent version, YYYYMMDD.", "note": "Counting this returns a COMPLETE date histogram regardless of limit, keyed 'time' rather than 'term'."},
        {"path": "receiptdate", "exact": False, "description": "Date of the most recent information, YYYYMMDD."},
        {"path": "transmissiondate", "exact": False, "description": "Date the record was transmitted, YYYYMMDD."},
        {"path": "reporttype", "exact": False, "description": "1 spontaneous, 2 report from study, 3 other, 4 not available."},
        {"path": "occurcountry", "exact": True, "description": "Country of occurrence, two-letter code."},
        {"path": "primarysourcecountry", "exact": True, "description": "Country of the primary reporter."},
        {"path": "companynumb", "exact": True, "description": "Manufacturer case number."},
        {"path": "primarysource.qualification", "exact": False, "description": "1 physician, 2 pharmacist, 3 other HCP, 4 lawyer, 5 consumer/non-HCP."},
        {"path": "primarysource.reportercountry", "exact": True, "description": "Reporter country."},
        {"path": "sender.senderorganization", "exact": True, "description": "Sending organisation."},
    ],
}

RANGE_SYNTAX = 'receivedate:[20230101 TO 20231231]'

SEARCH_TIPS = [
    'Join clauses with " AND " / " OR ". A "+" arrives at openFDA as a literal plus and matches nothing.',
    'Quote phrases: patient.reaction.reactionmeddrapt.exact:"ACUTE KIDNEY INJURY".',
    f"Date ranges use square brackets: {RANGE_SYNTAX}.",
    "Negate with NOT: serious:1 AND NOT seriousnessdeath:1.",
    "skip is capped at 25,000 by openFDA; narrow with a date window instead of paging.",
    "count returns at most 1000 values (999 without an API key) and cannot be paged with skip.",
]


def describe(category: str | None = None) -> dict:
    """Return the catalogue, optionally narrowed to one category."""
    if category:
        key = category.lower().strip()
        if key not in FIELDS:
            return {
                "error": f"Unknown category {category!r}.",
                "available_categories": sorted(FIELDS),
            }
        selected = {key: FIELDS[key]}
    else:
        selected = FIELDS

    return {
        "categories": {
            name: [
                {
                    "search_path": f["path"],
                    "count_path": f"{f['path']}.exact" if f["exact"] else f["path"],
                    "description": f["description"],
                    **({"note": f["note"]} if "note" in f else {}),
                }
                for f in fields
            ]
            for name, fields in selected.items()
        },
        "case_sensitivity": CASE_SENSITIVITY,
        "search_tips": SEARCH_TIPS,
    }
