"""Docling extractor with TableFormer ACCURATE mode.

Produces markdown plus structured table JSON (cells, headers, merged cell info)
and per-figure PNGs + a pictures index JSON.

`extract_split`: writes the same conversion into a two-tree
layout (`paper_runs/` for things Sonnet sees, `arbiter_inputs/` for things
Sonnet must not see), plus per-section markdown files split via
HierarchicalChunker — the basis for the section-iterating first-pass.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from docling.document_converter import DocumentConverter, PdfFormatOption

from paper_prep.markdown_parser import TABLE_CAPTION_RE


def _build_converter() -> DocumentConverter:
    """Build a DocumentConverter using TableFormer ACCURATE mode.

    Also enables picture image generation so we can render figures back out as
    PNGs at 2x scale.
    """
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
    pipeline_options.table_structure_options.do_cell_matching = True
    pipeline_options.do_ocr = True
    # Enable picture image generation so PictureItem.get_image() returns a PIL
    # Image at the requested scale.
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = 2.0

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )


def _table_to_dict(table_item: Any, index: int) -> dict[str, Any]:
    """Serialize a TableItem into a JSON-friendly dict with cells, headers, spans."""
    data = table_item.data
    cells: list[dict[str, Any]] = []

    # table_cells is the canonical list of cells (includes merged / header info).
    raw_cells = getattr(data, "table_cells", []) or []
    for cell in raw_cells:
        cells.append(
            {
                "text": getattr(cell, "text", ""),
                "row_span": getattr(cell, "row_span", 1),
                "col_span": getattr(cell, "col_span", 1),
                "start_row_offset_idx": getattr(cell, "start_row_offset_idx", None),
                "end_row_offset_idx": getattr(cell, "end_row_offset_idx", None),
                "start_col_offset_idx": getattr(cell, "start_col_offset_idx", None),
                "end_col_offset_idx": getattr(cell, "end_col_offset_idx", None),
                "column_header": getattr(cell, "column_header", False),
                "row_header": getattr(cell, "row_header", False),
                "row_section": getattr(cell, "row_section", False),
            }
        )

    # Also capture a 2D grid view for convenience (text only).
    grid_view: list[list[str]] = []
    grid = getattr(data, "grid", None)
    if grid:
        for row in grid:
            grid_view.append([getattr(c, "text", "") for c in row])

    try:
        md = table_item.export_to_markdown()
    except Exception:
        md = None

    caption = ""
    try:
        raw_caption = table_item.caption_text
        # Could be a method or a property depending on docling version.
        if callable(raw_caption):
            try:
                raw_caption = raw_caption()
            except Exception:
                raw_caption = ""
        caption = raw_caption or ""
        if not isinstance(caption, str):
            caption = str(caption)
    except Exception:
        caption = ""

    # Pull the first prov record's page number + bbox if available.
    page_no: int | None = None
    bbox: list[float] | None = None
    try:
        prov = getattr(table_item, "prov", None) or []
        if prov:
            p0 = prov[0]
            page_no = getattr(p0, "page_no", None)
            b = getattr(p0, "bbox", None)
            if b is not None:
                bbox = [
                    float(getattr(b, "l", 0.0)),
                    float(getattr(b, "t", 0.0)),
                    float(getattr(b, "r", 0.0)),
                    float(getattr(b, "b", 0.0)),
                ]
    except Exception:
        pass

    return {
        "index": index,
        "num_rows": getattr(data, "num_rows", len(grid_view)),
        "num_cols": getattr(data, "num_cols", len(grid_view[0]) if grid_view else 0),
        "caption": caption,
        "page": page_no,
        "bbox": bbox,
        "cells": cells,
        "grid": grid_view,
        "markdown": md,
    }


def _picture_to_dict(picture_item: Any, doc: Any, index: int) -> dict[str, Any]:
    """Serialize a PictureItem into a JSON-friendly dict (no image bytes).

    The image itself is written separately as a PNG; this dict captures the
    metadata (page, bbox, caption text) the manifest builder needs.
    """
    page_no: int | None = None
    bbox: list[float] | None = None
    try:
        prov = getattr(picture_item, "prov", None) or []
        if prov:
            p0 = prov[0]
            page_no = getattr(p0, "page_no", None)
            b = getattr(p0, "bbox", None)
            if b is not None:
                bbox = [
                    float(getattr(b, "l", 0.0)),
                    float(getattr(b, "t", 0.0)),
                    float(getattr(b, "r", 0.0)),
                    float(getattr(b, "b", 0.0)),
                ]
    except Exception:
        pass

    caption = ""
    try:
        raw_caption = picture_item.caption_text(doc)
        if callable(raw_caption):
            try:
                raw_caption = raw_caption()
            except Exception:
                raw_caption = ""
        caption = raw_caption or ""
        if not isinstance(caption, str):
            caption = str(caption)
    except Exception:
        caption = ""

    return {
        "index": index,
        "page": page_no,
        "bbox": bbox,
        "caption": caption,
    }




# ---------------------------------------------------------------------------
# Section split via HierarchicalChunker
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Lowercase + non-alnum→`_`, trimmed; empty → 'frontmatter'.

    Capped at 150 chars so the resulting filename (`NN_<slug>.md` or
    `<slug>.md`) stays under macOS's 255-byte limit.
    """
    s = _SLUG_RE.sub("_", (text or "").strip().lower()).strip("_")
    if len(s) > 150:
        s = s[:150].rstrip("_")
    return s or "frontmatter"


