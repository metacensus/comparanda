import json

from harness.eval.field_coverage_workspace import (
    checkpoint_status,
    load_session_registry,
    prepare_workspace,
)


def _comparison() -> dict:
    return {
        "paper_id": "paper_a",
        "gold_rows": 1,
        "extraction_rows": 1,
        "paired_rows": [
            {
                "gold_index": 0,
                "gold_fields": {"Group": "case", "Events": 4},
                "extraction_candidates": [
                    {
                        "extraction_index": 7,
                        "group": "case",
                        "n_events": 4,
                        "provenance": {"n_events": {"quote": "4"}},
                    }
                ],
            }
        ],
        "unmatched_extraction_rows": [],
    }


def test_prepare_workspace_creates_isolated_contract(tmp_path):
    comparison_path = tmp_path / "comparison.json"
    comparison_path.write_text(json.dumps(_comparison()))
    output_root = tmp_path / "runs"

    workspace = prepare_workspace(
        comparison_path=comparison_path,
        output_root=output_root,
        harness_dir=tmp_path / "harness",
    )

    assert workspace.run_dir == output_root / "paper_a"
    assert (workspace.run_dir / "input" / "comparison.json").exists()
    assert (workspace.run_dir / "input" / "gold_field_checklist.jsonl").exists()
    assert (workspace.run_dir / "input" / "workbench" / "summary.json").exists()
    assert (workspace.run_dir / "scratch" / "helpers" / "README.md").exists()
    assert (workspace.run_dir / "logs" / "run_meta.json").exists()
    prompt = (workspace.run_dir / "prompt.md").read_text()
    assert "Follow your eval-agent system instructions" in prompt
    assert "source of eval methodology" in prompt
    assert "one_shot final eval" in prompt
    assert not (workspace.run_dir / "batches").exists()
    assert workspace.run_mode == "one_shot"


def test_prepare_workspace_overwrite_removes_stale_artifacts(tmp_path):
    comparison_path = tmp_path / "comparison.json"
    comparison_path.write_text(json.dumps(_comparison()))
    stale_dir = tmp_path / "runs" / "paper_a"
    (stale_dir / "batches").mkdir(parents=True)
    (stale_dir / "batches" / "finding_scores.paper_a.json").write_text("{}")

    workspace = prepare_workspace(
        comparison_path=comparison_path,
        output_root=tmp_path / "runs",
        harness_dir=tmp_path / "harness",
        overwrite=True,
    )

    assert workspace.run_dir == stale_dir
    assert not (workspace.run_dir / "batches").exists()
    assert (workspace.run_dir / "input" / "workbench" / "summary.json").exists()


def test_prepare_outer_loop_workspace_copies_prior_eval_and_writes_registry(tmp_path):
    comparison_path = tmp_path / "comparison.json"
    comparison_path.write_text(json.dumps(_comparison()))
    previous = tmp_path / "prior_field_coverage.json"
    previous.write_text(json.dumps({"field_adjudications": []}))
    previous_validation = tmp_path / "prior_validation.json"
    previous_validation.write_text(json.dumps({"valid": True}))
    registry_path = tmp_path / "iterations" / "run_1" / "field_coverage_eval_sessions.json"

    workspace = prepare_workspace(
        comparison_path=comparison_path,
        output_root=tmp_path / "runs",
        harness_dir=tmp_path / "harness",
        run_mode="outer-loop",
        registry_path=registry_path,
        previous_field_coverage_path=previous,
        previous_validation_path=previous_validation,
        agent_id="eval-agent-1",
        parent_session_id="parent-session-1",
        model="claude-sonnet-5[1m]",
    )

    assert workspace.run_mode == "outer_loop"
    assert workspace.registry_path == registry_path
    assert (workspace.run_dir / "input" / "previous_field_coverage.json").exists()
    assert (workspace.run_dir / "input" / "previous_field_coverage_validation.json").exists()
    prompt = (workspace.run_dir / "prompt.md").read_text()
    assert "outer_loop resumable eval" in prompt
    assert "Follow your eval-agent system instructions" in prompt
    assert "field_coverage_delta.json" not in prompt

    registry = load_session_registry(registry_path)
    entry = registry["sessions"]["paper_a"]
    assert entry["agent_id"] == "eval-agent-1"
    assert entry["parent_session_id"] == "parent-session-1"
    assert entry["run_mode"] == "outer_loop"
    assert entry["previous_field_coverage"].endswith("input/previous_field_coverage.json")
    assert entry["output"].endswith("output/field_coverage.json")


def test_checkpoint_status_fails_after_too_many_tool_uses_without_checkpoint(tmp_path):
    run_dir = tmp_path / "paper_a"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True)
    events = []
    for idx in range(3):
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": f"echo {idx}"},
                        }
                    ]
                },
            }
        )
    (log_dir / "claude.stream.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n"
    )

    status = checkpoint_status(run_dir=run_dir, max_tool_uses_before_checkpoint=2)

    assert not status["checkpoint_gate_passed"]
    assert status["checkpoint_gate_violation"]
    assert status["tool_use_count"] == 3


def test_checkpoint_status_passes_with_checkpoint_and_flags_old_artifacts(tmp_path):
    run_dir = tmp_path / "paper_a"
    (run_dir / "scratch").mkdir(parents=True)
    (run_dir / "scratch" / "notes.md").write_text("started\n")
    log_dir = run_dir / "logs"
    log_dir.mkdir()
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "cat ../batches/batch_001/finding_scores.paper.json"},
                }
            ]
        },
    }
    (log_dir / "claude.stream.jsonl").write_text(json.dumps(event) + "\n")

    status = checkpoint_status(run_dir=run_dir, max_tool_uses_before_checkpoint=0)

    assert status["checkpoint_gate_passed"]
    assert status["has_checkpoint"]
    assert status["old_artifact_ref_count"] >= 2
