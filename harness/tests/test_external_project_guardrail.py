import json
import os
import subprocess
from pathlib import Path


HOOK = Path(__file__).parents[1] / ".claude/hooks/harness-guardrail-comparanda-writes.sh"


def _invoke(harness: Path, project: Path, target: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "CLAUDE_PROJECT_DIR": str(harness),
            "COMPARANDA_PROJECT_DIR": str(project),
            "COMPARANDA_ITERATIONS_DIR": str(project / "iterations"),
        }
    )
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"tool_input": {"file_path": str(target)}}),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_external_project_state_is_read_only_but_iterations_are_writable(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    harness = engine_root / "harness"
    inner = engine_root / "comparanda"
    project = tmp_path / "domain"
    for path in (harness, inner / ".claude/agents", project / "iterations"):
        path.mkdir(parents=True)

    denied = _invoke(harness, project, project / "extractions/paper.jsonl")
    allowed_iterations = _invoke(
        harness, project, project / "iterations/iter_1/eval.json"
    )
    allowed_methodology = _invoke(
        harness, project, inner / ".claude/agents/extractor.md"
    )

    assert denied.returncode == 2
    assert "System 2 may write only configured iteration artifacts" in denied.stderr
    assert allowed_iterations.returncode == 0
    assert allowed_methodology.returncode == 0

