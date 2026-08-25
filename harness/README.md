# Comparanda Calibration Harness

System 2 for [Comparanda](../comparanda/) — improves Comparanda's agent instructions by evaluating extractions against a gold standard and applying fixes that generalize across domains.

For the current harness agent scopes, file contracts, eval artifact paths,
script dependencies, and eval resume behavior, see
[`AGENT_INDEX.md`](./AGENT_INDEX.md). This README is a high-level overview.

## Evaluation Claim Boundary

The implemented harness primarily evaluates evidence recovery and faithfulness: whether source-supported fields and findings are represented in Comparanda output. Its aggregate and detailed field-coverage artifacts do **not** establish that Comparanda chose the correct synthesis question, inclusion set, comparator, arm partition, likelihood, or statistical model.

Phase 1 adds two separately reported questions for paper-level prerequisite information:

1. **Schema accommodation** — can the discovered schema represent each required per-paper prerequisite fact at the correct scope and granularity?
2. **Prerequisite-fact recovery** — did extraction recover and ground that fact correctly, particularly when the schema could accommodate it?

These require a separate paper-level fixture and report beside the current field benchmark; they must not be inferred from a strong field micro-average or silently change the meaning of historical scores. The fixture is semantic and schema-agnostic: it defines prerequisite concepts and acceptable meanings, not required DPYD field names.

Current `field_coverage` output can support semantic prerequisite-fact recovery adjudication, but it is not a headline composite input for the Phase 1 report. Schema accommodation and recovery must remain visible as separate results.

Cross-paper overlap conclusions and analyst actions may be stored in the fixture as contextual gold explaining why per-paper facts matter. They are unscored as Comparanda outputs. Phase 1 does not require a relationship graph, issue-surfacing benchmark, or native cross-paper inference.

Comparanda output should inform, but not perform, four downstream tasks: (1)
explore and select research questions, (2) determine analysis-specific
inclusion and exclusion, (3) select a suitable statistical model, and (4)
construct the inputs needed to execute that analysis.

## Domain Scope

This harness is domain-agnostic methodology. Per-domain forks provide benchmark data and eval config, either under `benchmark/` or through explicit CLI paths.

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
comparanda-harness/           (System 2)
├── .claude/
│   ├── agents/               monitor, eval, diagnostician, critic
│   └── settings.json         permissions + guard hooks
├── benchmark/                optional per-domain files in application forks
│   ├── gold_standard/        gold-standard spreadsheets
│   └── eval_config/          document_id_map.json, gold_config.json, ...
├── iterations/               trajectory.json, per-iteration archives
├── AGENT_INDEX.md            agent scopes, file contracts, scripts, resume behavior
├── src/harness/eval/
│   ├── aggregate.py          Fast deterministic completeness/fill-rate surfaces
│   ├── claim_faithfulness.py Extracted-claim precision/faithfulness checks
│   ├── field_coverage*.py    Semantic field-level coverage workspaces/artifacts
│   ├── score.py              Score/finding-score assembly
│   ├── render_eval.py        Human-readable eval summaries
│   └── batch_eval.py         Batched eval planning/merge helpers
└── CLAUDE.md                 Orchestrator operating manual

../comparanda/                (System 1 — the system under calibration)
├── .claude/agents/           Agent definitions that System 2 modifies
├── projects/<domain>/        Project data (extractions, schema, observations)
└── CLAUDE.md                 System 1 orchestrator
```

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
  --extractions-dir ../comparanda/projects/<domain>/extractions \
  --gold-standard "benchmark/gold_standard/<gold-standard-file>.xlsx" \
  --document-id-map benchmark/eval_config/document_id_map.json \
  --with-judge \
  --output iterations/iter_N/eval_report.json

# Field-level semantic coverage and one-shot/outer-loop eval are owned by the
# eval agent. See harness/CLAUDE.md and AGENT_INDEX.md for the current prompts,
# artifacts, and resume policy.
```

The historical composite score is emitted from aggregate row/field, finding-accuracy, claim-faithfulness, and fill-rate surfaces. Detailed semantic `field_coverage` is a supporting adjudication artifact, not a headline composite input.

That composite score is an evidence-recovery calibration signal. It should not be labeled synthesis accuracy or decision accuracy. Corpus-absent, inaccessible, or unmatched papers should be reported as coverage status rather than scored as if the extractor had read them and failed.

The separate Phase 1 paper-level result should be reported alongside, not
folded into, that historical composite. Its exact fixture, artifact, and runner
contract remain to be chosen after the current evaluation abstractions are
reviewed. It should report schema accommodation, prerequisite-fact recovery,
conditional recovery, provenance/uncertainty correctness, and
schema/extraction/source/gold-ambiguity attribution.

Native cross-paper inference is a conditional later capability. Consider it only if a downstream-use experiment shows that high-quality granular paper outputs are consistently insufficient; do not predeclare relationship or issue-surfacing metrics as current Comparanda requirements.

## Guard Hooks

A PreToolUse hook (`.claude/hooks/harness-guardrail-comparanda-writes.sh`) prevents System 2 from modifying any comparanda files except the universal instruction sections of `../comparanda/.claude/agents/*.md`.
