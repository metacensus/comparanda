# Comparanda

Agentic system for scientific paper schema extraction and structured data encoding.

## Problem



The bottleneck is the first step: turning a collection of papers into structured, comparable data. Today this is grueling manual work, repeated independently across institutions. Existing tools (Covidence, Elicit, etc.) can help with extraction — but they all assume a schema already exists. Someone still has to decide *what fields to extract* and *how to reconcile terminology* that has evolved across decades of literature. That schema design work is the hard part, and nobody automates it.


## How it works

Two-system architecture.

### System 1 — inner loop (this repo)

Phase-by-phase pipeline that turns paper-prep bundles into extracted atomic rows.
Orchestration lives in [`comparanda/CLAUDE.md`](./comparanda/CLAUDE.md); each
phase delegates to a named agent in
[`comparanda/.claude/agents/`](./comparanda/.claude/agents/).

| Phase | Agent | File | Role |
|---|---|---|---|
| 0 | (manual / agent reference-generation mode) | `comparanda/.claude/agents/*` | One-time domain onboarding: generate per-agent domain reference files. |
| 0.5 | `schema-proposer` | `comparanda/.claude/agents/schema-proposer.md` | Generates the **survey schema** (what to catalog in each paper). |
| 1 | `survey` | `comparanda/.claude/agents/survey.md` | Breadth pass over each paper — first agent to touch each paper. |
| 1.5 | `subset-selector` | `comparanda/.claude/agents/subset-selector.md` | Picks 3–4 diverse papers for each inner-loop iteration. Rotates subsets across iterations. |
| 2 | `schema-proposer` | `comparanda/.claude/agents/schema-proposer.md` | Synthesizes surveys + domain knowledge into a rich YAML extraction schema. The schema IS the product. |
| 3a | `extractor` | `comparanda/.claude/agents/extractor.md` | Workhorse — extracts atomic rows from a paper-prep bundle into JSONL. |
| 3a-qc | `extraction-qc` | `comparanda/.claude/agents/extraction-qc.md` | Checks extractions for completeness vs survey, schema compliance, and internal consistency. |
| 3b | `normalizer` | `comparanda/.claude/agents/normalizer.md` | Two-stage (deterministic + LLM) normalization of raw values to canonical IDs. |
| 3.5 | `ma-critique` | `comparanda/.claude/agents/ma-critique.md` | Decides whether the extracted data is sufficient for meta-analysis; proposes the minimum schema changes to reach convergence. |
| 4 | `schema-proposer` | `comparanda/.claude/agents/schema-proposer.md` | Refines the rich schema based on MA-critique findings. |

Other inner-loop components:
- **Hook:** `comparanda/.claude/hooks/validate_provenance_quotes.py` — PostToolUse validator. Verifies every provenance quote in an extraction JSONL appears verbatim in the paper-prep bundle (sections + tables). Fires on both `Write` and `Bash` paths.
- **Skill:** `comparanda/.claude/skills/comparanda/SKILL.md` — pipeline-knowledge skill surfaced to agents that need cross-phase context.
- **Script:** `comparanda/scripts/observation_log.py` — stdlib CLI invoked by the extractor to query the observation log for entries relevant to a specific paper.
- **Paper-prep preprocessor:** `comparanda/scripts/paper_prep/` — PDF → bundle pipeline (Docling extraction + per-table Sonnet first-pass + Stage-2 LLM arbiter + bundle assembly). Self-contained `uv` workspace. Run **before** the inner loop: `cd comparanda/scripts/paper_prep && uv sync && uv run python -m paper_prep <paper_id> --pdf <path> --output-root <projects/<domain>/data/processed/>`. Batch form: `uv run python -m paper_prep.batch ...`. See [`docs/paper-prep-integration.md`](./docs/paper-prep-integration.md) and Bootstrap Step 4 in [`CLAUDE.md`](./CLAUDE.md).
- **Settings:** `comparanda/.claude/settings.json` — agent-level configuration.

### System 2 — outer loop (`harness/`)

Calibration harness that watches inner-loop runs, evaluates outputs against the
gold standard, traces failures to instruction-level root causes, and proposes
generalizable fixes to the agents in `comparanda/`. Lives in
[`harness/`](./harness/) and is invoked from there.

| Agent | File | Role |
|---|---|---|
| `monitor` | `harness/.claude/agents/monitor.md` | Reads inner-loop state (observation log, phase artifacts) and produces a structured status summary. |
| `eval` | `harness/.claude/agents/eval.md` | Compares Comparanda extractions against the gold standard. Aggregate-mode (completeness) and detailed-mode (semantic accuracy). |
| `diagnostician` | `harness/.claude/agents/diagnostician.md` | Traces eval findings to instruction root causes. Classifies failures as instruction-fixable or domain-specific; identifies which agent file needs changes. |
| `critic` | `harness/.claude/agents/critic.md` | Proposes one specific, domain-independent instruction change per iteration based on the diagnosis. The only agent that modifies the System-1 agent files. |

