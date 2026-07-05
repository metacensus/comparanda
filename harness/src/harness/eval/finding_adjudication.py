"""Structured finding-adjudication validation for detailed eval artifacts.

The eval agent's semantic judgment remains the source of truth for hard
matching cases, but the judgment must be machine-readable enough for code to
check denominators, row references, and declared numeric transforms. This
module deliberately stays domain-agnostic: it knows about rows, fields, values,
and a small transform vocabulary, not any one scientific domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any


MATCH_STATUSES = {
    "represented",
    "represented_full",
    "represented_partial",
    "missing",
    "value_mismatch",
    "gold_error",
    "not_in_benchmark",
}

FIELD_STATUSES = {
    "represented",
    "represented_exact",
    "represented_by_transform",
    "represented_by_pooled_value",
    "represented_partial",
    "missing",
    "value_mismatch",
    "gold_error",
    "not_in_benchmark",
}

TRANSFORM_OPS = {
    "direct",
    "alias_equivalent",
    "taxonomy_subsumption",
    "complement",
    "sum",
    "pooled_sum",
    "zero_count",
    "gold_error",
    "not_in_benchmark",
}

TAXONOMY_SUBSUMPTION_DIRECTIONS = {
    "extraction_subtype_of_gold",
    "extraction_supertype_of_gold",
}

PARTIAL_RECOVERABILITY = {
    "non_invertible_partial",
    "arm_identity_unavailable",
    "partial",
}


@dataclass
class ValidationResult:
    products: list[float] = field(default_factory=list)
    per_paper_accuracy: dict[str, float] = field(default_factory=dict)
    per_paper_recall: dict[str, dict[str, Any]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    finding_count: int = 0
    structured_count: int = 0
    transform_count: int = 0
    invalid_transform_count: int = 0
    value_mismatch_count: int = 0
    missing_gold_with_values: int = 0
    gold_error_count: int = 0


def row_fingerprint(row: dict[str, Any]) -> str:
    """Stable, short fingerprint for audit linking without domain assumptions."""
    import json

    payload = json.dumps(row, sort_keys=True, default=str, ensure_ascii=False)
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()[:16]


def has_structured_adjudication(finding: dict[str, Any]) -> bool:
    return any(
        key in finding
        for key in ("match", "field_adjudications", "identity_basis", "candidate_audit")
    )


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


def _close(a: float, b: float, *, rel_tol: float = 0.05) -> bool:
    denom = max(abs(a), 1e-9)
    return abs(a - b) / denom <= rel_tol


def _extract_rows(comparison: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for pair in comparison.get("paired_rows", []) or []:
        for candidate in pair.get("extraction_candidates", []) or []:
            idx = candidate.get("extraction_index")
            if isinstance(idx, int):
                rows[idx] = candidate
    for row in comparison.get("unmatched_extraction_rows", []) or []:
        idx = row.get("extraction_index")
        if isinstance(idx, int):
            rows[idx] = row
    return rows


def _gold_by_index(comparison: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for pair in comparison.get("paired_rows", []) or []:
        idx = pair.get("gold_index")
        if isinstance(idx, int):
            rows[idx] = pair.get("gold_fields") or {}
    return rows


def _numeric_gold_fields(gold_fields: dict[str, Any]) -> list[str]:
    return [field for field, value in gold_fields.items() if _to_float(value) is not None]


def _ref_value(ref: dict[str, Any], extraction_rows: dict[int, dict[str, Any]]) -> tuple[float | None, str | None]:
    row_idx = ref.get("row")
    field_name = ref.get("field")
    if not isinstance(row_idx, int) or not isinstance(field_name, str):
        return None, "reference must include integer row and string field"
    row = extraction_rows.get(row_idx)
    if row is None:
        return None, f"referenced extraction row {row_idx} does not exist"
    if field_name not in row:
        return None, f"referenced field {field_name!r} is absent from extraction row {row_idx}"
    value = _to_float(row.get(field_name))
    if value is None:
        return None, f"referenced value row {row_idx}.{field_name} is not numeric"
    return value, None


def _ref_raw(ref: dict[str, Any], extraction_rows: dict[int, dict[str, Any]]) -> tuple[Any, str | None]:
    row_idx = ref.get("row")
    field_name = ref.get("field")
    if not isinstance(row_idx, int) or not isinstance(field_name, str):
        return None, "reference must include integer row and string field"
    row = extraction_rows.get(row_idx)
    if row is None:
        return None, f"referenced extraction row {row_idx} does not exist"
    if field_name not in row:
        return None, f"referenced field {field_name!r} is absent from extraction row {row_idx}"
    return row.get(field_name), None


def evaluate_expression(
    expression: dict[str, Any],
    extraction_rows: dict[int, dict[str, Any]],
) -> tuple[Any, list[str]]:
    """Evaluate the limited transform expression vocabulary."""
    if not isinstance(expression, dict):
        return None, ["extraction_expression must be an object"]
    op = expression.get("op")
    if op not in TRANSFORM_OPS:
        return None, [f"unknown transform op {op!r}"]

    problems: list[str] = []
    if op in {"gold_error", "not_in_benchmark", "alias_equivalent"}:
        return None, []
    if op == "taxonomy_subsumption":
        direction = expression.get("direction")
        source_value = expression.get("source_value")
        target_value = expression.get("target_value")
        if direction not in TAXONOMY_SUBSUMPTION_DIRECTIONS:
            problems.append(
                "taxonomy_subsumption requires direction "
                f"in {sorted(TAXONOMY_SUBSUMPTION_DIRECTIONS)!r}"
            )
        if not isinstance(source_value, str) or not source_value.strip():
            problems.append("taxonomy_subsumption requires non-empty source_value")
        if not isinstance(target_value, str) or not target_value.strip():
            problems.append("taxonomy_subsumption requires non-empty target_value")
        return None, problems
    if op == "zero_count":
        return 0.0, []
    if op == "direct":
        raw, problem = _ref_raw(expression, extraction_rows)
        if problem:
            return None, [problem]
        value = _to_float(raw)
        if value is None:
            return raw, []
        return value, [problem] if problem else []
    if op in {"sum", "pooled_sum"}:
        if op == "pooled_sum" and isinstance(expression.get("row"), int) and isinstance(
            expression.get("field"), str
        ):
            return _ref_value(expression, extraction_rows)
        total = 0.0
        terms = expression.get("terms")
        if terms is None and isinstance(expression.get("rows"), list) and isinstance(
            expression.get("field"), str
        ):
            terms = [
                {"row": row_idx, "field": expression.get("field")}
                for row_idx in expression.get("rows")
            ]
        if not isinstance(terms, list) or not terms:
            return None, [f"{op} requires a non-empty terms list"]
        for term in terms:
            value, problem = _ref_value(term, extraction_rows)
            if problem:
                problems.append(problem)
            elif value is not None:
                total += value
        return (None if problems else total), problems
    if op == "complement":
        if "minuend" in expression or "subtrahend" in expression:
            minuend_ref = expression.get("minuend")
            sub_ref = expression.get("subtrahend")
        else:
            row_idx = expression.get("row")
            minuend_ref = {"row": row_idx, "field": expression.get("total_field")}
            sub_ref = {"row": row_idx, "field": expression.get("event_field")}
        if not isinstance(minuend_ref, dict) or not isinstance(sub_ref, dict):
            return None, ["complement requires minuend/subtrahend references"]
        minuend, problem = _ref_value(minuend_ref, extraction_rows)
        if problem:
            problems.append(problem)
        subtrahend, problem = _ref_value(sub_ref, extraction_rows)
        if problem:
            problems.append(problem)
        return (
            None if problems or minuend is None or subtrahend is None else minuend - subtrahend,
            problems,
        )
    return None, [f"unsupported transform op {op!r}"]


def validate_field_adjudication(
    *,
    paper_id: str,
    gold_index: int,
    field_adjudication: dict[str, Any],
    extraction_rows: dict[int, dict[str, Any]],
    rel_tol: float,
) -> tuple[list[str], dict[str, int]]:
    problems: list[str] = []
    counts = {
        "transform_count": 0,
        "invalid_transform_count": 0,
        "value_mismatch_count": 0,
        "gold_error_count": 0,
    }
    if not isinstance(field_adjudication, dict):
        return [f"{paper_id}[gold_index={gold_index}]: field adjudication must be an object"], counts

    status = field_adjudication.get("status")
    if status not in FIELD_STATUSES:
        problems.append(
            f"{paper_id}[gold_index={gold_index}]: unknown field adjudication status {status!r}"
        )
    if status == "value_mismatch":
        counts["value_mismatch_count"] += 1
    if status == "gold_error":
        counts["gold_error_count"] += 1

    explanation = field_adjudication.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        problems.append(
            f"{paper_id}[gold_index={gold_index}]: field adjudication needs explanation"
        )

    expression = field_adjudication.get("extraction_expression")
    if expression is None:
        return problems, counts

    counts["transform_count"] += 1
    computed, transform_problems = evaluate_expression(expression, extraction_rows)
    if transform_problems:
        counts["invalid_transform_count"] += 1
        problems.extend(
            f"{paper_id}[gold_index={gold_index}]: {problem}"
            for problem in transform_problems
        )
        return problems, counts

    declared = _to_float(field_adjudication.get("extraction_value"))
    computed_numeric = _to_float(computed)
    if (
        declared is not None
        and computed_numeric is not None
        and not _close(declared, computed_numeric, rel_tol=rel_tol)
    ):
        counts["invalid_transform_count"] += 1
        problems.append(
            f"{paper_id}[gold_index={gold_index}]: declared extraction_value "
            f"{declared} != transform result {computed_numeric}"
        )

    gold_value = _to_float(field_adjudication.get("gold_value"))
    recoverability = field_adjudication.get("recoverability")
    op = expression.get("op") if isinstance(expression, dict) else None
    partial = (
        status in {"represented_by_pooled_value", "represented_partial"}
        or recoverability in PARTIAL_RECOVERABILITY
        or op == "pooled_sum"
    )
    if (
        gold_value is not None
        and computed_numeric is not None
        and not partial
        and status != "gold_error"
        and op
        not in {"gold_error", "not_in_benchmark", "alias_equivalent", "taxonomy_subsumption"}
        and not _close(gold_value, computed_numeric, rel_tol=rel_tol)
    ):
        counts["value_mismatch_count"] += 1
        problems.append(
            f"{paper_id}[gold_index={gold_index}]: transform result {computed_numeric} "
            f"does not match gold_value {gold_value}"
        )

    return problems, counts


def validate_finding_scores(
    payload: dict[str, Any],
    gold_rows_by_paper: dict[str, int],
    *,
    comparisons: dict[str, dict[str, Any]] | None = None,
    require_structured: bool = False,
    rel_tol: float = 0.05,
) -> ValidationResult:
    """Validate finding scores and recompute product metrics.

    Legacy finding-score records are still accepted unless `require_structured`
    is true. Structured records receive additional validation of match status,
    extraction-row references, field adjudications, and transforms.
    """
    result = ValidationResult()
    papers = payload.get("papers")
    if not isinstance(papers, dict) or not papers:
        result.problems.append("finding-scores file must have a non-empty 'papers' object")
        return result

    comparisons = comparisons or {}
    total_captured = 0
    total_partial = 0
    total_missing = 0
    total_inaccurate = 0
    total_completeness = 0.0

    for pid, body in papers.items():
        findings = body.get("findings") if isinstance(body, dict) else None
        if not isinstance(findings, list) or not findings:
            result.problems.append(f"{pid}: missing or empty 'findings' list")
            continue
        expected = gold_rows_by_paper.get(pid)
        if expected is None:
            result.problems.append(f"{pid}: not in the evaluated paper set")
            continue

        comparison = comparisons.get(pid) or {}
        extraction_rows = _extract_rows(comparison)
        gold_rows = _gold_by_index(comparison)
        idxs: list[int] = []
        paper_products: list[float] = []
        paper_captured = 0
        paper_partial = 0
        paper_missing = 0
        paper_inaccurate = 0
        paper_completeness = 0.0

        for i, finding in enumerate(findings):
            if not isinstance(finding, dict):
                result.problems.append(f"{pid}[{i}]: finding must be an object")
                continue
            gold_index = finding.get("gold_index")
            completeness = finding.get("completeness")
            accuracy = finding.get("accuracy")
            reasoning = finding.get("reasoning") or finding.get("human_rationale")
            structured = has_structured_adjudication(finding)
            if structured:
                result.structured_count += 1
            elif require_structured:
                result.problems.append(
                    f"{pid}[{i}]: structured match/field_adjudications are required"
                )

            if not isinstance(gold_index, int):
                result.problems.append(f"{pid}[{i}]: gold_index must be an int")
                continue
            idxs.append(gold_index)

            for label, value in (("completeness", completeness), ("accuracy", accuracy)):
                if not isinstance(value, (int, float)) or not (0.0 <= value <= 1.0):
                    result.problems.append(
                        f"{pid}[gold_index={gold_index}]: {label} must be in [0,1]"
                    )
            if not isinstance(reasoning, str) or not reasoning.strip():
                result.problems.append(
                    f"{pid}[gold_index={gold_index}]: non-empty reasoning/human_rationale is required"
                )

            if isinstance(completeness, (int, float)) and isinstance(accuracy, (int, float)):
                comp_f = float(completeness)
                acc_f = float(accuracy)
                product = comp_f * acc_f
                paper_products.append(product)
                result.products.append(product)
                paper_completeness += comp_f
                if comp_f <= 0.0:
                    paper_missing += 1
                elif comp_f < 1.0:
                    paper_partial += 1
                else:
                    paper_captured += 1
                if comp_f > 0.0 and acc_f <= 0.0:
                    paper_inaccurate += 1

            if not structured:
                continue

            match = finding.get("match") or {}
            if not isinstance(match, dict):
                result.problems.append(f"{pid}[gold_index={gold_index}]: match must be an object")
            else:
                status = match.get("status")
                if status not in MATCH_STATUSES:
                    result.problems.append(
                        f"{pid}[gold_index={gold_index}]: unknown match.status {status!r}"
                    )
                for key in ("primary_extraction_index",):
                    idx = match.get(key)
                    if idx is not None and (not isinstance(idx, int) or idx not in extraction_rows):
                        result.problems.append(
                            f"{pid}[gold_index={gold_index}]: {key} references missing extraction row {idx!r}"
                        )
                matched = match.get("matched_extraction_indices") or []
                if matched and (
                    not isinstance(matched, list)
                    or any(not isinstance(idx, int) or idx not in extraction_rows for idx in matched)
                ):
                    result.problems.append(
                        f"{pid}[gold_index={gold_index}]: matched_extraction_indices must reference existing rows"
                    )
                if status == "missing" and _numeric_gold_fields(gold_rows.get(gold_index, {})):
                    result.missing_gold_with_values += 1
                if status == "value_mismatch":
                    result.value_mismatch_count += 1
                if status == "gold_error":
                    result.gold_error_count += 1

            human_rationale = finding.get("human_rationale") or finding.get("reasoning")
            if not isinstance(human_rationale, str) or not human_rationale.strip():
                result.problems.append(
                    f"{pid}[gold_index={gold_index}]: structured records require human_rationale"
                )

            field_adjudications = finding.get("field_adjudications")
            if field_adjudications is not None:
                if not isinstance(field_adjudications, list):
                    result.problems.append(
                        f"{pid}[gold_index={gold_index}]: field_adjudications must be a list"
                    )
                else:
                    for adjudication in field_adjudications:
                        problems, counts = validate_field_adjudication(
                            paper_id=pid,
                            gold_index=gold_index,
                            field_adjudication=adjudication,
                            extraction_rows=extraction_rows,
                            rel_tol=rel_tol,
                        )
                        result.problems.extend(problems)
                        result.transform_count += counts["transform_count"]
                        result.invalid_transform_count += counts["invalid_transform_count"]
                        result.value_mismatch_count += counts["value_mismatch_count"]
                        result.gold_error_count += counts["gold_error_count"]

        if len(findings) != expected:
            result.problems.append(
                f"{pid}: {len(findings)} finding records != {expected} gold rows "
                "(denominator rule: every gold row counts, none dropped)"
            )
        if sorted(idxs) != list(range(expected)):
            result.problems.append(
                f"{pid}: gold_index set must be exactly 0..{expected - 1} "
                f"(got {sorted(set(idxs))[:10]}...)"
            )

        if paper_products:
            denom = len(paper_products)
            result.per_paper_accuracy[pid] = round(sum(paper_products) / denom, 4)
            result.per_paper_recall[pid] = {
                "gold_rows": expected,
                "captured_gold_rows": paper_captured,
                "fully_represented_gold_rows": paper_captured,
                "partially_captured_gold_rows": paper_partial,
                "partially_represented_gold_rows": paper_partial,
                "missing_gold_rows": paper_missing,
                "represented_gold_rows": paper_captured + paper_partial,
                "any_representation_gold_rows": paper_captured + paper_partial,
                "inaccurate_represented_gold_rows": paper_inaccurate,
                "mean_completeness": round(paper_completeness / denom, 6),
                "mean_gold_row_completeness": round(paper_completeness / denom, 6),
                "all_in_recall": round((paper_captured + paper_partial) / denom, 6),
                "any_representation_rate": round((paper_captured + paper_partial) / denom, 6),
                "field_value_recoverability_rate": round(
                    (paper_captured + paper_partial) / denom, 6
                ),
                "full_capture_rate": round(paper_captured / denom, 6),
                "full_representation_rate": round(paper_captured / denom, 6),
                "field_value_representation_rate": round(paper_captured / denom, 6),
            }
            total_captured += paper_captured
            total_partial += paper_partial
            total_missing += paper_missing
            total_inaccurate += paper_inaccurate
            total_completeness += paper_completeness

    result.finding_count = len(result.products)
    if result.finding_count:
        result.per_paper_recall["__aggregate__"] = {
            "captured_gold_rows": total_captured,
            "fully_represented_gold_rows": total_captured,
            "partially_captured_gold_rows": total_partial,
            "partially_represented_gold_rows": total_partial,
            "missing_gold_rows": total_missing,
            "represented_gold_rows": total_captured + total_partial,
            "any_representation_gold_rows": total_captured + total_partial,
            "inaccurate_represented_gold_rows": total_inaccurate,
            "mean_completeness": round(total_completeness / result.finding_count, 6),
            "mean_gold_row_completeness": round(
                total_completeness / result.finding_count, 6
            ),
            "all_in_recall": round(
                (total_captured + total_partial) / result.finding_count, 6
            ),
            "any_representation_rate": round(
                (total_captured + total_partial) / result.finding_count, 6
            ),
            "field_value_recoverability_rate": round(
                (total_captured + total_partial) / result.finding_count, 6
            ),
            "full_capture_rate": round(total_captured / result.finding_count, 6),
            "full_representation_rate": round(total_captured / result.finding_count, 6),
            "field_value_representation_rate": round(
                total_captured / result.finding_count, 6
            ),
        }
    return result
