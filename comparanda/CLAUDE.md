# Comparanda

Scientific paper schema extraction system. Given a domain and a corpus of papers, Comparanda discovers a structured schema and extracts data into atomic rows suitable for meta-analysis.

## How This Works

You are the orchestrator. Follow the phases below. Agents are in `.claude/agents/`. Invoke them with @agent-name. All project state for a given domain lives in `projects/<domain>/` (e.g. `projects/<domain>/`).

### Key Files
- **Schema:** `projects/<domain>/schema/` — rich YAML, versioned (v001.yaml, v002.yaml, ...)
- **Observations:** `projects/<domain>/observations/log.jsonl` — append-only structured log
- **Domain refs:** `projects/<domain>/domain/` — per-agent domain knowledge
- **Surveys:** `projects/<domain>/surveys/{paper_id}.json`
- **Extractions:** `projects/<domain>/extractions/{paper_id}.jsonl`
- **Normalization:** `projects/<domain>/normalization/{table}.json`
- **Critiques:** `projects/<domain>/critiques/iter_{NNN}.json`
- **Bundles:** `projects/<domain>/data/processed/<paper_id>/` (manifest.json + paper.md + tables/)
- **Source PDFs (gitignored):** `projects/<domain>/data/raw/pdfs/`
- **Phase 0 papers:** `projects/<domain>/data/raw/pdfs/phase0-references/`

### Observation Log Format
Append one JSON object per line to `projects/<domain>/observations/log.jsonl`:
```json
{"id": "OBS-001", "iteration": 1, "phase": "3.5_ma_critique", "agent": "ma_critique", "category": "schema_rationale", "content": "...", "related_fields": [], "timestamp": "2026-04-03T14:00:00Z"}
```
Categories: `schema_rationale`, `domain_pattern`, `paper_flag`, `normalization_note`, `extraction_difficulty`

