"""Paper Prep — turn a Phase-3 run output into an agent-consumable bundle.

Inputs (per paper, under `<run_dir>/`):
    claude_first_pass.md                       — Sonnet first-pass whole-paper
    docling.md                                 — Docling whole-paper markdown
    docling_tables.json                        — Docling structured tables
    docling_pictures.json (optional)           — Docling picture metadata
    docling_pictures/picture_<i>.png (optional)— rendered figure images
    tables.json                                — LLM ∪ Docling table enumeration
    claude_first_pass_arbited_table<id>.md     — per-table arbited final outputs
    merged_table<id>.json                      — full per-cell provenance
    run_status.json                            — per-paper run summary

Output bundle (under `<output_root>/<paper_id>/`):
    manifest.json
    sections/<slug>.md
    tables/table_<id>.{json,md}
    figures/figure_<i>.png
    _provenance/
        run_status.json
        tables/table_<id>.json
        arbiter_notes/table_<id>.md

The agent should read manifest.json + the clean section/table/figure files only;
`_provenance/` is for debugging audits, not consumption.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paper_prep.markdown_parser import parse_extractor_markdown


DEFAULT_OUTPUT_ROOT = Path(
    "/path/to/comparanda-<domain>/projects/ugt1a1/data/processed"
)


# ────────────────────────────────────────────────────────────────────────────
# Section parsing
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class Section:
    name: str  # filename slug, no extension
    heading: str  # original heading text
    body: str  # markdown body (heading included)


_TABLE_CAPTION_LINE = re.compile(r"^\s*\*{0,2}\s*Table\s+[A-Za-z]*\d+[A-Za-z]*", re.IGNORECASE)


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "section"


def _strip_table_captions(text: str) -> str:
    """Drop any lines that look like '**Table N.** ...' captions.

    Tables live in `tables/`; the section markdown should reference them by
    name without inlining their captions.
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        if _TABLE_CAPTION_LINE.match(line.strip()):
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def extract_sections(first_pass_md: str) -> tuple[str | None, list[Section]]:
    """Walk a first-pass markdown and split it into `(title, sections)`.

    Title comes from the first `# ...` heading line if present (lines below it
    until the first blank/`##` are also part of the title block, but we just
    keep the first H1 line as the title).

    Sections start at each `## <heading>` line. Sub-sections (`### ...`) are
    kept inline within their parent's body. The body always begins with the
    `## ...` heading line itself for readability.
    """
    lines = first_pass_md.splitlines()
    title: str | None = None

    # Detect the title. Two-pass:
    #   1. Skip the standard `# <paper_id> (Sonnet first-pass extraction)`
    #      synthetic header line.
    #   2. Take the first non-empty, non-author-list, non-`##` content line as
    #      the paper title.
    skip_synthetic = re.compile(
        r"^#\s+\S.*\((?:Sonnet first-pass extraction|.*extraction)\)\s*$",
        re.IGNORECASE,
    )
    h1_match = re.compile(r"^#\s+(.+?)\s*$")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if skip_synthetic.match(line):
            # Look ahead for the actual title.
            for j in range(i + 1, min(i + 10, len(lines))):
                if not lines[j].strip():
                    continue
                if lines[j].lstrip().startswith("##"):
                    break
                title = lines[j].strip().strip("*_")
                break
            break
        m = h1_match.match(line)
        if m:
            title = m.group(1).strip()
            break
        # First non-empty line wasn't a heading — treat it as the title.
        title = line.strip().strip("*_")
        break

    sections: list[Section] = []
    used_slugs: dict[str, int] = {}

    # Find all `## ` heading lines.
    starts: list[tuple[int, str]] = []  # (line_idx, heading)
    for i, line in enumerate(lines):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            starts.append((i, m.group(1).strip()))

    for k, (idx, heading) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        body_lines = lines[idx:end]
        body = "\n".join(body_lines).rstrip() + "\n"
        body = _strip_table_captions(body)

        # Pick a slug for the filename, with collision handling.
        base_slug = _slugify(heading)
        if base_slug in used_slugs:
            used_slugs[base_slug] += 1
            slug = f"{base_slug}-{used_slugs[base_slug]}"
        else:
            used_slugs[base_slug] = 1
            slug = base_slug

        sections.append(Section(name=slug, heading=heading, body=body))

    return title, sections


