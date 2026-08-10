#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML>=6.0.2"]
# ///
"""Emit normalizer-owned reconciliation hints for stale normalization entries.

This script is deliberately conservative: it identifies entries that deserve
LLM review after schema/guidance changes, but it never decides that a mapping is
wrong and never mutates extraction or normalization files unless asked to write
the candidate JSON artifact.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CATCHALL_IDS = {
    "other",
    "other_stratification_group",
    "unclear",
    "unknown",
    "unspecified",
}

TERM_RE = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b")


@dataclass(frozen=True)
class SchemaInfo:
    path: Path
    version: str
    fields: dict[str, dict[str, Any]]
    tables: dict[str, list[str]]


def load_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def load_schema(path: Path) -> SchemaInfo:
    data = yaml.safe_load(path.read_text()) or {}
    fields = {
        field["name"]: field
        for field in data.get("fields", [])
        if isinstance(field, dict) and field.get("name")
    }
    tables: dict[str, list[str]] = {}
    for name, field in fields.items():
        table = field.get("normalization")
        if isinstance(table, str) and table.strip():
            tables.setdefault(table, []).append(name)
    return SchemaInfo(
        path=path,
        version=str(data.get("schema_version") or path.stem),
        fields=fields,
        tables=tables,
    )


def schema_paths(schema_dir: Path, current_schema: str | None) -> tuple[Path, Path | None]:
    paths = sorted(schema_dir.glob("v*.yaml"))
    if not paths:
        raise SystemExit(f"No schema v*.yaml files found under {schema_dir}")
    if current_schema:
        current = schema_dir / current_schema
        if not current.exists():
            raise SystemExit(f"Requested schema does not exist: {current}")
    else:
        current = paths[-1]
    previous_candidates = [p for p in paths if p != current and p.name < current.name]
    previous = previous_candidates[-1] if previous_candidates else None
    return current, previous


def comparable_field(field: dict[str, Any]) -> dict[str, Any]:
    """Return the schema parts that should make normalization entries stale."""
    keep = {
        "name",
        "type",
        "normalization",
        "extraction_guidance",
        "allowed_values",
        "last_modified_iteration",
        "removal_rationale",
    }
    return {k: field.get(k) for k in keep if k in field}


def extract_terms(field: dict[str, Any]) -> set[str]:
    text_parts: list[str] = []
    for key in ("extraction_guidance", "description"):
        value = field.get(key)
        if isinstance(value, str):
            text_parts.append(value)
    value = field.get("allowed_values")
    if isinstance(value, list):
        text_parts.extend(str(v) for v in value)
    return set(TERM_RE.findall("\n".join(text_parts)))


def changed_normalization_tables(current: SchemaInfo, previous: SchemaInfo | None) -> dict[str, Any]:
    affected: dict[str, dict[str, Any]] = {}
    if previous is None:
        for table, fields in current.tables.items():
            affected[table] = {
                "fields": fields,
                "reasons": ["no_previous_schema_available"],
                "new_terms": sorted(
                    set().union(*(extract_terms(current.fields[f]) for f in fields))
                ),
            }
        return affected

    for table, fields in current.tables.items():
        reasons: set[str] = set()
        changed_fields: list[str] = []
        new_terms: set[str] = set()
        for field_name in fields:
            cur_field = current.fields[field_name]
            prev_field = previous.fields.get(field_name)
            if prev_field is None:
                reasons.add("normalizable_field_added")
                changed_fields.append(field_name)
                new_terms.update(extract_terms(cur_field))
                continue
            if comparable_field(cur_field) != comparable_field(prev_field):
                reasons.add("normalizable_field_changed_since_previous_schema")
                changed_fields.append(field_name)
                new_terms.update(extract_terms(cur_field) - extract_terms(prev_field))

        previous_fields_for_table = set(previous.tables.get(table, []))
        if previous_fields_for_table and set(fields) != previous_fields_for_table:
            reasons.add("normalizable_table_field_set_changed")

        if reasons:
            affected[table] = {
                "fields": sorted(changed_fields or fields),
                "reasons": sorted(reasons),
                "new_terms": sorted(new_terms),
            }
    return affected


def entry_last_touched(entry: dict[str, Any]) -> int | None:
    for key in ("iteration_updated", "iteration_added"):
        value = entry.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def candidate_for_entry(
    *,
    table: str,
    table_data: dict[str, Any],
    entry: dict[str, Any],
    iteration: int,
    affected_tables: dict[str, Any],
    confidence_threshold: float,
    catchall_ids: set[str],
) -> dict[str, Any] | None:
    reasons: list[str] = []
    risk_reasons: list[str] = []
    canonical_id = entry.get("canonical_id")
    confidence = entry.get("confidence")
    affected = affected_tables.get(table)

    if canonical_id is None:
        risk_reasons.append("canonical_id_null")
    if isinstance(confidence, (int, float)) and confidence < confidence_threshold:
        risk_reasons.append(f"confidence_below_{confidence_threshold:g}")
    if isinstance(canonical_id, str) and canonical_id in catchall_ids:
        risk_reasons.append("canonical_id_is_catchall")

    reasons.extend(risk_reasons)

    last_touched = entry_last_touched(entry)
    # A changed schema table supplies context for review, but does not by
    # itself make every old high-confidence entry a review candidate. That
    # would make the hint artifact noisy and push the agent toward token-burn
    # instead of semantic triage.
    if affected and risk_reasons:
        reasons.extend(affected.get("reasons", []))
        table_iter = table_data.get("last_updated_iteration")
        if isinstance(table_iter, int) and table_iter < iteration:
            reasons.append("table_last_updated_before_current_iteration_for_changed_table")
        if last_touched is not None and last_touched < iteration:
            reasons.append("entry_older_than_current_iteration_for_changed_table")

    if not reasons:
        return None

    return {
        "table": table,
        "raw": entry.get("raw"),
        "canonical_id": canonical_id,
        "confidence": confidence,
        "source": entry.get("source"),
        "iteration_added": entry.get("iteration_added"),
        "iteration_updated": entry.get("iteration_updated"),
        "reasons": sorted(set(reasons)),
        "schema_hint_terms": (affected or {}).get("new_terms", []),
        "instruction": (
            "Review this entry semantically under the current schema; "
            "the reasons and schema_hint_terms are hints, not a verdict."
        ),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    project = Path(args.project)
    schema_dir = project / "schema"
    normalization_dir = project / "normalization"
    current_path, previous_path = schema_paths(schema_dir, args.schema)
    current = load_schema(current_path)
    previous = load_schema(previous_path) if previous_path else None
    affected = changed_normalization_tables(current, previous)

    catchall_ids = set(DEFAULT_CATCHALL_IDS)
    catchall_ids.update(args.catchall_id or [])

    table_reports: dict[str, Any] = {}
    primary_candidates: list[dict[str, Any]] = []
    appendix_candidates: list[dict[str, Any]] = []
    missing_tables: list[dict[str, Any]] = []

    for table, fields in sorted(current.tables.items()):
        table_path = normalization_dir / f"{table}.json"
        table_reports[table] = {
            "fields": sorted(fields),
            "path": str(table_path),
            "exists": table_path.exists(),
            "affected_by_schema_change": table in affected,
        }
        if not table_path.exists():
            issue = {
                "table": table,
                "fields": sorted(fields),
                "reasons": ["normalization_table_missing"],
                "schema_hint_terms": affected.get(table, {}).get("new_terms", []),
                "instruction": "Create or review this table during normalization.",
            }
            missing_tables.append(issue)
            primary_candidates.append(issue)
            continue
        data = load_json(table_path)
        entries = data.get("entries", []) if isinstance(data, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            candidate = candidate_for_entry(
                table=table,
                table_data=data,
                entry=entry,
                iteration=args.iteration,
                affected_tables=affected,
                confidence_threshold=args.confidence_threshold,
                catchall_ids=catchall_ids,
            )
            if candidate:
                if table in affected:
                    primary_candidates.append(candidate)
                else:
                    appendix_candidates.append(candidate)

    candidates = (
        primary_candidates + appendix_candidates
        if args.include_appendix_in_candidates
        else primary_candidates
    )

    return {
        "project": str(project),
        "iteration": args.iteration,
        "current_schema": {
            "version": current.version,
            "path": str(current.path),
        },
        "previous_schema": (
            {"version": previous.version, "path": str(previous.path)}
            if previous
            else None
        ),
        "normalizable_tables": table_reports,
        "affected_tables": affected,
        "summary": {
            "candidate_count": len(candidates),
            "primary_candidate_count": len(primary_candidates),
            "appendix_candidate_count": len(appendix_candidates),
            "missing_table_count": len(missing_tables),
            "affected_table_count": len(affected),
            "confidence_threshold": args.confidence_threshold,
            "catchall_ids": sorted(catchall_ids),
        },
        "candidates": candidates,
        "primary_candidates": primary_candidates,
        "appendix_candidates": appendix_candidates,
    }


def default_output_path(project: Path, iteration: int) -> Path:
    return project / "normalization" / f"reconciliation_candidates_iter_{iteration}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Project directory, e.g. <project-root>")
    parser.add_argument("--iteration", type=int, required=True, help="Current inner-loop iteration")
    parser.add_argument("--schema", default=None, help="Current schema filename, defaults to latest v*.yaml")
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.9,
        help="Entries below this confidence become review candidates",
    )
    parser.add_argument(
        "--catchall-id",
        action="append",
        default=[],
        help="Additional canonical_id value to treat as catch-all/risky",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path. Defaults to normalization/reconciliation_candidates_iter_N.json. Use '-' for stdout only.",
    )
    parser.add_argument(
        "--include-appendix-in-candidates",
        action="store_true",
        help=(
            "Also include low-confidence/null/catch-all entries from schema-unaffected "
            "tables in the main candidates list. By default these are emitted only "
            "under appendix_candidates."
        ),
    )
    args = parser.parse_args()

    report = build_report(args)
    output_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        print(output_text, end="")
        return 0

    output_path = Path(args.output) if args.output else default_output_path(Path(args.project), args.iteration)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_text)
    print(f"wrote {output_path}")
    print(
        "candidates={candidate_count} affected_tables={affected_table_count} missing_tables={missing_table_count}".format(
            **report["summary"]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
