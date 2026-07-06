#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Unified Extraction Builder CLI.

This CLI is the sanctioned command-line surface for validating and finalizing
SDK-built staged extractions.  It shares the same finalization core used by
write_extraction.py and preserves the legacy JSONL output contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_core as core  # noqa: E402
import extraction_validator as ev  # noqa: E402


def _read_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def _staging_dir(project: str, paper_id: str, staging_dir: str | None) -> Path:
    if staging_dir:
        return Path(staging_dir)
    return Path(project) / "extractions" / ".staging" / paper_id


def _rows_path(staging: Path) -> Path:
    expanded = staging / "expanded" / "rows.expanded.json"
    if expanded.exists():
        return expanded
    legacy = staging / "rows.json"
    if legacy.exists():
        return legacy
    raise FileNotFoundError(
        f"no rows payload found at {expanded} or legacy fallback {legacy}"
    )


def _meta_path(staging: Path) -> Path:
    path = staging / "meta.json"
    if not path.exists():
        raise FileNotFoundError(f"meta payload not found: {path}")
    return path


def _report_context(staging: Path, name: str, rows_path: Path | None = None) -> dict[str, str]:
    context = {
        "label": name,
        "report_path": str(staging / "validation" / f"{name}.validate.json"),
    }
    if rows_path is not None:
        context["rows_path"] = str(rows_path)
    return context


def _write_report(
    staging: Path,
    name: str,
    failures: list[ev.Failure],
    *,
    rows_path: Path | None = None,
) -> None:
    validation_dir = staging / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    context = _report_context(staging, name, rows_path)
    (validation_dir / f"{name}.validate.json").write_text(
        json.dumps(ev.failure_report(failures, context=context), indent=2, ensure_ascii=False)
    )
    text = ev.render_failures(failures, context=context) if failures else "ok\n"
    (validation_dir / f"{name}.validate.txt").write_text(text)


def cmd_init(args: argparse.Namespace) -> int:
    staging = _staging_dir(args.project, args.paper_id, args.staging_dir)
    for rel in ("chunks", "expanded", "validation", "scripts"):
        (staging / rel).mkdir(parents=True, exist_ok=True)
    manifest = {
        "paper_id": args.paper_id,
        "schema_path": args.schema,
        "bundle_dir": args.bundle,
        "expanded_rows_path": "expanded/rows.expanded.json",
        "meta_path": "meta.json",
        "chunks": [],
    }
    (staging / "build_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False)
    )
    print(f"[extraction_builder init] initialized {staging}", file=sys.stderr)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    staging = _staging_dir(args.project, args.paper_id, args.staging_dir)
    rows_path = _rows_path(staging)
    rows_payload = _read_json(rows_path)
    meta_payload = _read_json(_meta_path(staging))
    failures = core.validate_iter1(
        schema_path=args.schema,
        bundle_dir=args.bundle,
        rows_payload=rows_payload,
        meta_payload=meta_payload,
    )
    _write_report(staging, "final", failures, rows_path=rows_path)
    if failures:
        print(
            ev.render_failures(
                failures,
                context=_report_context(staging, "final", rows_path),
            ),
            file=sys.stderr,
        )
        return 3
    print(f"[extraction_builder validate] ok: {staging}", file=sys.stderr)
    return 0


def cmd_validate_chunk(args: argparse.Namespace) -> int:
    staging = _staging_dir(args.project, args.paper_id, args.staging_dir)
    matches = sorted((staging / "chunks").glob(f"*{args.chunk}*.rows.json"))
    if not matches:
        print(f"chunk rows file not found for {args.chunk!r} under {staging / 'chunks'}", file=sys.stderr)
        return 2
    rows_path = matches[0]
    rows_payload = _read_json(rows_path)
    meta_payload = _read_json(_meta_path(staging))
    failures = core.validate_iter1(
        schema_path=args.schema,
        bundle_dir=args.bundle,
        rows_payload=rows_payload,
        meta_payload=meta_payload,
    )
    _write_report(staging, args.chunk, failures, rows_path=rows_path)
    if failures:
        print(
            ev.render_failures(
                failures,
                context=_report_context(staging, args.chunk, rows_path),
            ),
            file=sys.stderr,
        )
        return 3
    print(f"[extraction_builder validate-chunk] ok: {matches[0]}", file=sys.stderr)
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    staging = _staging_dir(args.project, args.paper_id, args.staging_dir)
    rows = _rows_path(staging)
    meta = _meta_path(staging)
    try:
        result = core.finalize_iter1(
            schema_path=args.schema,
            bundle_dir=args.bundle,
            out_path=args.out,
            rows_files=[rows],
            meta_file=meta,
        )
    except core.ExtractionValidationError as e:
        _write_report(staging, "final", e.failures, rows_path=rows)
        print(str(e), file=sys.stderr)
        return 3
    except core.ExtractionCoreError as e:
        print(str(e), file=sys.stderr)
        return 2
    _write_report(staging, "final", [], rows_path=rows)
    print(result.success_marker(), file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extraction_builder.py",
        description="Validate/finalize SDK-built Comparanda extraction staging.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", required=True)
    common.add_argument("--paper-id", required=True)
    common.add_argument("--schema", required=True)
    common.add_argument("--bundle", required=True)
    common.add_argument("--staging-dir")

    p_init = sub.add_parser("init", parents=[common])
    p_init.set_defaults(handler=cmd_init)

    p_validate = sub.add_parser("validate", parents=[common])
    p_validate.set_defaults(handler=cmd_validate)

    p_chunk = sub.add_parser("validate-chunk", parents=[common])
    p_chunk.add_argument("--chunk", required=True)
    p_chunk.set_defaults(handler=cmd_validate_chunk)

    p_finalize = sub.add_parser("finalize", parents=[common])
    p_finalize.add_argument("--out", required=True)
    p_finalize.set_defaults(handler=cmd_finalize)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
