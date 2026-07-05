# Comparanda Agent Index

This is the operator and migration map for the Comparanda inner-loop agents.
`CLAUDE.md` remains the authoritative phase procedure; this file is the concise
inventory of agent boundaries, file contracts, script dependencies, and resume
behavior. It is intended to help both humans and workflow-engine migrations
such as Temporal identify durable workflow state and activity boundaries.

## Workflow Shape

| Phase | Agent | Cardinality | Temporal-style boundary | Resume behavior |
|---|---|---:|---|---|
| 0 | `survey`, `schema-proposer`, `extractor`, `normalizer`, `ma-critique` in reference-generation mode | one per reference file | domain-reference generation activities | Not normally resumed; output files are small and can be regenerated if needed. |
| 0.5 | `schema-proposer` in survey-schema mode | one per domain | survey-schema activity | Not normally resumed. |
| 1 | `survey` | one per paper | independent paper-survey activity | Not normally resumed; survey JSON is the checkpoint. |
| 1 / 5 | `subset-selector` | one per subset decision | subset-selection activity | Not normally resumed; selection observation is the checkpoint. |
| 2 / 4 | `schema-proposer` | one per schema version | schema-version activity | Not normally resumed; previous schema, critique, and observation log are the checkpoints. |
| 3a | `extractor` | one per paper | long-running paper-extraction activity | Must be resumable. The orchestrator records session metadata in `projects/<domain>/extractions/.staging/extractor_sessions.json` and resumes the original extractor for QC repair and schema-iteration patches before launching a replacement. |
| 3a-QC | `extraction-qc` | one per paper QC pass | verification activity | Not currently resumed as a session. Re-QC is a fresh targeted activity after extractor repair. |
| 3b | `normalizer` | one per loop, project-wide | project normalization activity | May be resumed in future runs, but current contract relies on deterministic reconciliation hints and normalization files as checkpoints. |
| 3.5 | `ma-critique` | one per loop | convergence-assessment activity | Not normally resumed; critique JSON and observation entries are checkpoints. |

## Shared Project State

| Path | Owner / writers | Readers |
|---|---|---|
| `projects/<domain>/schema/survey_schema.json` | `schema-proposer` in survey-schema mode | `survey` |
| `projects/<domain>/surveys/{paper_id}.json` | `survey` | `schema-proposer`, `subset-selector`, `extraction-qc` |
| `projects/<domain>/schema/v*.yaml` | `schema-proposer` | all downstream agents |
| `projects/<domain>/extractions/{paper_id}.jsonl` | `extractor` through sanctioned extraction scripts; `normalizer` through normalization API apply | `extraction-qc`, `normalizer`, `ma-critique`, harness eval |
| `projects/<domain>/extractions/{paper_id}_qc.json` | `extraction-qc` | orchestrator, resumed `extractor`, harness diagnostician |
| `projects/<domain>/extractions/.staging/{paper_id}/` | `extractor` | resumed `extractor`, QC repair flow, debugging |
| `projects/<domain>/extractions/.staging/extractor_sessions.json` | orchestrator via `extractor_session_registry.py` | orchestrator QC/schema repair flow |
| `projects/<domain>/normalization/{table}.json` | `normalizer` through normalization API | `normalizer`, `ma-critique`, harness eval |
| `projects/<domain>/normalization/reconciliation_candidates_iter_{N}.json` | `normalizer` via reconciliation helper | `normalizer` |
| `projects/<domain>/critiques/iter_{NNN}.json` | `ma-critique` | orchestrator, `schema-proposer`, harness diagnostician |
| `projects/<domain>/observations/log.jsonl` | all agents through `observation_log.py` | all agents |

## Agents

### `survey`

| Item | Contract |
|---|---|
| Scope | Breadth-first paper inventory. Catalogs what extractable data exists; does not extract, normalize, or interpret. |
| Model | `haiku` |
| Inputs | paper text or bundle path, `projects/<domain>/schema/survey_schema.json`, `projects/<domain>/domain/survey_ref.md` |
| Outputs | `projects/<domain>/surveys/{paper_id}.json`; one `paper_flag` observation |
| Scripts | `scripts/observation_log.py append` |
| Resume / retry | Fresh rerun is acceptable. Survey output is the durable checkpoint. |

### `subset-selector`

| Item | Contract |
|---|---|
| Scope | Selects 3-4 diverse papers, preserving an anchor where appropriate, to stress-test schema generalization. |
| Model | `claude-sonnet-5[1m]` |
| Inputs | all survey JSON files, observation log subset history, current iteration |
| Outputs | subset selection returned to orchestrator; `subset_selection` observation |
| Scripts | `scripts/observation_log.py append` |
| Resume / retry | Fresh rerun is acceptable. Prior subset choices are recovered from observation log. |

### `schema-proposer`

| Item | Contract |
|---|---|
| Scope | Owns schema creation/refinement. Synthesizes surveys, domain references, observation history, and MA Critique recommendations into rich YAML schema versions. |
| Model | `claude-sonnet-5[1m]` |
| Inputs | `projects/<domain>/surveys/*.json`, `projects/<domain>/domain/schema_ref.md`, observation log, prior schema, latest critique for later iterations |
| Outputs | `projects/<domain>/schema/v001.yaml` or next `v*.yaml`; `schema_rationale` observations; in reference mode `projects/<domain>/domain/schema_ref.md` |
| Scripts | `scripts/observation_log.py append` |
| Resume / retry | Fresh rerun is acceptable if needed. Schema versions are durable checkpoints. |

### `extractor`

