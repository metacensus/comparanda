from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

from paper_prep import __main__ as cli
from paper_prep import batch, layer1, layer2


def _fake_layers(monkeypatch: pytest.MonkeyPatch, failures: list[dict]) -> None:
    monkeypatch.setattr(layer1, "run", lambda **_: {"first_pass_cost_usd": 0.5, "errors": []})

    def fake_build(*, paper_id: str, output_root: Path, **_: object) -> Path:
        manifest = Path(output_root) / paper_id / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"tables": [], "table_failures": failures}))
        return manifest

    monkeypatch.setattr(layer2, "build", fake_build)


def _process(tmp_path: Path, state: batch.BatchState) -> list[str]:
    summary = tmp_path / "_run_summary.csv"
    (tmp_path / "logs").mkdir()
    batch._process_paper(
        paper={"pmid": 1, "survey_key": "Synthetic_2020_1"},
        pdf_index={1: tmp_path / "paper.pdf"},
        run_root=tmp_path,
        bundle_out=tmp_path / "bundles",
        log_dir=tmp_path / "logs",
        summary_csv=summary,
        state=state,
        stop_sentinel=tmp_path / "STOP",
        total_budget_usd=10.0,
        per_table_budget_usd=1.0,
        table_batch_size=1,
        do_arbitrate=False,
    )
    rows = list(csv.reader(summary.open()))
    assert len(rows) == 1 and len(rows[0]) == len(batch.CSV_HEADER)
    return rows[0]


def test_batch_reports_tables_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_layers(monkeypatch, [{"id": "1", "docling_idx": "0", "error": "RuntimeError: boom"}])
    state = batch.BatchState()
    row = _process(tmp_path, state)
    assert row[2] == "tables_failed"
    assert row[5] == "True"
    assert row[6].startswith("1 table(s) left out of the bundle: table 1 (docling_idx 0)")
    assert state.failed_tables == 1


def test_batch_reports_ok_when_every_table_lands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_layers(monkeypatch, [])
    state = batch.BatchState()
    assert _process(tmp_path, state)[2] == "ok"
    assert state.failed_tables == 0


@pytest.mark.parametrize("failures, code", [([], 0), ([{"id": "1", "docling_idx": "0", "error": "x"}], 1)])
def test_cli_exit_code_reflects_failed_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: list[dict], code: int
) -> None:
    _fake_layers(monkeypatch, failures)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"")
    monkeypatch.setattr(sys, "argv", [
        "paper_prep", "Synthetic_2020_1", "--pdf", str(pdf),
        "--output-root", str(tmp_path / "bundles"), "--run-root", str(tmp_path / "run"),
    ])
    assert cli.main() == code
