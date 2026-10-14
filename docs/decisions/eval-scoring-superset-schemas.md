> **Historical (v1).** This record describes the v1 eval harness (`alignment.py`, `schema_bridge.py`, `scorer.py`), which has since been replaced; it is kept for design context.

# Decision Record: Superset Schema Scoring Problem

**Date:** 2026-04-01
**Status:** Identified, deferred to Tier 3.1 (Schema Evolution)
**Context:** Harness improvement review

## Problem

The eval harness penalizes comprehensive extractions that go beyond the gold standard:

1. **Row alignment (`alignment.py`):** Extra predicted rows with no gold match are classified as "hallucinations" (`unmatched_pred`). This conflates invented data (bad) with surplus real data not covered by the gold standard (potentially good).

2. **Schema bridge (`schema_bridge.py`):** Agent fields with no gold equivalent are tracked as `unmapped_agent` and reduce the `coverage_score`. An agent that discovers additional valid fields (e.g., dose-response data not in the DPYD gold standard) gets penalized for being more comprehensive.

3. **Composite scoring (`scorer.py`):** The composite metric `(schema 40% + extraction 60%)` uses coverage_score and extraction_quality, both of which are lowered by surplus data.

## Impact

System 2's hill-climbing mechanism optimizes the composite score. If surplus real data lowers the composite, System 2 will propose instruction changes that make the extraction agent MORE conservative — extracting less to avoid the "hallucination" penalty. This is the opposite of what we want for generalization.

## Interim Mitigation (applied in this round)

Add language to the critic prompt: "A superset extraction that covers all gold standard fields plus additional valid fields is a positive outcome, not an error. Do not propose changes that make the extraction agent more conservative unless it is genuinely hallucinating — inventing data not present in the source paper."

## Proper Fix (Tier 3.1)

When implementing schema evolution:
- Distinguish `hallucination` (data not in paper) from `surplus` (data in paper, not in gold standard) in the alignment result
- Track surplus rows/fields separately — they should not lower the composite score
- Allow System 2 to propose adding surplus fields to the schema when they appear consistently across papers
- Consider a secondary "comprehensiveness" metric that rewards surplus extraction (rather than penalizing it)

## Related Concern: Overfitting to DPYD

System 2 instruction changes should be domain-general. DPYD-specific terminology (star alleles, fluoropyrimidine, CTCAE grade scales) should not appear in instruction files — these belong in the domain context or schema, not in reusable extraction instructions. Interim mitigation: anti-overfitting language in critic prompt. Mechanical backstop: instruction lint (Tier 2.2) that flags domain-specific terms.