def _table_id_from_caption(caption: str) -> str | None:
    """Extract '1' from 'Table 1. ...' style captions; None if no match."""
    if not caption:
        return None
    for line in caption.splitlines():
        m = TABLE_CAPTION_RE.match(line)
        if m:
            return m.group(1)
    return None


def _extract_doc_title(dl_doc: Any) -> str:
    """Prefer first TitleItem text; fall back to dl_doc.name."""
    try:
        from docling_core.types.doc import TitleItem  # type: ignore
    except Exception:
        TitleItem = None  # noqa: N806

    if TitleItem is not None:
        try:
            for item, _level in dl_doc.iterate_items():
                if isinstance(item, TitleItem):
                    txt = (getattr(item, "text", "") or "").strip()
                    if txt:
                        return txt
        except Exception:
            pass

    return str(getattr(dl_doc, "name", "") or "").strip()


def _ref_to_index(self_ref: str, kind: str) -> int | None:
    """`#/tables/3` → 3 when kind == 'tables'; None otherwise."""
    if not self_ref:
        return None
    prefix = f"#/{kind}/"
    if not self_ref.startswith(prefix):
        return None
    try:
        return int(self_ref[len(prefix):])
    except ValueError:
        return None


def _render_item_full(item: Any, dl_doc: Any) -> str:
    """Render one DoclingDocument item to markdown (full, table content inline)."""
    cls = type(item).__name__
    if cls == "SectionHeaderItem":
        level = max(1, min(6, int(getattr(item, "level", 2) or 2)))
        return "#" * level + " " + (getattr(item, "text", "") or "")
    if cls == "TitleItem":
        return "# " + (getattr(item, "text", "") or "")
    if cls == "TableItem":
        # Don't render the bound caption — captions almost always appear as
        # a sibling TextItem rendered separately (verified on iyer). If a
        # paper turns up where the caption exists ONLY as a Docling-bound
        # caption, Sonnet's prose verification will flag the missing line.
        try:
            return item.export_to_markdown()
        except Exception:
            return ""
    if cls == "PictureItem":
        # Same logic as tables: caption usually appears as adjacent TextItem.
        # We omit the bound caption here to avoid duplication; the placeholder
        # below is just a hint that something visual was here.
        return "_[Figure]_"
    if cls == "ListItem":
        marker = getattr(item, "marker", None) or "-"
        return f"{marker} {(getattr(item, 'text', '') or '')}"
    # TextItem and anything else with .text
    txt = getattr(item, "text", None)
    if txt is not None:
        return str(txt)
    return ""


