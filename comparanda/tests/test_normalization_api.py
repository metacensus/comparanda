from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
API = REPO / "scripts" / "normalization" / "normalization_api.py"


def write_schema(project: Path) -> None:
    (project / "schema").mkdir(parents=True)
    (project / "schema" / "v001.yaml").write_text(
        """
schema_version: v001
fields:
  - name: paper_id
    type: string
    normalization: null
  - name: material_raw
    type: string
    normalization: materials
    extraction_guidance: Copy the material name.
  - name: material_id
    type: string
    normalization: materials
    extraction_guidance: Not populated by the extractor — value null at extraction time.
  - name: assay_names
    type: array
    normalization: assays
    extraction_guidance: Copy assay names.
  - name: assay_ids
    type: array
    normalization: assays
    extraction_guidance: Not populated by the extractor — value null at extraction time.
""".strip()
        + "\n"
    )


def write_table(project: Path, table: str, entries: list[dict]) -> None:
    path = project / "normalization" / f"{table}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"table_name": table, "last_updated_iteration": 1, "entries": entries}, indent=2)
        + "\n"
    )


def write_extraction(project: Path, rows: list[dict]) -> None:
    path = project / "extractions" / "paper_1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def run_api(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", str(API), *args],
        cwd=REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def init_git(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "initial",
        ],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "demo"
    write_schema(project)
    write_table(
        project,
        "materials",
        [
            {
                "raw": "Thing A",
                "canonical_id": "old_thing",
                "confidence": 0.6,
                "source": "llm",
                "iteration_added": 1,
                "previous_mappings": [{"canonical_id": None}],
                "reconciliation_reason": "old verbose audit text",
            }
        ],
    )
    write_table(
        project,
        "assays",
        [
            {
                "raw": "Assay X",
                "canonical_id": "assay_x",
                "confidence": 0.95,
                "source": "llm",
                "iteration_added": 1,
            }
        ],
    )
    write_extraction(
        project,
        [
            {
                "paper_id": "paper_1",
                "material_raw": "Thing A",
                "material_id": None,
                "assay_names": ["Assay X"],
                "assay_ids": None,
            }
        ],
    )
    return project


def test_reconcile_writes_ledger_and_keeps_table_compact(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    proc = run_api(
        project,
        "reconcile",
        "--project",
        str(project),
        "--iteration",
        "2",
        "--table",
        "materials",
        "--raw",
        "Thing A",
        "--disposition",
        "revise_mapping",
        "--canonical-id",
        "thing_a",
        "--confidence",
        "0.95",
        "--reason",
        "Schema-neutral example mapping.",
        "--evidence",
        "fixture evidence",
    )
    assert "[normalization_api] reconcile revise_mapping" in proc.stderr
    table = json.loads((project / "normalization" / "materials.json").read_text())
    entry = table["entries"][0]
    assert entry["canonical_id"] == "thing_a"
    assert entry["confidence"] == 0.95
    assert entry["iteration_updated"] == 2
    assert entry["reviewed_schema_version"] == "v001"
    assert "previous_mappings" not in entry
    assert "reconciliation_reason" not in entry

    ledger = project / "normalization" / "reconciliation" / "decisions_iter_002.jsonl"
    decisions = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert decisions[0]["before"]["canonical_id"] == "old_thing"
    assert decisions[0]["after"]["canonical_id"] == "thing_a"
    assert decisions[0]["reason"] == "Schema-neutral example mapping."


def test_apply_updates_only_canonical_fields_and_validate_passes(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    run_api(
        project,
        "reconcile",
        "--project",
        str(project),
        "--iteration",
        "2",
        "--table",
        "materials",
        "--raw",
        "Thing A",
        "--disposition",
        "revise_mapping",
        "--canonical-id",
        "thing_a",
        "--confidence",
        "0.95",
        "--reason",
        "Schema-neutral example mapping.",
    )
    init_git(tmp_path)
    proc = run_api(project, "apply", "--project", str(project))
    assert '"field_changes": 2' in proc.stdout
    rows = [
        json.loads(line)
        for line in (project / "extractions" / "paper_1.jsonl").read_text().splitlines()
    ]
    assert rows[0]["material_raw"] == "Thing A"
    assert rows[0]["material_id"] == "thing_a"
    assert rows[0]["assay_names"] == ["Assay X"]
    assert rows[0]["assay_ids"] == ["assay_x"]
    validate = run_api(project, "validate", "--project", str(project), "--base-ref", "HEAD")
    assert '"ok": true' in validate.stdout


def test_apply_below_auto_threshold_requires_explicit_reviewed_medium_flag(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    proc = run_api(
        project,
        "apply",
        "--project",
        str(project),
        "--threshold",
        "0.85",
        check=False,
    )
    assert proc.returncode != 0
    assert "--allow-reviewed-medium" in proc.stderr

    ok = run_api(
        project,
        "apply",
        "--project",
        str(project),
        "--threshold",
        "0.85",
        "--allow-reviewed-medium",
        "--dry-run",
    )
    assert '"allow_reviewed_medium": true' in ok.stdout


def test_validate_fails_when_raw_field_changes(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    init_git(tmp_path)
    write_extraction(
        project,
        [
            {
                "paper_id": "paper_1",
                "material_raw": "Changed raw value",
                "material_id": None,
                "assay_names": ["Assay X"],
                "assay_ids": None,
            }
        ],
    )
    proc = run_api(project, "validate", "--project", str(project), "--base-ref", "HEAD", check=False)
    assert proc.returncode == 1
    assert "raw fields changed" in proc.stdout
