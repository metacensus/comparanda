from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "normalization_reconcile.py"


def write_schema(path: Path, version: str, guidance: str, include_routes: bool = True) -> None:
    fields = [
        {
            "name": "grade_threshold_raw",
            "type": "string",
            "normalization": "grade_thresholds",
            "extraction_guidance": guidance,
        },
        {
            "name": "grade_threshold_canonical",
            "type": "string",
            "normalization": "grade_thresholds",
            "extraction_guidance": guidance,
        },
    ]
    if include_routes:
        fields.extend(
            [
                {
                    "name": "administration_route_raw",
                    "type": "string",
                    "normalization": "routes",
                    "extraction_guidance": "Use oral or intravenous route labels.",
                },
                {
                    "name": "administration_route_canonical",
                    "type": "string",
                    "normalization": "routes",
                    "extraction_guidance": "Canonical route.",
                },
            ]
        )
    path.write_text(
        "schema_version: \"{}\"\nfields:\n".format(version)
        + "\n".join(
            [
                "  - name: {name}\n"
                "    type: {type}\n"
                "    normalization: {normalization}\n"
                "    extraction_guidance: |\n"
                "      {extraction_guidance}".format(**field)
                for field in fields
            ]
        )
        + "\n"
    )


def write_table(path: Path, table_name: str, entries: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "table_name": table_name,
                "last_updated_iteration": 1,
                "entries": entries,
            },
            indent=2,
        )
    )


def run_reconcile(project: Path, iteration: int = 2) -> dict:
    result = subprocess.run(
        [
            "uv",
            "run",
            str(SCRIPT),
            "--project",
            str(project),
            "--iteration",
            str(iteration),
            "--output",
            "-",
        ],
        cwd=REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(result.stdout)


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "example_domain"
    (project / "schema").mkdir(parents=True)
    (project / "normalization").mkdir()
    return project


def test_changed_schema_null_entry_emits_candidate_with_hint_terms(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_schema(
        project / "schema" / "v001.yaml",
        "v001",
        "Allowed values include grade3_to_4.",
    )
    write_schema(
        project / "schema" / "v002.yaml",
        "v002",
        "Allowed values include grade3_to_4 and proportional_odds_ordinal_model.",
    )
    write_table(
        project / "normalization" / "grade_thresholds.json",
        "grade_thresholds",
        [
            {
                "raw": "01v2v34",
                "canonical_id": None,
                "confidence": 0.6,
                "source": "llm",
                "iteration_added": 1,
            }
        ],
    )
    write_table(
        project / "normalization" / "routes.json",
        "routes",
        [
            {
                "raw": "oral",
                "canonical_id": "oral",
                "confidence": 1.0,
                "source": "deterministic",
                "iteration_added": 1,
                "iteration_updated": 2,
            }
        ],
    )

    report = run_reconcile(project)
    candidates = report["candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["table"] == "grade_thresholds"
    assert candidate["raw"] == "01v2v34"
    assert "canonical_id_null" in candidate["reasons"]
    assert "confidence_below_0.9" in candidate["reasons"]
    assert "normalizable_field_changed_since_previous_schema" in candidate["reasons"]
    assert "proportional_odds_ordinal_model" in candidate["schema_hint_terms"]


def test_current_high_confidence_entry_in_unchanged_table_is_not_candidate(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    guidance = "Allowed values include grade3_to_4."
    write_schema(project / "schema" / "v001.yaml", "v001", guidance)
    write_schema(project / "schema" / "v002.yaml", "v002", guidance)
    write_table(
        project / "normalization" / "grade_thresholds.json",
        "grade_thresholds",
        [
            {
                "raw": "012v34",
                "canonical_id": "grade3_to_4",
                "confidence": 0.95,
                "source": "llm",
                "iteration_added": 1,
                "iteration_updated": 2,
            }
        ],
    )
    write_table(
        project / "normalization" / "routes.json",
        "routes",
        [
            {
                "raw": "oral",
                "canonical_id": "oral",
                "confidence": 1.0,
                "source": "deterministic",
                "iteration_added": 1,
                "iteration_updated": 2,
            }
        ],
    )

    report = run_reconcile(project)
    assert report["candidates"] == []


def test_missing_normalization_table_is_reported(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    guidance = "Allowed values include grade3_to_4."
    write_schema(project / "schema" / "v001.yaml", "v001", guidance, include_routes=False)
    write_schema(project / "schema" / "v002.yaml", "v002", guidance, include_routes=True)
    write_table(project / "normalization" / "grade_thresholds.json", "grade_thresholds", [])

    report = run_reconcile(project)
    missing = [c for c in report["candidates"] if c["reasons"] == ["normalization_table_missing"]]
    assert len(missing) == 1
    assert missing[0]["table"] == "routes"


def test_unaffected_low_confidence_entries_are_appendix_by_default(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_schema(
        project / "schema" / "v001.yaml",
        "v001",
        "Allowed values include grade3_to_4.",
    )
    write_schema(
        project / "schema" / "v002.yaml",
        "v002",
        "Allowed values include grade3_to_4 and proportional_odds_ordinal_model.",
    )
    write_table(
        project / "normalization" / "grade_thresholds.json",
        "grade_thresholds",
        [
            {
                "raw": "012v34",
                "canonical_id": "grade3_to_4",
                "confidence": 0.95,
                "source": "llm",
                "iteration_added": 1,
            }
        ],
    )
    write_table(
        project / "normalization" / "routes.json",
        "routes",
        [
            {
                "raw": "oral-ish",
                "canonical_id": "oral",
                "confidence": 0.8,
                "source": "llm",
                "iteration_added": 1,
            }
        ],
    )

    report = run_reconcile(project)
    assert report["candidates"] == []
    assert report["primary_candidates"] == []
    assert len(report["appendix_candidates"]) == 1
    assert report["appendix_candidates"][0]["table"] == "routes"
