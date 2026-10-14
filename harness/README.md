# Comparanda Calibration Harness

System 2 for [Comparanda](../comparanda/) — improves Comparanda's agent instructions by evaluating extractions against a gold standard and applying fixes that generalize across domains.

For the current harness agent scopes, file contracts, eval artifact paths,
script dependencies, and eval resume behavior, see
[`AGENT_INDEX.md`](./AGENT_INDEX.md). This README is a high-level overview.

## Evaluation Claim Boundary

The implemented harness primarily evaluates evidence recovery and faithfulness: whether source-supported fields and findings are represented in Comparanda output. Its aggregate and detailed field-coverage artifacts do **not** establish that Comparanda chose the correct synthesis question, inclusion set, comparator, arm partition, likelihood, or statistical model.

Phase 1 evolves the current benchmark rather than creating a parallel
evaluator. Original workbooks remain immutable indexed sources. Their curated
data are translated into normalized, entity-aware gold. Each benchmark slice
records its contributing workbook and benchmark version. Sheet-, field-, or
cell-level workbook lineage is optional when useful. Scored gold remains
grounded in source-paper evidence, and a legacy view must reproduce the
historical result before the gold is expanded.

Its load-bearing metrics are:

1. **Coverage** — whether each expected source-backed fact is fully, partially,
   or not represented.
2. **Reproducibility** — whether the fact or entity-bound record can be
   faithfully reconstructed at the required meaning, granularity, scope,
   associations, uncertainty, and provenance.

Reproducibility here extends current full-representation reasoning; it does not
mean run-to-run model stability. Current `field_coverage` remains reusable
semantic adjudication machinery and is not itself a headline composite input.

Partial and missing adjudications require one primary failure reason:
`source_absent`, `gold_ambiguous`, `extraction_miss`, `representation_loss`,
`schema_limitation`, or `unsupported_extraction`. Schema limitation is a
diagnostic cause rather than another scored surface.

Cross-paper overlap conclusions and analyst actions may be stored as
explanatory benchmark metadata showing why per-paper facts matter. They are
unscored and not expected from Comparanda. Phase 1 does not require a
relationship graph, issue-surfacing benchmark, or native cross-paper inference.

Comparanda output should inform, but not perform, four downstream tasks: (1)
explore and select research questions, (2) determine analysis-specific
inclusion and exclusion, (3) select a suitable statistical model, and (4)
construct the inputs needed to execute that analysis.

## Domain Scope

This harness is domain-agnostic methodology. A single-domain superproject
provides benchmark data, evaluation configuration, run state, and project data
through explicit paths. Legacy per-domain forks remain supported.

| Component | Domain-specific | Reusable |
|-----------|:-:|:-:|
| Eval scripts (`src/harness/eval/`) | | x |
| Gold standard data (`benchmark/`) | x | |
| Judge field mappings (gold column -> extraction field) | x | |
| Entity normalization crosswalks | x | |
| Agent definitions (`.claude/agents/`) | | x |
| Orchestration logic (`CLAUDE.md`) | | x |
| Trajectory tracking | | x |
| Overfitting test / simplicity principle | | x |

To apply Comparanda to a domain: keep the harness architecture and agents, and provide benchmark data, document-id mappings, field mappings, and optional entity crosswalks from the application fork. The universal agent instructions produced by calibration carry over.

## Architecture

```
harness/                      (System 2)
├── .claude/
│   ├── agents/               monitor, eval, diagnostician, critic
│   └── settings.json         permissions + guard hooks
├── benchmark/                optional per-domain files in application forks
│   ├── gold_standard/        gold-standard spreadsheets
│   └── eval_config/          document_id_map.json, gold_config.json, ...
├── iterations/               legacy in-repo run state
├── AGENT_INDEX.md            agent scopes, file contracts, scripts, resume behavior
├── src/harness/eval/
│   ├── aggregate.py          Fast deterministic completeness/fill-rate surfaces
│   ├── claim_faithfulness.py Extracted-claim precision/faithfulness checks
│   ├── field_coverage*.py    Semantic field-level coverage workspaces/artifacts
│   ├── score.py              Score/finding-score assembly
│   ├── render_eval.py        Human-readable eval summaries
│   └── batch_eval.py         Batched eval planning/merge helpers
└── CLAUDE.md                 Orchestrator operating manual

../comparanda/                (System 1 engine — under calibration)
├── .claude/agents/           Agent definitions that System 2 modifies
└── CLAUDE.md                 System 1 orchestrator

<project-root>/               external single-domain superproject
├── schema/, extractions/, observations/, ...
├── benchmark/                domain eval config + gold standard
└── iterations/               domain run state
```

Resolve `<project-root>`, `<iterations-dir>`, `<eval-config-dir>`, and
`<gold-standard-dir>` as described in `CLAUDE.md`; do not infer application
paths from the engine checkout.

## The Improvement Loop

1. Run Comparanda (System 1 inner loop)
2. Evaluate extractions against gold standard
3. Diagnose instruction-level root causes
4. Propose one fix (must pass overfitting test)
5. Apply fix, re-run, measure improvement
6. Keep or revert based on composite score
7. Repeat until converged

## Eval

```bash
# Aggregate metrics + deterministic eval surfaces (recommended):
uv run python -m harness.eval.aggregate \
  --extractions-dir <project-root>/extractions \
  --gold-standard "<gold-standard-dir>/<gold-standard-file>.xlsx" \
  --document-id-map <eval-config-dir>/document_id_map.json \
  --output <iterations-dir>/iter_N/eval_report.json

# Field-level semantic coverage and one-shot/outer-loop eval are owned by the
# eval agent. See harness/CLAUDE.md and AGENT_INDEX.md for the current prompts,
# artifacts, and resume policy.
```

The historical composite score is emitted from aggregate row/field, finding-accuracy, claim-faithfulness, and fill-rate surfaces. Detailed semantic `field_coverage` is a supporting adjudication artifact, not a headline composite input.

That composite score is an evidence-recovery calibration signal. It should not be labeled synthesis accuracy or decision accuracy. Corpus-absent, inaccessible, or unmatched papers should be reported as coverage status rather than scored as if the extractor had read them and failed.

The normalized legacy view must preserve the historical result before the domain-expert
prerequisite slice is added. The exact normalized tables, identity keys,
grouped-completeness rules, weighting, report presentation, and any future
effect on convergence are collaborative design gates; they should not be
predeclared in this architecture overview.

Native cross-paper inference is a conditional later capability. Consider it only if a downstream-use experiment shows that high-quality granular paper outputs are consistently insufficient; do not predeclare relationship or issue-surfacing metrics as current Comparanda requirements.

## Guard Hooks

A PreToolUse hook (`.claude/hooks/harness-guardrail-comparanda-writes.sh`)
restricts engine writes to universal agent/orchestrator instructions, treats
external domain state as read-only, and permits writes only under the configured
iterations directory.
