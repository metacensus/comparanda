from pathlib import Path

import pytest

from scripts.review.portal_export import _prepare_output_dir, main


def test_prepare_output_refuses_to_replace_published_run(tmp_path: Path) -> None:
    out = tmp_path / "runs" / "published"
    out.mkdir(parents=True)
    marker = out / "manifest.json"
    marker.write_text("{}")

    with pytest.raises(FileExistsError, match="output already exists"):
        _prepare_output_dir(out)

    assert marker.read_text() == "{}"


def test_prepare_output_force_is_explicit(tmp_path: Path) -> None:
    out = tmp_path / "candidate"
    out.mkdir()
    (out / "stale.json").write_text("{}")

    _prepare_output_dir(out, force=True)

    assert not (out / "stale.json").exists()
    assert (out / "rows").is_dir()
    assert (out / "bundles").is_dir()
    assert (out / "pdfs").is_dir()


def test_units_only_accepts_an_explicit_snapshot_without_project(tmp_path: Path) -> None:
    missing_snapshot = tmp_path / "missing"

    assert main(["--run-id", "ignored", "--out", str(missing_snapshot), "--units-only"]) == 1

