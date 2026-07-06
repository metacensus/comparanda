# Comparanda Agent Index

This is the operator reference for Comparanda inner-loop agents. `CLAUDE.md`
remains the authoritative phase procedure; this file summarizes agent scope,
file contracts, script dependencies, and the resume behavior that is currently
encoded in the orchestrator and agent instructions.

## Phase Map

| Phase | Agent | What Happens | Cardinality | Resume Policy |
|---|---|---|---:|---|
| 0 | `survey`, `schema-proposer`, `extractor`, `normalizer`, `ma-critique` in reference-generation mode | Build domain reference files for each role from seed materials and phase-0 reference papers. | one per reference file | Fresh invocation; reference files are the checkpoint. |
| 0.5 | `schema-proposer` in survey-schema mode | Create the survey schema used by paper-survey agents. | one per domain | Fresh invocation; survey schema file is the checkpoint. |
| 1 | `survey` | Read each processed bundle and write a breadth-first inventory of extractable data. | one per paper | Fresh invocation; survey JSON is the checkpoint. |
| 1 / 5 | `subset-selector` | Select a diverse 3-4 paper subset, with an anchor when rotating. | one per subset decision | Fresh invocation; prior selections are recovered from the observation log. |
| 2 | `schema-proposer` | Create initial rich extraction schema from surveys and domain references. | one per schema version | Fresh invocation; schema version is the checkpoint. |
| 3a | `extractor` | Extract one paper into atomic rows with provenance through the sanctioned SDK/write flow. | one per paper | Resumed for QC repair and schema-iteration patches when a recorded session exists. Replacement extractor is only a fallback after resume retries fail. |
| 3a-QC | `extraction-qc` | Verify one extraction against survey, schema, source evidence, and selection-error risks. | one per paper QC pass | Fresh invocation for each QC or re-QC pass. |
| 3b | `normalizer` | Reconcile normalization tables and apply canonical mappings to extraction rows. | one per loop, project-wide | Fresh invocation in the current orchestrator; normalization files and reconciliation candidates are checkpoints. |
| 3.5 | `ma-critique` | Judge meta-analysis readiness and issue structured schema/re-extraction recommendations. | one per loop | Fresh invocation; critique JSON is the checkpoint. |
| 4 | `schema-proposer` | Apply MA Critique recommendations into a new schema version. | one per schema version | Fresh invocation; prior schema, critique JSON, and observation log are inputs. |
| 5 | `subset-selector` plus phases 3a-3.5 | Rotate subsets to test schema generalization. | one subset per rotation | Extractor resume still applies for previously seen papers; new papers use fresh extractors. |
| 6 | `extractor`, `normalizer`, `ma-critique` | Produce the final full-corpus extraction against the converged schema, then normalize and critique the final dataset. | bounded batches, not full-corpus concurrency | Reuse/resume recorded extractor sessions and build scripts before fresh extraction; fresh extractors only for unseen or documented-unrecoverable papers. |

## Shared Project State (`projects/<domain>/`)

| Path | Owner / Writers | Readers |
|---|---|---|
| `schema/survey_schema.json` | `schema-proposer` in survey-schema mode | `survey` |
| `data/processed/{paper_id}/` | paper-prep outside this loop | `survey`, `extractor`, targeted QC/eval checks |
| `surveys/{paper_id}.json` | `survey` | `schema-proposer`, `subset-selector`, `extraction-qc` |
| `schema/v*.yaml` | `schema-proposer` | all downstream agents |
| `extractions/{paper_id}.jsonl` | `extractor` through sanctioned extraction scripts; `normalizer` through normalization API apply | `extraction-qc`, `normalizer`, `ma-critique`, harness eval |
| `extractions/{paper_id}_qc.json` | `extraction-qc` | orchestrator, resumed `extractor`, harness diagnostician |
| `extractions/.staging/{paper_id}/` | `extractor` | resumed `extractor`, QC repair flow, debugging |
| `extractions/.staging/extractor_sessions.json` | orchestrator via `extractor_session_registry.py` | orchestrator QC/schema repair flow |
| `normalization/{table}.json` | `normalizer` through normalization API | `normalizer`, `ma-critique`, harness eval |
| `normalization/reconciliation_candidates_iter_{N}.json` | `normalizer` via reconciliation helper | `normalizer` |
| `critiques/iter_{NNN}.json` | `ma-critique` | orchestrator, `schema-proposer`, harness diagnostician |
| `observations/log.jsonl` | all agents through `observation_log.py` | all agents |

