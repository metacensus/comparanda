import json
from pathlib import Path

import pytest

from scripts.project_layout import LayoutError, ProjectLayout


def test_loads_flattened_external_project_config(tmp_path: Path) -> None:
    project = tmp_path / "comparanda-example"
    project.mkdir()
    (project / "comparanda-project.json").write_text(
        json.dumps(
            {
                "domain": "dpyd",
                "project_root": ".",
                "iterations": "iterations",
                "benchmark": {
                    "eval_config": "benchmark/eval_config",
                    "gold_standard": "benchmark/gold_standard",
                },
                "export": {"runs": "export/runs"},
            }
        )
    )

    layout = ProjectLayout.load(project)

    assert layout.root == project.resolve()
    assert layout.domain == "dpyd"
    assert layout.iterations_dir == (project / "iterations").resolve()
    assert layout.eval_config_dir == (project / "benchmark/eval_config").resolve()
    assert layout.gold_standard_dir == (project / "benchmark/gold_standard").resolve()
    assert layout.export_dir("run-007") == (project / "export/runs/run-007").resolve()


def test_environment_overrides_related_paths(tmp_path: Path) -> None:
    layout = ProjectLayout.load(
        environ={
            "COMPARANDA_PROJECT_DIR": str(tmp_path / "project"),
            "COMPARANDA_ITERATIONS_DIR": str(tmp_path / "run-state"),
            "COMPARANDA_EVAL_CONFIG_DIR": str(tmp_path / "eval-config"),
            "COMPARANDA_GOLD_STANDARD_DIR": str(tmp_path / "gold-standard"),
            "COMPARANDA_EXPORT_RUNS_DIR": str(tmp_path / "exports"),
        }
    )

    assert layout.root == (tmp_path / "project").resolve()
    assert layout.iterations_dir == (tmp_path / "run-state").resolve()
    assert layout.eval_config_dir == (tmp_path / "eval-config").resolve()
    assert layout.gold_standard_dir == (tmp_path / "gold-standard").resolve()
    assert layout.export_dir("candidate") == (tmp_path / "exports/candidate").resolve()


def test_requires_an_explicit_project_source() -> None:
    with pytest.raises(LayoutError, match="missing project root"):
        ProjectLayout.load(environ={})


def test_rejects_traversal_when_export_path_is_derived(tmp_path: Path) -> None:
    layout = ProjectLayout.load(
        project=tmp_path,
        environ={"COMPARANDA_EXPORT_RUNS_DIR": str(tmp_path / "exports")},
    )

    with pytest.raises(LayoutError, match="single directory name"):
        layout.export_dir("../outside", environ={})
