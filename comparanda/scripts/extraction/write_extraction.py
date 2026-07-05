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
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make the validator importable whether this script is invoked directly
# (uv run --script) or imported as a module.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_validator as ev  # noqa: E402
import extraction_core as core  # noqa: E402


def _die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def _read_json(path: Path) -> Any:
    try:
        with path.open() as f:
            return json.load(f)
    except FileNotFoundError:
        _die(f"file not found: {path}")
    except json.JSONDecodeError as e:
        _die(f"invalid JSON in {path}: {e}")


def _read_rows_files(paths: list[str]) -> dict:
    """Load one or more rows-file payloads and merge their `rows` arrays.

    Each file must have the full top-level shape (schema_version,
    identity_dimensions, rows). schema_version and identity_dimensions
    must match across files. Optional top-level keys (prior_schema_version,
    removed_fields, removed_rows) may appear on any file and are merged
    (single value or unioned list). The merged `rows` array preserves
    argument order, then within-file order.
    """
    merged: dict = {}
    all_removed_fields: list = []
    all_removed_rows: list = []
    for i, p in enumerate(paths):
        payload = _read_json(Path(p))
        if not isinstance(payload, dict):
            _die(f"rows-file {p!r} top-level must be a JSON object")
        if i == 0:
            merged["schema_version"] = payload.get("schema_version")
            merged["identity_dimensions"] = payload.get("identity_dimensions")
            if "prior_schema_version" in payload:
                merged["prior_schema_version"] = payload["prior_schema_version"]
            merged["rows"] = []
        else:
            if payload.get("schema_version") != merged.get("schema_version"):
                _die(
                    f"rows-file {p!r} schema_version={payload.get('schema_version')!r} "
                    f"does not match first file's "
                    f"schema_version={merged.get('schema_version')!r}"
                )
            if payload.get("identity_dimensions") != merged.get("identity_dimensions"):
                _die(
                    f"rows-file {p!r} identity_dimensions does not match first file"
                )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            _die(f"rows-file {p!r} must have a 'rows' list")
        merged["rows"].extend(rows)
        if isinstance(payload.get("removed_fields"), list):
            all_removed_fields.extend(payload["removed_fields"])
        if isinstance(payload.get("removed_rows"), list):
            all_removed_rows.extend(payload["removed_rows"])
    if all_removed_fields:
        # Dedup while preserving order
        seen: set = set()
        merged["removed_fields"] = [
            f for f in all_removed_fields if not (f in seen or seen.add(f))
        ]
    if all_removed_rows:
        merged["removed_rows"] = all_removed_rows
    return merged


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                _die(f"invalid JSON at {path}:{ln}: {e}")
    return rows


