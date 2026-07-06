#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML>=6.0.2"]
# ///
"""Sanctioned write/apply/validate API for Comparanda normalization.

Normalizer agents should use this script for normalization-table decisions and
canonical-field propagation instead of writing ad hoc mutation scripts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


VERBOSE_TABLE_KEYS = {
    "previous_mappings",
    "reconciliation_reason",
    "reason_revised",
}

VALID_DISPOSITIONS = {
    "revise_mapping",
    "reuse_unchanged",
    "keep_disputed",
    "defer_with_reason",
}

COMPACT_REVIEW_KEYS = {
    "raw",
    "canonical_id",
    "confidence",
    "source",
    "iteration_added",
    "iteration_updated",
    "reviewed_schema_version",
    "status",
}


@dataclass(frozen=True)
class NormalizationPair:
    table: str
    raw_field: str
    canonical_field: str
    is_array: bool


def load_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def latest_schema_path(project: Path, schema: str | None = None) -> Path:
    schema_dir = project / "schema"
    if schema:
        path = schema_dir / schema
        if not path.exists():
            path = Path(schema)
        if not path.exists():
            raise SystemExit(f"schema not found: {schema}")
        return path
    paths = sorted(schema_dir.glob("v*.yaml"))
    if not paths:
        raise SystemExit(f"no schema v*.yaml files under {schema_dir}")
    return paths[-1]


def load_schema(project: Path, schema: str | None = None) -> tuple[Path, dict[str, Any]]:
    path = latest_schema_path(project, schema)
    return path, yaml.safe_load(path.read_text()) or {}


def schema_version(path: Path, schema_data: dict[str, Any]) -> str:
    return str(schema_data.get("schema_version") or path.stem)


def _is_canonical_field(field: dict[str, Any]) -> bool:
    guidance = str(field.get("extraction_guidance") or "").lower()
    name = str(field.get("name") or "")
    if "not populated by the extractor" in guidance:
        return True
    return name.endswith(("_id", "_ids", "_canonical", "_category"))


def discover_pairs(schema_data: dict[str, Any]) -> list[NormalizationPair]:
    fields = [
        f for f in schema_data.get("fields", [])
        if isinstance(f, dict) and f.get("name") and isinstance(f.get("normalization"), str)
    ]
    pairs: list[NormalizationPair] = []
    used_destinations: set[str] = set()
    for idx, field in enumerate(fields):
        raw_name = str(field["name"])
        if _is_canonical_field(field):
            continue
        table = str(field["normalization"])
        destination: dict[str, Any] | None = None
        for candidate in fields[idx + 1:]:
            if candidate.get("normalization") != table:
                continue
            candidate_name = str(candidate.get("name"))
            if candidate_name in used_destinations:
                continue
            if _is_canonical_field(candidate):
                destination = candidate
                break
            # Stop when the next source field for this table starts; otherwise
            # entity_raw could accidentally skip to entity_component_ids.
            break
        if destination is None:
            continue
        dest_name = str(destination["name"])
        used_destinations.add(dest_name)
        pairs.append(
            NormalizationPair(
                table=table,
                raw_field=raw_name,
                canonical_field=dest_name,
                is_array=field.get("type") == "array",
            )
        )
    return pairs


def table_path(project: Path, table: str) -> Path:
    return project / "normalization" / f"{table}.json"


def ledger_path(project: Path, iteration: int) -> Path:
    return project / "normalization" / "reconciliation" / f"decisions_iter_{iteration:03d}.jsonl"


def parse_canonical_id(value: str | None) -> Any:
    if value is None:
        return None
    if value == "__null__":
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def clean_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k not in VERBOSE_TABLE_KEYS}


def append_ledger(project: Path, iteration: int, decision: dict[str, Any]) -> Path:
    path = ledger_path(project, iteration)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(decision, sort_keys=True) + "\n")
    return path


def marker(message: str) -> None:
    print(f"[normalization_api] {message}", file=sys.stderr)


def cmd_reconcile(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    schema_path, schema_data = load_schema(project, args.schema)
    version = schema_version(schema_path, schema_data)
    if args.disposition not in VALID_DISPOSITIONS:
        raise SystemExit(f"unknown disposition: {args.disposition}")
    if not args.reason.strip():
        raise SystemExit("--reason is required and must be non-empty")

    path = table_path(project, args.table)
    if not path.exists():
        raise SystemExit(f"normalization table not found: {path}")
    data = load_json(path)
    entries = data.setdefault("entries", [])
    target = None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("raw") == args.raw:
            target = entry
            break
    if target is None:
        if not args.create:
            raise SystemExit(f"raw value not found in {path}: {args.raw!r}")
        target = {
            "raw": args.raw,
            "canonical_id": None,
            "confidence": None,
            "source": "llm",
            "iteration_added": args.iteration,
        }
        entries.append(target)

    before = dict(target)
    canonical_supplied = args.canonical_id is not None
    confidence_supplied = args.confidence is not None

    for key in VERBOSE_TABLE_KEYS:
        target.pop(key, None)

    if args.disposition == "revise_mapping":
        if not canonical_supplied:
            raise SystemExit("revise_mapping requires --canonical-id")
        if not confidence_supplied:
            raise SystemExit("revise_mapping requires --confidence")
        target["canonical_id"] = parse_canonical_id(args.canonical_id)
        target["confidence"] = args.confidence
        target["source"] = args.source
        target.setdefault("iteration_added", args.iteration)
        target["iteration_updated"] = args.iteration
        target["reviewed_schema_version"] = version
        target["status"] = "active" if target.get("canonical_id") is not None else "unresolved"
        data["last_updated_iteration"] = args.iteration
    else:
        if canonical_supplied or confidence_supplied:
            raise SystemExit(
                f"{args.disposition} must not change canonical_id/confidence; use revise_mapping"
            )
        target["reviewed_schema_version"] = version
        if args.disposition == "keep_disputed":
            target["status"] = "disputed"
        elif args.disposition == "defer_with_reason":
            target["status"] = "deferred"
        else:
            target.setdefault("status", "active" if target.get("canonical_id") is not None else "unresolved")

    for entry in entries:
        if isinstance(entry, dict):
            for key in VERBOSE_TABLE_KEYS:
                entry.pop(key, None)

    write_json(path, data)
    decision = {
        "iteration": args.iteration,
        "schema_version": version,
        "schema_path": str(schema_path),
        "table": args.table,
        "raw": args.raw,
        "disposition": args.disposition,
        "before": before,
        "after": dict(target),
        "reason": args.reason,
        "evidence": args.evidence or [],
        "affected_extraction_files": [],
        "validation": {"table_entry_contains_verbose_keys": False},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ledger = append_ledger(project, args.iteration, decision)
    marker(f"reconcile {args.disposition} {args.table}:{args.raw!r} -> {ledger}")
    print(json.dumps({"updated_table": str(path), "ledger": str(ledger), "decision": decision}, indent=2))
    return 0


def eligible_tables(args: argparse.Namespace) -> set[str] | None:
    return set(args.tables) if args.tables else None


def eligible_raws(args: argparse.Namespace) -> set[str] | None:
    return set(args.raws) if args.raws else None


def build_mapping(project: Path, table: str, threshold: float) -> dict[str, Any]:
    path = table_path(project, table)
    if not path.exists():
        return {}
    data = load_json(path)
    mapping: dict[str, Any] = {}
    for entry in data.get("entries", []):
        if not isinstance(entry, dict):
            continue
        raw = entry.get("raw")
        conf = entry.get("confidence")
        canonical_id = entry.get("canonical_id")
        if raw is None or canonical_id is None:
            continue
        if isinstance(conf, (int, float)) and conf >= threshold:
            mapping[str(raw)] = canonical_id
    return mapping


def map_value(value: Any, mapping: dict[str, Any], is_array: bool) -> tuple[Any, bool]:
    if is_array:
        if value is None:
            return value, False
        if not isinstance(value, list):
            return value, False
        changed = False
        out = []
        for item in value:
            mapped = mapping.get(str(item))
            if mapped is None:
                out.append(None)
            else:
                out.append(mapped)
                changed = True
        return out, changed
    mapped = mapping.get(str(value))
    if mapped is None:
        return None, False
    return mapped, True


def cmd_apply(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    if args.threshold < 0.9 and not args.allow_reviewed_medium:
        raise SystemExit(
            "--threshold below 0.9 requires --allow-reviewed-medium so medium-confidence "
            "application is explicit and auditable"
        )
    _, schema_data = load_schema(project, args.schema)
    table_filter = eligible_tables(args)
    raw_filter = eligible_raws(args)
    pairs = [
        p for p in discover_pairs(schema_data)
        if table_filter is None or p.table in table_filter
    ]
    mappings = {p.table: build_mapping(project, p.table, args.threshold) for p in pairs}
    if raw_filter is not None:
        mappings = {
            table: {raw: value for raw, value in mapping.items() if raw in raw_filter}
            for table, mapping in mappings.items()
        }

    extraction_dir = project / "extractions"
    changed_files: dict[str, int] = {}
    field_changes = 0
    for path in sorted(extraction_dir.glob("*.jsonl")):
        old_lines = path.read_text().splitlines()
        rows = [json.loads(line) for line in old_lines if line.strip()]
        row_changes = 0
        for row in rows:
            for pair in pairs:
                if pair.raw_field not in row:
                    continue
                new_value, has_mapping = map_value(row.get(pair.raw_field), mappings[pair.table], pair.is_array)
                if not has_mapping:
                    continue
                if row.get(pair.canonical_field) != new_value:
                    row[pair.canonical_field] = new_value
                    row_changes += 1
                    field_changes += 1
        if row_changes:
            changed_files[str(path)] = row_changes
            if not args.dry_run:
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    summary = {
        "dry_run": args.dry_run,
        "threshold": args.threshold,
        "allow_reviewed_medium": args.allow_reviewed_medium,
        "pairs": [pair.__dict__ for pair in pairs],
        "changed_files": changed_files,
        "field_changes": field_changes,
    }
    print(json.dumps(summary, indent=2))
    marker(f"apply field_changes={field_changes} dry_run={args.dry_run}")
    return 0


def git_root(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"not inside a git worktree: {path}")
    return Path(result.stdout.strip())


def git_show(root: Path, ref: str, path: Path) -> str | None:
    rel = path.relative_to(root)
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{rel}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def jsonl_rows(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def normalized_fields(schema_data: dict[str, Any]) -> set[str]:
    return {pair.canonical_field for pair in discover_pairs(schema_data)}


def validate_extractions(project: Path, schema_data: dict[str, Any], base_ref: str) -> list[str]:
    root = git_root(project)
    allowed = normalized_fields(schema_data)
    errors: list[str] = []
    for path in sorted((project / "extractions").glob("*.jsonl")):
        current_text = path.read_text()
        try:
            current_rows = jsonl_rows(current_text)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}: invalid JSONL: {exc}")
            continue
        base_text = git_show(root, base_ref, path)
        if base_text is None:
            continue
        try:
            base_rows = jsonl_rows(base_text)
        except json.JSONDecodeError:
            continue
        if len(base_rows) != len(current_rows):
            errors.append(f"{path}: row count changed {len(base_rows)} -> {len(current_rows)}")
            continue
        for idx, (before, after) in enumerate(zip(base_rows, current_rows)):
            changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
            raw_changed = sorted(k for k in changed if k.endswith("_raw"))
            if raw_changed:
                errors.append(f"{path}: row {idx} raw fields changed: {raw_changed}")
            unexpected = sorted(k for k in changed if k not in allowed)
            if unexpected:
                errors.append(f"{path}: row {idx} non-normalized fields changed: {unexpected}")
    return errors


def validate_tables(project: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted((project / "normalization").glob("*.json")):
        if path.name.startswith("reconciliation_candidates_"):
            continue
        try:
            data = load_json(path)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}: invalid JSON: {exc}")
            continue
        for idx, entry in enumerate(data.get("entries", [])):
            if not isinstance(entry, dict):
                continue
            extra = sorted(VERBOSE_TABLE_KEYS & set(entry))
            if extra:
                errors.append(f"{path}: entry {idx} contains verbose audit keys: {extra}")
    return errors


def validate_ledgers(project: Path) -> list[str]:
    errors: list[str] = []
    ledger_dir = project / "normalization" / "reconciliation"
    if not ledger_dir.exists():
        return errors
    for path in sorted(ledger_dir.glob("decisions_iter_*.jsonl")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{path}:{lineno}: invalid JSON: {exc}")
                continue
            if obj.get("disposition") not in VALID_DISPOSITIONS:
                errors.append(f"{path}:{lineno}: invalid disposition {obj.get('disposition')!r}")
            if not obj.get("reason"):
                errors.append(f"{path}:{lineno}: missing reason")
    return errors


def cmd_validate(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    _, schema_data = load_schema(project, args.schema)
    errors = []
    errors.extend(validate_extractions(project, schema_data, args.base_ref))
    errors.extend(validate_tables(project))
    errors.extend(validate_ledgers(project))
    result = {"ok": not errors, "errors": errors}
    print(json.dumps(result, indent=2))
    marker("validate ok" if not errors else f"validate failed errors={len(errors)}")
    return 0 if not errors else 1


def cmd_clean_audit_metadata(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    schema_path, schema_data = load_schema(project, args.schema)
    version = schema_version(schema_path, schema_data)
    cleaned: dict[str, int] = {}
    migrated = 0
    for path in sorted((project / "normalization").glob("*.json")):
        if path.name.startswith("reconciliation_candidates_"):
            continue
        data = load_json(path)
        changed = 0
        for entry in data.get("entries", []):
            if not isinstance(entry, dict):
                continue
            verbose_present = bool(VERBOSE_TABLE_KEYS & set(entry))
            if verbose_present and args.iteration and not args.dry_run:
                previous = entry.get("previous_mappings") or []
                before = None
                if isinstance(previous, list) and previous:
                    last = previous[-1]
                    if isinstance(last, dict):
                        before = {
                            "raw": entry.get("raw"),
                            "canonical_id": last.get("canonical_id"),
                            "confidence": last.get("confidence"),
                            "source": last.get("source"),
                            "iteration_added": last.get("iteration") or entry.get("iteration_added"),
                        }
                decision = {
                    "iteration": args.iteration,
                    "schema_version": version,
                    "schema_path": str(schema_path),
                    "table": data.get("table_name") or path.stem,
                    "raw": entry.get("raw"),
                    "disposition": "revise_mapping" if before else "reuse_unchanged",
                    "before": before or dict(entry),
                    "after": clean_entry(entry),
                    "reason": entry.get("reconciliation_reason")
                    or "Migrated verbose audit metadata out of normalization table.",
                    "evidence": [],
                    "affected_extraction_files": [],
                    "validation": {"migrated_from_table_verbose_metadata": True},
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                append_ledger(project, args.iteration, decision)
                migrated += 1
            for key in list(VERBOSE_TABLE_KEYS):
                if key in entry:
                    entry.pop(key, None)
                    changed += 1
        if changed:
            cleaned[str(path)] = changed
            if not args.dry_run:
                write_json(path, data)
    print(json.dumps({"dry_run": args.dry_run, "cleaned": cleaned, "migrated_decisions": migrated}, indent=2))
    marker(f"clean-audit-metadata tables={len(cleaned)} migrated={migrated} dry_run={args.dry_run}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("reconcile", help="Record/apply one reconciliation decision")
    rec.add_argument("--project", required=True)
    rec.add_argument("--iteration", type=int, required=True)
    rec.add_argument("--schema")
    rec.add_argument("--table", required=True)
    rec.add_argument("--raw", required=True)
    rec.add_argument("--disposition", required=True, choices=sorted(VALID_DISPOSITIONS))
    rec.add_argument("--canonical-id")
    rec.add_argument("--confidence", type=float)
    rec.add_argument("--source", default="llm")
    rec.add_argument("--reason", required=True)
    rec.add_argument("--evidence", action="append", default=[])
    rec.add_argument("--create", action="store_true")
    rec.set_defaults(func=cmd_reconcile)

    app = sub.add_parser("apply", help="Apply normalization mappings to extraction canonical fields")
    app.add_argument("--project", required=True)
    app.add_argument("--schema")
    app.add_argument("--threshold", type=float, default=0.9)
    app.add_argument("--tables", action="append", default=[])
    app.add_argument("--raws", "--raw", action="append", default=[])
    app.add_argument("--dry-run", action="store_true")
    app.add_argument(
        "--allow-reviewed-medium",
        action="store_true",
        help=(
            "Required when --threshold is below 0.9. Use only for explicitly "
            "reviewed medium-confidence reconciliation decisions."
        ),
    )
    app.set_defaults(func=cmd_apply)

    val = sub.add_parser("validate", help="Validate normalizer writes")
    val.add_argument("--project", required=True)
    val.add_argument("--schema")
    val.add_argument("--base-ref", default="HEAD")
    val.set_defaults(func=cmd_validate)

    clean = sub.add_parser("clean-audit-metadata", help="Strip verbose audit metadata from normalization tables")
    clean.add_argument("--project", required=True)
    clean.add_argument("--schema")
    clean.add_argument("--iteration", type=int, help="If supplied, migrate stripped audit metadata into this iteration's decision ledger.")
    clean.add_argument("--dry-run", action="store_true")
    clean.set_defaults(func=cmd_clean_audit_metadata)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
