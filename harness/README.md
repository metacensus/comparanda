# Comparanda Calibration Harness

System 2 for [Comparanda](../comparanda/) — improves Comparanda's agent instructions by evaluating extractions against a gold standard and applying fixes that generalize across domains.

## Domain Scope

This harness instance is configured for **DPYD pharmacogenetics**. The calibration logic is domain-agnostic, but the eval infrastructure is tied to the current gold standard.

| Component | Domain-specific | Reusable |
|-----------|:-:|:-:|
| Eval scripts (`src/harness/eval/`) | x | |
| Gold standard data (`benchmark/`) | x | |
| Judge field mappings (gold column -> extraction field) | x | |
| Variant normalization heuristics | x | |
| Agent definitions (`.claude/agents/`) | | x |
| Orchestration logic (`CLAUDE.md`) | | x |
| Trajectory tracking | | x |
| Overfitting test / simplicity principle | | x |

To apply Comparanda to a new domain: keep the harness architecture and agents, replace the benchmark data and eval field mappings. The universal agent instructions produced by calibration carry over.

## Architecture

```
comparanda-harness/           (System 2)
├── .claude/
│   ├── agents/               monitor, eval, diagnostician, critic
│   └── settings.json         permissions + guard hooks
├── benchmark/
│   ├── gold_standard/        gold_standard.xlsx
│   ├── eval_config/          paper_pmid_map.json
│   └── prior_meta_analyses/  Phase 0 reference papers
├── iterations/               trajectory.json, per-iteration archives
├── src/harness/eval/
│   ├── aggregate.py          Row coverage, variant coverage, fill rates, composite score
│   ├── judge.py              Finding accuracy (deterministic + optional LLM matching)
│   ├── gold_standard.py      Gold standard loader
│   └── trajectory.py         Iteration tracking, convergence detection
└── CLAUDE.md                 Orchestrator operating manual

../comparanda/                (System 1 — the system under calibration)
├── .claude/agents/           Agent definitions that System 2 modifies
├── projects/dpyd/            Project data (extractions, schema, observations)
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
  --extractions-dir ../comparanda/projects/dpyd/extractions \
  --gold-standard "benchmark/gold_standard/gold_standard.xlsx" \
  --pmid-map benchmark/eval_config/paper_pmid_map.json \
  --with-judge \
  --output iterations/iter_N/eval_report.json

# With LLM semantic matching (costs API, improves unmatched row pairing):
# Add --with-judge-llm --judge-cwd /path/to/comparanda-harness
```

Composite score: `0.4 * row_coverage + 0.3 * finding_accuracy + 0.2 * variant_coverage + 0.1 * fill_rate`

## Guard Hooks

A PreToolUse hook (`.claude/hooks/guard-comparanda.sh`) prevents System 2 from modifying any comparanda files except the universal instruction sections of `../comparanda/.claude/agents/*.md`.
