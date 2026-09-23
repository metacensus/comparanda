#!/usr/bin/env python3
"""Portal snapshot exporter for the Comparanda reviewer portal.

Produces an immutable reviewer snapshot under the configured domain-data
repository's ``runs/<run_id>/`` directory.

Resolves provenance quotes to PDF highlight rects with PyMuPDF (fitz):
  - table sources -> docling table rect (via manifest docling_idx), search within
    that rect -> "exact"; else whole-table rect -> "table_region".
  - prose sources -> page.search_for with normalization + all-page scan;
    truncation is a visibly-marked partial fallback, never "exact".

Pure stdlib + pymupdf. Run from the canonical inner-loop directory:
  uv run --no-project --with pymupdf python scripts/review/portal_export.py \
      --project /path/to/domain-superproject --run-id candidate-001 \
      --out /path/to/data-repo/runs/candidate-001 \
      [--eval-dir /path/to/domain-superproject/iterations/iter_1] \
      [--review-queue /path/to/domain-superproject/review_queue.json] \
      [--eval-profile full|reviewer-demo] [--blind] \
      [--demo-config demo_config.json] [--highlight-paper PAPER_ID ...]

When ``comparanda-project.json`` defines ``export.runs``, ``--out`` may be
omitted. ``--project`` may likewise be supplied by COMPARANDA_PROJECT_DIR.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import json
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_layout import LayoutError, ProjectLayout  # noqa: E402

# Last-resort fallback identity tuple (round-3 edumix, 12 fields). Used ONLY
# when the schema has no `identity_dimensions:` and the queue carries no
# identity dicts. The authoritative source is the project schema (see
# `load_identity_dimensions`); queue items also carry their own `identity`
# dict which we union as a cross-check.
IDENTITY_FIELDS_FALLBACK = [
    "document_id",
    "design_paradigm",
    "result_group_id",
    "outcome_raw",
    "estimand_type",
    "session_timepoint",
    "arithmetic_operation",
    "metric_curve_type",
    "bullying_role",
    "bullying_mode",
    "subgroup_factor",
    "subgroup_level",
]

MAX_RECTS_PER_SOURCE = 30
SCHEMA_VERSION = "v001"
# On-disk snapshot format written by this exporter (see runtime FORMAT-V002.md).
# v002 moves per-row evidence copies into evidence/<paper_id>.json; rows keep
# integer references. manifest.schema_version (SCHEMA_VERSION, "v001") is a
# different, unchanged marker.
SNAPSHOT_FORMAT_VERSION = "v002"
# Resolution keys every manifest highlight_stats carries, defaulting to 0.
CANONICAL_RESOLUTIONS = (
    "exact", "partial_quote", "table_region", "figure_region", "page_only",
)

# A normalized quote at or below this length is treated as "short" — too weak to
# broad-search on its own (e.g. "0", "11", "wt"). Short/numeric quotes are
# resolved by cell-anchoring (tables) or surrounding context (prose), never by a
# bare page-wide search that would light up every matching glyph.
SHORT_QUOTE_LEN = 3
# Pure-numeric / punctuation-only quote (digits, separators, %, parens, signs).
_NUMERICISH_RE = re.compile(r"^[\d.,()%+\-/ ]+$")


def _is_short_or_numeric(nq: str) -> bool:
    """True when the normalized quote is too weak to broad-search verbatim."""
    if not nq:
        return True
    if len(nq) <= SHORT_QUOTE_LEN:
        return True
    return bool(_NUMERICISH_RE.match(nq))


# ---------------------------------------------------------------------------
# Identity dimensions (schema-driven)
# ---------------------------------------------------------------------------


def _latest_schema_path(project: Path):
    """Return the highest-versioned schema/v{NNN}.yaml under the project, or None.

    Picks the lexically-greatest v*.yaml so v010 > v009 (zero-padded names sort
    correctly). Falls back to any *.yaml if no v-prefixed file exists.
    """
    schema_dir = project / "schema"
    if not schema_dir.exists():
        return None
    versioned = sorted(schema_dir.glob("v*.yaml"))
    if versioned:
        return versioned[-1]
    other = sorted(schema_dir.glob("*.yaml"))
    return other[-1] if other else None


def _parse_identity_dimensions_yaml(text: str):
    """Minimal parser for a schema's top-level `identity_dimensions:` block.

    Returns a list of {"field": str, "description": str}. Pure stdlib so the
    exporter keeps its (stdlib + pymupdf)-only footprint. Handles the two forms
    the schema uses:
        - field: document_id
          description: "..."          # single-line
        - field: zygosity
          description: >              # YAML folded block over indented lines
            multi-line text...
    Stops at the next top-level key (e.g. `fields:`).
    """
    lines = text.splitlines()
    # find the `identity_dimensions:` line (top-level, no indent)
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^identity_dimensions\s*:", ln):
            start = i + 1
            break
    if start is None:
        return []

    dims = []
    cur = None  # current {"field", "description"} being built
    desc_mode = None  # None | "folded" | "inline-cont"
    desc_indent = None

    def _flush():
        nonlocal cur
        if cur is not None and cur.get("field"):
            cur["description"] = re.sub(r"\s+", " ", cur.get("description", "")).strip()
            dims.append(cur)
        cur = None

    for ln in lines[start:]:
        # a new top-level key (column 0, not a list item) ends the block
        if ln and not ln[0].isspace() and not ln.lstrip().startswith("#"):
            break
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue

        m_item = re.match(r"^(\s*)-\s+field\s*:\s*(.+?)\s*$", ln)
        if m_item:
            _flush()
            field = m_item.group(2).strip().strip('"').strip("'")
            cur = {"field": field, "description": ""}
            desc_mode = None
            continue

        m_desc = re.match(r"^(\s*)description\s*:\s*(.*)$", ln)
        if m_desc and cur is not None:
            rest = m_desc.group(2).strip()
            if rest in (">", "|", ">-", "|-", ">+", "|+"):
                desc_mode = "folded"
                desc_indent = None
            else:
                cur["description"] = rest.strip().strip('"').strip("'")
                desc_mode = None
            continue

        # continuation lines of a folded/block description
        if desc_mode == "folded" and cur is not None:
            if desc_indent is None:
                desc_indent = len(ln) - len(ln.lstrip())
            cur["description"] = (cur.get("description", "") + " " + stripped).strip()
            continue

    _flush()
    return dims


def load_identity_dimensions(project: Path):
    """Resolve the project's identity dimensions, schema-first.

    Returns (dims, fields, source) where:
      - dims: list of {"field", "description"} (schema order)
      - fields: ordered list of identity field names
      - source: "schema" | "fallback"
    """
    schema_path = _latest_schema_path(project)
    if schema_path is not None:
        try:
            dims = _parse_identity_dimensions_yaml(schema_path.read_text())
        except OSError:
            dims = []
        if dims:
            return dims, [d["field"] for d in dims], "schema"
    # last-resort fallback
    dims = [{"field": f, "description": ""} for f in IDENTITY_FIELDS_FALLBACK]
    return dims, list(IDENTITY_FIELDS_FALLBACK), "fallback"


# ---------------------------------------------------------------------------
# Schema fields (for the Schema explorer) — minimal stdlib YAML parsing
# ---------------------------------------------------------------------------

# scalar keys we lift verbatim from each `- name:` field block.
_FIELD_SCALAR_KEYS = ("name", "type", "required", "identity_dimension")
# block/inline keys that carry prose (folded `>` or `|`).
_FIELD_TEXT_KEYS = ("extraction_guidance", "description")


def _parse_schema_fields_yaml(text: str):
    """Parse the top-level `fields:` block into list[{name,type,required,
    identity_dimension,description}]. Minimal parser (stdlib-only, mirrors the
    identity-dimensions parser) — handles inline scalars and folded `>`/`|`
    description/extraction_guidance blocks. Enum vocab lives in the guidance
    prose in this schema, so we surface it as `description` and rely on the
    OBSERVED value distribution for the actual token set."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^fields\s*:", ln):
            start = i + 1
            break
    if start is None:
        return []

    fields = []
    cur = None
    text_mode = False  # inside a folded/block scalar
    text_key = None

    def _flush():
        nonlocal cur
        if cur is not None and cur.get("name"):
            desc = cur.pop("_extraction_guidance", "") or cur.pop("_description", "")
            cur["description"] = re.sub(r"\s+", " ", desc).strip()
            fields.append(cur)
        cur = None

    for ln in lines[start:]:
        # a new top-level key (column 0, not a comment) ends the block
        if ln and not ln[0].isspace() and not ln.lstrip().startswith("#"):
            break
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue

        m_item = re.match(r"^(\s*)-\s+name\s*:\s*(.+?)\s*$", ln)
        if m_item:
            _flush()
            name = m_item.group(2).strip().strip('"').strip("'")
            cur = {"name": name, "type": None, "required": None,
                   "identity_dimension": None}
            text_mode = False
            continue
        if cur is None:
            continue

        m_kv = re.match(r"^\s{2,}([a-z_]+)\s*:\s*(.*)$", ln)
        if m_kv:
            key, rest = m_kv.group(1), m_kv.group(2).strip()
            if key in _FIELD_SCALAR_KEYS and key != "name":
                v = rest.strip().strip('"').strip("'")
                if key in ("required", "identity_dimension"):
                    cur[key] = (v.lower() == "true")
                else:
                    cur[key] = v
                text_mode = False
                continue
            if key in _FIELD_TEXT_KEYS:
                text_key = "_" + key
                if rest in (">", "|", ">-", "|-", ">+", "|+"):
                    text_mode = True
                    cur[text_key] = cur.get(text_key, "")
                else:
                    cur[text_key] = rest.strip().strip('"').strip("'")
                    text_mode = False
                continue
            # any other scalar key ends a folded block
            text_mode = False
            continue

        if text_mode and text_key:
            cur[text_key] = (cur.get(text_key, "") + " " + stripped).strip()

    _flush()
    return fields


def load_schema_fields(project: Path):
    """Return (fields, schema_version) parsed from the latest schema, or ([], None)."""
    schema_path = _latest_schema_path(project)
    if schema_path is None:
        return [], None
    try:
        text = schema_path.read_text()
    except OSError:
        return [], None
    fields = _parse_schema_fields_yaml(text)
    m = re.search(r'^schema_version\s*:\s*["\']?([^"\'\n]+)', text, re.MULTILINE)
    version = m.group(1).strip() if m else schema_path.stem
    return fields, version


def build_schema_summary(project: Path, rows_by_paper: dict, top_n: int = 25):
    """Build a bounded schema summary: per-field type/description + the OBSERVED
    value distribution across the run.

    `rows_by_paper`: {paper_id: [field_dict, ...]} — the non-provenance fields of
    each extracted row (so we can compute fill rate / token distribution /
    numeric range / how many papers populate each field).
    """
    fields, version = load_schema_fields(project)
    field_meta = {f["name"]: f for f in fields}

    # gather observed values per field
    observed = {}  # field -> {"non_null":int,"total":int,"papers":set,"counter":Counter,"nums":[]}

    def _slot(name):
        s = observed.get(name)
        if s is None:
            s = {"non_null": 0, "total": 0, "papers": set(),
                 "counter": Counter(), "nums": []}
            observed[name] = s
        return s

    for pid, rows in rows_by_paper.items():
        for fld_dict in rows:
            for name, val in fld_dict.items():
                s = _slot(name)
                s["total"] += 1
                if val is None or val == "":
                    continue
                s["non_null"] += 1
                s["papers"].add(pid)
                if isinstance(val, bool):
                    s["counter"][str(val).lower()] += 1
                elif isinstance(val, (int, float)):
                    s["nums"].append(val)
                    s["counter"][str(val)] += 1
                elif isinstance(val, str):
                    s["counter"][val] += 1
                elif isinstance(val, list):
                    for v in val:
                        s["counter"][str(v)] += 1
                else:
                    s["counter"][str(val)] += 1

    out_fields = []
    # iterate schema order first, then any observed-only fields
    names = [f["name"] for f in fields] + [
        n for n in observed if n not in field_meta
    ]
    for name in names:
        meta = field_meta.get(name, {})
        s = observed.get(name, {"non_null": 0, "total": 0, "papers": set(),
                                "counter": Counter(), "nums": []})
        ftype = meta.get("type")
        entry = {
            "name": name,
            "type": ftype,
            "required": meta.get("required"),
            "identity_dimension": meta.get("identity_dimension"),
            "description": _truncate(meta.get("description", ""), 400) or None,
            "non_null_count": s["non_null"],
            "total_count": s["total"],
            "fill_rate": round(s["non_null"] / s["total"], 4) if s["total"] else 0.0,
            "papers_populating": len(s["papers"]),
            "distinct_values": len(s["counter"]),
        }
        nums = s["nums"]
        # numeric range for numeric-typed fields (or fields that are all-numeric)
        if ftype in ("integer", "number") and nums:
            entry["numeric"] = {
                "min": min(nums),
                "max": max(nums),
                "count_non_null": len(nums),
            }
        # top-N token distribution (bounded) for everything with categorical-ish
        # cardinality; skip the long tail for high-cardinality free text.
        if s["counter"]:
            entry["top_values"] = [
                {"value": v, "count": c}
                for v, c in s["counter"].most_common(top_n)
            ]
        out_fields.append(entry)

    return {
        "schema_version": version,
        "field_count": len(fields),
        "top_n": top_n,
        "fields": out_fields,
    }


# ---------------------------------------------------------------------------
# Quote normalization
# ---------------------------------------------------------------------------

# unicode -> ascii canonicalization for PDF search.
_UNICODE_MAP = {
    "’": "'", "‘": "'", "ʼ": "'",           # right/left single quote, modifier apostrophe
    "“": '"', "”": '"',                            # curly double quotes
    "−": "-", "–": "-", "—": "-",           # minus, en-dash, em-dash
    " ": " ", " ": " ", " ": " ", " ": " ",  # nbsp, thin/hair/figure space
    "ﬁ": "fi", "ﬂ": "fl",                          # fi / fl ligatures
}
_TRANS = {ord(k): v for k, v in _UNICODE_MAP.items()}

# markdown emphasis / inline code / table-cell artifacts to strip from quotes.
_MD_EMPH = re.compile(r"[*`]")


