from harness.eval.field_checklist import build_checklist_for_comparison, is_nullish


def test_nullish_values_are_skipped() -> None:
    assert is_nullish(None)
    assert is_nullish("")
    assert is_nullish("-")
    assert is_nullish("not stated")
    assert not is_nullish("0")


def test_build_checklist_skips_nullish_and_carries_candidates() -> None:
    comparison = {
        "paper_id": "paper_a",
        "paired_rows": [
            {
                "gold_index": 4,
                "gold_fields": {
                    "Group Type": "case",
                    "Patient Count": 17,
                    "P Value": "-",
                    "Notes": "",
                },
                "extraction_candidates": [
                    {"extraction_index": 7, "group_label": "case", "n": 17},
                    {"extraction_index": 8, "group_label": "control", "n": 21},
                ],
                "resolution_rung": "semantic",
                "gold_entity": "case",
                "gold_resolved_token": "case",
                "has_match": True,
            }
        ],
    }

    rows = build_checklist_for_comparison(comparison)

    assert [row["gold_field"] for row in rows] == ["Group Type", "Patient Count"]
    assert rows[0]["checklist_id"] == "paper_a:4:0:group_type"
    assert rows[0]["role_hint"] == "identity_context"
    assert rows[1]["role_hint"] == "value"
    assert rows[0]["candidate_extraction_indices"] == [7, 8]
    assert rows[0]["candidate_count"] == 2
    assert rows[0]["resolution_rung"] == "semantic"


def test_checklist_ignores_pairs_without_integer_gold_index() -> None:
    comparison = {
        "paper_id": "paper_a",
        "paired_rows": [
            {"gold_index": "not-int", "gold_fields": {"Field": "value"}},
            {"gold_fields": {"Field": "value"}},
        ],
    }

    assert build_checklist_for_comparison(comparison) == []