### Rich Schema Format
Schema files are YAML at `projects/<domain>/schema/v{NNN}.yaml`:
```yaml
schema_version: "v001"
domain: "<domain>"
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

## Phase 0 — Domain Onboarding

> Run once per new domain.

### Prerequisites
Check that these exist:
- `projects/<domain>/domain/deep_research.md` — deep research report
- `projects/<domain>/domain/seed_knowledge.md` — seed domain knowledge
- `data/raw/pdfs/phase0-references/` — methodology + prior MA papers

### Steps

1. **Generate domain reference files.** For each agent that has a domain reference:

   Invoke the agent with this prefix before its normal prompt:
   ```
   REFERENCE GENERATION MODE

   Read the following materials and extract what is relevant to your role:
   - Deep research: projects/<domain>/domain/deep_research.md
   - Seed knowledge: projects/<domain>/domain/seed_knowledge.md
   - Phase 0 papers: [list relevant PDFs from data/raw/pdfs/phase0-references/]

   Write your domain reference file to: projects/<domain>/domain/{agent}_ref.md

   Include domain-specific knowledge that will help you perform your operational
   role: patterns, terminology, pitfalls, examples, normalization needs.
   ```

   Generate refs for: survey, schema-proposer, extractor, normalizer, ma-critique

   **For MA Critique:** Focus on prior meta-analysis papers in the domain — what schemas they used, what statistical models, what worked/failed.

   **For Extractor:** Focus on what good atomic rows look like in the domain, common extraction pitfalls.

   **For Normalizer:** Focus on terminology aliases, nomenclature variations across studies, scale/measurement differences.

2. **Review** generated domain reference files for quality and completeness.

3. **Commit:** `"Phase 0: Generate domain reference files for <domain>"`

---

## Phase 0.5 — Survey Schema Generation

1. Invoke @schema-proposer in survey schema mode:
   ```
   Generate a survey schema (not extraction schema) for the <domain>.
   Read: projects/<domain>/domain/deep_research.md
   Write: projects/<domain>/schema/survey_schema.json

   The survey schema defines what the survey agent catalogs in each paper.
   Layer 1: universal fields (study ID, design, sample size, tables).
   Layer 2: domain-specific fields.
   ```

2. **Commit:** `"Phase 0.5: Generate survey schema"`

---

## Phase 1 — Paper Survey

1. **Survey all papers.** For each bundle in `projects/<domain>/data/processed/<paper_id>/`:
   - Invoke @survey with file paths only (bundle dir + survey schema). Do not pre-digest content; the agent reads `manifest.json` and processes assets per its own instructions.
   - Agent writes `projects/<domain>/surveys/{paper_id}.json`
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
   - Writes: `projects/<domain>/schema/v001.yaml` (rich schema with extraction guidance per field)

2. **Commit:** `"Phase 2: Propose schema v001 — {N} fields"`

### Phase 3a — Extraction

1. For each paper in current subset:
   - Invoke @extractor with file paths to: bundle (`projects/<domain>/data/processed/{paper_id}/`), rich schema, domain reference. Pass file paths only — do not summarize, interpret, or pre-digest content. The extractor reads `manifest.json` and processes bundle assets per its own instructions.
   - Agent writes: `projects/<domain>/extractions/{paper_id}.jsonl`
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
   - Reads: MA Critique output (from critique JSON + observation log), current schema, all surveys, observation log
   - Applies MA Critique recommendations: add/remove/modify fields
   - Writes new schema version: `v001.yaml` → `v002.yaml`
   - Logs `schema_rationale` entries to observation log for every change

2. **Determine next action based on critique severity:**

   **Major schema change** (new identity dimensions, fields added/removed):
   → Clear ALL extraction cache. Re-extract entire current subset.
   → Return to Phase 3a with same papers + updated schema.

   **Minor schema change** (extraction guidance updates, vocabulary fixes):
   → Clear extraction cache for AFFECTED papers only (named in the critique).
   → Re-extract only those papers. Keep other extractions.
   → Return to Phase 3a with partial re-extraction.

   **Schema works** (critique says "ready" or "near-ready, no structural changes needed"):
   → Rotate subset (invoke @subset-selector for new 3-4 papers).
   → Return to Phase 3a with new papers + current schema.
   → This tests whether the schema generalizes beyond the current subset.

3. **Commit:** `"Phase 4: Schema v{N} → v{N+1} — {changes}, {action taken}"`

---

## Phase 5 — Subset Rotation (schema generalization testing)

After the inner loop converges on a subset, rotate to NEW papers to test
whether the schema generalizes. Do NOT expand to all papers yet.

1. **Archive current subset extractions.** Move to `extractions/archived/iter_{N}/`.
   These are no longer the active extraction — they were produced during schema
   development and may not reflect the final schema.

2. **Select new subset.** Invoke @subset-selector for 3-4 fresh papers (no overlap
   with previous subsets except the anchor).

3. **Extract new subset** with the current schema. Run Phase 3a → 3b → 3.5.

4. **If MA Critique says "ready"** on the new subset: schema generalizes.
   Rotate once more with different papers to confirm. After 2-3 successful
   rotations, the schema is stable → proceed to Phase 6.

5. **If MA Critique says "not ready"**: the schema has gaps exposed by the new
   papers. Return to Phase 4, refine, and continue the inner loop.

**Key principle:** Previous subset extractions are development artifacts, not
final output. Only the final full-corpus extraction (Phase 6) is the deliverable.

## Phase 6 — Final Full-Corpus Extraction

Once the schema is validated across multiple rotated subsets:

1. **Clear all extraction cache.** Start fresh with the final schema.

2. **Extract ALL papers** using the final schema. One extractor agent per paper.

3. **Normalize** the full corpus.

4. **Validate extractions.** For each paper, cross-check:
   - All identity dimensions are populated
   - Flag papers with large row count discrepancies for human review

5. **Final MA Critique** on the full dataset.

6. **Commit final output.** This is the deliverable.

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
9. **Always delegate to agents** — never perform extraction, normalization, survey, or critique yourself. Invoke the appropriate @agent. Each agent has specialized instructions that the orchestrator does not replicate. This applies to re-extraction as well as initial extraction.
10. **Neutral agent prompts** — when invoking any agent, pass file paths to its inputs only. Do not summarize, interpret, or pre-digest file content in the prompt. Do not embed MA Critique findings, survey conclusions, or your own analysis into agent prompts. Each agent reads its own inputs and makes its own decisions — that is what we are testing. If the prompt tells the agent what to do, you are testing prompt compliance, not instruction quality.

## Architecture Reference

See `docs/specs/comparanda-v2-spec.md` for full architecture.
See `docs/specs/implementation-plan.md` for implementation plan.
See `docs/research/v1-lessons-extracted.md` for v1 lessons.
See `docs/research/prompt-engineering-reference.md` for prompting guide.
