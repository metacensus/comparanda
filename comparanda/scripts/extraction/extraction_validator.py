"""Validator for Comparanda extraction writes.

Shared by:
  - scripts/extraction/write_extraction.py (backstop validation before writing JSONL)
  - .claude/hooks/validate_extraction_write.py (PreToolUse gate)

Validates the staging payloads (rows.json / meta.json / diff metadata) that
the extractor materializes before invoking write_extraction.py. The on-disk
JSONL is produced by the script from these payloads; the validator never
sees a half-written JSONL.

Provenance contract enforced here:
  - Every non-null field on every row has a well-formed provenance entry,
    except fields in the schema-derived exempt set:
      {provenance, _meta_commentary} ∪ {f : f.name endswith "_id" AND
                                         f.normalization is a non-empty str}
  - type ∈ {"paper", "derived"}. The pre-2026-05-27 "external" type is
    rejected (zero historical uses, no defined use case).
  - sources: non-empty list of {quote: str, location: str, page: int}.
  - For type == "derived", `derivation` is required and must be non-empty.
  - All quotes appear verbatim in the bundle (sections/*.md + flattened
    tables/*.json cell values + tables/*.md).

The validator returns a list of structured Failure objects; consumers
(script & hook) decide how to render them.
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MISSING_REASONS = frozenset({
    "not_found", "explicitly_unavailable", "referenced_source_unavailable",
    "definition_mismatch", "unreadable", "conflicting_evidence", "not_applicable",
})
MISSING_REASONS_REQUIRING_SOURCES = MISSING_REASONS - {"not_found", "not_applicable"}

ALLOWED_PROVENANCE_TYPES = frozenset({"paper", "derived"})
STRUCTURAL_EXEMPT_FIELDS = frozenset({"provenance", "_meta_commentary"})

# Per-field confidence/abstention, mechanically anchored (not
# self-reported). derivation_kind records HOW the value was obtained;
# extraction_certainty records HOW reliable the read was. These are optional
# on a field entry but VALIDATED when present, and `clean` is rejected when
# the source is uncorroborated/figure — so the tag cannot lie.
ALLOWED_DERIVATION_KINDS = frozenset({"verbatim", "derived", "inferred"})
ALLOWED_CERTAINTIES = frozenset({"clean", "ambiguous"})
# Table validation statuses (from paper-prep) that forbid a `clean`
# tag on a value sourced from that table.
_UNCORROBORATED_TABLE = frozenset({"single_source", "unresolved_conflict"})

# Quote locations matching this pattern are excluded from quote-verbatim
# checks: bundle text doesn't include figure interiors. This was tightened
# from the old loose `plot|chart|graph` (which let any quote be routed
# around the check via a creative location string) to anchored figure
# references only — a real figures/ bundle path or the words figure/forest
# plot. A value sourced only from a figure is necessarily `inferred`
# and cannot be marked `clean`.
FIGURE_LOCATION_PATTERN = re.compile(
    r"(^|/)figures/|\bfigure\b|forest[\s.-]?plot", re.IGNORECASE
)

# Fields the agent MUST NOT include in meta.json — these are bookkeeping
# the script fills deterministically. Catching them here prevents subtle
# drift between agent-declared and script-computed values.
META_BOOKKEEPING_FIELDS = frozenset({
    "_meta_commentary",
    "actual_rows",
    "cartesian_product_size",
    "schema_version",
    "iteration",
    "prior_iteration_extraction",
    "rows_passed_through",
    "rows_modified",
    "rows_added",
    "rows_removed",
})


@dataclass
class Failure:
    """Structured validation failure. `code` is the failure category;
    `location` is a human-readable row/field reference; `detail` is the
    failure-specific message."""

    code: str
    location: str
    detail: str

    def render(self) -> str:
        return f"[{self.code}] {self.location}: {self.detail}"


class FailureList(list["Failure"]):
    """List-like validation failures that render as the compact report.

    Extractor build scripts often do ``raise SystemExit(f"... {failures}")``
    after calling ``chunk.validate()``.  A normal list repr dumps every
    dataclass instance; this preserves list behavior while making those legacy
    string paths use the grouped, token-efficient report.
    """

    def __init__(
        self,
        failures: list["Failure"] | None = None,
        *,
        report_context: dict[str, Any] | None = None,
    ):
        super().__init__(failures or [])
        self.report_context = report_context or {}

    def __str__(self) -> str:
        return render_failures(self, context=self.report_context)

    def __repr__(self) -> str:
        return str(self)


@dataclass
class Schema:
    version: str
    domain: str
    identity_dimensions: list[str]
    field_names: list[str]
    exempt_fields: frozenset[str]
    canonical_label_fields: frozenset[str]

    @classmethod
    def load(cls, path: Path | str) -> "Schema":
        path = Path(path)
        data = _load_yaml(path)

        identity = [d["field"] for d in (data.get("identity_dimensions") or [])]
        fields = data.get("fields") or []
        field_names = [f["name"] for f in fields]

        exempt = set(STRUCTURAL_EXEMPT_FIELDS)
        canonical_label_fields = set()
        for f in fields:
            name = f.get("name", "")
            norm = f.get("normalization")
            if name.endswith("_id") and isinstance(norm, str) and norm.strip():
                exempt.add(name)
            if _is_canonical_label_field(f):
                canonical_label_fields.add(name)

        return cls(
            version=str(data.get("schema_version", "")),
            domain=str(data.get("domain", "")),
            identity_dimensions=identity,
            field_names=field_names,
            exempt_fields=frozenset(exempt),
            canonical_label_fields=frozenset(canonical_label_fields),
        )

    def requires_literal_value_support(self, field_name: str, value: Any) -> bool:
        """Whether the field value itself must appear in its cited quote/cell.

        Canonical string labels such as `grade3_or_higher`, `crc_cohort`, or
        `metastatic_palliative` are schema vocabulary chosen from paper text;
        the evidence quote should support the mapping but will not usually
        contain the exact normalized token. Raw strings and numeric values
        still need literal support from their own quote or table row.
        """
        if isinstance(value, str) and field_name in self.canonical_label_fields:
            return False
        return True


def _is_canonical_label_field(field_spec: dict[str, Any]) -> bool:
    """Identify string fields whose values are schema labels, not paper tokens."""
    if field_spec.get("type") != "string":
        return False
    name = str(field_spec.get("name") or "")
    if name.endswith("_canonical"):
        return True
    guidance = str(field_spec.get("extraction_guidance") or "").lower()
    markers = (
        "controlled vocabulary",
        "choose from:",
        "common values:",
        "values:",
        "always use exactly these string values",
    )
    if any(marker in guidance for marker in markers):
        return True
    return name in {
        "comparison_group",
        "outcome_category",
        "outcome_subtype",
        "grade_threshold_canonical",
        "cohort_label",
        "comparison_type",
        "effect_size_type",
        "data_source_table",
        "data_source_location",
        "treatment_setting",
    }


def _load_yaml(path: Path) -> dict[str, Any]:
    # Lazy yaml import keeps the validator importable in hook runtimes. If the
    # script was launched as `python scripts/extraction/write_extraction.py`, PEP 723 deps
    # are bypassed; fall back through uv so extractor invocations stay usable.
    try:
        import yaml  # type: ignore

        with path.open() as f:
            data = yaml.safe_load(f)
    except ImportError:
        code = (
            "import json,sys,yaml;"
            "print(json.dumps(yaml.safe_load(open(sys.argv[1]))))"
        )
        try:
            proc = subprocess.run(
                ["uv", "run", "--with", "pyyaml", "python", "-c", code, str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as e:
            raise RuntimeError(
                "pyyaml required to load schema; fallback `uv run --with pyyaml` "
                f"failed for schema path: {path}"
            ) from e
        data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        raise RuntimeError(f"schema must parse to a mapping: {path}")
    return data


# ────────────────────────────────────────────────────────────────────
# Bundle text + quote matching (lifted verbatim from the legacy hook
# so behavior matches existing extractions character-for-character)
# ────────────────────────────────────────────────────────────────────


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace("−", "-")
    text = text.replace("…", "...")
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def compress_text(text: str) -> str:
    normalized = normalize_text(text)
    normalized = re.sub(r"-\s+", "", normalized)
    return re.sub(r"\s+", "", normalized)


def find_quote_in_source(quote: str, source_text: str) -> bool:
    if not quote:
        return False
    if normalize_text(quote) in normalize_text(source_text):
        return True
    if compress_text(quote) in compress_text(source_text):
        return True
    return False


# ────────────────────────────────────────────────────────────────────
# Value-against-source helpers
#
# verbatim-quote-in-bundle proves only that a string exists somewhere.
# These helpers add the missing semantic checks: the value must appear
# inside its own cited quote (paper fields), and a pure-arithmetic
# derivation must actually evaluate to the value (derived fields).
# ────────────────────────────────────────────────────────────────────

_NUMBER_RE = re.compile(r"-?(?:\d+(?:\.\d+)?|\.\d+)")  # incl. leading-decimal ".29"
# A derivation that is "pure arithmetic" — only digits, operators, the
# decimal point, parentheses, whitespace, and exactly one '='.
_ARITH_CHARS_RE = re.compile(r"^[\d\s.+\-*/()=,]+$")
_SPACED_ARITH_OP_RE = re.compile(r"\d[\d,.]*\s+[+\-*/]\s+\d[\d,.]*")
_PROSE_ARITH_RE = re.compile(
    r"\b(plus|minus|sum|summed|add|added|subtract|subtracted|"
    r"difference|complement|remaining|remainder)\b",
    re.IGNORECASE,
)


def _numbers_in(text: str) -> list[float]:
    # Strip thousands separators between digits so "1,518" reads as 1518
    # (otherwise a comma-formatted count looks absent from its own quote and
    # the extractor is forced to mislabel a transcribed value as `derived`).
    text = re.sub(r"(?<=\d),(?=\d)", "", text or "")
    out: list[float] = []
    for m in _NUMBER_RE.findall(text):
        try:
            out.append(float(m))
        except ValueError:
            continue
    return out


def value_in_quotes(value: Any, sources: list[dict]) -> bool:
    """True if the field's value is supported by the text of at least one
    of its own source quotes. Numbers match a numeric token within
    tolerance; strings match as a normalized substring; bools are skipped
    (no meaningful in-quote token)."""
    quotes = [
        s.get("quote", "")
        for s in sources
        if isinstance(s, dict) and isinstance(s.get("quote"), str)
    ]
    if not quotes:
        return False
    if isinstance(value, bool):
        return True  # booleans carry no in-quote numeric/string token
    if isinstance(value, (int, float)):
        for q in quotes:
            for n in _numbers_in(q):
                if abs(n - float(value)) <= 1e-6 + 1e-4 * abs(float(value)):
                    return True
        # Also allow an exact string rendering (e.g. value 0.40 quote "0.40")
        sval = str(value)
        return any(find_quote_in_source(sval, q) for q in quotes)
    if isinstance(value, str):
        # An empty/very short string is not meaningfully checkable.
        if len(value.strip()) == 0:
            return True
        return any(find_quote_in_source(value, q) for q in quotes)
    return True


def evaluate_derivation(derivation: str, value: Any) -> tuple[str, str]:
    """Check a derivation string.

    Returns (status, detail) where status is one of:
      - "ok"            arithmetic evaluated and matches the value
      - "not_arith"     derivation is prose (composition/concatenation) — skip
      - "mismatch"      arithmetic evaluated but != value (or internally inconsistent)

    Only `=`-containing pure-arithmetic expressions are evaluated. Prose
    derivations ("concatenated from Table 1 and Table 2 footnote a") are
    left to LLM attestation and return "not_arith"."""
    if not isinstance(derivation, str) or "=" not in derivation:
        return ("not_arith", "")
    if not _ARITH_CHARS_RE.match(derivation):
        return ("not_arith", "")
    parts = derivation.split("=")
    if len(parts) != 2:
        return ("not_arith", "multiple '=' — not a simple expression")
    lhs, rhs = parts[0].strip().replace(",", ""), parts[1].strip().replace(",", "")
    if not lhs or not rhs:
        return ("not_arith", "")
    try:
        lhs_val = eval(lhs, {"__builtins__": {}}, {})  # noqa: S307 - chars whitelisted above
        rhs_val = float(rhs)
    except (SyntaxError, ZeroDivisionError, NameError, TypeError, ValueError):
        return ("not_arith", "could not evaluate")
    if not isinstance(lhs_val, (int, float)):
        return ("not_arith", "")
    tol = 1e-6 + 1e-4 * abs(rhs_val)
    if abs(float(lhs_val) - rhs_val) > tol:
        return ("mismatch", f"{lhs} = {lhs_val} but derivation claims {rhs_val}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if abs(float(value) - rhs_val) > (1e-6 + 1e-4 * abs(float(value))):
            return ("mismatch", f"derivation result {rhs_val} != field value {value}")
    return ("ok", "")


# ────────────────────────────────────────────────────────────────────
# Cell-anchor matching
#
# A table-sourced value gains a structured `cell: {row, column}` anchor so a
# reviewer can locate exactly which row × column of the grid the value sits
# in. These helpers resolve the anchor against the parsed grid: the row label
# (matched in ANY cell of a row, since labels aren't always the first cell),
# the column header, and the value's presence in the matched row.
# ────────────────────────────────────────────────────────────────────


def _cell_text_match(needle: str, haystack: str) -> bool:
    """Case-insensitive, whitespace-collapsed substring match used for cell
    row/column anchor resolution."""
    if not needle:
        return False
    n = re.sub(r"\s+", " ", needle).strip().lower()
    h = re.sub(r"\s+", " ", haystack or "").strip().lower()
    if not n:
        return False
    return n in h


def _grid_rows(grid: dict) -> list[list[str]]:
    """Return each grid row as a list of flattened cell strings (dict-rows are
    flattened in value order)."""
    out: list[list[str]] = []
    for row in grid.get("rows") or []:
        if isinstance(row, dict):
            out.append([_flatten_cell(v) for v in row.values()])
        else:
            out.append([_flatten_cell(c) for c in (row or [])])
    return out


def _find_anchor_row(grid: dict, row_label: str) -> list[str] | None:
    """Find the grid row whose ANY cell matches the anchor's row label.
    Returns the flattened cells of the first matching row, or None."""
    for cells in _grid_rows(grid):
        if any(_cell_text_match(row_label, c) for c in cells):
            return cells
    return None


def _grid_has_column(grid: dict, column: str) -> bool:
    # Primary: the declared header row.
    for h in grid.get("headers") or []:
        if _cell_text_match(column, _flatten_cell(h)):
            return True
    # Real papers frequently use multi-level headers that the PDF table
    # extractor flattens into the FIRST FEW DATA ROWS (the declared `headers`
    # then read as generic `col_0/col_1/...`). Accept a column label that
    # appears anywhere in that header band so legitimate sub-header anchors
    # (e.g. "BCT"/"Math"/"Raven" under spanning "All"/"Girls"/"Boys") resolve.
    for cells in _grid_rows(grid)[:3]:
        if any(_cell_text_match(column, c) for c in cells):
            return True
    return False


def _value_in_row_cells(value: Any, cells: list[str]) -> bool:
    """True if the field's value appears within the anchored row's cells
    (numeric or string form), reusing the in-quote matching helpers."""
    pseudo_sources = [{"quote": c} for c in cells if c]
    return value_in_quotes(value, pseudo_sources)


def _flatten_cell(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, (int, float)):
        return str(cell)
    if isinstance(cell, dict):
        for k in ("text", "value", "content"):
            if k in cell:
                return _flatten_cell(cell[k])
        return ""
    if isinstance(cell, (list, tuple)):
        return " ".join(_flatten_cell(c) for c in cell)
    return str(cell)


def load_bundle_text(bundle_dir: Path) -> str:
    parts: list[str] = []

    sections_dir = bundle_dir / "sections"
    if sections_dir.exists():
        for md in sorted(sections_dir.glob("*.md")):
            try:
                parts.append(md.read_text())
            except OSError:
                continue

    tables_dir = bundle_dir / "tables"
    if tables_dir.exists():
        for tj in sorted(tables_dir.glob("*.json")):
            try:
                tab = json.loads(tj.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if title := tab.get("title"):
                parts.append(str(title))
            for h in tab.get("headers") or []:
                parts.append(_flatten_cell(h))
            for row in tab.get("rows") or []:
                if isinstance(row, dict):
                    for v in row.values():
                        parts.append(_flatten_cell(v))
                else:
                    for cell in row or []:
                        parts.append(_flatten_cell(cell))
            tmd = tj.with_suffix(".md")
            if tmd.exists():
                try:
                    parts.append(tmd.read_text())
                except OSError:
                    pass

    manifest = bundle_dir / "manifest.json"
    if manifest.exists():
        try:
            parts.append(manifest.read_text())
        except OSError:
            pass

    return "\n\n".join(parts)


@dataclass
class BundleSources:
    """Per-location view of a bundle.

    `full_text` is the legacy whole-bundle concatenation (kept for the
    figure-exempt fallback). `by_file` maps each bundle-relative file path
    (e.g. "sections/methods.md", "tables/table_3.json") to its text.
    `tables` holds the parsed grids for optional cell-locator checks.
    """

    full_text: str
    by_file: dict[str, str] = field(default_factory=dict)
    tables: dict[str, dict] = field(default_factory=dict)
    available_files: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def load(cls, bundle_dir: Path) -> "BundleSources":
        by_file: dict[str, str] = {}
        tables: dict[str, dict] = {}

        sections_dir = bundle_dir / "sections"
        if sections_dir.exists():
            for md in sorted(sections_dir.glob("*.md")):
                try:
                    by_file[f"sections/{md.name}"] = md.read_text()
                except OSError:
                    continue

        tables_dir = bundle_dir / "tables"
        if tables_dir.exists():
            for tj in sorted(tables_dir.glob("*.json")):
                try:
                    tab = json.loads(tj.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                parts: list[str] = []
                if title := tab.get("title"):
                    parts.append(str(title))
                for h in tab.get("headers") or []:
                    parts.append(_flatten_cell(h))
                for row in tab.get("rows") or []:
                    if isinstance(row, dict):
                        parts.extend(_flatten_cell(v) for v in row.values())
                    else:
                        parts.extend(_flatten_cell(c) for c in (row or []))
                by_file[f"tables/{tj.name}"] = "\n".join(parts)
                tid = tj.stem  # e.g. "table_3"
                tables[tid] = tab
                tmd = tj.with_suffix(".md")
                if tmd.exists():
                    try:
                        by_file[f"tables/{tmd.name}"] = tmd.read_text()
                    except OSError:
                        pass

        manifest = bundle_dir / "manifest.json"
        if manifest.exists():
            try:
                by_file["manifest.json"] = manifest.read_text()
            except OSError:
                pass

        return cls(
            full_text=load_bundle_text(bundle_dir), by_file=by_file, tables=tables,
            available_files=frozenset(str(p.relative_to(bundle_dir)) for p in bundle_dir.rglob("*") if p.is_file()),
        )

    # Tokens that look like a bundle file path inside a free-text location.
    _PATH_RE = re.compile(r"(sections|tables|figures)/[\w.\-]+")

    def resolve_location(self, location: str) -> tuple[str | None, str | None]:
        """Map a `location` string to the bundle file it names.

        Returns (matched_file_key, text) or (None, None) if the location
        does not name a real bundle file. Accepts an exact relative path,
        a path embedded in free text, or a `table_<id>` reference that
        resolves to that table's json/md text."""
        if not location:
            return (None, None)
        loc = location.strip()
        if loc in self.by_file:
            return (loc, self.by_file[loc])
        m = self._PATH_RE.search(loc)
        if m:
            key = m.group(0)
            if key in self.by_file:
                return (key, self.by_file[key])
            # tables/table_3 (no extension) → prefer .json then .md
            for ext in (".json", ".md"):
                if key + ext in self.by_file:
                    return (key + ext, self.by_file[key + ext])
        # "table_3" style reference anywhere in the string.
        tm = re.search(r"table[_\s]?(\w+)", loc, re.IGNORECASE)
        if tm:
            tid = f"table_{tm.group(1)}"
            for ext in (".json", ".md"):
                if f"tables/{tid}{ext}" in self.by_file:
                    return (f"tables/{tid}{ext}", self.by_file[f"tables/{tid}{ext}"])
        return (None, None)

    def quote_locations(self, quote: str, *, limit: int = 5) -> list[str]:
        """Return bundle file keys where `quote` appears.

        Used only for diagnostics: quote-at-location remains strict, but when
        an agent cites the wrong file we tell it whether the quote exists
        elsewhere in the bundle or is absent from the corpus entirely.
        """
        if not quote:
            return []
        matches: list[str] = []
        for key, text in sorted(self.by_file.items()):
            if find_quote_in_source(quote, text):
                matches.append(key)
                if len(matches) >= limit:
                    break
        return matches

    def table_grid_for(self, location: str) -> tuple[str | None, dict | None]:
        """Resolve a location to the parsed table grid ({headers, rows, ...})
        backing it.

        Returns (matched_file_key, grid) where grid is the parsed tables/*.json
        dict. A `tables/*.json` location resolves to its own grid; a
        `tables/*.md` location resolves to the sibling .json grid if one
        exists, else (key, None). Non-table locations return (key|None, None).
        """
        matched, _ = self.resolve_location(location)
        if not matched or not matched.startswith("tables/"):
            return (matched, None)
        stem = Path(matched).stem  # tables/table_3.json|.md → table_3
        grid = self.tables.get(stem)
        return (matched, grid if isinstance(grid, dict) else None)

    def table_validation_for(self, location: str) -> str | None:
        """Return the paper-prep validation status of the table a location
        names ('corroborated' | 'single_source' | 'unresolved_conflict'), or
        None if the location is not a table."""
        matched, _ = self.resolve_location(location)
        if not matched or not matched.startswith("tables/"):
            return None
        # tables/table_3.json or .md → table_3
        stem = Path(matched).stem
        tab = self.tables.get(stem)
        if isinstance(tab, dict):
            v = tab.get("validation")
            if isinstance(v, str):
                return v
        return None


