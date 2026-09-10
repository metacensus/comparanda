# Comparanda

Scientific paper schema extraction system. Given a domain and a corpus of papers, Comparanda discovers a structured schema and extracts data into atomic rows suitable for meta-analysis.

## How This Works

You are the orchestrator. Follow the phases below. Agents are in `.claude/agents/`. Invoke them with @agent-name. All project state for a given domain lives in `projects/<domain>/` (e.g. `projects/<domain>/`).

For a concise map of every agent's scope, inputs, outputs, scripts, hooks, and
resume behavior, see `AGENT_INDEX.md`.

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
  - field: entity_raw
    description: "Primary entity as reported"
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

Repeat Phases 2-4 until MA Critique says "ready" on the current subset. There
is no fixed iteration cap; convergence is governed by generalization (see Phase
5), not a hard count. If the same subset churns without progress (e.g. the
schema oscillates or each iteration only fits one pathological paper), stop and
escalate to a human — that is a signal of overfitting, not a reason to keep
iterating.

### Phase 2 — Schema Proposal (first iteration only)

1. Invoke @schema-proposer:
   - Reads: all surveys, domain reference (`domain/schema_ref.md`), observation log
   - Writes: `projects/<domain>/schema/v001.yaml` (rich schema with extraction guidance per field)

2. **Commit:** `"Phase 2: Propose schema v001 — {N} fields"`

### Phase 3a — Extraction

1. For each paper in current subset:
   - Invoke @extractor with file paths to: bundle (`projects/<domain>/data/processed/{paper_id}/`), rich schema, domain reference. Pass file paths only — do not summarize, interpret, or pre-digest content. The extractor reads `manifest.json` and processes bundle assets per its own instructions.
   - Agent writes: `projects/<domain>/extractions/{paper_id}.jsonl`
   - Record the returned extractor agent/subagent ID, the parent session ID if available, schema version, staging script path, and finalized JSONL path in `projects/<domain>/extractions/.staging/extractor_sessions.json` using `uv run scripts/extraction/extractor_session_registry.py upsert ...`. This registry is required for QC repair and later schema-iteration re-extraction.
   - Agent writes paper flags to observation log

2. **Commit:** `"Phase 3a: Extract {N} papers ({total_rows} rows)"`

### Phase 3a-QC — Extraction QC

1. For each paper in current subset:
   - Invoke @extraction-qc with file paths to: extraction (`projects/<domain>/extractions/{paper_id}.jsonl`), survey (`projects/<domain>/surveys/{paper_id}.json`), schema (current version), observation log. Pass file paths only — do not summarize or interpret content.
   - Agent writes: `projects/<domain>/extractions/{paper_id}_qc.json`
   - Agent writes QC summary to observation log

2. **Parse each QC report's structured action fields only.** Each QC report must include `action_class`, `must_fix_before_normalization`, and `repair_packet`. Do not read or interpret the full QC prose to decide what to do; the QC agent owns that judgment. The current action classes are exactly: `blocking_reextract`, `targeted_extraction_repair`, `schema_or_normalization_note`, and `nonblocking_note`.

   - If `action_class` is `blocking_reextract` or `targeted_extraction_repair` and `must_fix_before_normalization=true`, look up the original extractor in `projects/<domain>/extractions/.staging/extractor_sessions.json`.
   - You MUST first resume the original extractor with `SendMessage` to the recorded extractor agent ID. Pass file paths only: extraction JSONL, QC report, repair packet, current schema, bundle path, SDK contract, and existing staging script path. Do not paste the QC findings into the prompt except by giving the file paths; the resumed extractor reads the repair packet.
   - Wait for the resumed extractor's completion/notification before deciding the repair is done. `SendMessage` may return before the resumed subagent finishes in the background.
   - If resume fails, retry resume up to 2 more times with the same file-path-only packet and record each failed attempt in `extractor_sessions.json` using `scripts/extraction/extractor_session_registry.py record-resume-attempt`. Include the tool error and whether the local transcript/meta files exist.
   - If all resume attempts fail, instantiate a NEW extractor for that paper with the same file-path-only repair inputs and the existing staging script path unless it is corrupt. Record the replacement as the current extractor with `extractor_session_registry.py upsert`, preserving the original extractor in lineage. Do not use `continue` as the repair fallback.
   - Rerun @extraction-qc for the affected paper after repair.
   - For `schema_or_normalization_note` and `nonblocking_note`, do not mutate extraction rows; carry the note forward.

3. **Commit:** `"Phase 3a-QC: QC {N} papers ({gap_count} gaps, {re_extract_count} re-extractions)"`

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

2. **Follow the critique's structured `recommended_action`.** Do not infer
   re-extraction scope from critique prose. The MA Critique output must include
   `recommended_action.type`, `recommended_action.papers_to_reextract`, and
   `recommended_action.extraction_cache_action`; the orchestrator follows those
   fields mechanically.

   - `major_schema_change`: re-extract the entire current subset against the new
     schema. Resume each original extractor from `extractor_sessions.json` when
     possible and have it patch its existing `.staging/<paper_id>/scripts/build.py`
     using the schema diff. If resume fails after the required retries, launch a
     replacement extractor and record the new session metadata. After any
     finalized JSONL is rewritten, rerun extraction QC and then rerun
     normalization apply before MA Critique or readiness decisions, because
     extraction rewrites reset normalizer-owned canonical fields.
   - `minor_guidance_update`: apply the updated vocabulary/extraction guidance
     to the schema, then re-extract only `papers_to_reextract`. Resume the
     recorded extractor first; if resume fails after retries, launch a new
     extractor for the affected paper. Keep other extractions. After any
     finalized JSONL is rewritten, rerun extraction QC and then rerun
     normalization apply before MA Critique or readiness decisions, because
     extraction rewrites reset normalizer-owned canonical fields.
   - `rotate_subset`: rotate subset with @subset-selector for new 3-4 papers and
     return to Phase 3a with new papers + current schema. This tests whether the
     schema generalizes beyond the current subset.

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