def normalize_quote(text: str) -> str:
    """Strip markdown emphasis, canonicalize unicode, collapse whitespace.

    Also removes markdown-table syntax (leading/trailing pipes, separator
    rows) so quotes lifted from `tables/table_N.md` are searchable.
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = s.translate(_TRANS)
    s = _MD_EMPH.sub("", s)
    # drop markdown table pipes and cell padding
    s = s.replace("|", " ")
    # collapse whitespace (incl. newlines)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# collapse whitespace around the variant operators '+' and '>' so a quote that
# reads "c.1905 + 1G>A" reconciles with a PDF glyph run "c.1905+1G>A" (and the
# inverse). HGVS-style variant names are routinely re-spaced by the extractor.
_OP_SPACE_RE = re.compile(r"\s*([+>])\s*")


def _collapse_op_spaces(q: str) -> str:
    return _OP_SPACE_RE.sub(r"\1", q)


def _dash_variants(q: str):
    """Yield the normalized quote plus dash / operator-spacing variants.

    The PDF may use en-dash / em-dash where the quote used ascii hyphen
    (verified: '2022-23' in quote vs '2022–23' in PDF). search_for matches
    against the page's actual glyphs, so we must try the dash the PDF holds.
    Also collapses spaces around '+'/'>' so "c.1905 + 1G>A" matches
    "c.1905+1G>A" (Joerger variant-name re-spacing).
    """
    seen = set()

    def _emit(v):
        if v and v not in seen:
            seen.add(v)
            return True
        return False

    if _emit(q):
        yield q
    collapsed = _collapse_op_spaces(q)
    if _emit(collapsed):
        yield collapsed
    for base in (q, collapsed):
        if "-" in base:
            for v in (base.replace("-", "–"), base.replace("-", "—")):
                if _emit(v):
                    yield v


# search flag: dehyphenate across line breaks when available.
_DEHYPHENATE = getattr(fitz, "TEXT_DEHYPHENATE", 0)


def _search(page, query, clip=None, *, textpage=None, metrics=None, phase=None):
    """search_for with dash variants + dehyphenate. Returns list[fitz.Rect]."""
    if not query:
        return []
    if textpage is not None and clip is not None:
        raise ValueError("a full-page TextPage cannot be used for a clipped search")
    for variant in _dash_variants(query):
        if metrics is not None:
            metrics["search_for_calls"] += 1
            metrics[
                "prose_search_for_calls" if textpage is not None
                else "clipped_search_for_calls" if clip is not None
                else "direct_search_for_calls"
            ] += 1
            if phase:
                metrics[f"{phase}_search_for_calls"] += 1
        try:
            kwargs = {"clip": clip, "flags": _DEHYPHENATE}
            if textpage is not None:
                kwargs["textpage"] = textpage
            hits = page.search_for(variant, **kwargs)
        except TypeError:
            # Compatibility with PyMuPDF releases predating the flags keyword.
            # Cached TextPages are built with the same fallback extraction
            # behavior, so direct and cached searches retain matching semantics.
            kwargs = {"clip": clip}
            if textpage is not None:
                kwargs["textpage"] = textpage
            hits = page.search_for(variant, **kwargs)
        if hits:
            return hits
    return []


def _word_key(token: str) -> str:
    """Comparable PDF word key without weakening numeric evidence.

    Punctuation around ordinary prose words is presentation noise, but decimal
    points, signs, and separators in tokens containing digits are evidence and
    must remain exact.  This deliberately is not a fuzzy matcher.
    """
    token = normalize_quote(token).lower()
    if not token:
        return ""
    if any(char.isdigit() for char in token):
        return token
    if token.endswith("-"):
        return re.sub(r"^[^\w]+", "", token)
    return re.sub(r"^[^\w]+|[^\w]+$", "", token)


def _full_quote_word_match(page, query: str, clip=None, words=None):
    """Return rects for one unambiguous, word-exact full-quote match.

    This is a bounded fallback for PDF line wrapping and extraction spacing.
    It only accepts distinctive prose quotes and maps the matching PDF words
    back to their actual rectangles.  A trailing line-break hyphen may join the
    immediately following word; all other token comparisons are exact.
    """
    query = normalize_quote(query)
    query_words = [_word_key(word) for word in re.findall(r"\S+", query)]
    query_words = [word for word in query_words if word]
    alpha_count = sum(char.isalpha() for word in query_words for char in word)
    if len(query_words) < 2 or alpha_count < 12:
        return []
    words = page.get_text("words", sort=True) if words is None else words
    if clip is not None:
        words = [word for word in words if fitz.Rect(word[:4]).intersects(clip)]
    entries = []
    index = 0
    while index < len(words):
        word = words[index]
        key = _word_key(word[4])
        # A PDF often exposes a line-end "hyphen-" and next-line "ation" as
        # separate words. Join only this adjacent, lossless variant.
        if key.endswith("-") and index + 1 < len(words):
            next_key = _word_key(words[index + 1][4])
            if next_key:
                entries.append((key[:-1] + next_key, index, index + 1))
                index += 2
                continue
        if key:
            entries.append((key, index, index))
        index += 1
    matches = []
    for start in range(len(entries)):
        pos = start
        rect_indexes = []
        for wanted in query_words:
            if pos >= len(entries) or entries[pos][0] != wanted:
                break
            rect_indexes.extend(range(entries[pos][1], entries[pos][2] + 1))
            pos += 1
        else:
            matches.append(tuple(dict.fromkeys(rect_indexes)))
    if len(matches) != 1:
        return []
    return [fitz.Rect(words[index][:4]) for index in matches[0]]


def _full_quote_compact_match(page, query: str, clip=None, words=None):
    """Match one distinctive quote after removing whitespace only.

    PDF extraction can lose or introduce spaces inside an otherwise verbatim
    quote (``n Z`` versus ``nZ``). This retains every punctuation mark and
    digit, accepts exactly one whole-token-bounded occurrence, and returns the
    underlying word boxes. It is intentionally narrower than fuzzy matching.
    """
    query = normalize_quote(query)
    if sum(char.isalpha() for char in query) < 12:
        return []
    words = page.get_text("words", sort=True) if words is None else words
    if clip is not None:
        words = [word for word in words if fitz.Rect(word[:4]).intersects(clip)]
    if not words:
        return []
    compact_words = []
    spans = []
    offset = 0
    for index, word in enumerate(words):
        text = normalize_quote(word[4])
        if not text:
            continue
        compact_words.append(text)
        spans.append((offset, offset + len(text), index))
        offset += len(text)
    haystack = "".join(compact_words)
    if not haystack:
        return []
    for variant in _dash_variants(query):
        needle = re.sub(r"\s+", "", variant)
        if not needle:
            continue
        starts = []
        start = haystack.find(needle)
        while start >= 0:
            end = start + len(needle)
            left_ok = not (needle[0].isalnum() and start and haystack[start - 1].isalnum())
            right_ok = not (
                needle[-1].isalnum() and end < len(haystack) and haystack[end].isalnum()
            )
            if left_ok and right_ok:
                starts.append((start, end))
            start = haystack.find(needle, start + 1)
        if len(starts) == 1:
            start, end = starts[0]
            return [fitz.Rect(words[index][:4]) for lo, hi, index in spans if lo < end and hi > start]
    return []


def _prepared_character_index(raw_chars, clip=None):
    """Build compact normalized characters and their PDF boxes once."""
    chars = []
    for raw, bbox_values in raw_chars:
        bbox = fitz.Rect(bbox_values)
        if clip is None or bbox.intersects(clip):
            chars.extend((output, bbox) for output in normalize_quote(raw))
    return (
        "".join(char for char, _ in chars if not char.isspace()),
        tuple(bbox for char, bbox in chars if not char.isspace()),
    )


def _full_quote_character_match(
    page, query: str, clip=None, raw_chars=None, prepared_index=None,
):
    """Match one distinctive full quote in PDF character runs.

    Some PDFs expose an entire sentence as a single text word (for example,
    ``trialassigned410patients``). Raw character boxes preserve the only safe
    geometry in that case. Whitespace alone is ignored; every other character
    remains exact. A unique complete quote is required: this is neither fuzzy
    matching nor a numeric-only search.
    """
    query = normalize_quote(query)
    if sum(char.isalpha() for char in query) < 12:
        return []
    if raw_chars is None:
        raw_chars = []
        for block in page.get_text("rawdict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        raw_chars.append((char["c"], tuple(char["bbox"])))
    if prepared_index is None:
        prepared_index = _prepared_character_index(raw_chars, clip)
    compact, compact_boxes = prepared_index
    if not compact:
        return []
    for variant in _dash_variants(query):
        needle = re.sub(r"\s+", "", variant)
        if not needle:
            continue
        starts = []
        start = compact.find(needle)
        while start >= 0:
            starts.append(start)
            start = compact.find(needle, start + 1)
        if len(starts) == 1:
            start = starts[0]
            return _merge_character_rects(compact_boxes[start:start + len(needle)])
    return []


def _merge_character_rects(rects):
    """Coalesce glyph boxes into nearby spans without crossing columns."""
    merged = []
    for rect in sorted((fitz.Rect(rect) for rect in rects), key=lambda r: (r.y0, r.x0)):
        if merged:
            previous = merged[-1]
            same_line = abs(previous.y0 - rect.y0) <= max(1.0, previous.height * .25)
            close = rect.x0 - previous.x1 <= max(2.0, previous.height * .35)
            if same_line and close:
                merged[-1] = previous | rect
                continue
        merged.append(rect)
    return merged


def _union_rect(rects):
    """Bounding box (fitz.Rect) covering all input rects, or None."""
    if not rects:
        return None
    u = fitz.Rect(rects[0])
    for r in rects[1:]:
        u |= fitz.Rect(r)
    return u


def _is_synthetic_col(label: str) -> bool:
    """True for synthetic docling column ids like 'col_3' that aren't real text."""
    return bool(re.fullmatch(r"col_\d+", normalize_quote(label or "")))


def _anchor_cell(
    page, table_rect, cell, value_query, *, metrics=None, clipped_search=None,
):
    """Resolve a table cell to a tight value rect by row × column intersection.

    Searches the row-label text within the table rect to get its y-band, the
    column-header text to get its x-band, intersects them into a sub-clip, and
    searches `value_query` inside that sub-clip. Returns list[fitz.Rect] (the
    value hits), or [] if the cell can't be anchored.

    This is what stops a short/numeric value (e.g. "0", "11 (23)") from matching
    every like cell in the table: the value is only accepted inside the one
    row×column band the provenance names.
    """
    if not cell:
        return []
    row_label = cell.get("row")
    col_label = cell.get("column")
    display_table_rect = fitz.Rect(table_rect) * page.rotation_matrix

    def _clipped(query, clip):
        if clipped_search is not None:
            return clipped_search(page, query, clip)
        return _search(
            page, query, clip=clip, metrics=metrics, phase="table",
        )

    # Row band: y-range of the row-label text within the table.
    y0 = display_table_rect.y0
    y1 = display_table_rect.y1
    if row_label and not _is_synthetic_col(row_label):
        row_hits = _clipped(normalize_quote(row_label), table_rect)
        if row_hits:
            ru = _union_rect([r * page.rotation_matrix for r in row_hits])
            # pad a little so wrapped / multi-line labels still cover the data row
            pad = max(2.0, (ru.y1 - ru.y0) * 0.6)
            y0 = ru.y0 - pad
            y1 = ru.y1 + pad

    # Column band: x-range of the column-header text within the table.
    x0 = display_table_rect.x0
    x1 = display_table_rect.x1
    if col_label and not _is_synthetic_col(col_label):
        col_hits = _clipped(normalize_quote(col_label), table_rect)
        if col_hits:
            cu = _union_rect([r * page.rotation_matrix for r in col_hits])
            pad = max(2.0, (cu.x1 - cu.x0) * 0.25)
            x0 = cu.x0 - pad
            x1 = cu.x1 + pad

    # Need at least one of the two bands to have narrowed the clip; otherwise
    # this is just a whole-table search and offers no disambiguation.
    narrowed = (y0 > display_table_rect.y0 or y1 < display_table_rect.y1
                or x0 > display_table_rect.x0 or x1 < display_table_rect.x1)
    if not narrowed:
        return []

    display_sub = fitz.Rect(x0, y0, x1, y1)
    display_sub.normalize()
    display_sub = display_sub & display_table_rect
    if display_sub.is_empty:
        return []
    sub = display_sub * page.derotation_matrix
    sub.normalize()
    return _clipped(value_query, sub) if value_query else []


# ---------------------------------------------------------------------------
# Rect helpers
# ---------------------------------------------------------------------------


def _norm_rects(rects, page):
    """Normalize unrotated PyMuPDF rects into the displayed page coordinate frame.

    PyMuPDF search and clip coordinates are always unrotated, while PDF.js
    displays the page's declared rotation. Apply the page rotation matrix before
    normalizing against ``page.rect`` so the stored overlay matches the canvas.
    """
    page_rect = page.rect
    page_w, page_h = page_rect.width, page_rect.height
    out = []
    for r in rects:
        displayed = fitz.Rect(r) * page.rotation_matrix
        out.append({
            "x": (displayed.x0 - page_rect.x0) / page_w,
            "y": (displayed.y0 - page_rect.y0) / page_h,
            "w": displayed.width / page_w,
            "h": displayed.height / page_h,
        })
    if len(out) > MAX_RECTS_PER_SOURCE:
        out = _merge_same_line(out)
    if len(out) > MAX_RECTS_PER_SOURCE:
        out = out[:MAX_RECTS_PER_SOURCE]
    return out


def _merge_same_line(rects):
    """Merge horizontally-adjacent rects that share a line (same y/h)."""
    merged = []
    for r in sorted(rects, key=lambda d: (round(d["y"], 4), d["x"])):
        if merged:
            prev = merged[-1]
            same_line = abs(prev["y"] - r["y"]) < 1e-3 and abs(prev["h"] - r["h"]) < 1e-3
            adjacent = r["x"] - (prev["x"] + prev["w"]) < 0.02
            if same_line and adjacent:
                right = max(prev["x"] + prev["w"], r["x"] + r["w"])
                prev["x"] = min(prev["x"], r["x"])
                prev["w"] = right - prev["x"]
                continue
        merged.append(dict(r))
    return merged


def _group_by_page(rects, page_no, page):
    """All rects share one page here; return the SourceLocation highlight array."""
    norm = _norm_rects(rects, page)
    return [{"page": page_no, "rects": norm}]


# ---------------------------------------------------------------------------
# Bundle / docling table mapping
# ---------------------------------------------------------------------------


