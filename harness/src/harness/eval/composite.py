"""Code-computed gating composite.

Before this module the headline composite that drives keep/revert was
computed *by the eval LLM agent*, which also emitted `agent_adjusted`
overrides of its own structural inputs — a closed self-grading loop. This
module computes the composite deterministically in Python from:

  - structural metrics (row/identity coverage, fill rate) — always available
  - finding_accuracy                                       — LLM detailed mode
  - a deterministic value-mismatch count                  — value_diff.py

The agent may still *report* `agent_adjusted` metrics, but they do not
silently replace the raw inputs to the gate. Weights are re-balanced toward
value-correctness (accuracy is principle #1 but was outweighed
2:1 by counting terms).
"""

from __future__ import annotations

from dataclasses import dataclass


# Re-weighted toward accuracy. The value-correctness block
# (finding_accuracy) carries the plurality; coverage is necessary but
# secondary. When finding_accuracy is unavailable (aggregate-only) the
# composite is explicitly capped and flagged so it cannot set a watermark.
WEIGHTS = {
    "row_coverage": 0.30,
    "entity_coverage": 0.15,
    "field_fill_rate": 0.05,
    "finding_accuracy": 0.50,
}
AGGREGATE_ONLY_CAP = 0.50  # without finding_accuracy the composite tops out here


@dataclass
class CompositeResult:
    composite: float
    mode: str                  # "full" | "aggregate_only"
    value_mismatch_count: int
    value_gate_passed: bool     # False blocks convergence regardless of score
    components: dict
    # The 100% recall gate. A gold finding that reports numeric
    # values but paired to NO extraction candidate is a coverage hole the
    # mismatch count cannot see (value_diff skips candidate-less rows). The
    # coverage gate is hard for CONVERGENCE (recall against gold must be
    # complete) but is intentionally NOT part of keep/revert — early
    # iterations legitimately have unmatched gold while coverage improves.
    unmatched_gold_with_values: int = 0
    coverage_gate_passed: bool = True


def gating_composite(
    *,
    row_coverage: float,
    entity_coverage: float,
    field_fill_rate: float,
    finding_accuracy: float | None,
    value_mismatch_count: int = 0,
    unmatched_gold_with_values: int = 0,
) -> CompositeResult:
    """Compute the composite that drives keep/revert and convergence.

    `finding_accuracy is None` → aggregate-only mode: the accuracy term is
    omitted and the result is capped, so an un-judged iteration can never
    look as good as a judged one (previously an aggregate-only composite
    could set the best-score watermark, and a missing finding_accuracy
    defaulted to 1.0).
    """
    row_c = min(max(row_coverage, 0.0), 1.0)          # cap over-extraction
    ent_c = min(max(entity_coverage, 0.0), 1.0)
    fill = min(max(field_fill_rate, 0.0), 1.0)

    if finding_accuracy is None:
        # Renormalise the available structural weights and cap.
        w = {k: WEIGHTS[k] for k in ("row_coverage", "entity_coverage", "field_fill_rate")}
        tot = sum(w.values())
        raw = (w["row_coverage"] * row_c
               + w["entity_coverage"] * ent_c
               + w["field_fill_rate"] * fill) / tot
        composite = round(min(raw * AGGREGATE_ONLY_CAP / 1.0, AGGREGATE_ONLY_CAP), 4)
        mode = "aggregate_only"
    else:
        fa = min(max(finding_accuracy, 0.0), 1.0)
        composite = round(
            WEIGHTS["row_coverage"] * row_c
            + WEIGHTS["entity_coverage"] * ent_c
            + WEIGHTS["field_fill_rate"] * fill
            + WEIGHTS["finding_accuracy"] * fa,
            4,
        )
        mode = "full"

    return CompositeResult(
        composite=composite,
        mode=mode,
        value_mismatch_count=value_mismatch_count,
        value_gate_passed=(value_mismatch_count == 0),
        unmatched_gold_with_values=unmatched_gold_with_values,
        coverage_gate_passed=(unmatched_gold_with_values == 0),
        components={
            "row_coverage": round(row_c, 4),
            "entity_coverage": round(ent_c, 4),
            "field_fill_rate": round(fill, 4),
            "finding_accuracy": finding_accuracy,
        },
    )
