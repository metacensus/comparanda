# Harness commit wrapper — spec

> **Status:** legacy per-domain-fork compatibility only. Domain superprojects
> use separate Git repositories for methodology, application state, and exports;
> commit directly in the repository that owns the path. Do not use this router
> in the composed layout. The remaining specification documents the older
> two-branch model.

## Problem

The harness auto-commits two kinds of changes during a run:

1. **Methodology edits** — diagnostician/critic agents amend files under `comparanda/.claude/`, `comparanda/scripts/`, `harness/.claude/`, `harness/src/`, or the `CLAUDE.md` orchestrator files. These are system-code changes that should eventually flow back to canonical via cross-repo PR.
2. **Domain artifacts** — iteration archives at `harness/iterations/iter_N/...`, trajectory updates, new extractions / critiques / observations under `comparanda/projects/<domain>/...`. These are per-domain state and **must never** reach canonical.

The fork's 2-branch model isolates these:
- `methodology` branch — tracks `upstream/main`; only methodology commits land here; PR'd upstream.
- `main` branch — `methodology` merged forward + domain commits stacked on top; harness reads from `main` so it always sees the latest methodology.

The harness today calls `git commit` against whatever branch is checked out (always `main` in practice). This means methodology and domain commits interleave on `main`, requiring manual cherry-pick to reconstruct `methodology` at PR time.

## What the wrapper does

Replace direct `git commit` calls in harness code with a `harness_commit()` helper. Before each commit, the helper:

1. Inspects `git diff --name-only --cached` to get the staged paths.
2. Classifies each path as `methodology` or `domain` (see "Path classification" below).
3. **Rejects mixed-path commits.** If both classes appear in the staged set, error out with the path lists, instruct the operator to `git reset HEAD <path>` to split into two commits, and abort.
4. For a pure-methodology commit:
   ```bash
   git stash --include-untracked   # preserve unrelated workdir state
   git checkout methodology
   git stash pop                   # restore — but only methodology paths are staged
   git commit -m "methodology: <subject>"
   git checkout main
   git merge methodology --ff
   ```
5. For a pure-domain commit:
   ```bash
   git commit -m "<domain-prefix>: <subject>"   # stays on main
   ```

Subject-line prefixes (`methodology:` / `dpyd:` / etc.) are emitted by the wrapper automatically based on classification.

## Path classification

**Methodology paths** (commit on `methodology`, merge forward to `main`):
- `comparanda/.claude/**`
- `comparanda/scripts/**`
- `comparanda/CLAUDE.md`
- `harness/.claude/**`
- `harness/src/**`
- `harness/CLAUDE.md`
- `harness/benchmark/eval_config/**` (eval-policy changes, not gold-standard data)
- `comparanda/.gitignore` (canonical lines only — fork's `!`-override lines live on `main` and must not be staged in a methodology commit; if the wrapper sees a stage of these specific lines, reject as mixed)
- `harness/.gitignore` (same caveat)
- Any `docs/**` or root-level `CLAUDE.md` / `README.md` edits

**Domain paths** (commit on `main` only):
- `comparanda/projects/**`
- `harness/iterations/**`
- `harness/benchmark/gold_standard/**` (symlink target; usually unchanged)

**Ambiguous / not-allowed paths:** `.git/`, anything outside the repo, untracked binaries. Reject with a clear message.

## Edge cases

1. **Empty stage.** No-op; warn.
2. **Methodology commit when `methodology` is behind `main`.** Shouldn't happen if discipline is followed (every methodology commit gets merged forward), but if `main` is ahead of `methodology` on methodology paths (e.g., a manual edit was committed to `main` directly), the wrapper detects via `git log methodology..main -- <methodology-paths>` and errors out: "main has unmerged methodology changes; reconcile before continuing."
3. **`.gitignore` files.** Fork-only override lines (`!/projects/dpyd/` etc.) live on `main`. If the harness stages a `.gitignore` change that only adds/modifies override lines, it's a domain commit. If it stages canonical-shape lines (anything not under the override block), it's methodology. The wrapper distinguishes via a marker comment in the gitignore file (e.g., `# Fork-local override (main only):` — everything after this line is fork-only).
4. **Crash mid-commit.** If `git checkout methodology` succeeds but `git commit` fails, the wrapper restores by `git checkout main` and surfacing the original staged paths.
5. **Operator already on `methodology`.** Wrapper detects, skips the branch-switch dance, commits directly.
6. **No `methodology` branch present.** This is canonical (`metacensus/comparanda`) or any non-fork checkout. The wrapper detects via `git rev-parse --verify methodology` and falls back to committing on the current branch (with classification still applied for subject-prefix purposes, but no branch switch). Log a one-line info message: "no methodology branch found — assuming canonical/non-fork checkout, committing on <current-branch>." This makes the wrapper safe to invoke from canonical's own CI / dev workflows.

## Configuration

Two knobs (env vars or `harness.toml`):
- `HARNESS_COMMIT_MODE` ∈ `{strict, lenient, off}`. `strict` (default) rejects mixed commits. `lenient` warns + commits everything to `main` (Option B behavior, useful as a temporary escape hatch). `off` disables auto-commit entirely.
- `HARNESS_COMMIT_DRY_RUN` ∈ `{true, false}`. Prints the classification and the commits it would make, but doesn't execute. For testing.

## Call sites to thread through

Every place the harness today calls `git commit` (or `subprocess.run(["git", "commit", ...])`) needs to route through `harness_commit()`. Audit before implementation:
```bash
grep -rn "git commit\|git.commit\|subprocess.*commit" harness/src/
```

Likely surfaces: diagnostician agent (when it rewrites an agent prompt), monitor (when it archives iteration state), trajectory writer, observation logger.

## Testing

- Unit test the path classifier with each path category.
- Integration test against a temp git repo: stage a methodology file, run wrapper, assert commit landed on `methodology` and was merged forward.
- Integration test mixed-path rejection: stage one methodology + one domain file, assert wrapper aborts with clear error.
- Integration test crash recovery: simulate `git commit` failure mid-flow, assert workdir state is restored.

## Enablement

The wrapper lands in canonical's `harness/src/` in Phase 2.5, before the fork is created. When the fork is cloned (Phase 5), the wrapper is already present in `harness/src/`. The operator sets `HARNESS_COMMIT_MODE=strict` in the harness session env (or `harness.toml` in the fork checkout) before the first harness run.

## Not in scope

- Squash-merge prevention at the canonical level (handled separately via `gh repo edit metacensus/comparanda --enable-squash-merge=false`).
- Branch protection on canonical `main` (also handled separately).
- A pre-commit hook variant: the wrapper is sufficient because the harness is the only auto-committer in the fork. Human commits use the standard `git checkout methodology` + `git merge --ff` flow documented in the root README / CLAUDE.md.
