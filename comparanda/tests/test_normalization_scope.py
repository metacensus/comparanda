from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
API = REPO / "scripts" / "normalization" / "normalization_api.py"
RECONCILE = REPO / "scripts" / "normalization" / "normalization_reconcile.py"
SCOPE_ENV = "COMPARANDA_NORMALIZE_PAPERS"

SCHEMA = """
schema_version: {version}
fields:
  - name: paper_id
    type: string
    normalization: null
  - name: material_raw
    type: string
    normalization: materials
    extraction_guidance: {guidance}
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
"""

# paper_1 and paper_2 share mapped values; each paper also has a value of its own.
PAPERS = {
    "paper_1": [("Thing A", ["Assay X"]), ("Thing New", ["Assay X"])],
    "paper_2": [("Thing A", ["Assay X"]), ("Only Out", ["Assay X"])],
    "paper_3": [("thing  a", ["Assay X"])],
}


def entry(raw: str, canonical_id: str | None, confidence: float) -> dict:
    return {
        "raw": raw,
        "canonical_id": canonical_id,
        "confidence": confidence,
        "source": "llm",
        "iteration_added": 1,
    }


def write_table(project: Path, table: str, entries: list[dict]) -> None:
    path = project / "normalization" / f"{table}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"table_name": table, "last_updated_iteration": 1, "entries": entries}, indent=2)
        + "\n"
    )


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "demo"
    (project / "schema").mkdir(parents=True)
    (project / "schema" / "v001.yaml").write_text(
        SCHEMA.format(version="v001", guidance="Copy the material name.")
    )
    write_table(
        project,
        "materials",
        [entry("Thing A", "thing_a", 0.95), entry("Only Out", None, 0.5)],
    )
    write_table(project, "assays", [entry("Assay X", "assay_x", 0.95)])
    for paper, rows in PAPERS.items():
        path = project / "extractions" / f"{paper}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps(
                    {
                        "paper_id": paper,
                        "material_raw": material,
                        "material_id": None,
                        "assay_names": assays,
                        "assay_ids": None,
                    }
                )
                + "\n"
                for material, assays in rows
            )
        )
    return project


