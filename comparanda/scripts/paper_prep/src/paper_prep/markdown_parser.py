"""Parse tables from extractor markdown into structured Table objects.

Handles both GitHub-style pipe tables (PyMuPDF4LLM, claude_native, mistral_ocr,
mineru's summary tables, docling) and inline HTML `<table>` blocks (chandra,
lightonocr, mineru's supplemental tables). All tables feed into the same
common schema for cross-extractor comparison.
"""

from __future__ import annotations

import html as html_module
import re
from html.parser import HTMLParser
from pathlib import Path

from paper_prep.schema import ExtractorOutput, Table


# Match "**Table 3.**" or "Table 3." or "Table 3:" or "## Table A1A:" as a
# caption anchor. Table IDs can be numeric ("3"), suffixed ("1B"), prefixed
# ("S2"), both ("A1A"), or Roman numerals ("I", "IV", "XII"). We keep the ID
# as a string. The leading `#{0,4}` handles extractors (e.g. claude_native)
# that render the caption as a heading.
TABLE_CAPTION_RE = re.compile(
    r"^\s*#{0,4}\s*\*{0,2}\s*Table\s+([A-Za-z]*\d+[A-Za-z]*|[IVX]+)\*{0,2}[\.\:]?\s*(.*)$",
    re.IGNORECASE,
)

# Same anchor but allowed to appear mid-line (chandra glues "**Table 2.**" to
# the tail of the previous paragraph). We require bold markers or a leading
# word boundary + colon/period to avoid false positives inside prose such as
# "shown in Table 2.".
_EMBEDDED_CAPTION_RE = re.compile(
    r"(?:\*\*|__)\s*Table\s+([A-Za-z]*\d+[A-Za-z]*|[IVX]+)\s*[\.\:]\s*(.*?)(?:\*\*|__)",
    re.IGNORECASE,
)


def _is_md_table_row(line: str) -> bool:
    """A line is a markdown table row iff it starts and ends with | (after strip)."""
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _is_md_table_separator(line: str) -> bool:
    """Markdown table separator row: | --- | --- |."""
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return False
    cells = [c.strip() for c in s.strip("|").split("|")]
    return all(re.fullmatch(r":?-+:?", c) for c in cells if c)


def _split_row(line: str) -> list[str]:
    """Split a markdown table row line into cells. Handles `<br>` -> newline."""
    s = line.strip().strip("|")
    cells = [c.strip() for c in s.split("|")]
    # Many extractors use <br> for line breaks within cells
    return [re.sub(r"\s*<br\s*/?>\s*", " | ", c) for c in cells]


