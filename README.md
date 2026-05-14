# Comparanda

Agentic system for scientific paper schema extraction and structured data encoding.

## Problem



The bottleneck is the first step: turning a collection of papers into structured, comparable data. Today this is grueling manual work, repeated independently across institutions. Existing tools (Covidence, Elicit, etc.) can help with extraction — but they all assume a schema already exists. Someone still has to decide *what fields to extract* and *how to reconcile terminology* that has evolved across decades of literature. That schema design work is the hard part, and nobody automates it.


## How it works

Two-system architecture:

**System 1 (Inner Loop)** — does the work:
- Survey all papers with cheap/fast models (one agent per paper)
- Synthesize surveys into a proposed schema
- Extract a test subset, refine the schema iteratively
- Expand to full extraction once the schema stabilizes
- Validate: can you run the meta-analysis from this data?

**System 2 (Outer Loop)** — improves how the work is done:
- Observes System 1 outputs, metrics, and agent reflections
- Diagnoses failure patterns, proposes instruction changes
- Modifies behavior through versioned instruction files, not code

The system is implemented as an agentic flow: orchestration lives in markdown
(`comparanda/CLAUDE.md`), agents are markdown prompt files (`comparanda/.claude/agents/`),
and the only Python is a small helper for log queries.

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
