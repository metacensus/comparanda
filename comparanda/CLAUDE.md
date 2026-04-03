# Comparanda

Scientific paper schema extraction system. Given a domain and a corpus of papers, Comparanda discovers a structured schema and extracts data into atomic rows suitable for meta-analysis.

## Active Project

Project: `projects/dpyd/` (DPYD pharmacogenetics)

## How This Works

You are the orchestrator. Follow the phases below. Agents are in `.claude/agents/`. Invoke them with @agent-name. All project state is in `projects/dpyd/`.

### Key Files
- **Schema:** `projects/dpyd/schema/` — rich YAML, versioned (v001.yaml, v002.yaml, ...)
- **Observations:** `projects/dpyd/observations/log.jsonl` — append-only structured log
- **Domain refs:** `projects/dpyd/domain/` — per-agent domain knowledge
- **Surveys:** `projects/dpyd/surveys/{paper_id}.json`
- **Extractions:** `projects/dpyd/extractions/{paper_id}.jsonl`
- **Normalization:** `projects/dpyd/normalization/{table}.json`
- **Critiques:** `projects/dpyd/critiques/iter_{NNN}.json`
- **PDFs:** `projects/dpyd/data/` (symlink to corpus)
- **Phase 0 papers:** `data/raw/pdfs/phase0-references/`

### Observation Log Format
Append one JSON object per line to `projects/dpyd/observations/log.jsonl`:
```json
{"id": "OBS-001", "iteration": 1, "phase": "3.5_ma_critique", "agent": "ma_critique", "category": "schema_rationale", "content": "...", "related_fields": [], "timestamp": "2026-04-03T14:00:00Z"}
```
Categories: `schema_rationale`, `domain_pattern`, `paper_flag`, `normalization_note`, `extraction_difficulty`

### Rich Schema Format
Schema files are YAML at `projects/dpyd/schema/v{NNN}.yaml`:
```yaml
schema_version: "v001"
domain: "dpyd_pharmacogenetics"
created_iteration: 1
last_modified_iteration: 1

identity_dimensions:
  - field: pmid
    description: "PubMed identifier"
  - field: variant_raw
    description: "Genetic variant as reported"
  - field: outcome_category
    description: "Clinical outcome measured"
  - field: comparison_group
    description: "Categorical group this row represents"

fields:
  - name: pmid
    type: integer
    required: true
    identity_dimension: true
    extraction_guidance: "PubMed ID of the paper"
    normalization: null
    added_iteration: 1
    removal_rationale: null
```

### Git Protocol
Commit after each phase completes. Message format: `"Phase {N}: {what changed}"`

---

## RAG Index Build

**Start this immediately on first run — it runs in the background while other phases proceed.**

The PaperQA2 RAG index over the corpus papers is used by the Extraction Agent in Phase 3 for targeted passage retrieval instead of full-paper reads. Building it takes time and costs money (OpenAI API for chunk summarization).

```python
# Build via Python:
from comparanda.data.rag import create_paper_index
index = await create_paper_index(pdf_dir="projects/dpyd/data", cache_dir=".rag_cache")
```

Or run in background: `uv run python -c "import asyncio; from comparanda.data.rag import create_paper_index; asyncio.run(create_paper_index(pdf_dir='projects/dpyd/data', cache_dir='.rag_cache'))" &`

The cache persists at `.rag_cache/` — do NOT delete it. It is NOT gitignored because it costs money to rebuild.

If the cache already exists, this step is instant (loads from disk).

---

## Phase 0 — Domain Onboarding

> Run once per new domain. For DPYD, deep research and seed knowledge already exist.

### Prerequisites
Check that these exist:
- `projects/dpyd/domain/deep_research.md` — deep research report
- `projects/dpyd/domain/dpyd_seed_knowledge.md` — seed domain knowledge
- `data/raw/pdfs/phase0-references/` — methodology + prior MA papers

### Steps

1. **Generate domain reference files.** For each agent that has a domain reference:

   Invoke the agent with this prefix before its normal prompt:
   ```
   REFERENCE GENERATION MODE

   Read the following materials and extract what is relevant to your role:
   - Deep research: projects/dpyd/domain/deep_research.md
   - Seed knowledge: projects/dpyd/domain/dpyd_seed_knowledge.md
   - Phase 0 papers: [list relevant PDFs from data/raw/pdfs/phase0-references/]

   Write your domain reference file to: projects/dpyd/domain/{agent}_ref.md

   Include domain-specific knowledge that will help you perform your operational
   role: patterns, terminology, pitfalls, examples, normalization needs.
   ```

   Generate refs for: survey, schema-proposer, extractor, normalizer, ma-critique

   **For MA Critique:** Focus on the DPYD-specific meta-analysis papers (Le Teuff, Meulendijks, Sharma, de Moraes, Kim, etc.) — what schemas they used, what statistical models, what worked/failed.

   **For Extractor:** Focus on what good atomic rows look like for DPYD, common extraction pitfalls.

   **For Normalizer:** Focus on variant naming aliases, drug regimen nomenclature, grading scale differences.

