from __future__ import annotations

from pathlib import Path

import pytest

from paper_prep import layer2

PAPER = "Synthetic_2020_0"


def _table_md(docling_idx: int, id_line: str) -> str:
    return (
        f"<!-- paper-prep-meta\ndocling_idx: {docling_idx}\n{id_line}\npage: 2\n-->\n"
        f"# {PAPER} — Table X (Sonnet first-pass)\n\n"
        "**Table 1.** Synthetic caption\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    )


@pytest.mark.parametrize("raw", ["S2", '"S2"', "'S2'", "`S2`", '  "S2"  '])
def test_read_table_meta_strips_quotes(tmp_path: Path, raw: str) -> None:
    md = tmp_path / "0.md"
    md.write_text(_table_md(0, f"canonical_id: {raw}"))
    assert layer2._read_table_meta(md)["canonical_id"] == "S2"