class Bundle:
    """Lazy holder for one paper's bundle + PDF."""

    def __init__(self, paper_id: str, bundle_dir: Path, pdf_path: Path):
        self.paper_id = paper_id
        self.bundle_dir = bundle_dir
        self.project_dir = bundle_dir.parents[2]
        self.pdf_path = pdf_path
        self.manifest = json.loads((bundle_dir / "manifest.json").read_text())
        # location (e.g. "tables/table_1.json" or its sibling .md) -> docling_idx
        self._loc_to_docling = {}
        self._table_by_location = {}
        for t in self.manifest.get("tables", []):
            di = t.get("docling_idx")
            for key in ("path", "markdown_path"):
                p = t.get(key)
                if p:
                    self._table_by_location[p] = t
                    if di is not None:
                        self._loc_to_docling[p] = di
        self._section_by_location = {
            section.get("path"): section
            for section in self.manifest.get("sections", [])
            if section.get("path")
        }
        self._figure_by_location = {
            figure.get("path"): figure
            for figure in self.manifest.get("figures", [])
            if figure.get("path")
        }
        # docling tables (index -> entry)
        dpath = bundle_dir / "_provenance" / "docling_tables.json"
        self._docling = {}
        if dpath.exists():
            for entry in json.loads(dpath.read_text()).get("tables", []):
                self._docling[entry.get("index")] = entry
        ppath = bundle_dir / "_provenance" / "docling_pictures.json"
        self._pictures = {}
        if ppath.exists():
            for entry in json.loads(ppath.read_text()).get("pictures", []):
                self._pictures[entry.get("index")] = entry
        self._documents = {}
        self._pdf_hashes = {}
        self._table_rect_cache = {}
        self._figure_rect_cache = {}
        self._source_resolution_cache = {}
        # TextPage retains a weak reference to its Page, so keep both alive.
        # These are full-page objects and are used only by prose_search().
        self._full_page_textpages = {}
        # Prose resolution never narrows to field_value, so distinct source
        # cache keys can safely share exact (page, normalized-query) searches.
        # Store immutable coordinates and return fresh Rects to each caller.
        self._prose_search_result_cache = {}
        self._word_quote_result_cache = {}
        self._page_words_cache = {}
        self._page_raw_chars_cache = {}
        self._page_character_index_cache = {}
        # Clipped searches remain ordinary Page.search_for calls. Exact clip
        # coordinates are part of the key so numeric cell boundaries cannot
        # alias one another.
        self._clipped_search_result_cache = {}
        self.diagnostics = Counter()

    @property
    def has_pdf(self) -> bool:
        """True when the source PDF is present (PDFs are gitignored, so a
        data-only clone may lack them — blind exports degrade gracefully)."""
        return self.pdf_path.exists()

    @property
    def doc(self):
        return self._document("primary")["doc"]

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _register_primary(self):
        current = self._documents.get("primary")
        if current is not None and current["path"] == self.pdf_path:
            return current
        if current is not None and current.get("doc") is not None:
            current["doc"].close()
        descriptor = {
            "key": "primary", "role": "primary", "path": self.pdf_path,
            "pdf": f"pdfs/{self.paper_id}.pdf", "sha256": None,
            "page_count": 0, "doc": None,
        }
        if self.pdf_path.exists():
            descriptor["sha256"] = self._sha256(self.pdf_path)
            descriptor["doc"] = fitz.open(self.pdf_path)
            descriptor["page_count"] = descriptor["doc"].page_count
        self._documents["primary"] = descriptor
        return descriptor

    def _resolve_supplement(self, table: dict):
        expected = table.get("source_sha256")
        artifact = table.get("source_artifact")
        if not artifact or not expected:
            raise ValueError(
                f"{self.paper_id}: supplementary table metadata requires "
                "source_artifact and source_sha256"
            )
        key = f"supplement:{expected}"
        if key in self._documents:
            return self._documents[key]
        candidates = []
        rel = Path(artifact)
        if not rel.is_absolute():
            direct = self.project_dir / rel
            if direct.is_file():
                candidates.append(direct)
        raw_dir = self.project_dir / "data" / "raw"
        if raw_dir.is_dir():
            candidates.extend(sorted(raw_dir.rglob("*.pdf")))
        seen = set()
        matched = None
        mismatched_direct = None
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            actual = self._pdf_hashes.get(resolved)
            if actual is None:
                actual = self._sha256(resolved)
                self._pdf_hashes[resolved] = actual
            if candidate == (self.project_dir / rel) and actual != expected:
                mismatched_direct = actual
            if actual == expected:
                matched = resolved
                break
        if matched is None:
            detail = (
                f"; source_artifact hash was {mismatched_direct}"
                if mismatched_direct else ""
            )
            raise FileNotFoundError(
                f"{self.paper_id}: supplement PDF with sha256 {expected} not found "
                f"under {raw_dir}{detail}"
            )
        doc = fitz.open(matched)
        descriptor = {
            "key": key, "role": "supplement", "path": matched,
            "pdf": f"pdfs/{self.paper_id}/supplement-{expected[:12]}.pdf",
            "sha256": expected, "page_count": doc.page_count, "doc": doc,
        }
        self._documents[key] = descriptor
        return descriptor

    def prepare_documents(self):
        """Resolve and hash every primary/supplement PDF before source work."""
        self._register_primary()
        for table in self.manifest.get("tables", []):
            if table.get("source_artifact"):
                self._resolve_supplement(table)

    def _document(self, key: str):
        self.prepare_documents()
        return self._documents[key]

    @staticmethod
    def _mapped_page(table: dict, page_no):
        if not isinstance(page_no, int) or page_no < 1:
            return None
        virtual = table.get("page_range") or [table.get("page"), table.get("page")]
        actual = table.get("supplement_page_range") or virtual
        if (
            len(virtual) != 2 or len(actual) != 2
            or not all(isinstance(v, int) for v in [*virtual, *actual])
            or virtual[1] - virtual[0] != actual[1] - actual[0]
        ):
            raise ValueError("invalid table page_range/supplement_page_range mapping")
        if not virtual[0] <= page_no <= virtual[1]:
            raise ValueError(
                f"source page {page_no} is outside table page_range {virtual}"
            )
        return actual[0] + page_no - virtual[0]

    def source_context(self, source: dict):
        location = source.get("location", "") or ""
        table = self._table_by_location.get(location)
        section = self._section_by_location.get(location)
        raw_page = source.get("page")
        if table and table.get("source_artifact"):
            descriptor = self._resolve_supplement(table)
            page_no = self._mapped_page(table, raw_page)
            actual_range = table.get("supplement_page_range") or table.get("page_range")
            allowed_pages = list(range(actual_range[0], actual_range[1] + 1))
        else:
            descriptor = self._register_primary()
            page_no = raw_page if isinstance(raw_page, int) and raw_page >= 1 else None
            allowed_pages = None
        preferred_pages = None
        if section:
            page_range = section.get("page_range")
            if (isinstance(page_range, list) and len(page_range) == 2
                    and all(isinstance(page, int) for page in page_range)):
                preferred_pages = list(range(page_range[0], page_range[1] + 1))
        return {
            "key": descriptor["key"], "doc": descriptor["doc"],
            "pdf": descriptor["pdf"], "page": page_no,
            "allowed_pages": allowed_pages, "preferred_pages": preferred_pages,
            "table": table,
        }

    def document_catalog(self):
        self.prepare_documents()
        return [
            {k: document[k] for k in ("pdf", "sha256", "page_count", "role")}
            for document in self._documents.values()
        ]

    def copy_documents(self, out: Path):
        self.prepare_documents()
        for document in self._documents.values():
            if not document["path"].is_file():
                continue
            destination = out / document["pdf"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(document["path"], destination)

    def close(self):
        self._clipped_search_result_cache.clear()
        self._prose_search_result_cache.clear()
        self._word_quote_result_cache.clear()
        self._page_words_cache.clear()
        self._page_raw_chars_cache.clear()
        self._page_character_index_cache.clear()
        self._full_page_textpages.clear()
        for document in self._documents.values():
            if document.get("doc") is not None:
                document["doc"].close()
                document["doc"] = None

    def page_count(self):
        if not self.has_pdf:
            return 0
        return self._register_primary()["page_count"]

    def prose_search(self, document_key: str, page_index: int, query: str, *, phase: str):
        """Search unclipped prose using one flags-matched TextPage per page."""
        result_key = (document_key, page_index, query)
        if result_key in self._prose_search_result_cache:
            self.diagnostics["prose_search_result_cache_hits"] += 1
            page, _ = self._full_page_textpages[(document_key, page_index)]
            coordinates = self._prose_search_result_cache[result_key]
            return page, [fitz.Rect(values) for values in coordinates]
        self.diagnostics["prose_search_result_cache_misses"] += 1
        page_key = (document_key, page_index)
        cached = self._full_page_textpages.get(page_key)
        if cached is None:
            page = self._document(document_key)["doc"][page_index]
            started = time.perf_counter()
            try:
                textpage = page.get_textpage(flags=_DEHYPHENATE)
            except TypeError:
                textpage = page.get_textpage()
            self.diagnostics["textpage_builds"] += 1
            self.diagnostics["textpage_build_seconds"] += time.perf_counter() - started
            cached = (page, textpage)
            self._full_page_textpages[page_key] = cached
        page, textpage = cached
        hits = _search(
            page, query, textpage=textpage, metrics=self.diagnostics, phase=phase,
        )
        self._prose_search_result_cache[result_key] = tuple(
            tuple(rect) for rect in hits
        )
        return page, [fitz.Rect(values) for values in self._prose_search_result_cache[result_key]]

    def clipped_search(self, document_key: str, page, query: str, clip):
        """Memoize an exact clipped search without using a full-page TextPage."""
        result_key = (document_key, page.number, tuple(clip), query)
        if result_key in self._clipped_search_result_cache:
            self.diagnostics["clipped_search_result_cache_hits"] += 1
            coordinates = self._clipped_search_result_cache[result_key]
            return [fitz.Rect(values) for values in coordinates]
        self.diagnostics["clipped_search_result_cache_misses"] += 1
        hits = _search(
            page, query, clip=clip, metrics=self.diagnostics, phase="table",
        )
        self._clipped_search_result_cache[result_key] = tuple(
            tuple(rect) for rect in hits
        )
        return [
            fitz.Rect(values)
            for values in self._clipped_search_result_cache[result_key]
        ]

    def word_quote_match(self, document_key: str, page, query: str, clip=None):
        """Memoize bounded word-geometry fallback, including its exact clip."""
        result_key = (document_key, page.number, query, tuple(clip) if clip else None)
        cached = self._word_quote_result_cache.get(result_key)
        if cached is None:
            self.diagnostics["word_quote_cache_misses"] += 1
            page_key = (document_key, page.number)
            words = self._page_words_cache.get(page_key)
            if words is None:
                words = tuple(page.get_text("words", sort=True))
                self._page_words_cache[page_key] = words
                self.diagnostics["word_quote_word_pages_built"] += 1
            rects = _full_quote_word_match(page, query, clip, words)
            if not rects:
                rects = _full_quote_compact_match(page, query, clip, words)
                if rects:
                    self.diagnostics["compact_quote_matches"] += 1
            if not rects:
                raw_chars = self._page_raw_chars_cache.get(page_key)
                if raw_chars is None:
                    raw_chars = []
                    for block in page.get_text("rawdict").get("blocks", []):
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                raw_chars.extend(
                                    (char["c"], tuple(char["bbox"]))
                                    for char in span.get("chars", [])
                                )
                    raw_chars = tuple(raw_chars)
                    self._page_raw_chars_cache[page_key] = raw_chars
                    self.diagnostics["word_quote_raw_pages_built"] += 1
                clip_key = tuple(clip) if clip else None
                index_key = (*page_key, clip_key)
                prepared_index = self._page_character_index_cache.get(index_key)
                if prepared_index is None:
                    prepared_index = _prepared_character_index(raw_chars, clip)
                    self._page_character_index_cache[index_key] = prepared_index
                    self.diagnostics["word_quote_character_indexes_built"] += 1
                rects = _full_quote_character_match(
                    page, query, clip, raw_chars, prepared_index,
                )
                if rects:
                    self.diagnostics["character_quote_matches"] += 1
            cached = tuple(tuple(rect) for rect in rects)
            self._word_quote_result_cache[result_key] = cached
        else:
            self.diagnostics["word_quote_cache_hits"] += 1
        return [fitz.Rect(values) for values in cached]

    def table_rect(self, location: str, context: dict):
        """Return (page_no_1based, fitz.Rect) in the routed PDF, or None."""
        if location in self._table_rect_cache:
            return self._table_rect_cache[location]
        di = self._loc_to_docling.get(location)
        result = None
        if di is not None:
            entry = self._docling.get(di)
            if entry and entry.get("bbox"):
                page_no = entry.get("page")  # 1-based virtual/source page
                table = self._table_by_location.get(location)
                if table and table.get("source_artifact"):
                    page_no = self._mapped_page(table, page_no)
                page = context["doc"][page_no - 1]
                display_height = page.rect.height
                l, t, r, b = entry["bbox"]  # bottom-left origin: t > b
                # Docling boxes follow the visually displayed page orientation,
                # with a bottom-left origin. Convert first to displayed top-left
                # coordinates, then derotate into PyMuPDF's search/clip frame.
                displayed = fitz.Rect(
                    l, display_height - t, r, display_height - b,
                )
                displayed.normalize()
                rect = displayed * page.derotation_matrix
                rect.normalize()
                result = (page_no, rect)
        self._table_rect_cache[location] = result
        return result

    def figure_rect(self, location: str, context: dict):
        """Return a broad figure-region rect from preserved Docling geometry."""
        if location in self._figure_rect_cache:
            return self._figure_rect_cache[location]
        figure = self._figure_by_location.get(location)
        entry = self._pictures.get(figure.get("docling_idx")) if figure else None
        result = None
        if entry and entry.get("bbox") and isinstance(entry.get("page"), int):
            page_no = entry["page"]
            if 1 <= page_no <= context["doc"].page_count:
                page = context["doc"][page_no - 1]
                l, t, r, b = entry["bbox"]
                displayed = fitz.Rect(l, page.rect.height - t, r, page.rect.height - b)
                displayed.normalize()
                rect = displayed * page.derotation_matrix
                rect.normalize()
                result = (page_no, rect)
        self._figure_rect_cache[location] = result
        return result


# ---------------------------------------------------------------------------
# Per-source resolution
# ---------------------------------------------------------------------------


def _value_query(field_value):
    """Normalized, searchable string for a field's value (or '' if unusable).

    Used to value-anchor a highlight to the precise number/token rather than the
    whole sentence/cell. Bools and None aren't useful PDF search anchors.
    """
    if field_value is None or isinstance(field_value, bool):
        return ""
    nv = normalize_quote(str(field_value))
    return nv if nv else ""


def _resolve_source_uncached(bundle: Bundle, source: dict, stats: Counter, field_value=None):
    """Mutate `source` adding `resolution` + `highlight`; bump global stats.

    `field_value` is the extracted value this source supports (when known). It
    lets us value-anchor: highlight the precise value within the matched
    sentence/cell rather than the whole span.
    """
    raw_quote = source.get("quote", "") or ""
    nq = normalize_quote(raw_quote)
    location = source.get("location", "") or ""
    val_q = _value_query(field_value)
    context = bundle.source_context(source)
    cited_page = context["page"]
    source["pdf"] = context["pdf"]

    # No primary source PDF (gitignored / data-only clone): cannot resolve rects.
    # Record page_only so the client jumps to the cited page without overlay.
    if context["doc"] is None:
        jump_page = cited_page if isinstance(cited_page, int) and cited_page >= 1 else 1
        source["resolution"] = "page_only"
        source["highlight"] = [{"page": jump_page, "rects": []}]
        stats["page_only"] += 1
        stats["pdf_missing"] += 1
        return

    doc = context["doc"]
    document_key = context["key"]
    cell = source.get("cell") or None
    short_or_numeric = _is_short_or_numeric(nq)

    # Preserve a native caption/full-quote hit when available; figure geometry
    # is a broad fallback for genuinely graphical evidence.
    figure = bundle.figure_rect(location, context)

    # ---- Table-cited source ----
    if location.startswith("tables/"):
        tr = bundle.table_rect(location, context)
        if tr is not None:
            page_no, rect = tr
            page = doc[page_no - 1]

            # Does the quote equal one of the cell labels (i.e. the extractor
            # quoted the header / row-label, not the data value)? If so a raw
            # search would highlight the label, not the cell — cell-anchor.
            quote_is_label = bool(cell) and nq in {
                normalize_quote(cell.get("row") or ""),
                normalize_quote(cell.get("column") or ""),
            }

            # 1. CELL-ANCHORING — preferred whenever we have a cell and the bare
            #    quote is weak (short/numeric) or merely echoes a cell label.
            #    Resolve the value inside the row×column intersection so e.g.
            #    "0" lands in exactly one cell, not all 128. Anchor on the field
            #    VALUE when available (the quote may echo a label), else the quote.
            if cell and (short_or_numeric or quote_is_label):
                for value_q in (val_q, nq):
                    if not value_q:
                        continue
                    hits = _anchor_cell(
                        page, rect, cell, value_q,
                        clipped_search=lambda p, q, c: bundle.clipped_search(
                            document_key, p, q, c,
                        ),
                    )
                    if hits:
                        source["resolution"] = "exact"
                        source["highlight"] = _group_by_page(hits, page_no, page)
                        stats["exact"] += 1
                        stats["cell_anchored"] += 1
                        return

            # 2. RAW QUOTE within the table rect (long, distinctive values).
            hits = bundle.clipped_search(
                document_key, page, nq, rect,
            ) if (nq and not short_or_numeric) else []
            if hits:
                source["resolution"] = "exact"
                source["highlight"] = _group_by_page(hits, page_no, page)
                stats["exact"] += 1
                return

            # Table PDFs often split a verified multi-word quote across rows or
            # line wraps. Keep this fallback clipped to the known table box and
            # reject short/numeric queries just as the direct search does.
            hits = bundle.word_quote_match(
                document_key, page, nq, clip=rect,
            ) if (nq and not short_or_numeric) else []
            if hits:
                source["resolution"] = "exact"
                source["highlight"] = _group_by_page(hits, page_no, page)
                stats["exact"] += 1
                stats["table_word_matches"] += 1
                return

            # Table captions commonly sit just above the detected table body.
            # Permit a whole-page retry only for an explicit Table <id> caption;
            # ordinary table evidence remains constrained to its body geometry.
            if re.match(r"^table\s+[a-z]*\d+\b", nq, flags=re.IGNORECASE):
                hits = bundle.word_quote_match(document_key, page, nq)
                if hits:
                    source["resolution"] = "exact"
                    source["highlight"] = _group_by_page(hits, page_no, page)
                    stats["exact"] += 1
                    stats["table_caption_matches"] += 1
                    return

            # 3. CELL-ANCHORING fallback even for non-short quotes that failed.
            if cell:
                hits = _anchor_cell(
                    page, rect, cell, nq,
                    clipped_search=lambda p, q, c: bundle.clipped_search(
                        document_key, p, q, c,
                    ),
                )
                if hits:
                    source["resolution"] = "exact"
                    source["highlight"] = _group_by_page(hits, page_no, page)
                    stats["exact"] += 1
                    stats["cell_anchored"] += 1
                    return

            # 4. degrade to whole-table region (still a useful jump target).
            source["resolution"] = "table_region"
            source["highlight"] = _group_by_page([rect], page_no, page)
            stats["table_region"] += 1
            return
        # table couldn't be mapped -> fall through to prose handling

    # ---- Prose source (sections/* or unmapped table) ----
    page_no = cited_page if isinstance(cited_page, int) and cited_page >= 1 else None

    # A short/numeric prose quote on its own would light up every matching glyph
    # on the page; require a distinctive (longer) quote to anchor.
    def _prose_queries():
        """Search full quotes first; use truncation only as partial fallback."""
        seen = set()
        for query, is_full_quote in ((nq, True), (nq[:80], False), (nq[:40], False)):
            if not query or query in seen:
                continue
            seen.add(query)
            yield query, is_full_quote or query == nq

    def _emit_prose(hits, pno, page, *, is_full_quote: bool):
        """Emit all rects for a prose quote.

        For prose provenance the quote itself is the evidence contract. Do not
        narrow to the extracted field value: doing so turns a multi-line quote
        into a tiny value glyph highlight and hides the supporting sentence.
        """
        highlight = _group_by_page(hits, pno, page)
        if is_full_quote:
            source["resolution"] = "exact"
            stats["exact"] += 1
        else:
            source["resolution"] = "partial_quote"
            for entry in highlight:
                entry["partial"] = True
            stats["partial_quote"] += 1
        source["highlight"] = highlight

    # Try the complete quote everywhere before allowing a prefix. Section page
    # ranges are a priority hint only: neighboring/off-by-one pages remain in
    # the all-page pass.
    if nq and not short_or_numeric:
        order = []
        if page_no and 1 <= page_no <= doc.page_count:
            order.append(page_no - 1)
        for p in context.get("preferred_pages") or []:
            if 1 <= p <= doc.page_count and p - 1 not in order:
                order.append(p - 1)
        allowed = context["allowed_pages"] or list(range(1, doc.page_count + 1))
        order += [
            p - 1 for p in allowed
            if 1 <= p <= doc.page_count and p - 1 not in order
        ]
        for pno in order:
            phase = (
                "cited_page" if page_no is not None and pno == page_no - 1
                else "all_page_fallback"
            )
            page, hits = bundle.prose_search(
                document_key, pno, nq, phase=phase,
            )
            if not hits:
                hits = bundle.word_quote_match(document_key, page, nq)
                if hits:
                    bundle.diagnostics["full_quote_word_matches"] += 1
            if hits:
                _emit_prose(hits, pno + 1, page, is_full_quote=True)
                return
        if page_no and 1 <= page_no <= doc.page_count:
            bundle.diagnostics["cited_page_misses"] += 1
        if figure is not None:
            figure_page, rect = figure
            page = doc[figure_page - 1]
            source["resolution"] = "figure_region"
            source["highlight"] = _group_by_page([rect], figure_page, page)
            stats["figure_region"] += 1
            return
        # Truncation is only a visibly marked last resort after every page has
        # failed the full quote. The first tuple element is the full query.
        for query, is_full_quote in list(_prose_queries())[1:]:
            for pno in order:
                page, hits = bundle.prose_search(
                    document_key, pno, query, phase="all_page_fallback",
                )
                if hits:
                    _emit_prose(hits, pno + 1, page, is_full_quote=is_full_quote)
                    return

    if figure is not None:
        figure_page, rect = figure
        page = doc[figure_page - 1]
        source["resolution"] = "figure_region"
        source["highlight"] = _group_by_page([rect], figure_page, page)
        stats["figure_region"] += 1
        return

    # no hit -> page_only (client jumps to cited page, no overlay)
    jump_page = page_no if page_no else (cited_page if cited_page else 1)
    source["resolution"] = "page_only"
    source["highlight"] = [{"page": jump_page, "rects": []}]
    stats["page_only"] += 1


def source_cache_key(bundle: Bundle, source: dict, field_value=None) -> str:
    """Shared cache key for one source occurrence (resolve_source + cache seeding).

    The key is the JSON of [quote, location, page, cell, field_value,
    context.key, context.page, context.allowed_pages], where field_value is
    the row's field value for top-level sources and the component's value for
    source_components sources (see provenance_sources).
    """
    context = bundle.source_context(source)
    return json.dumps(
        [source.get("quote"), source.get("location"), source.get("page"),
         source.get("cell"), field_value, context["key"], context["page"],
         context["allowed_pages"]],
        ensure_ascii=False, sort_keys=True,
    )


def load_snapshot_rows(snapshot: Path, paper_id: str) -> list:
    """Return rows/<paper_id>.json in v001 shape, from a v001 or v002 snapshot.

    Applies the FORMAT-V002.md section 5 rule exactly: v001 rows pass through
    unchanged; v002 integer references into evidence/<paper_id>.json expand to
    the table entries they index. Raises ValueError on a bad reference (a
    non-integer, an out-of-range index, or a reference where a table entry is
    expected) or on an unknown snapshot_format_version. Expanding an already
    expanded (v001-shaped) row raises because resolved objects are not ints.
    """
    snapshot = Path(snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    version = manifest.get("snapshot_format_version", "v001")
    rows = json.loads((snapshot / "rows" / f"{paper_id}.json").read_text())
    if version == "v001":
        return rows
    if version != "v002":
        raise ValueError(f"unsupported snapshot_format_version: {version!r}")
    ev = json.loads((snapshot / "evidence" / f"{paper_id}.json").read_text())

    def at(table, i):
        entries = ev.get(table)
        if not isinstance(entries, list) or type(i) is not int or not 0 <= i < len(entries):
            raise ValueError(f"{paper_id}: bad {table} ref {i!r}")
        return entries[i]

    for row in rows:
        for pv in row["provenance"].values():
            if "sources" in pv:
                pv["sources"] = [at("sources", i) for i in pv["sources"]]
            for comp in pv.get("source_components", []):
                if "sources" in comp:
                    comp["sources"] = [at("sources", i) for i in comp["sources"]]
            for key in ("reasoning", "derivation"):
                if key in pv:
                    pv[key] = at(key, pv[key])
    return rows


def _canonical_json(value) -> str:
    """Canonical-JSON equality key (FORMAT-V002.md section 6)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def intern_paper_evidence(rows_out: list) -> dict:
    """Intern one paper's evidence into v002 tables (FORMAT-V002.md sections 3-5).

    Mutates rows_out in place: the four slots (pv.sources,
    component sources, pv.reasoning, pv.derivation) become integer references.
    Table order is first occurrence in the section 5 scan order: rows in file
    order; provenance entries in file (insertion) order; within an entry sources in order,
    then each component's sources in order, then reasoning, then derivation.
    Returns {"sources": [...], "reasoning": [...], "derivation": [...]} holding
    the first-seen object of each distinct entry (canonical-JSON distinctness).
    Absent slots stay absent; repeats keep their length and order.
    """
    sources, reasoning, derivation = [], [], []
    source_index, reasoning_index, derivation_index = {}, {}, {}

    def _intern(table, entries, entry_key, index):
        i = index.get(entry_key)
        if i is None:
            i = len(entries)
            entries.append(table)
            index[entry_key] = i
        return i

    for row in rows_out:
        for pv in row["provenance"].values():
            if "sources" in pv:
                pv["sources"] = [
                    _intern(src, sources, _canonical_json(src), source_index)
                    for src in pv["sources"]
                ]
            for comp in pv.get("source_components", []):
                if "sources" in comp:
                    comp["sources"] = [
                        _intern(src, sources, _canonical_json(src), source_index)
                        for src in comp["sources"]
                    ]
            for key, entries, index in (
                ("reasoning", reasoning, reasoning_index),
                ("derivation", derivation, derivation_index),
            ):
                if key in pv:
                    pv[key] = _intern(pv[key], entries, _canonical_json(pv[key]), index)
    return {"sources": sources, "reasoning": reasoning, "derivation": derivation}


# Sentinels for the seed_highlight_cache key table: _MISSING means unseen,
# _DROPPED means conflicted (stays dropped for every later occurrence).
_MISSING = object()
_DROPPED = object()


def seed_highlight_cache(bundle: Bundle, cache_dir, paper_id: str) -> dict:
    """Seed bundle._source_resolution_cache from an existing export (one paper).

    Reads the cache export's rows for paper_id through load_snapshot_rows
    (accepting v001 or v002 snapshots), walks them with provenance_sources,
    and pre-populates the resolution cache with {pdf, resolution, highlight,
    stats} keyed by source_cache_key computed with the current run's bundle.
    A key mapping to two different results is dropped (resolved fresh later).
    Returns diagnostics {status, seeded_keys, conflicts} where status is one of
    seeded / documents_mismatch / bundle_manifest_mismatch / paper_absent.
    """
    cache_dir = Path(cache_dir)
    diagnostics = {"status": "paper_absent", "seeded_keys": 0, "conflicts": 0}
    if not (cache_dir / "rows" / f"{paper_id}.json").exists():
        return diagnostics
    cache_manifest = json.loads((cache_dir / "manifest.json").read_text())
    cache_papers = {
        p.get("paper_id"): p for p in cache_manifest.get("papers", []) if isinstance(p, dict)
    }
    cache_paper = cache_papers.get(paper_id)
    if cache_paper is None:
        return diagnostics
    published = {
        (d.get("pdf"), d.get("sha256")) for d in (cache_paper.get("documents") or [])
    }
    current = {
        (d.get("pdf"), d.get("sha256")) for d in bundle.document_catalog()
    }
    if published != current:
        diagnostics["status"] = "documents_mismatch"
        return diagnostics
    cache_bundle_manifest = cache_dir / "bundles" / paper_id / "manifest.json"
    bundle_manifest = bundle.bundle_dir / "manifest.json"
    if not cache_bundle_manifest.is_file():
        diagnostics["status"] = "bundle_manifest_mismatch"
        return diagnostics
    if (hashlib.sha256(cache_bundle_manifest.read_bytes()).hexdigest()
            != hashlib.sha256(bundle_manifest.read_bytes()).hexdigest()):
        diagnostics["status"] = "bundle_manifest_mismatch"
        return diagnostics
    cached_rows = load_snapshot_rows(cache_dir, paper_id)
    by_key = {}
    for row in cached_rows:
        for field_name, pv in row.get("provenance", {}).items():
            for source, value, _top in provenance_sources(
                    pv, row["fields"].get(field_name)):
                result = _canonical_json(
                    {k: source[k] for k in ("pdf", "resolution", "highlight")}
                )
                key = source_cache_key(bundle, source, field_value=value)
                seen = by_key.get(key, _MISSING)
                if seen is _MISSING:
                    by_key[key] = (result, source)
                elif seen is not _DROPPED and seen[0] != result:
                    by_key[key] = _DROPPED  # conflicting key: stays dropped
    for key, entry in by_key.items():
        if entry is _DROPPED:
            diagnostics["conflicts"] += 1
            continue
        _result, source = entry
        bundle._source_resolution_cache[key] = {
            "pdf": source["pdf"],
            "resolution": source["resolution"],
            "highlight": copy.deepcopy(source["highlight"]),
            "stats": Counter({source["resolution"]: 1}),
            "seeded": True,
        }
        diagnostics["seeded_keys"] += 1
    diagnostics["status"] = "seeded"
    return diagnostics


def resolve_source(bundle: Bundle, source: dict, stats: Counter, field_value=None):
    """Cache repeated citation/value searches while retaining per-source counts."""
    cache_key = source_cache_key(bundle, source, field_value=field_value)
    cached = bundle._source_resolution_cache.get(cache_key)
    if cached is None:
        bundle.diagnostics["source_cache_misses"] += 1
        delta = Counter()
        started = time.perf_counter()
        _resolve_source_uncached(bundle, source, delta, field_value=field_value)
        bundle.diagnostics["unique_resolution_seconds"] += time.perf_counter() - started
        cached = {
            "pdf": source["pdf"],
            "resolution": source["resolution"],
            "highlight": copy.deepcopy(source["highlight"]),
            "stats": delta,
        }
        bundle._source_resolution_cache[cache_key] = cached
    else:
        bundle.diagnostics["source_cache_hits"] += 1
        if cached.get("seeded"):
            bundle.diagnostics["highlight_cache_hits"] += 1
    source["pdf"] = cached["pdf"]
    source["resolution"] = cached["resolution"]
    source["highlight"] = copy.deepcopy(cached["highlight"])
    stats.update(cached["stats"])
    bundle.diagnostics["source_occurrences"] += 1
    bundle.diagnostics[f"resolution_{cached['resolution']}"] += 1


def provenance_sources(provenance: dict, field_value=None):
    """Yield citations with their supported value and whether they are top-level."""
    for source in provenance.get("sources", []):
        yield source, field_value, True
    for component in provenance.get("source_components", []):
        for source in component.get("sources", []):
            yield source, component.get("value"), False


def resolve_provenance_sources(
    bundle: Bundle, provenance: dict, stats: Counter, cited_locations: set,
    field_value=None, *, resolve_highlights=True,
):
    """Include derivation operands without changing top-level citation counts.

    A repeated citation may support different values. Resolve each component
    using its own value; the source/value cache safely reuses identical work.
    """
    for source, value, top_level in provenance_sources(provenance, field_value):
        location = source.get("location")
        if location:
            cited_locations.add(location)
        source_stats = stats if top_level else Counter()
        if resolve_highlights:
            resolve_source(bundle, source, source_stats, field_value=value)
        else:
            mark_source_not_resolved_for_demo(bundle, source, source_stats)


def mark_source_not_resolved_for_demo(bundle: Bundle, source: dict, stats: Counter):
    """Attach a page jump without doing PDF rect resolution for demo slices."""
    context = bundle.source_context(source)
    source["pdf"] = context["pdf"]
    cited_page = context["page"]
    jump_page = cited_page if isinstance(cited_page, int) and cited_page >= 1 else 1
    source["resolution"] = "not_resolved_for_demo"
    source["highlight"] = [{"page": jump_page, "rects": []}]
    stats["not_resolved_for_demo"] += 1


# ---------------------------------------------------------------------------
# Review units — decision-level abstraction (v2)
# See portal/CONTRACTS.md "Review units" — authoritative.
# ---------------------------------------------------------------------------

# kind -> suggested client action
_UNIT_ACTION = {
    "read_item": "read_each",
    "verify_table": "verify_then_approve",
    "verify_section": "verify_then_approve",
    "vocab_mapping": "decide_mapping",
    "abstention_policy": "decide_policy",
    "spot_check": "spot_check_then_approve",
    "vocab_normalization": "bulk_approve",
}


def build_derivation_lookup(extractions_dir: Path, paper_ids):
    """(paper_id, row_index, field) -> {derivation_kind, derivation} from rows.

    Lets the review-unit clusterer tell genuine high-leverage decisions (figure
    reads / cross-source assembly = derivation_kind 'inferred', real ambiguous
    verbatim values) from pure controlled-vocabulary normalization
    (derivation_kind 'derived' — schema-token coding, canonical mapping,
    transcription, arithmetic), which floods the verify tiers and should sit at
    tier 3.
    """
    lut = {}
    for pid in paper_ids:
        f = extractions_dir / f"{pid}.jsonl"
        if not f.exists():
            continue
        for idx, line in enumerate(f.read_text().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            for fld, pv in (row.get("provenance") or {}).items():
                if not isinstance(pv, dict):
                    continue
                lut[(pid, idx, fld)] = {
                    "derivation_kind": pv.get("derivation_kind"),
                    "derivation": pv.get("derivation"),
                }
    return lut


def _is_low_leverage_normalization(item: dict, derivation_lookup: dict) -> bool:
    """True when this item is pure controlled-vocab normalization / schema-token
    coding / transcription / arithmetic-derived value (NOT a figure read or
    genuine cross-source inference) — i.e. derivation_kind == 'derived'.

    KEEP HIGH (returns False): derivation_kind 'inferred' (cross-source assembly,
    figure-interior reads) and 'verbatim' (real table/prose values flagged
    ambiguous), plus anything with no derivation record (be conservative).
    """
    if not derivation_lookup:
        return False
    rec = derivation_lookup.get(
        (item.get("paper_id"), item.get("row_index"), item.get("field"))
    )
    if not rec:
        return False
    return rec.get("derivation_kind") == "derived"


def _short_id(paper_id: str) -> str:
    """Author_Year from Author_Year_DocumentId (best-effort)."""
    parts = (paper_id or "").split("_")
    if len(parts) >= 2:
        return f"{parts[0]}_{parts[1]}"
    return paper_id or "?"


def _first_source(item: dict) -> dict:
    srcs = item.get("sources") or []
    return srcs[0] if srcs else {}


def _first_location(item: dict):
    return _first_source(item).get("location")


def _first_page(item: dict):
    return _first_source(item).get("page")


def _first_quote(item: dict):
    return _first_source(item).get("quote")


def _truncate(text, n=160):
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _table_meta(manifests_by_paper: dict, paper_id: str, location: str) -> dict:
    """Look up a cited table location in the bundle manifest.

    Returns {title, validation, page}. Accepts either the .json `path` or the
    sibling `markdown_path`. Empty dict if unknown.
    """
    man = manifests_by_paper.get(paper_id) or {}
    for t in man.get("tables", []):
        if location in (t.get("path"), t.get("markdown_path")):
            return {
                "title": t.get("title"),
                "validation": t.get("validation"),
                "page": t.get("page"),
            }
    return {}


def _sample_evenly(members: list, k: int) -> list:
    """Deterministic evenly-spaced sample of up to k member refs."""
    n = len(members)
    k = min(k, n)
    if k <= 0:
        return []
    if k == n:
        return list(members)
    # evenly spaced indices across [0, n-1]
    idxs = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
    # round() can collide; backfill to keep exactly k where possible
    i = 0
    while len(idxs) < k and i < n:
        if i not in idxs:
            idxs.append(i)
        i += 1
    idxs = sorted(idxs)[:k]
    return [members[i] for i in idxs]


def _member_ref(item: dict) -> dict:
    return {"row_index": item.get("row_index"), "field": item.get("field")}


def compute_review_units(queue_items: list, manifests_by_paper: dict,
                         derivation_lookup: dict = None) -> dict:
    """Cluster every queue item into exactly one review unit.

    Implements the priority-ordered assignment algorithm from CONTRACTS.md.
    Returns the review_units.json structure (deterministically ordered).
    Pure logic — no I/O. Asserts exhaustive assignment.

    `derivation_lookup` (optional) lets us DEMOTE pure controlled-vocabulary
    normalization to a tier-3 `vocab_normalization` kind so Tier 1/2 stay
    dominated by genuinely high-leverage decisions. Demoted items still appear
    (exhaustiveness preserved) — just at tier 3.
    """
    derivation_lookup = derivation_lookup or {}
    # bucket id -> {meta..., items: [...]}
    units = {}

    def _bucket(unit_id, kind, paper_id):
        u = units.get(unit_id)
        if u is None:
            u = {
                "id": unit_id,
                "kind": kind,
                "paper_id": paper_id,
                "items": [],
            }
            units[unit_id] = u
        return u

    for item in queue_items:
        tier = item.get("risk_tier")
        pid = item.get("paper_id")

        # 0. DEMOTE pure controlled-vocab normalization (derivation_kind=='derived')
        #    out of the high-leverage verify tiers into a tier-3 low-leverage
        #    bucket. Applies to the `ambiguous` and `derived_high_leverage` tiers,
        #    which are otherwise flooded by schema-token coding / canonical
        #    mapping / transcription. Genuine cross-source inference ('inferred'),
        #    real ambiguous verbatim values, abstentions, normalizer disputes and
        #    unresolved conflicts are NOT demoted (they fall through below).
        if tier in ("ambiguous", "derived_high_leverage") and \
                _is_low_leverage_normalization(item, derivation_lookup):
            fld = item.get("field")
            uid = f"vocab_normalization:{pid}:{fld}"
            _bucket(uid, "vocab_normalization", pid)["items"].append(item)
            continue

        # 1. inferred_high_leverage -> read_item, MERGED across fields that share
        #    the same row + sentence (same page + location). Two fields read from
        #    one sentence (e.g. Joerger row-6 n_total & n_events on p.7
        #    discussion.md) become ONE multi-member unit; a different sentence in
        #    the same paper stays a separate unit.
        if tier == "inferred_high_leverage":
            ri = item.get("row_index")
            fsrc = _first_source(item)
            page = fsrc.get("page")
            loc = fsrc.get("location")
            uid = f"read_item:{pid}:{ri}:{page}:{loc}"
            _bucket(uid, "read_item", pid)["items"].append(item)
            continue

        # 2. normalizer_dispute -> vocab_mapping keyed by normalized raw label
        #    (first source quote), merging across fields.
        if tier == "normalizer_dispute":
            label = _first_quote(item) or "?"
            key = normalize_quote(label) or label
            uid = f"vocab_mapping:{pid}:{key}"
            _bucket(uid, "vocab_mapping", pid)["items"].append(item)
            continue

        # 3. abstained -> abstention_policy keyed by (paper_id, field)
        if tier == "abstained":
            fld = item.get("field")
            uid = f"abstention_policy:{pid}:{fld}"
            _bucket(uid, "abstention_policy", pid)["items"].append(item)
            continue

        # 4. derived_high_leverage -> spot_check keyed by (paper_id, field)
        if tier == "derived_high_leverage":
            fld = item.get("field")
            uid = f"spot_check:{pid}:{fld}"
            _bucket(uid, "spot_check", pid)["items"].append(item)
            continue

        # 5/6. ambiguous -> verify_table (under tables/) else verify_section
        if tier == "ambiguous":
            loc = _first_location(item) or ""
            if loc.startswith("tables/"):
                uid = f"verify_table:{pid}:{loc}"
                _bucket(uid, "verify_table", pid)["items"].append(item)
            else:
                uid = f"verify_section:{pid}:{loc}"
                _bucket(uid, "verify_section", pid)["items"].append(item)
            continue

        # unknown tier -> defensive bucket so nothing is dropped
        uid = f"other:{pid}:{item.get('field')}"
        _bucket(uid, "spot_check", pid)["items"].append(item)

    # ---- finalize each unit (tier, why, title, anchor, members, etc.) ----
    finalized = []
    for u in units.values():
        members = u["items"]
        kind = u["kind"]
        pid = u["paper_id"]
        n = len(members)
        gate_blocking = any(m.get("high_leverage") for m in members)
        short = _short_id(pid)

        unit = {
            "id": u["id"],
            "kind": kind,
            "tier": None,
            "gate_blocking": gate_blocking,
            "title": "",
            "why": "",
            "paper_id": pid,
            "anchor": {},
            "members": [_member_ref(m) for m in members],
            "member_count": n,
            "suggested_action": _UNIT_ACTION.get(kind, "bulk_approve"),
        }

        if kind == "read_item":
            m = members[0]
            ri = m.get("row_index")
            unit["tier"] = 1
            # enumerate every field=value read from this shared sentence
            pairs = [f"{mm.get('field')} = {mm.get('value')}" for mm in members]
            fields = [mm.get("field") for mm in members]
            unit["anchor"] = {"field": fields[0], "fields": fields}
            if len(members) == 1:
                unit["title"] = f"{pairs[0]} — {short} row {ri}"
            else:
                unit["title"] = f"{'; '.join(pairs)} — {short} row {ri}"
            # one reasoning per field; dedupe identical reasons for readability
            reasons = []
            seen_r = set()
            for mm in members:
                r = _truncate(mm.get("reasoning"))
                if r and r not in seen_r:
                    seen_r.add(r)
                    reasons.append(r)
            reason_blob = " ".join(reasons)
            vals_phrase = (
                f"this value" if len(members) == 1
                else f"these {len(members)} values ({', '.join(fields)})"
            )
            unit["why"] = (
                f"The ATTRIBUTION of {vals_phrase} was inferred, not read from an "
                f"explicit cell: {reason_blob} "
                f"The digits may appear verbatim in the paper — what needs verifying is "
                f"that they belong to THIS row's identity (model/outcome/group), since a "
                f"wrong pairing puts a correct number on the wrong result and flows "
                f"straight into the meta-analysis."
            )

        elif kind == "verify_table":
            loc = _first_location(members[0]) or ""
            meta = _table_meta(manifests_by_paper, pid, loc)
            title = meta.get("title")
            validation = meta.get("validation")
            page = meta.get("page")
            unresolved = validation == "unresolved_conflict"
            unit["tier"] = 1 if unresolved else 2
            disp = title or loc
            unit["title"] = f"Verify {disp} against the PDF"
            unit["anchor"] = {
                "location": loc,
                "page": page,
                "table_title": title,
                "validation": validation,
            }
            unit["why"] = (
                f"All {n} flagged values in this paper come from {disp} "
                f"({validation or 'unvalidated'}), which paper-prep could not "
                f"cross-corroborate. Verify the table against the PDF once; if it's "
                f"faithful, one approval clears all {n}."
            )

        elif kind == "verify_section":
            loc = _first_location(members[0]) or ""
            page = _first_page(members[0])
            fields = sorted({m.get("field") for m in members if m.get("field")})
            unit["tier"] = 2 if gate_blocking else 3
            unit["title"] = f"Verify {loc} against the PDF"
            unit["anchor"] = {"location": loc, "page": page}
            unit["why"] = (
                f"All {n} flagged values come from the prose section {loc} "
                f"(fields: {', '.join(fields)}), where extraction was ambiguous. "
                f"Read the passage once; one approval clears all {n}."
            )

        elif kind == "vocab_mapping":
            label = _first_quote(members[0])
            fields = sorted({m.get("field") for m in members if m.get("field")})
            unit["tier"] = 2
            unit["title"] = f"Decide mapping for '{label}'"
            unit["anchor"] = {"label": label, "fields": fields}
            unit["why"] = (
                f"The raw label '{label}' could not be normalized to a canonical id; "
                f"it maps into {', '.join(fields)}. One mapping decision here covers all "
                f"{n} occurrences across those fields."
            )

        elif kind == "abstention_policy":
            fld = members[0].get("field")
            reason = Counter(
                m.get("reasoning") for m in members if m.get("reasoning")
            )
            dominant = reason.most_common(1)[0][0] if reason else None
            unit["tier"] = 2 if gate_blocking else 3
            unit["title"] = f"Set abstention policy for {fld}"
            unit["anchor"] = {"field": fld}
            unit["why"] = (
                f"The extractor abstained on {fld} in {n} rows. Dominant recorded "
                f"reason: {_truncate(dominant)} Decide the policy for this field once; "
                f"it applies to all {n} abstentions."
            )

        elif kind == "spot_check":
            fld = members[0].get("field")
            sample = _sample_evenly(unit["members"], min(10, n))
            pattern = _truncate(members[0].get("reasoning")) or "a repeated derivation"
            unit["tier"] = 2
            unit["sample"] = sample
            unit["anchor"] = {"field": fld}
            unit["title"] = f"Spot-check derived {fld}"
            unit["why"] = (
                f"{n} values of {fld} were derived by the same pattern (e.g. \"{pattern}\"). "
                f"Spot-check the sample of {len(sample)}; if the derivation holds, "
                f"bulk-approve all {n}."
            )

        elif kind == "vocab_normalization":
            fld = members[0].get("field")
            sample = _sample_evenly(unit["members"], min(10, n))
            unit["tier"] = 3
            unit["sample"] = sample
            unit["anchor"] = {"field": fld}
            unit["title"] = f"Controlled-vocab normalization: {fld}"
            unit["why"] = (
                f"{n} values of {fld} are pure controlled-vocabulary normalization "
                f"(schema-token coding / canonical mapping / transcription), not "
                f"figure reads or cross-source inference. Low review leverage — "
                f"spot-check the sample of {len(sample)} and bulk-approve."
            )

        finalized.append(unit)

    # deterministic order: tier asc, member_count desc, id asc
    finalized.sort(key=lambda u: (u["tier"], -u["member_count"], u["id"]))

    total_items = sum(u["member_count"] for u in finalized)
    tier_counts = Counter(str(u["tier"]) for u in finalized)

    return {
        "total_units": len(finalized),
        "total_items": total_items,
        "tier_counts": dict(sorted(tier_counts.items())),
        "units": finalized,
    }


def build_and_write_units(out: Path, queue: dict, manifests_by_paper: dict,
                          derivation_lookup: dict = None):
    """Compute review units, assert exhaustiveness, write review_units.json,
    and return (review_units, units_summary) for the manifest patch."""
    queue_items = queue.get("queue", [])
    queue_total = queue.get("total_items", len(queue_items))
    review_units = compute_review_units(
        queue_items, manifests_by_paper, derivation_lookup
    )

    assigned = review_units["total_items"]
    if assigned != queue_total:
        raise SystemExit(
            f"ERROR: review-unit assignment not exhaustive: "
            f"sum(member_count)={assigned} != queue total={queue_total}"
        )

    (out / "review_units.json").write_text(
        json.dumps(review_units, ensure_ascii=False, indent=2)
    )

    units_summary = {
        "total_units": review_units["total_units"],
        "tier_counts": review_units["tier_counts"],
    }
    return review_units, units_summary


def print_unit_plan(review_units: dict):
    """Human-readable plan summary to stdout. Returns the printed text."""
    lines = []
    lines.append("")
    lines.append("=== Review unit plan ===")
    lines.append(
        f"total_units={review_units['total_units']} "
        f"total_items={review_units['total_items']} "
        f"tier_counts={review_units['tier_counts']}"
    )
    by_tier = {}
    for u in review_units["units"]:
        by_tier.setdefault(u["tier"], []).append(u)
    for tier in sorted(by_tier):
        units = by_tier[tier]
        members = sum(u["member_count"] for u in units)
        lines.append("")
        lines.append(f"--- Tier {tier}  ({len(units)} units, {members} items) ---")
        for u in units:
            lines.append(
                f"  [{u['kind']:<16}] {u['member_count']:>5}  {u['title']}"
            )
    text = "\n".join(lines)
    print(text)
    return text


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def split_row(row: dict, identity_fields):
    """Return (identity, fields, provenance) from a raw extraction row.

    `identity_fields` is the schema-driven ordered list of identity dimension
    field names (see `load_identity_dimensions`).
    """
    provenance = row.get("provenance", {})
    identity = {f: row.get(f) for f in identity_fields}
    fields = {k: v for k, v in row.items() if k != "provenance"}
    return identity, fields, provenance


def _resolve_pdf_override(manifest: dict, pdf_dir):
    """Find a paper's source PDF in --pdf-dir by the manifest source_pdf
    basename (PDFs are often named human-readably, not <paper_id>.pdf, and the
    manifest's absolute source_pdf path may not exist on this machine).
    Returns a Path or None."""
    if not pdf_dir:
        return None
    src = manifest.get("source_pdf")
    if not src:
        return None
    base = Path(src).name
    od = Path(pdf_dir)
    direct = od / base
    if direct.exists():
        return direct
    matches = [p for p in od.rglob("*") if p.is_file() and p.name == base]
    return matches[0] if matches else None


def _load_json_object(path: Path, label: str):
    """Read a JSON object from path, returning (obj, error_message)."""
    try:
        obj = json.loads(path.read_text())
    except OSError as e:
        return None, f"could not read {label} {path}: {e}"
    except json.JSONDecodeError as e:
        return None, f"{label} must be valid JSON object ({path}: {e})"
    if not isinstance(obj, dict):
        return None, f"{label} must be a JSON object ({path})"
    return obj, None


def _prepare_output_dir(out: Path, force: bool = False) -> None:
    """Create an empty snapshot directory without silently replacing a run."""
    if out.exists():
        if not force:
            raise FileExistsError(
                f"output already exists: {out}; choose a new run ID/path or pass --force"
            )
        shutil.rmtree(out)
    (out / "rows").mkdir(parents=True)
    (out / "bundles").mkdir(parents=True)
    (out / "pdfs").mkdir(parents=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Comparanda portal snapshot exporter")
    ap.add_argument(
        "--project",
        default=None,
        help="domain project root (or set COMPARANDA_PROJECT_DIR)",
    )
    ap.add_argument(
        "--project-config",
        default=None,
        help="comparanda-project.json (discovered under --project by default)",
    )
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--domain",
        default=None,
        help="domain identifier written to manifest.json (default: project config or run ID)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="output snapshot dir (or config export.runs/<run-id>)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output directory; published runs should remain immutable",
    )
    ap.add_argument(
        "--review-queue",
        default=None,
        help="review_queue.json path (default: <project>/review_queue.json)",
    )
    ap.add_argument("--eval-dir", default=None, help="iteration directory containing eval artifacts")
    ap.add_argument(
        "--eval-profile",
        choices=("full", "reviewer-demo"),
        default="full",
        help="eval.json payload profile (default: full)",
    )
    ap.add_argument("--adjudications", default=None, help="gold_adjudications.json")
    ap.add_argument(
        "--gold-config",
        default=None,
        help="gold_config.json (value_column_map + narrow_columns); written "
        "into eval.json so the client reads the map from the snapshot instead "
        "of a hardcoded constant. Defaults to "
        "the configured benchmark.eval_config directory or the legacy path "
        "relative to --eval-dir if present.",
    )
    ap.add_argument("--blind", action="store_true", help="skip eval.json")
    ap.add_argument(
        "--demo-config",
        default=None,
        help="optional reviewer-demo JSON object copied canonically to "
        "<out>/demo_config.json",
    )
    ap.add_argument(
        "--highlight-paper",
        action="append",
        default=None,
        help="paper ID to run exact/table PDF highlight resolution for. "
        "Repeatable. Defaults to all papers.",
    )
    ap.add_argument(
        "--pdf-dir",
        default=None,
        help="directory of source PDFs to fall back on when "
        "<project>/data/raw/pdfs/<paper_id>.pdf is absent (PDFs are gitignored "
        "and the bundle manifest's source_pdf may be an absolute path from "
        "another machine). Resolved by the manifest's source_pdf basename, "
        "searched recursively. Needed for PDF highlight overlays.",
    )
    ap.add_argument(
        "--bundle-status",
        default=None,
        help="bundle_status.json classifying each paper clean | missing_data | "
        "gold_scope | bundle_suspect (hand-curated from the run's accuracy "
        "root-cause analysis). "
        "Used to emit eval.json's clean_subset presentation block. Defaults to "
        "<eval-dir>/../../benchmark/eval_config/bundle_status.json.",
    )
    ap.add_argument(
        "--highlight-cache",
        default=None,
        help="existing export directory (v001 or v002) whose resolved "
        "highlights are reused instead of recomputed. The cache export is "
        "read one paper at a time; a paper is seeded only when its documents "
        "and bundle manifest match the current inputs.",
    )
    ap.add_argument(
        "--units-only",
        action="store_true",
        help="recompute review_units.json from an EXISTING snapshot dir "
        "(queue.json + bundles/*/manifest.json) and patch manifest.json; "
        "skip all PDF/rect work",
    )
    args = ap.parse_args(argv)

    if args.units_only and args.out and not (args.project or args.project_config):
        return _units_only(Path(args.out).expanduser().resolve())

    try:
        layout = ProjectLayout.load(args.project, args.project_config)
        project = layout.root
        out = layout.export_dir(args.run_id, args.out)
    except LayoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    domain = args.domain or layout.domain or args.run_id

    if args.units_only:
        return _units_only(out)

    if not args.adjudications:
        candidate = layout.eval_config_dir / "gold_adjudications.json"
        if candidate.is_file():
            args.adjudications = str(candidate)
    if not args.gold_config:
        candidate = layout.eval_config_dir / "gold_config.json"
        if candidate.is_file():
            args.gold_config = str(candidate)
    if not args.bundle_status:
        candidate = layout.eval_config_dir / "bundle_status.json"
        if candidate.is_file():
            args.bundle_status = str(candidate)


    extractions_dir = project / "extractions"
    processed_dir = project / "data" / "processed"
    pdfs_dir = project / "data" / "raw" / "pdfs"

    # schema-driven identity dimensions (with last-resort fallback)
    identity_dims, identity_fields, identity_source = load_identity_dimensions(project)
    print(f"identity_dimensions: {len(identity_fields)} fields (source={identity_source}): "
          f"{', '.join(identity_fields)}")

    # discover papers from extraction JSONL files. Exclude the compact views
    # (`*.data.jsonl`, `*.view.jsonl`) that make_view.py writes beside each
    # `<paper>.jsonl` — those are derived artifacts, not extraction sources.
    paper_files = sorted(
        p for p in extractions_dir.glob("*.jsonl")
        if not (p.name.endswith(".data.jsonl") or p.name.endswith(".view.jsonl"))
    )
    paper_ids = [p.stem for p in paper_files]
    if not paper_ids:
        print("ERROR: no extraction JSONL files found", file=sys.stderr)
        return 1

    demo_config = None
    if args.demo_config:
        demo_config, err = _load_json_object(Path(args.demo_config), "demo config")
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1

    highlight_papers = None
    if args.highlight_paper:
        highlight_papers = set(args.highlight_paper)
        unknown = sorted(highlight_papers - set(paper_ids))
        if unknown:
            print(
                "ERROR: --highlight-paper unknown paper_id(s): "
                + ", ".join(unknown),
                file=sys.stderr,
            )
            return 1

    # load queue once
    review_queue_path = (
        Path(args.review_queue) if args.review_queue else project / "review_queue.json"
    )
    queue = json.loads(review_queue_path.read_text())
    queue_items = queue.get("queue", [])

    # Cross-check: union identity keys that queue items carry but the schema
    # list lacks (defensive — keeps identity tuples complete if the schema and
    # the extraction drift). Appended after the schema-ordered fields.
    queue_identity_keys = []
    seen = set(identity_fields)
    for it in queue_items:
        for k in (it.get("identity") or {}):
            if k not in seen:
                seen.add(k)
                queue_identity_keys.append(k)
    if queue_identity_keys:
        print(f"NOTE: queue carries identity keys not in schema: "
              f"{', '.join(queue_identity_keys)} (appended)")
        identity_fields = identity_fields + queue_identity_keys
        existing = {d["field"] for d in identity_dims}
        for k in queue_identity_keys:
            if k not in existing:
                identity_dims = identity_dims + [{"field": k, "description": ""}]

    # tier counts per paper + gate
    tier_by_paper = {pid: Counter() for pid in paper_ids}
    blocking_total = 0
    gate_by_tier = Counter()
    for it in queue_items:
        pid = it.get("paper_id")
        tier = it.get("risk_tier")
        if pid in tier_by_paper:
            tier_by_paper[pid][tier] += 1
        if it.get("high_leverage"):
            blocking_total += 1
            gate_by_tier[tier] += 1

    highlight_cache_dir = None
    if args.highlight_cache:
        highlight_cache_dir = Path(args.highlight_cache).expanduser().resolve()
        out_resolved = out.resolve()
        if (highlight_cache_dir == out_resolved
                or highlight_cache_dir in out_resolved.parents
                or out_resolved in highlight_cache_dir.parents):
            print("ERROR: --highlight-cache must not point at, inside, or "
                  "outside-but-containing the output directory",
                  file=sys.stderr)
            return 2

    # prepare output dirs. Published run IDs are immutable unless the caller
    # makes replacement explicit with --force.
    try:
        _prepare_output_dir(out, force=args.force)
    except FileExistsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if demo_config is not None:
        (out / "demo_config.json").write_text(
            json.dumps(demo_config, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )

    global_rect_stats = Counter()
    tier_totals = Counter()
    papers_meta = []
    manifests_by_paper = {}
    rows_by_paper = {}  # pid -> [non-provenance field dicts] for schema_summary

    for pid in paper_ids:
        paper_started = time.perf_counter()
        bundle_dir = processed_dir / pid
        bundle = Bundle(pid, bundle_dir, pdfs_dir / f"{pid}.pdf")
        if not bundle.has_pdf:
            override = _resolve_pdf_override(bundle.manifest, args.pdf_dir)
            if override is not None:
                bundle.pdf_path = override
        bundle.prepare_documents()
        cache_report = None
        if highlight_cache_dir is not None:
            cache_report = seed_highlight_cache(bundle, highlight_cache_dir, pid)
            cache_report["source_cache_misses_before"] = bundle.diagnostics.get(
                "source_cache_misses", 0)
        pdf_path = bundle.pdf_path
        pdf_available = bundle.has_pdf
        resolve_highlights = highlight_papers is None or pid in highlight_papers
        manifests_by_paper[pid] = bundle.manifest
        if not pdf_available and resolve_highlights:
            print(f"WARNING: {pid}: source PDF missing ({pdf_path}); "
                  f"sources recorded as page_only (no rect overlays)",
                  file=sys.stderr)
        paper_stats = Counter()
        cited_locations = set()
        certainty_counts = Counter()

        rows_out = []
        fill_non_null = 0
        fill_total = 0
        abstained_count = 0
        paper_field_dicts = []  # non-provenance fields per row (schema_summary)

        raw_rows = []
        for line in (extractions_dir / f"{pid}.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("_meta_commentary") or row.get("_meta"):
                continue
            raw_rows.append(row)
        # Validate all cited files before expensive PDF work, including sources
        # cited only as derivation operands. Never reinterpret or rewrite a
        # provenance location to make an invalid extraction appear valid.
        checked_locations = set()
        for row in raw_rows:
            for pv in row.get("provenance", {}).values():
                for src, _, _ in provenance_sources(pv):
                    location = src.get("location")
                    if location and location not in checked_locations:
                        _cited_bundle_file(bundle_dir, location)
                        checked_locations.add(location)
        for idx, row in enumerate(raw_rows):
            identity, fields, provenance = split_row(row, identity_fields)
            paper_field_dicts.append(fields)
            # field fill rate: non-null fields (excluding provenance) / total
            for k, v in fields.items():
                fill_total += 1
                if v is not None:
                    fill_non_null += 1
            # resolve provenance sources, count certainty, track cited files
            for fld, pv in provenance.items():
                cert = pv.get("extraction_certainty")
                if cert is not None:
                    certainty_counts[cert] += 1
                if cert == "abstained":
                    abstained_count += 1
                field_value = fields.get(fld)
                resolve_provenance_sources(
                    bundle, pv, paper_stats, cited_locations,
                    field_value=field_value, resolve_highlights=resolve_highlights,
                )
            rows_out.append({
                "row_index": idx,
                "identity": identity,
                "fields": fields,
                "provenance": provenance,
            })

        # intern evidence into v002 tables, then write compact rows + evidence
        evidence = intern_paper_evidence(rows_out)
        (out / "rows" / f"{pid}.json").write_text(
            json.dumps(rows_out, ensure_ascii=False, separators=(",", ":")))
        (out / "evidence").mkdir(parents=True, exist_ok=True)
        (out / "evidence" / f"{pid}.json").write_text(
            json.dumps({
                "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
                "paper_id": pid,
                "sources": evidence["sources"],
                "reasoning": evidence["reasoning"],
                "derivation": evidence["derivation"],
            }, ensure_ascii=False, separators=(",", ":")))
        rows_by_paper[pid] = paper_field_dicts  # for schema_summary

        global_rect_stats.update(paper_stats)

        # copy bundle files cited by provenance + manifest + table sibling pairs
        _copy_bundle(bundle_dir, out / "bundles" / pid, cited_locations)

        # Copy every routed PDF verbatim (primary plus declared supplements).
        bundle.copy_documents(out)

        paper_seconds = time.perf_counter() - paper_started
        if cache_report is not None and cache_report.get("status") == "seeded":
            fresh = (bundle.diagnostics.get("source_cache_misses", 0)
                     - cache_report.pop("source_cache_misses_before", 0))
            bundle.diagnostics["highlight_cache_misses"] = max(fresh, 0)
        else:
            bundle.diagnostics.pop("highlight_cache_hits", None)
            bundle.diagnostics.pop("highlight_cache_misses", None)
        diagnostics = dict(bundle.diagnostics)
        # AC-3 compares these keys explicitly; a zero count must be visible,
        # not just absent from the Counter.
        diagnostics.setdefault("source_cache_hits", 0)
        diagnostics.setdefault("source_cache_misses", 0)
        if cache_report is not None:
            cache_report.pop("source_cache_misses_before", None)
            diagnostics["highlight_cache_status"] = cache_report["status"]
            diagnostics["highlight_cache_seeded_keys"] = cache_report["seeded_keys"]
            diagnostics["highlight_cache_hits"] = diagnostics.get("highlight_cache_hits", 0)
            diagnostics["highlight_cache_misses"] = diagnostics.get("highlight_cache_misses", 0)
            diagnostics["highlight_cache_conflicts"] = cache_report["conflicts"]
        diagnostics["paper_seconds"] = paper_seconds
        for key in ("unique_resolution_seconds", "textpage_build_seconds", "paper_seconds"):
            diagnostics[key] = round(diagnostics.get(key, 0.0), 3)
        print(
            f"{pid} exact={paper_stats['exact']} "
            f"partial_quote={paper_stats['partial_quote']} "
            f"table_region={paper_stats['table_region']} "
            f"page_only={paper_stats['page_only']} "
            f"seconds={diagnostics['paper_seconds']:.3f} "
            f"unique_seconds={diagnostics['unique_resolution_seconds']:.3f} "
            f"cache={diagnostics.get('source_cache_hits', 0)}/"
            f"{diagnostics.get('source_cache_misses', 0)} "
            f"prose_cache={diagnostics.get('prose_search_result_cache_hits', 0)}/"
            f"{diagnostics.get('prose_search_result_cache_misses', 0)} "
            f"clip_cache={diagnostics.get('clipped_search_result_cache_hits', 0)}/"
            f"{diagnostics.get('clipped_search_result_cache_misses', 0)} "
            f"textpages={diagnostics.get('textpage_builds', 0)} "
            f"searches={diagnostics.get('search_for_calls', 0)} "
            f"cited_misses={diagnostics.get('cited_page_misses', 0)} "
            f"fallback_searches={diagnostics.get('all_page_fallback_search_for_calls', 0)}",
            flush=True,
        )

        # abstained_count from queue (definitive count of abstained items for paper)
        tc = dict(tier_by_paper[pid])
        # ensure all five canonical tiers present
        for t in ("abstained", "ambiguous", "normalizer_dispute",
                  "derived_high_leverage", "inferred_high_leverage"):
            tc.setdefault(t, 0)
        tier_totals.update(tier_by_paper[pid])

        field_fill_rate = round(fill_non_null / fill_total, 4) if fill_total else 0.0

        papers_meta.append({
            "paper_id": pid,
            "document_id": str(raw_rows[0].get("document_id")) if raw_rows else "",
            "title": bundle.manifest.get("title", pid),
            "pdf": f"pdfs/{pid}.pdf",
            "pdf_available": pdf_available,
            "documents": bundle.document_catalog(),
            "page_count": bundle.page_count(),
            "row_count": len(raw_rows),
            "abstained_count": abstained_count,
            "field_fill_rate": field_fill_rate,
            "certainty_counts": dict(certainty_counts),
            "tier_counts": tc,
            "highlight_stats": {
                key: paper_stats.get(key, 0) for key in CANONICAL_RESOLUTIONS
            } | {k: v for k, v in paper_stats.items()
                  if k not in CANONICAL_RESOLUTIONS},
            "highlight_diagnostics": diagnostics,
        })
        bundle.close()

    # queue verbatim
    (out / "queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=2))

    # review units (decision-level clustering); asserts exhaustiveness.
    # The derivation lookup lets the clusterer demote pure controlled-vocab
    # normalization (derivation_kind=='derived') to tier 3.
    derivation_lookup = build_derivation_lookup(extractions_dir, paper_ids)
    review_units, units_summary = build_and_write_units(
        out, queue, manifests_by_paper, derivation_lookup
    )

    # manifest
    exported_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    has_eval = (not args.blind) and bool(args.eval_dir)
    manifest = {
        "run_id": args.run_id,
        "domain": domain,
        "exported_at": exported_at,
        "blind": bool(args.blind),
        "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "has_eval": has_eval,
        # Schema-driven identity tuple [{field, description}], in schema order.
        # Client reads these for identity labels/ordering (falls back to its
        # own constant if absent). Source: project schema identity_dimensions.
        "identity_dimensions": identity_dims,
        "papers": papers_meta,
        "tier_totals": dict(tier_totals),
        "gate": {
            "blocking_total": blocking_total,
            "by_tier": dict(gate_by_tier),
        },
        "rect_stats": dict(global_rect_stats),
        "units": units_summary,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    # schema_summary.json — per-field type/description + observed value
    # distribution across the run (feeds the client Schema explorer; no PDF work).
    schema_summary = build_schema_summary(project, rows_by_paper)
    (out / "schema_summary.json").write_text(
        json.dumps(schema_summary, ensure_ascii=False, indent=2)
    )
    print(f"schema_summary: {schema_summary['field_count']} fields, "
          f"version={schema_summary['schema_version']}")

    # eval.json
    if not args.blind:
        eval_obj = _build_eval(
            args.eval_dir, args.adjudications, paper_ids, args.gold_config,
            args.bundle_status, args.eval_profile,
        )
        if eval_obj is not None:
            (out / "eval.json").write_text(json.dumps(eval_obj, ensure_ascii=False, indent=2))

    print()
    print("rect_stats:", dict(global_rect_stats))
    total_sources = sum(global_rect_stats.values())
    if total_sources:
        po_frac = global_rect_stats["page_only"] / total_sources
        print(f"page_only fraction: {po_frac:.1%} of {total_sources} sources")

    print_unit_plan(review_units)
    return 0


def _units_only(out: Path) -> int:
    """Recompute review_units.json from an EXISTING snapshot dir and patch
    manifest.json's units summary. Skips all PDF/rect work."""
    queue_path = out / "queue.json"
    manifest_path = out / "manifest.json"
    if not queue_path.exists():
        print(f"ERROR: {queue_path} not found (need an existing snapshot)", file=sys.stderr)
        return 1
    if not manifest_path.exists():
        print(f"ERROR: {manifest_path} not found (need an existing snapshot)", file=sys.stderr)
        return 1

    queue = json.loads(queue_path.read_text())

    # load each paper's bundle manifest from the snapshot's bundles/ dir
    manifests_by_paper = {}
    bundles_dir = out / "bundles"
    if bundles_dir.exists():
        for pdir in sorted(p for p in bundles_dir.iterdir() if p.is_dir()):
            mpath = pdir / "manifest.json"
            if mpath.exists():
                manifests_by_paper[pdir.name] = json.loads(mpath.read_text())

    review_units, units_summary = build_and_write_units(out, queue, manifests_by_paper)

    # patch manifest.json units summary in place (leave everything else intact)
    manifest = json.loads(manifest_path.read_text())
    manifest["units"] = units_summary
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    print(f"units-only: wrote review_units.json + patched manifest.json units summary in {out}")
    print_unit_plan(review_units)
    return 0


def _cited_bundle_file(bundle_dir: Path, location: str) -> Path:
    """Require a real file inside this paper's bundle, with a portable location."""
    rel = Path(location)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(
            f"invalid provenance location {location!r}: expected a bundle-relative "
            "path without '..'; repair the source extraction before exporting"
        )
    src = bundle_dir / rel
    if not src.resolve().is_relative_to(bundle_dir.resolve()):
        raise ValueError(f"provenance location escapes paper bundle: {location!r}")
    if not src.is_file():
        raise FileNotFoundError(f"cited bundle file missing: {src}")
    return src


def _copy_bundle(bundle_dir: Path, dest: Path, cited_locations: set):
    """Copy cited bundle files + manifest.json + table .json/.md sibling pairs."""
    # Validate the whole citation set before copying any file. Absolute paths
    # otherwise discard `dest`, and missing files used to vanish silently.
    for location in cited_locations:
        _cited_bundle_file(bundle_dir, location)
    dest.mkdir(parents=True, exist_ok=True)
    to_copy = set(cited_locations)
    # add table sibling pairs
    for loc in list(cited_locations):
        m = re.match(r"tables/(table_\d+)\.(json|md)$", loc)
        if m:
            base = m.group(1)
            to_copy.add(f"tables/{base}.json")
            to_copy.add(f"tables/{base}.md")
    to_copy.add("manifest.json")
    for rel in sorted(to_copy):
        src = bundle_dir / rel
        if src.exists():
            src = _cited_bundle_file(bundle_dir, rel)
            d = dest / rel
            if not d.resolve().is_relative_to(dest.resolve()):
                raise ValueError(f"bundle destination escapes snapshot: {d}")
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d)


def _load_gold_config(eval_dir: Path, gold_config_arg):
    """Resolve and read the gold_config.json -> (value_column_map, narrow_columns).

    Resolution order:
      1. --gold-config <path> if given.
      2. <eval-dir>/../../benchmark/eval_config/gold_config.json (canonical
         harness layout: iterations/iter_N -> benchmark/eval_config).
      3. <eval-dir>/gold_config.json (co-located convenience).
    Returns ({}, []) when no config is found (client falls back to its
    constant). Never fabricates columns.
    """
    candidates = []
    if gold_config_arg:
        candidates.append(Path(gold_config_arg))
    else:
        candidates.append(
            eval_dir.parent.parent / "benchmark" / "eval_config" / "gold_config.json"
        )
        candidates.append(eval_dir / "gold_config.json")
    for p in candidates:
        if p.exists():
            try:
                gc = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError) as e:
                print(f"WARNING: could not read gold_config {p}: {e}", file=sys.stderr)
                continue
            vcm = gc.get("value_column_map") or {}
            narrow = gc.get("narrow_columns") or []
            print(f"gold_config: loaded {len(vcm)} value-column mappings from {p}")
            return vcm, narrow
    print("NOTE: no gold_config.json found; eval.json omits value_column_map "
          "(client falls back to its constant)", file=sys.stderr)
    return {}, []


def _resolve_bundle_status(eval_dir: Path, bundle_status_arg):
    """Resolve + read bundle_status.json -> {paper_id: {status, mechanism, ...}}.

    Resolution: --bundle-status if given, else
    <eval-dir>/../../benchmark/eval_config/bundle_status.json. Returns {} when
    absent (clean_subset block is then omitted)."""
    candidates = []
    if bundle_status_arg:
        candidates.append(Path(bundle_status_arg))
    else:
        candidates.append(
            eval_dir.parent.parent / "benchmark" / "eval_config" / "bundle_status.json"
        )
    for p in candidates:
        if p.exists():
            try:
                bs = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError) as e:
                print(f"WARNING: could not read bundle_status {p}: {e}", file=sys.stderr)
                continue
            print(f"bundle_status: loaded {p}")
            return bs.get("papers", {}) or {}
    print("NOTE: no bundle_status.json found; eval.json omits clean_subset block",
          file=sys.stderr)
    return {}


