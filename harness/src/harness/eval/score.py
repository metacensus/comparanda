"""Compute the gating composite for an iteration — in code, not by the LLM.

Wiring for review note/F10/F21: the eval agent no longer self-computes the
headline composite. It runs aggregate metrics, runs the deterministic
value-diff over the prepared comparisons, supplies its detailed-mode
finding_accuracy (a number it reasons to, per paper, on the *pairing* — not
the score), and this module combines them deterministically and emits an
auditable IterationRecord. `agent_adjusted` metrics may be reported in the
eval report but are NOT inputs here.

Usage (run by the eval agent after aggregate + detailed passes):

    uv run python -m harness.eval.score \
        --extractions-dir ../comparanda/projects/<domain>/extractions \
        --gold-standard benchmark/gold_standard/<file>.xlsx \
        --document-id-map benchmark/eval_config/document_id_map.json \
        --finding-accuracy 0.71 \
        --iteration 3 --schema-version v004 \
        --agents-dir ../comparanda/.claude/agents \
        --output iterations/iter_3/score.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from harness.eval.aggregate import evaluate_all
from harness.eval.composite import gating_composite
from harness.eval.gold_config import load_adjudications, load_gold_config
from harness.eval.gold_standard import check_gold_consistency, load_gold_standard
from harness.eval.prepare_comparison import prepare_comparison
from harness.eval.finding_adjudication import validate_finding_scores
from harness.eval.semantic_value_gate import compute_semantic_value_gate
from harness.eval.value_diff import compute_value_diff
from harness.eval.trajectory import measure_instruction_sizes


def _gold_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unknown"


def load_finding_scores(
    path: Path,
    gold_rows_by_paper: dict[str, int],
    *,
    comparisons: dict[str, dict] | None = None,
) -> tuple[float, dict]:
    """Validate a per-finding scores file and recompute finding_accuracy IN CODE.

    review note: the gate's plurality input must not be a bare self-reported
    LLM float. The eval agent emits one record per gold row —
    {gold_index, completeness, accuracy, extraction_index|null, reasoning} —
    and this function recomputes the mean of completeness×accuracy, enforcing
    the denominator rule (eval.md: every gold row counts) mechanically:
    record count and gold_index coverage must exactly match the gold row set
    for every scored paper. Raises ValueError on any violation.
    """
    payload = json.loads(Path(path).read_text())
    validation = validate_finding_scores(
        payload,
        gold_rows_by_paper,
        comparisons=comparisons,
        require_structured=False,
    )
    if validation.problems:
        raise ValueError(
            "finding-scores validation failed:\n  "
            + "\n  ".join(validation.problems)
        )

    fa = sum(validation.products) / len(validation.products)
    total = len(validation.products)
    aggregate_recall = validation.per_paper_recall.get("__aggregate__", {})
    return fa, {
        "papers_scored": sorted(payload.get("papers", {})),
        "finding_count": total,
        "per_paper_finding_accuracy": validation.per_paper_accuracy,
        "semantic_recall": {
            "captured_gold_rows": aggregate_recall.get("captured_gold_rows", 0),
            "fully_represented_gold_rows": aggregate_recall.get(
                "fully_represented_gold_rows",
                aggregate_recall.get("captured_gold_rows", 0),
            ),
            "partially_captured_gold_rows": aggregate_recall.get(
                "partially_captured_gold_rows", 0
            ),
            "partially_represented_gold_rows": aggregate_recall.get(
                "partially_represented_gold_rows",
                aggregate_recall.get("partially_captured_gold_rows", 0),
            ),
            "missing_gold_rows": aggregate_recall.get("missing_gold_rows", 0),
            "represented_gold_rows": aggregate_recall.get("represented_gold_rows", 0),
            "any_representation_gold_rows": aggregate_recall.get(
                "any_representation_gold_rows",
                aggregate_recall.get("represented_gold_rows", 0),
            ),
            "inaccurate_represented_gold_rows": aggregate_recall.get(
                "inaccurate_represented_gold_rows", 0
            ),
            "mean_completeness": aggregate_recall.get("mean_completeness"),
            "mean_gold_row_completeness": aggregate_recall.get(
                "mean_gold_row_completeness",
                aggregate_recall.get("mean_completeness"),
            ),
            "all_in_recall": aggregate_recall.get("all_in_recall"),
            "any_representation_rate": aggregate_recall.get(
                "any_representation_rate",
                aggregate_recall.get("all_in_recall"),
            ),
            "field_value_recoverability_rate": aggregate_recall.get(
                "field_value_recoverability_rate",
                aggregate_recall.get(
                    "any_representation_rate",
                    aggregate_recall.get("all_in_recall"),
                ),
            ),
            "full_capture_rate": aggregate_recall.get("full_capture_rate"),
            "full_representation_rate": aggregate_recall.get(
                "full_representation_rate",
                aggregate_recall.get("full_capture_rate"),
            ),
            "field_value_representation_rate": aggregate_recall.get(
                "field_value_representation_rate",
                aggregate_recall.get(
                    "full_representation_rate",
                    aggregate_recall.get("full_capture_rate"),
                ),
            ),
            "per_paper": {
                k: v
                for k, v in validation.per_paper_recall.items()
                if k != "__aggregate__"
            },
        },
        "structured_adjudications": {
            "structured_finding_count": validation.structured_count,
            "all_findings_structured": validation.structured_count == total,
            "transform_count": validation.transform_count,
        },
    }


def _full_mode_metric_inputs(
    *,
    raw_row_coverage: float,
    raw_entity_coverage: float,
    raw_field_fill_rate: float,
    finding_scores_summary: dict | None,
    semantic_field_overlap: dict | None,
) -> tuple[dict, dict]:
    """Choose composite inputs and document their source.

    In detailed/full mode, the hybrid eval design treats the LLM adjudicated
    gold-row records as the source of truth for recall. Raw row counts,
    deterministic identity coverage, and all-schema fill rate remain useful
    diagnostics, but they should not override validated semantic matching.
    """
    inputs = {
        "row_coverage": raw_row_coverage,
        "entity_coverage": raw_entity_coverage,
        "field_fill_rate": raw_field_fill_rate,
    }
    sources = {
        "mode": "raw_structural",
        "row_coverage": "aggregate_row_count",
        "entity_coverage": "deterministic_identity_resolution",
        "field_fill_rate": "all_extraction_schema_fields",
    }

    if not finding_scores_summary:
        return inputs, sources

    recall = finding_scores_summary.get("semantic_recall") or {}
    semantic_recall = recall.get(
        "field_value_recoverability_rate",
        recall.get("any_representation_rate", recall.get("all_in_recall")),
    )
    mean_completeness = recall.get(
        "mean_gold_row_completeness",
        recall.get("mean_completeness"),
    )
    if isinstance(mean_completeness, (int, float)):
        inputs["row_coverage"] = float(mean_completeness)
        sources["row_coverage"] = "finding_scores.mean_gold_row_completeness"
    if isinstance(semantic_recall, (int, float)):
        inputs["entity_coverage"] = float(semantic_recall)
        sources["entity_coverage"] = "finding_scores.field_value_recoverability_rate"

    overlap_aggregate = (semantic_field_overlap or {}).get("aggregate") or {}
    source_faithfulness = overlap_aggregate.get("source_faithfulness")
    if isinstance(source_faithfulness, (int, float)):
        inputs["field_fill_rate"] = float(source_faithfulness)
        sources["field_fill_rate"] = "semantic_field_overlap.source_faithfulness"

    sources["mode"] = "semantic_full"
    return inputs, sources


def compute_score(
    *,
    extractions_dir: Path,
    gold_standard: Path,
    document_id_map: Path,
    finding_accuracy: float | None,
    iteration: int,
    schema_version: str | None = None,
    agents_dir: Path | None = None,
    rel_tol: float = 0.05,
    finding_scores_path: Path | None = None,
    semantic_field_overlap_path: Path | None = None,
    semantic_value_gate_output: Path | None = None,
) -> dict:
    agg = evaluate_all(extractions_dir, gold_standard, document_id_map)

    eval_config_dir = Path(document_id_map).parent
    gold_config = load_gold_config(eval_config_dir)
    adjudications = load_adjudications(eval_config_dir)

    # Deterministic value-diff across every paper's prepared comparison.
    total_mismatches = 0
    total_unmatched_gold = 0
    total_adjudicated_gold_errors = 0
    total_fields_compared = 0
    per_paper_mismatches: dict[str, int] = {}
    per_paper_unmatched_gold: dict[str, int] = {}
    gold_rows_by_paper: dict[str, int] = {}
    ambiguous_pairings: dict[str, list] = {}
    gold_missing_columns: list[str] = []
    comparison_by_paper: dict[str, dict] = {}
    benchmark_statuses: dict[str, dict] = {}
    for p in agg.papers:
        cmp = prepare_comparison(p.paper_id, extractions_dir, gold_standard, document_id_map)
        benchmark_statuses[p.paper_id] = {
            "benchmark_status": cmp.get("benchmark_status", "gold_scored"),
            "gold_scored": bool(cmp.get("gold_scored", True)),
            "precision_only": bool(cmp.get("precision_only", False)),
            "gold_rows": cmp.get("gold_rows", 0),
            "extraction_rows": cmp.get("extraction_rows", 0),
            "reason": cmp.get("reason"),
        }
        if cmp.get("benchmark_status") != "gold_scored" or not cmp.get("gold_scored", True):
            continue
        comparison_by_paper[p.paper_id] = cmp
        gold_rows_by_paper[p.paper_id] = cmp.get("gold_rows", 0)
        # Same sheet for every paper — capture the missing-column surface once.
        if not gold_missing_columns:
            gold_missing_columns = cmp.get("gold_missing_columns", []) or []
        vd = compute_value_diff(
            cmp, gold_config=gold_config, rel_tol=rel_tol, adjudications=adjudications
        )
        per_paper_mismatches[p.paper_id] = vd["mismatch_count"]
        per_paper_unmatched_gold[p.paper_id] = vd.get("unmatched_gold_with_values", 0)
        total_mismatches += vd["mismatch_count"]
        total_unmatched_gold += vd.get("unmatched_gold_with_values", 0)
        total_adjudicated_gold_errors += len(vd.get("adjudicated_gold_errors", []))
        total_fields_compared += vd.get("fields_compared", 0)
        if vd.get("ambiguous_pairings"):
            ambiguous_pairings[p.paper_id] = vd["ambiguous_pairings"]

    # Aggregate evaluation deliberately excludes papers with no gold rows. Keep
    # those visible as precision-only benchmark statuses so reviewer/export
    # surfaces do not mistake "not in gold" for "not evaluated".
    for extraction_path in sorted(Path(extractions_dir).glob("*.jsonl")):
        paper_id = extraction_path.stem
        if paper_id in benchmark_statuses:
            continue
        cmp = prepare_comparison(paper_id, extractions_dir, gold_standard, document_id_map)
        status = cmp.get("benchmark_status", "gold_scored")
        if status == "gold_scored" and cmp.get("gold_scored", True):
            continue
        benchmark_statuses[paper_id] = {
            "benchmark_status": status,
            "gold_scored": bool(cmp.get("gold_scored", False)),
            "precision_only": bool(cmp.get("precision_only", False)),
            "gold_rows": cmp.get("gold_rows", 0),
            "extraction_rows": cmp.get("extraction_rows", 0),
            "reason": cmp.get("reason"),
        }

    # review note: finding_accuracy is recomputed in code from per-finding
    # records whenever a finding-scores file is supplied. A bare scalar is a
    # flagged degraded mode that cannot satisfy convergence.
    finding_scores_summary: dict | None = None
    semantic_value_gate: dict | None = None
    if finding_scores_path is not None:
        finding_accuracy, finding_scores_summary = load_finding_scores(
            finding_scores_path, gold_rows_by_paper, comparisons=comparison_by_paper
        )
        finding_accuracy_source = "per_finding"
        structured = finding_scores_summary.get("structured_adjudications") or {}
        if structured.get("all_findings_structured"):
            semantic_value_gate = compute_semantic_value_gate(
                finding_scores=json.loads(Path(finding_scores_path).read_text()),
                comparisons=comparison_by_paper,
                rel_tol=rel_tol,
                require_structured=True,
            )
    elif finding_accuracy is not None:
        finding_accuracy_source = "unvalidated_scalar"
    else:
        finding_accuracy_source = None
    papers_without_finding_scores = sorted(
        set(gold_rows_by_paper)
        - set((finding_scores_summary or {}).get("papers_scored", []))
    ) if finding_scores_summary else sorted(gold_rows_by_paper)

    field_fill = (
        sum(sum(p.field_fill_rates.values()) / len(p.field_fill_rates)
            for p in agg.papers if p.field_fill_rates) / len(agg.papers)
        if agg.papers else 0.0
    )

    semantic_field_overlap: dict | None = None
    if semantic_field_overlap_path is not None:
        semantic_field_overlap = json.loads(semantic_field_overlap_path.read_text())

    composite_inputs, composite_input_sources = _full_mode_metric_inputs(
        raw_row_coverage=agg.mean_row_coverage,
        raw_entity_coverage=agg.mean_entity_coverage,
        raw_field_fill_rate=field_fill,
        finding_scores_summary=finding_scores_summary,
        semantic_field_overlap=semantic_field_overlap,
    )
    semantic_recall = (finding_scores_summary or {}).get("semantic_recall") or {}
    semantic_missing_gold = semantic_recall.get("missing_gold_rows")
    recall_gate_source = "deterministic_value_diff"
    unmatched_gold_for_gate = total_unmatched_gold
    per_paper_unmatched_gold_for_gate = per_paper_unmatched_gold
    if isinstance(semantic_missing_gold, int):
        recall_gate_source = "finding_scores.semantic_recall"
        unmatched_gold_for_gate = semantic_missing_gold
        per_paper_unmatched_gold_for_gate = {
            pid: body.get("missing_gold_rows", 0)
            for pid, body in (semantic_recall.get("per_paper") or {}).items()
        }

    gate_value_mismatches = total_mismatches
    gate_unmatched_gold = unmatched_gold_for_gate
    value_gate_source = "legacy_value_diff"
    if semantic_value_gate is not None:
        gate_value_mismatches = semantic_value_gate["mismatch_count"]
        gate_unmatched_gold = semantic_value_gate["missing_gold_with_values"]
        value_gate_source = "semantic_value_gate"

    result = gating_composite(
        row_coverage=composite_inputs["row_coverage"],
        entity_coverage=composite_inputs["entity_coverage"],
        field_fill_rate=composite_inputs["field_fill_rate"],
        finding_accuracy=finding_accuracy,
        value_mismatch_count=gate_value_mismatches,
        unmatched_gold_with_values=gate_unmatched_gold,
    )

    sizes = measure_instruction_sizes(agents_dir) if agents_dir else {}

    # ----- Gate-vacuity guard (review note) -----------------------------------
    # The value/recall gates pass when their counts are zero — which is also
    # what happens when the comparison machinery never engaged (wrong or
    # missing gold_config column names → nothing compared → vacuous pass).
    # A gate that never ran must fail shut, and a fully-unmapped value map is
    # always a configuration bug, never a valid state.
    gold_rows_total = sum(gold_rows_by_paper.values())
    value_map_columns_missing = sorted(
        set(gold_config.value_column_map) & set(gold_missing_columns)
    )
    if gold_rows_by_paper and len(value_map_columns_missing) == len(gold_config.value_column_map):
        raise ValueError(
            "Gate-vacuity check (review note): NONE of the gold_config "
            f"value_column_map columns {sorted(gold_config.value_column_map)} "
            "exist in the gold-standard sheet — the deterministic value gate "
            "and recall gate would pass vacuously. Fix eval_config/gold_config.json "
            f"to name real gold columns. Missing: {value_map_columns_missing}."
        )
    value_gate_vacuous = (
        gold_rows_total > 0
        and total_fields_compared == 0
        and total_unmatched_gold == 0
    )

    # Gold-side internal consistency (review note): the gold standard is
    # corrigible, so screen it with the same deterministic checks the
    # extraction validator applies to extraction rows. Warnings, never gates.
    gold_df = load_gold_standard(gold_standard, gold_config=gold_config)
    gold_warnings = check_gold_consistency(gold_df, gold_config)

    # Convergence eligibility is a conjunction the trajectory can trust.
    # In full semantic mode, recall is derived from validated per-gold-row
    # adjudications. Deterministic value diff remains an audit surface, but
    # deterministic candidate pairing must not define recall when the LLM has
    # semantically adjudicated every gold row.
    deterministic_vacuity_blocks = (
        value_gate_vacuous and finding_accuracy_source != "per_finding"
    )
    convergence_eligible = (
        result.mode == "full"
        and finding_accuracy_source == "per_finding"
        and result.value_gate_passed
        and result.coverage_gate_passed
        and not papers_without_finding_scores
        and not deterministic_vacuity_blocks
        and not value_map_columns_missing
    )

    return {
        "iteration": iteration,
        "composite_score": result.composite,
        "composite_mode": result.mode,
        "raw_composite": result.composite,
        "value_mismatch_count": gate_value_mismatches,
        "legacy_value_mismatch_count": total_mismatches,
        "semantic_value_mismatch_count": (
            semantic_value_gate.get("mismatch_count") if semantic_value_gate else None
        ),
        "value_gate_source": value_gate_source,
        "value_gate_passed": result.value_gate_passed,
        # R3-01: a gate that never engaged must be distinguishable from a gate
        # that passed. Vacuous = gold rows exist but zero mapped numeric values
        # were seen by either the comparison or the recall side.
        "value_gate_vacuous": value_gate_vacuous,
        "gold_missing_columns": gold_missing_columns,
        "value_map_columns_missing": value_map_columns_missing,
        "per_paper_value_mismatches": per_paper_mismatches,
        "benchmark_statuses": benchmark_statuses,
        "precision_only_papers": sorted(
            pid for pid, body in benchmark_statuses.items() if body.get("precision_only")
        ),
        # --- recall gate (review note; design decision: 100% recall vs gold) ---
        "unmatched_gold_with_values": gate_unmatched_gold,
        "per_paper_unmatched_gold": per_paper_unmatched_gold_for_gate,
        "recall_gate_source": recall_gate_source,
        "deterministic_unmatched_gold_with_values": total_unmatched_gold,
        "deterministic_per_paper_unmatched_gold": per_paper_unmatched_gold,
        "coverage_gate_passed": result.coverage_gate_passed,
        "adjudicated_gold_errors": total_adjudicated_gold_errors,
        "ambiguous_pairings": ambiguous_pairings,
        # --- accuracy decomposition (review note): what was confirmed by
        # code vs judged semantically, so the headline number is auditable ---
        "finding_accuracy": finding_accuracy,
        "finding_accuracy_source": finding_accuracy_source,
        "finding_scores_summary": finding_scores_summary,
        "semantic_value_gate": semantic_value_gate,
        "papers_without_finding_scores": papers_without_finding_scores,
        # R3-07: gold-side internal consistency screen (warnings, never gates —
        # gold is corrigible; these are adjudication inputs).
        "gold_consistency": {
            "warning_count": len(gold_warnings),
            "warnings": gold_warnings,
        },
        "accuracy_decomposition": {
            "gold_rows_total": gold_rows_total,
            "deterministic_fields_compared": total_fields_compared,
            "deterministic_mismatches": total_mismatches,
            "value_gate_source": value_gate_source,
            "semantic_value_gate_source": str(semantic_value_gate_output)
            if semantic_value_gate_output and semantic_value_gate
            else None,
            "unmatched_gold_with_values": gate_unmatched_gold,
            "deterministic_unmatched_gold_with_values": total_unmatched_gold,
            "recall_gate_source": recall_gate_source,
            "adjudicated_gold_errors": total_adjudicated_gold_errors,
            "semantic_findings_scored": (finding_scores_summary or {}).get("finding_count", 0),
            "semantic_source": str(finding_scores_path) if finding_scores_path else None,
            "semantic_field_overlap_source": str(semantic_field_overlap_path)
            if semantic_field_overlap_path
            else None,
        },
        "semantic_field_value_overlap": (semantic_field_overlap or {}).get("aggregate"),
        "convergence_eligible": convergence_eligible,
        "row_coverage": round(composite_inputs["row_coverage"], 4),
        "mean_gold_row_completeness": round(composite_inputs["row_coverage"], 4),
        "entity_coverage": round(composite_inputs["entity_coverage"], 4),
        "any_representation_rate": round(composite_inputs["entity_coverage"], 4),
        "field_value_recoverability_rate": round(composite_inputs["entity_coverage"], 4),
        "field_fill_rate": round(composite_inputs["field_fill_rate"], 4),
        "composite_input_sources": composite_input_sources,
        "raw_structural_metrics": {
            "row_coverage": agg.mean_row_coverage,
            "entity_coverage": agg.mean_entity_coverage,
            "field_fill_rate": round(field_fill, 4),
            "definition": (
                "Raw aggregate diagnostics: row count ratio, deterministic "
                "identity resolution, and all-schema field fill. In full "
                "semantic mode these are not the source of truth for recall."
            ),
        },
        "schema_version": schema_version,
        "gold_hash": _gold_hash(gold_standard),
        "papers_evaluated": [p.paper_id for p in agg.papers],
        "instruction_sizes": sizes,
        "total_instruction_words": sum(sizes.values()),
        "note": (
            "Composite computed in code (review note). agent_adjusted metrics are "
            "report-only. finding_accuracy recomputed from per-finding records "
            "when --finding-scores is given; a bare --finding-accuracy "
            "scalar is a degraded mode that cannot satisfy convergence. "
            "Recall vs gold is a hard convergence gate and is derived "
            "from finding_scores.json when per-finding semantic adjudications "
            "are available."
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Code-computed gating composite")
    ap.add_argument("--extractions-dir", required=True)
    ap.add_argument("--gold-standard", required=True)
    ap.add_argument("--document-id-map", required=True)
    ap.add_argument("--finding-accuracy", type=float, default=None,
                    help="DEGRADED mode: bare scalar, flagged unvalidated_scalar, "
                         "cannot satisfy convergence. Prefer --finding-scores.")
    ap.add_argument("--finding-scores", default=None,
                    help="Per-finding scores JSON (one record per gold row, with "
                         "reasoning); finding_accuracy is recomputed in code.")
    ap.add_argument("--semantic-field-overlap", default=None,
                    help="Semantic field-value overlap JSON; its aggregate metrics "
                         "are embedded in score.json for reviewer/report surfaces.")
    ap.add_argument("--semantic-value-gate-output", default=None,
                    help="Optional path to write semantic_value_gate.json when "
                         "structured finding adjudications are present.")
    ap.add_argument("--iteration", type=int, required=True)
    ap.add_argument("--schema-version", default=None)
    ap.add_argument("--agents-dir", default=None)
    ap.add_argument("--rel-tol", type=float, default=0.05)
    ap.add_argument("--output", default=None)
    a = ap.parse_args()

    out = compute_score(
        extractions_dir=Path(a.extractions_dir),
        gold_standard=Path(a.gold_standard),
        document_id_map=Path(a.document_id_map),
        finding_accuracy=a.finding_accuracy,
        iteration=a.iteration,
        schema_version=a.schema_version,
        agents_dir=Path(a.agents_dir) if a.agents_dir else None,
        rel_tol=a.rel_tol,
        finding_scores_path=Path(a.finding_scores) if a.finding_scores else None,
        semantic_field_overlap_path=Path(a.semantic_field_overlap)
        if a.semantic_field_overlap
        else None,
        semantic_value_gate_output=Path(a.semantic_value_gate_output)
        if a.semantic_value_gate_output
        else None,
    )
    if a.semantic_value_gate_output and out.get("semantic_value_gate"):
        gate_path = Path(a.semantic_value_gate_output)
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        gate_path.write_text(json.dumps(out["semantic_value_gate"], indent=2) + "\n")
    text = json.dumps(out, indent=2, default=str)
    if a.output:
        Path(a.output).parent.mkdir(parents=True, exist_ok=True)
        Path(a.output).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