# ────────────────────────────────────────────────────────────────────
# Payload shape + field-entry validation
# ────────────────────────────────────────────────────────────────────


ALLOWED_SOURCE_KEYS = frozenset({"quote", "location", "page", "cell"})
ALLOWED_SOURCE_COMPONENT_KEYS = frozenset({"label", "value", "sources", "role"})


def _is_valid_source_dict(src: Any) -> tuple[bool, str]:
    if not isinstance(src, dict):
        return False, "source is not a dict"
    if not isinstance(src.get("quote"), str) or not src["quote"]:
        return False, "source.quote must be a non-empty string"
    if not isinstance(src.get("location"), str) or not src["location"]:
        return False, "source.location must be a non-empty string"
    if not isinstance(src.get("page"), int):
        return False, "source.page must be an integer"
    if "cell" in src:
        cell = src["cell"]
        if not isinstance(cell, dict):
            return False, "source.cell must be a dict {row, column}"
        if not isinstance(cell.get("row"), str) or not cell["row"].strip():
            return False, "source.cell.row must be a non-empty string"
        if not isinstance(cell.get("column"), str) or not cell["column"].strip():
            return False, "source.cell.column must be a non-empty string"
    unknown = set(src.keys()) - ALLOWED_SOURCE_KEYS
    if unknown:
        return False, f"source contains unknown key(s): {sorted(unknown)}"
    return True, ""