def _atomic_write_jsonl(out_path: Path, rows: list[dict], meta: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=str(out_path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        os.replace(tmp_name, out_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ────────────────────────────────────────────────────────────────────
# Array-form → on-disk nested-dict transform
# ────────────────────────────────────────────────────────────────────


def _fields_to_disk_row(
    fields_list: list[dict], schema: ev.Schema, *, fill_nulls: bool
) -> dict:
    """Convert array-of-{name,value,type,sources,derivation?} into the
    on-disk shape: flat row keys + `provenance` dict.

    fill_nulls=True (iter1): schema-declared fields not mentioned get
    written as null on disk so downstream readers see the full row shape.
    fill_nulls=False (itern partial merge): only mentioned fields are
    returned; caller merges into the prior row."""
    row: dict[str, Any] = {}
    provenance: dict[str, dict] = {}
    for entry in fields_list:
        name = entry["name"]
        value = entry["value"]
        row[name] = value
        if name in ev.STRUCTURAL_EXEMPT_FIELDS:
            continue
        if value is None:
            # review note: an explicit abstention carries provenance even though
            # the value is null, so "couldn't read" is distinguishable on the
            # row from "study didn't report" (plain null, no provenance).
            if entry.get("extraction_certainty") == "abstained":
                abst: dict[str, Any] = {
                    "extraction_certainty": "abstained",
                    "sources": entry.get("sources", []),
                }
                if entry.get("reasoning"):
                    abst["reasoning"] = entry["reasoning"]
                provenance[name] = abst
            continue
        prov_entry: dict[str, Any] = {
            "type": entry["type"],
            "sources": entry["sources"],
        }
        if entry.get("derivation"):
            prov_entry["derivation"] = entry["derivation"]
        if entry.get("derivation_kind"):
            prov_entry["derivation_kind"] = entry["derivation_kind"]
        if entry.get("extraction_certainty"):
            prov_entry["extraction_certainty"] = entry["extraction_certainty"]
        if entry.get("reasoning"):  # review note
            prov_entry["reasoning"] = entry["reasoning"]
        provenance[name] = prov_entry

    if fill_nulls:
        for fname in schema.field_names:
            if fname not in row:
                row[fname] = None

    if provenance:
        row["provenance"] = provenance
    return row


def _identity_of_disk_row(row: dict, schema: ev.Schema) -> tuple[Any, ...]:
    return tuple(row.get(d) for d in schema.identity_dimensions)


def _identity_of_input_row(
    row_entry: dict, schema: ev.Schema
) -> tuple[Any, ...] | None:
    """Compute the identity tuple for a staging-file row entry. Prefers
    the explicit 'identity' key (itern merge target); falls back to
    extracting identity-dim values from 'fields' (new row)."""
    if isinstance(row_entry.get("identity"), list):
        return tuple(row_entry["identity"])
    by_name: dict[str, Any] = {
        f["name"]: f.get("value")
        for f in row_entry.get("fields", [])
        if isinstance(f, dict) and isinstance(f.get("name"), str)
    }
    if all(d in by_name for d in schema.identity_dimensions):
        return tuple(by_name[d] for d in schema.identity_dimensions)
    return None


def _merge_partial_into_prior(
    prior_row: dict, partial_fields: list[dict]
) -> dict:
    """Overwrite each partial field's value and provenance entry on the
    prior row. Pass-through fields are untouched. Null-valued partial
    entries drop the field's prior provenance entry — UNLESS the partial
    entry is an explicit abstention, in which case an abstention provenance
    entry is written (review note)."""
    merged = dict(prior_row)
    prov = dict(merged.get("provenance") or {})
    for entry in partial_fields:
        name = entry["name"]
        value = entry["value"]
        # review note: a human correction is durable. If the prior provenance for
        # this field was human-verified, re-extraction must NOT clobber it.
        prior_entry = prov.get(name)
        if isinstance(prior_entry, dict) and prior_entry.get("reviewed_by"):
            continue
        merged[name] = value
        if value is None:
            # review note: an iter-n abstention (null + extraction_certainty
            # 'abstained') must preserve its abstention provenance, not be
            # silently rewritten as "the paper does not report this". A plain
            # null (no abstention) still pops.
            if entry.get("extraction_certainty") == "abstained":
                abst: dict[str, Any] = {
                    "extraction_certainty": "abstained",
                    "sources": entry.get("sources", []),
                }
                if entry.get("reasoning"):
                    abst["reasoning"] = entry["reasoning"]
                prov[name] = abst
            else:
                prov.pop(name, None)
        elif name not in ev.STRUCTURAL_EXEMPT_FIELDS:
            prov_entry: dict[str, Any] = {
                "type": entry["type"],
                "sources": entry["sources"],
            }
            if entry.get("derivation"):
                prov_entry["derivation"] = entry["derivation"]
            # review note: mirror the iter-1 path — carry the confidence tags
            # and reasoning so certainty/abstention vocabulary survives merge.
            if entry.get("derivation_kind"):
                prov_entry["derivation_kind"] = entry["derivation_kind"]
            if entry.get("extraction_certainty"):
                prov_entry["extraction_certainty"] = entry["extraction_certainty"]
            if entry.get("reasoning"):
                prov_entry["reasoning"] = entry["reasoning"]
            prov[name] = prov_entry
    if prov:
        merged["provenance"] = prov
    elif "provenance" in merged:
        del merged["provenance"]
    return merged


# ────────────────────────────────────────────────────────────────────
# Meta assembly
# ────────────────────────────────────────────────────────────────────


def _assemble_meta(
    *,
    agent_meta: dict,
    schema_version: str,
    actual_rows: int,
    iteration: int | None = None,
    prior_path: Path | None = None,
    rows_passed_through: int | None = None,
    rows_modified: int | None = None,
    rows_added: int | None = None,
    rows_removed: int | None = None,
) -> dict:
    meta: dict[str, Any] = {"_meta_commentary": True}
    meta.update(agent_meta)
    meta["schema_version"] = schema_version
    meta["actual_rows"] = actual_rows
    idv = agent_meta.get("identity_dimension_values")
    if isinstance(idv, dict) and idv:
        product = 1
        for vals in idv.values():
            if isinstance(vals, list):
                product *= max(1, len(vals))
        meta["cartesian_product_size"] = product
    meta["generated_at"] = datetime.now(timezone.utc).isoformat()
    if iteration is not None:
        meta["iteration"] = iteration
    if prior_path is not None:
        meta["prior_iteration_extraction"] = str(prior_path)
    for key, val in (
        ("rows_passed_through", rows_passed_through),
        ("rows_modified", rows_modified),
        ("rows_added", rows_added),
        ("rows_removed", rows_removed),
    ):
        if val is not None:
            meta[key] = val
    return meta


# ────────────────────────────────────────────────────────────────────
# Subcommand handlers
# ────────────────────────────────────────────────────────────────────


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
