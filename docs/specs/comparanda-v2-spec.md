# Comparanda v2 — System Specification

> Produced 2026-04-03. Updated 2026-04-03 with final architecture decisions.
> This spec supersedes the v1 build plan. v1 code is the starting point for migration.

---

## 1. Problem Statement

Comparanda v1 achieved:
- A working pipeline (survey → schema proposal → extraction → normalization → eval)
- A System 2 improvement loop (critic → instruction changes → re-extract)
- 108 tests passing, end-to-end orchestrator, PaperQA2 RAG integration

But hit fundamental blockers:
- **extraction_quality = 0.0** across all iterations — structural schema mismatch (binary rows vs multi-group), missing identity dimensions (`comparison_group`), entity undersampling
- **No inner loop** — System 1 runs single-pass per iteration; schema never refines within a cycle
- **Deterministic eval can't score across schema mismatch** — the system discovers its own schema but eval requires alignment with a differently-structured gold standard
- **Rate limit crashes** — 8 papers × concurrent Sonnet calls exhausted API quota
- **Missing agents** — MA Critique Agent not built; no meta-analysis readiness signal; no schema refinement step
- **System 2 can't fix structural problems** — it modifies instruction markdown but can't change the schema JSON or add missing dimensions
- **No separation between tool and project** — System 1 code, System 2 code, domain data, and benchmark data all mixed in one flat structure

v2 addresses all of the above through architectural redesign.

---

## 2. Core Principles

### 2.1 Comparanda is the product; the harness is a development tool

**Comparanda** is a standalone extraction system. Given a domain and a corpus of papers, it discovers a schema, extracts structured data, and iterates until the extraction is meta-analysis ready. It runs independently — no gold standard required.

**The calibration harness** (`harness/`) is a development tool that wraps comparanda. It evaluates comparanda's output against a gold standard benchmark, diagnoses instruction-level problems, and modifies comparanda's agent instructions to improve quality. The harness exists only during calibration — in production, comparanda runs alone.

The dependency is one-way: harness → comparanda, never the reverse. Comparanda has no knowledge of the harness.

### 2.2 Tool vs project data (plugin-ready)

Comparanda has two distinct concerns:

- **The tool**: agents, orchestration code, shared utilities. Domain-agnostic. Reusable across projects. Eventually an installable Claude Code plugin.
- **The project**: domain reference files, schema, extractions, observations, surveys, normalization tables. Domain-specific. One project per domain (e.g., `projects/dpyd/`).

This separation means comparanda can be applied to any domain by creating a new project directory. The tool stays the same; only the project data changes.

### 2.3 One file per agent

Each agent is defined in a single `.claude/agents/*.md` file containing: model selection, tool access, role description, AND universal behavioral instructions. No separate instruction files — the agent definition IS the instruction. This is the single source of truth for agent behavior in both Claude Code interactive mode and SDK programmatic mode.

The harness modifies the universal instruction sections of these files during calibration. It never modifies the frontmatter (model, tools, description) or domain reference pointers.

### 2.4 Git is the provenance mechanism

All project files (domain references, schema, observations, extractions) are git-tracked and committed incrementally as changes occur. Every agent that modifies project files must commit with a descriptive message. This provides:

- **Reproducibility**: checkout any commit to reproduce that state
- **Audit trail**: `git log` shows what changed, when, and why
- **No read-only constraints needed**: git history preserves every version

There are no "seed" vs "learned" file distinctions. Files evolve. Git tracks the evolution.

### 2.5 Two nested loops

```
COMPARANDA — Inner Loop (does the work)
  Phase 0:   Domain onboarding + prior MA extraction + reference doc generation
  Phase 0.5: Survey schema generation
  Phase 1:   Paper survey (all papers) → subset selection
  ┌─────────────────────────────────────────────────────┐
  │ Phase 2:   Schema proposal (rich format)            │
  │ Phase 3:   Extract → Normalize                      │
  │ Phase 3.5: MA Critique (convergence signal)         │
  │ Phase 4:   Apply schema changes from critique       │
  │            (only place schema changes happen)        │
  │ → loop until MA Critique says "ready" or cap hit    │
  └─────────────────────────────────────────────────────┘
  Phase 5:   Progressive expansion
  Phase 6:   Final convergence

HARNESS — Outer Loop (improves how the work is done)
  Run comparanda for N loops → Read outputs → Evaluate against benchmark
  → Diagnose → Modify agent instruction sections → Re-run comparanda
  → Loop until harness detects recurring instruction-caused problem
```

