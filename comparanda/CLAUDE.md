# MetaCensus — DPYD Schema Extraction Prototype

## Session Recovery — READ FIRST

**Current phase**: Build — all pre-planning decisions locked, constructing the system.

1. Read this section fully for current state and build plan
4. Check `docs/research/` for deep research outputs (R1, R2, R3, R5, System 2 reference architectures)

**Decisions summary** (all locked as of 2026-04-01):
- **Q1-Q3**: Custom pytest/pandas eval harness, PyMuPDF4LLM + Claude vision for PDFs, JSONL output with versioned JSON Schema
- **Q4-Q6**: Two-phase extract-then-normalize, files-on-disk git-tracked state, composite hill-climbing convergence
- **Q7-Q10**: Deterministic relational checks (defer LLM reviewer), two metric families (schema + extraction), per-agent context budgets, log-first autonomous with observability
- **Q11-Q13**: Seven-phase schema development loop, DPYD benchmark metrics, hybrid progressive expansion
- **Q14-Q15**: PaperQA2 retrieval backbone (buy), PubTator/UMLS/scispaCy normalization seeding (buy)
- **Q16**: Build custom System 2 (~500 lines), porting patterns from autocontext (lesson store, dimensional scoring, regression detection, playbook enrichment). Two-layer eval: deterministic for countable/exact/numeric + LLM judge for schema equivalence, semantic matching, structural assessment.

**Key architectural decisions**:
- Schema is the product, not a fixed template (Critical Reframe, 2026-02-22)
- Two-layer eval harness: deterministic (fast/cheap, rapid iteration) + LLM judge (semantic equivalence at generation boundaries)
- System 2 built, not bought — autocontext evaluated but LLM-judge-centric architecture doesn't fit our deterministic gold standard comparison
- Multiple valid schemas can reach the same mountain — LLM judge handles "is `patient_ethnicity` equivalent to `ancestry`?" type questions

**Build status** (2026-04-01):
- ✅ Two-layer eval harness — 108 tests passing (matchers, alignment, scoring, regression, LLM judge, integration)
- ✅ System 2 improvement loop — lesson store, trajectory, playbook enrichment, runner
- ✅ System 1 agents — survey, schema proposer, extractor, normalizer, subset selector
- ✅ PDF ingestion — PyMuPDF4LLM, tested on real papers
- ✅ Domain context — deep research output for DPYD
- ✅ Survey schema — generated from domain context (34 fields, two-layer design)
- ✅ Proposed extraction schema — 74 fields discovered from 4 surveys (no gold standard leakage)
- ✅ Orchestrator — end-to-end pipeline runner
- ✅ First extraction — Amstutz 2009, 38 atomic rows
- 🔄 First autonomous iteration — running
- 🔲 PaperQA2 integration
- 🔲 Progressive subset expansion (Phase 5)

**Project organization**:
- Agent definitions: `.claude/agents/*.md` (work both interactively and via SDK)
- System 2 instruction files: `src/comparanda/instructions/` (modification targets)
- Skills: `.claude/skills/comparanda/` (pipeline knowledge)
- Orchestrator: `src/comparanda/orchestrator.py` (chains all phases)
- Agent SDK: `claude-agent-sdk` with `setting_sources=["project"]` for config loading


## CRITICAL: The Schema IS the Product

The principal output of this system is the **schema itself** — not just data extracted into a
pre-existing schema. The core challenge: given a domain and a collection of papers, how do you
build a schema that enables apples-to-apples comparisons across papers?

- **Universal fields** are invariant across domains (title, DOI, PMID, authors, etc.)
- **Domain-specific fields** must be DISCOVERED from the papers themselves
  schema + extraction looks like — not a fixed template to extract into
- The DPYD dataset is a BENCHMARK to validate whether the agent system can produce comparable
  schema + extraction quality to human expert work
- The **Schema Development Agent is the core** of the system, not a deferred concern

## Project Summary

Build an **agentic system** that reviews scientific papers, develops a structured data schema
for encoding findings as **atomic rows**, and extracts data into that schema. The immediate

## Key Concepts

- **Atomic Rows**: Maximum-granularity data points extracted from papers. One row = one indivisible finding. A single paper can yield hundreds of rows. Goal: extract once, never re-extract.
- **Dynamic ETL**: Not static Extract-Transform-Load. Transformations are definitions run at query time ("just-in-time"), keeping data fluid as new papers or definitions arrive.
- **Schema/Extraction Loop**: Schema definition and data extraction are tightly iterative — you extract to discover what schema you need, then refine and re-extract. This is the central mechanism, not a side process.

## Two-System Architecture

The system has two distinct loops:

### System 1: Inner Loop (Do the Work)
Schema development + extraction workflow. Reads papers, proposes schema, extracts data, validates.

**Workflow phases:**

0. **Domain Onboarding** — User fills scoping questionnaire. System runs deep research (via API)
   to build a domain context document. For DPYD benchmark: can use domain knowledge but NOT

1. **Paper Survey** — Survey ALL papers using cheap/fast model (Haiku). One subagent per paper,
   standardized output schema to file. Then a synthesis subagent reads all survey outputs and
   picks a diverse subset for the test batch.

2. **Schema Proposal** — Subagent synthesizes survey into a proposed schema: universal fields +
   domain-specific fields + identity dimensions + normalization needs. Human reviews and
   approves. This prompt is one of the main targets for improvement through the outer loop.

3. **Test Extraction** — Extract the subset papers. One subagent per paper. May use web search
   for ambiguous questions — provenance tagged as `paper`, `derived`, or `external`.

4. **Schema Refinement** — Analyze extraction results, identify schema gaps, propose changes.
   Human approves schema changes. Re-extract affected papers. Iterate on subset.

