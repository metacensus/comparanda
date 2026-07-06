import json

from harness.eval.batch_eval import merge_batch_artifacts, plan_batches


def test_plan_batches_splits_by_gold_rows(tmp_path):
    comparisons = tmp_path / "comparisons"
    comparisons.mkdir()
    for paper_id, gold_rows in (("paper_a", 200), ("paper_b", 100), ("paper_c", 80)):
        (comparisons / f"{paper_id}.json").write_text(
            json.dumps(
                {
                    "paper_id": paper_id,
                    "benchmark_status": "gold_scored",
                    "gold_rows": gold_rows,
                    "extraction_rows": gold_rows,
                }
            )
        )

    manifest = plan_batches(comparisons_dir=comparisons, max_gold_rows=250)

    assert manifest["paper_count"] == 3
    assert manifest["batch_count"] == 2
    assert all(batch["gold_rows"] <= 250 for batch in manifest["batches"])


def test_merge_batch_artifacts_rejects_duplicate_papers(tmp_path):
    batch_a = tmp_path / "batch_a"
    batch_b = tmp_path / "batch_b"
    batch_a.mkdir()
    batch_b.mkdir()
    payload = {"papers": {"paper_a": {"findings": []}}}
    (batch_a / "finding_scores.paper_a.json").write_text(json.dumps(payload))
    (batch_b / "finding_scores.paper_a.json").write_text(json.dumps(payload))

    try:
        merge_batch_artifacts(batch_dirs=[batch_a, batch_b], output_dir=tmp_path / "out")
    except ValueError as exc:
        assert "duplicate papers" in str(exc)
    else:
        raise AssertionError("expected duplicate-paper merge failure")
