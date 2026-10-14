"""Prepare and guard isolated field-coverage eval workspaces.

This module formalizes the isolated-retest workflow: one paper per clean workspace, authoritative inputs under ``input/``,
agent scratch space under ``scratch/``, final artifacts under ``output/``, and
a mechanical early-checkpoint gate.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.eval.field_checklist import build_checklist_for_comparison, write_jsonl
from harness.eval.workbench import write_workbench_bundle


# Model used for the isolated eval agent. Override with COMPARANDA_EVAL_MODEL
# or the --model CLI flag.
DEFAULT_EVAL_MODEL = "claude-sonnet-5[1m]"


def default_eval_model() -> str:
    return os.environ.get("COMPARANDA_EVAL_MODEL") or DEFAULT_EVAL_MODEL


CHECKPOINT_FILES = ("progress.json", "notes.md")
RUN_MODES = ("one_shot", "outer_loop")
OLD_ARTIFACT_NAMES = (
    "batches",
    "finding_scores",
    "merged",
    "score.",
    "semantic_field_value_overlap",
    "semantic_value_gate",
    "field_coverage_retests",
)


@dataclass(frozen=True)
class PreparedWorkspace:
    run_dir: Path
    paper_id: str
    prompt_path: Path
    comparison_path: Path
    checklist_path: Path
    workbench_dir: Path
    output_path: Path
    validation_path: Path
    run_mode: str = "one_shot"
    registry_path: Path | None = None


def normalize_run_mode(value: str) -> str:
    mode = value.strip().replace("-", "_")
    if mode not in RUN_MODES:
        raise ValueError(f"unknown field coverage eval run mode: {value!r}")
    return mode


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_session_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "sessions": {}}
    payload = json.loads(path.read_text())
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        raise ValueError(f"field coverage eval session registry has no sessions object: {path}")
    payload.setdefault("version", 1)
    return payload


def upsert_session_registry(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    paper_id = entry.get("paper_id")
    if not paper_id:
        raise ValueError("field coverage eval session entry requires paper_id")
    path.parent.mkdir(parents=True, exist_ok=True)
    registry = load_session_registry(path)
    existing = registry["sessions"].get(paper_id) or {}
    merged = {**existing, **entry}
    registry["sessions"][paper_id] = merged
    registry["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(registry, indent=2) + "\n")
    return merged


def _copy_or_write_comparison(comparison: dict[str, Any], source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.copy2(source, destination)
    else:
        destination.write_text(json.dumps(comparison, indent=2, ensure_ascii=False) + "\n")


def _copy_workbench(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _write_helper_readme(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Field-Coverage Helper Script Workspace",
                "",
                "Use this directory for domain-neutral helper scripts that make the field",
                "coverage adjudication auditable and reproducible.",
                "",
                "Rules:",
                "",
                "- Read authoritative data only from `../../input/`.",
                "- Write final eval output only to `../../output/`.",
                "- Keep helper scripts deterministic and rerunnable.",
                "- Prefer compact row maps, arithmetic reconciliation checks, and",
                "  validation-oriented summaries over hand-built one-off JSON.",
                "- Record important discovered structure in `../notes.md` or",
                "  `../progress.json` before broad analysis continues.",
                "- Do not inspect parent or sibling iteration directories.",
                "- Do not use old `finding_scores`, `score`, `semantic_*`, `merged`,",
                "  `batches`, or previous `field_coverage` artifacts as scaffolding.",
                "",
            ]
        )
    )


def render_prompt(
    *,
    run_dir: Path,
    paper_id: str,
    harness_dir: Path,
    run_mode: str = "one_shot",
    previous_field_coverage: Path | None = None,
    previous_validation: Path | None = None,
) -> str:
    run_mode = normalize_run_mode(run_mode)
    comparison = run_dir / "input" / "comparison.json"
    checklist = run_dir / "input" / "gold_field_checklist.jsonl"
    output = run_dir / "output" / "field_coverage.json"
    validation = run_dir / "output" / "field_coverage_validation.json"
    if run_mode == "outer_loop":
        mode_block = """Run mode:
- outer_loop resumable eval.
- This paper may have prior field-level coverage output copied into this isolated workspace.
"""
    else:
        mode_block = """Run mode:
