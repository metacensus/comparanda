"""Common schema for extractor outputs and consensus comparison.

All extractors normalize their output to this structure so that cell-level
comparison across extractors becomes possible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Table(BaseModel):
    """A single extracted table.

    Headers are flattened to a single row of strings. Multi-level headers should
    be combined with " | " or repeated — extractors document their choice in
    `header_flattening_note`.
    """

    table_id: Optional[str] = Field(
        None,
        description=(
            "Table identifier as reported in paper (e.g., '3' for 'Table 3', "
            "'A1A' for 'Table A1A'). None if unlabeled. Always a string so that "
            "alphanumeric IDs (appendices, supplements) work uniformly with "
            "numeric ones."
        ),
    )
    caption: Optional[str] = Field(None, description="Caption text following 'Table N.'")
    page: Optional[int] = Field(None, description="Page in the PDF where the table appears")

    headers: list[str] = Field(
        default_factory=list,
        description="Flattened column headers, one per column. For multi-level headers, combine levels with ' | '.",
    )
    rows: list[list[str]] = Field(
        default_factory=list,
        description="Rows of cell values. Each row has len(headers) cells; missing cells are empty string.",
    )
    footnotes: list[str] = Field(
        default_factory=list,
        description="Footnote text associated with this table (abbreviation keys, significance markers, etc.)",
    )

    header_flattening_note: Optional[str] = Field(
        None,
        description="Notes about how multi-level headers were flattened, merged cell handling, etc.",
    )
    extraction_confidence: Optional[str] = Field(
        None,
        description="Extractor's self-reported confidence: 'high', 'medium', 'low'",
    )


class ExtractorOutput(BaseModel):
    """Normalized output from one extractor for one paper."""

    paper_id: str
    extractor: str = Field(description="e.g., 'pymupdf4llm', 'docling', 'glm_ocr', 'claude_native'")
    extractor_version: Optional[str] = None
    extraction_timestamp: datetime = Field(default_factory=datetime.utcnow)

    tables: list[Table] = Field(default_factory=list)
    section_headings: list[str] = Field(
        default_factory=list,
        description="Ordered list of section headings detected (for structural comparison)",
    )
    full_markdown_path: Optional[str] = Field(
        None, description="Relative path to the full markdown output for this extractor"
    )
    notes: Optional[str] = None


class CellDisagreement(BaseModel):
    """A single cell where extractors disagree."""

    table_id: Optional[str]
    row_index: int
    col_index: int
    header: Optional[str]
    row_label: Optional[str]  # First cell of the row, typically the entity name
    values_by_extractor: dict[str, str]  # extractor_name -> cell_value


class TableComparison(BaseModel):
    """Comparison result for one table across multiple extractors."""

    table_id: Optional[str]
    caption: Optional[str]
    extractors_with_table: list[str]
    extractors_missing_table: list[str]

    agreement_cells: int = Field(description="Cells where all extractors with this table agree")
    total_cells: int
    disagreements: list[CellDisagreement] = Field(default_factory=list)

    @property
    def agreement_rate(self) -> float:
        return self.agreement_cells / self.total_cells if self.total_cells else 0.0


def normalize_cell(value: str) -> str:
    """Normalize a cell value for comparison across extractors.

    Handles: whitespace, unicode dashes, decimal separators, percentage signs,
    degree signs, common scientific notation variants.
    """
    import re
    import unicodedata

    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    # Unicode NFKC normalization (e.g., combining chars, full-width digits)
    s = unicodedata.normalize("NFKC", s)

    # Normalize various dash/minus characters to ASCII hyphen-minus
    for ch in ("‐", "‑", "‒", "–", "—", "−", "­"):
        s = s.replace(ch, "-")

    # Normalize various space characters
    s = re.sub(r"\s+", " ", s)

    # Strip markdown emphasis (**bold**, _italic_) — extractors render these differently
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", s)

    # Common OCR/encoding confusions in PDFs
    # "¼" often represents "=" (ligature misread), "–" often "-"
    s = s.replace("¼", "=")

    return s.strip()
