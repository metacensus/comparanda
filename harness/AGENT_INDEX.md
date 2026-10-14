# Harness Agent Index

This is the operator reference for the Comparanda calibration harness
(System 2). `CLAUDE.md` remains the authoritative operating manual; this file
summarizes each harness agent's scope, file contracts, script dependencies, and
the resume behavior currently encoded in the orchestrator and agent
instructions.

## Evaluation Scope

The current harness agents calibrate Comparanda's evidence substrate. Their implemented metrics primarily cover row/field recovery, claim faithfulness, semantic field coverage, and fill-rate behavior. A good score means the configured source evidence was represented faithfully under the benchmark mappings; it does not mean the system selected a correct meta-analysis question, inclusion set, comparator, partition, or statistical model.

Use the following Phase 1 claim layers when interpreting or extending the harness:

| Layer | Question | Current status |
|---|---|---|
| Recovery / faithfulness | Were source-supported fields and findings recovered with adequate provenance? | Implemented by aggregate, claim-faithfulness, and detailed field-coverage workflows |
| Coverage | Is each expected source-backed fact fully, partially, or not represented? | Existing semantic field coverage is reusable; gold must be normalized and expanded |
| Reproducibility | Can a fact or entity-bound record be faithfully reconstructed at the required scope, associations, uncertainty, and provenance? | Extends current full-representation reasoning; entity-aware rules remain a design checkpoint |
| Failure attribution | Why is an item partial or missing? | Add a required diagnostic reason; schema limitation is not a separate score |

Final study inclusion, arm construction, likelihood selection, model fitting, and interpretation are downstream analytical responsibilities. A diagnostician should not classify failure to reproduce one analyst's cross-paper conclusion or preferred action as an instruction defect.

Comparanda output should inform four downstream tasks without performing them:
research-question exploration and selection; analysis-specific inclusion and
exclusion; selection of a suitable statistical model; and construction of the
inputs needed to execute that analysis.

Cross-paper conclusions and analyst actions may appear in Phase 1 fixtures only as contextual, unscored gold. Phase 1 does not require the harness to score relationship inference or issue surfacing. Native cross-paper inference is considered only later if a downstream-use experiment shows that granular paper outputs are insufficient.

## Phase Map

| Phase | Agent / Actor | What Happens | Cardinality | Resume Policy |
|---|---|---|---:|---|
| 1 | harness orchestrator launching Comparanda | Starts a full Comparanda inner-loop run from `../comparanda/` and monitors it as a long-running process. | one background run per calibration attempt | Harness does not directly resume Comparanda subagents; Comparanda owns its own extractor resume policy. |
| 2 | `monitor` | Reads Comparanda project state and reports whether to wait, investigate, or evaluate. | one or more per run | Fresh invocation; read-only and cheap. |
| 3a | `eval` aggregate mode | Computes fast deterministic completeness, fill-rate, normalization, and claim-faithfulness surfaces. | one per completed iteration | Fresh invocation; deterministic outputs are reproducible. |
| 3b | `eval` detailed mode | Performs semantic field-level coverage adjudication against gold for selected papers. | one per paper or batch | Resumed in outer-loop mode when `field_coverage_eval_sessions.json` records a prior eval session. One-shot mode may start fresh. |
| 4 | `diagnostician` | Traces eval failures to root causes and decides whether System 2 should change instructions. | one per eval cycle | Fresh invocation; diagnosis JSON is the checkpoint. |
| 5 | `critic` | Proposes exactly one domain-independent instruction change, or a no-change proposal. | one per accepted diagnosis | Fresh invocation; proposal JSON is the handoff. |
| 6-8 | harness orchestrator | Applies, tests, keeps, or reverts accepted instruction changes and updates trajectory. | one per calibration iteration | Git state, tests, trajectory, and proposal artifacts are checkpoints. |

## Shared Harness State