def run(script: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    child = {k: v for k, v in os.environ.items() if k != SCOPE_ENV}
    child.update(env or {})
    return subprocess.run(
        ["uv", "run", str(script), *args],
        cwd=REPO,
        env=child,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def snapshot(project: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.stem: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted((project / "extractions").glob("*.jsonl"))
    }


def rows(project: Path, paper: str) -> list[dict]:
    path = project / "extractions" / f"{paper}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def stdout_json(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_scoped_apply_rewrites_in_scope_file_and_never_opens_the_others(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    before = snapshot(project)
    # Unreadable and unwritable: if apply opened an out-of-scope file at all, it would fail.
    for paper in ("paper_2", "paper_3"):
        (project / "extractions" / f"{paper}.jsonl").chmod(0o000)
    try:
        proc = run(API, "apply", "--project", str(project), "--papers", "paper_1")
    finally:
        for paper in ("paper_2", "paper_3"):
            (project / "extractions" / f"{paper}.jsonl").chmod(0o644)
    after = snapshot(project)

    assert after["paper_2"] == before["paper_2"]
    assert after["paper_3"] == before["paper_3"]
    assert after["paper_1"][0] != before["paper_1"][0]
    first, second = rows(project, "paper_1")
    assert first["material_raw"] == "Thing A"
    assert first["material_id"] == "thing_a"
    assert first["assay_ids"] == ["assay_x"]
    assert second["material_id"] is None
    summary = stdout_json(proc)
    assert list(summary["changed_files"]) == [str(project / "extractions" / "paper_1.jsonl")]
    assert summary["paper_scope"]["source"] == "--papers"
    assert summary["paper_scope"]["papers"] == ["paper_1"]


def test_environment_sets_the_default_scope_and_flag_overrides_it(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    before = snapshot(project)
    proc = run(API, "apply", "--project", str(project), env={SCOPE_ENV: "paper_2"})
    after = snapshot(project)
    assert stdout_json(proc)["paper_scope"]["source"] == SCOPE_ENV
    assert after["paper_1"] == before["paper_1"]
    assert after["paper_3"] == before["paper_3"]
    assert rows(project, "paper_2")[0]["material_id"] == "thing_a"

    run(API, "apply", "--project", str(project), "--papers", "paper_3", env={SCOPE_ENV: "paper_2"})
    final = snapshot(project)
    assert final["paper_1"] == before["paper_1"]
    assert final["paper_2"] == after["paper_2"]
    assert rows(project, "paper_3")[0]["material_id"] is None
    assert rows(project, "paper_3")[0]["assay_ids"] == ["assay_x"]


def test_dry_run_reports_the_scope_and_writes_nothing(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    before = snapshot(project)
    proc = run(
        API,
        "apply",
        "--project",
        str(project),
        "--papers",
        "paper_1,missing_paper.jsonl",
        "--dry-run",
    )
    assert snapshot(project) == before
    scope = stdout_json(proc)["paper_scope"]
    assert scope["papers"] == ["missing_paper", "paper_1"]
    assert scope["files_in_scope"] == [str(project / "extractions" / "paper_1.jsonl")]
    assert scope["papers_without_extraction_file"] == ["missing_paper"]
    assert "dry_run=True paper_scope=1/2 files" in proc.stderr


def test_no_scope_behaves_as_before(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    for env in ({}, {SCOPE_ENV: " "}):
        proc = run(API, "apply", "--project", str(project), "--dry-run", env=env)
        summary = stdout_json(proc)
        assert "paper_scope" not in summary
        assert len(summary["changed_files"]) == 3
        assert "paper_scope" not in proc.stderr
    run(API, "apply", "--project", str(project))
    assert rows(project, "paper_1")[0]["material_id"] == "thing_a"
    assert rows(project, "paper_2")[0]["material_id"] == "thing_a"
    assert rows(project, "paper_3")[0]["assay_ids"] == ["assay_x"]


def test_empty_papers_flag_is_refused(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    proc = run(API, "apply", "--project", str(project), "--papers", ",", check=False)
    assert proc.returncode != 0
    assert "names no paper" in proc.stderr


def test_unresolved_listing_respects_the_scope(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    everything = stdout_json(run(API, "unresolved", "--project", str(project)))
    assert "paper_scope" not in everything
    materials = everything["tables"]["materials"]
    assert [v["raw"] for v in materials["not_in_table"]] == ["Thing New"]
    assert {v["raw"]: v["papers"] for v in materials["in_table"]} == {
        "Only Out": ["paper_2"],
        "Thing A": ["paper_1", "paper_2"],
        "thing  a": ["paper_3"],
    }
    loose = next(v for v in materials["in_table"] if v["raw"] == "thing  a")
    assert loose["match"] == "case_or_whitespace"
    assert loose["table_raw"] == "Thing A"

    scoped = stdout_json(run(API, "unresolved", "--project", str(project), env={SCOPE_ENV: "paper_1"}))
    assert scoped["extraction_files"] == 1
    assert scoped["paper_scope"]["papers"] == ["paper_1"]
    materials = scoped["tables"]["materials"]
    assert [v["raw"] for v in materials["not_in_table"]] == ["Thing New"]
    assert [v["raw"] for v in materials["in_table"]] == ["Thing A"]
    assert all(v["papers"] == ["paper_1"] for v in materials["in_table"] + materials["not_in_table"])

    other = stdout_json(run(API, "unresolved", "--project", str(project), "--papers", "paper_2"))
    assert other["tables"]["materials"]["not_in_table"] == []
    assert [v["raw"] for v in other["tables"]["materials"]["in_table"]] == ["Only Out", "Thing A"]


def test_reconcile_helper_audits_only_entries_the_scoped_papers_report(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "schema" / "v002.yaml").write_text(
        SCHEMA.format(version="v002", guidance="Copy the material name; prefer thing_a.")
    )
    write_table(
        project,
        "materials",
        [entry("Thing A", "thing_a", 0.6), entry("Only Out", None, 0.5)],
    )

    def report(*args: str, env: dict | None = None) -> dict:
        return stdout_json(
            run(RECONCILE, "--project", str(project), "--iteration", "2", "--output", "-", *args, env=env)
        )

    everything = report()
    assert "paper_scope" not in everything
    assert sorted(c["raw"] for c in everything["candidates"]) == ["Only Out", "Thing A"]

    scoped = report(env={SCOPE_ENV: "paper_1"})
    assert [c["raw"] for c in scoped["candidates"]] == ["Thing A"]
    assert scoped["summary"]["candidate_count"] == 1
    assert scoped["paper_scope"]["candidates_excluded_by_scope"] == 1

    # paper_3 reports "thing  a": matched case- and whitespace-insensitively.
    assert [c["raw"] for c in report("--papers", "paper_3")["candidates"]] == ["Thing A"]
    assert report("--papers", "missing_paper")["candidates"] == []
