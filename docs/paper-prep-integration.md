# Paper-prep integration — manual pre-step

Bundles are NOT auto-built by the comparanda orchestrator — they are produced
manually by `paper-prep` BEFORE running any comparanda extraction phases.

The preprocessor lives in-tree at `comparanda/scripts/paper_prep/` as a
self-contained `uv` workspace. (Historically it was an external repo
`<owner>/comparanda-paper-prep`; that repo still exists for ad-hoc
debug tooling — interactive viewer, alternative extractor experiments — but
is no longer needed to produce bundles.)

## Pre-step: build the bundles before running comparanda

First-time setup:

```bash
cd "$COMPARANDA_PROJECT_DIR/comparanda/comparanda/scripts/paper_prep"
uv sync   # installs docling + pydantic
```

Per-paper build:

```bash
uv run python -m paper_prep <paper_id> \
    --pdf "/path/to/<paper_id>.pdf" \
    --output-root "$COMPARANDA_PROJECT_DIR/data/processed/"
```

Corpus build (subset JSON maps PMID → paper_id):

```bash
uv run python -m paper_prep.batch \
    --subset examples/dpyd_batch_subset.json \
    --pdfs-dir "$COMPARANDA_PROJECT_DIR/data/raw/pdfs/" \
    --bundle-out "$COMPARANDA_PROJECT_DIR/data/processed/"
```

> **Run from a regular terminal, not from inside a Claude Code session.**
> The Sonnet calls are `claude -p` subprocesses needing OAuth keychain auth;
> nested invocations from inside an outer Claude Code session are broken.

## Pipeline (per paper)

1. **Layer 1** (`paper_prep.layer1`) — Docling extracts sections + tables +
   figures; per-table Sonnet first-pass runs in parallel (default batch=4,
   budget ~$1.50/table).
2. **Layer 2** (`paper_prep.layer2` + `paper_prep.arbitrate`) — Stage-1
   deterministic merge (Needleman-Wunsch row alignment + greedy column
   alignment + neighborhood-consistency cell classifier) compares Sonnet
   vs Docling per table. Conflicts go to Stage-2 LLM arbiter (`claude -p`
   Sonnet with PDF + diff guide). Bundle assembly writes manifest.json,
   sections/, tables/, figures/, _provenance/.

The bundle lands at:

```
<project-root>/data/processed/<paper_id>/
    manifest.json
    sections/<name>.md
    tables/table_<id>.{json,md}
    figures/figure_<n>.png
    _provenance/
```

Use `--no-arbitrate` for a free dry-run (deterministic merge only). Bundle
drops any two-sided table whose conflicts went unresolved.

## What changed at the extractor agent + provenance hook (when paper-prep landed)

- `.claude/agents/extractor.md` — reads bundles instead of flat markdown.
  Always emits its JSONL via Bash + Python heredoc (iterative materialization).
- `.claude/hooks/validate_provenance_quotes.py` — bundle-aware. Source of
  truth is the bundle (sections + table cells + table markdown), not the PDF.
  No PyMuPDF dependency. Triggers on both `Write` AND `Bash` (for the
  heredoc path).
- `comparanda/CLAUDE.md` is unchanged. The orchestrator doesn't know about
  bundles; it invokes the extractor agent which knows where to look.

## What to do if a bundle is missing

The extractor agent expects `<project-root>/data/processed/<paper_id>/`
to exist when it's invoked. If invoked on a paper without a bundle, the
agent will fail at the "Read manifest.json" step with a missing-file error
— there is no fallback to PDF or markdown.

Fix: build the missing bundle via the pre-step above. The bundle build is
deterministic on a given run-root state, so re-running is safe.

The provenance hook also expects the bundle to exist when an extraction is
written. If the bundle is missing it returns `provenance_validation: skipped`
rather than failing — the agent gets no validation feedback in that case.

## Outstanding follow-ups

- **Provenance Option C** (structured-by-source) — deferred. See
  rewrite. Today's hook is Option B (search the whole bundle blob).
- **Boige content-filter fallback** — per-table extraction path for papers
  where `claude -p` first-pass hits a content filter. Not built yet.
- **Figure quality / caption pairing.** A handful of bundles have duplicate
  or missing figure captions; the agent has no figure-access protocol yet.

## Why this is manual rather than auto-orchestrated

Per design: don't complicate the orchestrator with bundle build logic. The
orchestrator's mental model stays "read bundle, extract, normalize, critique,
refine"; the bundle build is an offline preprocessing step the user runs
before pointing comparanda at a corpus.
