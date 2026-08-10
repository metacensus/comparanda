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
For the current agent scopes, file contracts, script dependencies, hooks, and
resume behavior, use [`comparanda/AGENT_INDEX.md`](./comparanda/AGENT_INDEX.md)
as the operator reference.

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
- **Hooks:** `comparanda/.claude/hooks/validate_extraction_write.py` blocks direct extraction JSONL writes, and `comparanda/.claude/hooks/ensure_extraction_finalized.py` enforces sanctioned finalization on extractor stop. See [`comparanda/AGENT_INDEX.md`](./comparanda/AGENT_INDEX.md#guardrails).
- **Skill:** `comparanda/.claude/skills/comparanda/SKILL.md` — pipeline-knowledge skill surfaced to agents that need cross-phase context.
- **Scripts:** `comparanda/scripts/` — documented helper APIs for extraction, normalization, review, observation logging, and paper prep. Start with [`comparanda/scripts/README.md`](./comparanda/scripts/README.md).
- **Paper-prep preprocessor:** `comparanda/scripts/paper_prep/` — PDF → bundle pipeline (Docling extraction + per-table Sonnet first-pass + Stage-2 LLM arbiter + bundle assembly). Self-contained `uv` workspace. Run **before** the inner loop: `cd comparanda/scripts/paper_prep && uv sync && uv run python -m paper_prep <paper_id> --pdf <path> --output-root <project-root>/data/processed/`. Batch form: `uv run python -m paper_prep.batch ...`. See [`docs/paper-prep-integration.md`](./docs/paper-prep-integration.md) and Bootstrap Step 4 in [`CLAUDE.md`](./CLAUDE.md).
- **Settings:** `comparanda/.claude/settings.json` — agent-level configuration.

### System 2 — outer loop (`harness/`)

Calibration harness that watches inner-loop runs, evaluates outputs against the
gold standard, traces failures to instruction-level root causes, and proposes
generalizable fixes to the agents in `comparanda/`. Lives in
[`harness/`](./harness/) and is invoked from there.
For the current harness agent scopes, eval artifacts, scripts, and eval resume
policy, use [`harness/AGENT_INDEX.md`](./harness/AGENT_INDEX.md).

| Agent | File | Role |
|---|---|---|
| `monitor` | `harness/.claude/agents/monitor.md` | Reads inner-loop state (observation log, phase artifacts) and produces a structured status summary. |
| `eval` | `harness/.claude/agents/eval.md` | Compares Comparanda extractions against the gold standard. Aggregate-mode (completeness) and detailed-mode (semantic accuracy). |
| `diagnostician` | `harness/.claude/agents/diagnostician.md` | Traces eval findings to instruction root causes. Classifies failures as instruction-fixable or domain-specific; identifies which agent file needs changes. |
| `critic` | `harness/.claude/agents/critic.md` | Proposes one specific, domain-independent instruction change per iteration based on the diagnosis. The only agent that modifies the System-1 agent files. |

Other harness components:
- **Orchestrator:** `harness/CLAUDE.md` — improvement loop walkthrough (run Comparanda → monitor → evaluate → diagnose → propose → apply → keep or revert).
- **Guard hook:** `harness/.claude/hooks/harness-guardrail-comparanda-writes.sh` — PreToolUse hook that prevents the harness from modifying anything in `comparanda/` except the universal instruction sections of `comparanda/.claude/agents/*.md`.
- **Eval scripts:** `harness/src/harness/eval/` — aggregate metrics, claim faithfulness, semantic field coverage, scoring, rendering, batching, and workbench helpers invoked by the `eval` agent. See [`harness/AGENT_INDEX.md`](./harness/AGENT_INDEX.md#eval).
- **Benchmark config:** supplied by the domain superproject or by explicit eval CLI arguments. Canonical does not ship a live domain gold standard.

The outer-loop agents modify the inner-loop agent prompts in
`comparanda/.claude/agents/`. They do **not** modify Python or hook code.

## Companion repos

- **This repo** — System 1 (`comparanda/`, inner loop) and System 2 (`harness/`, outer loop) live here as sibling subdirectories.
- **Per-domain superproject** — composes this repository as a pinned submodule
  with one flattened domain workspace and a pinned domain-export repository.
- **Per-domain data repo** — immutable deployable reviewer exports. It can be
  pinned independently by the domain superproject and downstream consumers.

See [`docs/domain-superproject.md`](./docs/domain-superproject.md) for the
composition and path contracts.

## Bootstrap

See [`CLAUDE.md`](./CLAUDE.md) for the from-scratch setup walkthrough.

## Layout

```
CLAUDE.md            # Bootstrap + setup guide; top-level routing
README.md            # This file
comparanda/          # System 1 — the inner loop
  CLAUDE.md          # Phase-by-phase orchestration
  AGENT_INDEX.md     # Agent scopes, contracts, scripts, hooks, resume behavior
  .claude/agents/    # Agent prompts (extractor, survey, ma-critique, ...)
  .claude/hooks/     # Extraction write/finalization validation hooks
  .claude/skills/    # Skill definitions
  scripts/
    README.md            # Script index
    observation_log.py   # Structured observation-log helper
    extraction/          # SDK, validator, materializers, session registry
    normalization/       # Normalizer reconcile/apply/validate API
    review/              # Human-review queue/packet/apply helpers
    paper_prep/          # PDF → bundle preprocessor (uv workspace, run before inner loop)
harness/             # System 2 — the outer loop (calibration)
  CLAUDE.md          # Improvement loop walkthrough
  AGENT_INDEX.md     # Harness agent scopes, eval artifacts, resume behavior
  .claude/agents/    # Agent prompts (monitor, eval, diagnostician, critic)
  .claude/hooks/     # Guard hook protecting comparanda/ from over-broad edits
  src/harness/       # Eval scripts invoked by the harness agents
  benchmark/         # Legacy local benchmark path; superprojects supply external paths
docs/                # Architecture, specs, research, paper-prep integration notes
```

## Application data in a domain superproject

Each domain has one private composition repository. Its root is the project
root, so application state is not nested under a redundant
`projects/<domain>/` directory.

```text
comparanda-<domain>/
├── comparanda/                 submodule -> this repository
├── export/                     submodule -> comparanda-<domain>-data
├── schema/, surveys/, extractions/, observations/
├── normalization/, critiques/, domain/
├── data/raw/                   source corpus (binary assets may use Git LFS)
├── data/processed/             paper-prep bundles
├── benchmark/                  evaluation config + gold standard
├── iterations/                 domain run history
└── comparanda-project.json     portable path contract
```

Methodology changes are committed on a feature branch inside the `comparanda/`
submodule. Domain and run state are committed in the superproject. Reviewer
exports are committed in `export/`. Consumers pin each repository independently;
advancing one pin is an explicit reviewable change.

Recommended naming convention for `<paper_id>`: `Author_Year_DocumentID`.

See [`docs/domain-superproject.md`](./docs/domain-superproject.md) for setup and
[`CLAUDE.md`](./CLAUDE.md) for the inner-loop bootstrap walkthrough.
