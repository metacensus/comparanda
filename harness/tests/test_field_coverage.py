from harness.eval.field_checklist import build_checklist_for_comparison
from harness.eval.field_coverage import validate_field_coverage


def _comparison() -> dict:
    return {
        "paper_id": "paper_a",
        "paired_rows": [
            {
                "gold_index": 0,
                "gold_fields": {
                    "Group Type": "case",
                    "Event Count": 4,
                    "Non Event Count": 6,
                    "Comment": "reported in table",
                },
                "extraction_candidates": [
                    {
                        "extraction_index": 2,
                        "group_label": "case",
                        "n_total": 10,
                        "n_events": 4,
                        "evidence_note": "reported in table",
                    }
                ],
                "resolution_rung": "semantic",
                "has_match": True,
            }
        ],
    }


def _base_adjudication(checklist_item: dict) -> dict:
    return {
        "checklist_id": checklist_item["checklist_id"],
        "paper_id": checklist_item["paper_id"],
        "gold_index": checklist_item["gold_index"],
        "gold_field": checklist_item["gold_field"],
        "gold_value": checklist_item["gold_value"],
        "extraction_refs": [],
        "derivation": None,
        "confidence": 0.9,
        "provenance_checked": False,
        "rationale": "Synthetic test adjudication.",
    }


def test_validate_field_coverage_accepts_direct_and_derived_representations() -> None:
    comparison = _comparison()
    checklist = build_checklist_for_comparison(comparison)
    by_field = {row["gold_field"]: row for row in checklist}

    group = {
        **_base_adjudication(by_field["Group Type"]),
        "role": "identity_context",
        "status": "used_for_identity_matching",
        "representation_kind": "identity_only",
        "extraction_refs": [{"row": 2, "field": "group_label"}],
    }
    direct = {
        **_base_adjudication(by_field["Event Count"]),
        "role": "value",
        "status": "represented_exact",
        "representation_kind": "direct",
        "extraction_refs": [{"row": 2, "field": "n_events"}],
        "derivation": {"op": "direct", "row": 2, "field": "n_events"},
    }
    derived = {
        **_base_adjudication(by_field["Non Event Count"]),
        "role": "value",
        "status": "represented_by_derivation",
        "representation_kind": "derived",
        "extraction_refs": [{"row": 2, "field": "n_total"}, {"row": 2, "field": "n_events"}],
        "derivation": {
            "op": "complement",
            "row": 2,
            "total_field": "n_total",
            "event_field": "n_events",
        },
    }
    missing = {
        **_base_adjudication(by_field["Comment"]),
        "role": "metadata",
        "status": "not_represented",
        "representation_kind": "missing",
        "extraction_refs": [],
    }

    report = validate_field_coverage(
        checklist_rows=checklist,
        field_coverage={"field_adjudications": [group, direct, derived, missing]},
        comparison=comparison,
    )

    assert report["valid"], report["problems"]
    assert report["gold_field_value_count"] == 4
    assert report["represented_any_count"] == 3
    assert report["represented_full_count"] == 3
    assert report["field_value_recoverable_count"] == 3
    assert report["field_value_fully_represented_count"] == 3
    assert report["direct_count"] == 1
    assert report["derived_count"] == 1
    assert report["identity_only_count"] == 1
    assert report["missing_count"] == 1
    assert report["derivation_count"] == 2
    assert report["rates"]["any_representation_rate"] == 0.75
    assert report["rates"]["field_value_recoverability_rate"] == 0.75
    assert report["rates"]["field_value_representation_rate"] == 0.75
    assert report["rates"]["direct_representation_rate"] == 0.25
    assert report["rates"]["derived_representation_rate"] == 0.25
    assert report["rates"]["missing_rate"] == 0.25


