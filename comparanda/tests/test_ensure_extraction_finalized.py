from __future__ import annotations

import importlib.util
from pathlib import Path
import json
import os
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / ".claude" / "hooks" / "ensure_extraction_finalized.py"


def load_hook_module():
    spec = importlib.util.spec_from_file_location("ensure_extraction_finalized", HOOK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transcript_success_accepts_write_extraction_marker(tmp_path: Path) -> None:
    module = load_hook_module()
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text(
        "[write_extraction iter1] wrote 12 rows to projects/example/extractions/paper.jsonl\n"
    )

    assert module.transcript_has_success(transcript)


def test_transcript_success_accepts_builder_runner_final_validation(tmp_path: Path) -> None:
    module = load_hook_module()
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text(
        "[run_extraction_build] staged_rows=12 rows_path=projects/example/extractions/.staging/paper/expanded/rows.expanded.json\n"
        "[run_extraction_build] final_validation_ok=True failure_count=0\n"
        "[run_extraction_build] ok\n"
    )

    assert module.transcript_has_success(transcript)


def test_transcript_success_rejects_builder_runner_without_final_validation(tmp_path: Path) -> None:
    module = load_hook_module()
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text("[run_extraction_build] ok\n")

    assert not module.transcript_has_success(transcript)


def _write_meta(subagents: Path, agent_id: str, agent_type: str = "extractor") -> Path:
    path = subagents / f"agent-{agent_id}.meta.json"
    path.write_text(json.dumps({"agentType": agent_type}))
    return path


def _write_transcript(subagents: Path, agent_id: str, record: dict, mtime: float) -> Path:
    path = subagents / f"agent-{agent_id}.jsonl"
    path.write_text(json.dumps(record) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def test_find_extractor_prefers_stopping_transcript_over_active_sibling(tmp_path: Path) -> None:
    module = load_hook_module()
    parent = tmp_path / "parent-session.jsonl"
    parent.write_text("{}\n")
    subagents = tmp_path / "parent-session" / "subagents"
    subagents.mkdir(parents=True)

    now = time.time()
    _write_meta(subagents, "active")
    _write_meta(subagents, "stopped")
    active = _write_transcript(
        subagents,
        "active",
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "still running"}],
            },
        },
        now + 10,
    )
    stopped = _write_transcript(
        subagents,
        "stopped",
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "Done."}],
            },
        },
        now,
    )

    assert module.transcript_looks_stopping(stopped)
    assert not module.transcript_looks_stopping(active)
    assert module.find_extractor_subagent_transcript(parent) == stopped
