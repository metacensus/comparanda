#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Run an extractor-generated build script through a stable wrapper.

Extractor agents naturally create per-paper generator scripts under
``<project-root>/extractions/.staging/<paper_id>/scripts/build.py``.  This
wrapper gives those scripts a blessed invocation: repository-root cwd, import
paths for both ``scripts.extraction.extraction_sdk`` and ``extraction_sdk``,
predictable environment variables, and optional finalization through
``scripts/extraction/extraction_builder.py finalize``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _latest_schema(project: Path) -> Path:
    schema_dir = project / "schema"
    candidates = sorted(schema_dir.glob("v*.yaml"))
    if not candidates:
        raise FileNotFoundError(f"no schema v*.yaml files under {schema_dir}")
    return candidates[-1]


def _staging_dir(project: Path, paper_id: str) -> Path:
    return project / "extractions" / ".staging" / paper_id


def _row_count(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    return len(rows) if isinstance(rows, list) else None


def _print_staging_summary(staging: Path) -> None:
    expanded = staging / "expanded" / "rows.expanded.json"
    legacy = staging / "rows.json"
    rows_path = expanded if expanded.exists() else legacy
    count = _row_count(rows_path)
    if count is None:
        print(f"[run_extraction_build] no staged rows found under {staging}", file=sys.stderr)
    else:
        print(
            f"[run_extraction_build] staged_rows={count} rows_path={rows_path}",
            file=sys.stderr,
        )

    validation = staging / "validation" / "final.validate.json"
    if validation.exists():
        try:
            report = json.loads(validation.read_text())
            print(
                "[run_extraction_build] "
                f"final_validation_ok={report.get('ok')} "
                f"failure_count={report.get('failure_count')}",
                file=sys.stderr,
            )
        except json.JSONDecodeError:
            print(f"[run_extraction_build] validation report is not JSON: {validation}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a Comparanda extraction build script with blessed paths.",
    )
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--script", required=True, help="Path to build.py")
    parser.add_argument(
        "--project",
        default=os.environ.get("COMPARANDA_PROJECT_DIR"),
        help=(
            "Project directory, e.g. <project-root>. Defaults to "
            "COMPARANDA_PROJECT_DIR when set."
        ),
    )
    parser.add_argument("--schema")
    parser.add_argument("--bundle")
    parser.add_argument("--staging-dir")
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Finalize staged rows through scripts/extraction/extraction_builder.py after the build script exits.",
    )
    parser.add_argument(
        "--out",
        help="Final JSONL path when --finalize is used. Defaults to <project>/extractions/<paper_id>.jsonl.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.project:
        print(
            "missing --project (or COMPARANDA_PROJECT_DIR) for extraction build",
            file=sys.stderr,
        )
        return 2

    project = Path(args.project)
    schema = Path(args.schema) if args.schema else _latest_schema(project)
    bundle = Path(args.bundle) if args.bundle else project / "data" / "processed" / args.paper_id
    staging = Path(args.staging_dir) if args.staging_dir else _staging_dir(project, args.paper_id)
    script = Path(args.script)
    out = Path(args.out) if args.out else project / "extractions" / f"{args.paper_id}.jsonl"

    if not script.exists():
        print(f"build script not found: {script}", file=sys.stderr)
        return 2
    if not schema.exists():
        print(f"schema not found: {schema}", file=sys.stderr)
        return 2
    if not bundle.exists():
        print(f"bundle not found: {bundle}", file=sys.stderr)
        return 2

    env = os.environ.copy()
    pythonpath_parts = [
        str(REPO_ROOT),
        str(REPO_ROOT / "scripts"),
        str(REPO_ROOT / "scripts" / "extraction"),
    ]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["COMPARANDA_PROJECT_DIR"] = str(project)
    env["COMPARANDA_PAPER_ID"] = args.paper_id
    env["COMPARANDA_SCHEMA_PATH"] = str(schema)
    env["COMPARANDA_BUNDLE_DIR"] = str(bundle)
    env["COMPARANDA_STAGING_DIR"] = str(staging)

    print(
        "[run_extraction_build] "
        f"paper_id={args.paper_id} schema={schema} bundle={bundle} script={script}",
        file=sys.stderr,
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(f"[run_extraction_build] build script failed with exit={proc.returncode}", file=sys.stderr)
        return proc.returncode

    if args.finalize:
        finalize_cmd = [
            sys.executable,
            "scripts/extraction/extraction_builder.py",
            "finalize",
            "--project",
            str(project),
            "--paper-id",
            args.paper_id,
            "--schema",
            str(schema),
            "--bundle",
            str(bundle),
            "--staging-dir",
            str(staging),
            "--out",
            str(out),
        ]
        finalize = subprocess.run(
            finalize_cmd,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            check=False,
        )
        if finalize.returncode != 0:
            print(
                f"[run_extraction_build] finalization failed with exit={finalize.returncode}",
                file=sys.stderr,
            )
            return finalize.returncode

    _print_staging_summary(staging)
    print("[run_extraction_build] ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