def _render_item_stripped(item: Any, dl_doc: Any, ref_to_table_id: dict[str, str]) -> str:
    """Same as _render_item_full but tables → placeholder comments.

    When we have a caption-derived ID (e.g. "1"), the placeholder includes the
    number so Sonnet can match the placeholder to a "Table N." in the PDF.
    When the ID is a `docling_idx_N` fallback (Docling could not pair a caption,
    common in JCO-style papers), we omit the number — letting Sonnet derive
    the canonical ID from the PDF without anchoring to a meaningless internal
    counter.
    """
    cls = type(item).__name__
    if cls == "TableItem":
        ref = getattr(item, "self_ref", "") or ""
        tid = ref_to_table_id.get(ref, "")
        if tid and not tid.startswith("docling_idx_"):
            return f"<!-- Table {tid} appears here -->"
        return "<!-- table appears here -->"
    return _render_item_full(item, dl_doc)


def _build_sections(
    dl_doc: Any,
    tables_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    """Walk HierarchicalChunker output, group adjacent same-heading chunks
    into logical sections, return a dict with title + sections[].

    Each section dict has: index, slug, headings, page_range (start,end),
    table_ids (caption-derived), figure_indexes (ints into doc.pictures),
    full_md (table content inline), stripped_md (tables → placeholders).
    """
    from docling_core.transforms.chunker.hierarchical_chunker import HierarchicalChunker

    # First pass: caption-derived ID per table from the docling table caption
    # field (works when Docling bound a Caption to the TableItem).
    ref_to_table_id: dict[str, str] = {}
    for tdict in tables_payload:
        idx = tdict["index"]
        ref = f"#/tables/{idx}"
        tid = _table_id_from_caption(tdict.get("caption", ""))
        if tid is not None:
            ref_to_table_id[ref] = tid

    # Second pass: walk iterate_items() in document order to (a) build a
    # ref→position map for placing pictures into sections and (b) collect a
    # by-position list of captions seen as plain TextItems, so we can pair
    # captions to TableItems when Docling didn't bind them.
    ref_position: dict[str, int] = {}
    plain_caption_at_pos: dict[int, str] = {}
    table_ref_at_pos: dict[int, str] = {}
    pos = 0
    for item, _level in dl_doc.iterate_items():
        ref = getattr(item, "self_ref", "") or ""
        if ref:
            ref_position[ref] = pos
        cls = type(item).__name__
        if cls == "TableItem":
            table_ref_at_pos[pos] = ref
        else:
            txt = getattr(item, "text", "") or ""
            if txt:
                m = TABLE_CAPTION_RE.match(txt.strip())
                if m:
                    plain_caption_at_pos[pos] = m.group(1)
        pos += 1
    n_items = pos

    # Pair each unbound TableItem with the nearest plain caption (search
    # backward up to 5 items, then forward up to 2 — captions usually precede
    # the table). Won't overwrite a caption-bound ID from the first pass.
    for tpos, tref in table_ref_at_pos.items():
        if tref in ref_to_table_id:
            continue
        chosen: str | None = None
        for d in range(1, 6):
            if (tpos - d) in plain_caption_at_pos:
                chosen = plain_caption_at_pos[tpos - d]
                break
        if chosen is None:
            for d in range(1, 3):
                if (tpos + d) in plain_caption_at_pos:
                    chosen = plain_caption_at_pos[tpos + d]
                    break
        idx = _ref_to_index(tref, "tables")
        ref_to_table_id[tref] = chosen or (f"docling_idx_{idx}" if idx is not None else "?")

    chunker = HierarchicalChunker(always_emit_headings=True)
    chunks = list(chunker.chunk(dl_doc))

    # Group adjacent chunks with the same heading-tuple. New group whenever
    # heading-tuple changes (so heading reuse later in the doc starts a new
    # section, not merges into the earlier one).
    groups: list[list[Any]] = []
    last_key: tuple[str, ...] | None = object()  # type: ignore
    for ch in chunks:
        key = tuple(getattr(ch.meta, "headings", []) or [])
        if key != last_key:
            groups.append([])
            last_key = key
        groups[-1].append(ch)

    sections: list[dict[str, Any]] = []
    section_pos_ranges: list[tuple[int, int]] = []  # filled in below
    for sec_idx, group in enumerate(groups):
        headings = list(getattr(group[0].meta, "headings", []) or [])
        leaf = headings[-1] if headings else ""
        slug = _slugify(leaf)

        all_doc_items: list[Any] = []
        pages: set[int] = set()
        table_ids: list[str] = []
        seen_table_refs: set[str] = set()
        positions: list[int] = []

        for ch in group:
            for item in getattr(ch.meta, "doc_items", []) or []:
                all_doc_items.append(item)
                ref = getattr(item, "self_ref", "") or ""
                if ref in ref_position:
                    positions.append(ref_position[ref])
                for prov in (getattr(item, "prov", None) or []):
                    p = getattr(prov, "page_no", None)
                    if p is not None:
                        pages.add(p)
                cls = type(item).__name__
                if cls == "TableItem" and ref and ref not in seen_table_refs:
                    seen_table_refs.add(ref)
                    tid = ref_to_table_id.get(ref)
                    if tid is not None:
                        table_ids.append(tid)

        full_md = "\n\n".join(
            x for x in (_render_item_full(it, dl_doc) for it in all_doc_items) if x
        ).strip() + "\n"
        stripped_md = "\n\n".join(
            x for x in (_render_item_stripped(it, dl_doc, ref_to_table_id) for it in all_doc_items) if x
        ).strip() + "\n"

        page_range = (min(pages), max(pages)) if pages else (None, None)
        if positions:
            section_pos_ranges.append((min(positions), max(positions)))
        else:
            # Empty section (e.g. heading-only chunk produced no doc_items in
            # the chunker's view) — give it a degenerate range that won't
            # capture pictures.
            section_pos_ranges.append((-1, -1))

        sections.append(
            {
                "index": sec_idx,
                "slug": slug,
                "headings": headings,
                "page_range": list(page_range),
                "table_ids": table_ids,
                "figure_indexes": [],  # filled in below
                "full_md": full_md,
                "stripped_md": stripped_md,
            }
        )

    # Pictures: assign to sections by doc-iteration position. PictureItems are
    # absent from HierarchicalChunker output (they aren't text), so we attribute
    # each picture to the section whose item-position range contains it. If a
    # picture sits past the last section's range, attach to the last section.
    pictures = list(getattr(dl_doc, "pictures", []) or [])
    for pic_idx, pic in enumerate(pictures):
        ref = getattr(pic, "self_ref", "") or ""
        if not ref:
            ref = f"#/pictures/{pic_idx}"
        ppos = ref_position.get(ref)
        if ppos is None:
            continue
        target_sec: int | None = None
        for sec_idx, (lo, hi) in enumerate(section_pos_ranges):
            if lo <= ppos <= hi:
                target_sec = sec_idx
                break
        if target_sec is None:
            # Find latest section whose range starts before this picture.
            best = None
            for sec_idx, (lo, hi) in enumerate(section_pos_ranges):
                if lo != -1 and lo <= ppos:
                    best = sec_idx
            target_sec = best
        if target_sec is not None:
            sections[target_sec]["figure_indexes"].append(pic_idx)

    return {
        "title": _extract_doc_title(dl_doc),
        "sections": sections,
    }


def extract_split(
    pdf_path: Path | str,
    paper_runs_dir: Path | str,
    arbiter_inputs_dir: Path | str,
) -> dict[str, Path]:
    """Convert once, write outputs into the two-tree split.

    paper_runs_dir/ (Sonnet-visible) gets:
      - docling_pictures.json + docling_pictures/picture_*.png
      - docling_sections/<NN>_<slug>.md  (tables → placeholders)
      - docling_sections.json            (index)

    arbiter_inputs_dir/ (NOT in Sonnet's prompt) gets:
      - docling.md
      - docling_tables.json
      - docling_sections_full/<NN>_<slug>.md  (full prose with tables inline)
    """
    pdf_path = Path(pdf_path)
    paper_runs_dir = Path(paper_runs_dir)
    arbiter_inputs_dir = Path(arbiter_inputs_dir)
    paper_runs_dir.mkdir(parents=True, exist_ok=True)
    arbiter_inputs_dir.mkdir(parents=True, exist_ok=True)

    converter = _build_converter()
    t0 = time.time()
    result = converter.convert(str(pdf_path))
    elapsed = time.time() - t0
    doc = result.document

    # Full markdown + tables JSON → arbiter_inputs.
    md_text = doc.export_to_markdown()
    md_path = arbiter_inputs_dir / "docling.md"
    md_path.write_text(md_text)

    tables_payload: list[dict[str, Any]] = [
        _table_to_dict(t, i) for i, t in enumerate(doc.tables)
    ]
    tables_path = arbiter_inputs_dir / "docling_tables.json"
    tables_path.write_text(
        json.dumps(
            {
                "source_pdf": str(pdf_path),
                "num_tables": len(tables_payload),
                "elapsed_seconds": round(elapsed, 2),
                "tables": tables_payload,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    # Pictures (metadata + PNGs) → paper_runs.
    pictures_payload: list[dict[str, Any]] = []
    pictures_dir = paper_runs_dir / "docling_pictures"
    pictures = getattr(doc, "pictures", []) or []
    if pictures:
        pictures_dir.mkdir(parents=True, exist_ok=True)
    for i, picture in enumerate(pictures):
        meta = _picture_to_dict(picture, doc, i)
        try:
            img = picture.get_image(doc)
            if img is not None:
                img.save(pictures_dir / f"picture_{i}.png")
        except Exception:
            pass
        pictures_payload.append(meta)
    pictures_path = paper_runs_dir / "docling_pictures.json"
    pictures_path.write_text(
        json.dumps(
            {
                "source_pdf": str(pdf_path),
                "num_pictures": len(pictures_payload),
                "pictures": pictures_payload,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    # Section split.
    section_data = _build_sections(doc, tables_payload)

    sections_full_dir = arbiter_inputs_dir / "docling_sections_full"
    sections_full_dir.mkdir(parents=True, exist_ok=True)
    sections_stripped_dir = paper_runs_dir / "docling_sections"
    sections_stripped_dir.mkdir(parents=True, exist_ok=True)

    section_index_entries: list[dict[str, Any]] = []
    for sec in section_data["sections"]:
        fname = f"{sec['index']:02d}_{sec['slug']}.md"
        (sections_full_dir / fname).write_text(sec["full_md"])
        (sections_stripped_dir / fname).write_text(sec["stripped_md"])
        section_index_entries.append(
            {
                "index": sec["index"],
                "slug": sec["slug"],
                "file": fname,
                "headings": sec["headings"],
                "page_range": sec["page_range"],
                "table_ids": sec["table_ids"],
                "figure_indexes": sec["figure_indexes"],
            }
        )

    sections_index_path = paper_runs_dir / "docling_sections.json"
    sections_index_path.write_text(
        json.dumps(
            {
                "source_pdf": str(pdf_path),
                "title": section_data["title"],
                "num_sections": len(section_index_entries),
                "sections": section_index_entries,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    print(
        f"Docling split: {pdf_path.name} → "
        f"{len(tables_payload)} tables, {len(pictures_payload)} pictures, "
        f"{len(section_index_entries)} sections, {elapsed:.1f}s"
    )
    return {
        "markdown": md_path,
        "tables": tables_path,
        "pictures": pictures_path,
        "sections_index": sections_index_path,
        "sections_full_dir": sections_full_dir,
        "sections_stripped_dir": sections_stripped_dir,
    }


