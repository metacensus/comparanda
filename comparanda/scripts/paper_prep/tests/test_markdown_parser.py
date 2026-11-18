from __future__ import annotations

import pytest

from paper_prep import arbitrate
from paper_prep.markdown_parser import _split_row, parse_markdown_tables
from paper_prep.schema import Table


@pytest.mark.parametrize(
    "line, cells",
    [
        (r"| Deaths | 12 (4%)\|\| | 3 |", ["Deaths", "12 (4%)||", "3"]),
        (r"| a | b\||", ["a", "b|"]),
    ],
)
def test_split_row_keeps_an_escaped_pipe_in_its_cell(line: str, cells: list[str]) -> None:
    assert _split_row(line) == cells


def test_rendered_table_parses_back_to_the_same_cells() -> None:
    table = Table(
        table_id="1",
        headers=["Outcome", "Group | n", "Group | %"],
        rows=[["Deaths", "12||", "4"], ["Other", "a | b", "c"]],
    )
    md = "**Table 1.** Synthetic caption\n\n" + arbitrate._render_table_markdown(table) + "\n"
    (parsed,) = parse_markdown_tables(md)
    assert parsed.headers == table.headers
    assert parsed.rows == table.rows
