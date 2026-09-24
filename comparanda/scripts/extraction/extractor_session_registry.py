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


def _upsert_session_lineage(entry: dict[str, Any], session: dict[str, Any]) -> None:
    agent_id = session.get("agent_id")
    if not agent_id:
        return
    sessions = entry.setdefault("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
        entry["sessions"] = sessions

    clean_session = {k: v for k, v in session.items() if v is not None}
    for idx, existing in enumerate(sessions):
        if isinstance(existing, dict) and existing.get("agent_id") == agent_id:
            sessions[idx] = {**existing, **clean_session}
            return
    sessions.append(clean_session)


def _append_resume_attempt(
    entry: dict[str, Any],
    *,
    target_agent_id: str,
    status: str,
    error: str | None = None,
    local_transcript_path: str | None = None,
    local_meta_path: str | None = None,
    notes: str | None = None,
) -> None:
    attempts = entry.setdefault("resume_attempts", [])
    if not isinstance(attempts, list):
        attempts = []
        entry["resume_attempts"] = attempts
    attempts.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target_agent_id": target_agent_id,
        "status": status,
        "error": error,
        "local_transcript_path": local_transcript_path,
        "local_meta_path": local_meta_path,
        "notes": notes,
    })


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
    tool_use_id: str | None = None,
    local_transcript_path: str | None = None,
    output_file: str | None = None,
    session_role: str = "extractor",
    resume_status: str | None = None,
    resumable: bool = True,
    notes: str | None = None,
) -> dict[str, Any]:
    payload = load_registry(project)
    existing = dict(payload["extractors"].get(paper_id) or {})
    previous_agent_id = (
        existing.get("current_agent_id")
        or existing.get("extractor_agent_id")
        or existing.get("original_agent_id")
    )
    entry: dict[str, Any] = {
        **existing,
        "paper_id": paper_id,
        "resumable": bool(resumable),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if extractor_agent_id:
        entry.setdefault("original_agent_id", previous_agent_id or extractor_agent_id)
        entry["current_agent_id"] = extractor_agent_id
        # Backward-compatible alias for existing orchestrator/docs/tests.
        entry["extractor_agent_id"] = extractor_agent_id
    elif previous_agent_id:
        entry.setdefault("original_agent_id", previous_agent_id)
        entry.setdefault("current_agent_id", previous_agent_id)
        entry.setdefault("extractor_agent_id", entry["current_agent_id"])

    for key, value in (
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
    if extractor_agent_id:
        _upsert_session_lineage(
            entry,
            {
                "agent_id": extractor_agent_id,
                "role": session_role,
                "tool_use_id": tool_use_id,
                "parent_session_id": parent_session_id,
                "output_file": output_file,
                "local_transcript_path": local_transcript_path,
                "schema_version": schema_version,
                "schema_path": schema_path,
                "staging_script_path": entry.get("staging_script_path"),
                "finalized_jsonl_path": entry.get("finalized_jsonl_path"),
                "resume_status": resume_status or ("resumable" if resumable else "not_resumable"),
                "updated_at": entry["updated_at"],
            },
        )
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
        help="Project directory, e.g. <project-root>",
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
    upsert.add_argument("--tool-use-id")
    upsert.add_argument("--local-transcript-path")
    upsert.add_argument("--output-file")
    upsert.add_argument("--session-role", default="extractor")
    upsert.add_argument("--resume-status")
    upsert.add_argument("--notes")
    upsert.add_argument("--not-resumable", action="store_true")

    attempt = sub.add_parser("record-resume-attempt", parents=[common])
    attempt.add_argument("--paper-id", required=True)
    attempt.add_argument("--target-agent-id", required=True)
    attempt.add_argument("--status", required=True)
    attempt.add_argument("--error")
    attempt.add_argument("--local-transcript-path")
    attempt.add_argument("--local-meta-path")
    attempt.add_argument("--notes")

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
            tool_use_id=args.tool_use_id,
            local_transcript_path=args.local_transcript_path,
            output_file=args.output_file,
            session_role=args.session_role,
            resume_status=args.resume_status,
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
    if args.command == "record-resume-attempt":
        payload = load_registry(args.project)
        entry = dict(payload["extractors"].get(args.paper_id) or {"paper_id": args.paper_id})
        entry.setdefault("staging_script_path", infer_staging_script(args.project, args.paper_id))
        entry.setdefault("finalized_jsonl_path", infer_final_jsonl(args.project, args.paper_id))
        _append_resume_attempt(
            entry,
            target_agent_id=args.target_agent_id,
            status=args.status,
            error=args.error,
            local_transcript_path=args.local_transcript_path,
            local_meta_path=args.local_meta_path,
            notes=args.notes,
        )
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        payload["extractors"][args.paper_id] = entry
        save_registry(args.project, payload)
        hydrate_manifest(args.project, args.paper_id)
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
