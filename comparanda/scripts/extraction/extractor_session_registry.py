#!/usr/bin/env python3
"""Record extractor subagent resume targets for Comparanda inner-loop runs.

The orchestrator learns extractor agent IDs from Claude's Agent/SendMessage
tool results, while build scripts know staging paths and finalized outputs.
This helper keeps those facts in one small JSON registry so subsequent QC
repairs and schema-iteration re-extractions can resume the original extractor
instead of silently launching a fresh one.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REGISTRY_NAME = "extractor_sessions.json"


def registry_path(project: str | Path) -> Path:
    return Path(project) / "extractions" / ".staging" / REGISTRY_NAME


def load_registry(project: str | Path) -> dict[str, Any]:
    path = registry_path(project)
    if not path.exists():
        return {"version": 1, "extractors": {}}
    with path.open() as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"registry top-level must be an object: {path}")
    payload.setdefault("version", 1)
    payload.setdefault("extractors", {})
    if not isinstance(payload["extractors"], dict):
        raise ValueError(f"registry extractors must be an object: {path}")
    return payload


def save_registry(project: str | Path, payload: dict[str, Any]) -> Path:
    path = registry_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def infer_staging_script(project: str | Path, paper_id: str) -> str:
    return str(
        Path(project)
        / "extractions"
        / ".staging"
        / paper_id
        / "scripts"
        / "build.py"
    )


def infer_final_jsonl(project: str | Path, paper_id: str) -> str:
    return str(Path(project) / "extractions" / f"{paper_id}.jsonl")


def upsert_session(
    project: str | Path,
    *,
    paper_id: str,
    extractor_agent_id: str | None = None,
    parent_session_id: str | None = None,
    schema_version: str | None = None,
    schema_path: str | None = None,
    staging_dir: str | None = None,
    staging_script_path: str | None = None,
    finalized_jsonl_path: str | None = None,
    resumable: bool = True,
    notes: str | None = None,
) -> dict[str, Any]:
    payload = load_registry(project)
    existing = dict(payload["extractors"].get(paper_id) or {})
    entry: dict[str, Any] = {
        **existing,
        "paper_id": paper_id,
        "resumable": bool(resumable),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    for key, value in (
        ("extractor_agent_id", extractor_agent_id),
        ("parent_session_id", parent_session_id),
        ("schema_version", schema_version),
        ("schema_path", schema_path),
        ("staging_dir", staging_dir),
        ("staging_script_path", staging_script_path),
        ("finalized_jsonl_path", finalized_jsonl_path),
        ("notes", notes),
    ):
        if value is not None:
            entry[key] = value

    entry.setdefault("staging_script_path", infer_staging_script(project, paper_id))
    entry.setdefault("finalized_jsonl_path", infer_final_jsonl(project, paper_id))
    payload["extractors"][paper_id] = entry
    save_registry(project, payload)
    return entry


def get_session(project: str | Path, paper_id: str) -> dict[str, Any] | None:
    payload = load_registry(project)
    entry = payload["extractors"].get(paper_id)
    return dict(entry) if isinstance(entry, dict) else None


def hydrate_manifest(project: str | Path, paper_id: str) -> Path | None:
    """Copy registry metadata into an existing build_manifest.json if present."""

    entry = get_session(project, paper_id)
    manifest_path = Path(project) / "extractions" / ".staging" / paper_id / "build_manifest.json"
    if entry is None or not manifest_path.exists():
        return None
    with manifest_path.open() as f:
        manifest = json.load(f)
    if not isinstance(manifest, dict):
        raise ValueError(f"build manifest top-level must be an object: {manifest_path}")
    manifest["extractor_session"] = entry
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--project",
        required=True,
        help="Project directory, e.g. projects/<domain>",
    )

    upsert = sub.add_parser("upsert", parents=[common])
    upsert.add_argument("--paper-id", required=True)
    upsert.add_argument("--extractor-agent-id")
    upsert.add_argument("--parent-session-id")
    upsert.add_argument("--schema-version")
    upsert.add_argument("--schema-path")
    upsert.add_argument("--staging-dir")
    upsert.add_argument("--staging-script-path")
    upsert.add_argument("--finalized-jsonl-path")
    upsert.add_argument("--notes")
    upsert.add_argument("--not-resumable", action="store_true")

    get = sub.add_parser("get", parents=[common])
    get.add_argument("--paper-id", required=True)

    hydrate = sub.add_parser("hydrate-manifest", parents=[common])
    hydrate.add_argument("--paper-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "upsert":
        entry = upsert_session(
            args.project,
            paper_id=args.paper_id,
            extractor_agent_id=args.extractor_agent_id,
            parent_session_id=args.parent_session_id,
            schema_version=args.schema_version,
            schema_path=args.schema_path,
            staging_dir=args.staging_dir,
            staging_script_path=args.staging_script_path,
            finalized_jsonl_path=args.finalized_jsonl_path,
            resumable=not args.not_resumable,
            notes=args.notes,
        )
        hydrate_manifest(args.project, args.paper_id)
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return 0
    if args.command == "get":
        entry = get_session(args.project, args.paper_id)
        if entry is None:
            return 1
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return 0
    if args.command == "hydrate-manifest":
        path = hydrate_manifest(args.project, args.paper_id)
        if path is None:
            return 1
        print(path)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