## Agents

### `survey`

Scope: breadth-first paper inventory. Catalogs what extractable data exists; does not extract, normalize, or interpret.

Model: `haiku`

Inputs:
- `data/processed/{paper_id}/` paper-prep bundle
- `schema/survey_schema.json`
- `domain/survey_ref.md`

Outputs:
- `surveys/{paper_id}.json`
- one `paper_flag` observation in `observations/log.jsonl`
- in reference-generation mode: `domain/survey_ref.md`

Scripts:
- `scripts/observation_log.py append`

Resume policy:
- Current orchestrator behavior is fresh invocation per survey.
- If rerun is needed, replace the survey JSON and append a new observation.

### `subset-selector`

Scope: selects 3-4 diverse papers, preserving an anchor where appropriate, to stress-test schema generalization.

Model: `claude-sonnet-5[1m]`

Inputs:
- `surveys/*.json`
- `observations/log.jsonl` for subset history
- current iteration/rotation context from the orchestrator

Outputs:
- selected paper IDs returned to orchestrator
- `subset_selection` observation in `observations/log.jsonl`
- in reference-generation mode: `domain/subset_selector_ref.md`

Scripts:
- `scripts/observation_log.py append`

Resume policy:
- Current orchestrator behavior is fresh invocation per subset decision.
- Prior choices are recovered from the observation log, not hidden session memory.

### `schema-proposer`

Scope: owns schema creation/refinement. Synthesizes surveys, domain references, observation history, and MA Critique recommendations into rich YAML schema versions.

Model: `claude-sonnet-5[1m]`

Inputs:
- `surveys/*.json`
- `domain/schema_ref.md`
- `observations/log.jsonl`
- prior `schema/v*.yaml` on later iterations
- latest `critiques/iter_{NNN}.json` on schema-refinement iterations

Outputs:
- `schema/v001.yaml` or next `schema/v*.yaml`
- `schema_rationale` observations in `observations/log.jsonl`
- in reference-generation mode: `domain/schema_ref.md`

Scripts:
- `scripts/observation_log.py append`

Resume policy:
- Current orchestrator behavior is fresh invocation per schema version.
- Schema files, critique JSON, and observation log are the durable handoff.

### `extractor`

Scope: per-paper workhorse. Reads a paper-prep bundle and schema, writes atomic rows with provenance, certainty, derivation labels, table cell anchors, and extraction meta-commentary. Does not normalize.

Model: `claude-sonnet-5[1m]`

Inputs:
- `data/processed/{paper_id}/` paper-prep bundle
- current `schema/v*.yaml`
- `domain/extraction_ref.md`
- filtered observation-log query for the paper
- `docs/extraction-sdk-agent-contract.md`
- optional prior `extractions/{paper_id}.jsonl`
- optional QC repair packet from `extractions/{paper_id}_qc.json`
- optional existing staging script at `extractions/.staging/{paper_id}/scripts/build.py`

Outputs:
- `extractions/{paper_id}.jsonl`
- `extractions/.staging/{paper_id}/` staging directory:
  - `scripts/build.py` for the preferred SDK build workflow
  - `build_manifest.json` from SDK/finalization metadata
  - `common_fields.json` for common study-level field declarations
  - `chunks/*.rows.json` for SDK chunked row payloads
  - `validation/*.validate.json` for chunk/finalization validation reports
  - fallback `rows.json`, optional `rows_part_*.json`, and `meta.json` when using `write_extraction.py` directly
- extraction difficulty observations in `observations/log.jsonl`
- in reference-generation mode: `domain/extraction_ref.md`

Scripts and contracts:
- `scripts/observation_log.py query`
- `scripts/observation_log.py append`
- `scripts/extraction/run_extraction_build.py`
- `scripts/extraction/extraction_builder.py`
- `scripts/extraction/write_extraction.py`
- SDK modules under `scripts/extraction/`
- `docs/extraction-sdk-agent-contract.md`

Hooks:
- `.claude/hooks/validate_extraction_write.py` blocks direct JSONL writes.
- `.claude/hooks/ensure_extraction_finalized.py` enforces sanctioned finalization on subagent stop.