### 2.6 Separation of concerns

| Concern | Owner | What it modifies |
|---------|-------|-----------------|
| Schema structure + fields | Comparanda (MA Critique, Phase 4) | `projects/{domain}/schema/` |
| Domain knowledge | Comparanda (all agents) | `projects/{domain}/domain/` |
| Extraction data | Comparanda (extractor, normalizer) | `projects/{domain}/extractions/`, `normalization/` |
| Cross-iteration observations | Comparanda (all agents) | `projects/{domain}/observations/` |
| Universal agent behavior | Harness (critic) | `comparanda/.claude/agents/*.md` |
| Benchmark evaluation | Harness (eval) | `harness/iterations/` |

### 2.7 Commit-on-modify principle

**Every agent that modifies project files must commit the change with a descriptive message.** This applies to:
- MA Critique Agent → commits schema changes after Phase 4
- Extraction Agent → commits extraction output
- Normalization Agent → commits normalization table updates
- Schema Proposer → commits new schema version
- Survey Agent → commits survey output
- Any agent updating domain reference files → commits with rationale

The harness critic also commits when it modifies comparanda's agent files.

This ensures the git history is a complete record of every change, attributable to the agent that made it.

---

## 3. File Structure

### 3.1 Comparanda (the product)

```
comparanda/
├── .claude/
│   ├── agents/                          # ONE file per agent = definition + instructions
│   │   ├── survey.md
│   │   ├── schema-proposer.md
│   │   ├── extractor.md
│   │   ├── normalizer.md
│   │   ├── subset-selector.md
│   │   └── ma-critique.md
│   ├── skills/
│   │   └── comparanda/SKILL.md          # Pipeline knowledge
│   ├── commands/
│   │   ├── run-extraction.md            # Run inner loop
│   │   └── run-phase0.md                # Domain onboarding
│   └── settings.json
│
├── src/comparanda/                      # Python = orchestration + data plumbing
│   ├── __init__.py
│   ├── orchestrator.py                  # Inner loop controller
│   ├── data/
│   │   ├── pdf.py                       # PDF → markdown (PyMuPDF4LLM)
│   │   ├── rag.py                       # PaperQA2 index
│   │   ├── rich_schema.py               # Rich schema parse/write
│   │   └── observation_log.py           # Observation log read/write
│   └── shared/
│       ├── config.py                    # Path resolution, constants
│       └── sdk_utils.py                 # Token tracking, SDK helpers
│
├── projects/                            # Project workspaces (one per domain)
│   └── dpyd/                            # DPYD pharmacogenetics project
│       ├── domain/                      # Domain knowledge (evolves, git-tracked)
│       │   ├── survey_ref.md
│       │   ├── schema_ref.md
│       │   ├── extraction_ref.md
│       │   ├── normalization_ref.md
│       │   ├── ma_critique_ref.md
│       │   └── deep_research.md
│       ├── schema/                      # Rich schema (versioned YAML)
│       │   ├── v001.yaml
│       │   └── current -> v001.yaml
│       ├── observations/                # System 1 observation log
│       │   └── log.jsonl
│       ├── surveys/                     # Phase 1 output (1 JSON per paper)
│       ├── extractions/                 # Phase 3 output (1 JSONL per paper)
│       ├── normalization/               # Normalization tables
│       └── data -> /path/to/pdfs        # Symlink to corpus PDFs
│
├── tests/
├── CLAUDE.md                            # Interactive orchestrator
├── pyproject.toml
└── .gitignore
```

### 3.2 Calibration Harness