3. **Extract new subset** with the current schema. Run Phase 3a → 3a-QC → 3b → 3.5.

   **Convergence rule (replaces any fixed iteration cap):** the schema is
   stable when it survives **3 consecutive clean rotations** — three new subsets
   in a row that need **no new structural schema change**. A *structural* change
   is adding/removing an identity dimension or a field. Vocabulary additions and
   extraction-guidance tweaks are non-structural and do NOT reset the counter,
   but they still MUST be folded into the next schema version and applied to any
   affected re-extractions named by MA Critique. Non-structural means "does not
   reset the counter," not "ignore or defer the fix." A "clean" rotation is one
   whose MA Critique returns "ready" or asks only for non-structural fixes. Keep
   a single fixed regression anchor across all rotations to detect regressions.

4. **If the rotation is clean** (ready / non-structural only): apply any
   non-structural schema updates, then increment the consecutive-clean counter.
   At 3, the schema is validated → proceed to Phase 6.
   Otherwise select another fresh subset and rotate again.

5. **If the rotation needs a structural change**: reset the consecutive-clean
   counter to 0, return to Phase 4, refine the schema, then resume rotating.
   Distinguish a *generalizable* gap (a structural need that recurs across
   multiple papers) from *overfitting* (a dimension demanded by a single
   pathological paper) — defer the latter as a documented edge case rather than
   growing the schema for one paper.

**Key principle:** Previous subset extractions are development artifacts, not
final output. Only the final full-corpus extraction (Phase 6) is the deliverable.

## Phase 6 — Final Full-Corpus Extraction

Once the schema is validated across multiple rotated subsets:

1. **Prepare the final extraction queue.** Include every in-scope paper that
   should appear in the final corpus. Exclude papers only when the observation
   log records a corpus-curation or substrate/paper-prep reason.

2. **Reuse before fresh extraction.** For each paper, look up
   `projects/<domain>/extractions/.staging/extractor_sessions.json` and any
   existing staging script. If the paper was extracted during schema
   development, resume the recorded extractor when possible and have it patch
   the existing build script against the final schema diff. If the recorded
   extractor cannot be resumed after the required retries, launch a replacement
   extractor against the same staging script and record the fallback reason.
   Launch a fresh extractor only for papers that have no prior extraction state
   or whose prior state is explicitly unrecoverable.

3. **Run bounded batches.** Do not launch the full corpus concurrently. Use
   small batches of a few extractor agents at a time, with smaller batches for
   known hard papers, image/table-heavy papers, or papers with prior validator
   repair churn. Before moving to the next batch, verify every paper in the
   current batch has either a validated final JSONL or a documented exclusion /
   confirmed-empty outcome.

4. **Normalize** the full corpus. Any finalized JSONL rewritten by extraction
   resets normalizer-owned canonical fields, so normalization apply must run
   after each Phase 6 extraction batch or at minimum before final critique/eval.

5. **Validate extractions.** For each paper, cross-check:
   - All identity dimensions are populated
   - Flag papers with large row count discrepancies for human review

6. **Final MA Critique** on the full dataset.

7. **Commit final output.** This is the deliverable.

---

## Critical Rules

1. **Schema changes ONLY after MA Critique** (Phase 4)
2. **Extract raw, never normalize during extraction** — strict phase separation
3. **Use null for missing data** — never empty string, `0`, "N/A", or
   "unknown" as a missing-value substitute. Observed numeric zero is a real
   value when the paper reports zero.
4. **Commit after every phase** — git history is the audit trail
5. **Read observation log before each phase** — maintain context across iterations
6. **One row = one finding** — Cartesian expansion: paper × entities × outcomes × grade levels × groups
7. **Reference/control group is always a row** — not implicit background
8. **Provenance on everything** — tag as `paper`, `derived`, or `external`
9. **Always delegate to agents** — never perform extraction, normalization, survey, or critique yourself. Invoke the appropriate @agent. Each agent has specialized instructions that the orchestrator does not replicate. This applies to re-extraction as well as initial extraction.
10. **Neutral agent prompts** — when invoking any agent, pass file paths to its inputs only. Do not summarize, interpret, or pre-digest file content in the prompt. Do not embed MA Critique findings, survey conclusions, or your own analysis into agent prompts. Each agent reads its own inputs and makes its own decisions — that is what we are testing. If the prompt tells the agent what to do, you are testing prompt compliance, not instruction quality.

## Missingness Taxonomy

The extractor uses `missing(reason, explanation, sources=...)` for absent scientific values. The SDK contract defines seven reasons and the counted uncategorized fallback; read `docs/extraction-sdk-agent-contract.md`. Do not use `null()` or `abstained()` as authoring alternatives. New absent fields require an explanation; schema-declared canonical IDs awaiting normalization remain exempt.

Keep schema-defined non-null `not_applicable` and `not_stated` values where required, with their explanation. Zero is a value, not missingness. Do not infer that the paper is silent merely because an extraction value is absent. Preserve historical results rather than guessing missingness reasons for them.

## Architecture Reference

See `docs/specs/comparanda-v2-spec.md` for full architecture.
See `docs/specs/implementation-plan.md` for implementation plan.
See `docs/research/v1-lessons-extracted.md` for v1 lessons.
See `docs/research/prompt-engineering-reference.md` for prompting guide.