Other harness components:
- **Orchestrator:** `harness/CLAUDE.md` — improvement loop walkthrough (run Comparanda → monitor → evaluate → diagnose → propose → apply → keep or revert).
- **Guard hook:** `harness/.claude/hooks/guard-comparanda.sh` — PreToolUse hook that prevents the harness from modifying anything in `comparanda/` except the universal instruction sections of `comparanda/.claude/agents/*.md`.
- **Eval scripts:** `harness/src/harness/{monitor.py, eval/aggregate.py, eval/prepare_comparison.py, eval/gold_standard.py}` — invoked by `monitor` and `eval` agents.
- **Benchmark:** `harness/benchmark/` — DPYD gold standard spreadsheet (symlink to local data; not committed), `eval_config/paper_pmid_map.json`, `MODIFICATIONS.md`.

The outer-loop agents modify the inner-loop agent prompts in
`comparanda/.claude/agents/`. They do **not** modify Python or hook code.

## Companion repos

- **This repo** — System 1 (`comparanda/`, inner loop) and System 2 (`harness/`, outer loop) live here as sibling subdirectories.
- **Per-domain application repo** — holds the actual schemas, surveys, observation logs, extraction outputs, paper-prep bundles, and domain reference files for a given domain (e.g., DPYD pharmacogenetics). Both System 1 and System 2 read from and write into this repo at runtime; it is intentionally separate so application state can iterate on a different cadence than system code.

## Bootstrap

See [`CLAUDE.md`](./CLAUDE.md) for the from-scratch setup walkthrough.

## Layout

```
CLAUDE.md            # Bootstrap + setup guide; top-level routing
README.md            # This file
comparanda/          # System 1 — the inner loop
  CLAUDE.md          # Phase-by-phase orchestration
  .claude/agents/    # Agent prompts (extractor, survey, ma-critique, ...)
  .claude/hooks/     # Validation hooks (provenance quotes)
  .claude/skills/    # Skill definitions
  scripts/
    observation_log.py   # Stdlib CLI invoked by the extractor
    paper_prep/          # PDF → bundle preprocessor (uv workspace, run before inner loop)
harness/             # System 2 — the outer loop (calibration)
  CLAUDE.md          # Improvement loop walkthrough
  .claude/agents/    # Agent prompts (monitor, eval, diagnostician, critic)
  .claude/hooks/     # Guard hook protecting comparanda/ from over-broad edits
  src/harness/       # Eval scripts invoked by the harness agents
  benchmark/         # Gold standard + eval config for DPYD
docs/                # Architecture, specs, research, paper-prep integration notes
```

## Application data in a fork

When you fork this repo to run a domain, the fork adds domain data **inside the
existing system-code tree** — not as a sibling repo. There are no symlinks for
system code; everything is in one checkout.

```
<fork-root>/                          # e.g., <workspace>/comparanda-<domain>/
├── comparanda/                       # System 1 (canonical)
│   └── projects/<domain>/            ← added in fork (gitignored at canonical)
│       ├── data/raw/{pdfs,spreadsheets}/
│       │   ├── pdfs/                       # Source PDFs (gitignored — large)
│       │   └── pdfs/phase0-references/     # Methodology + prior meta-analysis PDFs
│       ├── data/processed/<paper_id>/      # Paper-prep bundles
│       │   ├── manifest.json
│       │   ├── sections/<name>.md
│       │   ├── tables/table_<id>.{json,md}
│       │   └── figures/figure_<n>.png
│       ├── schema/v{NNN}.yaml              # Rich schema versions
│       ├── domain/{deep_research,seed_knowledge,*_ref}.md
│       ├── surveys/<paper_id>.json         # Phase 1
│       ├── extractions/<paper_id>.jsonl    # Phase 3a
│       ├── normalization/<table>.json      # Phase 3b
│       ├── critiques/iter_<NNN>.json       # Phase 3.5
│       └── observations/log.jsonl          # Append-only structured log
├── harness/                          # System 2 (canonical)
│   ├── iterations/                   ← added in fork (gitignored at canonical)
│   │   ├── trajectory.json
│   │   ├── accuracy_coverage.json
│   │   └── iter_*/
│   └── benchmark/gold_standard → ../../comparanda/projects/<domain>/data/raw/spreadsheets/
└── docs/                             # System 1+2 (canonical)
```

The fork is created via GitHub's fork button. Domain data is committed to the
fork's `main` branch. Only methodology changes — agent prompts, hooks, eval
code — are PR'd back to canonical from the fork's `methodology` branch.

Naming convention for `<paper_id>`: `Author_Year_PMID` (e.g., `Amstutz_2009_19530960`).

See [`CLAUDE.md`](./CLAUDE.md) for the bootstrap walkthrough that populates this tree.