2. **Review** generated domain reference files for quality and completeness.

3. **Commit:** `"Phase 0: Generate domain reference files for dpyd"`

---

## Phase 0.5 — Survey Schema Generation

1. Invoke @schema-proposer in survey schema mode:
   ```
   Generate a survey schema (not extraction schema) for the DPYD pharmacogenetics domain.
   Read: projects/dpyd/domain/deep_research.md
   Write: projects/dpyd/schema/survey_schema.json

   The survey schema defines what the survey agent catalogs in each paper.
   Layer 1: universal fields (study ID, design, sample size, tables).
   Layer 2: domain-specific fields (variants, outcomes, regimens, grading).
   ```

2. **Commit:** `"Phase 0.5: Generate survey schema"`

---

## Phase 1 — Paper Survey

1. **Survey all papers.** For each PDF in `projects/dpyd/data/`:
   - Read the PDF (use PyMuPDF4LLM or Read tool for PDF)
   - Invoke @survey with the paper text + survey schema
   - Agent writes `projects/dpyd/surveys/{paper_id}.json`
   - Can run multiple surveys in sequence (or note for parallelism)

2. **Select subset.** Invoke @subset-selector:
   - Reads all survey outputs
   - Selects 3-4 diverse papers
   - Writes selection rationale to observation log

3. **Commit:** `"Phase 1: Survey {N} papers, select subset [{paper_ids}]"`

---

## Inner Loop

Repeat Phases 2-4 until MA Critique says "ready" or 5 iterations reached.

### Phase 2 — Schema Proposal (first iteration only)

1. Invoke @schema-proposer:
   - Reads: all surveys, domain reference (`domain/schema_ref.md`), observation log
   - Writes: `projects/dpyd/schema/v001.yaml` (rich schema with extraction guidance per field)

2. **Commit:** `"Phase 2: Propose schema v001 — {N} fields"`

### Phase 3a — Extraction

1. For each paper in current subset:
   - Read the paper (PDF → markdown)
   - Optionally query RAG index for targeted passages
   - Invoke @extractor with: paper text/passages, rich schema, survey output, domain reference
   - Agent writes: `projects/dpyd/extractions/{paper_id}.jsonl`
   - Agent writes paper flags to observation log

2. **Commit:** `"Phase 3a: Extract {N} papers ({total_rows} rows)"`

### Phase 3b — Normalization

1. Invoke @normalizer:
   - Reads: all extractions, schema (for `x-normalizable` fields), normalization tables, domain reference
   - Writes: updated extraction files, normalization table files
   - Writes normalization notes to observation log

2. **Commit:** `"Phase 3b: Normalize ({resolved} resolved, {disputes} disputes)"`

### Phase 3.5 — MA Critique

1. Invoke @ma-critique:
   - Reads: extractions, current schema, domain reference, deep research, observation log (including own prior feedback)
   - Writes: structured critique to observation log
   - Reports: convergence assessment ("ready" or "not ready" with reasons)

2. **If "ready":** Exit inner loop. Proceed to Phase 5.

3. **If "not ready":** Proceed to Phase 4.

4. **Commit:** `"Phase 3.5: MA critique — {ready|not ready}, {summary}"`

### Phase 4 — Schema Refinement

1. Invoke @schema-proposer in subsequent-iteration mode:
   - Reads: MA Critique output (from observation log), current schema, all surveys, observation log
   - Applies MA Critique recommendations: add/remove/modify fields
   - Writes new schema version: `v001.yaml` → `v002.yaml`
   - Logs `schema_rationale` entries to observation log for every change

2. Select new subset (invoke @subset-selector — rotate papers, keep 1 anchor)

3. **Commit:** `"Phase 4: Schema v{N} → v{N+1} — {changes}"`

4. Return to Phase 3a with updated schema + new subset.

---

## Phase 5 — Progressive Expansion
> Not yet implemented. After inner loop converges, expand to more papers.

## Phase 6 — Final Convergence
> Not yet implemented. Run MA Critique on full dataset.

---

## Critical Rules

1. **Schema changes ONLY after MA Critique** (Phase 4)
2. **Extract raw, never normalize during extraction** — strict phase separation
3. **Use null for missing data** — never empty string, 0, "N/A", or "unknown"
4. **Commit after every phase** — git history is the audit trail
5. **Read observation log before each phase** — maintain context across iterations
6. **One row = one finding** — Cartesian expansion: paper × entities × outcomes × grade levels × groups
7. **Reference/wildtype group is always a row** — not implicit background
8. **Provenance on everything** — tag as `paper`, `derived`, or `external`

## Architecture Reference

See `docs/specs/comparanda-v2-spec.md` for full architecture.
See `docs/specs/implementation-plan.md` for implementation plan.
See `docs/research/v1-lessons-extracted.md` for v1 lessons.
See `docs/research/prompt-engineering-reference.md` for prompting guide.