def parse_markdown_tables(md_text: str) -> list[Table]:
    """Extract all markdown tables from a markdown string.

    A table is a contiguous block of pipe-delimited lines. Captions are detected
    by looking for 'Table N.' on lines near the table (within 3 lines before or
    after the block).
    """
    lines = md_text.splitlines()
    tables: list[Table] = []

    i = 0
    while i < len(lines):
        if not _is_md_table_row(lines[i]):
            i += 1
            continue

        # Start of a table block
        block_start = i
        block_lines: list[str] = []
        while i < len(lines) and (_is_md_table_row(lines[i]) or lines[i].strip() == ""):
            if _is_md_table_row(lines[i]):
                block_lines.append(lines[i])
            elif block_lines and i + 1 < len(lines) and not _is_md_table_row(lines[i + 1]):
                break
            i += 1

        if len(block_lines) < 2:
            continue

        # Find header separator — the first separator row
        sep_idx = None
        for j, bl in enumerate(block_lines):
            if _is_md_table_separator(bl):
                sep_idx = j
                break

        if sep_idx is None:
            # No separator — treat first row as header
            header_rows = [_split_row(block_lines[0])]
            raw_data_lines = block_lines[1:]
        else:
            header_rows = [_split_row(bl) for bl in block_lines[:sep_idx]]
            raw_data_lines = block_lines[sep_idx + 1 :]

        # Drop cross-page-break re-emissions of separators and re-emitted
        # header rows (e.g. docling restates `**Table A1A: Overall**` banner
        # + column header + `|---|---|` every page). Without this filter
        # those rows end up as "data rows" of `---` or banner text.
        header_text_set = {
            "||".join(_split_row(bl)) for bl in block_lines[: sep_idx if sep_idx is not None else 0]
        }
        data_rows: list[list[str]] = []
        for bl in raw_data_lines:
            if _is_md_table_separator(bl):
                continue
            split = _split_row(bl)
            # Skip re-emitted header rows verbatim
            if "||".join(split) in header_text_set:
                continue
            # Skip re-emitted banner rows (all cells same, matching the
            # table-caption banner pattern). Detected lazily per-row so later
            # page-break banners don't survive.
            banner_probe, _ = _probe_caption_row(split)
            if banner_probe is not None:
                continue
            data_rows.append(split)

        # Probe the first header row for a caption banner.
        banner_id: str | None = None
        banner_caption: str | None = None
        if header_rows:
            banner_id, banner_caption = _probe_caption_row(header_rows[0])
            if banner_id is not None:
                header_rows = header_rows[1:]
        if banner_id is None and data_rows:
            banner_id, banner_caption = _probe_caption_row(data_rows[0])
            if banner_id is not None:
                data_rows = data_rows[1:]

        # Flatten multi-row headers by joining with " | "
        if not header_rows and not data_rows:
            continue
        n_cols = max(len(r) for r in header_rows + data_rows)

        def pad(row: list[str]) -> list[str]:
            return row + [""] * (n_cols - len(row))

        header_rows = [pad(r) for r in header_rows]
        data_rows = [pad(r) for r in data_rows]

        flat_headers = []
        for col in range(n_cols):
            parts = [r[col] for r in header_rows if r[col]]
            flat_headers.append(" | ".join(parts) if parts else f"col_{col}")

        # Look for a caption within 3 lines before block_start or after block end
        block_end = block_start + len(block_lines)
        caption = banner_caption
        table_id: str | None = banner_id
        if table_id is not None:
            # Banner already identified the table; skip the proximity scan.
            offset_ranges: tuple[range, ...] = ()
        else:
            offset_ranges = (
                range(block_start - 1, max(block_start - 5, -1), -1),
                range(block_end, min(block_end + 5, len(lines))),
            )
        for offset_range in offset_ranges:
            for k in offset_range:
                if k < 0 or k >= len(lines):
                    continue
                m = TABLE_CAPTION_RE.match(lines[k].strip())
                if m:
                    # Preserve casing of alpha parts (e.g. "A1A"), but strip
                    # any enclosing whitespace.
                    table_id = m.group(1).strip()
                    caption = m.group(2).strip() or None
                    break
            if table_id is not None:
                break

        tables.append(
            Table(
                table_id=table_id,
                caption=caption,
                headers=flat_headers,
                rows=data_rows,
                footnotes=[],
                header_flattening_note=(
                    f"Multi-row headers ({len(header_rows)} rows) flattened with ' | '"
                    if len(header_rows) > 1
                    else None
                ),
            )
        )

    return tables


# ────────────────────────────────────────────────────────────────────────────
# HTML table parsing
# ────────────────────────────────────────────────────────────────────────────


_TABLE_OPEN_RE = re.compile(r"<table\b[^>]*>", re.IGNORECASE)
_TABLE_CLOSE_RE = re.compile(r"</table>", re.IGNORECASE)


