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


## Status

Pre-planning. Researching open design questions before writing the implementation spec.

## Repo structure

```
CLAUDE.md                  # Project instructions and architecture overview
docs/
  context/                 # Reference documents (vision, schema, agents, tech stack, ...)
  research/                # Deep research outputs (eval frameworks, PDF ingestion, ...)
  presentation/            # Architecture diagrams for stakeholder walkthroughs
```
