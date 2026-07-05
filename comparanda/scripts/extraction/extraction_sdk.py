"""Unified Extraction Builder SDK for Comparanda extractor agents.

The SDK writes the same validated staging shape accepted by
``write_extraction.py`` while giving agents safer primitives for evidence,
derived values, common-field inheritance, chunking, validation, and final
JSONL materialization.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extraction_core as core
import extraction_validator as ev


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _flatten_cell(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, (int, float)):
        return str(cell)
    if isinstance(cell, dict):
        for key in ("text", "value", "content"):
            if key in cell:
                return _flatten_cell(cell[key])
        return " ".join(_flatten_cell(v) for v in cell.values())
    if isinstance(cell, (list, tuple)):
        return " ".join(_flatten_cell(c) for c in cell)
    return str(cell)


@dataclass(frozen=True)
class EvidenceHandle:
    quote: str
    location: str
    page: int
    cell: dict[str, str] | None = None
    source_kind: str | None = None

    def as_source(self) -> dict[str, Any]:
        src: dict[str, Any] = {
            "quote": self.quote,
            "location": self.location,
            "page": self.page,
        }
        if self.cell is not None:
            src["cell"] = self.cell
        return src


class QuoteScope:
    def __init__(self, store: "EvidenceStore", location: str):
        self.store = store
        self.location = location

    def exact(self, text: str, *, page: int | None = None) -> EvidenceHandle:
        _key, loc_text = self.store.bundle.resolve_location(self.location)
        if loc_text is None:
            raise ValueError(f"location does not resolve to bundle file: {self.location}")
        if not ev.find_quote_in_source(text, loc_text):
            candidates = self.store.bundle.quote_locations(text)
            hint = f"; found elsewhere: {candidates}" if candidates else "; not found anywhere"
            raise ValueError(f"quote not found at {self.location}{hint}: {text[:160]!r}")
        return EvidenceHandle(
            quote=text,
            location=self.location,
            page=page if page is not None else self.store.page_for(self.location),
            source_kind=self.store.kind_for(self.location),
        )

    def find(self, text_or_regex: str, *, page: int | None = None) -> EvidenceHandle:
        _key, loc_text = self.store.bundle.resolve_location(self.location)
        if loc_text is None:
            raise ValueError(f"location does not resolve to bundle file: {self.location}")
        if ev.find_quote_in_source(text_or_regex, loc_text):
            return EvidenceHandle(
                quote=text_or_regex,
                location=self.location,
                page=page if page is not None else self.store.page_for(self.location),
                source_kind=self.store.kind_for(self.location),
            )
        match = re.search(text_or_regex, loc_text, re.IGNORECASE | re.DOTALL)
        if not match:
            candidates = self.store.bundle.quote_locations(text_or_regex)
            hint = f"; found elsewhere: {candidates}" if candidates else "; not found anywhere"
            raise ValueError(f"pattern not found at {self.location}{hint}: {text_or_regex[:160]!r}")
        return EvidenceHandle(
            quote=match.group(0),
            location=self.location,
            page=page if page is not None else self.store.page_for(self.location),
            source_kind=self.store.kind_for(self.location),
        )

    def slice_between(
        self,
        start_text: str,
        end_text: str,
        *,
        include_end: bool = True,
        page: int | None = None,
    ) -> EvidenceHandle:
        _key, loc_text = self.store.bundle.resolve_location(self.location)
        if loc_text is None:
            raise ValueError(f"location does not resolve to bundle file: {self.location}")
        start = loc_text.index(start_text)
        end = loc_text.index(end_text, start)
        if include_end:
            end += len(end_text)
        quote = loc_text[start:end]
        return EvidenceHandle(
            quote=quote,
            location=self.location,
            page=page if page is not None else self.store.page_for(self.location),
            source_kind=self.store.kind_for(self.location),
        )


class FigureScope:
    def __init__(self, store: "EvidenceStore", location: str):
        self.store = store
        self.location = location

    def label(self, text: str, *, page: int | None = None) -> EvidenceHandle:
        return EvidenceHandle(
            quote=text,
            location=self.location,
            page=page if page is not None else self.store.page_for(self.location),
            source_kind="figure",
        )

    def caption(self, *, page: int | None = None) -> EvidenceHandle:
        caption = self.store.figure_caption(self.location)
        if not caption:
            raise ValueError(f"no caption found for figure: {self.location}")
        return self.label(caption, page=page)


class EvidenceStore:
    def __init__(self, bundle_dir: Path | str):
        self.bundle_dir = Path(bundle_dir)
        self.bundle = ev.BundleSources.load(self.bundle_dir)
        self.manifest = self._load_manifest()
        self._page_by_location = self._build_page_index()

    def _load_manifest(self) -> dict[str, Any]:
        path = self.bundle_dir / "manifest.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}

    def _build_page_index(self) -> dict[str, int]:
        pages: dict[str, int] = {"manifest.json": 0}
        for section in self.manifest.get("sections") or []:
            path = section.get("path")
            page_range = section.get("page_range")
            if isinstance(path, str):
                if isinstance(page_range, list) and page_range:
                    pages[path] = int(page_range[0])
                elif isinstance(section.get("page"), int):
                    pages[path] = int(section["page"])
        for table in self.manifest.get("tables") or []:
            page = table.get("page")
            for key in ("path", "markdown_path"):
                path = table.get(key)
                if isinstance(path, str) and isinstance(page, int):
                    pages[path] = page
        for fig in self.manifest.get("figures") or []:
            path = fig.get("path")
            page = fig.get("page")
            if isinstance(path, str) and isinstance(page, int):
                pages[path] = page
        return pages

    def page_for(self, location: str) -> int:
        if location in self._page_by_location:
            return self._page_by_location[location]
        match = re.search(r"(sections|tables|figures)/[\w.\-]+", location)
        if match and match.group(0) in self._page_by_location:
            return self._page_by_location[match.group(0)]
        return 0

    def kind_for(self, location: str) -> str:
        if location.startswith("sections/"):
            return "section"
        if location.startswith("tables/"):
            return "table"
        if location.startswith("figures/"):
            return "figure"
        if location == "manifest.json":
            return "metadata"
        return "unknown"

    def quote(self, location: str) -> QuoteScope:
        return QuoteScope(self, location)

    def find_quote(self, text: str) -> EvidenceHandle:
        locations = self.bundle.quote_locations(text, limit=1)
        if not locations:
            raise ValueError(f"quote not found anywhere in bundle: {text[:160]!r}")
        return self.quote(locations[0]).exact(text)

    def table_row(self, table_path: str, *, row: str, page: int | None = None) -> EvidenceHandle:
        matched, text = self.bundle.resolve_location(table_path)
        if text is None:
            raise ValueError(f"table location not found: {table_path}")
        for line in text.splitlines():
            if ev.find_quote_in_source(row, line):
                return EvidenceHandle(
                    quote=line.strip(),
                    location=table_path,
                    page=page if page is not None else self.page_for(table_path),
                    source_kind="table",
                )
        raise ValueError(f"row label {row!r} not found in {matched or table_path}")

    def table_cell(
        self,
        table_path: str,
        *,
        row: str,
        column: str | None = None,
        page: int | None = None,
    ) -> EvidenceHandle:
        row_handle = self.table_row(table_path, row=row, page=page)
        if column is None:
            return row_handle
        return EvidenceHandle(
            quote=row_handle.quote,
            location=table_path,
            page=row_handle.page,
            cell={"row": row, "column": column},
            source_kind="table",
        )

    def figure(self, location: str) -> FigureScope:
        return FigureScope(self, location)

    def figure_caption(self, location: str) -> str | None:
        for fig in self.manifest.get("figures") or []:
            if fig.get("path") == location and isinstance(fig.get("caption"), str):
                return fig["caption"]
        return None

    def metadata(self, key: str, *, page: int = 0) -> EvidenceHandle:
        value = self.manifest.get(key)
        if value is None:
            raise ValueError(f"manifest metadata key not found: {key}")
        return EvidenceHandle(
            quote=str(value),
            location="manifest.json",
            page=page,
            source_kind="metadata",
        )


@dataclass
class SourceComponent:
    label: str
    value: Any
    sources: list[dict[str, Any]]
    role: str | None = None

    def to_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "label": self.label,
            "value": self.value,
            "sources": self.sources,
        }
        if self.role:
            entry["role"] = self.role
        return entry


@dataclass
class FieldClaim:
    value: Any
    type: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    derivation: str | None = None
    derivation_kind: str | None = None
    extraction_certainty: str | None = None
    reasoning: str | None = None
    source_components: list[dict[str, Any]] = field(default_factory=list)

    def to_entry(self, name: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"name": name, "value": self.value}
        if self.value is None and self.extraction_certainty != "abstained":
            return entry
        if self.type is not None:
            entry["type"] = self.type
        if self.sources:
            entry["sources"] = self.sources
        if self.derivation:
            entry["derivation"] = self.derivation
        if self.source_components:
            entry["source_components"] = self.source_components
        if self.derivation_kind:
            entry["derivation_kind"] = self.derivation_kind
        if self.extraction_certainty:
            entry["extraction_certainty"] = self.extraction_certainty
        if self.reasoning:
            entry["reasoning"] = self.reasoning
        return entry


def _source_dicts(sources: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for source in _as_list(sources):
        if isinstance(source, EvidenceHandle):
            out.append(source.as_source())
        elif isinstance(source, dict):
            out.append(dict(source))
        else:
            raise TypeError(f"unsupported source type: {type(source).__name__}")
    return out


def source_component(
    label: str,
    value: Any,
    sources: Any,
    *,
    role: str | None = None,
) -> SourceComponent:
    """Declare one evidence-backed component of a derived field.

    Validators check that each component value appears in its own cited source
    and, for arithmetic derivations, that every numeric operand in the formula
    is represented by a component.
    """
    return SourceComponent(
        label=label,
        value=value,
        sources=_source_dicts(sources),
        role=role,
    )


def _component_dicts(components: Any | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for component in _as_list(components):
        if isinstance(component, SourceComponent):
            out.append(component.to_entry())
        elif isinstance(component, dict):
            out.append(dict(component))
        else:
            raise TypeError(f"unsupported source component type: {type(component).__name__}")
    return out


def paper(
    value: Any,
    sources: Any,
    *,
    reasoning: str | None = None,
    derivation_kind: str = "verbatim",
    extraction_certainty: str = "clean",
) -> FieldClaim:
    return FieldClaim(
        value=value,
        type="paper",
        sources=_source_dicts(sources),
        derivation_kind=derivation_kind,
        extraction_certainty=extraction_certainty,
        reasoning=reasoning,
    )


def derived(
    value: Any,
    sources: Any,
    *,
    derivation: str,
    reasoning: str | None = None,
    source_components: Any | None = None,
    derivation_kind: str = "derived",
    extraction_certainty: str = "clean",
) -> FieldClaim:
    return FieldClaim(
        value=value,
        type="derived",
        sources=_source_dicts(sources),
        derivation=derivation,
        derivation_kind=derivation_kind,
        extraction_certainty=extraction_certainty,
        reasoning=reasoning,
        source_components=_component_dicts(source_components),
    )


def inferred(
    value: Any,
    sources: Any,
    *,
    reasoning: str,
    derivation: str | None = None,
    type: str = "paper",
    extraction_certainty: str = "ambiguous",
) -> FieldClaim:
    return FieldClaim(
        value=value,
        type=type,
        sources=_source_dicts(sources),
        derivation=derivation,
        derivation_kind="inferred",
        extraction_certainty=extraction_certainty,
        reasoning=reasoning,
    )


def abstained(reasoning: str, sources: Any | None = None) -> FieldClaim:
    return FieldClaim(
        value=None,
        sources=_source_dicts(sources),
        extraction_certainty="abstained",
        reasoning=reasoning,
    )


def null(reason: str | None = None) -> FieldClaim:
    return FieldClaim(value=None, reasoning=reason)


def not_applicable(reasoning: str, sources: Any, *, derivation: str | None = None) -> FieldClaim:
    return derived(
        "not_applicable",
        sources,
        derivation=derivation or "'not_applicable' is the schema sentinel for this field in the cited context.",
        reasoning=reasoning,
        derivation_kind="inferred",
    )


class ChunkBuilder:
    def __init__(self, builder: "ExtractionBuilder", name: str):
        self.builder = builder
        self.name = name
        self.rows: list[dict[str, FieldClaim | dict[str, Any] | Any]] = []

    def __enter__(self) -> "ChunkBuilder":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.builder._chunks[self.name] = self

    def add_row(self, fields: dict[str, FieldClaim | dict[str, Any] | Any]) -> None:
        self.rows.append(fields)

    def payload(self) -> dict[str, Any]:
        return self.builder._rows_payload_for_chunks([self.name])

    def validate(self) -> list[ev.Failure]:
        return self.builder.validate(chunks=[self.name])


class ExtractionBuilder:
    def __init__(
        self,
        *,
        project_dir: str | Path,
        paper_id: str,
        schema_path: str | Path,
        bundle_dir: str | Path,
        staging_dir: str | Path | None = None,
    ):
        self.project_dir = Path(project_dir)
        self.paper_id = paper_id
        self.schema_path = Path(schema_path)
        self.bundle_dir = Path(bundle_dir)
        self.schema = ev.Schema.load(self.schema_path)
        self.evidence = EvidenceStore(self.bundle_dir)
        self.staging_dir = (
            Path(staging_dir)
            if staging_dir is not None
            else self.project_dir / "extractions" / ".staging" / paper_id
        )
        self._common_fields: dict[str, FieldClaim | dict[str, Any] | Any] = {}
        self._chunks: dict[str, ChunkBuilder] = {}
        self._meta: dict[str, Any] = {
            "sources_used": [],
            "identity_dimension_values": {},
        }

    def common_fields(self, fields: dict[str, FieldClaim | dict[str, Any] | Any]) -> None:
        self._common_fields.update(fields)

    def chunk(self, name: str) -> ChunkBuilder:
        chunk = self._chunks.get(name)
        if chunk is None:
            chunk = ChunkBuilder(self, name)
            self._chunks[name] = chunk
        return chunk

    def meta(self, payload: dict[str, Any]) -> None:
        self._meta.update(payload)

    def copy_script(self, source: str | Path, *, name: str | None = None) -> Path:
        scripts_dir = self.staging_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        source = Path(source)
        dest = scripts_dir / (name or source.name)
        shutil.copy2(source, dest)
        return dest

    def _entry(self, name: str, claim: FieldClaim | dict[str, Any] | Any) -> dict[str, Any]:
        if isinstance(claim, FieldClaim):
            return claim.to_entry(name)
        if isinstance(claim, dict) and claim.get("name") == name:
            return dict(claim)
        if isinstance(claim, dict) and "value" in claim:
            entry = dict(claim)
            entry.setdefault("name", name)
            return entry
        return {"name": name, "value": claim}

    def _expanded_row(self, row_fields: dict[str, FieldClaim | dict[str, Any] | Any]) -> dict[str, Any]:
        merged = dict(self._common_fields)
        merged.update(row_fields)
        return {"fields": [self._entry(name, value) for name, value in merged.items()]}

    def _rows_payload_for_chunks(self, chunk_names: list[str] | None = None) -> dict[str, Any]:
        names = chunk_names or list(self._chunks.keys())
        rows: list[dict[str, Any]] = []
        for name in names:
            chunk = self._chunks[name]
            rows.extend(self._expanded_row(row) for row in chunk.rows)
        return {
            "schema_version": self.schema.version,
            "identity_dimensions": self.schema.identity_dimensions,
            "rows": rows,
        }

    def write_staging(self) -> tuple[Path, Path]:
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir = self.staging_dir / "chunks"
        validation_dir = self.staging_dir / "validation"
        expanded_dir = self.staging_dir / "expanded"
        chunks_dir.mkdir(exist_ok=True)
        validation_dir.mkdir(exist_ok=True)
        expanded_dir.mkdir(exist_ok=True)

        common_path = self.staging_dir / "common_fields.json"
        common_entries = [
            self._entry(name, value) for name, value in self._common_fields.items()
        ]
        common_path.write_text(json.dumps(common_entries, indent=2, ensure_ascii=False))

        chunk_manifest: list[dict[str, Any]] = []
        for index, (name, chunk) in enumerate(self._chunks.items()):
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or f"chunk_{index}"
            path = chunks_dir / f"{index:03d}_{safe_name}.rows.json"
            payload = self._rows_payload_for_chunks([name])
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
            chunk_manifest.append({
                "name": name,
                "path": str(path.relative_to(self.staging_dir)),
                "row_count": len(chunk.rows),
            })

        rows_path = expanded_dir / "rows.expanded.json"
        rows_path.write_text(
            json.dumps(self._rows_payload_for_chunks(), indent=2, ensure_ascii=False)
        )
        meta_path = self.staging_dir / "meta.json"
        meta_path.write_text(json.dumps(self._meta, indent=2, ensure_ascii=False))

        session = {
            "paper_id": self.paper_id,
            "schema_version": self.schema.version,
            "staging_dir": str(self.staging_dir),
            "staging_script_path": str(self.staging_dir / "scripts" / "build.py"),
        }
        for env_key, manifest_key in (
            ("COMPARANDA_EXTRACTOR_AGENT_ID", "extractor_agent_id"),
            ("COMPARANDA_PARENT_SESSION_ID", "parent_session_id"),
        ):
            value = os.environ.get(env_key)
            if value:
                session[manifest_key] = value

        manifest = {
            "paper_id": self.paper_id,
            "schema_path": str(self.schema_path),
            "schema_version": self.schema.version,
            "bundle_dir": str(self.bundle_dir),
            "common_fields_path": str(common_path.relative_to(self.staging_dir)),
            "expanded_rows_path": str(rows_path.relative_to(self.staging_dir)),
            "meta_path": str(meta_path.relative_to(self.staging_dir)),
            "staging_script_path": str(self.staging_dir / "scripts" / "build.py"),
            "extractor_session": session,
            "chunks": chunk_manifest,
        }
        (self.staging_dir / "build_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False)
        )
        return rows_path, meta_path

    def validate(
        self,
        *,
        chunks: list[str] | None = None,
        write_report: bool = True,
    ) -> list[ev.Failure]:
        payload = self._rows_payload_for_chunks(chunks)
        bundle = ev.BundleSources.load(self.bundle_dir)
        failures = ev.validate_rows_payload(
            payload, schema=self.schema, bundle=bundle, mode="iter1"
        )
        failures += ev.validate_meta_payload(self._meta, mode="iter1")
        if write_report:
            self._write_validation_report(failures, chunks=chunks)
        return failures

    def _write_validation_report(
        self,
        failures: list[ev.Failure],
        *,
        chunks: list[str] | None = None,
    ) -> None:
        validation_dir = self.staging_dir / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        label = "final" if chunks is None else "_".join(chunks)
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "chunk"
        report = ev.failure_report(failures)
        (validation_dir / f"{safe_label}.validate.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False)
        )
        text = ev.render_failures(failures) if failures else "ok\n"
        (validation_dir / f"{safe_label}.validate.txt").write_text(text)

    def finalize(self, out_path: str | Path) -> core.FinalizationResult:
        rows_path, meta_path = self.write_staging()
        result = core.finalize_iter1(
            schema_path=self.schema_path,
            bundle_dir=self.bundle_dir,
            out_path=out_path,
            rows_files=[rows_path],
            meta_file=meta_path,
        )
        self.validate(write_report=True)
        manifest_path = self.staging_dir / "build_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            manifest["finalized_jsonl_path"] = str(out_path)
            if isinstance(manifest.get("extractor_session"), dict):
                manifest["extractor_session"]["finalized_jsonl_path"] = str(out_path)
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        return result

    def chunk_names(self) -> Iterator[str]:
        return iter(self._chunks.keys())
