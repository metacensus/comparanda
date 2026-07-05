"""Validate and summarize field-level gold coverage adjudications."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from harness.eval.field_checklist import build_checklist_for_comparison
from harness.eval.finding_adjudication import evaluate_expression


ROLES = {
    "identity_context",
    "value",
    "method_context",
    "scope_context",
    "metadata",
    "out_of_benchmark_scope",
    "unknown",
}

STATUSES = {
    "represented_exact",
    "represented_by_derivation",
    "represented_by_pooled_value",
    "represented_partial",
    "used_for_identity_matching",
    "not_represented",
    "not_applicable",
    "out_of_benchmark_scope",
    "gold_error",
    "source_conflict",
}

REPRESENTATION_KINDS = {
    "direct",
    "derived",
    "inferred",
    "partial",
    "identity_only",
    "missing",
    "not_applicable",
    "out_of_scope",
    "gold_error",
    "source_conflict",
}

ANY_REPRESENTED = {
    "represented_exact",
    "represented_by_derivation",
    "represented_by_pooled_value",
    "represented_partial",
    "used_for_identity_matching",
    "gold_error",
}

FULL_REPRESENTED = {
    "represented_exact",
    "represented_by_derivation",
    "used_for_identity_matching",
    "gold_error",
}


def _extract_rows(comparison: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for pair in comparison.get("paired_rows", []) or []:
        for candidate in pair.get("extraction_candidates") or []:
            idx = candidate.get("extraction_index")
            if isinstance(idx, int):
                rows[idx] = candidate
    for row in comparison.get("unmatched_extraction_rows", []) or []:
        idx = row.get("extraction_index")
        if isinstance(idx, int):
            rows[idx] = row
    return rows


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _adjudications_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("field_adjudications"), list):
        return payload["field_adjudications"]
    papers = payload.get("papers")
    if isinstance(papers, dict):
        rows: list[dict[str, Any]] = []
        for paper in papers.values():
            if isinstance(paper, dict) and isinstance(paper.get("field_adjudications"), list):
                rows.extend(paper["field_adjudications"])
        return rows
    return []


def _validate_refs(
    refs: Any,
    extraction_rows: dict[int, dict[str, Any]],
    *,
    checklist_id: str,
) -> list[str]:
    problems: list[str] = []
    if refs in (None, []):
        return problems
    if not isinstance(refs, list):
        return [f"{checklist_id}: extraction_refs must be a list"]
    for ref in refs:
        if not isinstance(ref, dict):
            problems.append(f"{checklist_id}: extraction ref must be an object")
            continue
        row = ref.get("row")
        field = ref.get("field")
        if not isinstance(row, int):
            problems.append(f"{checklist_id}: extraction ref row must be an int")
            continue
        if row not in extraction_rows:
            problems.append(f"{checklist_id}: extraction ref row {row} does not exist")
            continue
        if field is not None:
            if not isinstance(field, str):
                problems.append(f"{checklist_id}: extraction ref field must be a string")
            elif field not in extraction_rows[row]:
                problems.append(f"{checklist_id}: extraction ref field {row}.{field} does not exist")
    return problems


def _validate_derivation(
    derivation: Any,
    extraction_rows: dict[int, dict[str, Any]],
    *,
    checklist_id: str,
) -> tuple[bool, list[str]]:
    if derivation in (None, {}):
        return False, []
    if not isinstance(derivation, dict):
        return False, [f"{checklist_id}: derivation must be an object"]
    _, problems = evaluate_expression(derivation, extraction_rows)
    return True, [f"{checklist_id}: {problem}" for problem in problems]


def validate_field_coverage(
    *,
    checklist_rows: list[dict[str, Any]],
    field_coverage: dict[str, Any],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    checklist_by_id = {row["checklist_id"]: row for row in checklist_rows}
    extraction_rows = _extract_rows(comparison)
    adjudications = _adjudications_from_payload(field_coverage)
    problems: list[str] = []
    seen: set[str] = set()
    status_counts: Counter[str] = Counter()
    representation_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    provenance_counts: Counter[str] = Counter()
    derivation_count = 0
    missing_item_count = 0
    gold_error_item_count = 0
    source_conflict_item_count = 0

    if not adjudications:
        problems.append("field coverage payload must include field_adjudications")

    for idx, adj in enumerate(adjudications):
        if not isinstance(adj, dict):
            problems.append(f"adjudication[{idx}]: must be an object")
            continue
        checklist_id = adj.get("checklist_id")
        if not isinstance(checklist_id, str):
            problems.append(f"adjudication[{idx}]: checklist_id must be a string")
            continue
        if checklist_id not in checklist_by_id:
            problems.append(f"{checklist_id}: checklist_id not found in checklist")
            continue
        if checklist_id in seen:
            problems.append(f"{checklist_id}: duplicate adjudication")
            continue
        seen.add(checklist_id)

        for key in (
            "paper_id",
            "gold_index",
            "gold_field",
            "gold_value",
            "role",
            "status",
            "representation_kind",
            "extraction_refs",
            "derivation",
            "rationale",
            "provenance_checked",
            "confidence",
        ):
            if key not in adj:
                problems.append(f"{checklist_id}: missing required key {key}")
        checklist_item = checklist_by_id[checklist_id]
        if adj.get("paper_id") != checklist_item.get("paper_id"):
            problems.append(f"{checklist_id}: paper_id does not match checklist")
        if adj.get("gold_index") != checklist_item.get("gold_index"):
            problems.append(f"{checklist_id}: gold_index does not match checklist")
        if adj.get("gold_field") != checklist_item.get("gold_field"):
            problems.append(f"{checklist_id}: gold_field does not match checklist")
        if adj.get("gold_value") != checklist_item.get("gold_value"):
            problems.append(f"{checklist_id}: gold_value does not match checklist")

        role = adj.get("role")
        if role not in ROLES:
            problems.append(f"{checklist_id}: role {role!r} is not allowed")
        else:
            role_counts[role] += 1

        status = adj.get("status")
        if status not in STATUSES:
            problems.append(f"{checklist_id}: status {status!r} is not allowed")
        else:
            status_counts[status] += 1

        representation_kind = adj.get("representation_kind")
        if representation_kind not in REPRESENTATION_KINDS:
            problems.append(
                f"{checklist_id}: representation_kind {representation_kind!r} is not allowed"
            )
        else:
            representation_counts[representation_kind] += 1

        if status == "not_represented" or representation_kind == "missing":
            missing_item_count += 1
        if status == "gold_error" or representation_kind == "gold_error":
            gold_error_item_count += 1
        if status == "source_conflict" or representation_kind == "source_conflict":
            source_conflict_item_count += 1

        rationale = adj.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            problems.append(f"{checklist_id}: rationale is required")

        confidence = adj.get("confidence")
        if confidence is not None and (
            not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0
        ):
            problems.append(f"{checklist_id}: confidence must be in [0,1]")

        provenance_checked = adj.get("provenance_checked", False)
        provenance_counts[str(bool(provenance_checked)).lower()] += 1

        problems.extend(
            _validate_refs(
                adj.get("extraction_refs"),
                extraction_rows,
                checklist_id=checklist_id,
            )
        )
        has_derivation, derivation_problems = _validate_derivation(
            adj.get("derivation"),
            extraction_rows,
            checklist_id=checklist_id,
        )
        derivation_count += int(has_derivation)
        problems.extend(derivation_problems)
        derivation = adj.get("derivation")
        if (
            isinstance(derivation, dict)
            and derivation.get("op") == "taxonomy_subsumption"
            and derivation.get("direction") == "extraction_supertype_of_gold"
            and (status != "not_represented" or representation_kind != "missing")
        ):
            problems.append(
                f"{checklist_id}: extraction_supertype_of_gold is a near-miss and "
                "must use status 'not_represented' with representation_kind 'missing'"
            )

    missing = sorted(set(checklist_by_id) - seen)
    for checklist_id in missing[:100]:
        problems.append(f"{checklist_id}: missing adjudication")
    if len(missing) > 100:
        problems.append(f"... {len(missing) - 100} additional missing adjudications")

    total = len(checklist_rows)
    out_of_scope = status_counts["out_of_benchmark_scope"]
    not_applicable = status_counts["not_applicable"]
    denominator = max(0, total - out_of_scope - not_applicable)
    represented_any = sum(status_counts[status] for status in ANY_REPRESENTED)
    represented_full = sum(status_counts[status] for status in FULL_REPRESENTED)
    direct = representation_counts["direct"]
    derived = representation_counts["derived"]
    inferred = representation_counts["inferred"]
    partial = representation_counts["partial"]

    def rate(count: int) -> float:
        return round(count / denominator, 6) if denominator else 0.0

    return {
        "mode": "field_coverage_validation",
        "paper_id": comparison.get("paper_id"),
        "valid": not problems,
        "problems": problems,
        "gold_field_value_count": total,
        "applicable_gold_field_value_count": denominator,
        "adjudication_count": len(adjudications),
        "represented_any_count": represented_any,
        "represented_full_count": represented_full,
        "field_value_recoverable_count": represented_any,
        "field_value_fully_represented_count": represented_full,
        "direct_count": direct,
        "derived_count": derived,
        "inferred_count": inferred,
        "partial_count": partial,
        "missing_count": missing_item_count,
        "identity_only_count": representation_counts["identity_only"],
        "out_of_scope_count": out_of_scope,
        "not_applicable_count": not_applicable,
        "gold_error_count": gold_error_item_count,
        "source_conflict_count": source_conflict_item_count,
        "derivation_count": derivation_count,
        "rates": {
            "any_representation_rate": rate(represented_any),
            "full_representation_rate": rate(represented_full),
            "field_value_recoverability_rate": rate(represented_any),
            "field_value_representation_rate": rate(represented_full),
            "direct_representation_rate": rate(direct),
            "derived_representation_rate": rate(derived),
            "inferred_representation_rate": rate(inferred),
            "partial_representation_rate": rate(partial),
            "missing_rate": rate(missing_item_count),
        },
        "status_counts": dict(status_counts),
        "representation_kind_counts": dict(representation_counts),
        "role_counts": dict(role_counts),
        "provenance_checked_counts": dict(provenance_counts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", required=True)
    parser.add_argument("--checklist")
    parser.add_argument("--field-coverage", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    comparison = json.loads(Path(args.comparison).read_text())
    checklist_rows = (
        load_jsonl(Path(args.checklist))
        if args.checklist
        else build_checklist_for_comparison(comparison)
    )
    field_coverage = json.loads(Path(args.field_coverage).read_text())
    report = validate_field_coverage(
        checklist_rows=checklist_rows,
        field_coverage=field_coverage,
        comparison=comparison,
    )
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n")
    else:
        print(payload)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
