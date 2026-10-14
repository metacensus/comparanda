# Comparanda

Agentic system for scientific paper schema extraction and structured data encoding.

## Problem

[MetaCensus](https://github.com/metacensus) is building an open-access platform to modernize scientific meta-analysis — tightening the loop between published evidence and policy by making extraction granular enough to do once and query forever.

The bottleneck is the first step: turning a collection of papers into structured, comparable data. Today this is grueling manual work, repeated independently across institutions. Existing tools (Covidence, Elicit, etc.) can help with extraction — but they all assume a schema already exists. Someone still has to decide *what fields to extract* and *how to reconcile terminology* that has evolved across decades of literature. That schema design work is the hard part, and nobody automates it.

Comparanda is the evidence-preparation engine for MetaCensus. It treats the schema as the product: given a domain and a collection of papers, the system discovers what structured data should be extracted, builds normalization tables (Rosetta Stones) to reconcile cross-era terminology, and produces a grounded, reusable evidence substrate for downstream evidence synthesis.

Comparanda does not choose the research question, define eligibility, include
or exclude studies, select an estimand or model, or run a meta-analysis. It
preserves and surfaces the evidence, context, relations, dependencies,
uncertainties, and missing information that a downstream user or platform needs
to: (1) explore and select research questions, (2) determine inclusion and
exclusion, (3) determine a suitable statistical model, and (4) construct the
required inputs and run that model.

## How it works

Two-system architecture:

**System 1 (Inner Loop)** — does the work:
- Survey all papers with cheap/fast models (one agent per paper)
- Synthesize surveys into a proposed schema
- Extract a test subset, refine the schema iteratively
- Expand to full extraction once the schema stabilizes
- Validate: is the evidence substrate complete, grounded, and sufficiently contextualized for downstream question exploration and synthesis decisions?

**System 2 (Outer Loop)** — improves how the work is done:
- Observes System 1 outputs, metrics, and agent reflections
- Diagnoses failure patterns, proposes instruction changes
- Modifies behavior through versioned instruction files, not code

## Benchmark

The system is validated against human-extracted reference datasets — a known schema and a corpus of papers, with the system's output measured against the human-built ground truth.

## Status

In iteration on the inner-loop + outer-loop architecture.

## Repo structure

```
CLAUDE.md            # Orchestrator instructions (phase-by-phase inner loop)
AGENT_INDEX.md       # Agent scopes, file contracts, scripts, resume behavior
.claude/
  agents/            # Agent prompts (extractor, normalizer, survey, ma-critique, ...)
  hooks/             # Validation hooks (e.g. provenance quote checks)
  skills/            # Skill definitions invoked during orchestration
scripts/             # Utilities invoked by agents; see scripts/README.md
```

System-level docs (architecture, specs, research) live at the repo root in `../docs/`.
