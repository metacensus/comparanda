#!/usr/bin/env python3
"""Shared extraction validation/finalization core.

This module is the common implementation behind the legacy
``write_extraction.py`` CLI and the newer Extraction Builder SDK.  It preserves
the existing on-disk JSONL contract while making the finalization path
importable for tests and builder workflows.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_validator as ev


class ExtractionCoreError(RuntimeError):
    """Base exception for extraction core failures."""


class ExtractionValidationError(ExtractionCoreError):
    """Raised when staged rows/meta fail validation."""

    def __init__(self, failures: list[ev.Failure]):
        self.failures = failures
        super().__init__(ev.render_failures(failures))


@dataclass
class FinalizationResult:
    out_path: Path
    row_count: int
    schema_version: str
    mode: str
    rows_passed_through: int | None = None
    rows_modified: int | None = None
    rows_added: int | None = None
    rows_removed: int | None = None

    def success_marker(self) -> str:
        if self.mode == "iter1":
            return (
                f"[write_extraction iter1] wrote {self.row_count} rows to "
                f"{self.out_path} (schema {self.schema_version})"
            )
        return (
            f"[write_extraction itern] wrote {self.row_count} rows to "
            f"{self.out_path} (passed={self.rows_passed_through} "
            f"modified={self.rows_modified} added={self.rows_added} "
            f"removed={self.rows_removed}, schema {self.schema_version})"
        )


def read_json(path: Path | str) -> Any:
    path = Path(path)
    with path.open() as f:
        return json.load(f)


def read_rows_files(paths: list[str | Path]) -> dict:
    """Load one or more legacy rows payloads and concatenate their rows."""

    merged: dict[str, Any] = {}
    all_removed_fields: list[Any] = []
    all_removed_rows: list[Any] = []
    for i, raw_path in enumerate(paths):
        path = Path(raw_path)
        payload = read_json(path)
        if not isinstance(payload, dict):
            raise ExtractionCoreError(f"rows-file {path!s} top-level must be an object")
        if i == 0:
            merged["schema_version"] = payload.get("schema_version")
            merged["identity_dimensions"] = payload.get("identity_dimensions")
            if "prior_schema_version" in payload:
                merged["prior_schema_version"] = payload["prior_schema_version"]
            merged["rows"] = []
        else:
            if payload.get("schema_version") != merged.get("schema_version"):
                raise ExtractionCoreError(
                    f"rows-file {path!s} schema_version={payload.get('schema_version')!r} "
                    f"does not match first file's schema_version={merged.get('schema_version')!r}"
                )
            if payload.get("identity_dimensions") != merged.get("identity_dimensions"):
                raise ExtractionCoreError(
                    f"rows-file {path!s} identity_dimensions does not match first file"
                )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ExtractionCoreError(f"rows-file {path!s} must have a 'rows' list")
        merged["rows"].extend(rows)
        if isinstance(payload.get("removed_fields"), list):
            all_removed_fields.extend(payload["removed_fields"])
        if isinstance(payload.get("removed_rows"), list):
            all_removed_rows.extend(payload["removed_rows"])

    if all_removed_fields:
        seen: set[Any] = set()
        merged["removed_fields"] = [
            f for f in all_removed_fields if not (f in seen or seen.add(f))
        ]
    if all_removed_rows:
        merged["removed_rows"] = all_removed_rows
    return merged


def read_jsonl(path: Path | str) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open() as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ExtractionCoreError(f"invalid JSON at {path}:{ln}: {e}") from e
    return rows


def atomic_write_jsonl(out_path: Path | str, rows: list[dict], meta: dict) -> None:
    out_path = Path(out_path)
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


def missing_provenance(entry: dict) -> dict:
    return {
        "missing_reason": entry["missing_reason"],
        "reasoning": entry["reasoning"],
        "sources": entry.get("sources", []),
    }


def write_validation_report(out_path: Path | str, payload: dict, schema: ev.Schema, failures: list[ev.Failure]) -> None:
    out_path = Path(out_path)
    folder = out_path.parent / ".staging" / out_path.stem / "validation"
    folder.mkdir(parents=True, exist_ok=True)
    report = ev.failure_report(failures, context={"paper_id": out_path.stem, "mode": "final"})
    report.update(ev.missingness_report(payload, schema))
    (folder / "final.validate.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    text = ev.render_failures(failures) if failures else "ok\n"
    (folder / "final.validate.txt").write_text(text + "\n" + ev.render_missingness_summary(report))
    if report["warning_count"]:
        print(ev.render_missingness_summary(report), file=sys.stderr, end="")


def fields_to_disk_row(
    fields_list: list[dict], schema: ev.Schema, *, fill_nulls: bool
) -> dict:
    row: dict[str, Any] = {}
    provenance: dict[str, dict] = {}
    for entry in fields_list:
        name = entry["name"]
        value = entry["value"]
        row[name] = value
        if name in ev.STRUCTURAL_EXEMPT_FIELDS:
            continue
        if value is None:
            if name not in schema.exempt_fields:
                provenance[name] = missing_provenance(entry)
            continue
        prov_entry: dict[str, Any] = {
            "type": entry["type"],
            "sources": entry["sources"],
        }
        if entry.get("derivation"):
            prov_entry["derivation"] = entry["derivation"]
        if entry.get("source_components"):
            prov_entry["source_components"] = entry["source_components"]
        if entry.get("derivation_kind"):
            prov_entry["derivation_kind"] = entry["derivation_kind"]
        if entry.get("extraction_certainty"):
            prov_entry["extraction_certainty"] = entry["extraction_certainty"]
        if entry.get("reasoning"):
            prov_entry["reasoning"] = entry["reasoning"]
        provenance[name] = prov_entry

    if fill_nulls:
        for fname in schema.field_names:
            if fname not in row:
                row[fname] = None
    if provenance:
        row["provenance"] = provenance
    return row


def identity_of_disk_row(row: dict, schema: ev.Schema) -> tuple[Any, ...]:
    return tuple(row.get(d) for d in schema.identity_dimensions)


def identity_of_input_row(row_entry: dict, schema: ev.Schema) -> tuple[Any, ...] | None:
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


def merge_partial_into_prior(prior_row: dict, partial_fields: list[dict]) -> dict:
    merged = dict(prior_row)
    prov = dict(merged.get("provenance") or {})
    for entry in partial_fields:
        name = entry["name"]
        value = entry["value"]
        prior_entry = prov.get(name)
        if isinstance(prior_entry, dict) and prior_entry.get("reviewed_by"):
            continue
        merged[name] = value
        if value is None:
            if "missing_reason" in entry:
                prov[name] = missing_provenance(entry)
            else:
                # The validator only permits unannotated canonical placeholders.
                prov.pop(name, None)
        elif name not in ev.STRUCTURAL_EXEMPT_FIELDS:
            prov_entry: dict[str, Any] = {
                "type": entry["type"],
                "sources": entry["sources"],
            }
            if entry.get("derivation"):
                prov_entry["derivation"] = entry["derivation"]
            if entry.get("source_components"):
                prov_entry["source_components"] = entry["source_components"]
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


def assemble_meta(
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


def validate_iter1(
    *,
    schema_path: Path | str,
    bundle_dir: Path | str,
    rows_payload: dict,
    meta_payload: dict,
) -> list[ev.Failure]:
    schema = ev.Schema.load(schema_path)
    bundle = ev.BundleSources.load(Path(bundle_dir))
    if not bundle.full_text:
        return [ev.Failure("empty_bundle", "bundle", f"bundle at {bundle_dir} produced no text")]
    failures = ev.validate_rows_payload(
        rows_payload, schema=schema, bundle=bundle, mode="iter1"
    )
    failures += ev.validate_meta_payload(meta_payload, mode="iter1")
    return failures


def finalize_iter1(
    *,
    schema_path: Path | str,
    bundle_dir: Path | str,
    out_path: Path | str,
    rows_files: list[str | Path],
    meta_file: Path | str,
) -> FinalizationResult:
    schema = ev.Schema.load(schema_path)
    bundle = ev.BundleSources.load(Path(bundle_dir))
    if not bundle.full_text:
        raise ExtractionCoreError(f"bundle at {bundle_dir} produced no text")

    rows_payload = read_rows_files(rows_files)
    meta_payload = read_json(meta_file)
    failures = ev.validate_rows_payload(
        rows_payload, schema=schema, bundle=bundle, mode="iter1"
    )
    failures += ev.validate_meta_payload(meta_payload, mode="iter1")
    write_validation_report(out_path, rows_payload, schema, failures)
    if failures:
        raise ExtractionValidationError(failures)

    out_rows = [
        fields_to_disk_row(r["fields"], schema, fill_nulls=True)
        for r in rows_payload["rows"]
    ]
    meta = assemble_meta(
        agent_meta=meta_payload,
        schema_version=schema.version,
        actual_rows=len(out_rows),
    )
    atomic_write_jsonl(out_path, out_rows, meta)
    return FinalizationResult(
        out_path=Path(out_path),
        row_count=len(out_rows),
        schema_version=schema.version,
        mode="iter1",
    )


def finalize_itern(
    *,
    schema_path: Path | str,
    bundle_dir: Path | str,
    out_path: Path | str,
    rows_files: list[str | Path],
    meta_file: Path | str,
    prior_path: Path | str,
) -> FinalizationResult:
    schema = ev.Schema.load(schema_path)
    bundle = ev.BundleSources.load(Path(bundle_dir))
    if not bundle.full_text:
        raise ExtractionCoreError(f"bundle at {bundle_dir} produced no text")

    prior_path = Path(prior_path)
    prior_lines = read_jsonl(prior_path)
    prior_rows = [r for r in prior_lines if not r.get("_meta_commentary")]
    prior_meta = next((r for r in prior_lines if r.get("_meta_commentary")), None)

    rows_payload = read_rows_files(rows_files)
    meta_payload = read_json(meta_file)
    failures: list[ev.Failure] = []

    declared_prior = rows_payload.get("prior_schema_version")
    if declared_prior is not None and prior_meta is not None:
        actual_prior = prior_meta.get("schema_version")
        if declared_prior != actual_prior:
            failures.append(ev.Failure(
                "prior_schema_version_mismatch",
                "rows-file",
                f"rows.prior_schema_version={declared_prior!r} but prior "
                f"JSONL declares schema_version={actual_prior!r}",
            ))

    failures += ev.validate_prior_quotes(prior_rows, bundle=bundle)
    failures += ev.validate_rows_payload(
        rows_payload, schema=schema, bundle=bundle, mode="itern"
    )
    failures += ev.validate_meta_payload(meta_payload, mode="itern")
    write_validation_report(out_path, rows_payload, schema, failures)
    if failures:
        raise ExtractionValidationError(failures)

    prior_by_identity: dict[tuple, dict] = {}
    duplicate_identities: dict[tuple, int] = {}
    for r in prior_rows:
        key = identity_of_disk_row(r, schema)
        if key in prior_by_identity:
            duplicate_identities[key] = duplicate_identities.get(key, 1) + 1
            continue
        prior_by_identity[key] = r
    if duplicate_identities:
        lines = [
            f"  {count}x identity {key}"
            for key, count in duplicate_identities.items()
        ]
        raise ExtractionCoreError(
            "prior extraction has rows with duplicate identity tuples; "
            "itern merge would silently drop data. Duplicates:\n" + "\n".join(lines)
        )

    new_rows = rows_payload["rows"]
    removed_field_names = set(rows_payload.get("removed_fields") or [])
    removed_identities = {
        tuple(r["identity"]) for r in (rows_payload.get("removed_rows") or [])
    }

    rows_added = 0
    rows_modified = 0
    rows_passed_through = 0
    out_by_identity: dict[tuple, dict] = {}

    for key, prior_row in prior_by_identity.items():
        if key in removed_identities:
            continue
        out_by_identity[key] = prior_row
        rows_passed_through += 1

    for entry in new_rows:
        identity = identity_of_input_row(entry, schema)
        if identity is None:
            raise ExtractionCoreError(f"itern row missing identity: {entry}")
        has_explicit_identity_key = isinstance(entry.get("identity"), list)
        if identity in out_by_identity and has_explicit_identity_key:
            out_by_identity[identity] = merge_partial_into_prior(
                out_by_identity[identity], entry["fields"]
            )
            rows_modified += 1
            rows_passed_through -= 1
        elif identity in out_by_identity and not has_explicit_identity_key:
            raise ExtractionCoreError(
                f"itern new-row identity {identity!r} collides with an existing "
                "prior row; use the 'identity' key to mark a modification."
            )
        else:
            out_by_identity[identity] = fields_to_disk_row(
                entry["fields"], schema, fill_nulls=True
            )
            rows_added += 1

    if removed_field_names:
        for key, row in list(out_by_identity.items()):
            updated = dict(row)
            prov = dict(updated.get("provenance") or {})
            touched = False
            for fname in removed_field_names:
                if fname in updated:
                    del updated[fname]
                    touched = True
                if fname in prov:
                    del prov[fname]
                    touched = True
            if touched:
                if prov:
                    updated["provenance"] = prov
                else:
                    updated.pop("provenance", None)
                out_by_identity[key] = updated

    prior_iter = (prior_meta or {}).get("iteration")
    iteration = (prior_iter + 1) if isinstance(prior_iter, int) else 2
    out_rows = list(out_by_identity.values())
    rows_removed = len(removed_identities)

    meta = assemble_meta(
        agent_meta=meta_payload,
        schema_version=schema.version,
        actual_rows=len(out_rows),
        iteration=iteration,
        prior_path=prior_path,
        rows_passed_through=rows_passed_through,
        rows_modified=rows_modified,
        rows_added=rows_added,
        rows_removed=rows_removed,
    )
    atomic_write_jsonl(out_path, out_rows, meta)
    return FinalizationResult(
        out_path=Path(out_path),
        row_count=len(out_rows),
        schema_version=schema.version,
        mode="itern",
        rows_passed_through=rows_passed_through,
        rows_modified=rows_modified,
        rows_added=rows_added,
        rows_removed=rows_removed,
    )
