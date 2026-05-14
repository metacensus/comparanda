# Comparanda v1 → v2: Extracted Lessons

> Extracted 2026-04-03 from v1 codebase by analysis agent.

---

## 1. Universal Lessons (carry forward to v2 agent definitions)

### 1.1 The Atomic Row Model

- **One row = one indivisible finding** at the intersection of all identity dimensions. Cartesian expansion: `paper × variants × outcomes × grade categories × optional splits`.
- **Reference/wildtype group is always a row**, not implicit background. Without it, 2×2 tables can't be reconstructed.
- **Grade categories split into separate rows.** If paper reports them separately, split. If combined, keep combined.
- **The schema is the product.** Schema discovery is the hard problem. Extraction into a given schema is comparatively mechanical.

### 1.2 Phase Separation Principle

Three phases strictly separated:
1. **Extract raw** — copy text exactly as the paper writes it, never normalize
2. **Normalize** — map raw text to canonical IDs in a separate pass
3. **Evaluate** — compare against gold standard or run consistency checks

Conflating extract + normalize in one pass produces contradictions — agents oscillate.

### 1.3 Provenance Tagging on Everything

Every extracted value carries: `paper` (from text/table), `derived` (calculated), or `external` (web/database). This lets System 2 distinguish hallucinations from legitimate inferences.

### 1.4 Two Extraction Paths

- **Direct effect size** — paper reports OR/HR/RR with CI; extract directly, tag `paper`
- **Count derivation** — paper reports 2×2 counts; extract counts, tag `derived` for calculated effect sizes

Agent must recognize which path applies per-finding, not per-paper.

### 1.5 Survey-First Architecture

Cheap/fast survey before extraction: inventory of tables, complexity, expected row count, pages needing vision. The survey is a breadth pass — catalogs, doesn't interpret.

### 1.6 Agent Context Discipline

- One subagent per paper (parallelizable, context-isolated)
- Haiku for survey, Sonnet for extraction
- Concurrent execution with rate-limit-aware caps (5 surveys, 2-3 extractions)
- Survey instructions: 800–1200 words. Extraction: 1200–1800 words. Beyond these, agents lose focus.

### 1.7 Null Semantics Are Load-Bearing

- Use `null` when not reported or not determinable
- **Never** use `""`, `0`, `"N/A"`, `"not reported"`, `"unknown"` as substitutes
- **Never** invent a plausible value to avoid null

### 1.8 Meta-Commentary as Diagnostic Signal

`_meta_commentary` block on every agent output: `tables_processed`, `tables_skipped`, `ambiguities`, `estimated_completeness`, `confidence_concerns`. Asking agents to identify specific uncertainties produces useful signal; asking them to rate their own accuracy does not.

### 1.9 Universal Schema Fields

Invariant across domains:
- Study identifiers: PMID, DOI, first author, year, journal
- Study design type
- Sample sizes: `total_n_study`, `n_genotyped`, `n_evaluable_outcome`

### 1.10 Schema Field Structure

- All fields in `required` array (forces null rather than omission)
- Nullable via `anyOf: [{type}, {type: "null"}]`
- Grouped logically (bibliometric, entity, demographics, therapy, outcome, measurement, comparison)
- Each with `description` explaining what to extract

### 1.11 Normalization Table Architecture (Rosetta Stone Pattern)

- Both raw text field + canonical ID field in schema
- `x-normalizable` annotation makes normalization domain-agnostic
- Table entry: `{"raw", "canonical_id", "confidence", "source", "iteration_added"}`
- Thresholds: auto-apply ≥0.9, flag ≥0.7, manual review <0.7

### 1.12 Instructions-as-Config

System 2 modifies instruction files, never code. One targeted change per iteration for attribution.

---

## 2. Good Prompting Patterns (carry forward)

### 2.1 Instruction File Skeleton

Front-load the constraint that matters most:
```
1. Role statement     (one sentence)
2. Core principle     (single most important constraint)
3. Input description  (what you receive)
4. Procedure          (numbered steps or decision tree)
5. Output format      (schema, example, field descriptions)
6. Rules / guardrails (hard constraints, as imperatives)
7. Meta-commentary    (self-reflection instructions)
```

### 2.2 Specificity Ladder

```
VAGUE:    "Try to capture relevant data"
GENERAL:  "Extract all quantitative outcomes"
SPECIFIC: "Extract every cell containing a count, percentage, OR, HR, or p-value"
RIGID:    "Extract columns 3-7 from Table 2"
```
Sweet spot is SPECIFIC: precise about what kind, flexible about where.

