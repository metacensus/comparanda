# Harness Agent Index

This is the operator and migration map for the Comparanda calibration harness
(System 2). `CLAUDE.md` remains the authoritative operating manual; this file
summarizes each agent's scope, file contracts, script dependencies, and resume
behavior. It is intended to help workflow-engine migrations such as Temporal
separate orchestration state from agent activities.

## Workflow Shape

| Step | Agent | Cardinality | Temporal-style boundary | Resume behavior |
|---|---|---:|---|---|
| 1 | Comparanda inner loop, launched from harness orchestrator | one background run per calibration attempt | child workflow or external workflow invocation | The harness does not resume Comparanda agents directly except by instructing the Comparanda orchestrator. Comparanda owns extractor resume. |
| 2 | `monitor` | one or more per run | read-only status polling activity | Not resumed; cheap read-only scan. |
| 3 | `eval` aggregate mode | one per completed iteration | deterministic eval activity plus claim-faithfulness activity | Not resumed; deterministic outputs are reproducible. |
| 3 | `eval` detailed mode | one per paper or batch | long-running semantic adjudication activity | Must be resumable in outer-loop mode through `iterations/iter_{N}/field_coverage_eval_sessions.json`; writes early workspace checkpoints. |
| 4 | `diagnostician` | one per eval cycle | root-cause diagnosis activity | Not normally resumed; diagnosis JSON is the checkpoint. |
| 5 | `critic` | one per accepted diagnosis | instruction-change proposal activity | Not normally resumed; proposal JSON is the checkpoint. |
| 6-8 | harness orchestrator | one per calibration iteration | apply/verify/keep-or-revert workflow logic | Uses git, trajectory, tests, and guard hooks as durable state. |

## Shared Harness State

| Path | Owner / writers | Readers |
|---|---|---|
| `iterations/trajectory.json` | orchestrator, eval/trajectory scripts | all harness agents |
| `iterations/accuracy_coverage.json` | eval agent / eval scripts | eval, diagnostician, orchestrator |
| `iterations/iter_{N}/eval_report.json` | eval aggregate mode | diagnostician, orchestrator |
| `iterations/iter_{N}/claim_faithfulness.json` | eval aggregate mode via script | eval, diagnostician, portal/export consumers |
| `iterations/iter_{N}/comparisons/{paper_id}.json` | eval detailed setup | eval detailed workspace |
| `iterations/iter_{N}/field_coverage/{paper_id}/` | eval detailed mode | eval validation, orchestrator, diagnostician |
| `iterations/iter_{N}/field_coverage_eval_sessions.json` | eval detailed outer-loop mode | orchestrator, resumed eval agent |
| `iterations/iter_{N}/diagnoses/{slug}.json` | diagnostician | critic, orchestrator |
| `iterations/iter_{N}/proposals/{slug}.json` | critic | orchestrator |
| `benchmark/gold_standard/` and `benchmark/eval_config/` | application/domain fork | eval, diagnostician |
| `../comparanda/.claude/agents/*.md` | critic proposal, orchestrator applies approved changes | all harness agents |

## Agents

### `monitor`

| Item | Contract |
|---|---|
| Scope | Cheap read-only status scanner for Comparanda project state. Determines whether to wait, investigate, or evaluate. |
| Model | `haiku` |
| Inputs | `../comparanda/projects/<domain>/observations/log.jsonl`, `extractions/*.jsonl`, `schema/v*.yaml`, `critiques/iter_*.json`, recent Comparanda git log |
| Outputs | JSON status object to orchestrator with current phase, row counts, schema version, critique status, anomalies, and recommendation |
| Scripts | shell/grep/glob only; optional `uv run python -m harness.monitor` for live monitoring from the orchestrator docs |
| Resume / retry | Never resumed; rerun as needed. It is read-only and fast. |

### `eval`