```
harness/
├── .claude/
│   ├── agents/
│   │   └── critic.md                    # Definition + instructions, all in one
│   ├── skills/
│   │   └── harness/SKILL.md             # Harness knowledge
│   ├── commands/
│   │   ├── run-pipeline.md              # Full calibration cycle
│   │   └── run-improvement.md           # One improvement iteration
│   └── settings.json
│
├── src/harness/                         # Python = outer loop + eval
│   ├── __init__.py
│   ├── orchestrator.py                  # Outer loop — runs comparanda, evaluates, improves
│   ├── eval/
│   │   ├── matchers.py                  # Field-level comparison
│   │   ├── alignment.py                 # Row alignment
│   │   ├── scorer.py                    # Aggregate scoring
│   │   ├── regression.py                # Regression detection
│   │   ├── schema_bridge.py             # Schema mapping
│   │   └── schema_config.py             # Field configuration
│   ├── judge.py                         # LLM judge (semantic evaluation)
│   ├── lessons.py                       # Lesson store
│   ├── trajectory.py                    # Iteration tracking
│   ├── runner.py                        # Improvement decision logic
│   └── versioning.py                    # Agent file version tracking
│
├── benchmark/                           # Gold standard + eval config
│   ├── gold_standard/                   # Spreadsheets (DPYD-specific)
│   ├── eval_config/                     # paper_pmid_map.json, schema_config.json
│   └── prior_meta_analyses/             # Papers for Phase 0 extraction
│
├── iterations/                          # Improvement tracking
│   ├── trajectory.json
│   ├── lessons.json
│   └── iter_{N:03d}/
│       ├── eval_results.json
│       ├── critic_proposal.json
│       └── agent_snapshots/             # Copies of agent .md files at this iteration
│
├── tests/
├── CLAUDE.md                            # Harness orchestrator instructions
├── pyproject.toml                       # Depends on comparanda package
└── .gitignore
```

### 3.3 Future: Plugin structure

When comparanda stabilizes, the tool extracts as a Claude Code plugin:

```
comparanda-plugin/                       # Installable plugin
├── plugin.json
├── agents/                              # Agent definitions
├── skills/
├── commands/
├── hooks/
└── src/comparanda/                      # Python package

any-research-project/                    # A project that uses the plugin
├── .claude/settings.json                # References comparanda-plugin
├── domain/
├── schema/
├── observations/
├── surveys/
├── extractions/
├── normalization/
└── data/
```

The migration is mechanical: move tool files into plugin layout, project files stay in the project directory. No architectural changes needed.

---

## 4. Phase Pipeline

### Phase 0 — Domain Onboarding (Enhanced)

**Goal:** Build comprehensive domain context from multiple sources.

**Steps:**
1. **User scoping questionnaire** — defines domain, target papers, research questions
2. **Deep research** — API-driven domain research (produces `domain/deep_research.md`)
3. **Prior meta-analysis acquisition** — find and obtain existing meta-analyses for this domain (semi-manual for DPYD; automated agent long-term). See `docs/research/phase0-paper-list.md` for DPYD papers.
4. **Prior MA extraction** — structured extraction from each prior meta-analysis:
   - Field inventory (what schema did they use?)
   - Granularity decisions, statistical models, inclusion/exclusion criteria
   - Normalization approaches, terminology evolution
   - Lessons learned, domain traps, reporting gaps
5. **Best-practice synthesis** — extract guidance from methodology papers (PRISMA, Cochrane, CPIC)
6. **Agent reference doc generation** — each agent runs in "reference generation" mode: reads all prior research, extracts what's relevant to its role, writes its domain reference file to `projects/{domain}/domain/`

**Outputs:** `projects/{domain}/domain/*.md` files — one per agent, git-committed.

**Commit:** "Phase 0: Generate domain reference files for {domain} from prior research"

### Phase 0.5 — Survey Schema Generation

Schema Generator Agent reads domain context + universal fields → produces `survey_schema.json`.

Unchanged from v1.

### Phase 1 — Paper Survey

- Survey Agents (1 Haiku per paper, parallel, `MAX_CONCURRENT_SURVEYS = 5`)
- Subset Selector picks **3-4 papers** (down from 7-8)
- **Subset rotation**: each inner loop iteration uses a different 3-4 paper subset to avoid overfitting. Some overlap allowed for regression detection. Selector tracks used papers in `observations/log.jsonl`.

**Commit:** "Phase 1: Survey {N} papers, select subset [{paper_ids}]"

### Phase 2 — Schema Proposal

Schema Proposer reads:
- All survey outputs
- Domain reference files (`domain/schema_ref.md`)
- Observation log (schema rationale entries — prevents re-adding removed fields)

Produces: **Rich schema** (see Section 5.1) at `schema/v{N}.yaml`.

On first inner loop iteration: proposes from scratch.
On subsequent iterations: reads MA Critique feedback and modifies existing schema.

**Commit:** "Phase 2: Propose schema v{N} — {N_fields} fields, {summary of changes}"

### Phase 3 — Extraction + Normalization