# ────────────────────────────────────────────────────────────────────────────
# Table extraction
# ────────────────────────────────────────────────────────────────────────────


_LLM_ONLY_MARKER = "(LLM-only, no cross-validation)"
_TABLE_CAPTION_RE = re.compile(
    r"^\s*\*{0,2}\s*Table\s+([A-Za-z]*\d+[A-Za-z]*)\*{0,2}[\.\:]?\s*\*{0,2}\s*(.*?)\s*\*{0,2}\s*$",
    re.IGNORECASE,
)
_ARBITRATION_NOTES_HEADING = re.compile(r"^##\s+Arbitration notes\s*$", re.IGNORECASE)


def _split_arbitration_notes(arbited_md: str) -> tuple[str, str | None]:
    """Return `(body, arbitration_notes)` from an arbited markdown file.

    Body excludes the `## Arbitration notes` section and everything after.
    `arbitration_notes` is the notes section's full text (including heading),
    or None if no notes section is present.
    """
    lines = arbited_md.splitlines()
    cut: int | None = None
    for i, line in enumerate(lines):
        if _ARBITRATION_NOTES_HEADING.match(line):
            cut = i
            break
    if cut is None:
        return arbited_md, None
    body = "\n".join(lines[:cut]).rstrip() + "\n"
    notes = "\n".join(lines[cut:]).rstrip() + "\n"
    return body, notes


def _parse_table_from_arbited(
    arbited_md: str, table_id: str
) -> tuple[list[str], list[list[str]], str | None]:
    """Extract `(headers, rows, title)` from the arbited markdown body.

    We piggyback on the existing markdown table parser. The arbited file is
    expected to contain exactly one pipe-style table.
    """
    # Write to a temp-y inline parse: parse_extractor_markdown takes a path.
    # We'll write the content to a NamedTemporaryFile and let the parser run.
    import tempfile

    title: str | None = None
    for line in arbited_md.splitlines():
        m = _TABLE_CAPTION_RE.match(line.strip())
        if m and m.group(1).strip().lower() == str(table_id).lower():
            title = (m.group(2) or "").strip().strip("*").strip() or None
            break
        if title is None and m:
            # Fallback: any caption matches if we haven't seen the right one.
            title = (m.group(2) or "").strip().strip("*").strip() or None

    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(arbited_md)
        tmp_path = Path(tf.name)
    try:
        out = parse_extractor_markdown(tmp_path, paper_id="_prep", extractor="arbited")
    finally:
        tmp_path.unlink(missing_ok=True)

    if not out.tables:
        return [], [], title
    # Take the first parsed table.
    t = out.tables[0]
    return list(t.headers), [list(r) for r in t.rows], title


def _grid_to_clean_rows(grid: list[list[Any]]) -> tuple[list[str], list[list[str]]]:
    """Convert a Docling grid (list of rows of cell-text) into headers + rows.

    Treats the first row as the header. Falls back to placeholder col_n names
    when the grid is empty.
    """
    if not grid:
        return [], []
    text_grid: list[list[str]] = []
    for row in grid:
        text_grid.append(
            [str(cell).strip() if cell is not None else "" for cell in row]
        )
    if len(text_grid) == 1:
        n_cols = len(text_grid[0])
        return [f"col_{i}" for i in range(n_cols)], [text_grid[0]]
    headers = text_grid[0]
    rows = text_grid[1:]
    return headers, rows


