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
- **Settings:** `comparanda/.claude/settings.json` — agent-level configuration.

### System 2 — outer loop (separate repo: `comparanda-harness`)

Calibration harness that watches inner-loop runs, evaluates outputs against the
gold standard, traces failures to instruction-level root causes, and proposes
generalizable fixes to the agents in this repo. Four agents:

| Agent | Role |
|---|---|
| `monitor` | Reads inner-loop state (observation log, phase artifacts) and produces a structured status summary. |
| `eval` | Compares Comparanda extractions against the gold standard. Aggregate-mode (completeness) and detailed-mode (semantic accuracy). |
| `diagnostician` | Traces eval findings to instruction root causes. Classifies failures as instruction-fixable or domain-specific; identifies which agent file needs changes. |
| `critic` | Proposes one specific, domain-independent instruction change per iteration based on the diagnosis. The only agent that modifies the System-1 agent files. |

The outer-loop agents modify the inner-loop agent prompts in
`comparanda/.claude/agents/`. They do **not** modify Python or hook code.

## Companion repos

- **This repo** — System 1 (inner loop): system code (agents, hooks, orchestration) that turns paper-prep bundles into extracted atomic rows.
- **`comparanda-harness`** — System 2 (outer loop): calibration harness that watches inner-loop runs, evaluates against gold standard, diagnoses instruction-level problems, and proposes generalizable fixes to the agents in this repo. (Local-only at time of writing; remote URL TBD.)
- **Per-domain application repo** — holds the actual schemas, surveys, observation logs, extraction outputs, paper-prep bundles, and domain reference files for a given domain (e.g., DPYD pharmacogenetics). Comparanda reads from and writes into this repo at runtime; it is intentionally separate so application state can iterate on a different cadence than system code.

## Bootstrap

See [`CLAUDE.md`](./CLAUDE.md) for the from-scratch setup walkthrough.

## Layout

```
CLAUDE.md            # Bootstrap + setup guide; top-level routing
README.md            # This file
comparanda/          # The system
  CLAUDE.md          # Phase-by-phase orchestration (inner loop)
  .claude/agents/    # Agent prompts (extractor, survey, ma-critique, ...)
  .claude/hooks/     # Validation hooks (provenance quotes)
  .claude/skills/    # Skill definitions
  scripts/           # Stdlib utilities invoked by agents (observation_log)
docs/                # Architecture, specs, research, paper-prep integration notes
```

## Application data lives elsewhere

Application instances — actual schemas, observation logs, extraction state, PDFs,
domain references — live in a sibling per-domain repo so they can iterate
independently of system development. Do not add a `projects/<domain>/` directory
back into this repo.

### Application repo layout

The system expects this tree inside the per-domain application repo
(`comparanda-<domain>/`):

```
projects/<domain>/
├── data/
│   ├── raw/
│   │   ├── pdfs/                       # Source PDFs (gitignored — large)
│   │   └── pdfs/phase0-references/     # Methodology + prior meta-analysis PDFs
│   └── processed/                      # Paper-prep bundles land here
│       └── <paper_id>/
│           ├── manifest.json
│           ├── sections/<name>.md
│           ├── tables/table_<id>.{json,md}
│           └── figures/figure_<n>.png
├── domain/
│   ├── deep_research.md                # SEED: deep domain research report
│   ├── seed_knowledge.md               # SEED: domain primer for agents
│   └── {agent}_ref.md                  # Generated in Phase 0
├── schema/
│   ├── survey_schema.json              # Generated in Phase 0.5
│   └── v{NNN}.yaml                     # Rich schema versions; grows over iterations
├── surveys/<paper_id>.json             # Phase 1
├── extractions/<paper_id>.jsonl        # Phase 3a
├── normalization/<table>.json          # Phase 3b
├── critiques/iter_<NNN>.json           # Phase 3.5
└── observations/log.jsonl              # Append-only structured log
```

Naming convention for `<paper_id>`: `Author_Year_PMID` (e.g., `Amstutz_2009_19530960`).

See [`CLAUDE.md`](./CLAUDE.md) for the bootstrap walkthrough that populates this tree.
