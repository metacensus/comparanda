# Comparanda Calibration Harness

System 2 for [Comparanda](../comparanda/) — improves Comparanda's agent instructions by evaluating extractions against a gold standard and applying fixes that generalize across domains.

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
├── src/harness/eval/
│   ├── aggregate.py          Row coverage, identity-match coverage, fill rates, composite score
│   ├── judge.py              Finding accuracy (deterministic + optional LLM matching)
│   ├── gold_standard.py      Gold standard loader
│   └── trajectory.py         Iteration tracking, convergence detection
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
# Aggregate metrics + deterministic judge (recommended):
uv run python -m harness.eval.aggregate \
  --extractions-dir ../comparanda/projects/<domain>/extractions \
  --gold-standard "benchmark/gold_standard/<gold-standard-file>.xlsx" \
  --document-id-map benchmark/eval_config/document_id_map.json \
  --with-judge \
  --output iterations/iter_N/eval_report.json

# With LLM semantic matching (costs API, improves unmatched row pairing):
# Add --with-judge-llm --judge-cwd /path/to/comparanda-harness
```

Composite score is emitted by the eval pipeline from row/field coverage, finding accuracy, claim faithfulness, and fill-rate surfaces.

## Guard Hooks

A PreToolUse hook (`.claude/hooks/harness-guardrail-comparanda-writes.sh`) prevents System 2 from modifying any comparanda files except the universal instruction sections of `../comparanda/.claude/agents/*.md`.
