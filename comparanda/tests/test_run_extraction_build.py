from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def make_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "projects" / "example_domain"
    paper_id = "paper_1"
    bundle = project / "data" / "processed" / paper_id
    schema = project / "schema" / "v001.yaml"
    (bundle / "sections").mkdir(parents=True)
    (project / "extractions" / ".staging" / paper_id / "scripts").mkdir(parents=True)
    schema.parent.mkdir(parents=True)

    (bundle / "manifest.json").write_text(json.dumps({
        "paper_id": "111",
        "sections": [
            {"path": "sections/results.md", "heading": "Results", "page_range": [4, 4]}
        ],
        "tables": [],
        "figures": [],
    }))
    (bundle / "sections" / "results.md").write_text(
        "Treatment group A had response in 3 of 10 participants."
    )
    schema.write_text(
        """
schema_version: v001
domain: example_domain
identity_dimensions:
  - field: pmid
  - field: intervention_raw
  - field: outcome_raw
  - field: comparison_group_raw
fields:
  - name: pmid
    type: integer
  - name: intervention_raw
    type: string
  - name: outcome_raw
    type: string
  - name: comparison_group_raw
    type: string
  - name: n_total
    type: integer
""".strip()
    )
    return project, project / "extractions" / ".staging" / paper_id / "scripts" / "build.py"


def test_runner_sets_environment_runs_script_and_finalizes(tmp_path: Path) -> None:
    project, script = make_project(tmp_path)
    paper_id = "paper_1"
    out = project / "extractions" / f"{paper_id}.jsonl"
    script.write_text(
        """
from pathlib import Path
import os

from scripts.extraction_sdk import ExtractionBuilder, paper

paper_id = os.environ["COMPARANDA_PAPER_ID"]
project_dir = Path(os.environ["COMPARANDA_PROJECT_DIR"])
assert Path.cwd().name == "comparanda"
assert os.environ["COMPARANDA_SCHEMA_PATH"].endswith("v001.yaml")

b = ExtractionBuilder(
    project_dir=project_dir,
    paper_id=paper_id,
    schema_path=os.environ["COMPARANDA_SCHEMA_PATH"],
    bundle_dir=os.environ["COMPARANDA_BUNDLE_DIR"],
    staging_dir=os.environ["COMPARANDA_STAGING_DIR"],
)
E = b.evidence
quote = E.quote("sections/results.md").exact("Treatment group A had response in 3 of 10 participants.")
b.common_fields({"pmid": paper(111, E.metadata("paper_id"))})
with b.chunk("results") as c:
    c.add_row({
        "intervention_raw": paper("Treatment group A", quote),
        "outcome_raw": paper("response", quote),
        "comparison_group_raw": paper("group A", quote),
        "n_total": paper(10, quote),
    })
    assert c.validate() == []
b.meta({
    "sources_used": [{"source": "sections/results.md", "role": "direct"}],
    "identity_dimension_values": {"pmid": [111], "intervention_raw": ["Treatment group A"]},
})
b.write_staging()
""".strip()
    )

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_extraction_build.py",
            "--paper-id",
            paper_id,
            "--project",
            str(project),
            "--script",
            str(script),
            "--finalize",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "[run_extraction_build] staged_rows=1" in proc.stderr
    assert "[write_extraction iter1] wrote 1 rows" in proc.stderr
    assert out.exists()
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines[0]["intervention_raw"] == "Treatment group A"