def test_validate_field_coverage_separates_recoverable_pooled_from_full_representation() -> None:
    comparison = _comparison()
    checklist = build_checklist_for_comparison(comparison)

    adjudications = []
    for item in checklist:
        if item["gold_field"] == "Event Count":
            adjudications.append(
                {
                    **_base_adjudication(item),
                    "role": "value",
                    "status": "represented_by_pooled_value",
                    "representation_kind": "derived",
                    "extraction_refs": [
                        {"row": 2, "field": "n_total"},
                        {"row": 2, "field": "n_events"},
                    ],
                    "derivation": {},
                    "rationale": (
                        "The value is recoverable from a pooled/composite structure, "
                        "but not represented as the same field-value unit."
                    ),
                }
            )
            continue
        adjudications.append(
            {
                **_base_adjudication(item),
                "role": "metadata",
                "status": "not_applicable",
                "representation_kind": "not_applicable",
            }
        )

    report = validate_field_coverage(
        checklist_rows=checklist,
        field_coverage={"field_adjudications": adjudications},
        comparison=comparison,
    )

    assert report["valid"], report["problems"]
    assert report["applicable_gold_field_value_count"] == 1
    assert report["field_value_recoverable_count"] == 1
    assert report["field_value_fully_represented_count"] == 0
    assert report["rates"]["field_value_recoverability_rate"] == 1.0
    assert report["rates"]["field_value_representation_rate"] == 0.0


def test_validate_field_coverage_accepts_taxonomy_subsumption_for_inferred_values() -> None:
    comparison = _comparison()
    comparison["paired_rows"][0]["gold_fields"]["Outcome Category"] = "safety"
    comparison["paired_rows"][0]["extraction_candidates"][0]["outcome_category"] = "haematological"
    checklist = build_checklist_for_comparison(comparison)
    by_field = {row["gold_field"]: row for row in checklist}

    adjudications = []
    for item in checklist:
        if item["gold_field"] == "Outcome Category":
            adjudications.append(
                {
                    **_base_adjudication(item),
                    "role": "identity_context",
                    "status": "represented_by_derivation",
                    "representation_kind": "inferred",
                    "extraction_refs": [{"row": 2, "field": "outcome_category"}],
                    "derivation": {
                        "op": "taxonomy_subsumption",
                        "direction": "extraction_subtype_of_gold",
                        "source_value": "haematological",
                        "target_value": "safety",
                    },
                    "rationale": (
                        "The extraction uses a finer-grained category that is nested under "
                        "the broader gold category."
                    ),
                }
            )
            continue
        adjudications.append(
            {
                **_base_adjudication(item),
                "role": item["role_hint"],
                "status": "not_represented",
                "representation_kind": "missing",
            }
        )

    report = validate_field_coverage(
        checklist_rows=checklist,
        field_coverage={"field_adjudications": adjudications},
        comparison=comparison,
    )

    assert report["valid"], report["problems"]
    assert report["represented_any_count"] == 1
    assert report["represented_full_count"] == 1
    assert report["inferred_count"] == 1
    assert report["derivation_count"] == 1


def test_validate_field_coverage_rejects_incomplete_taxonomy_subsumption() -> None:
    comparison = _comparison()
    comparison["paired_rows"][0]["gold_fields"]["Outcome Category"] = "safety"
    checklist = build_checklist_for_comparison(comparison)
    by_field = {row["gold_field"]: row for row in checklist}
    adjudications = []
    for item in checklist:
        if item["gold_field"] == "Outcome Category":
            adjudications.append(
                {
                    **_base_adjudication(item),
                    "role": "identity_context",
                    "status": "represented_by_derivation",
                    "representation_kind": "inferred",
                    "extraction_refs": [{"row": 2, "field": "evidence_note"}],
                    "derivation": {
                        "op": "taxonomy_subsumption",
                        "source_value": "haematological",
                    },
                }
            )
            continue
        adjudications.append(
            {
                **_base_adjudication(item),
                "role": item["role_hint"],
                "status": "not_represented",
                "representation_kind": "missing",
            }
        )

    report = validate_field_coverage(
        checklist_rows=checklist,
        field_coverage={"field_adjudications": adjudications},
        comparison=comparison,
    )

    assert not report["valid"]
    assert any("taxonomy_subsumption requires direction" in problem for problem in report["problems"])
    assert any("taxonomy_subsumption requires non-empty target_value" in problem for problem in report["problems"])