Resume policy:
- Current orchestrator behavior requires extractor session recording in `extractions/.staging/extractor_sessions.json`.
- QC repair must first resume the recorded extractor with `SendMessage`.
- Schema-iteration re-extraction must first resume the recorded extractor when possible.
- Resume is retried before a replacement extractor is launched.
- Replacement extractor is the fallback only when recorded-session resume fails.
- The registry records `current_agent_id`, `original_agent_id`, `sessions[]`,
  and `resume_attempts[]`. When replacement is required, the replacement must
  become `current_agent_id`; the original remains in lineage for audit.

### `extraction-qc`

Scope: per-paper verifier between extraction and normalization. Checks survey coverage, schema compliance, internal consistency, and selection-error/table-cell risks. Does not extract or mutate rows.

Model: `claude-sonnet-5[1m]`

Inputs:
- `extractions/{paper_id}.jsonl`
- `surveys/{paper_id}.json`
- current `schema/v*.yaml`
- `observations/log.jsonl`
- targeted reads from `data/processed/{paper_id}/` when evidence checks require source context
- optional staging artifacts under `extractions/.staging/{paper_id}/`:
  - `build_manifest.json`
  - `common_fields.json`
  - `chunks/*.rows.json`
  - `validation/*.validate.json`

Outputs:
- `extractions/{paper_id}_qc.json`:
  - `coverage_gaps`
  - `compliance_issues`
  - `consistency_issues`
  - `selection_suspects`
  - `selection_check_coverage`
  - `action_class`
  - `must_fix_before_normalization`
  - `repair_packet`
  - `summary`
- QC observation in `observations/log.jsonl`

Scripts:
- `scripts/observation_log.py append`
- shell/grep/glob for source and extraction inspection

Resume policy:
- Current orchestrator behavior is fresh invocation for QC and re-QC.
- QC repair is executed by resuming or replacing the extractor, then invoking QC again.

### `normalizer`

Scope: project-wide normalization pass. Discovers normalizable fields from schema annotations, maps raw values to canonical IDs, reconciles stale mappings after schema/guidance changes, and applies mappings to extraction rows through the API.

Model: `claude-sonnet-5[1m]`

Inputs:
- `docs/normalization-api-agent-contract.md`
- `extractions/*.jsonl`
- current `schema/v*.yaml`
- existing `normalization/*.json`
- `domain/normalization_ref.md`
- `observations/log.jsonl`
- reconciliation candidates from `normalization/reconciliation_candidates_iter_{N}.json` after helper execution

Outputs:
- updated `normalization/{table}.json`
- canonical-field updates in `extractions/*.jsonl` through `normalization_api.py apply`
- normalization observations in `observations/log.jsonl`
- in reference-generation mode: `domain/normalization_ref.md`

Scripts and contracts:
- `scripts/normalization/normalization_reconcile.py`
- `scripts/normalization/normalization_api.py reconcile`
- `scripts/normalization/normalization_api.py apply`
- `scripts/normalization/normalization_api.py validate`
- `scripts/observation_log.py append`
- `docs/normalization-api-agent-contract.md`

Resume policy:
- Current orchestrator behavior is fresh invocation per normalization phase.
- Correctness is file-checkpointed in normalization tables, reconciliation candidates, API audit output, and extraction files.

### `ma-critique`

Scope: meta-analysis readiness judge and convergence signal. Determines whether current schema/extractions are sufficient for pooling and originates schema-change recommendations.

Model: `claude-sonnet-5[1m]`

Inputs:
- normalized `extractions/*.jsonl`
- current `schema/v*.yaml`
- `domain/ma_critique_ref.md`
- `domain/deep_research.md`
- `observations/log.jsonl`, including prior critiques

Outputs:
- `critiques/iter_{NNN}.json`
- structured `recommended_action`
- critique observations in `observations/log.jsonl`
- optional update to `domain/ma_critique_ref.md`

Scripts:
- `scripts/observation_log.py append`

Resume policy:
- Current orchestrator behavior is fresh invocation per critique.
- Critique JSON is the durable handoff to orchestrator and schema-proposer.

## Guardrails

- `.claude/hooks/validate_extraction_write.py`: blocks direct writes to `extractions/{paper_id}.jsonl`; only sanctioned extraction finalizers may materialize JSONL.
- `.claude/hooks/ensure_extraction_finalized.py`: ensures extractor agents finish with successful sanctioned finalization or a legitimate iter-n no-op.
- `scripts/extraction/write_extraction.py`, `scripts/extraction/extraction_builder.py`, and `scripts/extraction/run_extraction_build.py`: sanctioned materialization paths that validate provenance, table anchors, derivations, certainty, and reasoning.