def _accuracy_band(acc):
    """Bucket a finding_accuracy into a coarse band label for the dashboard."""
    if acc is None:
        return "unscored"
    if acc >= 1.0:
        return "1.0"
    if acc >= 0.85:
        return "0.85-0.99"
    if acc >= 0.5:
        return "0.5-0.85"
    if acc >= 0.2:
        return "0.2-0.5"
    return "<0.2"


def _build_clean_subset(score, detailed_eval, finding_scores, bundle_status):
    """Build the clean_subset presentation block + per_paper_accuracy table.

    The all-paper composite (score.json composite_score) remains the gating
    number elsewhere — this block is presentation-only. Row-weights per-paper
    finding accuracy by gold_rows (authoritative source: detailed_eval[pid]
    .gold_rows; falls back to finding_scores finding count).
    """
    if not bundle_status:
        return None

    fss = (score or {}).get("finding_scores_summary") or {}
    ppfa = fss.get("per_paper_finding_accuracy") or {}

    def _gold_rows(pid):
        d = (detailed_eval or {}).get(pid) or {}
        if isinstance(d, dict) and d.get("gold_rows") is not None:
            return d["gold_rows"]
        fp = (finding_scores or {}).get("papers", {}).get(pid) or {}
        return len(fp.get("findings", []))

    def _status(pid):
        return (bundle_status.get(pid) or {}).get("status", "clean")

    all_papers = list(ppfa.keys())
    clean = [p for p in all_papers if _status(p) == "clean"]
    missing = [p for p in all_papers if _status(p) == "missing_data"]
    gold_scope = [p for p in all_papers if _status(p) == "gold_scope"]
    bundle_suspect = [p for p in all_papers if _status(p) == "bundle_suspect"]

    def _row_weighted(pids):
        num = sum(ppfa[p] * _gold_rows(p) for p in pids)
        den = sum(_gold_rows(p) for p in pids)
        return (round(num / den, 4) if den else None), den

    def _unweighted(pids):
        vals = [ppfa[p] for p in pids]
        return round(sum(vals) / len(vals), 4) if vals else None

    all_rw, all_gold = _row_weighted(all_papers)
    clean_rw, clean_gold = _row_weighted(clean)
    composite = (score or {}).get("composite_score")

    # Clean-only composite estimate: hold the global coverage/fill terms constant
    # (they aren't re-derivable per-subset here) and substitute the clean
    # row-weighted finding accuracy into the 0.50 finding-accuracy weight.
    composite_estimate = None
    if composite is not None and clean_rw is not None and all_rw is not None:
        composite_estimate = round(composite + 0.50 * (clean_rw - all_rw), 4)

    missing_rows = [
        {
            "paper_id": p,
            "finding_accuracy": round(ppfa[p], 4),
            "gold_rows": _gold_rows(p),
            "mechanism": (bundle_status.get(p) or {}).get("mechanism", ""),
        }
        for p in sorted(missing, key=lambda x: _gold_rows(x), reverse=True)
    ]
    bundle_suspect_rows = [
        {
            "paper_id": p,
            "finding_accuracy": round(ppfa[p], 4),
            "gold_rows": _gold_rows(p),
            "mechanism": (bundle_status.get(p) or {}).get("mechanism", ""),
        }
        for p in sorted(bundle_suspect, key=lambda x: _gold_rows(x), reverse=True)
    ]

    per_paper_accuracy = sorted(
        [
            {
                "paper_id": p,
                "finding_accuracy": round(ppfa[p], 4),
                "gold_rows": _gold_rows(p),
                "bundle_status": _status(p),
                "band": _accuracy_band(ppfa[p]),
            }
            for p in all_papers
        ],
        key=lambda r: (r["finding_accuracy"], -r["gold_rows"]),
    )

    return {
        "per_paper_bundle_status": {p: _status(p) for p in all_papers},
        "missing_data_papers": missing_rows,
        "bundle_suspect_papers": bundle_suspect_rows,
        "counts": {
            "clean": len(clean),
            "missing_data": len(missing),
            "gold_scope": len(gold_scope),
            "bundle_suspect": len(bundle_suspect),
        },
        "all_papers": {
            "finding_accuracy_row_weighted": all_rw,
            "composite": composite,
            "gold_rows": all_gold,
        },
        "clean_only": {
            "finding_accuracy_row_weighted": clean_rw,
            "finding_accuracy_unweighted": _unweighted(clean),
            "composite_estimate": composite_estimate,
            "gold_rows": clean_gold,
            "composite_caveat": (
                "Presentation-only. composite_estimate holds the global "
                "coverage/entity/fill terms constant and substitutes the clean "
                "row-weighted finding accuracy into the 0.50 finding-accuracy "
                "weight. The gating composite remains the all-paper "
                f"{composite} from score.json."
            ),
        },
        "per_paper_accuracy": per_paper_accuracy,
    }