| Path | Owner / Writers | Readers |
|---|---|---|
| `<iterations-dir>/trajectory.json` | orchestrator, eval/trajectory scripts | all harness agents |
| `<iterations-dir>/accuracy_coverage.json` | eval agent / eval scripts | eval, diagnostician, orchestrator |
| `<iterations-dir>/iter_{N}/eval_report.json` | eval aggregate mode | diagnostician, orchestrator |
| `<iterations-dir>/iter_{N}/claim_faithfulness.json` | eval aggregate mode via script | eval, diagnostician, portal/export consumers |
| `<iterations-dir>/iter_{N}/comparisons/{paper_id}.json` | eval detailed setup | eval detailed workspace |
| `<iterations-dir>/iter_{N}/field_coverage/{paper_id}/` | eval detailed mode | eval validation, orchestrator, diagnostician |
| `<iterations-dir>/iter_{N}/field_coverage_eval_sessions.json` | eval detailed outer-loop mode | orchestrator, resumed eval agent |
| `<iterations-dir>/iter_{N}/diagnoses/{slug}.json` | diagnostician | critic, orchestrator |
| `<iterations-dir>/iter_{N}/proposals/{slug}.json` | critic | orchestrator |
| `<gold-standard-dir>/` | application/domain fork | eval, diagnostician |
| `<eval-config-dir>/` | application/domain fork | eval, diagnostician |
| `../comparanda/.claude/agents/*.md` | critic proposal, orchestrator applies approved changes | all harness agents |

## Agents

### `monitor`

Scope: cheap read-only status scanner for Comparanda project state. Determines whether to wait, investigate, or evaluate.

Model: `haiku`

Inputs:
- `<project-root>/observations/log.jsonl`
- `<project-root>/extractions/*.jsonl`
- `<project-root>/schema/v*.yaml`
- `<project-root>/critiques/iter_*.json`
- recent Comparanda git log

Outputs:
- JSON status object to orchestrator
- current phase
- row counts
- schema version
- critique status
- anomalies and recommendation

Scripts:
- shell/grep/glob for direct checks
- optional live monitor: `uv run python -m harness.monitor`

Resume policy:
- Current orchestrator behavior is fresh invocation.
- No session state is needed because the agent is read-only and cheap.

### `eval`

Scope: measurement layer. Runs aggregate completeness/faithfulness surfaces and detailed semantic field-level coverage against gold. Produces machine-readable artifacts with audit rationale.

Claim boundary: this agent currently evaluates configured evidence-recovery
targets. Phase 1 will translate gold into normalized entity-aware records,
preserve a reproducible legacy view, add domain-expert prerequisite facts, and extend
failure attribution. The artifact and runner contract are not yet chosen. It
does not score cross-paper overlap conclusions, analyst
actions, the uniquely correct downstream synthesis, or a statistical model.
Gold-absent papers must be distinguished from source-present extraction
failures.

Model: `opus` for strongest adjudication; deterministic scripts run locally.

Inputs:
- `<project-root>/extractions/`
- `<project-root>/schema/v*.yaml`
- `<project-root>/normalization/`
- `<project-root>/observations/log.jsonl`
- `<project-root>/domain/`
- `<gold-standard-dir>/*`
- `<eval-config-dir>/document_id_map.json`
- optional `<eval-config-dir>/gold_config.json`
- iteration id
- mode: `aggregate`, `detailed`, `outer-loop`, or `one-shot` as directed by orchestrator
- paper list for detailed runs

Outputs:
- `<iterations-dir>/iter_{N}/eval_report.json`
- `<iterations-dir>/iter_{N}/claim_faithfulness.json`
- `<iterations-dir>/iter_{N}/comparisons/{paper_id}.json`
- `<iterations-dir>/iter_{N}/field_coverage/{paper_id}/output/field_coverage.json`
- `<iterations-dir>/iter_{N}/field_coverage/{paper_id}/output/field_coverage_validation.json`
- optional `<iterations-dir>/iter_{N}/field_coverage/{paper_id}/output/field_coverage_delta.json`
- updated `<iterations-dir>/accuracy_coverage.json`
- portal-compatible artifacts when invoked for export-style runs: `score.json`, `finding_scores.json`, `gold_adjudications.json`, `bundle_status.json`, `eval_explainer.md`