def _find_html_table_blocks(text: str) -> list[tuple[int, int, str]]:
    """Yield (start, end, block_text) for each HTML <table> block.

    Tolerates missing </table> closing tags (lightonocr emits streams of
    unclosed tables) by treating the next `<table>` as an implicit close.
    """
    blocks: list[tuple[int, int, str]] = []
    opens = [m.start() for m in _TABLE_OPEN_RE.finditer(text)]
    closes = [m.end() for m in _TABLE_CLOSE_RE.finditer(text)]
    if not opens:
        return blocks
    close_idx = 0
    for i, open_pos in enumerate(opens):
        next_open = opens[i + 1] if i + 1 < len(opens) else len(text)
        # Find the first close after this open
        end = None
        while close_idx < len(closes) and closes[close_idx] <= open_pos:
            close_idx += 1
        if close_idx < len(closes) and closes[close_idx] <= next_open:
            end = closes[close_idx]
        else:
            # Implicit close: either nothing closes, or the next close is
            # after a later <table> open (so it doesn't belong to this block).
            end = next_open
        block = text[open_pos:end]
        # Append a synthetic close so the HTML parser is happy
        if not _TABLE_CLOSE_RE.search(block):
            block = block + "</table>"
        blocks.append((open_pos, end, block))
    return blocks


