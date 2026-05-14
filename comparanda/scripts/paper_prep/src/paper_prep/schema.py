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


