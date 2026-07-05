import json

from harness.eval.workbench import distill_comparison, write_workbench_bundle


def test_distill_comparison_keeps_indices_and_drops_heavy_fields():
    comparison = {
        "paper_id": "paper_a",
        "gold_rows": 1,
        "extraction_rows": 1,
        "paired_rows": [
            {
                "gold_index": 0,
                "gold_fields": {"Outcome": "outcome"},
                "extraction_candidates": [
                    {
                        "extraction_index": 3,
                        "n_total": 10,
                        "provenance": {"n_total": {"quote": "10"}},
                    }
                ],
                "has_match": True,
            }
        ],
        "unmatched_extraction_rows": [],
    }

    distilled = distill_comparison(comparison)
    candidate = distilled["paired_rows"][0]["extraction_candidates"][0]

    assert candidate["extraction_index"] == 3
    assert candidate["n_total"] == 10
    assert "provenance" not in candidate
    assert distilled["gold_row_count"] == 1
    assert distilled["extraction_row_count"] == 1
    assert distilled["truncation"]["dropped_heavy_fields"]


def test_write_workbench_bundle_emits_chunked_indexes(tmp_path):
    comparison = {
        "paper_id": "paper_a",
        "gold_rows": 2,
        "extraction_rows": 2,
        "paired_rows": [
            {
                "gold_index": 0,
                "gold_fields": {"Outcome": "outcome"},
                "extraction_candidates": [
                    {"extraction_index": 3, "n_total": 10, "quote": "heavy"},
                    {"extraction_index": 4, "n_total": 12},
                ],
            },
            {
                "gold_index": 1,
                "gold_fields": {"Outcome": "survival"},
                "extraction_candidates": [{"extraction_index": 4, "n_total": 12}],
            },
        ],
        "unmatched_extraction_rows": [{"extraction_index": 5, "outcome_raw": "other"}],
    }

    summary = write_workbench_bundle(comparison, tmp_path, max_jsonl_bytes=120)

    assert summary["gold_row_count"] == 2
    assert summary["unique_extraction_row_index_count"] == 3
    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "paired_gold_rows.manifest.json").exists()
    assert (tmp_path / "extraction_row_index.manifest.json").exists()

    extraction_manifest = json.loads((tmp_path / "extraction_row_index.manifest.json").read_text())
    assert extraction_manifest["row_count"] == 3
    assert extraction_manifest["files"]