def test_validate_field_coverage_treats_extraction_supertype_as_missing_near_miss() -> None:
    comparison = _comparison()
    comparison["paired_rows"][0]["gold_fields"]["Outcome Category"] = "specific_outcome"
    comparison["paired_rows"][0]["extraction_candidates"][0]["outcome_category"] = "broad_outcome_family"
    checklist = build_checklist_for_comparison(comparison)
    adjudications = []
    for item in checklist:
        adjudication = {
            **_base_adjudication(item),
            "role": item["role_hint"],
            "status": "not_represented",
            "representation_kind": "missing",
        }
        if item["gold_field"] == "Outcome Category":
            adjudication["extraction_refs"] = [{"row": 2, "field": "outcome_category"}]
            adjudication["derivation"] = {
                "op": "taxonomy_subsumption",
                "direction": "extraction_supertype_of_gold",
                "source_value": "broad_outcome_family",
                "target_value": "specific_outcome",
            }
            adjudication["rationale"] = (
                "The extraction reports only a broader ancestor category, not the "
                "specific gold outcome."
            )
        adjudications.append(adjudication)

    report = validate_field_coverage(
        checklist_rows=checklist,
        field_coverage={"field_adjudications": adjudications},
        comparison=comparison,
    )

    assert report["valid"], report["problems"]
    assert report["represented_any_count"] == 0
    assert report["represented_full_count"] == 0
    assert report["missing_count"] == len(checklist)
    assert report["derivation_count"] == 1


def test_validate_field_coverage_rejects_represented_extraction_supertype() -> None:
    comparison = _comparison()
    comparison["paired_rows"][0]["gold_fields"]["Outcome Category"] = "specific_outcome"
    comparison["paired_rows"][0]["extraction_candidates"][0]["outcome_category"] = "broad_outcome_family"
    checklist = build_checklist_for_comparison(comparison)
    adjudications = []
    for item in checklist:
        if item["gold_field"] == "Outcome Category":
            adjudications.append(
                {
                    **_base_adjudication(item),
                    "role": "identity_context",
                    "status": "represented_by_derivation",
                    "representation_kind": "inferred",
                    "extraction_refs": [{"row": 2, "field": "outcome_category"}],
                    "derivation": {
                        "op": "taxonomy_subsumption",
                        "direction": "extraction_supertype_of_gold",
                        "source_value": "broad_outcome_family",
                        "target_value": "specific_outcome",
                    },
                }
            )
            continue
        adjudications.append(
            {
                **_base_adjudication(item),
                "role": item["role_hint"],
                "status": "not_represented",
                "representation_kind": "missing",
            }
        )

    report = validate_field_coverage(
        checklist_rows=checklist,
        field_coverage={"field_adjudications": adjudications},
        comparison=comparison,
    )

    assert not report["valid"]
    assert any("extraction_supertype_of_gold is a near-miss" in problem for problem in report["problems"])


def test_validate_field_coverage_rejects_missing_and_duplicate_adjudications() -> None:
    comparison = _comparison()
    checklist = build_checklist_for_comparison(comparison)
    item = checklist[0]
    adjudication = {
        **_base_adjudication(item),
        "role": "identity_context",
        "status": "used_for_identity_matching",
        "representation_kind": "identity_only",
    }

    report = validate_field_coverage(
        checklist_rows=checklist,
        field_coverage={"field_adjudications": [adjudication, adjudication]},
        comparison=comparison,
    )

    assert not report["valid"]
    assert any("duplicate adjudication" in problem for problem in report["problems"])
    assert any("missing adjudication" in problem for problem in report["problems"])


def test_validate_field_coverage_rejects_bad_extraction_refs_and_derivations() -> None:
    comparison = _comparison()
    checklist = build_checklist_for_comparison(comparison)
    adjudications = []
    for item in checklist:
        adjudications.append(
            {
                **_base_adjudication(item),
                "role": item["role_hint"],
                "status": "represented_exact",
                "representation_kind": "direct",
                "extraction_refs": [{"row": 999, "field": "missing"}],
                "derivation": {"op": "direct", "row": 2, "field": "missing"},
            }
        )

    report = validate_field_coverage(
        checklist_rows=checklist,
        field_coverage={"field_adjudications": adjudications},
        comparison=comparison,
    )

    assert not report["valid"]
    assert any("extraction ref row 999 does not exist" in problem for problem in report["problems"])
    assert any("referenced field 'missing' is absent" in problem for problem in report["problems"])
