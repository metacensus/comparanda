"""Tests for harness.git_wrapper.commit — path classification + branch routing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from harness.git_wrapper.commit import (
    Classification,
    classify_paths,
    harness_commit,
)


# ────────────────────────────────────────────────────────────────────
# Pure classification (no git needed)
# ────────────────────────────────────────────────────────────────────


def test_classify_pure_methodology():
    c = classify_paths([
        "comparanda/.claude/agents/extractor.md",
        "comparanda/scripts/observation_log.py",
        "harness/src/harness/eval/aggregate.py",
        "harness/CLAUDE.md",
        "docs/specs/anything.md",
    ])
    assert c.is_pure_methodology
    assert not c.is_pure_domain
    assert not c.is_mixed
    assert not c.unknown


def test_classify_pure_domain():
    c = classify_paths([
        "comparanda/projects/example_domain/schema/v007.yaml",
        "comparanda/projects/example_domain/extractions/foo.jsonl",
        "harness/iterations/iter_10/aggregate_eval.json",
        "harness/iterations/trajectory.json",
    ])
    assert c.is_pure_domain
    assert not c.is_pure_methodology


def test_classify_mixed_rejected_by_caller():
    c = classify_paths([
        "comparanda/.claude/agents/extractor.md",      # methodology
        "comparanda/projects/example_domain/extractions/foo.jsonl",  # domain
    ])
    assert c.is_mixed
    assert not c.is_pure_methodology
    assert not c.is_pure_domain


def test_classify_unknown_path():
    c = classify_paths(["random/wat/file.txt"])
    assert c.unknown == ("random/wat/file.txt",)
    assert not c.methodology
    assert not c.domain


# ────────────────────────────────────────────────────────────────────
# Integration: full commit flow against a temp git repo
# ────────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    base_env = os.environ.copy()
    base_env.setdefault("GIT_AUTHOR_NAME", "test")
    base_env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    base_env.setdefault("GIT_COMMITTER_NAME", "test")
    base_env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=base_env,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temp git repo with a methodology + main branch and a baseline commit."""
    _git("init", "-b", "main", cwd=tmp_path)
    # Baseline commit on main with placeholder system code.
    (tmp_path / "comparanda").mkdir()
    (tmp_path / "comparanda" / ".claude").mkdir()
    (tmp_path / "comparanda" / ".claude" / "agents").mkdir()
    (tmp_path / "comparanda" / ".claude" / "agents" / "extractor.md").write_text("baseline\n")
    (tmp_path / "comparanda" / "projects").mkdir()
    (tmp_path / "comparanda" / "projects" / "example_domain").mkdir()
    (tmp_path / "comparanda" / "projects" / "example_domain" / "extractions").mkdir()
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "iterations").mkdir()
    (tmp_path / "README.md").write_text("hi\n")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "baseline", cwd=tmp_path)
    _git("branch", "methodology", cwd=tmp_path)

    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_pure_methodology_from_main_routes_to_methodology_and_merges_forward(repo: Path):
    # Stage a methodology change while on main.
    (repo / "comparanda" / ".claude" / "agents" / "extractor.md").write_text("improved\n")
    _git("add", "comparanda/.claude/agents/extractor.md", cwd=repo)

    result = harness_commit("improve extractor")

    # methodology branch should have the new commit; main fast-forwarded.
    methodology_head = _git("rev-parse", "methodology", cwd=repo).stdout.strip()
    main_head = _git("rev-parse", "main", cwd=repo).stdout.strip()
    assert methodology_head == main_head, "main should be at methodology tip (fast-forwarded)"
    assert result.merged_to_main

    # Subject should be prefixed.
    subject = _git("log", "-1", "--format=%s", "methodology", cwd=repo).stdout.strip()
    assert subject.startswith("methodology:")

    # Working tree on main matches.
    assert (repo / "comparanda" / ".claude" / "agents" / "extractor.md").read_text() == "improved\n"