**Phase 3a — Extraction:**
- Extraction Agents (1 per paper, `MAX_CONCURRENT_EXTRACTIONS = 2`)
- Reads: rich schema (with per-field extraction guidance), domain reference, survey output, RAG passages
- Produces: JSONL atomic rows + meta-commentary
- Writes to observation log: paper-specific flags, ambiguities, extraction difficulties

**Phase 3b — Normalization:**
- Two-stage: deterministic table lookups → batched LLM for unknowns
- Normalization tables accumulate across iterations
- Writes to observation log: new terminology, normalization conflicts

**Commit:** "Phase 3: Extract {N} papers ({total_rows} rows), normalize ({resolved} resolved, {disputes} disputes)"

### Phase 3.5 — MA Critique (Convergence Signal)

The **MA Critique Agent** is the unified meta-analysis readiness + gap analysis agent. This is the convergence signal for comparanda's inner loop.

**Reads:**
- Normalized extraction rows (current iteration)
- Current rich schema
- Domain reference (`domain/ma_critique_ref.md`)
- Deep research report (`domain/deep_research.md`)
- Observation log (including its own prior feedback — prevents cycling)

**Produces:**
- Structured critique:
  - Missing fields needed for meta-analysis (with rationale)
  - Over-specified fields to remove (with removal rationale)
  - Granularity issues (too coarse / too fine)
  - Pooling blockers (can't derive effect sizes, can't pool)
  - Field extraction guidance updates
  - Cross-paper consistency issues
- Convergence assessment: "meta-analysis ready" or "not ready, here's why"
- Writes to observation log: schema rationale, domain patterns

**Convergence logic:**
- "Ready" when: extraction suitable for pooling, effect sizes derivable, inclusion/exclusion decisions possible without returning to papers
- Or: critique is stable (same feedback repeated → escalate, may need harness intervention)

**Commit:** "Phase 3.5: MA critique — {ready|not ready}, {N} schema changes proposed"

### Phase 4 — Schema Refinement

**The only place schema changes happen.**

Based on MA Critique output:
- Apply field additions (with extraction guidance)
- Apply field removals (with removal rationale in observation log)
- Update field extraction guidance
- Update identity dimensions
- Version the schema (v001 → v002 → ...)
- Update domain reference files if MA Critique discovered domain patterns

This is deterministic application of the critique's structured output, not another LLM call.

**Commit:** "Phase 4: Schema v{N} → v{N+1} — added [{fields}], removed [{fields}], modified [{fields}]"

### Inner Loop Iteration

After Phase 4:
- If MA Critique said "ready" → exit inner loop, proceed to Phase 5
- If not ready → rotate subset (select new 3-4 papers), return to Phase 3
- Phase 2 (full re-proposal) only on first iteration; subsequent iterations apply Phase 4 refinements
- Hard cap: configurable (default 5 inner loop iterations)

### Phase 5 — Progressive Expansion

After inner loop converges:
- Expand to next batch of papers
- Monitor for metric regression on new papers (overfitting detection)
- If regression → re-enter inner loop with expanded set

### Phase 6 — Final Convergence

- MA Critique Agent runs on full dataset
- "Meta-analysis ready?" across all papers
- If no → back to schema refinement
- If yes → produce final outputs

**Final outputs:** Rich schema, atomic rows (JSONL), normalization tables

---

## 5. Data Architecture

### 5.1 Rich Schema Format

Each field carries extraction guidance, normalization pointers, and lifecycle metadata:

```yaml
schema_version: "v002"
domain: "dpyd_pharmacogenetics"
created_iteration: 1
last_modified_iteration: 3

identity_dimensions:
  - field: pmid
    description: "PubMed identifier — unique study key"
  - field: variant_raw
    description: "Genetic variant as reported in the paper"
  - field: outcome_category
    description: "Clinical outcome being measured"
  - field: comparison_group
    description: "Which categorical group this row represents"
    extraction_guidance: |
      For codominant models: extract separate rows for each genotype (AA, AB, BB).
      For dominant models: extract carrier vs non-carrier.
      Always extract the reference/wildtype group as a distinct row.

fields:
  - name: genotype_group
    type: string
    identity_dimension: true
    required: true
    extraction_guidance: |
      The categorical genetic group this row represents.
      Must match one of the groups reported in the study's statistical model.
    normalization: genotype_lookup_table
    added_iteration: 1
    removal_rationale: null  # populated when removed, prevents re-addition

  - name: hazard_ratio
    type: number
    required: false
    extraction_guidance: |
      Extract the point estimate as reported.
      Do not convert between HR and OR — use the field matching what the study reports.
    added_iteration: 1
    removal_rationale: null
```

