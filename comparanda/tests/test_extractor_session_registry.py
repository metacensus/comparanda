from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.extractor_session_registry import (  # noqa: E402
    get_session,
    hydrate_manifest,
    load_registry,
    registry_path,
    upsert_session,
)


def test_registry_upsert_and_get(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "example_domain"

    entry = upsert_session(
        project,
        paper_id="paper_1",
        extractor_agent_id="agent123",
        parent_session_id="parent456",
        schema_version="v002",
        schema_path="projects/example_domain/schema/v002.yaml",
    )

    assert entry["paper_id"] == "paper_1"
    assert entry["extractor_agent_id"] == "agent123"
    assert entry["parent_session_id"] == "parent456"
    assert entry["schema_version"] == "v002"
    assert entry["staging_script_path"].endswith(
        "projects/example_domain/extractions/.staging/paper_1/scripts/build.py"
    )
    assert registry_path(project).exists()
    assert get_session(project, "paper_1")["extractor_agent_id"] == "agent123"


def test_registry_upsert_preserves_existing_fields(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "example_domain"

    upsert_session(
        project,
        paper_id="paper_1",
        extractor_agent_id="agent123",
        schema_version="v001",
    )
    entry = upsert_session(
        project,
        paper_id="paper_1",
        schema_version="v002",
        notes="schema iteration",
    )

    assert entry["extractor_agent_id"] == "agent123"
    assert entry["schema_version"] == "v002"
    assert entry["notes"] == "schema iteration"


def test_hydrate_manifest_handles_missing_legacy_manifest(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "example_domain"
    upsert_session(project, paper_id="legacy_paper", extractor_agent_id="agent123")

    assert hydrate_manifest(project, "legacy_paper") is None
    assert load_registry(project)["extractors"]["legacy_paper"]["extractor_agent_id"] == "agent123"


def test_hydrate_manifest_writes_extractor_session(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "example_domain"
    manifest = project / "extractions" / ".staging" / "paper_1" / "build_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"paper_id": "paper_1"}))
    upsert_session(project, paper_id="paper_1", extractor_agent_id="agent123")

    hydrated = hydrate_manifest(project, "paper_1")

    assert hydrated == manifest
    payload = json.loads(manifest.read_text())
    assert payload["extractor_session"]["extractor_agent_id"] == "agent123"