def _validate_cell_anchor(
    src: dict,
    *,
    value: Any,
    loc: str,
    src_index: int,
    matched_key: str | None,
    bundle: BundleSources,
    check_value: bool = True,
) -> list[Failure]:
    """A non-null field's source located in a `tables/*.json` grid MUST
    carry a resolvable `cell: {row, column}` anchor. Sources in sections/,
    figures/, or `tables/*.md` are exempt from the *requirement*; a `cell`
    present on a `tables/*.md` source is still validated against the sibling
    .json grid if one exists (else skipped). The anchor checks: row label
    present in the grid, column header present, and the value present in the
    matched row's cells."""
    failures: list[Failure] = []
    cell = src.get("cell")
    is_table_json = (
        bool(matched_key)
        and matched_key.startswith("tables/")
        and matched_key.endswith(".json")
    )

    if cell is None:
        if is_table_json:
            failures.append(Failure(
                "cell_anchor_missing", f"{loc} sources[{src_index}]",
                f"a table-sourced value (location {matched_key!r}) must carry a "
                f"cell anchor: cell: {{row: '<row label>', column: '<column "
                f"header>'}}.",
            ))
        return failures

    # A cell is present. Resolve the grid backing the location; for a
    # tables/*.md source we use the sibling .json grid if present, else skip.
    _key, grid = bundle.table_grid_for(src.get("location", ""))
    if not isinstance(grid, dict):
        return failures  # no resolvable grid (e.g. .md with no .json sibling)

    row_label = cell.get("row", "") if isinstance(cell, dict) else ""
    column = cell.get("column", "") if isinstance(cell, dict) else ""

    anchor_cells = _find_anchor_row(grid, row_label)
    if anchor_cells is None:
        failures.append(Failure(
            "cell_row_not_found", f"{loc} sources[{src_index}]",
            f"cell.row {row_label!r} does not match any row of table "
            f"{_key!r}.",
        ))
    if not _grid_has_column(grid, column):
        failures.append(Failure(
            "cell_column_not_found", f"{loc} sources[{src_index}]",
            f"cell.column {column!r} does not appear in the headers of table "
            f"{_key!r}: {grid.get('headers')!r}.",
        ))
    # For `derived` values the cell anchor points at an OPERAND cell, not the
    # computed result — so the value-in-row check only applies to transcribed
    # (`paper`) values, mirroring the `value_not_in_quote` scoping.
    if check_value and anchor_cells is not None and not _value_in_row_cells(value, anchor_cells):
        failures.append(Failure(
            "cell_value_not_at_anchor", f"{loc} sources[{src_index}]",
            f"value {value!r} does not appear in the cells of the anchored row "
            f"(cell.row={row_label!r}) of table {_key!r}.",
        ))
    return failures