def _docling_table_caption_id(caption: str) -> tuple[str | None, str | None]:
    """Pull `(table_id, title)` out of a Docling table caption string."""
    if not caption:
        return None, None
    m = _TABLE_CAPTION_RE.match(caption.strip())
    if m:
        return m.group(1).strip(), (m.group(2) or "").strip() or None
    return None, caption.strip() or None


def _docling_tables_index(
    docling_tables: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Index Docling structured tables by ID where the caption gives one.

    Returns `{table_id: {"page": int|None, "title": str|None, "raw": dict, "index": int}}`.
    Tables without an extractable ID are excluded; the caller can iterate the
    raw list separately to pick them up as `docling_only` candidates.
    """
    indexed: dict[str, dict[str, Any]] = {}
    for i, t in enumerate(docling_tables):
        tid, title = _docling_table_caption_id(t.get("caption") or "")
        if tid is None:
            continue
        if tid in indexed:
            # Keep the first occurrence; later duplicates are usually
            # cross-page banner re-emissions.
            continue
        indexed[tid] = {
            "page": t.get("page"),
            "title": title,
            "raw": t,
            "index": i,
        }
    return indexed


def _cell_bag(grid: list[list[Any]]) -> set[str]:
    """Bag of non-empty normalized cell strings, useful for cheap matching."""
    bag: set[str] = set()
    for row in grid or []:
        for cell in row:
            s = str(cell or "").strip().lower()
            if s and len(s) >= 2:
                bag.add(s)
    return bag


def _match_docling_only_to_llm(
    docling_table: dict[str, Any],
    llm_tables_by_id: dict[str, tuple[list[str], list[list[str]]]],
    used_ids: set[str],
    min_score: float = 0.15,
) -> str | None:
    """Try to assign a Docling-found table to an LLM table by content overlap.

    Looks for the unused LLM table with the highest cell-bag overlap. The
    threshold (min_score, default 0.15) is intentionally low because Docling
    OCR artifacts (`=` -> `5`, `≥` -> `$`, etc.) substantially reduce
    cell-by-cell overlap. Returns the matched LLM table_id or None.
    """
    d_bag = _cell_bag(docling_table.get("grid") or [])
    if not d_bag:
        return None
    best_id: str | None = None
    best_score = 0.0
    for tid, (headers, rows) in llm_tables_by_id.items():
        if tid in used_ids:
            continue
        l_bag = _cell_bag([headers] + rows) if headers else _cell_bag(rows)
        if not l_bag:
            continue
        inter = len(d_bag & l_bag)
        union = len(d_bag | l_bag)
        score = inter / union if union else 0.0
        if score > best_score:
            best_score = score
            best_id = tid
    if best_score >= min_score:
        return best_id
    return None


def _render_table_md(
    headers: list[str], rows: list[list[str]], title: str | None
) -> str:
    """Render a table back as a simple GFM markdown body, with optional title."""
    out: list[str] = []
    if title:
        out.append(f"**{title}**")
        out.append("")
    if headers:
        out.append("| " + " | ".join(headers) + " |")
        out.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        # Pad/truncate to header width.
        if headers:
            n = len(headers)
            r = (row + [""] * n)[:n]
        else:
            r = row
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out) + "\n"


# ────────────────────────────────────────────────────────────────────────────
# Figure extraction
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class FigureCandidate:
    src_index: int  # index into docling_pictures
    page: int | None
    bbox: list[float] | None
    caption: str


def _is_decorative(
    caption: str, page: int | None, bbox: list[float] | None
) -> bool:
    """Heuristic: drop pictures that look like journal furniture (page-corner
    glyphs, issue marks, header ornaments) rather than real figures.

    Captioned pictures are always kept. Uncaptioned pictures only get the
    size-based filter applied when they are on page 1 (where banner glyphs,
    publisher logos, and journal-issue marks live). On page 2+, an
    uncaptioned figure is more likely to be a real figure whose caption
    Docling's matcher missed than to be furniture — keep it and let the
    agent (and downstream review) sort it out.
    """
    if caption.strip():
        return False
    # Page 2+ uncaptioned: keep. Caption matching is best-effort, not a gate.
    if page is not None and page >= 2:
        return False
    if bbox is None or len(bbox) != 4:
        # No bbox info — be conservative and keep it.
        return False
    width = abs(bbox[2] - bbox[0])
    height = abs(bbox[3] - bbox[1])
    area = width * height
    # Standard letter-size page ≈ 612 x 792 pt = 484,704 sq pt.
    page_area = 612.0 * 792.0
    if area <= 0:
        return False
    if width < 30 or height < 30:
        return True
    return area / page_area < 0.01


def _figure_label(caption: str, fallback_id: int) -> str:
    """Produce a short label like 'Fig. 1' from a caption, falling back to
    'Figure N' if the caption doesn't clearly start with one."""
    cap = caption.strip()
    m = re.match(r"^(fig(?:ure|\.)?\s*\d+[A-Za-z]?)\b", cap, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return f"Figure {fallback_id}"


# ────────────────────────────────────────────────────────────────────────────
# Build entry point
# ────────────────────────────────────────────────────────────────────────────


def _table_id_sort_key(tid: str) -> tuple:
    """Sort '1' < '2' < '10' < 'A1' < 'docling_0'."""
    if tid.isdigit():
        return (0, int(tid), "")
    m = re.match(r"^(\d+)([A-Za-z].*)$", tid)
    if m:
        return (0, int(m.group(1)), m.group(2).upper())
    if tid.startswith("docling_"):
        try:
            return (2, int(tid.split("_", 1)[1]), "")
        except (ValueError, IndexError):
            return (2, 0, tid.upper())
    return (1, 0, tid.upper())


def build(
    paper_id: str,
    run_dir: Path,
    output_root: Path,
    pdf_path: Path,
) -> Path:
    """Build the agent-consumable bundle for one paper. Returns the manifest path.

    The output bundle replaces any existing bundle for this paper at
    `<output_root>/<paper_id>/`.
    """
    run_dir = Path(run_dir)
    output_root = Path(output_root)
    pdf_path = Path(pdf_path)

    bundle_dir = output_root / paper_id
    sections_dir = bundle_dir / "sections"
    tables_dir = bundle_dir / "tables"
    figures_dir = bundle_dir / "figures"
    prov_dir = bundle_dir / "_provenance"
    prov_tables_dir = prov_dir / "tables"
    prov_notes_dir = prov_dir / "arbiter_notes"

    # Wipe and recreate the bundle dir so the build is idempotent.
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    for d in (
        sections_dir,
        tables_dir,
        figures_dir,
        prov_tables_dir,
        prov_notes_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # ── Sections ────────────────────────────────────────────────────────
    first_pass_md_path = run_dir / "claude_first_pass.md"
    if not first_pass_md_path.exists():
        raise FileNotFoundError(
            f"claude_first_pass.md not found under {run_dir} — cannot build sections"
        )
    first_pass_md = first_pass_md_path.read_text()
    title, sections = extract_sections(first_pass_md)

    section_entries: list[dict[str, Any]] = []
    for s in sections:
        rel = f"sections/{s.name}.md"
        (sections_dir / f"{s.name}.md").write_text(s.body)
        section_entries.append(
            {"name": s.name, "path": rel, "heading": s.heading}
        )

    # ── Tables ──────────────────────────────────────────────────────────
    docling_tables_path = run_dir / "docling_tables.json"
    docling_tables_blob = (
        json.loads(docling_tables_path.read_text()) if docling_tables_path.exists() else {}
    )
    docling_tables = docling_tables_blob.get("tables") or []
    docling_by_id = _docling_tables_index(docling_tables)

    # Collect all per-table arbited markdowns.
    arbited_files = sorted(
        run_dir.glob("claude_first_pass_arbited_table*.md"),
        key=lambda p: _table_id_sort_key(
            re.sub(r"^.*arbited_table([A-Za-z0-9]+)\.md$", r"\1", p.name)
        ),
    )

    table_entries: list[dict[str, Any]] = []
    consumed_docling_ids: set[str] = set()
    llm_tables_by_id: dict[str, tuple[list[str], list[list[str]]]] = {}

    for arb_path in arbited_files:
        m = re.search(r"arbited_table([A-Za-z0-9]+)\.md$", arb_path.name)
        if not m:
            continue
        tid = m.group(1)

        arb_md = arb_path.read_text()
        is_llm_only = _LLM_ONLY_MARKER in arb_md.splitlines()[0] if arb_md else False
        body_md, notes_md = _split_arbitration_notes(arb_md)
        headers, rows, title_from_arb = _parse_table_from_arbited(body_md, tid)
        llm_tables_by_id[tid] = (headers, rows)

        # Page from Docling structured output, if Docling has the table.
        page = None
        title_text = title_from_arb
        if tid in docling_by_id:
            docling_meta = docling_by_id[tid]
            page = docling_meta.get("page")
            title_text = title_text or docling_meta.get("title")
            consumed_docling_ids.add(tid)

        table_json = {
            "id": tid,
            "title": title_text,
            "page": page,
            "headers": headers,
            "rows": rows,
        }
        (tables_dir / f"table_{tid}.json").write_text(
            json.dumps(table_json, indent=2, ensure_ascii=False)
        )

        # Markdown rendering — keep the body of the arbited markdown verbatim
        # (sans arbitration notes) so the agent sees footnotes, ordering, etc.
        (tables_dir / f"table_{tid}.md").write_text(body_md)

        entry: dict[str, Any] = {
            "id": tid,
            "title": title_text,
            "page": page,
            "path": f"tables/table_{tid}.json",
            "markdown_path": f"tables/table_{tid}.md",
            "cross_validated": not is_llm_only,
        }
        if is_llm_only:
            entry["source"] = "llm_only"
            entry["note"] = (
                "Docling did not extract this table; LLM first-pass used as "
                "final without cross-validation."
            )
        table_entries.append(entry)

        # Provenance
        merged_path = run_dir / f"merged_table{tid}.json"
        if merged_path.exists():
            shutil.copy2(merged_path, prov_tables_dir / f"table_{tid}.json")
        if notes_md:
            (prov_notes_dir / f"table_{tid}.md").write_text(notes_md)

    # Docling-only tables: any in docling_by_id we didn't consume, plus any
    # captionless Docling tables.
    docling_unmatched_with_id = [
        (tid, meta)
        for tid, meta in docling_by_id.items()
        if tid not in consumed_docling_ids
    ]
    for tid, meta in docling_unmatched_with_id:
        # Try to match against the LLM tables we've already written; if a
        # match is found, just skip (we already wrote that table from the
        # arbited markdown — the Docling side is non-canonical).
        match = _match_docling_only_to_llm(
            meta["raw"], llm_tables_by_id, consumed_docling_ids
        )
        if match is not None:
            consumed_docling_ids.add(match)
            continue
        # Genuinely Docling-only.
        headers, rows = _grid_to_clean_rows(meta["raw"].get("grid") or [])
        page = meta.get("page")
        title_text = meta.get("title")
        out_id = tid
        table_json = {
            "id": out_id,
            "title": title_text,
            "page": page,
            "headers": headers,
            "rows": rows,
        }
        (tables_dir / f"table_{out_id}.json").write_text(
            json.dumps(table_json, indent=2, ensure_ascii=False)
        )
        (tables_dir / f"table_{out_id}.md").write_text(
            _render_table_md(headers, rows, title_text)
        )
        table_entries.append(
            {
                "id": out_id,
                "title": title_text,
                "page": page,
                "path": f"tables/table_{out_id}.json",
                "markdown_path": f"tables/table_{out_id}.md",
                "cross_validated": False,
                "source": "docling_only",
                "note": (
                    "LLM first-pass did not extract this table; Docling-only, "
                    "no cross-validation."
                ),
            }
        )

    # Captionless Docling tables (no extractable ID).
    for i, t in enumerate(docling_tables):
        tid_existing, _ = _docling_table_caption_id(t.get("caption") or "")
        if tid_existing is not None:
            continue  # handled above
        # Skip degenerate "tables" — Docling sometimes emits a 1x1 cell that
        # is just a stray caption line, or a wide row of column headers with
        # no body rows. These shouldn't surface as docling_only entries.
        n_rows = int(t.get("num_rows") or 0)
        n_cols = int(t.get("num_cols") or 0)
        if n_rows < 2 or n_cols < 2:
            continue
        match = _match_docling_only_to_llm(t, llm_tables_by_id, consumed_docling_ids)
        if match is not None:
            consumed_docling_ids.add(match)
            continue
        # No match — emit as docling_<index>.
        headers, rows = _grid_to_clean_rows(t.get("grid") or [])
        page = t.get("page")
        out_id = f"docling_{i}"
        table_json = {
            "id": out_id,
            "title": None,
            "page": page,
            "headers": headers,
            "rows": rows,
        }
        (tables_dir / f"table_{out_id}.json").write_text(
            json.dumps(table_json, indent=2, ensure_ascii=False)
        )
        (tables_dir / f"table_{out_id}.md").write_text(
            _render_table_md(headers, rows, None)
        )
        table_entries.append(
            {
                "id": out_id,
                "title": None,
                "page": page,
                "path": f"tables/table_{out_id}.json",
                "markdown_path": f"tables/table_{out_id}.md",
                "cross_validated": False,
                "source": "docling_only",
                "note": (
                    "Docling-detected table without a caption; not cross-validated. "
                    "Likely a real table the LLM missed, or a layout artifact."
                ),
            }
        )

    table_entries.sort(key=lambda e: _table_id_sort_key(e["id"]))

    # ── Figures ─────────────────────────────────────────────────────────
    figure_entries: list[dict[str, Any]] = []
    pictures_path = run_dir / "docling_pictures.json"
    pictures_dir = run_dir / "docling_pictures"
    if pictures_path.exists():
        pictures_blob = json.loads(pictures_path.read_text())
        next_id = 1
        for pic in pictures_blob.get("pictures") or []:
            caption = (pic.get("caption") or "").strip()
            page = pic.get("page")
            bbox = pic.get("bbox")
            if _is_decorative(caption, page, bbox):
                continue
            src_idx = pic.get("index")
            src_png = pictures_dir / f"picture_{src_idx}.png"
            if not src_png.exists():
                # Skip if the rendered image isn't actually on disk; nothing
                # for the agent to look at.
                continue
            dst_png = figures_dir / f"figure_{next_id}.png"
            shutil.copy2(src_png, dst_png)
            figure_entries.append(
                {
                    "id": next_id,
                    "label": _figure_label(caption, next_id),
                    "caption": caption,
                    "page": page,
                    "path": f"figures/figure_{next_id}.png",
                }
            )
            next_id += 1

    # ── Provenance: run_status ─────────────────────────────────────────
    run_status_path = run_dir / "run_status.json"
    if run_status_path.exists():
        shutil.copy2(run_status_path, prov_dir / "run_status.json")

    # ── Manifest ────────────────────────────────────────────────────────
    manifest = {
        "paper_id": paper_id,
        "source_pdf": str(pdf_path),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "title": title,
        "sections": section_entries,
        "tables": table_entries,
        "figures": figure_entries,
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest_path


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paper_id")
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--pdf", required=True, type=Path)
    ap.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Where the bundle goes (default: {DEFAULT_OUTPUT_ROOT})",
    )
    args = ap.parse_args()

    manifest_path = build(
        paper_id=args.paper_id,
        run_dir=args.run_dir,
        output_root=args.output_root,
        pdf_path=args.pdf,
    )
    print(f"prep: wrote {manifest_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
