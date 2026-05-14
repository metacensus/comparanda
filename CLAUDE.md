# Comparanda — bootstrap & top-level orchestration

This file covers two things:

1. **Routing** — when working inside this repo, which subdirectory owns the task.
2. **Bootstrap** — how to stand up a fresh extraction run from zero.

---

## Routing

- **"Run the system" / "extract from these papers" / "iterate the schema"** →
  `cd comparanda/` and follow `comparanda/CLAUDE.md` (phase-by-phase orchestrator).

- **"How is the system put together" / "what's the spec"** → read `docs/specs/`
  and `docs/presentation/system-architecture-v1/`.

- **"How do bundles work" / "what's paper-prep"** → read
  `docs/paper-prep-integration.md` and the
  [`comparanda-paper-prep`](https://github.com/<owner>/comparanda-paper-prep) repo.

`comparanda/` is a self-contained subproject (its own `CLAUDE.md`, `.claude/agents/`,
`scripts/`). `cd` into it before invoking agents.

## Application data is not in this repo

Per-domain application state (schemas, observation logs, extraction outputs,
domain references, source PDFs, paper-prep bundles) lives in a separate
per-domain repo. This repo ships system code only.

---

## Bootstrap: from zero to a running extraction

To run Comparanda on a domain (DPYD or new), you coordinate three repos and one
per-domain application directory tree. The high-level flow:

```
PDFs → paper-prep bundles → comparanda inner loop → extracted atomic rows
```

### The three repos

| Repo | Purpose | URL |
|---|---|---|
| `comparanda` (this) | System code: agents, hooks, orchestration | `git@github.com:metacensus/comparanda.git` |
| `comparanda-paper-prep` | PDF → structured bundle preprocessor | `git@github.com:<owner>/comparanda-paper-prep.git` |
| `comparanda-<domain>` | Per-domain application data (schemas, surveys, extractions, PDFs) | TBD per domain — see "Application repo layout" below |

### Prerequisites

- Python 3.12+ and [`uv`](https://github.com/astral-sh/uv)
- Anthropic API key (for the agentic runs)
- Source PDFs for your domain

### Step 1 — clone the system repos as siblings

```bash
mkdir -p ~/metacensus && cd ~/metacensus
git clone git@github.com:metacensus/comparanda.git
git clone git@github.com:<owner>/comparanda-paper-prep.git paper-prep
cd paper-prep && uv sync && cd ..
```

### Step 2 — create or clone the per-domain application repo

```bash
cd ~/metacensus
# For a new domain:
mkdir -p comparanda-<domain>/projects/<domain>
# For DPYD specifically, the working dataset lives outside the public repo
```

### Application repo layout

The system expects this tree inside the per-domain repo:

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

### Step 3 — seed the domain knowledge files

Two files must exist before running Phase 0:

- `projects/<domain>/domain/deep_research.md` — a deep research report covering
  the domain's variants/entities/outcomes, the prior literature, common
  measurement scales, and known terminology drift.
- `projects/<domain>/domain/seed_knowledge.md` — a shorter primer that gives
  each agent enough domain footing to start.

> **Gap:** there is no script that generates these. For DPYD they were produced
> manually via deep research. For a new domain, plan to spend time on this step
> — agent quality downstream depends on it.

Drop methodology + prior meta-analysis PDFs into
`projects/<domain>/data/raw/pdfs/phase0-references/`.

### Step 4 — build paper-prep bundles

For each source PDF, build a bundle:

```bash
cd <workspace>/paper-prep
uv run python -m paper_prep.prep <paper_id> \
    --run-dir experiments/phase3_run/<paper_id> \
    --pdf "/path/to/<paper_id>.pdf" \
    --output-root <workspace>/comparanda-<domain>/projects/<domain>/data/processed/
```

> **Gap:** `--run-dir` points at a paper-prep "Phase 3 production output dir",
> which is built by paper-prep's own pipeline (Sonnet first-pass + Docling +
> cross-validated merge). See paper-prep's README and `scripts/run_paper.py` /
> `scripts/run_dpyd_batch.py` for how that's produced. A turn-key one-command
> bundle build is not yet wired up; for now, build the run-dir with paper-prep
> first, then invoke `prep` to materialize the bundle.

See `docs/paper-prep-integration.md` for what the agents and hook expect from
the bundle, and what happens if a bundle is missing.

### Step 5 — run the inner loop

```bash
cd <workspace>/comparanda/comparanda
# Read comparanda/CLAUDE.md and follow Phase 0 onward.
```

`comparanda/CLAUDE.md` walks the orchestrator through Phase 0 (generate
agent-specific domain reference files) → Phase 0.5 (survey schema) → Phase 1
(survey all papers, pick a subset) → Phase 2 (initial schema) → Phase 3a/3b/3.5
(extract / normalize / critique loop) → Phase 4 (schema refinement) → Phase 5
(subset rotation) → Phase 6 (full-corpus extraction).

The agents read from and write into `projects/<domain>/` paths. Run the
orchestrator from a working directory that resolves those paths correctly
relative to your per-domain repo (typically `cd comparanda-<domain>/` and invoke
the orchestrator from there, with `comparanda/.claude/` and `comparanda/scripts/`
reachable).

### Known gaps to close

These would make the bootstrap turn-key but don't exist yet:

- A scaffolding script that creates the `projects/<domain>/` tree.
- A documented process (or generator) for `deep_research.md` and `seed_knowledge.md`.
- A single-command path from PDF → bundle (folding paper-prep's run-dir build into the bundle materialization).
- A bootstrap doc for the outer-loop calibration harness (currently lives separately as `comparanda-harness`, not yet pushed).