def _validate_component_sources(
    component: dict[str, Any],
    *,
    loc: str,
    bundle: BundleSources,
) -> list[Failure]:
    """Validate one source component inside a derived field.

    A component is an evidence-backed operand/fact used by a derived value.
    Unlike the parent derived value, the component value itself must be found
    in its own component source quote, giving the validator a mechanical way
    to reject "derived" as an escape hatch for unsupported values.
    """
    failures: list[Failure] = []
    value = component.get("value")
    sources = component.get("sources")
    valid_nonfigure_sources: list[dict] = []

    if not isinstance(sources, list) or not sources:
        failures.append(Failure(
            "source_component_missing_sources", loc,
            "source component requires non-empty sources",
        ))
        return failures

    for i, src in enumerate(sources):
        ok, reason = _is_valid_source_dict(src)
        src_loc = f"{loc} sources[{i}]"
        if not ok:
            failures.append(Failure("source_component_bad_source", src_loc, reason))
            continue

        location_str = src.get("location", "")
        if FIGURE_LOCATION_PATTERN.search(location_str):
            continue

        matched_key, loc_text = bundle.resolve_location(location_str)
        if loc_text is None:
            failures.append(Failure(
                "source_component_location_not_a_bundle_file", src_loc,
                f"location does not name a real bundle file "
                f"(location={location_str!r})",
            ))
            if not find_quote_in_source(src["quote"], bundle.full_text):
                failures.append(Failure(
                    "source_component_quote_not_in_bundle", src_loc,
                    f"quote not found verbatim anywhere in bundle: "
                    f"{src['quote'][:200]!r}",
                ))
            else:
                valid_nonfigure_sources.append(src)
            continue

        if not find_quote_in_source(src["quote"], loc_text):
            other_locations = [
                key for key in bundle.quote_locations(src["quote"])
                if key != matched_key
            ]
            if other_locations:
                quote_status = (
                    "quote_status=found_elsewhere; "
                    f"candidate_locations={other_locations!r}; "
                )
            else:
                quote_status = "quote_status=not_found_anywhere; "
            failures.append(Failure(
                "source_component_quote_not_at_location", src_loc,
                f"{quote_status}quote not found in cited location "
                f"{matched_key!r}: {src['quote'][:200]!r}",
            ))
        else:
            valid_nonfigure_sources.append(src)

        failures.extend(_validate_cell_anchor(
            src, value=value, loc=loc, src_index=i,
            matched_key=matched_key, bundle=bundle,
            check_value=True,
        ))

    if valid_nonfigure_sources and not value_in_quotes(value, valid_nonfigure_sources):
        failures.append(Failure(
            "source_component_value_not_in_quote", loc,
            f"component value {value!r} does not appear in any of its cited "
            "component quotes",
        ))
    return failures


def _arithmetic_operand_numbers(derivation: Any, value: Any) -> list[float]:
    """Return numeric operands that require source components.

    Pure formulas use the numbers left of the equals sign. Prose arithmetic is
    necessarily fuzzier, so only obvious arithmetic language/operator patterns
    on numeric derived values trigger the requirement, and the declared result
    value is excluded from the operand set.
    """

    if not isinstance(derivation, str):
        return []
    if "=" in derivation and _ARITH_CHARS_RE.match(derivation):
        parts = derivation.split("=")
        if len(parts) == 2:
            return _numbers_in(parts[0])
    if not _numbers_in(str(value)):
        return []
    if not (_SPACED_ARITH_OP_RE.search(derivation) or _PROSE_ARITH_RE.search(derivation)):
        return []
    operands = [n for n in _numbers_in(derivation) if not _number_matches(value, n)]
    return operands


def _number_matches(value: Any, target: float) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return abs(float(value) - target) <= 1e-6 + 1e-4 * abs(target)
    if isinstance(value, str):
        try:
            return abs(float(value.strip().replace(",", "")) - target) <= 1e-6 + 1e-4 * abs(target)
        except ValueError:
            return False
    return False


def _validate_source_components(
    entry: dict[str, Any],
    *,
    loc: str,
    bundle: BundleSources,
) -> list[Failure]:
    failures: list[Failure] = []
    components = entry.get("source_components")
    prov_type = entry.get("type")
    derivation = entry.get("derivation")
    operands = _arithmetic_operand_numbers(derivation, entry.get("value"))

    if components is None:
        if prov_type == "derived" and operands:
            failures.append(Failure(
                "missing_source_components", loc,
                "arithmetic derived fields must include source_components so "
                "each operand can be mechanically aligned to its evidence",
            ))
        return failures

    if prov_type != "derived":
        failures.append(Failure(
            "source_components_on_nonderived", loc,
            "source_components are only valid on type='derived' fields",
        ))

    if not isinstance(components, list) or not components:
        failures.append(Failure(
            "bad_source_components", loc,
            "source_components must be a non-empty list",
        ))
        return failures

    labels: set[str] = set()
    component_values: list[Any] = []
    for i, component in enumerate(components):
        comp_loc = f"{loc} source_components[{i}]"
        if not isinstance(component, dict):
            failures.append(Failure(
                "bad_source_component", comp_loc,
                f"source component must be a dict, got {type(component).__name__}",
            ))
            continue
        unknown = set(component.keys()) - ALLOWED_SOURCE_COMPONENT_KEYS
        if unknown:
            failures.append(Failure(
                "bad_source_component", comp_loc,
                f"source component contains unknown key(s): {sorted(unknown)}",
            ))
        label = component.get("label")
        if not isinstance(label, str) or not label.strip():
            failures.append(Failure(
                "bad_source_component", comp_loc,
                "source component requires non-empty string label",
            ))
        elif label in labels:
            failures.append(Failure(
                "duplicate_source_component_label", comp_loc,
                f"duplicate source component label {label!r}",
            ))
        else:
            labels.add(label)
        if "value" not in component:
            failures.append(Failure(
                "bad_source_component", comp_loc,
                "source component missing value",
            ))
        else:
            component_values.append(component.get("value"))
        failures.extend(_validate_component_sources(component, loc=comp_loc, bundle=bundle))

    for operand in operands:
        if not any(_number_matches(value, operand) for value in component_values):
            failures.append(Failure(
                "derived_operand_missing_component", loc,
                f"arithmetic operand {operand:g} in derivation {derivation!r} "
                "is not represented by any source_component.value",
            ))

    return failures


