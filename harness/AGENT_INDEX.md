# Harness Agent Index

This is the operator reference for the Comparanda calibration harness
(System 2). `CLAUDE.md` remains the authoritative operating manual; this file
summarizes each harness agent's scope, file contracts, script dependencies, and
the resume behavior currently encoded in the orchestrator and agent
instructions.

## Evaluation Scope

The current harness agents calibrate Comparanda's evidence substrate. Their implemented metrics primarily cover row/field recovery, claim faithfulness, semantic field coverage, and fill-rate behavior. A good score means the configured source evidence was represented faithfully under the benchmark mappings; it does not mean the system selected a correct meta-analysis question, inclusion set, comparator, partition, or statistical model.

Use the following claim layers when interpreting or extending the harness:

| Layer | Question | Current status |
|---|---|---|
| Recovery / faithfulness | Were source-supported fields and findings recovered with adequate provenance? | Implemented by aggregate, claim-faithfulness, and detailed field-coverage workflows |
| Representation / relationships | Were identity, dependence, timing, boundedness, and unknown states preserved without material collapse? | Only indirectly observed today; requires explicit fixtures and metrics |
| Surfacing / downstream usability | Did the system foreground consequential issues and provide enough traceable context for analyst review? | Not established by current composite score; requires a separate issue-surfacing evaluation |

Final study inclusion, arm construction, likelihood selection, model fitting, and interpretation are downstream analytical responsibilities. A diagnostician should not classify failure to reproduce one analyst's preferred action as an instruction defect unless an approved benchmark explicitly tests the underlying evidence, relationship, or surfacing requirement.

The surfacing/downstream-usability layer must ask whether Comparanda output can inform four tasks: research-question exploration and selection, inclusion/exclusion determination, suitable-model determination, and construction of all information required to run that model. Comparanda supplies traceable evidence and constraints for these tasks; it does not perform the final selections or execute the analysis.

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
| `iterations/trajectory.json` | orchestrator, eval/trajectory scripts | all harness agents |
| `iterations/accuracy_coverage.json` | eval agent / eval scripts | eval, diagnostician, orchestrator |
| `iterations/iter_{N}/eval_report.json` | eval aggregate mode | diagnostician, orchestrator |
| `iterations/iter_{N}/claim_faithfulness.json` | eval aggregate mode via script | eval, diagnostician, portal/export consumers |
| `iterations/iter_{N}/comparisons/{paper_id}.json` | eval detailed setup | eval detailed workspace |
| `iterations/iter_{N}/field_coverage/{paper_id}/` | eval detailed mode | eval validation, orchestrator, diagnostician |
| `iterations/iter_{N}/field_coverage_eval_sessions.json` | eval detailed outer-loop mode | orchestrator, resumed eval agent |
| `iterations/iter_{N}/diagnoses/{slug}.json` | diagnostician | critic, orchestrator |
| `iterations/iter_{N}/proposals/{slug}.json` | critic | orchestrator |
| `benchmark/gold_standard/` | application/domain fork | eval, diagnostician |
| `benchmark/eval_config/` | application/domain fork | eval, diagnostician |
| `../comparanda/.claude/agents/*.md` | critic proposal, orchestrator applies approved changes | all harness agents |

## Agents

### `monitor`

Scope: cheap read-only status scanner for Comparanda project state. Determines whether to wait, investigate, or evaluate.

Model: `haiku`

Inputs:
- `../comparanda/projects/<domain>/observations/log.jsonl`
- `../comparanda/projects/<domain>/extractions/*.jsonl`
- `../comparanda/projects/<domain>/schema/v*.yaml`
- `../comparanda/projects/<domain>/critiques/iter_*.json`
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

Claim boundary: this agent evaluates configured evidence-recovery targets. It may identify missing relationship or surfacing coverage when an explicit fixture supports that conclusion, but it does not adjudicate the uniquely correct downstream synthesis or model. Gold-absent papers must be distinguished from source-present extraction failures.

Model: `opus` for strongest adjudication; deterministic scripts run locally.