def _build_accuracy_decomposition(score, finding_scores, bundle_status):
    """Build lost-mass attribution for the measured finding-accuracy gap."""
    if not bundle_status:
        return None

    fss = (score or {}).get("finding_scores_summary") or {}
    ppfa = fss.get("per_paper_finding_accuracy") or {}
    papers = (finding_scores or {}).get("papers", {}) or {}

    bucket_meta = [
        (
            "missing_source",
            "Missing source data (paper-prep)",
            "paper_prep",
            "The relevant source table/supplement never reached the pipeline — "
            "a paper-prep ingestion gap, not an extraction error.",
        ),
        (
            "comparanda_extraction",
            "Comparanda extraction recall",
            "comparanda",
            "Comparanda emitted the foregrounded comparison but under-expanded "
            "implied rows (grade bands, per-variant reference arms). Few wrong "
            "values; the loss is missing rows.",
        ),
        (
            "bundle_quality",
            "Paper-prep bundle quality",
            "paper_prep",
            "The source table reached the pipeline, but the structured bundle "
            "or table mapping itself is suspect. Confirm against the PDF before "
            "treating this as extraction, eval, or gold-standard loss.",
        ),
        (
            "gold_errors",
            "Gold-standard errors",
            "gold",
            "The gold row itself is wrong, out of scope, or unsupported by the "
            "paper — a correct extraction scored against a bad target.",
        ),
        (
            "plumbing",
            "Eval + normalization plumbing",
            "eval",
            "The extraction was correct but the scorer/normalizer mis-paired or "
            "mis-mapped it (token-only pairing, null variant_grouping_id).",
        ),
        (
            "residual",
            "Residual / accurate",
            "none",
            "Small undiagnosed remainder on otherwise-accurate papers.",
        ),
    ]
    bucket_order = [m[0] for m in bucket_meta]
    bucket_mass = {key: 0.0 for key in bucket_order}
    bucket_papers = {key: set() for key in bucket_order}

    def _gold_rows(pid):
        fp = papers.get(pid) or {}
        return len(fp.get("findings", []))

    def _bucket(pid):
        status = bundle_status.get(pid) or {}
        data_availability = status.get("data_availability", "")
        loss_attribution = status.get("loss_attribution", "")
        if (
            data_availability == "bundle_suspect"
            or loss_attribution == "paper_prep_bundle_error"
        ):
            return "bundle_quality", data_availability, loss_attribution
        if data_availability in {"paper_prep_gap", "missing_supplement"}:
            return "missing_source", data_availability, loss_attribution
        if loss_attribution in {"comparanda_recall", "comparanda_wrong_source"}:
            return "comparanda_extraction", data_availability, loss_attribution
        if loss_attribution in {"gold_error", "gold_overspecified", "gold_scope"}:
            return "gold_errors", data_availability, loss_attribution
        if loss_attribution in {"eval_pairing", "normalization"}:
            return "plumbing", data_availability, loss_attribution
        return "residual", data_availability, loss_attribution

    per_paper = []
    for pid, finding_accuracy in ppfa.items():
        gold_rows = _gold_rows(pid)
        lost_mass = gold_rows * (1 - finding_accuracy)
        key, data_availability, loss_attribution = _bucket(pid)
        bucket_mass[key] += lost_mass
        bucket_papers[key].add(pid)
        per_paper.append(
            {
                "paper_id": pid,
                "bucket": key,
                "lost_mass": round(lost_mass, 1),
                "finding_accuracy": round(finding_accuracy, 4),
                "gold_rows": gold_rows,
                "data_availability": data_availability,
                "loss_attribution": loss_attribution,
            }
        )

    total = sum(bucket_mass.values())
    if total == 0:
        return None

    buckets = []
    for key, label, layer, gloss in bucket_meta:
        mass = bucket_mass[key]
        buckets.append(
            {
                "key": key,
                "label": label,
                "layer": layer,
                "gloss": gloss,
                "mass": round(mass, 1),
                "pct": round(mass / total * 100, 1),
                "paper_count": len(bucket_papers[key]),
            }
        )

    per_paper.sort(key=lambda r: r["lost_mass"], reverse=True)
    headline = next(
        b["pct"] for b in buckets if b["key"] == "comparanda_extraction"
    )
    return {
        "total_lost_mass": round(total, 1),
        "headline_comparanda_pct": headline,
        "buckets": buckets,
        "per_paper": per_paper,
    }


