"""Semantic-aware value gate for structured detailed eval adjudications."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from harness.eval.finding_adjudication import validate_finding_scores


def load_comparisons(comparisons_dir: Path) -> dict[str, dict[str, Any]]:
    comparisons: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(comparisons_dir).glob("*.json")):
        body = json.loads(path.read_text())
        paper_id = body.get("paper_id") or path.stem
        if isinstance(paper_id, str):
            comparisons[paper_id] = body
    return comparisons


def gold_rows_by_paper_from_comparisons(
    comparisons: dict[str, dict[str, Any]],
) -> dict[str, int]:
    return {
        paper_id: int(body.get("gold_rows") or 0)
        for paper_id, body in comparisons.items()
        if body.get("benchmark_status", "gold_scored") == "gold_scored"
        and bool(body.get("gold_scored", True))
    }


def compute_semantic_value_gate(
    *,
    finding_scores: dict[str, Any],
    comparisons: dict[str, dict[str, Any]],
    rel_tol: float = 0.05,
    require_structured: bool = True,
) -> dict[str, Any]:
    """Validate structured semantic adjudications and summarize hard gates."""
    all_gold_rows_by_paper = gold_rows_by_paper_from_comparisons(comparisons)
    scored_papers = set((finding_scores.get("papers") or {}).keys())
    gold_rows_by_paper = {
        paper_id: rows
        for paper_id, rows in all_gold_rows_by_paper.items()
        if paper_id in scored_papers
    }
    result = validate_finding_scores(
        finding_scores,
        gold_rows_by_paper,
        comparisons=comparisons,
        require_structured=require_structured,
        rel_tol=rel_tol,
    )
    all_structured = (
        result.finding_count > 0 and result.structured_count == result.finding_count
    )
    gate_failures = []
    if result.problems:
        gate_failures.append("validation_failed")
    if require_structured and not all_structured:
        gate_failures.append("structured_adjudications_incomplete")
    if result.invalid_transform_count:
        gate_failures.append("invalid_transform")
    if result.value_mismatch_count:
        gate_failures.append("value_mismatch")
    if result.missing_gold_with_values:
        gate_failures.append("missing_gold_with_values")
    blocking_issue_count = (
        result.value_mismatch_count
        + result.invalid_transform_count
        + len(result.problems)
        + (0 if all_structured or not require_structured else 1)
    )

    return {
        "mode": "semantic_value_gate",
        "gate_source": "structured_finding_adjudications",
        "rel_tol": rel_tol,
        "value_gate_passed": not gate_failures,
        "mismatch_count": blocking_issue_count,
        "value_mismatch_count": result.value_mismatch_count,
        "missing_gold_with_values": result.missing_gold_with_values,
        "coverage_gate_passed": result.missing_gold_with_values == 0,
        "gate_failures": gate_failures,
        "finding_count": result.finding_count,
        "structured_finding_count": result.structured_count,
        "all_findings_structured": all_structured,
        "transform_count": result.transform_count,
        "invalid_transform_count": result.invalid_transform_count,
        "gold_error_count": result.gold_error_count,
        "gold_scored_papers": sorted(gold_rows_by_paper),
        "gold_rows_by_paper": gold_rows_by_paper,
        "precision_only_papers": sorted(
            paper_id
            for paper_id, body in comparisons.items()
            if body.get("precision_only")
        ),
        "problems": result.problems,
        "note": (
            "This gate validates LLM semantic adjudications as structured data. "
            "Legacy deterministic value_diff remains useful diagnostics, but "
            "does not define convergence when every gold row has a structured "
            "semantic adjudication."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparisons-dir", required=True)
    parser.add_argument("--finding-scores", required=True)
    parser.add_argument("--rel-tol", type=float, default=0.05)
    parser.add_argument(
        "--allow-legacy",
        action="store_true",
        help="Do not fail solely because records lack structured fields.",
    )
    parser.add_argument("--output")
    args = parser.parse_args()

    comparisons = load_comparisons(Path(args.comparisons_dir))
    finding_scores = json.loads(Path(args.finding_scores).read_text())
    report = compute_semantic_value_gate(
        finding_scores=finding_scores,
        comparisons=comparisons,
        rel_tol=args.rel_tol,
        require_structured=not args.allow_legacy,
    )
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n")
    else:
        print(payload)
    return 0 if report["value_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