Inputs:
- `../comparanda/projects/<domain>/extractions/`
- `../comparanda/projects/<domain>/schema/v*.yaml`
- `../comparanda/projects/<domain>/normalization/`
- `../comparanda/projects/<domain>/observations/log.jsonl`
- `../comparanda/projects/<domain>/domain/`
- `benchmark/gold_standard/*`
- `benchmark/eval_config/document_id_map.json`
- optional `benchmark/eval_config/gold_config.json`
- iteration id
- mode: `aggregate`, `detailed`, `outer-loop`, or `one-shot` as directed by orchestrator
- paper list for detailed runs

Outputs:
- `iterations/iter_{N}/eval_report.json`
- `iterations/iter_{N}/claim_faithfulness.json`
- `iterations/iter_{N}/comparisons/{paper_id}.json`
- `iterations/iter_{N}/field_coverage/{paper_id}/output/field_coverage.json`
- `iterations/iter_{N}/field_coverage/{paper_id}/output/field_coverage_validation.json`
- optional `iterations/iter_{N}/field_coverage/{paper_id}/output/field_coverage_delta.json`
- updated `iterations/accuracy_coverage.json`
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
- Outer-loop detailed eval should resume the prior eval session when `iterations/iter_{N}/field_coverage_eval_sessions.json` has a session for that paper/run mode.
- If session resume is unavailable, the eval workspace still provides checklist/progress/output artifacts for deterministic continuation.
- One-shot mode may use a fresh isolated workspace.
- Aggregate mode is deterministic and rerun fresh.

### `diagnostician`

Scope: explains why eval failed and classifies whether the failure is instruction-fixable, already handled by System 1, domain-specific, or needs more investigation. Does not edit instructions.

The diagnosis must separate:

- corpus/source coverage gaps;
- source-present recovery failures;
- representation or relationship failures;
- surfacing/provenance failures; and
- downstream analytical choices that are not Comparanda extraction requirements.

Only the first four can justify a Comparanda instruction change, and a corpus/source coverage gap normally calls for an input or coverage-status fix rather than an extraction-prompt change.

Model: `sonnet`

Inputs:
- `../docs/specs/system2-lessons.md`
- `iterations/iter_{N}/eval_report.json`
- detailed eval artifacts under `iterations/iter_{N}/field_coverage/`
- `../comparanda/.claude/agents/*.md`
- `../comparanda/projects/<domain>/observations/log.jsonl`
- `../comparanda/projects/<domain>/schema/v*.yaml`
- `benchmark/gold_standard/`
- latest `../comparanda/projects/<domain>/critiques/iter_*.json`
- `../comparanda/projects/<domain>/surveys/*.json`
- normalization tables when relevant

Outputs:
- `iterations/iter_{N}/diagnoses/{slug}.json`
- structured recommendation: `invoke_critic`, `run_inner_loop`, `no_instruction_fix_needed`, or `investigate_further`

Scripts:
- shell/grep/glob for artifact inspection
- eval helper outputs as read-only inputs

Resume policy:
- Current orchestrator behavior is fresh invocation.
- Diagnosis JSON is the durable checkpoint.

### `critic`

Scope: proposes exactly one domain-independent instruction change for one Comparanda agent file, or a no-change proposal. Conservative bridge from diagnosis to implementation.

The critic must not encode one domain's preferred comparator, inclusion decision, partition, or model as universal extraction methodology. Changes should improve faithful recovery, relationship representation, uncertainty/provenance, or issue surfacing across domains.

Model: `sonnet`

Inputs:
- one `iterations/iter_{N}/diagnoses/{slug}.json`
- target file under `../comparanda/.claude/agents/`
- `iterations/trajectory.json`
- `../docs/specs/system2-lessons.md`
- `../docs/research/prompt-engineering-reference.md`

Outputs:
- `iterations/iter_{N}/proposals/{slug}.json`
- exact `current_text` / `proposed_text` or insertion point
- rationale, expected impact, instruction-size delta, and overfitting-test result

Scripts:
- none required beyond file reads/writes; orchestrator applies or rejects the proposal

Resume policy:
- Current orchestrator behavior is fresh invocation.
- Proposal JSON is the durable handoff.

## Guardrails

- `.claude/hooks/harness-guardrail-comparanda-writes.sh`: prevents harness-side agents from writing arbitrary Comparanda files. The harness may only modify approved universal instruction/orchestrator files in `../comparanda/`.