class _HtmlTableParser(HTMLParser):
    """Collect rows/cells from a single <table> block, honoring rowspan/colspan.

    Produces a dense 2-D grid where spanned cells are replicated into the
    positions they cover. Headers are whichever rows appeared inside <thead>
    (or before the first tbody <tr>); all subsequent rows are data.
    """

    def __init__(self) -> None:
        super().__init__()
        # Grid is a dict keyed by (row, col) so sparse filling works; we
        # densify at the end.
        self._grid: dict[tuple[int, int], str] = {}
        self._header_rows: set[int] = set()
        self._in_thead = False
        self._row = -1
        self._col = -1
        self._cur_text: list[str] = []
        self._in_cell = False
        self._cur_rowspan = 1
        self._cur_colspan = 1
        # Pending rowspans so subsequent rows skip occupied columns
        self._rowspan_carry: dict[int, int] = {}

    # HTMLParser overrides ------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "thead":
            self._in_thead = True
        elif tag == "tr":
            self._row += 1
            self._col = -1
            if self._in_thead:
                self._header_rows.add(self._row)
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cur_text = []
            # advance column, skipping any positions still occupied by rowspan
            self._col += 1
            while self._rowspan_carry.get(self._col, 0) > 0:
                self._col += 1
            self._cur_rowspan = 1
            self._cur_colspan = 1
            for k, v in attrs:
                if v is None:
                    continue
                if k.lower() == "rowspan":
                    try:
                        self._cur_rowspan = max(1, int(v))
                    except ValueError:
                        pass
                elif k.lower() == "colspan":
                    try:
                        self._cur_colspan = max(1, int(v))
                    except ValueError:
                        pass
            # th inside tbody still counts as a header cell visually; we keep
            # the tag mapping simple (header only if inside thead).
            if tag == "th" and not self._in_thead and self._row == 0:
                self._header_rows.add(self._row)
        elif tag == "br":
            if self._in_cell:
                self._cur_text.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "thead":
            self._in_thead = False
        elif tag in ("td", "th") and self._in_cell:
            text = "".join(self._cur_text).strip()
            text = re.sub(r"\s+", " ", text)
            r = self._row
            c = self._col
            for dr in range(self._cur_rowspan):
                for dc in range(self._cur_colspan):
                    self._grid[(r + dr, c + dc)] = text
            # Register rowspan carry for future rows (dr > 0) so they skip these columns
            for dr in range(1, self._cur_rowspan):
                for dc in range(self._cur_colspan):
                    self._rowspan_carry[c + dc] = max(
                        self._rowspan_carry.get(c + dc, 0), dr
                    )
            # Extend current col cursor across the colspan
            self._col += self._cur_colspan - 1
            self._in_cell = False
        elif tag == "tr":
            # Decrement rowspan carries at end of row
            for k in list(self._rowspan_carry.keys()):
                self._rowspan_carry[k] = max(0, self._rowspan_carry[k] - 1)
                if self._rowspan_carry[k] == 0:
                    del self._rowspan_carry[k]

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cur_text.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._in_cell:
            self._cur_text.append(html_module.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if self._in_cell:
            self._cur_text.append(html_module.unescape(f"&#{name};"))

    # --------------------------------------------------------------------
    def densify(self) -> tuple[list[list[str]], list[list[str]]]:
        """Return (header_rows, data_rows) as lists of equal-length lists."""
        if not self._grid:
            return [], []
        max_row = max(r for r, _ in self._grid)
        max_col = max(c for _, c in self._grid)
        grid: list[list[str]] = [
            [self._grid.get((r, c), "") for c in range(max_col + 1)]
            for r in range(max_row + 1)
        ]
        header_rows = [grid[r] for r in sorted(self._header_rows) if r < len(grid)]
        data_rows = [grid[r] for r in range(len(grid)) if r not in self._header_rows]
        return header_rows, data_rows


def _parse_html_table_block(html_block: str) -> tuple[list[list[str]], list[list[str]]]:
    p = _HtmlTableParser()
    p.feed(html_block)
    p.close()
    return p.densify()


def _probe_caption_row(row: list[str]) -> tuple[str | None, str | None]:
    """Return (table_id, caption) if ``row`` looks like a single-value caption
    banner (e.g. a colspan header spanning the whole table saying
    ``**Table A1A: Overall**``). Otherwise returns ``(None, None)``.

    Used by both HTML and markdown pipe-table parsers. pymupdf4llm in
    particular flattens colspan banners into a row of cells that all contain
    the same caption text, so we normalise by collapsing duplicates.
    """
    # Strip surrounding bold markers so the regex's trailing `.*` doesn't
    # absorb the closing `**` into the caption text.
    def _unbold(s: str) -> str:
        s = s.strip()
        if s.startswith("**") and s.endswith("**"):
            s = s[2:-2].strip()
        if s.startswith("__") and s.endswith("__"):
            s = s[2:-2].strip()
        return s

    uniq = {_unbold(c) for c in row if c.strip()}
    if len(uniq) == 1:
        m = TABLE_CAPTION_RE.match(next(iter(uniq)))
        if m:
            return m.group(1).strip(), (m.group(2).strip() or None)
    non_empty = [_unbold(c) for c in row if c.strip()]
    if len(non_empty) == 1:
        m = TABLE_CAPTION_RE.match(non_empty[0])
        if m:
            return m.group(1).strip(), (m.group(2).strip() or None)
    return None, None


def _extract_caption_from_first_cell(
    header_rows: list[list[str]],
    data_rows: list[list[str]],
) -> tuple[str | None, str | None, bool, bool]:
    """Detect captions embedded in the first row (e.g. a single-cell colspan
    banner saying 'Table A1A: Overall').

    Returns (table_id, caption, consumed_from_headers, consumed_from_data).
    mineru sometimes emits such banners in the first <tbody> row rather than
    <thead>, so we probe both.
    """
    if header_rows:
        tid, cap = _probe_caption_row(header_rows[0])
        if tid is not None:
            return tid, cap, True, False
    if data_rows:
        tid, cap = _probe_caption_row(data_rows[0])
        if tid is not None:
            return tid, cap, False, True
    return None, None, False, False


def parse_html_tables(md_text: str) -> list[Table]:
    """Extract inline <table>...</table> blocks as Table objects."""
    tables: list[Table] = []
    lines = md_text.splitlines()
    # Build a mapping of char offset -> line index for caption-nearby lookup
    offsets: list[int] = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line) + 1  # +1 for newline

    def _line_index(char_offset: int) -> int:
        # Binary search the last offset <= char_offset
        lo, hi = 0, len(offsets) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if offsets[mid] <= char_offset:
                lo = mid
            else:
                hi = mid - 1
        return lo

    for start, end, block in _find_html_table_blocks(md_text):
        header_rows, data_rows = _parse_html_table_block(block)
        if not data_rows and not header_rows:
            continue

        # Drop rows whose cells are all empty (common artifact when a paper
        # uses blank spacer rows or the HTML parser sees stray `<tr></tr>`).
        data_rows = [r for r in data_rows if any(c.strip() for c in r)]

        table_id, caption, consumed_header, consumed_data = (
            _extract_caption_from_first_cell(header_rows, data_rows)
        )
        if consumed_header and header_rows:
            header_rows = header_rows[1:]
        if consumed_data and data_rows:
            # The banner was in tbody; treat the *next* row as the header if
            # we have no thead. Otherwise just drop the banner row.
            data_rows = data_rows[1:]
            if not header_rows and data_rows:
                header_rows = [data_rows[0]]
                data_rows = data_rows[1:]

        # Fall back to searching nearby lines for "Table N." / "Table A1A:".
        # We also allow the caption to appear embedded in a line (chandra glues
        # "**Table 2.** ..." to the end of the preceding paragraph) by
        # searching for the pattern anywhere in each line.
        if table_id is None:
            block_line = _line_index(start)
            block_end_line = _line_index(end)
            for offset_range in (
                range(block_line - 1, max(block_line - 8, -1), -1),
                range(block_end_line + 1, min(block_end_line + 8, len(lines))),
            ):
                for k in offset_range:
                    if k < 0 or k >= len(lines):
                        continue
                    line_stripped = lines[k].strip()
                    if not line_stripped:
                        continue
                    cm = TABLE_CAPTION_RE.match(line_stripped)
                    if cm is None:
                        cm = _EMBEDDED_CAPTION_RE.search(line_stripped)
                    if cm:
                        table_id = cm.group(1).strip()
                        caption = cm.group(2).strip() or None
                        # Trim trailing content after the caption ends (e.g.
                        # the next `<table>` tag) — caption is only the
                        # leading prose on the same "line".
                        caption = re.split(r"<table|<br", caption)[0].strip() or None
                        break
                if table_id is not None:
                    break

        # Flatten multi-row headers to a single list
        if header_rows:
            n_cols = max(len(r) for r in header_rows + data_rows)
        elif data_rows:
            n_cols = max(len(r) for r in data_rows)
        else:
            n_cols = 0

        def pad(row: list[str]) -> list[str]:
            return row + [""] * (n_cols - len(row))

        header_rows = [pad(r) for r in header_rows]
        data_rows = [pad(r) for r in data_rows]

        flat_headers: list[str] = []
        for col in range(n_cols):
            parts = [r[col] for r in header_rows if r[col]]
            # de-duplicate consecutive repeats (common after colspan expansion)
            dedup: list[str] = []
            for p in parts:
                if not dedup or dedup[-1] != p:
                    dedup.append(p)
            flat_headers.append(" | ".join(dedup) if dedup else f"col_{col}")

        tables.append(
            Table(
                table_id=table_id,
                caption=caption,
                headers=flat_headers,
                rows=data_rows,
                footnotes=[],
                header_flattening_note=(
                    f"HTML table; {len(header_rows)} header rows flattened"
                    if len(header_rows) > 1
                    else "HTML table"
                ),
            )
        )

    return tables


def parse_extractor_markdown(
    md_path: Path | str,
    paper_id: str,
    extractor: str,
    extractor_version: str | None = None,
) -> ExtractorOutput:
    """Parse a markdown file from an extractor into the common schema.

    Understands both pipe-style markdown tables and inline `<table>` HTML
    blocks; extractors that mix the two (e.g. mineru) will yield both.
    """
    md_path = Path(md_path)
    md_text = md_path.read_text()

    tables = parse_markdown_tables(md_text) + parse_html_tables(md_text)

    # Extract section headings (lines starting with # or ## or ###)
    section_headings = []
    for line in md_text.splitlines():
        m = re.match(r"^(#{1,4})\s+\*{0,2}(.+?)\*{0,2}\s*$", line)
        if m:
            section_headings.append(m.group(2).strip())

    return ExtractorOutput(
        paper_id=paper_id,
        extractor=extractor,
        extractor_version=extractor_version,
        tables=tables,
        section_headings=section_headings,
        full_markdown_path=str(md_path),
    )