def _load_field_coverage_summary(eval_dir: Path):
    """Load the best field-coverage aggregate available in the eval run."""
    candidates = (
        eval_dir / "adjusted_full_benchmark_summary.json",
        eval_dir / "final_field_coverage_summary.json",
    )
    for p in candidates:
        if not p.exists():
            continue
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARNING: could not read field coverage summary {p}: {e}",
                  file=sys.stderr)
    return None


def _build_eval(eval_dir, adjudications, paper_ids, gold_config_arg=None,
                bundle_status_arg=None, eval_profile="full"):
    if not eval_dir:
        return None
    ed = Path(eval_dir)
    if not ed.exists():
        print(f"WARNING: eval-dir {ed} missing; skipping eval.json", file=sys.stderr)
        return None

    def _load(name):
        p = ed / name
        return json.loads(p.read_text()) if p.exists() else None

    score = _load("score.json") or {}

    adj_list = []
    if adjudications:
        ap = Path(adjudications)
        if ap.exists():
            adj_list = json.loads(ap.read_text()).get("adjudications", [])

    explainer = ""
    ex = ed / "eval_explainer.md"
    if ex.exists():
        explainer = ex.read_text()

    value_column_map, narrow_columns = _load_gold_config(ed, gold_config_arg)

    if eval_profile == "reviewer-demo":
        eval_obj = {
            "score": score,
            "finding_scores": {"papers": {}},
            "claim_faithfulness": {},
            "comparisons": {},
            "adjudications": adj_list,
            "explainer_md": explainer,
            "value_column_map": value_column_map,
            "narrow_columns": narrow_columns,
        }
        field_coverage_summary = _load_field_coverage_summary(ed)
        if field_coverage_summary is not None:
            eval_obj["field_coverage_summary"] = field_coverage_summary
        return eval_obj

    finding_scores = _load("finding_scores.json") or {"papers": {}}
    claim_faithfulness = _load("claim_faithfulness.json") or {}
    detailed_eval = _load("detailed_eval.json") or {}
    comparisons = {}
    # Prefer full comparison exports, but current harness snapshots may only
    # carry the per-paper slim files used by the portal's Forensics view.
    for cmp_dir in (ed / "comparisons", ed / "comparisons_slim"):
        if not cmp_dir.exists():
            continue
        for pid in paper_ids:
            if pid in comparisons:
                continue
            f = cmp_dir / f"{pid}.json"
            if f.exists():
                comparisons[pid] = json.loads(f.read_text())

    bundle_status = _resolve_bundle_status(ed, bundle_status_arg)
    clean_subset = _build_clean_subset(
        score, detailed_eval, finding_scores, bundle_status
    )
    accuracy_decomposition = _build_accuracy_decomposition(
        score, finding_scores, bundle_status
    )

    eval_obj = {
        "score": score,
        "finding_scores": finding_scores,
        "claim_faithfulness": claim_faithfulness,
        "comparisons": comparisons,
        "adjudications": adj_list,
        "explainer_md": explainer,
        # Gold-column -> extraction-field map (+ narrow columns), read from the
        # project's gold_config.json. The client reads these from the snapshot
        # instead of a hardcoded constant (INTERPRETATION_LOG [HARDCODE] #1).
        # Empty when no gold_config is available -> client falls back.
        "value_column_map": value_column_map,
        "narrow_columns": narrow_columns,
    }
    # clean_subset: presentation-only breakdown (clean vs missing_data vs
    # gold_scope) + per-paper accuracy table. The all-paper composite stays the
    # gating number; this never replaces it.
    if clean_subset is not None:
        eval_obj["clean_subset"] = clean_subset
    if accuracy_decomposition is not None:
        eval_obj["accuracy_decomposition"] = accuracy_decomposition
    return eval_obj


if __name__ == "__main__":
    raise SystemExit(main())