**Key properties:**
- `extraction_guidance` per field replaces field-specific content from the old `extraction.md`
- `removal_rationale` prevents re-addition of removed fields (MA Critique reads this)
- `identity_dimensions` are explicit
- Schema versioned with iteration provenance per field

### 5.2 Observation Log

Structured, append-only log. All agents read from and write to it.

**File:** `projects/{domain}/observations/log.jsonl`

**Entry format:**
```json
{
  "id": "OBS-007",
  "iteration": 3,
  "phase": "3.5_ma_critique",
  "agent": "ma_critique",
  "category": "schema_rationale",
  "content": "Removed 'toxicity_binary' — collapses grade-level data needed for dose-response. Replaced with 'toxicity_grade' + 'toxicity_grade_system'.",
  "related_fields": ["toxicity_binary", "toxicity_grade", "toxicity_grade_system"],
  "timestamp": "2026-04-03T14:22:00Z"
}
```

**Categories:**
- `schema_rationale` — why fields were added/removed/modified
- `domain_pattern` — domain-specific patterns discovered during processing
- `paper_flag` — per-paper notes: unusual format, missing data, ambiguities
- `normalization_note` — terminology conflicts, new mappings
- `extraction_difficulty` — challenges that may inform instruction updates

**Retention:** Grows across all iterations. For context window management, agents receive all `schema_rationale` entries (critical for preventing cycles) + recent entries from other categories.

### 5.3 Domain Reference Files

One per agent, stored in `projects/{domain}/domain/`:

| File | Agent(s) that read it | Content |
|------|----------------------|---------|
| `survey_ref.md` | Survey | Domain-specific entities, naming patterns, expected study designs |
| `schema_ref.md` | Schema Proposer | Prior MA field inventories, granularity decisions |
| `extraction_ref.md` | Extractor | What good atomic rows look like, extraction pitfalls |
| `normalization_ref.md` | Normalizer | Terminology evolution, code tables from prior MAs |
| `ma_critique_ref.md` | MA Critique | Prior MA schemas, statistical models, domain traps |
| `deep_research.md` | All agents | Comprehensive domain research output |

Created during Phase 0 by agents in "reference generation" mode. Updated by agents during processing as they discover new domain knowledge. All changes git-committed.

---

## 6. Agent Definitions

### 6.1 Agent file structure

Each agent is a single `.claude/agents/*.md` file:

```markdown
---
model: sonnet-4-6
description: One-line description for Claude Code agent picker
tools: [Read, Write, Bash]
---

# Role
You are a [role description].

# Universal Instructions
[Behavioral rules, principles, methodology — THIS SECTION is what the
harness critic modifies during calibration]

# Domain Reference
Read your domain reference file at: projects/{domain}/{ref_file}.md
Read the observation log at: projects/{domain}/observations/log.jsonl

# Output Format
[What to produce and where to write it]

# Git
After completing your work, commit all modified files with a descriptive message.
```

### 6.2 Agent roster

#### Survey Agent
- **Model:** Haiku (cheapest)
- **File:** `.claude/agents/survey.md`
- **Role:** Inventory what extractable data exists in each paper
- **Universal instructions:** What to look for (study metadata, tables, outcomes, variants, demographics), output schema, meta-commentary guidelines
- **Reads:** Paper text (markdown), survey schema, domain reference
- **Writes:** `surveys/{paper_id}.json`, observation log (paper flags)
- **Commits:** survey output

#### Subset Selector Agent
- **Model:** Sonnet
- **File:** `.claude/agents/subset-selector.md`
- **Role:** Pick diverse paper subsets for extraction
- **Universal instructions:** Diversity maximization, rotation strategy, overlap for regression detection
- **Reads:** All surveys, observation log (subset history)
- **Writes:** Observation log (subset selection rationale)
- **v2 change:** Tracks previously-used papers, rotates 3-4 per iteration

