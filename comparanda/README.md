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

## Benchmark

The system is validated against human-extracted reference datasets — a known schema and a corpus of papers, with the system's output measured against the human-built ground truth.

## Status

In iteration on the inner-loop + outer-loop architecture.

## Repo structure

```
CLAUDE.md            # Orchestrator instructions (phase-by-phase inner loop)
.claude/
  agents/            # Agent prompts (extractor, normalizer, survey, ma-critique, ...)
  hooks/             # Validation hooks (e.g. provenance quote checks)
  skills/            # Skill definitions invoked during orchestration
scripts/             # Utilities invoked by agents (observation log query, ...)
```

System-level docs (architecture, specs, research) live at the repo root in `../docs/`.