- one_shot final eval.
"""
    previous_block = ""
    if previous_field_coverage:
        previous_block += f"\nPrior field coverage draft: {previous_field_coverage}\n"
    if previous_validation:
        previous_block += f"Prior field coverage validation: {previous_validation}\n"
    return f"""Run Comparanda field-level coverage eval for this prepared workspace.

Paper:
- {paper_id}

{mode_block}{previous_block}

Inputs:
- comparison: {comparison}
- checklist: {checklist}
- workbench: {run_dir / "input" / "workbench"}

Write:
- field coverage: {output}
- validation: {validation}

Validation:
- cd {harness_dir} &&
  uv run python -m harness.eval.field_coverage \\
    --comparison {comparison} \\
    --checklist {checklist} \\
    --field-coverage {output} \\
    --output {validation}

Follow your eval-agent system instructions for workspace setup, artifact scope,
output contract, checkpointing, helper scripts, outer-loop resume behavior, and
validation. This prompt is only the per-paper task pointer; it is not the
source of eval methodology.
"""


def prepare_workspace(
    *,
    comparison_path: Path,
    output_root: Path,
    paper_id: str | None = None,
    checklist_path: Path | None = None,
    workbench_dir: Path | None = None,
    harness_dir: Path | None = None,
    overwrite: bool = False,
    run_mode: str = "one_shot",
    registry_path: Path | None = None,
    previous_field_coverage_path: Path | None = None,
    previous_validation_path: Path | None = None,
    agent_id: str | None = None,
    parent_session_id: str | None = None,
    model: str | None = None,
) -> PreparedWorkspace:
    model = model or default_eval_model()
    run_mode = normalize_run_mode(run_mode)
    comparison_path = comparison_path.resolve()
    output_root = output_root.resolve()
    comparison = load_json(comparison_path)
    resolved_paper_id = paper_id or comparison.get("paper_id") or comparison_path.stem
    run_dir = output_root / str(resolved_paper_id)
    if run_dir.exists() and any(run_dir.iterdir()):
        if not overwrite:
            raise FileExistsError(f"workspace already exists: {run_dir}")
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    input_dir = run_dir / "input"
    output_dir = run_dir / "output"
    scratch_dir = run_dir / "scratch"
    logs_dir = run_dir / "logs"
    helper_dir = scratch_dir / "helpers"
    for directory in (input_dir, output_dir, helper_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    workspace_comparison = input_dir / "comparison.json"
    _copy_or_write_comparison(comparison, comparison_path, workspace_comparison)

    workspace_checklist = input_dir / "gold_field_checklist.jsonl"
    if checklist_path:
        shutil.copy2(checklist_path.resolve(), workspace_checklist)
    else:
        write_jsonl(workspace_checklist, build_checklist_for_comparison(comparison))

    workspace_workbench = input_dir / "workbench"
    if workbench_dir:
        _copy_workbench(workbench_dir.resolve(), workspace_workbench)
    else:
        if workspace_workbench.exists():
            shutil.rmtree(workspace_workbench)
        write_workbench_bundle(comparison, workspace_workbench)

    _write_helper_readme(helper_dir / "README.md")

    workspace_previous_field_coverage: Path | None = None
    if previous_field_coverage_path:
        workspace_previous_field_coverage = input_dir / "previous_field_coverage.json"
        shutil.copy2(previous_field_coverage_path.resolve(), workspace_previous_field_coverage)

    workspace_previous_validation: Path | None = None
    if previous_validation_path:
        workspace_previous_validation = input_dir / "previous_field_coverage_validation.json"
        shutil.copy2(previous_validation_path.resolve(), workspace_previous_validation)

    resolved_harness_dir = (harness_dir or Path.cwd()).resolve()
    prompt_path = run_dir / "prompt.md"
    prompt_path.write_text(
        render_prompt(
            run_dir=run_dir,
            paper_id=str(resolved_paper_id),
            harness_dir=resolved_harness_dir,
            run_mode=run_mode,
            previous_field_coverage=workspace_previous_field_coverage,
            previous_validation=workspace_previous_validation,
        )
    )

    run_meta = {
        "mode": "isolated_field_coverage_workspace",
        "run_mode": run_mode,
        "paper_id": resolved_paper_id,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_comparison": str(comparison_path),
        "source_checklist": str(checklist_path.resolve()) if checklist_path else None,
        "source_workbench": str(workbench_dir.resolve()) if workbench_dir else None,
        "source_previous_field_coverage": (
            str(previous_field_coverage_path.resolve()) if previous_field_coverage_path else None
        ),
        "source_previous_validation": (
            str(previous_validation_path.resolve()) if previous_validation_path else None
        ),
        "prompt": str(prompt_path),
        "expected_outputs": {
            "field_coverage": str(output_dir / "field_coverage.json"),
            "validation": str(output_dir / "field_coverage_validation.json"),
            "delta": str(output_dir / "field_coverage_delta.json"),
            "blocker": str(output_dir / "blocker.md"),
        },
        "checkpoint_files": [str(scratch_dir / name) for name in CHECKPOINT_FILES],
        "claude_command": (
            "claude -p \"$(cat prompt.md)\" --agent eval "
            f"--model '{model}' --permission-mode bypassPermissions "
            "--output-format stream-json --verbose | tee logs/claude.stream.jsonl"
        ),
    }
    (logs_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2) + "\n")

    resolved_registry_path: Path | None = registry_path.resolve() if registry_path else None
    if resolved_registry_path:
        entry = {
            "paper_id": str(resolved_paper_id),
            "run_mode": run_mode,
            "agent_id": agent_id,
            "parent_session_id": parent_session_id,
            "model": model,
            "workspace": str(run_dir),
            "comparison": str(workspace_comparison),
            "workbench": str(workspace_workbench),
            "checklist": str(workspace_checklist),
            "previous_field_coverage": (
                str(workspace_previous_field_coverage) if workspace_previous_field_coverage else None
            ),
            "previous_validation": (
                str(workspace_previous_validation) if workspace_previous_validation else None
            ),
            "output": str(output_dir / "field_coverage.json"),
            "validation": str(output_dir / "field_coverage_validation.json"),
            "delta": str(output_dir / "field_coverage_delta.json"),
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "last_resumed_at": None,
        }
        upsert_session_registry(resolved_registry_path, entry)

    return PreparedWorkspace(
        run_dir=run_dir,
        paper_id=str(resolved_paper_id),
        prompt_path=prompt_path,
        comparison_path=workspace_comparison,
        checklist_path=workspace_checklist,
        workbench_dir=workspace_workbench,
        output_path=output_dir / "field_coverage.json",
        validation_path=output_dir / "field_coverage_validation.json",
        run_mode=run_mode,
        registry_path=resolved_registry_path,
    )


def _has_checkpoint(run_dir: Path) -> tuple[bool, list[str]]:
    found: list[str] = []
    scratch_dir = run_dir / "scratch"
    for name in CHECKPOINT_FILES:
        path = scratch_dir / name
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            found.append(str(path))
    return bool(found), found


def _stream_events(log_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not log_path.exists():
        return events
    for line in log_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _tool_inputs(events: list[dict[str, Any]]) -> list[str]:
    inputs: list[str] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        for content in message.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "tool_use":
                inputs.append(json.dumps(content.get("input") or {}, sort_keys=True))
    return inputs


def checkpoint_status(
    *,
    run_dir: Path,
    max_tool_uses_before_checkpoint: int = 6,
    log_path: Path | None = None,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    log = log_path.resolve() if log_path else run_dir / "logs" / "claude.stream.jsonl"
    has_checkpoint, checkpoint_files = _has_checkpoint(run_dir)
    events = _stream_events(log)
    tool_inputs = _tool_inputs(events)
    old_refs = [
        {"needle": needle, "tool_input_index": idx, "snippet": tool_input[:500]}
        for idx, tool_input in enumerate(tool_inputs)
        for needle in OLD_ARTIFACT_NAMES
        if needle in tool_input
    ]
    violation = (
        not has_checkpoint
        and len(tool_inputs) > max_tool_uses_before_checkpoint
    )
    return {
        "mode": "isolated_field_coverage_checkpoint_status",
        "run_dir": str(run_dir),
        "log_path": str(log),
        "has_checkpoint": has_checkpoint,
        "checkpoint_files": checkpoint_files,
        "tool_use_count": len(tool_inputs),
        "max_tool_uses_before_checkpoint": max_tool_uses_before_checkpoint,
        "checkpoint_gate_passed": not violation,
        "checkpoint_gate_violation": violation,
        "old_artifact_ref_count": len(old_refs),
        "old_artifact_refs": old_refs,
    }


def validate_workspace(*, run_dir: Path, harness_dir: Path | None = None) -> int:
    run_dir = run_dir.resolve()
    harness = (harness_dir or Path.cwd()).resolve()
    output = run_dir / "output" / "field_coverage_validation.json"
    cmd = [
        sys.executable,
        "-m",
        "harness.eval.field_coverage",
        "--comparison",
        str(run_dir / "input" / "comparison.json"),
        "--checklist",
        str(run_dir / "input" / "gold_field_checklist.jsonl"),
        "--field-coverage",
        str(run_dir / "output" / "field_coverage.json"),
        "--output",
        str(output),
    ]
    result = subprocess.run(cmd, cwd=harness, check=False)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="create an isolated field-coverage workspace")
    prepare.add_argument("--comparison", required=True)
    prepare.add_argument("--output-root", required=True)
    prepare.add_argument("--paper-id")
    prepare.add_argument("--checklist")
    prepare.add_argument("--workbench")
    prepare.add_argument("--harness-dir", default=".")
    prepare.add_argument("--overwrite", action="store_true")
    prepare.add_argument(
        "--run-mode",
        choices=("one-shot", "one_shot", "outer-loop", "outer_loop"),
        default="one-shot",
        help="one-shot final eval or resumable outer-loop eval",
    )
    prepare.add_argument("--registry", help="optional field coverage eval session registry path")
    prepare.add_argument("--previous-field-coverage", help="prior field_coverage.json to copy into input/")
    prepare.add_argument("--previous-validation", help="prior field_coverage_validation.json to copy into input/")
    prepare.add_argument("--agent-id", help="resumable eval agent id, when known")
    prepare.add_argument("--parent-session-id", help="parent Claude session id, when known")
    prepare.add_argument("--model", default=None,
                         help="eval agent model (default: $COMPARANDA_EVAL_MODEL or "
                              f"{DEFAULT_EVAL_MODEL})")

    checkpoint = subparsers.add_parser("checkpoint", help="check early checkpoint compliance")
    checkpoint.add_argument("--run-dir", required=True)
    checkpoint.add_argument("--log")
    checkpoint.add_argument("--max-tool-uses", type=int, default=6)
    checkpoint.add_argument("--output")

    validate = subparsers.add_parser("validate", help="validate a workspace field_coverage.json")
    validate.add_argument("--run-dir", required=True)
    validate.add_argument("--harness-dir", default=".")

    args = parser.parse_args()

    if args.command == "prepare":
        workspace = prepare_workspace(
            comparison_path=Path(args.comparison),
            output_root=Path(args.output_root),
            paper_id=args.paper_id,
            checklist_path=Path(args.checklist) if args.checklist else None,
            workbench_dir=Path(args.workbench) if args.workbench else None,
            harness_dir=Path(args.harness_dir),
            overwrite=args.overwrite,
            run_mode=args.run_mode,
            registry_path=Path(args.registry) if args.registry else None,
            previous_field_coverage_path=(
                Path(args.previous_field_coverage) if args.previous_field_coverage else None
            ),
            previous_validation_path=(
                Path(args.previous_validation) if args.previous_validation else None
            ),
            agent_id=args.agent_id,
            parent_session_id=args.parent_session_id,
            model=args.model,
        )
        print(json.dumps(workspace.__dict__, indent=2, default=str))
        return 0

    if args.command == "checkpoint":
        status = checkpoint_status(
            run_dir=Path(args.run_dir),
            log_path=Path(args.log) if args.log else None,
            max_tool_uses_before_checkpoint=args.max_tool_uses,
        )
        payload = json.dumps(status, indent=2)
        if args.output:
            Path(args.output).write_text(payload + "\n")
        else:
            print(payload)
        return 0 if status["checkpoint_gate_passed"] else 1

    if args.command == "validate":
        return validate_workspace(run_dir=Path(args.run_dir), harness_dir=Path(args.harness_dir))

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