5. **Full Extraction** — Schema stable, extract all remaining papers. Continue discovering
   normalization entries. Progressive subset expansion to detect overfitting.

6. **Convergence Check** — Can you build the meta-analysis from this data? (C8 test)

### System 2: Outer Loop (Improve How the Work is Done)
Progression monitoring + improvement system. Observes System 1's outputs and metrics, records
problems, creates critique, modifies prompts/instructions (version controlled), kicks off the
next iteration. **Implementation-blind** — sees inputs, outputs, and metrics, not internal

### Separation Principle
System 2 improves behavior by modifying instruction files, not by patching agent code. It has
visibility into System 1 via:
- **Structured workflow trace** — which agents ran, what they produced, timing, token usage
- **System 1 meta-commentary** — agents record reflections on what worked/didn't, what was
  ambiguous, what required inference vs direct extraction (C14)
- **Verification results** — what went wrong, error taxonomy, field-level metrics
- **Current instruction files** — what agents were told to do
- **Progression metrics** — across iterations, including convergence signals

## Agentic Design Principles

1. **Isolated context per subagent** — each subagent gets only what it needs. No shared mutable state during execution. Communicate via files.
2. **Context window discipline everywhere** — limited context is a constraint at every step. Multiple focused passes rather than trying to hold everything in one context. Deep research outputs must be synthesized to fit.
3. **Instructions-as-config** — extraction and schema behavior lives in versioned markdown instruction files, not code. Enables System 2 (critic) to propose changes.
4. **Provenance on everything** — every extracted value tagged with source: `paper` (from PDF text), `derived` (calculated), or `external` (web search / database lookup).
5. **One subagent per paper** for extraction tasks. Parallelizable and context-isolated.
6. **Cheap models for survey, expensive models for extraction** — use the right model tier for each task.
7. **Progressive subset expansion** — start with small test batch, expand as schema stabilizes, detect overfitting by measuring performance on new papers.
8. **Human-in-the-loop for schema decisions** — schema changes require human approval. Extraction review can be automated for benchmark runs.
9. **System 1 meta-commentary** — all agents record reflections on their process (what worked, what didn't, ambiguities, inference vs direct extraction). Feeds System 2 and supports auditability.
10. **Gold standard is assumed imperfect** — track a disputed values log for cases where the system consistently disagrees with human extraction. Consistent disagreement = flag for review, not automatic failure.

## Agent Roles

### Shared Infrastructure
- **PaperQA2 index** — retrieval backbone shared across agents; ingests corpus once, provides citation-grounded Q&A [Q14]
- **Biomedical API layer** — PubTator 3.0, UMLS, scispaCy for normalization seeding [Q15]

### System 1 Agents
1. **Survey Agent** (per paper) — cheap/fast model, catalogs what data exists in a paper; queries PaperQA2 for retrieval
2. **Subset Selector Agent** — reads all surveys, picks diverse test batch
3. **Schema Proposer Agent** — synthesizes surveys into schema proposal; uses PaperQA2 for cross-corpus queries
4. **Extraction Agent** (per paper) — extracts data into current schema, with web search capability; uses PaperQA2 for targeted retrieval
5. **Normalization Agent** — separate pass mapping raw values to canonical IDs; seeded by PubTator/UMLS/scispaCy
6. **Validation Agent** — deterministic checks + LLM-based semantic review

### System 2 Agents
6. **Progression Monitor** — tracks metrics across iterations, records problems
7. **Critic Agent** — analyzes failure patterns, proposes instruction/prompt changes
8. **Human-in-the-Loop** — approves/edits/rejects schema changes and instruction changes

## Current Phase: Build


## Tech Stack (MVP)

- **Claude Code** — primary multi-agent development harness (subagents, skills, hooks)
- **PaperQA2** — retrieval backbone for scientific paper Q&A with citation provenance (MIT, self-hosted) [Q14]
- **PubTator 3.0 + UMLS + scispaCy** — biomedical terminology APIs for normalization seeding [Q15]
- **PyMuPDF4LLM + Claude vision** — PDF ingestion (markdown conversion + complex table extraction) [Q2]
- **Anthropic Structured Outputs + Instructor** — structured extraction with schema enforcement [Q3]
- **Custom pytest + pandas** — evaluation harness [Q1]

## Tech Stack (Long-term)

- **OpenAI Agent SDK** (with Temporal flag) — durable workflow execution
- **Temporal** — orchestration engine (workflows + activities)
- **Go** — workers and core API

- **R Shiny** — meta-analysis tool (forest plots, odds ratios)


- **~106 DPYD PDFs** — scientific papers, naming convention: `Author Year (Journal) [PMID].pdf`
- See `docs/context/data-inventory.md` for complete inventory

## Detailed Context

See `docs/context/` for deep-dive reference documents:
- `project-vision.md` — problem, vision, differentiators, end-to-end flow
- `atomic-rows.md` — core data model, characteristics, role in system
- `dpyd-pilot.md` — pharmacogenetics use case, domain fields, gold standard benchmark

- `risks-and-mitigations.md` — sycophancy, copyright, quality control, technical risks
- `mvp-scope.md` — deliverables, priorities, development approach, inputs needed

See `docs/research/` for completed research outputs.

## Working Conventions

### Git Commits
- **Commit incrementally** — after each meaningful step (new module, passing test suite, etc.), not in big batches
- **Commit messages**: imperative mood, concise first line, body explains why not how
- **Use HEREDOC** for multi-line commit messages
- **Don't batch** — one logical change per commit

### Session Behavior
- **Never suggest stopping** — only the user decides when the session is over
- **After completing a step**: state what's done, state what's next, proceed
