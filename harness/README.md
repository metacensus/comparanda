# Comparanda Calibration Harness

System 2 for [Comparanda](../comparanda/) — improves Comparanda's agent instructions by evaluating extractions against a gold standard and applying fixes that generalize across domains.

For the current harness agent scopes, file contracts, eval artifact paths,
script dependencies, and eval resume behavior, see
[`AGENT_INDEX.md`](./AGENT_INDEX.md). This README is a high-level overview.

## Evaluation Claim Boundary

The implemented harness primarily evaluates evidence recovery and faithfulness: whether source-supported fields and findings are represented in Comparanda output. Its aggregate and detailed field-coverage artifacts do **not** establish that Comparanda chose the correct synthesis question, inclusion set, comparator, arm partition, likelihood, or statistical model.

Evaluation should distinguish three claim layers:

1. **Recovery and faithfulness** — current aggregate, claim-faithfulness, and semantic field-coverage surfaces.
2. **Representation and relationships** — whether material identity, dependence, timing, boundedness, and unknown states can be expressed without lossy collapse.
3. **Surfacing and downstream usability** — whether consequential issues are foregrounded with traceable evidence and sufficient context for analyst review.

Layers 2 and 3 require explicit benchmark fixtures and outputs; they must not be inferred from a strong field micro-average. They should be reported alongside the current benchmark rather than silently changing the meaning of its historical scores. Final synthesis decisions and model fitting remain downstream analysis responsibilities unless a future benchmark and system component explicitly scope them in.

The downstream-usability layer should test whether Comparanda output is sufficient to **inform**, but not perform: (1) research-question exploration and selection, (2) inclusion/exclusion determination, (3) suitable statistical-model determination, and (4) construction of all inputs needed to run the selected model. It should score missing or unsafe evidence constraints, not exact agreement with one analyst's ultimate choices.

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

Composite score is emitted by the eval pipeline from row/field coverage, finding accuracy, claim faithfulness, and fill-rate surfaces.

That composite score is an evidence-recovery calibration signal. It should not be labeled synthesis accuracy or decision accuracy. Corpus-absent, inaccessible, or unmatched papers should be reported as coverage status rather than scored as if the extractor had read them and failed.

Future relationship/surfacing evaluation should use open-ended evidence-issue probes with hidden, source-anchored constraints. It should score issue recognition, provenance, uncertainty fidelity, and downstream input sufficiency without requiring exact agreement with one analyst's final model choice.

## Guard Hooks

A PreToolUse hook (`.claude/hooks/harness-guardrail-comparanda-writes.sh`) prevents System 2 from modifying any comparanda files except the universal instruction sections of `../comparanda/.claude/agents/*.md`.
