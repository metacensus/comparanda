from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.observation_log import ObservationEntry, append, append_payload, next_id


def test_next_id_uses_max_existing_obs_id_not_line_count(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text(
        "\n".join([
            json.dumps({"id": "OBS-001", "iteration": 1, "phase": "x", "agent": "a", "category": "paper_flag", "content": "one"}),
            json.dumps({"id": "OBS-080", "iteration": 1, "phase": "x", "agent": "a", "category": "paper_flag", "content": "two"}),
            json.dumps({"id": "OBS-080", "iteration": 1, "phase": "x", "agent": "a", "category": "paper_flag", "content": "duplicate historical id"}),
        ])
        + "\n"
    )

    assert next_id(log) == "OBS-081"


def test_append_payload_allocates_collision_safe_id(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text(
        json.dumps({"id": "OBS-002", "iteration": 1, "phase": "x", "agent": "a", "category": "paper_flag", "content": "existing"})
        + "\n"
    )

    entry = append_payload(log, {
        "iteration": 1,
        "phase": "3a_extraction",
        "agent": "extractor",
        "category": "extraction_difficulty",
        "content": "new",
    })

    assert entry.id == "OBS-003"
    assert json.loads(log.read_text().splitlines()[-1])["id"] == "OBS-003"
    assert not log.with_suffix(log.suffix + ".lock").exists()


def test_append_rejects_duplicate_id(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    first = ObservationEntry(
        id="OBS-001",
        iteration=1,
        phase="x",
        agent="a",
        category="paper_flag",
        content="first",
    )
    append(log, first)

    with pytest.raises(ValueError, match="already exists"):
        append(log, first)


def test_append_cli_reads_json_and_prints_allocated_entry(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    payload = {
        "iteration": 1,
        "phase": "3a_extraction",
        "agent": "extractor",
        "category": "paper_flag",
        "content": "via cli",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/observation_log.py",
            "append",
            "--log",
            str(log),
            "--entry-json",
            json.dumps(payload),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    printed = json.loads(proc.stdout)
    assert printed["id"] == "OBS-001"
    assert json.loads(log.read_text())["id"] == "OBS-001"
