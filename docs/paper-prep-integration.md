# Paper-prep integration — manual pre-step

This branch (`paper-prep-integration`) replaces the flat-paper-markdown
extractor with one that reads paper-prep bundles. Bundles are NOT
auto-built by the comparanda orchestrator — they are produced manually
by `paper-prep` BEFORE running any comparanda extraction phases.

## What changed in this branch

- `.claude/agents/extractor.md` — replaced with the merged paper-prep
  variant. Reads bundles instead of flat markdown. Always emits its
  JSONL via Bash + Python heredoc (iterative materialization). See
- `.claude/hooks/validate_provenance_quotes.py` — replaced with the
  bundle-aware version. Source of truth is the bundle (sections + table
  cells + table markdown), not the PDF. Drops PyMuPDF dependency.
  Triggers on both `Write` AND `Bash` (for the heredoc path).
- CLAUDE.md is unchanged. The orchestrator does not know about bundles;
  it just invokes the extractor agent which knows where to look.

## Pre-step: build the bundles before running comparanda

```bash
# From paper-prep/, with paper-prep installed in its venv.
cd /path/to/paper-prep

# Build per-paper. The two-arg invocation requires --run-dir (the
# Phase 3 production output dir) and --pdf (the source PDF).
uv run python -m paper_prep.prep <paper_id> \
    --run-dir experiments/phase3_run/<paper_id> \
    --pdf "/path/to/<paper_id>.pdf" \
    --output-root <workspace>/comparanda-<domain>/comparanda/projects/<domain>/data/processed/
```

The bundle lands at:

```
projects/dpyd/data/processed/<paper_id>/
    manifest.json
    sections/<name>.md
    tables/table_<id>.{json,md}
    figures/figure_<n>.png
    _provenance/
```

For batch runs across the corpus, see Task F in
there once it exists).

## What to do if a bundle is missing

The extractor agent expects `projects/dpyd/data/processed/<paper_id>/`
to exist when it's invoked. If invoked on a paper without a bundle,
the agent will fail at the "Read manifest.json" step with a missing-
file error — there is no fallback to PDF or markdown.

Fix: build the missing bundle via the pre-step above before re-running
the orchestrator. Bundle build is deterministic on a given Phase 3 run
output, so re-running is safe.

The provenance hook also expects the bundle to exist when an extraction
is written. If the bundle is missing it returns
`provenance_validation: skipped` rather than failing — the agent gets
no validation feedback in that case.

## Outstanding follow-ups

- **Provenance Option C** (structured-by-source) — deferred. See
  rewrite. Today's hook is Option B (search the whole bundle blob).
- **Boige content-filter fallback** — per-table extraction path for
  papers where `claude -p` first-pass hits a content filter. Not built
  yet. Catch in Task F's run summary; revisit if DPYD has Boige analogs.
- **Paper-prep as a Python dependency of comparanda** — at cutover
  (Task I), `paper-prep` will be added to `comparanda/pyproject.toml`
  so `python -m paper_prep.prep` is invokable from within the comparanda
  environment. Until then, run prep.py from the paper-prep venv.

## Why this is manual rather than auto-orchestrated

Per Task E user direction: don't complicate the orchestrator with bundle
build logic. The orchestrator's mental model stays "read bundle, extract,
normalize, critique, refine"; the bundle build is an offline preprocessing
step the user runs before pointing comparanda at a corpus.
