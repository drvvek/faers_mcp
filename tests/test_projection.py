"""Compact projection and per-record suspect verification."""

from __future__ import annotations

import json

from faers.projection import compact_case, project, suspect_substances, verify_suspect


def build_report(**overrides) -> dict:
    report = {
        "safetyreportid": "10886389",
        "receivedate": "20150101",
        "serious": "1",
        "seriousnessdeath": "1",
        "occurcountry": "US",
        "primarysource": {"qualification": "1"},
        "patient": {
            "patientsex": "2",
            "patientagegroup": "5",
            "drug": [
                {
                    "drugcharacterization": "1",
                    "medicinalproduct": "JARDIANCE",
                    "activesubstance": {"activesubstancename": "EMPAGLIFLOZIN"},
                },
                {
                    "drugcharacterization": "2",
                    "medicinalproduct": "ASPIRIN",
                    "activesubstance": {"activesubstancename": "ASPIRIN"},
                },
            ],
            "reaction": [{"reactionmeddrapt": "PANCREATITIS", "reactionoutcome": "5"}],
        },
    }
    report.update(overrides)
    return report


class TestSuspectVerification:
    """The check openFDA cannot do: name and role must be on the same drug element."""

    def test_suspect_drug_verified(self):
        assert verify_suspect(build_report(), "EMPAGLIFLOZIN") is True

    def test_concomitant_drug_is_not_suspect(self):
        assert verify_suspect(build_report(), "ASPIRIN") is False

    def test_case_insensitive_on_the_drug_name(self):
        assert verify_suspect(build_report(), "empagliflozin") is True

    def test_absent_drug_is_not_suspect(self):
        assert verify_suspect(build_report(), "METFORMIN") is False

    def test_only_suspect_substances_listed(self):
        assert suspect_substances(build_report()) == ["EMPAGLIFLOZIN"]

    def test_report_with_no_suspect_drug(self):
        report = build_report()
        for drug in report["patient"]["drug"]:
            drug["drugcharacterization"] = "2"
        assert verify_suspect(report, "EMPAGLIFLOZIN") is False
        assert suspect_substances(report) == []


class TestCompactCard:
    def test_decodes_coded_fields(self):
        card = compact_case(build_report(), "EMPAGLIFLOZIN")
        assert card["sex"] == "female"
        assert card["age_group"] == "adult"
        assert card["reporter"] == "physician"
        assert card["reactions"][0] == {"pt": "PANCREATITIS", "outcome": "fatal"}
        assert card["seriousness_criteria"] == ["death"]
        assert card["suspect_verified"] is True

    def test_card_is_dramatically_smaller_than_the_record(self, icsr):
        """limit=10 on raw records measured 724 KB (~181,000 tokens)."""
        full = len(json.dumps(icsr))
        compact = len(json.dumps(compact_case(icsr, "EMPAGLIFLOZIN")))
        assert compact < full
        assert compact < 2_000, "a triage card should stay well under 2 KB"

    def test_full_passthrough_is_untouched(self, icsr):
        assert project([icsr], "EMPAGLIFLOZIN", full=True) == [icsr]

    def test_projection_never_raises_on_sparse_records(self):
        card = compact_case({"safetyreportid": "1"}, "X")
        assert card["safetyreportid"] == "1"
        assert card["serious"] is False
        assert card["suspect_drugs"] == []
        assert card["reactions"] == []

    def test_real_record_projects(self, icsr):
        card = compact_case(icsr, "EMPAGLIFLOZIN")
        assert card["safetyreportid"]
        assert isinstance(card["reactions"], list)
        assert "suspect_verified" in card
