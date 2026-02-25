# Foundational Principles

> The architecture isn't arbitrary — it's driven by real constraints. This document captures the three-tier reasoning: immutable constraints force design principles, which lead to architectural choices.
>
> Format TBD: may become a slide, a diagram, or remain as talking points.

---

## Tier 1: Immutable Constraints

These are true regardless of what we decide. They are the physics of the problem.

### Context windows are finite
An LLM agent can only see a limited amount of text at once. You cannot give one agent all 47 papers, the full schema, all normalization tables, and the iteration history. This forces decomposition into focused, isolated agents.

### LLMs hallucinate
Models generate plausible-sounding content that isn't grounded in source material. For scientific data extraction, a hallucinated effect size or patient count is worse than a missing one. You cannot trust a single-pass extraction without verification.

### LLMs are stochastic
The same input can produce different outputs. A single extraction attempt is a sample, not a guarantee. Quality requires iteration and validation, not one-shot execution.

### Scientific papers are heterogeneous
Papers span decades of evolving terminology, journal formats, statistical reporting conventions, and data presentation styles. A system that works on one paper may fail on another. The same concept (a gene variant, a clinical outcome) gets described differently across papers and eras.

### Schema development requires cross-document reasoning
Deciding what fields a schema should have requires seeing patterns across many papers — which data categories appear consistently, what varies, where normalization is needed. But you can't fit all papers in one context window. This creates a fundamental tension: cross-document reasoning within single-document context limits.

---

## Tier 2: Design Principles

These are deliberate responses to the constraints. Once you accept the constraints, these principles feel necessary.

### Context discipline → Isolated agents with focused context
Each agent gets only what it needs. One paper per extraction agent. Grouped field passes rather than all-at-once. Survey summaries rather than full papers for cross-document reasoning. Multiple focused passes rather than monolithic processing.

### Stochastic + hallucination → Iterative refinement, not one-shot
Don't extract once and ship. Extract, validate, evaluate, refine, re-extract. Each iteration improves quality. Deterministic validation catches mechanical errors. Evaluation against benchmarks catches systematic patterns.

### Cross-document + context limits → Survey-first pattern
A cheap, fast model surveys each paper individually (fits in context). A synthesis agent reads all survey summaries (fits in context because they're structured and compressed). This bridges the gap between needing cross-document reasoning and having single-document context limits.

### Iteration improves quality → Refinement loops
The core mechanism is a loop: schema proposal → extraction → evaluation → refinement → repeat. The schema and the extraction improve together through iteration, not through a single brilliant design.

### Iteration without acceptance criteria is just burning tokens → Measurable convergence
The system needs to know when to stop — at two levels. **System 1's core loop** converges when the schema stabilizes (rate of change approaching zero) and the Meta-Analysis Readiness Agent confirms the extraction is suitable for pooling across studies. The acceptance test is functional: can a researcher make inclusion/exclusion decisions, derive effect sizes, and pool results without going back to the papers? (C8) **System 2's outer loop** converges separately, using the gold standard benchmark — composite metric of schema coverage (40%) + extraction F1 (60%) against the human expert's data. System 2 stops when this metric plateaus (<1% improvement over 3 iterations) or hits a hard cap (10 iterations). Crucially, System 1 never sees the gold standard — it optimizes for internal quality and meta-analysis readiness. System 2 uses the benchmark to decide when to stop modifying instructions. Progressive expansion to new papers detects overfitting at both levels.

### Separation of concerns → Two systems
System 1 does the work (develops schema, extracts data). System 2 improves how the work is done (evaluates results, identifies failure patterns, modifies agent instructions). Separating "do" from "improve" prevents the system from being unable to reflect on its own process.

### Instructions-as-config → Behavior in files, not code
Agent behavior lives in versioned markdown instruction files, not hardcoded in implementation. This means the improvement loop (System 2) can modify how agents work by editing text files — without touching code. It also means every behavior change is trackable, diffable, and reversible.

---

## Tier 3: Architecture

The specific implementation that follows from the principles. This is what the remaining diagrams cover:

- **Phase pipeline (0-6)**: The specific workflow for schema development + extraction
- **Agent roles**: Survey, schema proposer, extractor, normalizer, validator, critic
- **Two-system structure**: System 1 (inner loop) + System 2 (outer loop)
- **Tool choices**: Claude, PyMuPDF4LLM, pytest + pandas evaluation
- **State model**: Files on disk, git-tracked, versioned schemas and instructions

These are our decisions — they follow from the principles, but other implementations are possible. The principles are what we're confident about. The architecture is what we're building and testing.

---

## How They Connect

```
CONSTRAINTS (can't change these)
  Context windows are finite
  LLMs hallucinate
  LLMs are stochastic
  Papers are heterogeneous
  Schema needs cross-doc reasoning
         │
         ▼
PRINCIPLES (follow from constraints)
  Context discipline
  Iterative refinement
  Survey-first pattern
  Refinement loops
  Measurable convergence
  Two-system separation
  Instructions-as-config
         │
         ▼
ARCHITECTURE (our specific choices)
  Phase 0-6 pipeline
  System 1 + System 2
  Agent roles & tools
  State model
```