def _validate_missing_entry(entry: dict, *, loc: str, bundle: BundleSources) -> list[Failure]:
    failures: list[Failure] = []
    if "missing_reason" not in entry:
        failures.append(Failure("missing_reason_required", loc, "use missing(reason, explanation); bare null and abstention shortcuts are not accepted"))
    reason = entry.get("missing_reason")
    if reason is not None and (not isinstance(reason, str) or reason not in MISSING_REASONS):
        failures.append(Failure("bad_missing_reason", loc, f"choose one of {sorted(MISSING_REASONS)}, or explicit null only for an uncovered case"))
    explanation = entry.get("reasoning")
    if not isinstance(explanation, str) or not explanation.strip():
        failures.append(Failure("reasoning_required_missing", loc, "every absent value requires a concrete explanation"))
    for key in ("type", "derivation", "derivation_kind", "extraction_certainty", "source_components"):
        if key in entry:
            failures.append(Failure("shape_error", loc, f"an absent value must not carry {key!r}; its sources support missingness, not a value claim"))
    sources = entry.get("sources", [])
    if not isinstance(sources, list):
        failures.append(Failure("bad_source_shape", loc, "sources must be a list"))
        return failures
    if isinstance(reason, str) and reason in MISSING_REASONS_REQUIRING_SOURCES and not sources:
        failures.append(Failure("missingness_sources_required", loc, f"{reason} requires evidence for the reason, not an invented value"))
    for i, src in enumerate(sources):
        source_loc = f"{loc} sources[{i}]"
        if not isinstance(src, dict):
            failures.append(Failure("bad_source_shape", source_loc, "source must be an object"))
            continue
        location = src.get("location")
        page = src.get("page")
        quote = src.get("quote", "")
        if not isinstance(location, str) or not location.strip() or type(page) is not int or page < 0:
            failures.append(Failure("bad_source_shape", source_loc, "source requires a nonempty location and nonnegative integer page"))
            continue
        if not isinstance(quote, str):
            failures.append(Failure("bad_source_shape", source_loc, "quote must be a string when supplied"))
            continue
        unknown = set(src) - {"location", "page", "quote", "cell"}
        if unknown:
            failures.append(Failure("bad_source_shape", source_loc, f"unknown source keys: {sorted(unknown)}"))
        matched, text = bundle.resolve_location(location)
        if matched is None and location not in bundle.available_files:
            failures.append(Failure("location_not_a_bundle_file", source_loc, "cite an available source or the passage referencing missing material, not an absent file"))
            continue
        # Explicit absence and references need a verifiable source statement.
        if reason in ("explicitly_unavailable", "referenced_source_unavailable") and (not quote.strip() or text is None):
            failures.append(Failure("missingness_quote_required", source_loc, "this reason requires a quoted statement in an available text source"))
        elif text is not None and quote and not find_quote_in_source(quote, text):
            failures.append(Failure("quote_not_at_location", source_loc, "the missingness-support quote is absent from the cited source"))
        elif not quote.strip() and reason != "unreadable":
            failures.append(Failure("missingness_quote_required", source_loc, "a readable supporting source needs its relevant quote"))
    return failures


