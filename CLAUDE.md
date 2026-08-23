# Comparanda — bootstrap & top-level orchestration

This file covers two things:

1. **Routing** — when working inside this repo, which subdirectory owns the task.
2. **Bootstrap** — how to stand up a fresh extraction run from zero.

## Responsibility boundary

Before changing system scope, agent responsibilities, readiness claims, or
evaluation targets, read
[`README.md#responsibility-boundary`](./README.md#responsibility-boundary).
That section is the authoritative product boundary.

In brief, Comparanda discovers schemas and extracts, normalizes, represents, and
surfaces the prerequisite evidence substrate. It does not select the final
research question, make analysis-specific eligibility decisions, choose a
statistical model, execute the meta-analysis, or make guideline or policy
recommendations. Those are user and downstream MetaCensus platform
responsibilities. Comparanda must nevertheless provide the prerequisite
information needed to (1) explore and select research questions, (2) determine
inclusion and exclusion, (3) determine a suitable statistical model, and (4)
construct the required inputs and run that model. Do not turn the resulting
downstream analyst decision into a required Comparanda output; use it to test
whether the substrate preserved the evidence and context needed for those four
tasks.

---

## Routing

- **"Run the system" / "extract from these papers" / "iterate the schema"** →
  `cd comparanda/` and follow `comparanda/CLAUDE.md` (phase-by-phase orchestrator).
  Use `comparanda/AGENT_INDEX.md` as the quick reference for agent scopes,
  file contracts, scripts, hooks, and resume behavior.

- **"Improve the agents" / "calibrate against gold standard" / "diagnose what failed"** →
  `cd harness/` and follow `harness/CLAUDE.md` (improvement loop).
  Use `harness/AGENT_INDEX.md` as the quick reference for harness agents,
  eval artifacts, script dependencies, and eval resume policy.

- **"How is the system put together" / "what's the spec"** → read `docs/specs/`
  and `docs/presentation/system-architecture-v1/`, after reading the responsibility
  boundary in `README.md`.

- **"How do bundles work" / "what's paper-prep"** → read
  `docs/paper-prep-integration.md`. The preprocessor itself lives in-tree at
  `comparanda/scripts/paper_prep/` (Docling + per-table Sonnet + Stage-2
  arbiter + bundle assembly).

- **"Run paper-prep on a PDF"** → `cd comparanda/scripts/paper_prep/ && uv sync`,
  then `uv run python -m paper_prep <paper_id> --pdf <path> --output-root <path>`
  (or `python -m paper_prep.batch ...` for a corpus). See Bootstrap Step 4.

`comparanda/` (inner loop) and `harness/` (outer loop) are self-contained
subprojects. `cd` into the one you're working in before invoking agents.

## System components

### Inner-loop agents (this repo, `comparanda/.claude/agents/`)

| File | Phase | Role |
|---|---|---|
| `survey.md` | 1 | Breadth pass over each paper — catalogs what extractable data exists. First agent to touch each paper. |
| `subset-selector.md` | 1.5 | Picks 3–4 diverse papers for each inner-loop iteration; rotates subsets across iterations to detect overfitting. |
| `schema-proposer.md` | 0.5, 2, 4 | Generates the survey schema (0.5) and the rich extraction schema (2). Refines the rich schema based on MA-critique findings (4). |
| `extractor.md` | 3a | Workhorse. Reads a paper-prep bundle and emits atomic-row JSONL. Most token spend happens here. |
| `extraction-qc.md` | 3a (post) | Reviews extractions for completeness vs survey, schema compliance, internal consistency. Runs after extraction and before normalization. |
| `normalizer.md` | 3b | Two-stage (deterministic + LLM) normalization of raw values to canonical IDs. |
| `ma-critique.md` | 3.5 | Assesses whether the extracted substrate preserves evidence needed downstream; proposes the minimum schema changes to reach convergence. |

### Other inner-loop components

| Path | Role |
|---|---|
| `comparanda/CLAUDE.md` | Orchestrator — phase-by-phase instructions for the inner loop. The orchestrator delegates to agents; it does not extract directly. |
| `comparanda/AGENT_INDEX.md` | Operator reference for inner-loop agents, file contracts, script dependencies, hooks, and resume behavior. |
| `comparanda/.claude/hooks/validate_extraction_write.py` | PreToolUse hook that blocks direct extraction JSONL writes outside sanctioned materialization scripts. |
| `comparanda/.claude/hooks/ensure_extraction_finalized.py` | SubagentStop hook that requires extractor agents to finish with a sanctioned finalizer success marker or explicit no-op. |
| `comparanda/.claude/skills/comparanda/SKILL.md` | Cross-phase pipeline-knowledge skill surfaced to agents that need it. |
| `comparanda/.claude/settings.json` | Agent-level configuration. |
| `comparanda/scripts/README.md` | Script index for extraction, normalization, review, observation logging, and paper-prep helper APIs. |
| `comparanda/scripts/observation_log.py` | Structured observation-log append/query helper used by all inner-loop agents. |
| `comparanda/scripts/paper_prep/` | PDF → bundle preprocessor (Docling + per-table Sonnet + Stage-2 arbiter + bundle assembly). Self-contained uv workspace; run via `python -m paper_prep` or `python -m paper_prep.batch`. Bootstrap Step 4. |

### Outer-loop agents (`harness/.claude/agents/`)

| File | Role |
|---|---|
| `harness/.claude/agents/monitor.md` | Reads inner-loop state (observation log, phase artifacts) and produces a structured status summary. |
| `harness/.claude/agents/eval.md` | Compares Comparanda extractions against the gold standard. Produces aggregate metrics plus semantic field-level coverage and rationale artifacts. |
| `harness/.claude/agents/diagnostician.md` | Traces eval findings to instruction root causes. Identifies which agent file needs changes. |
| `harness/.claude/agents/critic.md` | Proposes one specific, domain-independent instruction change per iteration. The only agent that modifies inner-loop agent files. |

### Other outer-loop components

| Path | Role |
|---|---|
| `harness/CLAUDE.md` | Outer-loop orchestrator — improvement loop walkthrough. |
| `harness/AGENT_INDEX.md` | Operator reference for harness agents, eval artifacts, script dependencies, and eval resume policy. |
| `harness/.claude/hooks/harness-guardrail-comparanda-writes.sh` | PreToolUse hook. Prevents the harness from modifying anything in `comparanda/` except the universal instruction sections of `comparanda/.claude/agents/*.md`. |
| `harness/src/harness/monitor.py` | Phase-transition monitor used by the `monitor` agent. |
| `harness/src/harness/eval/` | Eval helpers for aggregate metrics, claim faithfulness, semantic field coverage, scoring, rendering, batching, and workbench setup. See `harness/AGENT_INDEX.md#eval` for the current script inventory. |
| `harness/benchmark/` | Optional fork-owned gold standard/config. Canonical does not ship live domain benchmark data. |

The outer loop modifies inner-loop agent prompts in `comparanda/.claude/agents/`. It does **not** modify Python or hook code.

## Application data is not in this repo

Per-domain application state (schemas, observation logs, extraction outputs,
domain references, source PDFs, paper-prep bundles) lives in a separate
per-domain repo. This repo ships system code only.

---

## Bootstrap: from zero to a running extraction

To run Comparanda on a domain, you coordinate one repo and one
per-domain application directory tree. The high-level flow:

```
PDFs → paper-prep bundles → comparanda inner loop → reusable evidence substrate
```

### The repo

| Repo | Purpose | URL |
|---|---|---|
| `comparanda` (canonical) | System code: agents, hooks, orchestration, eval, **paper-prep preprocessor** | `git@github.com:metacensus/comparanda.git` |

The paper-prep preprocessor (Docling extraction + per-table Sonnet first-pass
+ Stage-2 LLM arbiter + bundle assembly) lives in-tree at
`comparanda/scripts/paper_prep/`. The original research repo
`<owner>/comparanda-paper-prep` still exists for ad-hoc debug tooling
(interactive viewer, alternative extractor experiments) but is not required
to run the pipeline.

Per-domain data lives in a **fork** of canonical (for example, `comparanda-<domain>`).
The fork adds domain data inside the existing system-code tree — no separate app repo.

### Prerequisites

- Python 3.12+ and [`uv`](https://github.com/astral-sh/uv)
- Anthropic API key (for the agentic runs)
- Source PDFs for your domain

### Step 1 — (optional) clone canonical for read-only reference

Not required to run a domain. If you want canonical handy for diffing methodology against your fork:

```bash
mkdir -p ~/metacensus && cd ~/metacensus
git clone git@github.com:metacensus/comparanda.git
```

### Step 2 — fork canonical, clone the fork, set up two branches

```bash
# Fork via GitHub web UI (github.com/metacensus/comparanda → Fork), or:
gh repo fork metacensus/comparanda --fork-name comparanda-<domain> --clone=false --remote=false

# Clone the fork.
cd ~/metacensus
git clone git@github.com:<your-org>/comparanda-<domain>.git
cd comparanda-<domain>

# Add canonical as upstream so methodology PRs flow back cleanly.
git remote add upstream git@github.com:metacensus/comparanda.git

# Create methodology branch (tracks upstream/main; accumulates methodology
# commits before they're PR'd back to canonical).
git checkout -b methodology
git branch --set-upstream-to=upstream/main methodology
git push origin methodology

# main is what the harness reads from: methodology + domain commits on top.
git checkout main

# Domain data ignores need un-ignoring on main (NOT on methodology).
# See "Fork .gitignore overrides" in the migration plan.
```

### Application data layout in a fork

The fork has this tree (everything below the system-code paths is fork-only):

```
<workspace>/comparanda-<domain>/
├── comparanda/                                  # system code (canonical)
│   ├── CLAUDE.md, .claude/, scripts/            # canonical contents
│   └── projects/<domain>/                       ← fork-only (gitignored at canonical)
│       ├── data/
│       │   ├── raw/{pdfs,spreadsheets}/
│       │   └── processed/<paper_id>/            # paper-prep bundles
│       ├── domain/{deep_research,seed_knowledge,...}.md
│       ├── schema/v{NNN}.yaml
│       ├── surveys/<paper_id>.json
│       ├── extractions/<paper_id>.jsonl
│       ├── normalization/<table>.json
│       ├── critiques/iter_<NNN>.json
│       └── observations/log.jsonl
├── harness/                                     # system code (canonical)
│   ├── CLAUDE.md, .claude/, src/, benchmark/    # canonical contents
│   └── iterations/                              ← fork-only (gitignored at canonical)
│       ├── trajectory.json
│       ├── accuracy_coverage.json
│       └── iter_*/
└── docs/                                        # canonical
```

Recommended naming convention for `<paper_id>`: `Author_Year_DocumentID`.

### Step 3 — seed the domain knowledge files

Two files must exist before running Phase 0:

- `projects/<domain>/domain/deep_research.md` — a deep research report covering
  the domain's variants/entities/outcomes, the prior literature, common
  measurement scales, and known terminology drift.
- `projects/<domain>/domain/seed_knowledge.md` — a shorter primer that gives
  each agent enough domain footing to start.

> **Gap:** there is no script that generates these. For a new domain, plan to
> spend time on this step — agent quality downstream depends on it.

Drop methodology + prior meta-analysis PDFs into
`projects/<domain>/data/raw/pdfs/phase0-references/`.

### Step 4 — build paper-prep bundles

The bundle preprocessor ships in-tree at `comparanda/scripts/paper_prep/`.
**Run it from a regular terminal** — not from inside a Claude Code session
(nested `claude -p` subprocesses don't work).

First time setup:

```bash
cd <workspace>/comparanda-<domain>/comparanda/scripts/paper_prep
uv sync   # installs docling + pydantic
```

Build one paper:

```bash
uv run python -m paper_prep <paper_id> \
    --pdf "/path/to/<paper_id>.pdf" \
    --output-root <workspace>/comparanda-<domain>/comparanda/projects/<domain>/data/processed/
```

Build a corpus (uses a subset JSON to map document IDs to paper IDs):

```bash
uv run python -m paper_prep.batch \
    --subset examples/example_batch_subset.json \
    --pdfs-dir <workspace>/comparanda-<domain>/comparanda/projects/<domain>/data/raw/pdfs/ \
    --bundle-out <workspace>/comparanda-<domain>/comparanda/projects/<domain>/data/processed/
```

Pipeline steps per paper (both `python -m paper_prep` and `python -m paper_prep.batch`):
1. **Layer 1** — Docling section/table/figure extraction + per-table Sonnet first-pass
   (parallel batch of 4 by default, ~$1.50/table budget cap).
2. **Layer 2** — Per-table Stage-1 deterministic merge (NW row alignment +
   cell classification) + optional Stage-2 LLM arbiter (`claude -p` Sonnet
   with PDF + diff guide) + bundle assembly (manifest.json + sections/ +
   tables/ + figures/ + _provenance/).

Use `--no-arbitrate` for a free dry-run that skips Stage-2 (still gives
dual-extractor deterministic merge; conflicts go unresolved, the bundle
drops any two-sided table with unresolved conflicts).

See `docs/paper-prep-integration.md` for what the agents and hook expect from
the bundle, and what happens if a bundle is missing.

### Step 5 — run the inner loop

```bash
cd <workspace>/comparanda-<domain>/comparanda
# Read CLAUDE.md and follow Phase 0 onward.
claude    # or: claude -p "..."
```

No symlinks needed — system code is in the same checkout. `comparanda/projects/<domain>/...`
resolves locally because cwd is `comparanda/`. The orchestrator at `comparanda/CLAUDE.md`
walks through Phase 0 → 0.5 → 1 → 2 → 3a/3b/3.5 → 4 → 5 → 6.

### Step 6 — run the calibration harness (optional, after you have output)

```bash
cd <workspace>/comparanda-<domain>/harness
# Optional: pin commit-routing mode (strict is the default — see
# docs/specs/harness-commit-wrapper.md for lenient/off escape hatches).
# export HARNESS_COMMIT_MODE=strict
claude
```

The harness reads `../comparanda/projects/<domain>/...` and writes to
`./iterations/`. Supply the gold standard and document-id map from the
per-domain fork when running eval commands.

### Known gaps to close

These would make the bootstrap turn-key but don't exist yet:

- A scaffolding script that creates the `projects/<domain>/` tree.
- A documented process (or generator) for `deep_research.md` and `seed_knowledge.md`.
- A small scaffolding helper for per-domain benchmark config would make one-shot eval setup less manual.
