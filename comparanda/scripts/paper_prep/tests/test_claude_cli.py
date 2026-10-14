"""The `claude -p` calls must be permission-scoped, never permission-skipping."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paper_prep import arbitrate
from paper_prep.claude_cli import build_claude_argv
from paper_prep.extractors import claude_extractor


def _flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _assert_scoped(argv: list[str], expected_write: str) -> None:
    assert "--dangerously-skip-permissions" not in argv
    assert "bypassPermissions" not in argv
    assert argv[:2] == ["claude", "-p"]
    assert "--restricted" in argv
    assert "--strict-mcp-config" in argv
    assert _flag_value(argv, "--tools") == "Read,Write,Edit"
    assert _flag_value(argv, "--permission-mode") == "dontAsk"
    assert _flag_value(argv, "--allowedTools") == f"Read,Edit(./{expected_write})"
    assert not any(a.startswith("Bash") for a in argv)


class _FakeRun:
    """Stands in for subprocess.run; records the call and writes the output."""

    def __init__(self, write_to: Path | None = None):
        self.write_to = write_to
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.write_to is not None:
            self.write_to.parent.mkdir(parents=True, exist_ok=True)
            self.write_to.write_text("x")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.01}), "")


def test_build_argv_scopes_reads_and_single_write(tmp_path: Path) -> None:
    cwd = tmp_path / "run"
    (cwd / "sub").mkdir(parents=True)
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    argv = build_claude_argv(
        "prompt",
        cwd=cwd,
        output_path=cwd / "sub" / "out.md",
        model="sonnet",
        max_budget_usd=0.5,
        read_dirs=[pdfs, cwd, cwd / "sub", pdfs],
    )
    _assert_scoped(argv, "sub/out.md")
    assert _flag_value(argv, "--max-budget-usd") == "0.50"
    # Only dirs outside cwd are added, once each.
    assert argv[argv.index("--add-dir") + 1 :] == [str(pdfs.resolve())]


def test_build_argv_rejects_output_outside_cwd(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_claude_argv(
            "p", cwd=tmp_path / "a", output_path=tmp_path / "b" / "out.md",
            model="sonnet", max_budget_usd=0.1,
        )


def test_extract_table_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "paper_runs" / "p1"
    inputs = tmp_path / "arbiter_inputs" / "p1"
    pdfs = tmp_path / "pdfs"
    for d in (runs, inputs, pdfs):
        d.mkdir(parents=True)
    pdf = pdfs / "Smith 2024 (J Example) [12345678].pdf"
    pdf.write_bytes(b"%PDF-1.4")
    (inputs / "docling_tables.json").write_text(
        json.dumps([{"index": 3, "page": 2, "num_rows": 4, "num_cols": 2}])
    )
    fake = _FakeRun(write_to=runs / "sonnet_tables" / "3.md")
    monkeypatch.setattr(claude_extractor.subprocess, "run", fake)

    res = claude_extractor.extract_table(pdf, runs, inputs, "p1", 3, max_budget_usd=1.5)

    assert res["ok"] and res["cost_usd"] == 0.01
    (argv, kwargs), = fake.calls
    _assert_scoped(argv, "sonnet_tables/3.md")
    assert kwargs["cwd"] == str(runs.resolve())
    assert kwargs["stdin"] is subprocess.DEVNULL
    # The PDF dir is readable; arbiter_inputs (hidden from the first pass) is not.
    added = argv[argv.index("--add-dir") + 1 :]
    assert added == [str(pdfs.resolve())]
    assert str(inputs.resolve()) not in argv


def test_run_arbiter_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "paper_runs" / "p1"
    inputs = tmp_path / "arbiter_inputs" / "p1"
    pdfs = tmp_path / "pdfs"
    guide_dir = runs / "merged_table_1_arbitration"
    for d in (runs, inputs, pdfs, guide_dir):
        d.mkdir(parents=True)
    arbited = runs / "arbited_table_1.md"
    fake = _FakeRun()
    monkeypatch.setattr(arbitrate.subprocess, "run", fake)

    rc, _, _ = arbitrate._run_arbiter(
        "prompt", 0.75, 600,
        arbited_path=arbited,
        read_dirs=[runs, inputs, pdfs, guide_dir],
    )

    assert rc == 0
    (argv, kwargs), = fake.calls
    _assert_scoped(argv, "arbited_table_1.md")
    assert kwargs["cwd"] == str(runs.resolve())
    assert argv[argv.index("--add-dir") + 1 :] == [str(inputs.resolve()), str(pdfs.resolve())]


def test_arbiter_prompt_needs_no_shell(tmp_path: Path) -> None:
    prompt = arbitrate._build_arbiter_prompt(
        "p1", "1", tmp_path / "a.md", tmp_path / "b.md", tmp_path / "x.pdf",
        tmp_path / "guide.md", tmp_path / "out.md",
    )
    assert "uv run" not in prompt


def test_arbited_parses(tmp_path: Path) -> None:
    good = tmp_path / "good.md"
    good.write_text(
        "# p1 — Table 2 (Stage-2 arbited extraction)\n\nArbitrated.\n\n"
        "**Table 2.** Example caption\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    )
    assert arbitrate._arbited_parses(good, "p1", "2")
    assert not arbitrate._arbited_parses(good, "p1", "3")
    assert not arbitrate._arbited_parses(tmp_path / "missing.md", "p1", "2")