def missingness_report(payload: dict, schema: Schema) -> dict[str, Any]:
    """Audit supplied fields only; old untouched rows are not silently reclassified."""
    counts = {key: 0 for key in sorted(MISSING_REASONS)}
    fallbacks: list[dict[str, Any]] = []
    unexplained: list[dict[str, Any]] = []
    shared: dict[str, int] = {}
    total = 0
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("fields"), list):
            continue
        for entry in row["fields"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            if entry["name"] in schema.exempt_fields or "value" not in entry or entry["value"] is not None:
                continue
            total += 1
            reason = entry.get("missing_reason")
            item = {"row": index, "field": entry.get("name"), "explanation": entry.get("reasoning")}
            if "missing_reason" not in entry or not isinstance(entry.get("reasoning"), str) or not entry["reasoning"].strip():
                unexplained.append(item)
            elif reason is None:
                fallbacks.append(item)
                shared[entry["reasoning"]] = shared.get(entry["reasoning"], 0) + 1
            elif isinstance(reason, str) and reason in counts:
                counts[reason] += 1
    return {
        "missingness": {
            "scope": "submitted new/modified field occurrences; untouched prior rows excluded",
            "missing_field_count": total, "by_reason": counts,
            "uncategorized_count": len(fallbacks),
            "uncategorized_fraction": len(fallbacks) / total if total else 0.0,
            "uncategorized_fields": fallbacks,
            "uncategorized_distinct_explanations": len(shared),
            "unexplained_fields": unexplained,
        },
        "warnings": [{"code": "uncategorized_missing", **item} for item in fallbacks],
        "warning_count": len(fallbacks),
    }


def render_missingness_summary(report: dict[str, Any]) -> str:
    summary = report["missingness"]
    text = f"Missing values: {summary['missing_field_count']}; uncategorized: {summary['uncategorized_count']}."
    if summary["uncategorized_count"]:
        text += " WARNING: QC must review each uncategorized explanation in the machine report."
    return text + "\n"


def _validate_field_entry(
    entry: Any,
    *,
    schema: Schema,
    row_loc: str,
    bundle: BundleSources,
) -> list[Failure]:
    """Validate a single {name, value, type?, sources?, derivation?} entry."""
    failures: list[Failure] = []

    if not isinstance(entry, dict):
        failures.append(Failure(
            "shape_error", row_loc,
            f"field entry must be a dict, got {type(entry).__name__}",
        ))
        return failures

    name = entry.get("name")
    if not isinstance(name, str) or not name:
        failures.append(Failure(
            "shape_error", row_loc,
            "field entry missing 'name' (non-empty string)",
        ))
        return failures

    loc = f"{row_loc} field={name}"

    if "value" not in entry:
        failures.append(Failure("shape_error", loc, "field entry missing 'value'"))
        return failures

    ALLOWED_FIELD_ENTRY_KEYS = {
        "name", "value", "type", "sources", "derivation",
        "source_components",
        "derivation_kind", "extraction_certainty",
        "reasoning", "missing_reason",  # Explanations and structured missingness
    }
    unknown = set(entry.keys()) - ALLOWED_FIELD_ENTRY_KEYS
    if unknown:
        failures.append(Failure(
            "unknown_entry_key", loc,
            f"field entry contains unknown key(s): {sorted(unknown)}. "
            f"Allowed keys: {sorted(ALLOWED_FIELD_ENTRY_KEYS)}",
        ))

    value = entry["value"]
    certainty = entry.get("extraction_certainty")
    if certainty is not None and certainty not in ALLOWED_CERTAINTIES:
        failures.append(Failure(
            "bad_certainty", loc,
            f"extraction_certainty must be one of {sorted(ALLOWED_CERTAINTIES)}; "
            f"got {certainty!r}",
        ))

    if value is None:
        if name in schema.exempt_fields:
            # Canonical IDs pending normalization are not missing scientific facts.
            if set(entry) - {"name", "value"}:
                failures.append(Failure("shape_error", loc, "normalization placeholders must not carry extraction provenance"))
            return failures
        return failures + _validate_missing_entry(entry, loc=loc, bundle=bundle)

    if "missing_reason" in entry:
        failures.append(Failure("shape_error", loc, "a populated value must not carry missing_reason"))

    # Non-null value: provenance is required UNLESS the field is exempt
    # (schema-derived: normalizer-populated _id fields).
    if name in schema.exempt_fields:
        # Exempt field with a non-null value during extraction is unusual
        # (these are normalizer-populated). Don't fail — but the script
        # writes the value through anyway. The downstream normalizer will
        # see it.
        return failures

    prov_type = entry.get("type")
    if prov_type not in ALLOWED_PROVENANCE_TYPES:
        failures.append(Failure(
            "bad_provenance_type", loc,
            f"type must be one of {sorted(ALLOWED_PROVENANCE_TYPES)}; "
            f"got {prov_type!r} (external is no longer allowed)",
        ))

    sources = entry.get("sources")
    valid_nonfigure_sources: list[dict] = []
    if not isinstance(sources, list) or len(sources) == 0:
        failures.append(Failure(
            "missing_sources", loc,
            "non-null field requires sources: [{quote, location, page}, ...]",
        ))
    else:
        for i, src in enumerate(sources):
            ok, reason = _is_valid_source_dict(src)
            if not ok:
                failures.append(Failure(
                    "bad_source_shape", f"{loc} sources[{i}]", reason,
                ))
                continue
            location_str = src.get("location", "")
            if FIGURE_LOCATION_PATTERN.search(location_str):
                # Figure-located quotes can't be verified against text; skip
                # the verbatim/location check (the value is `inferred`).
                continue
            # Quote must appear in the file the location NAMES, not just
            # anywhere in the bundle. Decorative locations are now caught.
            matched_key, loc_text = bundle.resolve_location(location_str)
            if loc_text is None:
                failures.append(Failure(
                    "location_not_a_bundle_file", f"{loc} sources[{i}]",
                    f"location does not name a real bundle file "
                    f"(location={location_str!r}). Use a path like "
                    f"sections/<name>.md or tables/table_<id>.json/.md, or a "
                    f"figures/ location for figure-interior values.",
                ))
                # Fall back to whole-bundle so we still flag a hallucinated quote.
                if not find_quote_in_source(src["quote"], bundle.full_text):
                    failures.append(Failure(
                        "quote_not_in_bundle", f"{loc} sources[{i}]",
                        f"quote not found verbatim anywhere in bundle: "
                        f"{src['quote'][:200]!r}",
                    ))
                else:
                    valid_nonfigure_sources.append(src)
                continue
            if not find_quote_in_source(src["quote"], loc_text):
                other_locations = [
                    key for key in bundle.quote_locations(src["quote"])
                    if key != matched_key
                ]
                if other_locations:
                    quote_status = (
                        "quote_status=found_elsewhere; "
                        f"candidate_locations={other_locations!r}; "
                    )
                else:
                    quote_status = "quote_status=not_found_anywhere; "
                failures.append(Failure(
                    "quote_not_at_location", f"{loc} sources[{i}]",
                    f"{quote_status}"
                    f"quote not found in cited location {matched_key!r} "
                    f"(page={src.get('page')}): {src['quote'][:200]!r}",
                ))
            else:
                valid_nonfigure_sources.append(src)

            # Structured cell anchors for table-sourced values.
            failures.extend(_validate_cell_anchor(
                src, value=value, loc=loc, src_index=i,
                matched_key=matched_key, bundle=bundle,
                check_value=(
                    prov_type == "paper"
                    and schema.requires_literal_value_support(name, value)
                ),
            ))

    # The value must be supported by its own quote, not merely co-present
    # in the bundle. Applies to transcribed (`paper`) values with at least one
    # verifiable (non-figure) source. Figure-only values are exempt.
    if (
        prov_type == "paper"
        and valid_nonfigure_sources
        and schema.requires_literal_value_support(name, value)
    ):
        if not value_in_quotes(value, valid_nonfigure_sources):
            failures.append(Failure(
                "value_not_in_quote", loc,
                f"value {value!r} does not appear in any of its cited "
                f"quotes — a quote that exists in the bundle but does not "
                f"contain the value does not support it.",
            ))

    if prov_type == "derived":
        deriv = entry.get("derivation")
        if not isinstance(deriv, str) or not deriv.strip():
            failures.append(Failure(
                "missing_derivation", loc,
                "type='derived' requires a non-empty 'derivation' string",
            ))
        else:
            # Evaluate pure-arithmetic derivations; prose composition
            # ("concatenated from ...") is left to LLM attestation.
            status, detail = evaluate_derivation(deriv, value)
            if status == "mismatch":
                failures.append(Failure(
                    "derivation_arithmetic_mismatch", loc,
                    f"derivation does not compute: {detail}",
                ))

    failures.extend(_validate_source_components(entry, loc=loc, bundle=bundle))

    # Anchor the confidence tags to mechanical signals so `clean`
    # cannot be self-asserted on an uncorroborated or figure-only value.
    all_sources = entry.get("sources") if isinstance(entry.get("sources"), list) else []
    has_figure_source = any(
        isinstance(s, dict) and FIGURE_LOCATION_PATTERN.search(s.get("location", ""))
        for s in all_sources
    )
    has_uncorroborated_table = any(
        isinstance(s, dict)
        and bundle.table_validation_for(s.get("location", "")) in _UNCORROBORATED_TABLE
        for s in all_sources
    )
    dkind = entry.get("derivation_kind")
    if dkind is not None and dkind not in ALLOWED_DERIVATION_KINDS:
        failures.append(Failure(
            "bad_derivation_kind", loc,
            f"derivation_kind must be one of {sorted(ALLOWED_DERIVATION_KINDS)}; got {dkind!r}",
        ))
    elif dkind is not None:
        if has_figure_source and dkind != "inferred":
            failures.append(Failure(
                "figure_value_not_inferred", loc,
                "a figure-sourced value must be derivation_kind='inferred' "
                "(its interior cannot be verbatim-verified)",
            ))
        if dkind == "verbatim" and prov_type == "derived":
            failures.append(Failure(
                "derivation_kind_type_mismatch", loc,
                "derivation_kind='verbatim' is inconsistent with type='derived'",
            ))
    if certainty == "clean":
        if has_figure_source:
            failures.append(Failure(
                "clean_on_figure", loc,
                "extraction_certainty='clean' is not allowed on a figure-sourced "
                "value — figure interiors are unverifiable; use 'ambiguous'.",
            ))
        if has_uncorroborated_table:
            failures.append(Failure(
                "clean_on_uncorroborated_table", loc,
                "extraction_certainty='clean' requires a corroborated source; this "
                "value cites a single_source/unresolved_conflict table. "
                "Use 'ambiguous'.",
            ))

    # Prose reasoning is REQUIRED when the interpretive step is
    # non-trivial — a derived/inferred value, an ambiguous read, or a
    # multi-source field. For verbatim+clean single-source values the cell
    # anchor itself is the context, so reasoning stays optional (cost
    # discipline).
    reasoning = entry.get("reasoning")
    derivation = entry.get("derivation")
    has_derivation_text = isinstance(derivation, str) and bool(derivation.strip())
    n_sources = len(all_sources)
    clean_canonical_mapping = (
        name in schema.canonical_label_fields
        and certainty == "clean"
        and dkind == "derived"
        and n_sources == 1
    )
    reasoning_required = (
        certainty == "ambiguous"
        or (dkind == "inferred" and not has_derivation_text)
        or (
            dkind == "derived"
            and not clean_canonical_mapping
            and not has_derivation_text
        )
        # type='derived' triggers the requirement even when derivation_kind
        # was omitted — otherwise omitting the tag dodges the rule.
        or (
            entry.get("type") == "derived"
            and not clean_canonical_mapping
            and not has_derivation_text
        )
    )
    if reasoning_required:
        if not isinstance(reasoning, str) or not reasoning.strip():
            failures.append(Failure(
                "reasoning_required_missing", loc,
                "a non-empty 'reasoning' is required when derivation_kind is "
                "derived/inferred, extraction_certainty is ambiguous, "
                "or the field has multiple sources — connect the evidence to "
                "the claim in 1-2 sentences.",
            ))

    return failures


def _row_loc(row_index: int, identity_repr: str) -> str:
    if identity_repr:
        return f"row[{row_index}] identity={identity_repr}"
    return f"row[{row_index}]"


def _numeric(value: Any) -> float | None:
    """Coerce a field value to a float for consistency checks; None if the
    value is missing, boolean, or non-numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _check_internal_consistency(
    fields_list: list[Any], *, row_loc: str
) -> list[Failure]:
    """Internal-consistency checks over the Standards Core numerics of
    one assembled row. HARD failures — a transcription that is faithful but
    incoherent (inverted CI, estimate outside its own CI, events > total) is
    caught here, which also flags the right-value-wrong-cell / column-transpose
    class. Only runs over participants that are present and numeric."""
    failures: list[Failure] = []
    by_name: dict[str, float | None] = {}
    for f in fields_list:
        if isinstance(f, dict) and isinstance(f.get("name"), str):
            by_name[f["name"]] = _numeric(f.get("value"))

    def first_numeric(*names: str) -> tuple[str, float | None]:
        for name in names:
            value = by_name.get(name)
            if value is not None:
                return name, value
        return names[0], None

    lo_name, lo = first_numeric("ci_lower", "effect_size_ci_lower")
    hi_name, hi = first_numeric("ci_upper", "effect_size_ci_upper")
    est_name, est = first_numeric("effect_estimate", "effect_size", "effect_size_value")
    events_name, n_events = first_numeric(
        "n_events", "group_events", "reference_events",
    )
    total_name, n_total = first_numeric(
        "n_total", "total_n", "group_n", "reference_n",
    )

    if lo is not None and hi is not None and lo > hi:
        failures.append(Failure(
            "ci_bounds_inverted", row_loc,
            f"{lo_name} ({lo}) > {hi_name} ({hi}) — the confidence interval is "
            f"inverted; check for a column transposition.",
        ))
    if est is not None and lo is not None and hi is not None and lo <= hi:
        slack = 0.05 * (hi - lo)
        if est < lo - slack or est > hi + slack:
            failures.append(Failure(
                "estimate_outside_ci", row_loc,
                f"{est_name} ({est}) falls outside its own CI "
                f"[{lo}, {hi}] (slack ±{slack:g}) — the estimate and its "
                f"interval are mutually inconsistent.",
            ))
    if n_events is not None and n_total is not None and n_events > n_total:
        failures.append(Failure(
            "events_exceed_total", row_loc,
            f"{events_name} ({n_events}) > {total_name} ({n_total}) — an event count "
            f"cannot exceed the denominator.",
        ))
    return failures


def _identity_from_fields(
    fields_list: list[Any], identity_dims: list[str]
) -> tuple[Any, ...] | None:
    """Pull the identity tuple out of a fields list. Returns None if any
    identity dim is missing or null. Tuples whose values include None
    are returned as-is so callers can decide how to handle them."""
    by_name: dict[str, Any] = {}
    for f in fields_list:
        if isinstance(f, dict) and isinstance(f.get("name"), str):
            by_name[f["name"]] = f.get("value")
    if not all(d in by_name for d in identity_dims):
        return None
    return tuple(by_name[d] for d in identity_dims)


def _validate_row(
    row: Any,
    *,
    row_index: int,
    schema: Schema,
    bundle: BundleSources,
    mode: str,  # "iter1" or "itern"
) -> list[Failure]:
    failures: list[Failure] = []

    if not isinstance(row, dict):
        failures.append(Failure(
            "shape_error", f"row[{row_index}]",
            f"row must be a dict, got {type(row).__name__}",
        ))
        return failures

    fields_list = row.get("fields")
    if not isinstance(fields_list, list) or len(fields_list) == 0:
        failures.append(Failure(
            "shape_error", f"row[{row_index}]",
            "row must have a non-empty 'fields' list",
        ))
        return failures

    identity_key = row.get("identity")
    has_identity_key = identity_key is not None

    # Itern rows MAY have an identity key (= modification target). If they
    # don't, they're new rows and must carry identity_dimensions in fields.
    # Iter1 rows MUST NOT carry an identity key.
    if mode == "iter1" and has_identity_key:
        failures.append(Failure(
            "shape_error", f"row[{row_index}]",
            "iter1 rows must not include an 'identity' key (merge "
            "semantics are itern-only)",
        ))

    if has_identity_key:
        if not isinstance(identity_key, list):
            failures.append(Failure(
                "shape_error", f"row[{row_index}]",
                "'identity' must be a list",
            ))
        elif len(identity_key) != len(schema.identity_dimensions):
            failures.append(Failure(
                "shape_error", f"row[{row_index}]",
                f"'identity' has {len(identity_key)} values, schema "
                f"declares {len(schema.identity_dimensions)} identity "
                f"dimensions: {schema.identity_dimensions}",
            ))
        identity_repr = repr(identity_key)
    else:
        identity_from_fields = _identity_from_fields(
            fields_list, schema.identity_dimensions
        )
        if identity_from_fields is None:
            failures.append(Failure(
                "shape_error", f"row[{row_index}]",
                f"new row missing one or more identity_dimensions: "
                f"{schema.identity_dimensions}",
            ))
            identity_repr = "<missing>"
        else:
            identity_repr = repr(list(identity_from_fields))

    row_loc = _row_loc(row_index, identity_repr)

    seen_names: set[str] = set()
    for entry in fields_list:
        failures.extend(_validate_field_entry(
            entry, schema=schema, row_loc=row_loc, bundle=bundle,
        ))
        if isinstance(entry, dict):
            n = entry.get("name")
            if isinstance(n, str):
                if n in seen_names:
                    failures.append(Failure(
                        "duplicate_field", row_loc,
                        f"field {n!r} appears more than once in this row",
                    ))
                seen_names.add(n)
                if n not in schema.field_names:
                    failures.append(Failure(
                        "unknown_field", row_loc,
                        f"field {n!r} is not declared in schema "
                        f"v{schema.version}",
                    ))

    if not has_identity_key:
        for missing_name in sorted(set(schema.field_names) - seen_names - schema.exempt_fields):
            failures.append(Failure(
                "unexplained_missing_field", f"{row_loc} field={missing_name}",
                "new rows must explicitly provide a value or missing(reason, explanation); automatic bare nulls are not allowed",
            ))

    # Internal-consistency over the assembled row's Standards Core
    # numerics (CI ordering, estimate-in-CI, events ≤ total).
    failures.extend(_check_internal_consistency(fields_list, row_loc=row_loc))

    return failures


# ────────────────────────────────────────────────────────────────────
# Top-level validators
# ────────────────────────────────────────────────────────────────────


def validate_rows_payload(
    payload: Any,
    *,
    schema: Schema,
    bundle: BundleSources,
    mode: str,  # "iter1" or "itern"
) -> list[Failure]:
    failures: list[Failure] = []

    if not isinstance(payload, dict):
        return [Failure(
            "shape_error", "rows-file",
            f"top-level must be a JSON object, got {type(payload).__name__}",
        )]

    declared_version = payload.get("schema_version")
    if declared_version != schema.version:
        failures.append(Failure(
            "schema_version_mismatch", "rows-file",
            f"rows.schema_version={declared_version!r} does not match "
            f"--schema v{schema.version!r}",
        ))

    declared_dims = payload.get("identity_dimensions")
    if declared_dims is not None and declared_dims != schema.identity_dimensions:
        failures.append(Failure(
            "identity_dimensions_mismatch", "rows-file",
            f"rows.identity_dimensions={declared_dims!r} does not match "
            f"schema identity_dimensions={schema.identity_dimensions!r}",
        ))

    rows = payload.get("rows")
    if not isinstance(rows, list):
        failures.append(Failure(
            "shape_error", "rows-file",
            "rows.rows must be a list",
        ))
        return failures
    if mode == "iter1" and len(rows) == 0:
        failures.append(Failure(
            "shape_error", "rows-file",
            "iter1 requires at least one row",
        ))

    for i, row in enumerate(rows):
        failures.extend(_validate_row(
            row, row_index=i, schema=schema, bundle=bundle, mode=mode,
        ))

    removed_fields = payload.get("removed_fields")
    if removed_fields is not None:
        if mode == "iter1":
            failures.append(Failure(
                "shape_error", "rows-file",
                "iter1 must not declare 'removed_fields' (no prior to "
                "remove from)",
            ))
        elif not isinstance(removed_fields, list) or not all(
            isinstance(s, str) for s in removed_fields
        ):
            failures.append(Failure(
                "shape_error", "rows-file.removed_fields",
                "must be a list of strings",
            ))

    removed_rows = payload.get("removed_rows")
    if removed_rows is not None:
        if mode == "iter1":
            failures.append(Failure(
                "shape_error", "rows-file",
                "iter1 must not declare 'removed_rows'",
            ))
        elif not isinstance(removed_rows, list):
            failures.append(Failure(
                "shape_error", "rows-file.removed_rows",
                "must be a list",
            ))
        else:
            for i, r in enumerate(removed_rows):
                if not isinstance(r, dict) or not isinstance(r.get("identity"), list):
                    failures.append(Failure(
                        "shape_error",
                        f"rows-file.removed_rows[{i}]",
                        "must be {'identity': [<values in identity_dim order>]}",
                    ))

    return failures


def validate_meta_payload(payload: Any, *, mode: str) -> list[Failure]:
    failures: list[Failure] = []
    if not isinstance(payload, dict):
        return [Failure(
            "shape_error", "meta-file",
            f"meta-file must be a JSON object, got {type(payload).__name__}",
        )]

    for key in payload.keys():
        if key in META_BOOKKEEPING_FIELDS:
            failures.append(Failure(
                "meta_bookkeeping_conflict", f"meta-file.{key}",
                "this field is filled deterministically by "
                "write_extraction.py — remove from meta-file",
            ))

    # Soft expectations — log if missing, don't fail.
    expected_keys = {
        "sources_used", "identity_dimension_values",
    }
    if mode == "itern":
        expected_keys.add("schema_diff_applied")
    for k in expected_keys:
        if k not in payload:
            failures.append(Failure(
                "meta_missing_field", f"meta-file.{k}",
                "expected field is missing — agent should declare this",
            ))

    return failures


def validate_prior_quotes(
    prior_rows: Iterable[dict],
    *,
    bundle: BundleSources,
) -> list[Failure]:
    """Re-validate every provenance quote in the prior JSONL against the
    current bundle. Run on itern to surface bundle drift early (OQ2:
    re-validate everything every time). Checks at the cited location
    and falls back to whole-bundle so legacy free-text locations still
    surface a genuinely-vanished quote."""
    failures: list[Failure] = []
    for i, row in enumerate(prior_rows):
        if not isinstance(row, dict):
            continue
        if row.get("_meta_commentary"):
            continue
        provenance = row.get("provenance") or {}
        if not isinstance(provenance, dict):
            continue
        for field_name, entry in provenance.items():
            if not isinstance(entry, dict):
                continue
            for j, src in enumerate(entry.get("sources") or []):
                if not isinstance(src, dict):
                    continue
                quote = src.get("quote")
                location = src.get("location", "")
                if not isinstance(quote, str) or not quote:
                    continue
                if FIGURE_LOCATION_PATTERN.search(location):
                    continue
                _key, loc_text = bundle.resolve_location(location)
                search_text = loc_text if loc_text is not None else bundle.full_text
                if not find_quote_in_source(quote, search_text):
                    failures.append(Failure(
                        "prior_quote_not_in_bundle",
                        f"prior_row[{i}] field={field_name} sources[{j}]",
                        f"prior quote no longer matches bundle "
                        f"(location={location!r}): {quote[:200]!r}",
                    ))
    return failures


_ROW_INDEX_RE = re.compile(r"row\[(\d+)\]")
_FIELD_RE = re.compile(r"field=([^\s:]+)")
_SOURCE_RE = re.compile(r"sources\[(\d+)\]")
_QUOTED_RE = re.compile(r": (?P<quote>['\"].*['\"])$")
_CITED_LOCATION_RE = re.compile(r"cited location (?P<loc>['\"][^'\"]+['\"])")
_CANDIDATE_LOCATIONS_RE = re.compile(r"candidate_locations=\[(?P<locs>[^\]]*)\]")


def _compact_ranges(values: list[int]) -> str:
    if not values:
        return "n/a"
    vals = sorted(set(values))
    ranges: list[str] = []
    start = prev = vals[0]
    for v in vals[1:]:
        if v == prev + 1:
            prev = v
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = v
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def _failure_group_key(f: Failure) -> tuple[str, str, str, str]:
    field_match = _FIELD_RE.search(f.location)
    source_match = _SOURCE_RE.search(f.location)
    field = field_match.group(1) if field_match else "(row)"
    source = source_match.group(1) if source_match else "*"
    return (f.code, field, source, f.detail)


def _shorten(text: str, *, limit: int = 96) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _summarize_candidate_locations(detail: str) -> str:
    match = _CANDIDATE_LOCATIONS_RE.search(detail)
    if not match:
        return ""
    locs = re.findall(r"['\"]([^'\"]+)['\"]", match.group("locs"))
    if not locs:
        return ""
    shown = locs[:2]
    suffix = f", +{len(locs) - len(shown)}" if len(locs) > len(shown) else ""
    return f" candidates={shown!r}{suffix};"


def _summarize_detail(code: str, detail: str) -> str:
    if code == "quote_not_at_location":
        status = (
            "found_elsewhere"
            if "quote_status=found_elsewhere" in detail
            else "not_found_anywhere"
            if "quote_status=not_found_anywhere" in detail
            else "unknown"
        )
        cited_match = _CITED_LOCATION_RE.search(detail)
        cited = f" cited={cited_match.group('loc')};" if cited_match else ""
        candidates = _summarize_candidate_locations(detail)
        quote_match = _QUOTED_RE.search(detail)
        quote = f" quote={_shorten(quote_match.group('quote'), limit=90)}" if quote_match else ""
        return f"quote_status={status};{cited}{candidates}{quote}"
    if code == "value_not_in_quote":
        return _shorten(detail, limit=180)
    if code.startswith("cell_"):
        return _shorten(detail, limit=180)
    return _shorten(detail, limit=220)


def hint_code_for_failure(code: str, detail: str = "") -> str | None:
    if code == "value_not_in_quote":
        return "PROV_LITERAL"
    if code in {
        "missing_source_components",
        "derived_operand_missing_component",
        "bad_source_components",
        "bad_source_component",
        "duplicate_source_component_label",
        "source_components_on_nonderived",
    }:
        return "DERIVED_COMPONENTS"
    if code.startswith("source_component_"):
        return "SOURCE_COMPONENT_LITERAL"
    if code.startswith("cell_"):
        return "CELL_ANCHOR"
    if code in {
        "quote_not_at_location",
        "quote_not_in_bundle",
        "prior_quote_not_in_bundle",
        "location_not_a_bundle_file",
    }:
        return "QUOTE_LOCATION"
    if code == "shape_error" and "null" in detail.lower():
        return "NULL_SHAPE"
    if code in {"abstention_missing_source", "abstention_bad_source"}:
        return "NULL_SHAPE"
    return None


def _render_failure_group(failures: list[Failure]) -> str:
    first = failures[0]
    field_match = _FIELD_RE.search(first.location)
    source_match = _SOURCE_RE.search(first.location)
    field = field_match.group(1) if field_match else "(row)"
    source = source_match.group(1) if source_match else "*"
    rows = [
        int(m.group(1))
        for failure in failures
        for m in [_ROW_INDEX_RE.search(failure.location)]
        if m
    ]
    row_part = f"rows[{_compact_ranges(rows)}]" if rows else "rows[n/a]"
    count_part = f"{len(failures)} occurrence(s)"
    hint = hint_code_for_failure(first.code, first.detail)
    hint_part = f" hint={hint}" if hint else ""
    return (
        f"[{first.code}] {row_part} field={field} sources[{source}]: "
        f"{count_part};{hint_part} {_summarize_detail(first.code, first.detail)}"
    )


def _render_report_context(context: dict[str, Any] | None) -> list[str]:
    if not context:
        return []
    lines: list[str] = []
    label = context.get("label")
    if label:
        lines.append(f"Validation target: {label}")
    rows_path = context.get("rows_path")
    if rows_path:
        lines.append(f"Rows file: {rows_path}")
    report_path = context.get("report_path")
    if report_path:
        lines.append(f"Machine report: {report_path}")
    if rows_path:
        lines.append("Rows are zero-based indexes within the rows file above.")
    elif label:
        lines.append("Rows are zero-based indexes within the submitted rows payload.")
    return lines


def render_failures(
    failures: list[Failure],
    *,
    max_shown: int = 12,
    context: dict[str, Any] | None = None,
) -> str:
    if not failures:
        return ""
    lines = [
        f"Extraction write blocked: {len(failures)} validation failure(s)."
    ]
    context_lines = _render_report_context(context)
    if context_lines:
        lines.extend(context_lines)
    by_code: dict[str, int] = {}
    for f in failures:
        by_code[f.code] = by_code.get(f.code, 0) + 1
    lines.append("By code: " + ", ".join(
        f"{code}={n}" for code, n in sorted(by_code.items())
    ))
    lines.append("")
    grouped: dict[tuple[str, str, str, str], list[Failure]] = {}
    for f in failures:
        grouped.setdefault(_failure_group_key(f), []).append(f)
    groups = sorted(
        grouped.values(),
        key=lambda items: (-len(items), items[0].code, items[0].location),
    )
    lines.append(
        f"Deduplicated clusters: {len(groups)}."
    )
    lines.append("")
    shown = groups[:max_shown]
    for group in shown:
        lines.append(f"  {_render_failure_group(group)}")
    if len(groups) > len(shown):
        remaining_failures = sum(len(g) for g in groups[len(shown):])
        lines.append(
            f"  ... and {len(groups) - len(shown)} more cluster(s) "
            f"covering {remaining_failures} validation failure(s)."
        )
    lines.append("")
    lines.append(
        "Fix: repair by cluster first. Hint-code definitions are in "
        "docs/extraction-sdk-agent-contract.md. Inspect the machine report or "
        "staged rows only when the clustered row/field context is insufficient."
    )
    return "\n".join(lines)


def failure_report(
    failures: list[Failure],
    *,
    max_groups: int | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Machine-readable companion to render_failures().

    The grouped report intentionally mirrors the compact text rendering so the
    extractor can get a short CLI response while tests, review packets, and
    future portal/export adapters can consume structured diagnostics.
    """
    by_code: dict[str, int] = {}
    for f in failures:
        by_code[f.code] = by_code.get(f.code, 0) + 1

    grouped: dict[tuple[str, str, str, str], list[Failure]] = {}
    for f in failures:
        grouped.setdefault(_failure_group_key(f), []).append(f)
    groups = sorted(
        grouped.values(),
        key=lambda items: (-len(items), items[0].code, items[0].location),
    )
    if max_groups is not None:
        groups = groups[:max_groups]

    out_groups: list[dict[str, Any]] = []
    for group in groups:
        first = group[0]
        field_match = _FIELD_RE.search(first.location)
        source_match = _SOURCE_RE.search(first.location)
        rows = [
            int(m.group(1))
            for failure in group
            for m in [_ROW_INDEX_RE.search(failure.location)]
            if m
        ]
        out_groups.append({
            "code": first.code,
            "hint_code": hint_code_for_failure(first.code, first.detail),
            "field": field_match.group(1) if field_match else None,
            "source_index": int(source_match.group(1)) if source_match else None,
            "count": len(group),
            "rows": sorted(set(rows)),
            "row_ranges": _compact_ranges(rows) if rows else None,
            "summary": _summarize_detail(first.code, first.detail),
            "first_location": first.location,
            "first_detail": first.detail,
            "examples": [
                {"location": f.location, "detail": f.detail}
                for f in group[:3]
            ],
        })

    return {
        "ok": not failures,
        "failure_count": len(failures),
        "by_code": dict(sorted(by_code.items())),
        "group_count": len(grouped),
        "context": context or {},
        "groups": out_groups,
    }