#### Schema Proposer Agent
- **Model:** Sonnet
- **File:** `.claude/agents/schema-proposer.md`
- **Role:** Synthesize surveys into a rich schema with extraction guidance
- **Universal instructions:** How to propose fields, identity dimension principles, field granularity framework, rich schema format
- **Reads:** Survey outputs, domain reference, observation log (schema rationale — must not re-add removed fields)
- **Writes:** `schema/v{N}.yaml`, observation log (schema rationale)
- **Commits:** new schema version

#### Extraction Agent (per paper)
- **Model:** Sonnet
- **File:** `.claude/agents/extractor.md`
- **Role:** Extract atomic rows from a paper into the current schema
- **Universal instructions:** Atomic row definition, provenance rules, Cartesian expansion, completeness. Field-specific guidance comes from the rich schema, NOT from this section.
- **Reads:** Rich schema (with per-field extraction guidance), domain reference, survey output, RAG passages
- **Writes:** `extractions/{paper_id}.jsonl`, observation log (paper flags, extraction difficulties)
- **Commits:** extraction output

#### Normalization Agent
- **Model:** Sonnet (LLM phase only)
- **File:** `.claude/agents/normalizer.md`
- **Role:** Map raw extracted values to canonical IDs
- **Universal instructions:** Two-stage process (deterministic → LLM), confidence thresholds, dispute handling
- **Reads:** Extractions, normalization tables, domain reference, schema (normalizable fields)
- **Writes:** Updated extractions, normalization tables, observation log (normalization notes)
- **Commits:** normalization table updates + modified extractions

#### MA Critique Agent
- **Model:** Sonnet (or Opus for complex critiques)
- **File:** `.claude/agents/ma-critique.md`
- **Role:** Evaluate extraction for meta-analysis readiness, propose schema changes
- **Universal instructions:** Meta-analysis expertise — statistical models, inclusion/exclusion reasoning, effect size requirements, pooling requirements, PRISMA/Cochrane principles. How to evaluate data granularity. How to detect over/under-specification.
- **Reads:** Normalized extractions, current schema, domain reference, deep research, observation log (including own prior feedback)
- **Writes:** Structured critique output, observation log (schema rationale, domain patterns), updated domain reference files
- **Commits:** critique output + any domain reference updates

### 6.3 Reference generation mode

During Phase 0, each agent is invoked with a special instruction prefix:

```
You are running in REFERENCE GENERATION mode.

Read the following research materials:
- Deep research report: [path]
- Prior meta-analysis extractions: [paths]
- Best-practice papers: [paths]

Extract what is relevant to YOUR ROLE and write a domain reference file
to: projects/{domain}/domain/{your_ref_file}.md

This file should contain domain-specific knowledge that will help you
perform your operational role. Include: relevant patterns, terminology,
pitfalls, examples, and any domain context that won't be in your
universal instructions.
```

The agent's universal instructions (in the .md file) define its role; the reference generation mode tells it to create domain context for that role.

---

## 7. Calibration Harness

### 7.1 Role

The harness is an external operator. It:
1. Runs comparanda (via CLI or SDK) for N inner loop iterations
2. Reads comparanda's outputs (extractions, schema, observations)
3. Evaluates against a gold standard benchmark
4. Diagnoses instruction-level problems
5. Modifies the universal instruction sections of comparanda's agent .md files
6. Re-runs comparanda and measures improvement

### 7.2 Running comparanda

**Script mode:**
```bash
# Run 1 inner loop iteration
uv run python -m comparanda.orchestrator \
  --project-dir ../comparanda/projects/dpyd \
  --loops 1

# Run 3 loops before returning control
uv run python -m comparanda.orchestrator \
  --project-dir ../comparanda/projects/dpyd \
  --loops 3
```

**SDK mode:**
```python
from comparanda.orchestrator import System1Orchestrator

s1 = System1Orchestrator(project_dir="../comparanda/projects/dpyd")
result = await s1.run_loops(n=1)
```

The harness decides how many loops to let comparanda run before intervening — tight feedback (1 loop) or loose (3+), based on trajectory.

### 7.3 Evaluation architecture

**LLM-judge-primary.** The core scoring mechanism is semantic comparison:
- Does the extraction capture the same information as the gold standard, regardless of schema differences?
- Are findings equivalent even if organized differently?
- Per-study: "did we get the same findings?"

**Deterministic layer retained as fast sanity checks:**
- Row count comparison (right ballpark?)
- Paper coverage (same studies?)
- Numeric value proximity (where directly comparable)
- These inform the LLM judge but don't drive the composite score

