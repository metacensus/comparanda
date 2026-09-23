# Comparanda Scripts

This directory contains stable command-line and helper APIs used by the
Comparanda agents. Agent prompts should point to the documented command surface,
not to implementation internals, unless a contract explicitly says otherwise.

## Extraction

Path: `scripts/extraction/`

| Script | Purpose | Primary caller |
|---|---|---|
| `run_extraction_build.py` | Runs an extractor-authored staging script from a controlled repository-root environment and optionally finalizes JSONL output. | `extractor` agent |
| `extraction_sdk.py` | Python SDK used inside extractor-authored build scripts to construct staged rows safely. | extractor-authored `build.py` |
| `extraction_builder.py` | CLI for initializing, validating, chunk-validating, and finalizing staged extractions. | `run_extraction_build.py`, extractor agent |
| `write_extraction.py` | Lower-level materializer for iter-1 and iter-n extraction writes. Kept as a sanctioned path for non-SDK flows. | `extractor` agent |
| `extraction_validator.py` | Shared validator for row shape, provenance, table anchors, derivations, certainty, and reasoning requirements. | write/build/review helpers |
| `extraction_core.py` | Shared row materialization and merge core used by extraction CLIs. | extraction CLIs |
| `extractor_session_registry.py` | Records extractor session IDs, staging scripts, schema versions, and finalized output paths for QC repair/resume. | orchestrator |

Stable agent contract: `../docs/extraction-sdk-agent-contract.md`.

## Normalization

Path: `scripts/normalization/`

| Script | Purpose | Primary caller |
|---|---|---|
| `normalization_reconcile.py` | Emits deterministic stale-entry/review hints after schema or guidance changes. It does not decide truth. | `normalizer` agent |
| `normalization_api.py` | Stable write API for reconciliation decisions, applying canonical mappings, validation, and audit cleanup. | `normalizer` agent |

Stable agent contract: `../docs/normalization-api-agent-contract.md`.

## Review

Path: `scripts/review/`

| Script | Purpose | Primary caller |
|---|---|---|
| `review_queue.py` | Builds a queue of high-leverage extracted claims needing human review. | reviewer workflow |
| `render_review_packet.py` | Renders a compact paper/review packet from extraction rows and bundle evidence. | reviewer workflow |
| `apply_review.py` | Applies human review decisions back to extraction provenance in an audited form. | reviewer workflow |
| `portal_export.py` | Builds an immutable reviewer snapshot (format v002 with `evidence/` store) in a domain data repository; resolves external project/config/output paths through `project_layout.py`; `--highlight-cache <export>` reuses resolved highlights from an existing export. | reviewer release workflow |

## Shared Utility

| Script | Purpose | Primary caller |
|---|---|---|
| `project_layout.py` | Resolves a flattened external domain root plus iteration, benchmark, and export paths from config/environment. | orchestration and exporters |
| `observation_log.py` | Append/query helper for structured observation logs. Agents use it instead of editing `log.jsonl` directly. | all inner-loop agents |

## Paper Prep

Path: `scripts/paper_prep/`

Self-contained `uv` workspace for PDF-to-bundle preprocessing. It intentionally
keeps its own `pyproject.toml`, lockfile, modules, and examples.

## Runtime State

Do not commit runtime state produced by these tools:

- `<project-root>/extractions/.staging/`
- `<project-root>/extractions/*.provenance-failed.json`
- transient run logs and stream logs. Curated run/eval artifacts belong in the
  domain superproject; published reviewer snapshots belong in its export
  submodule.

## Compatibility

Older staged extractor scripts may import `scripts.extraction_sdk` or related
extraction modules. `scripts/__init__.py` maps those imports to the new grouped
extraction modules so existing staged scripts can still be resumed. New code
should use the grouped import/CLI paths documented above.