### 2.3 Directive Language

Replace hedging with imperatives. Agents interpret "consider" and "you may want to" as optional.

### 2.4 Cartesian Expansion Example

Most effective example shows a 2-variant × 3-grade table producing 6 rows, not 2. Makes the non-obvious case explicit with concrete table format.

### 2.5 Schema as Concrete Example

Complete example object with inline type annotations as comments — communicates structure, types, allowed values simultaneously.

### 2.6 Pre-Completion Checklist

Placed after procedure, before output format. Forces verification step in agent reasoning.

### 2.7 Domain-General Language

Instead of "Map variant names to rs numbers using dbSNP" → "For entity fields with a normalization table, map raw text to canonical ID using the relevant lookup resource."

### 2.8 RAG Query Design

Targeted natural-language questions derived from schema field categories. Each query targets a specific data need. Dedup across queries. Reduces ~30k tokens (full paper) to ~5-8k (passages).

---

## 3. Anti-Patterns (avoid in v2)

1. **Extract + Normalize in one pass** — irresolvable contradiction, agents oscillate
2. **Instructions too long** — beyond guidelines, agents ignore the middle
3. **Over-specification of location** — "Table 2 columns 3-5" breaks on different paper formats
4. **Under-specification of splitting rules** — Grade 3-4 combined vs separate needs explicit rule
5. **"Be thorough" with truncated content** — contradictory, must acknowledge constraint
6. **Vague outcome descriptions** — agents collapse specific to vague without two-field pattern
7. **Omitting wildtype rows** — consistent failure, needs CRITICAL note + example
8. **Building normalization from gold standard** — contaminates benchmark
9. **Agent over-generalizing** — without row count heuristic (K × M × G), agents combine
10. **Implicit null semantics** — agents use soft-nulls unless rules are explicit
11. **Monolithic critic changes** — multiple fixes per iteration = unattributable results

---

## 4. DPYD Domain Knowledge (for domain/ files)

### Variant Naming and Aliases
- `IVS14+1G>A` = `c.1905+1G>A` = `rs3918290` = `DPYD*2A`
- `I560S` = `c.1679T>G` = `rs55886062` = `DPYD*13`
- `D949V` = `c.2846A>T` = `rs67376798`
- `HapB3 tag` = `c.1236G>A` = `rs56038477` (proxy, not causal)
- `HapB3 causal` = `c.1129-5923C>G` = `rs75017182`

### CPIC Activity Score System
- Each allele: 0 (no-function) / 0.5 (decreased) / 1 (normal)
- Gene-level: sum both alleles
- AS 2.0 = normal, AS 1.5/1.0 = intermediate (50% dose reduction), AS 0.5 = poor (>75%), AS 0 = contraindicated

### Outcome Grading
- CTCAE v1-v6, NCI-CTC v1-v2, WHO grading
- Papers often don't specify version — flag as inferred
- Grade ≥3 is standard primary endpoint

### Drug Regimens
- mFOLFOX6, FOLFOX4, FOLFIRI, CAPOX/XELOX, capecitabine mono, Mayo Clinic, de Gramont, Nordic
- Three FP drugs: 5-FU (IV), capecitabine (oral), tegafur (S-1)
- Bolus vs infusional 5-FU have different toxicity profiles — do not pool

### Critical Confounders
- Preemptive dose reduction (post-2018 studies)
- Bolus vs infusional 5-FU
- 12-week toxicity window (FUSAFE standard)

### External Databases
- PharmVar, PharmGKB, ClinVar, dbSNP, CPIC, gnomAD v4.0.0

---

## 5. Code Patterns Worth Keeping

1. **PipelineConfig dataclass** — derives all paths from single `project_dir`
2. **Versioned schema discovery** — `sorted(schema_dir.glob("v*.yaml"))[-1]`
3. **Concurrent execution with semaphore caps** — per-task-type limits
4. **Schema-driven normalization** — `x-normalizable` annotations, zero hardcoded field names
5. **Two-stage normalization** — exact/fuzzy match (ms) → batched LLM (seconds)
6. **JSONL parsing defensiveness** — filter markdown fences, skip unparseable
7. **Schema summary injection** — concise field list, not full JSON Schema
8. **Survey as extraction context** — inject survey JSON (truncated) into extraction prompt
9. **RAG query set as constants** — derived from schema categories, customizable per domain
10. **Iteration result logging** — composite, rows, wall clock, change applied