| Item | Contract |
|---|---|
| Scope | Per-paper workhorse. Reads a paper-prep bundle and schema, writes atomic rows with provenance, certainty, derivation labels, table cell anchors, and extraction meta-commentary. Does not normalize. |
| Model | `claude-sonnet-5[1m]` |
| Inputs | `projects/<domain>/data/processed/{paper_id}/`, current `schema/v*.yaml`, `domain/extraction_ref.md`, filtered observation-log query, optional prior extraction, optional QC repair packet, optional existing staging script |
| Outputs | `projects/<domain>/extractions/{paper_id}.jsonl`; staging files under `projects/<domain>/extractions/.staging/{paper_id}/`; extraction difficulty observations; in reference mode `domain/extraction_ref.md` |
| Scripts | `scripts/observation_log.py query/append`; `scripts/extraction/run_extraction_build.py`; `scripts/extraction/extraction_builder.py`; `scripts/extraction/write_extraction.py`; SDK modules under `scripts/extraction/` |
| Hooks | `validate_extraction_write.py` blocks direct JSONL writes; `ensure_extraction_finalized.py` enforces finalization on subagent stop |
| Resume / retry | Primary resumable agent. Orchestrator records extractor session/subagent ID, schema version, staging script path, and output path in `extractions/.staging/extractor_sessions.json` via `scripts/extraction/extractor_session_registry.py upsert`. QC repair and schema-iteration re-extraction must first resume this original extractor and ask it to patch its staging script. Launch a replacement only after documented resume retries fail. |

### `extraction-qc`

| Item | Contract |
|---|---|
| Scope | Per-paper verifier between extraction and normalization. Checks survey coverage, schema compliance, internal consistency, and selection-error/table-cell risks. Does not extract data itself. |
| Model | `claude-sonnet-5[1m]` |
| Inputs | `extractions/{paper_id}.jsonl`, `surveys/{paper_id}.json`, current schema, targeted source paper reads, observation log, optional staging manifest |
| Outputs | `projects/<domain>/extractions/{paper_id}_qc.json` with `action_class`, `must_fix_before_normalization`, and compact `repair_packet`; QC observation |
| Scripts | `scripts/observation_log.py append`; may inspect extraction/staging files with shell tools |
| Resume / retry | Re-QC is currently a fresh targeted invocation after extractor repair. The repair packet is designed for resuming the extractor, not for QC to repair rows itself. |

### `normalizer`

| Item | Contract |
|---|---|
| Scope | Project-wide normalization pass. Discovers normalizable fields from schema annotations, maps raw values to canonical IDs, reconciles stale mappings after schema/guidance changes, and applies mappings to extraction rows through the API. |
| Model | `claude-sonnet-5[1m]` |
| Inputs | `extractions/*.jsonl`, current schema, existing `normalization/*.json`, `domain/normalization_ref.md`, observation log, reconciliation candidates |
| Outputs | updated `projects/<domain>/normalization/{table}.json`; applied canonical fields in extraction JSONL through API; normalization observations; in reference mode `domain/normalization_ref.md` |
| Scripts | `scripts/normalization/normalization_reconcile.py`; `scripts/normalization/normalization_api.py reconcile/apply/validate`; `scripts/observation_log.py append` |
| Resume / retry | Current durable state is file-based: normalization tables, reconciliation candidates, and API audit output. Future workflow engines may resume the same normalizer session, but correctness must not depend on hidden session memory. |

### `ma-critique`

| Item | Contract |
|---|---|
| Scope | Meta-analysis readiness judge and convergence signal. Determines whether current schema/extractions are sufficient for pooling and originates schema-change recommendations. |
| Model | `claude-sonnet-5[1m]` |
| Inputs | normalized `extractions/*.jsonl`, current schema, `domain/ma_critique_ref.md`, `domain/deep_research.md`, full observation log including prior critiques |
| Outputs | `projects/<domain>/critiques/iter_{NNN}.json`; structured `recommended_action`; critique observations; optional update to `domain/ma_critique_ref.md` |
| Scripts | `scripts/observation_log.py append` |
| Resume / retry | Fresh rerun is acceptable. Critique JSON is the durable handoff to orchestrator and schema-proposer. |

## Mechanical Guardrails

| Guardrail | File | Purpose |
|---|---|---|
| Direct extraction write block | `.claude/hooks/validate_extraction_write.py` | Prevents direct writes to `extractions/{paper_id}.jsonl`; only sanctioned extraction finalizers may materialize JSONL. |
| Extraction finalization stop check | `.claude/hooks/ensure_extraction_finalized.py` | Ensures extractor agents finish with successful sanctioned finalization or legitimate iter-n no-op. |
| Script index | `scripts/README.md` | Human navigation map for extraction, normalization, review, observation-log, and paper-prep utilities. |
| Extraction SDK contract | `docs/extraction-sdk-agent-contract.md` | Stable extractor-facing API contract; agents should read this instead of implementation internals. |
| Normalization API contract | `docs/normalization-api-agent-contract.md` | Stable normalizer-facing API contract; agents should read this instead of implementation internals. |

## Workflow-Engine Migration Notes

- Treat each agent invocation as an activity with file paths as inputs and
  explicit artifact paths as outputs. Do not pass interpreted paper content
  through orchestrator prompts.
- Persist extractor session metadata as workflow state. Extractor resume is part
  of the correctness and token-efficiency contract.
- Treat schema versions, QC reports, critiques, and normalization tables as
  durable event payloads/checkpoints.
- Preserve the rule that QC diagnoses and the extractor repairs; QC does not
  mutate extraction rows.
- Preserve sanctioned write APIs as activities or local activities rather than
  allowing direct file mutation from model output.
