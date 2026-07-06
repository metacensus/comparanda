from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / ".claude" / "hooks" / "validate_extraction_write.py"


def run_hook(tool_name: str, tool_input: dict) -> subprocess.CompletedProcess[str]:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_blocks_write_tool_to_final_extraction_jsonl() -> None:
    proc = run_hook(
        "Write",
        {"file_path": "projects/example_domain/extractions/paper_1.jsonl"},
    )

    assert proc.returncode == 2
    assert "BLOCKED" in proc.stderr


def test_allows_write_tool_to_staging_jsonl() -> None:
    proc = run_hook(
        "Write",
        {"file_path": "projects/example_domain/extractions/.staging/paper_1/debug.jsonl"},
    )

    assert proc.returncode == 0


def test_allows_read_only_bash_inspection() -> None:
    for command in (
        "wc -l projects/example_domain/extractions/paper_1.jsonl",
        "head -n 2 projects/example_domain/extractions/paper_1.jsonl",
        "python3 -c \"open('projects/example_domain/extractions/paper_1.jsonl', 'r').read()\"",
        "python3 -c \"from pathlib import Path; Path('projects/example_domain/extractions/paper_1.jsonl').read_text()\"",
    ):
        proc = run_hook("Bash", {"command": command})
        assert proc.returncode == 0, command


def test_blocks_bash_mutations_to_final_extraction_jsonl() -> None:
    commands = [
        "printf '{}' > projects/example_domain/extractions/paper_1.jsonl",
        "printf '{}' >> projects/example_domain/extractions/paper_1.jsonl",
        "printf '{}' | tee projects/example_domain/extractions/paper_1.jsonl",
        "touch projects/example_domain/extractions/paper_1.jsonl",
        "sed -i '' 's/a/b/' projects/example_domain/extractions/paper_1.jsonl",
        "perl -pi -e 's/a/b/' projects/example_domain/extractions/paper_1.jsonl",
        "python3 -c \"open('projects/example_domain/extractions/paper_1.jsonl', 'w').write('{}')\"",
        "python3 -c \"from pathlib import Path; Path('projects/example_domain/extractions/paper_1.jsonl').write_text('{}')\"",
    ]

    for command in commands:
        proc = run_hook("Bash", {"command": command})
        assert proc.returncode == 2, command


def test_allows_sanctioned_finalizers_and_runner() -> None:
    for command in (
        "uv run scripts/extraction/write_extraction.py iter1 --out projects/example_domain/extractions/paper_1.jsonl",
        "uv run scripts/extraction/extraction_builder.py finalize --out projects/example_domain/extractions/paper_1.jsonl",
        "uv run scripts/extraction/run_extraction_build.py --finalize --out projects/example_domain/extractions/paper_1.jsonl",
    ):
        proc = run_hook("Bash", {"command": command})
        assert proc.returncode == 0, command