| Item | Contract |
|---|---|
| Scope | Measurement layer. Runs aggregate completeness/faithfulness surfaces and detailed semantic field-level coverage against gold. Produces machine-readable artifacts with audit rationale. |
| Model | `opus` for strongest adjudication; deterministic scripts may run locally |
| Inputs | `../comparanda/projects/<domain>/extractions/`, current schema, normalization tables, observation log, domain refs, `benchmark/gold_standard/*`, `benchmark/eval_config/document_id_map.json`, optional `gold_config.json`, iteration id, mode, paper list |
| Outputs | aggregate: `iterations/iter_{N}/eval_report.json`, `claim_faithfulness.json`; detailed: `comparisons/{paper_id}.json`, `field_coverage/{paper_id}/output/field_coverage.json`, validation JSON, optional `field_coverage_delta.json`, updated `accuracy_coverage.json`; portal-compatible artifacts such as `score.json`, `finding_scores.json`, `gold_adjudications.json`, `bundle_status.json`, `eval_explainer.md` when invoked for export-style runs |
| Scripts | `harness.eval.aggregate`, `claim_faithfulness`, `prepare_comparison`, `field_coverage_workspace`, `field_checklist`, `field_coverage`, `finding_adjudication`, `semantic_field_overlap`, `semantic_value_gate`, `score`, `composite`, `render_eval`, `value_diff`, `gold_config`, `batch_eval`, `workbench` |
| Resume / retry | Detailed outer-loop eval is resumable. The orchestrator should resume the prior eval agent when `iterations/iter_{N}/field_coverage_eval_sessions.json` has a session for that paper/run mode. If session resume is unavailable, the eval workspace still contains checklist/progress/output artifacts for deterministic continuation. One-shot mode may use a fresh isolated workspace. |

### `diagnostician`

| Item | Contract |
|---|---|
| Scope | Explains why eval failed and classifies whether the failure is instruction-fixable, already handled by System 1, domain-specific, or needs more investigation. Does not edit instructions. |
| Model | `sonnet` |
| Inputs | `../docs/specs/system2-lessons.md`, `iterations/iter_{N}/eval_report.json`, detailed eval artifacts, Comparanda agent files, observation log, current schema, gold standard, latest MA Critique, surveys, normalization tables when relevant |
| Outputs | `iterations/iter_{N}/diagnoses/{slug}.json` with findings, root cause, target agent/section, and recommended next action |
| Scripts | shell/grep/glob for artifact inspection; may read eval helper outputs but does not mutate them |
| Resume / retry | Fresh rerun is acceptable. Diagnosis JSON is the durable checkpoint. |

### `critic`

| Item | Contract |
|---|---|
| Scope | Proposes exactly one domain-independent instruction change for one Comparanda agent file, or a no-change proposal. Conservative bridge from diagnosis to implementation. |
| Model | `sonnet` |
| Inputs | one diagnosis JSON, current target agent file under `../comparanda/.claude/agents/`, `iterations/trajectory.json`, `../docs/specs/system2-lessons.md`, prompt-engineering reference |
| Outputs | `iterations/iter_{N}/proposals/{slug}.json` containing exact current/proposed text, insertion point, rationale, expected impact, size delta, and overfitting-test result |
| Scripts | none required beyond file reads/writes; orchestrator applies or rejects the proposal |
| Resume / retry | Fresh rerun is acceptable. Proposal JSON is the durable handoff. |

## Harness Scripts and Guardrails

| Component | Path | Purpose |
|---|---|---|
| Live monitor | `src/harness/monitor.py` | Watches Comparanda session/project state and emits progress events. |
| Aggregate eval | `src/harness/eval/aggregate.py` | Fast completeness, fill-rate, normalization, and composite surfaces. |
| Detailed eval workspace | `src/harness/eval/field_coverage_workspace.py` | Creates isolated per-paper workspaces, validates output, and tracks resumable eval sessions. |
| Comparison prep | `src/harness/eval/prepare_comparison.py` | Builds gold/extraction comparison packages for semantic adjudication. |
| Field coverage / scoring | `src/harness/eval/field_coverage.py`, `score.py`, `composite.py` | Converts adjudications into benchmark metrics. |
| Semantic helpers | `semantic_field_overlap.py`, `semantic_value_gate.py`, `finding_adjudication.py` | Machine-readable semantic matching, value-gate, and adjudication support. |
| Claim faithfulness | `claim_faithfulness.py` | Audits high-leverage quote-traced extracted claims. |
| Batch eval | `batch_eval.py` | Batches detailed eval work for larger corpora. |
| Guard hook | `.claude/hooks/harness-guardrail-comparanda-writes.sh` | Prevents harness from writing arbitrary Comparanda files; only universal instruction changes are allowed. |

## Workflow-Engine Migration Notes

- Treat the Comparanda inner loop as a child workflow or external workflow. The
  harness launches and observes it; Comparanda owns its internal agent resume.
- Treat aggregate eval as deterministic activities and detailed eval as
  resumable long-running activities keyed by paper, iteration, run mode, and
  workspace path.
- Keep diagnosis and critic outputs as explicit workflow artifacts. Do not let
  the orchestrator encode undocumented rationale only in chat history.
- Preserve the one-change-at-a-time critic boundary. It is a guard against
  overfitting and makes keep/revert decisions interpretable.
- Preserve the harness guard hook or equivalent workflow permission boundary:
  System 2 may propose universal instruction changes, but must not mutate
  domain project data or Comparanda runtime artifacts directly.
