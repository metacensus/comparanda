"""Path-based commit router for the legacy fork 2-branch model.

Inspect staged paths, classify each as methodology vs domain, and route the
commit to the correct branch. Mixed-path commits are rejected in strict mode.

See docs/specs/harness-commit-wrapper.md for the full spec.

CLI:

    harness-commit -m "subject line" [--mode strict|lenient|off] [--dry-run]

Or as a module:

    uv run python -m harness.git_wrapper.commit -m "subject line"

The wrapper is invoked AFTER the operator stages changes with `git add ...`.
It does not stage anything itself.

Domain superprojects must not use this router: methodology, application state,
and deployable exports are separate Git repositories in that composition.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Sequence

# ────────────────────────────────────────────────────────────────────
# Path classification
# ────────────────────────────────────────────────────────────────────

# Methodology paths: system code that flows back to canonical via PR.
METHODOLOGY_PREFIXES: tuple[str, ...] = (
    "comparanda/.claude/",
    "comparanda/scripts/",
    "comparanda/CLAUDE.md",
    "harness/.claude/",
    "harness/src/",
    "harness/CLAUDE.md",
    "harness/benchmark/eval_config/",
    "docs/",
    "README.md",
    "CLAUDE.md",
    "harness/pyproject.toml",
    "harness/uv.lock",
)

# Domain paths: per-fork application/runtime state, never PR'd upstream.
DOMAIN_PREFIXES: tuple[str, ...] = (
    "comparanda/projects/",
    "harness/iterations/",
    "harness/benchmark/gold_standard/",
)

# Special handling for .gitignore files: classified by marker line.
# Everything above "# Fork-local override (main only):" is methodology;
# everything below is domain.
GITIGNORE_FILES: tuple[str, ...] = (
    ".gitignore",
    "comparanda/.gitignore",
    "harness/.gitignore",
)
GITIGNORE_FORK_OVERRIDE_MARKER = "# Fork-local override (main only):"


@dataclass(frozen=True)
class Classification:
    methodology: tuple[str, ...]
    domain: tuple[str, ...]
    unknown: tuple[str, ...]

    @property
    def is_pure_methodology(self) -> bool:
        return bool(self.methodology) and not self.domain and not self.unknown

    @property
    def is_pure_domain(self) -> bool:
        return bool(self.domain) and not self.methodology and not self.unknown

    @property
    def is_mixed(self) -> bool:
        return bool(self.methodology) and bool(self.domain)


def classify_paths(paths: Sequence[str]) -> Classification:
    methodology: list[str] = []
    domain: list[str] = []
    unknown: list[str] = []
    for raw in paths:
        p = PurePosixPath(raw).as_posix()
        if p in GITIGNORE_FILES:
            # We can't easily tell which lines were staged without diffing the
            # index against HEAD. Conservative: classify the whole file based
            # on a simple heuristic — if marker is present in the working tree,
            # treat any change as domain-class (fork-only override). If not,
            # treat as methodology. This matches the convention that fork-only
            # gitignore edits live AFTER the marker comment.
            if _gitignore_has_fork_override(p):
                domain.append(p)
            else:
                methodology.append(p)
            continue
        if any(p == prefix.rstrip("/") or p.startswith(prefix) for prefix in METHODOLOGY_PREFIXES):
            methodology.append(p)
        elif any(p.startswith(prefix) for prefix in DOMAIN_PREFIXES):
            domain.append(p)
        else:
            unknown.append(p)
    return Classification(
        methodology=tuple(methodology),
        domain=tuple(domain),
        unknown=tuple(unknown),
    )


def _gitignore_has_fork_override(rel_path: str) -> bool:
    abs_path = os.path.join(_git_toplevel(), rel_path)
    try:
        with open(abs_path) as f:
            return GITIGNORE_FORK_OVERRIDE_MARKER in f.read()
    except FileNotFoundError:
        return False


# ────────────────────────────────────────────────────────────────────
# Git plumbing helpers
# ────────────────────────────────────────────────────────────────────


def _run(args: Sequence[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=check,
        capture_output=capture,
        text=True,
    )


def _git_toplevel() -> str:
    return _run(["rev-parse", "--show-toplevel"]).stdout.strip()


def _staged_paths() -> tuple[str, ...]:
    out = _run(["diff", "--name-only", "--cached"]).stdout
    return tuple(line for line in out.splitlines() if line)


def _unstaged_modifications() -> tuple[str, ...]:
    """Tracked files with unstaged modifications (excludes untracked)."""
    out = _run(["diff", "--name-only"]).stdout
    return tuple(line for line in out.splitlines() if line)


def _current_branch() -> str | None:
    proc = _run(["symbolic-ref", "--short", "-q", "HEAD"], check=False)
    if proc.returncode != 0:
        return None  # detached HEAD
    return proc.stdout.strip()


def _has_branch(name: str) -> bool:
    proc = _run(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], check=False)
    return proc.returncode == 0


# ────────────────────────────────────────────────────────────────────
# Commit orchestration
# ────────────────────────────────────────────────────────────────────


Mode = Literal["strict", "lenient", "off"]


@dataclass
class CommitResult:
    branch: str
    sha: str
    classification: Classification
    merged_to_main: bool = False


def harness_commit(
    message: str,
    *,
    mode: Mode = "strict",
    dry_run: bool = False,
) -> CommitResult:
    """Path-aware commit. See module docstring for behavior."""
    staged = _staged_paths()
    if not staged:
        raise SystemExit("warning: nothing staged — nothing to commit")

    classification = classify_paths(staged)

    # Validate
    if classification.unknown and mode == "strict":
        raise SystemExit(
            "harness-commit (strict): refusing to commit unclassified paths:\n  "
            + "\n  ".join(classification.unknown)
            + "\n\nAdd them to METHODOLOGY_PREFIXES or DOMAIN_PREFIXES in "
              "harness/src/harness/git_wrapper/commit.py, or use --mode=lenient."
        )
    if classification.is_mixed and mode == "strict":
        raise SystemExit(
            "harness-commit (strict): refusing to commit mixed methodology + domain paths.\n"
            f"  methodology:\n    " + "\n    ".join(classification.methodology) + "\n"
            f"  domain:\n    " + "\n    ".join(classification.domain) + "\n\n"
            "Split into two commits: `git reset HEAD <path>` to unstage one class, commit the other, then re-stage and commit."
        )

    if dry_run:
        print(_dry_run_report(message, classification, mode))
        return CommitResult(branch=_current_branch() or "HEAD", sha="(dry-run)", classification=classification)

    if mode == "off":
        # No routing — just commit on current branch with classification logged.
        return _commit_current(message, classification)

    # Lenient mode: warn but proceed on current branch.
    if mode == "lenient" and (classification.is_mixed or classification.unknown):
        print(
            f"harness-commit (lenient): mixed/unknown paths — committing on current branch ({_current_branch()}).",
            file=sys.stderr,
        )
        return _commit_current(message, classification)

    # Pure-methodology: route to methodology branch (if available).
    if classification.is_pure_methodology:
        if not _has_branch("methodology"):
            print(
                "harness-commit: no methodology branch found — assuming canonical/non-fork checkout. "
                f"Committing on current branch ({_current_branch()}).",
                file=sys.stderr,
            )
            return _commit_current(message, classification, prefix="methodology: ")
        return _commit_methodology(message, classification)

    # Pure-domain: must commit on main.
    return _commit_domain(message, classification)


def _dry_run_report(message: str, c: Classification, mode: Mode) -> str:
    lines = [f"DRY RUN — would commit message: {message!r}", f"  mode: {mode}"]
    if c.methodology:
        lines.append(f"  methodology paths ({len(c.methodology)}):")
        for p in c.methodology:
            lines.append(f"    {p}")
    if c.domain:
        lines.append(f"  domain paths ({len(c.domain)}):")
        for p in c.domain:
            lines.append(f"    {p}")
    if c.unknown:
        lines.append(f"  unknown paths ({len(c.unknown)}):")
        for p in c.unknown:
            lines.append(f"    {p}")
    if c.is_pure_methodology:
        target = "methodology (then merge into main)"
    elif c.is_pure_domain:
        target = "main"
    elif c.is_mixed:
        target = "REJECTED (mixed)"
    else:
        target = "current branch (no classification)"
    lines.append(f"  target branch: {target}")
    return "\n".join(lines)


def _ensure_prefix(message: str, prefix: str) -> str:
    return message if message.startswith(prefix) else f"{prefix}{message}"


def _commit_current(message: str, classification: Classification, *, prefix: str = "") -> CommitResult:
    full = _ensure_prefix(message, prefix) if prefix else message
    _run(["commit", "-m", full], capture=False)
    sha = _run(["rev-parse", "HEAD"]).stdout.strip()
    return CommitResult(branch=_current_branch() or "HEAD", sha=sha, classification=classification)


def _commit_domain(message: str, classification: Classification) -> CommitResult:
    branch = _current_branch()
    if branch == "methodology":
        raise SystemExit(
            "harness-commit: refusing to commit domain paths on `methodology` branch. "
            "Domain commits belong on `main`. `git checkout main` first."
        )
    full = _ensure_prefix(message, "")  # subject as-is; operator picks prefix
    _run(["commit", "-m", full], capture=False)
    sha = _run(["rev-parse", "HEAD"]).stdout.strip()
    return CommitResult(branch=branch or "HEAD", sha=sha, classification=classification)


def _commit_methodology(message: str, classification: Classification) -> CommitResult:
    """Commit pure-methodology changes on `methodology` branch, then bring forward to main.

    Strategy:
      1. Require working tree clean (no unstaged tracked modifications).
      2. Stash staged changes (`git stash push --staged`).
      3. Checkout methodology.
      4. Pop stash (with --index to restore stage state).
      5. Commit with `methodology:` prefix.
      6. Return to original branch.
      7. If original was main: merge methodology in. Use --ff-only if possible,
         otherwise a merge commit (main has domain commits that diverged from methodology).
    """
    starting_branch = _current_branch()
    if starting_branch is None:
        raise SystemExit("harness-commit: detached HEAD — cannot route methodology commit. Checkout a branch first.")

    if _unstaged_modifications():
        raise SystemExit(
            "harness-commit: refusing to route methodology commit while there are unstaged "
            "modifications to tracked files. Either stage them (if related) or stash them first.\n"
            "  unstaged: " + ", ".join(_unstaged_modifications())
        )

    full_msg = _ensure_prefix(message, "methodology: ")

    if starting_branch == "methodology":
        # Already on methodology — commit directly.
        _run(["commit", "-m", full_msg], capture=False)
        sha = _run(["rev-parse", "HEAD"]).stdout.strip()
        # Bring forward to main, then return to methodology so operator stays put.
        merged = _merge_methodology_into_main()
        _run(["checkout", "methodology"], capture=False)
        return CommitResult(branch="methodology", sha=sha, classification=classification, merged_to_main=merged)

    # On main (or another branch) — stash, switch, commit, return, merge.
    stash_made = False
    try:
        proc = _run(["stash", "push", "--staged", "-m", "harness-commit-staging"], check=False)
        if proc.returncode != 0:
            raise SystemExit(f"harness-commit: `git stash push --staged` failed:\n{proc.stderr}")
        stash_made = True

        _run(["checkout", "methodology"], capture=False)

        pop = _run(["stash", "pop", "--index"], check=False)
        if pop.returncode != 0:
            # Try to recover by going back to starting branch.
            _run(["checkout", starting_branch], check=False, capture=False)
            raise SystemExit(
                f"harness-commit: failed to pop stash on methodology branch (likely conflict with existing methodology state).\n"
                f"  git stderr: {pop.stderr}\n"
                "Recover manually: `git stash list` (look for harness-commit-staging), then `git stash drop` or `git stash pop` after resolving."
            )
        stash_made = False  # stash consumed

        _run(["commit", "-m", full_msg], capture=False)
        sha = _run(["rev-parse", "HEAD"]).stdout.strip()

        _run(["checkout", starting_branch], capture=False)
        merged = False
        if starting_branch == "main":
            merged = _merge_methodology_into_main()
        return CommitResult(branch="methodology", sha=sha, classification=classification, merged_to_main=merged)
    except SystemExit:
        raise
    except Exception as exc:
        # Best-effort recovery
        _run(["checkout", starting_branch], check=False, capture=False)
        if stash_made:
            print(f"harness-commit: error — your changes are in git stash. {exc}", file=sys.stderr)
        raise


def _merge_methodology_into_main() -> bool:
    """Merge methodology branch into main. Use --ff-only when possible (linear),
    otherwise a merge commit. Returns True on success."""
    proc = _run(["merge", "--ff-only", "methodology"], check=False)
    if proc.returncode == 0:
        return True
    # Fall back to merge commit (main has diverged with domain commits).
    proc2 = _run(["merge", "--no-ff", "methodology", "-m", "Merge methodology forward"], check=False)
    if proc2.returncode != 0:
        raise SystemExit(
            f"harness-commit: failed to merge methodology into main:\n{proc2.stderr}\n"
            "Resolve conflicts manually, then commit + run `git checkout main` to verify state."
        )
    return True


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="harness-commit",
        description="Path-aware commit router for the fork 2-branch model (methodology vs domain).",
    )
    p.add_argument("-m", "--message", required=True, help="Commit message subject.")
    default_mode = os.environ.get("HARNESS_COMMIT_MODE", "strict")
    p.add_argument(
        "--mode",
        choices=("strict", "lenient", "off"),
        default=default_mode,
        help=f"Routing mode (default {default_mode!r} from $HARNESS_COMMIT_MODE).",
    )
    p.add_argument("--dry-run", action="store_true", help="Print plan, don't commit.")
    args = p.parse_args(argv)

    try:
        result = harness_commit(args.message, mode=args.mode, dry_run=args.dry_run)
    except SystemExit:
        raise
    except subprocess.CalledProcessError as e:
        print(f"harness-commit: git failed: {e.stderr or e}", file=sys.stderr)
        return 1

    if not args.dry_run:
        print(
            f"harness-commit: {result.sha[:8]} on {result.branch}"
            + (" (merged into main)" if result.merged_to_main else "")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
