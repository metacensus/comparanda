# System 2 Lessons

> Observations from interactive testing that should become universal instruction
> improvements when the calibration harness is built. These generalize across domains.

## Lesson 1: Meta-analyst annotations don't belong in extraction schemas

**Observed:** Schema v001 included `quality_score_raw` and `quality_instrument` — 0% fill rate because these are assigned by meta-analysts reviewing studies, not extractable from the papers themselves.

**Universal instruction fix:** Schema Proposer should include a rule: "Do not add fields that represent analyst judgments (quality scores, risk-of-bias ratings, certainty assessments). These are assigned during meta-analysis, not during extraction from source papers. If a domain reference mentions quality instruments (NOS, GRADE, AMSTAR), note them as meta-analyst tools, not extraction targets."

**Applies to:** schema-proposer.md universal instructions

## Lesson 2: Identity dimensions must accommodate the finest granularity in the corpus


**Root cause:** The schema's identity dimension guidance and the extractor's instructions were domain-specific about genotypes but missed the universal principle. The real issue is: if the schema's identity dimensions can only represent coarse groupings, fine-grained data gets silently compressed during extraction — and no System 1 agent can detect the loss.

**Universal principle:** Extract at the granularity the paper reports, never collapse to a coarser grouping. Identity dimensions must accommodate the finest granularity reported across papers in the corpus. Data can always be aggregated downstream; data lost to forced collapsing cannot be recovered.

**Applies to:**
- schema-proposer.md: "design identity dimensions for the finest granularity in the corpus"
- ma-critique.md: "check whether identity dimensions force lossy data compression"
- extractor.md: "extract at the paper's granularity, never collapse"

**System 2 implication:** This is the strongest evidence for why System 2 is needed. The MA Critique (System 1's quality signal) cannot detect completeness issues — it evaluates whether EXISTING rows are poolable, not whether MISSING rows exist. Only comparison against a gold standard (the harness) reveals that 73% of data was dropped. System 2 would: (1) detect the row count discrepancy, (2) trace it to the schema's identity dimension granularity, (3) propose the universal principle above.

## Lesson 3: MA Critique convergence is necessary but not sufficient


**Implication for System 2 architecture:** The harness must provide a completeness signal that System 1 lacks. Two mechanisms:
1. **Gold standard comparison** (when available): row count comparison, field coverage, finding-level matching
2. **Extraction validation agent** (always available): cross-checks extraction row count against survey's estimated_atomic_rows, verifies all surveyed variants appear in extraction, flags large discrepancies

The extraction validation agent should be a System 1 agent (runs before MA Critique), not a harness agent. It provides a completeness check independent of the gold standard. The harness provides the definitive completeness assessment.

## Lesson 4: Schema development extractions are disposable

**Observed:** Extractions from the inner loop schema development phase (subsets) are development artifacts, not final output. They were produced with evolving schemas (v001, v002, v003) and may not reflect the final schema. Previous subset extractions should be archived, not carried forward.

**Implication:** The pipeline should have two distinct phases:
1. **Schema development** (rotate subsets, iterate schema) — extractions are temporary
2. **Final extraction** (full corpus with final schema) — extractions are the deliverable

The protocol should clearly separate these phases. Previous subset extractions should be archived when rotating to a new subset.

## Lesson 9: Complementary threshold vocabulary gap — structural limitation


- Surveys don't catalog derivable complements, only explicitly reported thresholds
- Domain references from prior meta-analyses focus on ge3 pooling, not complementary extraction
- The extractor sees the data as "ge3 + n_noevent" not "two separate thresholds"
- MA Critique sees consistent ge3 rows and says "poolable"
- The gold standard is the only source that shows both thresholds are expected

**Root cause:** The information that complementary thresholds should be separate rows exists only in the gold standard, which System 1 never sees. System 2 sees the row count gap but doesn't connect it to a vocabulary addition in the schema.

**This is not fixable by agent instructions alone.** It requires either:
1. The gold standard comparison (System 2) to specifically diagnose "missing complementary threshold vocabulary" as a category — not just "under-extraction"
2. The survey agent to be enhanced to catalog derivable complements, not just explicit thresholds  
3. The domain reference files to explicitly note that gold-standard-quality extraction includes complementary thresholds
4. A direct vocabulary addition during calibration based on the gold standard pattern

