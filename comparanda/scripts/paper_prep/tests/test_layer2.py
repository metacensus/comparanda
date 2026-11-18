from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_prep import arbitrate, layer2

PAPER = "Synthetic_2020_0"


def _table_md(docling_idx: int, id_line: str) -> str:
    return (
        f"<!-- paper-prep-meta\ndocling_idx: {docling_idx}\n{id_line}\npage: 2\n-->\n"
        f"# {PAPER} — Table X (Sonnet first-pass)\n\n"
        "**Table 1.** Synthetic caption\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    )


def _run_root(tmp_path: Path, tables: dict[int, str]) -> Path:
    runs = tmp_path / "run" / "paper_runs" / PAPER
    (runs / "sonnet_tables").mkdir(parents=True)
    (runs / "docling_sections.json").write_text(json.dumps({"title": "T", "sections": []}))
    (runs / "docling_pictures.json").write_text("[]")
    for idx, id_line in tables.items():
        (runs / "sonnet_tables" / f"{idx}.md").write_text(_table_md(idx, id_line))
    inputs = tmp_path / "run" / "arbiter_inputs" / PAPER
    inputs.mkdir(parents=True)
    (inputs / "docling.md").write_text("")
    (inputs / "docling_tables.json").write_text(json.dumps({"tables": []}))
    return tmp_path / "run"


@pytest.mark.parametrize("raw", ["S2", '"S2"', "'S2'", "`S2`", '  "S2"  '])
def test_read_table_meta_strips_quotes(tmp_path: Path, raw: str) -> None:
    md = tmp_path / "0.md"
    md.write_text(_table_md(0, f"canonical_id: {raw}"))
    assert layer2._read_table_meta(md)["canonical_id"] == "S2"


def test_build_records_tables_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_root = _run_root(tmp_path, {0: 'canonical_id: "1"', 1: "canonical_id: 2", 2: "page_only: 3"})
    seen: list[str] = []

    def fake_merge(*, table_id: str, arbited_out: Path, **_: object) -> dict:
        seen.append(table_id)
        if table_id == "2":
            raise RuntimeError(f"No Table {table_id} in either sonnet_combined.md or docling.md")
        arbited_out.write_text(f"Table {table_id}. Synthetic caption\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n")
        return {}

    monkeypatch.setattr(arbitrate, "merge_and_arbitrate", fake_merge)
    manifest_path = layer2.build(PAPER, run_root, tmp_path / "bundles", tmp_path / "paper.pdf")

    assert seen == ["1", "2"]
    manifest = json.loads(manifest_path.read_text())
    assert [t["id"] for t in manifest["tables"]] == ["1"]
    assert [(f["id"], f["docling_idx"]) for f in layer2.failed_tables(manifest_path)] == [
        (None, "2"),
        ("2", "1"),
    ]


def test_failed_tables_is_empty_for_manifest_without_the_key(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tables": []}))
    assert layer2.failed_tables(manifest) == []