**Composite score:** Weighted LLM judge scores:
- Information completeness
- Information accuracy
- Schema quality for meta-analysis
- Weights TBD after initial calibration runs

### 7.4 Harness convergence

No prescribed iteration count. The harness loops until:
- It detects a recurring problem that IS caused by universal agent instructions (and can propose a fix)
- OR composite score plateaus and remaining gaps are domain-specific (not instruction-fixable)
- Hard safety cap: configurable (default 10)

### 7.5 Modification scope

The harness critic modifies ONLY the universal instruction sections of comparanda's agent .md files. It never modifies:
- Agent frontmatter (model, tools, description)
- Domain reference pointers
- Project data (schema, extractions, observations, domain files)

After applying changes, the critic commits to comparanda's git repo with a message explaining the rationale.

---

## 8. CLAUDE.md Files

### 8.1 Comparanda CLAUDE.md (interactive orchestrator)

When working interactively in `comparanda/`, CLAUDE.md instructs Claude to act as the inner loop orchestrator:

```markdown
# Comparanda — Interactive Orchestrator

## What This Is
Comparanda discovers structured schemas from scientific paper corpora
and extracts data suitable for meta-analysis.

## Current Project
Check projects/ for active projects. Each has domain/, schema/,
observations/, surveys/, extractions/, normalization/.

## Workflow

### Phase 0 — Domain Onboarding (run once per domain)
1. Create projects/{domain}/
2. Run deep research → domain/deep_research.md
3. Acquire prior meta-analyses → extract schemas/lessons
4. Invoke each agent in reference generation mode → domain/*_ref.md
5. Commit: "Phase 0: Initialize {domain} project"

### Inner Loop (repeat until MA Critique says "ready")
1. Invoke @survey for unsurveyed papers
2. Invoke @subset-selector for 3-4 diverse papers
3. Invoke @extractor for each subset paper
4. Invoke @normalizer on extractions
5. Invoke @ma-critique — read convergence assessment
6. If not converged: apply schema changes (Phase 4), rotate subset, repeat
7. Each agent commits its own output

### Rules
- Schema changes ONLY happen after MA Critique (Phase 4)
- Every file modification gets a git commit
- Read observation log before each phase to maintain context
```

### 8.2 Harness CLAUDE.md (calibration orchestrator)

```markdown
# Comparanda Calibration Harness

## What This Is
Development tool that improves Comparanda's agent instructions by
evaluating output against a gold standard benchmark.

## Running Comparanda
Run comparanda's inner loop:
  uv run python -m comparanda.orchestrator \
    --project-dir ../comparanda/projects/dpyd --loops N

Read results from:
- ../comparanda/projects/dpyd/observations/log.jsonl
- ../comparanda/projects/dpyd/extractions/*.jsonl
- ../comparanda/projects/dpyd/schema/current.yaml

## Improvement Cycle
1. Run comparanda for N loops (your judgment: 1 for tight feedback, 3+ to let it settle)
2. Evaluate: run benchmark eval against benchmark/gold_standard/
3. Invoke @critic with eval results + comparanda observations
4. Apply critic's changes to ../comparanda/.claude/agents/*.md
   (universal instruction sections ONLY)
5. Commit changes to comparanda repo with rationale
6. Re-run comparanda, compare scores
7. Keep or revert based on improvement

## Convergence
Stop when: remaining quality gaps are domain-specific (not fixable by
instruction changes), or recurring problems persist after intervention.
```

---

## 9. Tactical Fixes

Applied to v1 code during migration, independent of architectural changes.

### 9.1 Concurrency
- `MAX_CONCURRENT_EXTRACTIONS`: 3 → 2
- Add exponential backoff: base 2s, max 60s, 3 retries on rate limit / server error

### 9.2 Subset size
- Default: 3-4 papers (down from 7-8)
- Rotate per inner loop iteration

### 9.3 Convergence safeguards
- Comparanda: MA Critique-driven (not score-based)
- Harness: minimum 3 iterations before convergence check
- Never declare convergence at extraction_quality = 0.0

---

## 10. Migration Plan

### From v1 to v2


