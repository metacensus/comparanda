from harness.eval.semantic_field_overlap import (
    build_overlap_report,
    build_semantic_field_overlap,
)


def test_semantic_field_overlap_surfaces(tmp_path):
    comparisons = tmp_path / "comparisons"
    comparisons.mkdir()
    (comparisons / "paper_a.json").write_text(
        """
        {
          "paper_id": "paper_a",
          "gold_rows": 1,
          "extraction_rows": 1,
          "paired_rows": [
            {
              "gold_index": 0,
              "gold_fields": {
                "Entity": "Entity A",
                "Treatment": "Treatment A",
                "Patients with Entity": 10,
                "Patients with Entity and Outcome": 2
              },
              "extraction_candidates": [
                {
                  "extraction_index": 0,
                  "entity_raw": "Entity A",
                  "treatment_raw": "Treatment A",
                  "n_total": 10,
                  "n_events": 2,
                  "effect_estimate": 3.4
                }
              ],
              "has_match": true
            }
          ],
          "unmatched_extraction_rows": []
        }
        """
    )
    finding_scores = tmp_path / "finding_scores.json"
    finding_scores.write_text(
        """
        {"papers": {"paper_a": {"findings": [
          {"gold_index": 0, "completeness": 1.0, "accuracy": 1.0, "extraction_index": 0, "reasoning": "match"}
        ]}}}
        """
    )
    claim_faithfulness = tmp_path / "claim_faithfulness.json"
    claim_faithfulness.write_text(
        """
        {"papers": {"paper_a": {"claims": [
          {"field": "n_total", "value": 10, "status": "quote_found"},
          {"field": "n_events", "value": 2, "status": "quote_missing"}
        ]}}}
        """
    )

    report = build_overlap_report(
        comparisons_dir=comparisons,
        finding_scores_path=finding_scores,
        claim_faithfulness_path=claim_faithfulness,
    )

    paper = report["papers"]["paper_a"]
    assert paper["extraction_to_gold"]["present_in_gold"] == 4
    assert paper["extraction_to_gold"]["outside_gold_surface"] == 1
    assert paper["gold_to_extraction"]["represented_in_extraction"] == 4
    assert paper["source_faithfulness"]["source_faithfulness"] == 0.5
    assert report["aggregate"]["extraction_to_gold_overlap"] == 0.8
    assert report["aggregate"]["gold_to_extraction_coverage"] == 1.0


def test_direct_builder_uses_aliases_and_value_aliases_for_overlap():
    artifact = build_semantic_field_overlap(
        gold_rows=[
            {
                "Entity": "entity-a-id",
                "P Value": "0.010",
                "Patients with Entity and Outcome": 7,
                "Notes": "not scored",
            },
            {
                "Entity": "entity-a-id",
                "P Value": "0.01",
                "Patients with Entity and Outcome": 7,
            },
        ],
        extraction_rows=[
            {
                "entity_raw": "Entity A",
                "p_value_raw": "0.01",
                "n_events": "7",
                "provenance": {
                    "entity_raw": {"sources": [{"quote": "Entity A", "location": "table.md"}]},
                    "p_value_raw": {"sources": [{"quote": "p = 0.01", "location": "table.md"}]},
                    "n_events": {"sources": [{"quote": "7 cases", "location": "table.md"}]},
                },
            }
        ],
        field_aliases={
            "Entity": ["entity_raw"],
            "P Value": ["p_value_raw"],
            "Patients with Entity and Outcome": ["n_events"],
        },
        value_aliases={"entity-a-id": ["Entity A"]},
        field_classes={"Notes": "non_scored"},
    )

    paper = artifact["papers"]["paper"]
    assert paper["gold_unique_scored_field_values"] == 3
    assert paper["extraction_unique_scored_field_values"] == 3
    assert paper["extraction_to_gold"] == {
        "present_in_gold": 3,
        "outside_gold_surface": 0,
        "contradicts_gold": 0,
        "unadjudicated": 0,
    }
    assert paper["gold_to_extraction"] == {
        "represented_in_extraction": 3,
        "missing_from_extraction": 0,
    }
    assert paper["source_faithfulness"] == {
        "source_supported": 3,
        "unsupported_or_bad_provenance": 0,
    }


def test_direct_builder_leaves_unmatched_extraction_unadjudicated_by_default():
    artifact = build_semantic_field_overlap(
        gold_rows=[
            {
                "Entity": "entity-b-id",
                "Surface": "table_s6_group_count_contingency",
                "Patients with Entity and Outcome": 3,
            }
        ],
        extraction_rows=[
            {
                "entity_raw": "entity-b-id",
                "surface": "table_2_model_based_estimate",
                "effect_estimate": 1.7,
                "provenance": {
                    "entity_raw": {"sources": [{"quote": "entity-b-id", "location": "table2.md"}]},
                    "surface": {"sources": [{"quote": "model estimate", "location": "table2.md"}]},
                    "effect_estimate": {"sources": [{"quote": "OR 1.7", "location": "table2.md"}]},
                },
            }
        ],
        field_aliases={
            "Entity": ["entity_raw"],
            "Surface": ["surface"],
            "Patients with Entity and Outcome": ["n_events"],
        },
    )

    paper = artifact["papers"]["paper"]
    assert paper["extraction_to_gold"] == {
        "present_in_gold": 1,
        "outside_gold_surface": 0,
        "contradicts_gold": 0,
        "unadjudicated": 2,
    }
    assert paper["gold_to_extraction"] == {
        "represented_in_extraction": 1,
        "missing_from_extraction": 2,
    }


def test_direct_builder_explicit_adjudications_override_fallbacks():
    artifact = build_semantic_field_overlap(
        gold_rows=[{"P Value": "0.03"}],
        extraction_rows=[
            {
                "p_value_raw": "0.04",
                "n_events": 12,
                "provenance": {
                    "p_value_raw": {"sources": [{"quote": "p = 0.04", "location": "table.md"}]},
                },
            }
        ],
        field_aliases={
            "P Value": ["p_value_raw"],
            "Patients with Entity and Outcome": ["n_events"],
        },
        adjudications={
            "extraction_to_gold": [
                {
                    "extraction": {"row_index": 0, "field": "p_value_raw"},
                    "status": "contradicts_gold",
                    "gold_key": "p_value=0.03",
                },
                {
                    "extraction": {"row_index": 0, "field": "n_events"},
                    "status": "outside_gold_surface",
                },
            ],
            "gold_to_extraction": [
                {
                    "gold": {"row_index": 0, "field": "P Value"},
                    "status": "represented_in_extraction",
                    "extraction_key": "p_value=0.04",
                }
            ],
            "source_faithfulness": [
                {
                    "extraction": {"row_index": 0, "field": "n_events"},
                    "status": "unsupported_or_bad_provenance",
                }
            ],
        },
    )

    paper = artifact["papers"]["paper"]
    assert paper["extraction_to_gold"] == {
        "present_in_gold": 0,
        "outside_gold_surface": 1,
        "contradicts_gold": 1,
        "unadjudicated": 0,
    }
    assert paper["gold_to_extraction"] == {
        "represented_in_extraction": 1,
        "missing_from_extraction": 0,
    }
    assert paper["source_faithfulness"] == {
        "source_supported": 1,
        "unsupported_or_bad_provenance": 1,
    }
