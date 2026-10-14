"""Shared helpers for the bundle builder.

Pruned from an earlier manifest builder. Only the helpers still consumed
by `paper_prep.layer2` remain; the obsolete
whole-paper-rewrite `build()` and its Section/Docling-pairing infrastructure
are gone.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from paper_prep.markdown_parser import parse_extractor_markdown


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
    title: str | None = None
    for line in arbited_md.splitlines():
        m = _TABLE_CAPTION_RE.match(line.strip())
        if m and m.group(1).strip().lower() == str(table_id).lower():
            title = (m.group(2) or "").strip().strip("*").strip() or None
            break
        if title is None and m:
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
    t = out.tables[0]
    return list(t.headers), [list(r) for r in t.rows], title


def _is_decorative(
    caption: str, page: int | None, bbox: list[float] | None
) -> bool:
    """Heuristic: drop pictures that look like journal furniture (page-corner
    glyphs, issue marks, header ornaments) rather than real figures.

    Captioned pictures are always kept. Uncaptioned pictures only get the
    size-based filter applied when they are on page 1 (where banner glyphs,
    publisher logos, and journal-issue marks live). On page 2+, an
    uncaptioned figure is more likely to be a real figure whose caption
    Docling's matcher missed than to be furniture.
    """
    if caption.strip():
        return False
    if page is not None and page >= 2:
        return False
    if bbox is None or len(bbox) != 4:
        return False
    width = abs(bbox[2] - bbox[0])
    height = abs(bbox[3] - bbox[1])
    area = width * height
    page_area = 612.0 * 792.0  # standard letter-size page in pt²
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