Scripts:
- `harness.eval.aggregate`
- `harness.eval.claim_faithfulness`
- `harness.eval.prepare_comparison`
- `harness.eval.field_coverage_workspace`
- `harness.eval.field_checklist`
- `harness.eval.field_coverage`
- `harness.eval.finding_adjudication`
- `harness.eval.semantic_field_overlap`
- `harness.eval.semantic_value_gate`
- `harness.eval.score`
- `harness.eval.composite`
- `harness.eval.render_eval`
- `harness.eval.value_diff`
- `harness.eval.gold_config`
- `harness.eval.batch_eval`
- `harness.eval.workbench`

Resume policy:
- Outer-loop detailed eval should resume the prior eval session when `<iterations-dir>/iter_{N}/field_coverage_eval_sessions.json` has a session for that paper/run mode.
- If session resume is unavailable, the eval workspace still provides checklist/progress/output artifacts for deterministic continuation.
- One-shot mode may use a fresh isolated workspace.
- Aggregate mode is deterministic and rerun fresh.

### `diagnostician`

Scope: explains why eval failed and classifies whether the failure is instruction-fixable, already handled by System 1, domain-specific, or needs more investigation. Does not edit instructions.

The diagnosis must separate:

- corpus/source coverage gaps;
- source-present recovery failures;
- representation or schema limitations;
- provenance or uncertainty failures;
- gold ambiguity;
- unsupported extraction; and
- downstream analytical choices that are not Comparanda extraction requirements.

Only demonstrated paper-level coverage, reproducibility, or grounding failures
can justify a Comparanda instruction change. The locked failure attribution
must distinguish schema limitation from extraction miss without turning it into
a parallel metric. A corpus/source coverage gap normally calls for an input or
coverage-status fix rather than an extraction-prompt change; a cross-paper
inference or analyst-action mismatch does not justify a Phase 1 instruction
change.

Model: `sonnet`

Inputs:
- `../docs/specs/system2-lessons.md`
- `<iterations-dir>/iter_{N}/eval_report.json`
- detailed eval artifacts under `<iterations-dir>/iter_{N}/field_coverage/`
- `../comparanda/.claude/agents/*.md`
- `<project-root>/observations/log.jsonl`
- `<project-root>/schema/v*.yaml`
- `<gold-standard-dir>/`
- latest `<project-root>/critiques/iter_*.json`
- `<project-root>/surveys/*.json`
- normalization tables when relevant

Outputs:
- `<iterations-dir>/iter_{N}/diagnoses/{slug}.json`
- structured recommendation: `invoke_critic`, `run_inner_loop`, `no_instruction_fix_needed`, or `investigate_further`

Scripts:
- shell/grep/glob for artifact inspection
- eval helper outputs as read-only inputs

Resume policy:
- Current orchestrator behavior is fresh invocation.
- Diagnosis JSON is the durable checkpoint.

### `critic`

Scope: proposes exactly one domain-independent instruction change for one Comparanda agent file, or a no-change proposal. Conservative bridge from diagnosis to implementation.

The critic must not encode one domain's preferred field name, cross-paper conclusion, comparator, inclusion decision, partition, or model as universal extraction methodology. Phase 1 changes should improve general coverage or reproducibility of per-paper prerequisite facts, uncertainty, and provenance across domains. Direct domain-schema edits are not harness instruction fixes. Any proposed universal abstraction or SDK change is a collaborative design gate.

Model: `sonnet`

Inputs:
- one `<iterations-dir>/iter_{N}/diagnoses/{slug}.json`
- target file under `../comparanda/.claude/agents/`
- `<iterations-dir>/trajectory.json`
- `../docs/specs/system2-lessons.md`
- `../docs/research/prompt-engineering-reference.md`

Outputs:
- `<iterations-dir>/iter_{N}/proposals/{slug}.json`
- exact `current_text` / `proposed_text` or insertion point
- rationale, expected impact, instruction-size delta, and overfitting-test result

Scripts:
- none required beyond file reads/writes; orchestrator applies or rejects the proposal

Resume policy:
- Current orchestrator behavior is fresh invocation.
- Proposal JSON is the durable handoff.

## Guardrails

- `.claude/hooks/harness-guardrail-comparanda-writes.sh`: prevents harness-side agents from writing arbitrary Comparanda files. The harness may only modify approved universal instruction/orchestrator files in `../comparanda/`.
