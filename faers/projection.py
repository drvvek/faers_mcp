"""Compact case projection.

A FAERS ICSR is enormous. Measured over ten real empagliflozin records:

    smallest        8,140 bytes
    largest       367,213 bytes
    mean           72,378 bytes
    limit=10      723,781 bytes  (~181,000 tokens)

The default limit of 10 could therefore return more than a full context window.
The compact card below averages 348 bytes - about 208x smaller - and carries the
fields an analyst actually triages on. Full records are available from
faers_get_report, one at a time, where the size is a deliberate choice.
"""

from __future__ import annotations

from typing import Any, Optional

SUSPECT = "1"
CONCOMITANT = "2"
INTERACTING = "3"

CHARACTERIZATION = {SUSPECT: "suspect", CONCOMITANT: "concomitant", INTERACTING: "interacting"}

OUTCOME = {
    "1": "recovered",
    "2": "recovering",
    "3": "not recovered",
    "4": "recovered with sequelae",
    "5": "fatal",
    "6": "unknown",
}

SEX = {"0": "unknown", "1": "male", "2": "female"}

AGE_GROUP = {
    "1": "neonate",
    "2": "infant",
    "3": "child",
    "4": "adolescent",
    "5": "adult",
    "6": "elderly",
}

QUALIFICATION = {
    "1": "physician",
    "2": "pharmacist",
    "3": "other health professional",
    "4": "lawyer",
    "5": "consumer or non-health professional",
}

SERIOUSNESS_CRITERIA = {
    "seriousnessdeath": "death",
    "seriousnesshospitalization": "hospitalization",
    "seriousnesslifethreatening": "life-threatening",
    "seriousnessdisabling": "disability",
    "seriousnesscongenitalanomali": "congenital anomaly",
    "seriousnessother": "medically significant",
}

# Named so callers know what they gave up, and where to get it back.
FIELDS_OMITTED = (
    "Full ICSR fields (drug dosage, route, indication, dates, narrative codes, "
    "openfda mappings, duplicate/sender metadata) are omitted from compact cards. "
    "Call faers_get_report with the safetyreportid for the complete record."
)


def _drugs(report: dict) -> list[dict]:
    drugs = report.get("patient", {}).get("drug") or []
    return [d for d in drugs if isinstance(d, dict)]


def _substance(drug: dict) -> Optional[str]:
    name = (drug.get("activesubstance") or {}).get("activesubstancename")
    return name.strip() if isinstance(name, str) else None


def verify_suspect(report: dict, drug_name: str) -> bool:
    """True when `drug_name` is carried as a SUSPECT drug on this report.

    This is the check openFDA cannot do server-side: it requires the substance
    name and drugcharacterization to belong to the *same* element of
    patient.drug[], which a flattened Lucene query cannot express.

    Mirrors filterForSuspectDrug() in the companion AEMS Chrome extension
    (dashboard.js:265), the browser tool this server's logic was derived from.
    """
    target = drug_name.strip().upper()
    for drug in _drugs(report):
        substance = _substance(drug)
        if substance and substance.upper() == target and drug.get("drugcharacterization") == SUSPECT:
            return True
    return False


def suspect_substances(report: dict) -> list[str]:
    out = []
    for drug in _drugs(report):
        if drug.get("drugcharacterization") == SUSPECT:
            name = _substance(drug) or drug.get("medicinalproduct")
            if name:
                out.append(name)
    return sorted(set(out))


def _seriousness(report: dict) -> list[str]:
    return [label for field, label in SERIOUSNESS_CRITERIA.items() if report.get(field) == "1"]


def _reactions(report: dict) -> list[dict]:
    out = []
    for reaction in report.get("patient", {}).get("reaction") or []:
        if not isinstance(reaction, dict):
            continue
        pt = reaction.get("reactionmeddrapt")
        if not pt:
            continue
        entry: dict[str, Any] = {"pt": pt}
        outcome = reaction.get("reactionoutcome")
        if outcome:
            entry["outcome"] = OUTCOME.get(str(outcome), str(outcome))
        out.append(entry)
    return out


def compact_case(report: dict, drug_name: Optional[str] = None) -> dict:
    """Project one ICSR down to a triage card."""
    patient = report.get("patient") or {}
    source = report.get("primarysource") or {}

    card: dict[str, Any] = {
        "safetyreportid": report.get("safetyreportid"),
        "receivedate": report.get("receivedate"),
        "serious": report.get("serious") == "1",
    }

    criteria = _seriousness(report)
    if criteria:
        card["seriousness_criteria"] = criteria

    sex = patient.get("patientsex")
    if sex is not None:
        card["sex"] = SEX.get(str(sex), str(sex))

    age_group = patient.get("patientagegroup")
    if age_group is not None:
        card["age_group"] = AGE_GROUP.get(str(age_group), str(age_group))
    if patient.get("patientonsetage"):
        card["age"] = patient.get("patientonsetage")

    qualification = source.get("qualification")
    if qualification is not None:
        card["reporter"] = QUALIFICATION.get(str(qualification), str(qualification))

    if report.get("occurcountry"):
        card["country"] = report.get("occurcountry")

    card["suspect_drugs"] = suspect_substances(report)
    card["reactions"] = _reactions(report)

    if drug_name:
        card["suspect_verified"] = verify_suspect(report, drug_name)

    return card


def project(reports: list[dict], drug_name: Optional[str] = None, full: bool = False) -> list[dict]:
    if full:
        return reports
    return [compact_case(r, drug_name) for r in reports]
