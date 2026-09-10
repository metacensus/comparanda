#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Materialize a Comparanda extraction JSONL from validated staging files.

Two subcommands matching extractor.md's two write modes:

  iter1   Fresh extraction. Writes a new JSONL from --rows-file + --meta-file.
  itern   Iterative update. Merges --rows-file's touched-fields-only entries
          into --prior, applies --rows-file's removed_fields / removed_rows,
          and writes the result.

The script validates the staging files via extraction_validator.py before
touching the output (defense in depth — the PreToolUse hook is the primary
gate). It then transforms the array-of-field-objects input shape into the
on-disk nested-dict shape (flat row keys + `provenance: {field: {...}}`).

Bookkeeping fields in `_meta_commentary` (actual_rows, schema_version,
rows_passed_through, etc.) are filled here from script-known state — the
agent's meta-file MUST NOT include them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the validator importable whether this script is invoked directly
# (uv run --script) or imported as a module.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_core as core  # noqa: E402


def cmd_iter1(args: argparse.Namespace) -> int:
    try:
        result = core.finalize_iter1(
            schema_path=args.schema,
            bundle_dir=args.bundle,
            out_path=args.out,
            rows_files=args.rows_file,
            meta_file=args.meta_file,
        )
    except core.ExtractionValidationError as e:
        print(str(e), file=sys.stderr)
        return 3
    except core.ExtractionCoreError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(result.success_marker(), file=sys.stderr)
    return 0


def cmd_itern(args: argparse.Namespace) -> int:
    try:
        result = core.finalize_itern(
            schema_path=args.schema,
            bundle_dir=args.bundle,
            out_path=args.out,
            rows_files=args.rows_file,
            meta_file=args.meta_file,
            prior_path=args.prior,
        )
    except core.ExtractionValidationError as e:
        print(str(e), file=sys.stderr)
        return 3
    except core.ExtractionCoreError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(result.success_marker(), file=sys.stderr)
    return 0


# ────────────────────────────────────────────────────────────────────
# argparse
# ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="write_extraction.py",
        description="Write a validated Comparanda extraction JSONL.",
    )
    sub = p.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--paper-id", required=True)
    common.add_argument("--schema", required=True)
    common.add_argument("--bundle", required=True)
    common.add_argument("--out", required=True)
    common.add_argument(
        "--rows-file", required=True, action="append", dest="rows_file",
        help="Path to a rows payload JSON. May be repeated; the script "
             "concatenates the `rows` arrays in argument order. "
             "schema_version and identity_dimensions must match across files.",
    )
    common.add_argument("--meta-file", required=True)

    p_iter1 = sub.add_parser("iter1", parents=[common], help="fresh extraction")
    p_iter1.set_defaults(handler=cmd_iter1)

    p_itern = sub.add_parser("itern", parents=[common], help="diff-based update")
    p_itern.add_argument("--prior", required=True)
    p_itern.set_defaults(handler=cmd_itern)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