def test_pure_domain_commits_on_main_directly(repo: Path):
    # Stage a domain change.
    extractions = repo / "comparanda" / "projects" / "example_domain" / "extractions"
    (extractions / "foo.jsonl").write_text('{"x":1}\n')
    _git("add", "comparanda/projects/example_domain/extractions/foo.jsonl", cwd=repo)

    result = harness_commit("add foo extraction")

    main_head = _git("rev-parse", "main", cwd=repo).stdout.strip()
    methodology_head = _git("rev-parse", "methodology", cwd=repo).stdout.strip()
    assert main_head != methodology_head, "main should be ahead of methodology"
    assert result.branch == "main"
    # methodology should be a strict ancestor of main.
    proc = _git("merge-base", "--is-ancestor", "methodology", "main", cwd=repo, check=False)
    assert proc.returncode == 0


def test_mixed_commit_rejected_in_strict_mode(repo: Path):
    (repo / "comparanda" / ".claude" / "agents" / "extractor.md").write_text("changed\n")
    (repo / "comparanda" / "projects" / "example_domain" / "extractions" / "foo.jsonl").write_text("{}\n")
    _git("add", "-A", cwd=repo)

    with pytest.raises(SystemExit) as exc:
        harness_commit("mixed")

    assert "mixed" in str(exc.value).lower()


def test_unknown_path_rejected_in_strict_mode(repo: Path):
    (repo / "random.txt").write_text("hello\n")
    _git("add", "random.txt", cwd=repo)

    with pytest.raises(SystemExit) as exc:
        harness_commit("unclassified")

    assert "unclassified" in str(exc.value).lower() or "unknown" in str(exc.value).lower()


def test_lenient_mode_allows_mixed(repo: Path):
    (repo / "comparanda" / ".claude" / "agents" / "extractor.md").write_text("changed\n")
    (repo / "comparanda" / "projects" / "example_domain" / "extractions" / "foo.jsonl").write_text("{}\n")
    _git("add", "-A", cwd=repo)

    result = harness_commit("mixed in lenient", mode="lenient")
    # Commit lands on current branch (main) — no routing in lenient.
    assert result.branch == "main"


def test_methodology_from_main_when_main_has_diverged_uses_merge_commit(repo: Path):
    """If main has domain commits past methodology, the merge can't be --ff-only."""
    # First: domain commit on main.
    (repo / "comparanda" / "projects" / "example_domain" / "extractions" / "domain.jsonl").write_text("{}\n")
    _git("add", "-A", cwd=repo)
    harness_commit("first domain")

    # Now methodology commit from main.
    (repo / "comparanda" / ".claude" / "agents" / "extractor.md").write_text("v2\n")
    _git("add", "-A", cwd=repo)
    harness_commit("methodology bump")

    # main should have a merge commit; methodology should be linear (no domain commits).
    methodology_log = _git("log", "--format=%s", "methodology", cwd=repo).stdout.splitlines()
    assert any("methodology bump" in s for s in methodology_log)
    assert not any("first domain" in s for s in methodology_log), \
        "methodology should not contain domain commits"


def test_no_methodology_branch_falls_back_to_current(repo: Path):
    """On canonical checkout (no methodology branch), wrapper commits on current branch."""
    _git("branch", "-D", "methodology", cwd=repo)
    (repo / "comparanda" / ".claude" / "agents" / "extractor.md").write_text("change\n")
    _git("add", "-A", cwd=repo)
    result = harness_commit("on canonical")
    assert result.branch == "main"
    # Subject should still be prefixed since this was classified as methodology.
    subject = _git("log", "-1", "--format=%s", cwd=repo).stdout.strip()
    assert subject.startswith("methodology:")


def test_nothing_staged_exits_with_warning(repo: Path):
    with pytest.raises(SystemExit) as exc:
        harness_commit("nothing")
    assert "nothing staged" in str(exc.value).lower()


def test_dry_run_no_commit_made(repo: Path):
    (repo / "comparanda" / ".claude" / "agents" / "extractor.md").write_text("dr\n")
    _git("add", "-A", cwd=repo)
    before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    harness_commit("dry-run", dry_run=True)
    after = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert before == after
