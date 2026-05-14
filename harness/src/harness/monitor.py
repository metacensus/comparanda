#!/usr/bin/env python3
"""
Monitor Comparanda inner loop execution.

Watches for phase transitions and reports events with session/subagent IDs
so System 2 can investigate behavior alignment.

Usage:
    uv run python -m harness.monitor \
        --project-dir ../comparanda/projects/dpyd \
        --session-dir ~/.claude/projects/<encoded-project-dir>

Prints timestamped events to stdout as phases complete. System 2 reads these
events and decides when/what to investigate in the session and subagent logs.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


def timestamp():
    return datetime.now().strftime("%H:%M:%S")


def find_latest_session(session_dir: Path) -> Path | None:
    """Find the most recently modified session JSONL file."""
    sessions = list(session_dir.glob("*.jsonl"))
    if not sessions:
        return None
    return max(sessions, key=lambda p: p.stat().st_mtime)


def scan_subagents(session_dir: Path, session_id: str) -> list[dict]:
    """Read subagent metadata for a session."""
    subagent_dir = session_dir / session_id / "subagents"
    if not subagent_dir.exists():
        return []

    agents = []
    for meta_file in sorted(subagent_dir.glob("*.meta.json")):
        try:
            with open(meta_file) as f:
                meta = json.load(f)
            agent_id = meta_file.stem.replace(".meta", "")
            agents.append({
                "id": agent_id,
                "type": meta.get("agentType", "unknown"),
                "description": meta.get("description", ""),
                "log": str(subagent_dir / f"{agent_id}.jsonl"),
            })
        except (json.JSONDecodeError, OSError):
            pass
    return agents


def scan_extractions(extractions_dir: Path) -> dict[str, int]:
    """Return {filename: line_count} for all extraction JSONL files."""
    results = {}
    for f in extractions_dir.glob("*.jsonl"):
        try:
            results[f.name] = sum(1 for _ in open(f))
        except OSError:
            results[f.name] = -1
    return results


def scan_normalization(norm_dir: Path) -> dict[str, str]:
    """Return {filename: mtime} for normalization tables."""
    results = {}
    for f in norm_dir.glob("*.json"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%H:%M:%S")
            results[f.name] = mtime
        except OSError:
            pass
    return results


def scan_critiques(critiques_dir: Path) -> list[str]:
    """Return list of critique filenames."""
    return [f.name for f in sorted(critiques_dir.glob("*.json"))]


def scan_schema(schema_dir: Path) -> list[str]:
    """Return list of schema version files."""
    return sorted([f.name for f in schema_dir.glob("v*.yaml")])


_output_file = None


def set_output_file(path: str | None):
    """Set output file for events. None means stdout."""
    global _output_file
    _output_file = path


def emit(event_type: str, message: str, details: dict | None = None):
    """Write a structured event line to output file or stdout."""
    line = f"[{timestamp()}] {event_type}: {message}"
    detail_lines = []
    if details:
        for k, v in details.items():
            detail_lines.append(f"  {k}: {v}")

    if _output_file:
        with open(_output_file, "a") as f:
            f.write(line + "\n")
            for dl in detail_lines:
                f.write(dl + "\n")
    else:
        print(line, flush=True)
        for dl in detail_lines:
            print(dl, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Monitor Comparanda inner loop")
    parser.add_argument("--project-dir", required=True, help="Path to comparanda project")
    parser.add_argument("--session-dir", required=True, help="Path to claude session storage")
    parser.add_argument("--interval", type=int, default=15, help="Poll interval in seconds")
    parser.add_argument("--output", default=None, help="Write events to this file (default: stdout). System 2 reads this file to check progress.")
    args = parser.parse_args()

    project = Path(args.project_dir)
    session_dir = Path(args.session_dir).expanduser()
    interval = args.interval

    if args.output:
        set_output_file(args.output)
        # Clear the file at start
        with open(args.output, "w") as f:
            pass

    extractions_dir = project / "extractions"
    norm_dir = project / "normalization"
    critiques_dir = project / "critiques"
    schema_dir = project / "schema"

    # Initial state
    known_extractions: set[str] = set()
    known_norm: set[str] = set()
    known_critiques: set[str] = set()
    known_schemas: set[str] = set(scan_schema(schema_dir))
    known_subagents: set[str] = set()
    current_session_id: str | None = None
    session_reported = False

    emit("MONITOR", "Started", {
        "project": str(project),
        "session_dir": str(session_dir),
        "interval": f"{interval}s",
        "initial_schemas": ", ".join(known_schemas),
    })

    try:
        while True:
            # --- Session tracking ---
            latest = find_latest_session(session_dir)
            if latest:
                sid = latest.stem
                if sid != current_session_id:
                    current_session_id = sid
                    session_reported = False

                if not session_reported:
                    size_kb = latest.stat().st_size / 1024
                    emit("SESSION", f"{sid}", {
                        "log": str(latest),
                        "size": f"{size_kb:.1f}KB",
                    })
                    session_reported = True

            # --- Subagent tracking ---
            if current_session_id:
                agents = scan_subagents(session_dir, current_session_id)
                for agent in agents:
                    if agent["id"] not in known_subagents:
                        known_subagents.add(agent["id"])
                        emit("SUBAGENT", f'{agent["id"]} spawned', {
                            "type": agent["type"],
                            "description": agent["description"],
                            "log": agent["log"],
                        })

            # --- Extraction tracking ---
            current_extractions = scan_extractions(extractions_dir)
            for name, lines in current_extractions.items():
                if name not in known_extractions:
                    known_extractions.add(name)
                    emit("EXTRACTION", f"{name} created ({lines} lines)")

            # --- Normalization tracking ---
            current_norm = scan_normalization(norm_dir)
            for name, mtime in current_norm.items():
                if name not in known_norm:
                    known_norm.add(name)
                    emit("NORMALIZATION", f"{name} created/updated at {mtime}")

            # --- Critique tracking ---
            current_critiques = scan_critiques(critiques_dir)
            for name in current_critiques:
                if name not in known_critiques:
                    known_critiques.add(name)
                    emit("CRITIQUE", f"{name} created")

            # --- Schema tracking ---
            current_schemas = set(scan_schema(schema_dir))
            new_schemas = current_schemas - known_schemas
            for name in new_schemas:
                known_schemas.add(name)
                emit("SCHEMA", f"{name} created (new version)")

            time.sleep(interval)

    except KeyboardInterrupt:
        emit("MONITOR", "Stopped")


if __name__ == "__main__":
    main()