**What exists in v1:**
- `.claude/agents/*.md` — 6 agent definitions (thin, reference instruction files)
- `src/comparanda/instructions/*.md` — 8 instruction files
- `src/comparanda/agents/*.py` — 5 Python agent wrappers
- `src/comparanda/loop/*.py` — 6 System 2 modules
- `src/comparanda/eval/*.py` — 7 eval modules
- `src/comparanda/data/*.py` — 3 data modules
- `src/comparanda/orchestrator.py` — 1064-line monolith (both loops)
- `surveys/`, `extractions/`, `schema/`, `normalization/`, `iterations/` — project data
- `tests/` — 108 passing tests

### Wave 0: Research prerequisites
- [x] Prompt engineering reference (`docs/research/prompt-engineering-reference.md`)
- [x] Paper acquisition list (`docs/research/phase0-paper-list.md`)
- [ ] Acquire priority papers (Le Teuff 2024, Meulendijks 2015, CPIC 2017, PRISMA 2020)
- [ ] Extract schemas/lessons from acquired prior MA papers

### Wave 1: Restructure (no new functionality)
- [ ] Create `comparanda/` directory with new layout
- [ ] Merge agent .md definitions + instruction .md files into single agent .md files
- [ ] Move project data into `projects/dpyd/` workspace
- [ ] Create `harness/` directory
- [ ] Move eval + loop code into harness
- [ ] Remove Python agent wrappers (agents defined by .md files, invoked by orchestrator via SDK)
- [ ] Split monolith orchestrator into comparanda orchestrator + harness orchestrator
- [ ] Update imports, fix tests
- [ ] Write CLAUDE.md for both projects

### Wave 2: Core new capability
- [ ] Write MA Critique Agent (.claude/agents/ma-critique.md)
- [ ] Implement rich schema format (YAML parser, writer, versioning)
- [ ] Implement observation log (read/write APIs, JSONL format)
- [ ] Add reference generation mode to all agent prompts
- [ ] Implement subset rotation in subset selector
- [ ] Add commit-on-modify to all agents
- [ ] Add `--loops N` flag to orchestrator CLI

### Wave 3: Wire inner loop
- [ ] Implement Phase 3.5 (MA Critique) in orchestrator
- [ ] Implement Phase 4 (schema refinement from critique output)
- [ ] Wire inner loop: Phase 2 → 3 → 3.5 → 4 → loop
- [ ] Phase 0 orchestration (reference generation mode invocations)

### Wave 4: Harness eval redesign
- [ ] LLM judge as primary scorer
- [ ] Deterministic layer as sanity checks
- [ ] New composite score formula
- [ ] Harness convergence logic (detect recurring instruction-caused problems)
- [ ] Harness orchestrator: run comparanda N loops → evaluate → critique → apply

### Wave 5: Expansion
- [ ] Phase 5 progressive expansion
- [ ] Phase 6 final convergence
- [ ] Automated prior MA paper discovery agent
- [ ] Plugin extraction (tool → plugin, project stays)

---

## 11. Open Items

### 11.1 Observation log boundaries per agent
Each agent writes to both primary output files AND the observation log. Per-agent boundaries (what goes where) to be defined during Wave 2.

### 11.2 Rich schema file format details
YAML with JSON Schema validation layer. Exact structure to be finalized during Wave 2 implementation.

### 11.3 LLM judge scoring dimensions and weights
Need calibration after first successful inner loop run. Initial dimensions proposed (completeness, accuracy, schema quality) but weights TBD.

### 11.4 Subset rotation overlap strategy
How much overlap between rotated subsets? Proposal: 1 anchor paper + 2-3 rotating. Needs experimentation.

### 11.5 Context window budget per agent
Token budgets for: universal instructions, domain reference, observation log entries, task-specific input. To be defined based on model limits and prompt engineering reference.

### 11.6 Agent SDK invocation patterns
Exact SDK API for invoking .claude/agents/*.md files programmatically. Verify `setting_sources=["project"]` discovers agent definitions correctly. Test hot-reload behavior.

---

## 12. References

### Architecture
- Phase pipeline diagram: `docs/presentation/system-architecture-v1/02-phase-pipeline.mmd`
- System 2 loop diagram: `docs/presentation/system-architecture-v1/03-system2-loop.mmd`
- Foundational principles: `docs/presentation/system-architecture-v1/00-principles.md`

### Research
- Prompt engineering reference: `docs/research/prompt-engineering-reference.md`
- Paper acquisition list: `docs/research/phase0-paper-list.md`

### v1 Reference
- v1 orchestrator: `src/comparanda/orchestrator.py`
- v1 CLAUDE.md: `CLAUDE.md`
