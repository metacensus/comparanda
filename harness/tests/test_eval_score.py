import json

from harness.eval.score import _full_mode_metric_inputs, load_finding_scores


def test_finding_scores_emit_semantic_recall_summary(tmp_path):
    scores = tmp_path / "finding_scores.json"
    scores.write_text(
        json.dumps(
            {
                "papers": {
                    "paper_a": {
                        "findings": [
                            {
                                "gold_index": 0,
                                "completeness": 1.0,
                                "accuracy": 1.0,
                                "extraction_index": 3,
                                "reasoning": "captured exactly",
                            },
                            {
                                "gold_index": 1,
                                "completeness": 0.5,
                                "accuracy": 1.0,
                                "extraction_index": 4,
                                "reasoning": "partially captured",
                            },
                            {
                                "gold_index": 2,
                                "completeness": 0.0,
                                "accuracy": 0.0,
                                "extraction_index": None,
                                "reasoning": "missing",
                            },
                        ]
                    }
                }
            }
        )
    )

    finding_accuracy, summary = load_finding_scores(scores, {"paper_a": 3})

    assert finding_accuracy == 0.5
    assert summary["finding_count"] == 3
    assert summary["semantic_recall"]["captured_gold_rows"] == 1
    assert summary["semantic_recall"]["fully_represented_gold_rows"] == 1
    assert summary["semantic_recall"]["partially_captured_gold_rows"] == 1
    assert summary["semantic_recall"]["partially_represented_gold_rows"] == 1
    assert summary["semantic_recall"]["missing_gold_rows"] == 1
    assert summary["semantic_recall"]["represented_gold_rows"] == 2
    assert summary["semantic_recall"]["any_representation_gold_rows"] == 2
    assert summary["semantic_recall"]["mean_completeness"] == 0.5
    assert summary["semantic_recall"]["mean_gold_row_completeness"] == 0.5
    assert summary["semantic_recall"]["all_in_recall"] == 0.666667
    assert summary["semantic_recall"]["any_representation_rate"] == 0.666667
    assert summary["semantic_recall"]["field_value_recoverability_rate"] == 0.666667
    assert summary["semantic_recall"]["full_capture_rate"] == 0.333333
    assert summary["semantic_recall"]["full_representation_rate"] == 0.333333
    assert summary["semantic_recall"]["field_value_representation_rate"] == 0.333333


def test_full_mode_metric_inputs_use_semantic_surfaces_when_available():
    inputs, sources = _full_mode_metric_inputs(
        raw_row_coverage=0.854,
        raw_entity_coverage=0.0,
        raw_field_fill_rate=0.5386,
        finding_scores_summary={
            "semantic_recall": {
                "mean_gold_row_completeness": 1.0,
                "field_value_recoverability_rate": 1.0,
            }
        },
        semantic_field_overlap={
            "aggregate": {
                "source_faithfulness": 1.0,
            }
        },
    )

    assert inputs == {
        "row_coverage": 1.0,
        "entity_coverage": 1.0,
        "field_fill_rate": 1.0,
    }
    assert sources["mode"] == "semantic_full"
    assert sources["row_coverage"] == "finding_scores.mean_gold_row_completeness"
    assert sources["entity_coverage"] == "finding_scores.field_value_recoverability_rate"
    assert sources["field_fill_rate"] == "semantic_field_overlap.source_faithfulness"


def test_full_mode_metric_inputs_fall_back_to_raw_without_semantic_scores():
    inputs, sources = _full_mode_metric_inputs(
        raw_row_coverage=0.5,
        raw_entity_coverage=0.25,
        raw_field_fill_rate=0.75,
        finding_scores_summary=None,
        semantic_field_overlap=None,
    )

    assert inputs == {
        "row_coverage": 0.5,
        "entity_coverage": 0.25,
        "field_fill_rate": 0.75,
    }
    assert sources["mode"] == "raw_structural"
