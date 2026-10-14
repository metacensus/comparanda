# Paper-prep integration — manual pre-step

Bundles are NOT auto-built by the comparanda orchestrator — they are produced
manually by `paper-prep` BEFORE running any comparanda extraction phases.

The preprocessor lives in-tree at `comparanda/scripts/paper_prep/` as a
self-contained `uv` workspace.

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
    --subset examples/example_batch_subset.json \
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

Use `--no-arbitrate` to skip the Stage-2 arbiter calls (deterministic merge only;
the per-table first-pass `claude -p` calls still run and cost money). Bundle
drops any two-sided table whose conflicts went unresolved.

## How the extractor and guardrails use bundles

- `.claude/agents/extractor.md` reads bundles (`manifest.json`, sections,
  tables, figures) rather than PDFs or flat markdown, and writes rows through
  the staged Extraction Builder SDK (see
  `comparanda/docs/extraction-sdk-agent-contract.md`).
- Provenance is validated by `scripts/extraction/extraction_validator.py`,
  which the sanctioned finalizers (`write_extraction.py`,
  `extraction_builder.py`, `run_extraction_build.py`) call with the paper's
  bundle (`--bundle`). The bundle is the source of truth: every cited quote
  must appear verbatim in `sections/*.md`, the flattened `tables/*.json` cell
  values, or `tables/*.md`; table values need resolvable row/column anchors.
- `.claude/hooks/validate_extraction_write.py` (PreToolUse on Write, Edit,
  MultiEdit, NotebookEdit, and Bash) blocks any direct write to
  `extractions/<paper_id>.jsonl` outside the sanctioned finalizers, so the
  validator cannot be bypassed.
- `.claude/hooks/ensure_extraction_finalized.py` (SubagentStop) blocks an
  extractor from stopping until a sanctioned finalizer has reported success.
- `comparanda/CLAUDE.md` does not build bundles. The orchestrator passes the
  bundle path to the extractor agent, which knows where to look.

## What to do if a bundle is missing

The extractor agent expects `<project-root>/data/processed/<paper_id>/`
to exist when it's invoked. If invoked on a paper without a bundle, the
agent will fail at the "Read manifest.json" step with a missing-file error
— there is no fallback to PDF or markdown.

Fix: build the missing bundle via the pre-step above. The bundle build is
deterministic on a given run-root state, so re-running is safe.

Finalization also needs the bundle: the sanctioned finalizers take a
required `--bundle` argument and validate every quote against it.

## Outstanding follow-ups

- **Structured-by-source provenance** — deferred. Quote validation currently
  searches the whole bundle text rather than the specific cited section or
  table.
- **Content-filter fallback** — a per-table extraction path for papers where
  the `claude -p` first pass hits a content filter. Not built yet.
- **Figure quality / caption pairing.** A handful of bundles have duplicate
  or missing figure captions; the agent has no figure-access protocol yet.

## Why this is manual rather than auto-orchestrated

Per design: don't complicate the orchestrator with bundle build logic. The
orchestrator's mental model stays "read bundle, extract, normalize, critique,
refine"; the bundle build is an offline preprocessing step the user runs
before pointing comparanda at a corpus.
