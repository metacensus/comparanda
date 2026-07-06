"""Semantic field-value overlap surfaces for Comparanda eval artifacts.

This module answers three different questions that row-pair scoring tends to
collapse:

1. Of the unique field/value concepts extracted, how many are represented in
   the gold benchmark?
2. Of the unique field/value concepts in gold, how many are represented in the
   extraction?
3. Of the extracted high-leverage claims, how many are source-faithful?

The implementation is deliberately conservative. It uses normalized concept
keys as the deterministic fallback, and records row-level LLM adjudications
from finding_scores.json as the semantic basis when available. It does not
pretend that deterministic string overlap is the whole semantic story.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


_NULLISH = {"", "-", "—", "na", "n/a", "none", "not stated", "not_stated", "null"}

_GOLD_FIELD_CONCEPTS = {
    "Entity": "entity",
    "Entity ID": "entity_id",
    "Treatment": "treatment",
    "Treatment ID": "treatment_id",
    "Outcome Category": "outcome_category",
    "Outcome": "outcome",
    "Outcome Clean": "outcome",
    "Outcome Description": "outcome_detail",
    "Grade Clean": "grade",
    "Subjects with Entity": "n_total",
    "Patients with Entity": "n_total",
    "Patients with Outcome": "n_outcome",
    "Patients with Entity and Outcome": "n_events",
    "Comparison Type": "comparison_type",
    "Compared Entity": "compared_entity",
    "Effect Size": "effect_estimate",
    "Lower 95 CI": "ci_lower",
    "Upper 95 CI": "ci_upper",
    "P Value": "p_value",
}

_EXTRACTION_FIELD_CONCEPTS = {
    "entity_raw": "entity",
    "entity_id": "entity_id",
    "entity_components": "entity",
    "entity_grouping_raw": "entity",
    "entity_grouping_id": "entity_id",
    "treatment_raw": "treatment",
    "treatment_id": "treatment_id",
    "outcome_category": "outcome_category",
    "outcome_category_raw": "outcome_category",
    "outcome_raw": "outcome",
    "outcome_id": "outcome",
    "grade_threshold_raw": "grade",
    "grade_threshold_canonical": "grade",
    "n_total": "n_total",
    "n_events": "n_events",
    "effect_estimate": "effect_estimate",
    "ci_lower": "ci_lower",
    "ci_upper": "ci_upper",
    "p_value": "p_value",
    "p_value_raw": "p_value",
    "comparison_group_raw": "comparison_type",
    "comparison_group_canonical": "comparison_type",
    "comparison_type": "comparison_type",
}

_EXTRACTION_TO_GOLD_STATUSES = (
    "present_in_gold",
    "outside_gold_surface",
    "contradicts_gold",
    "unadjudicated",
)
_GOLD_TO_EXTRACTION_STATUSES = (
    "represented_in_extraction",
    "missing_from_extraction",
)
_SOURCE_FAITHFULNESS_STATUSES = (
    "source_supported",
    "unsupported_or_bad_provenance",
)
_NON_SCORED_FIELD_TOKENS = {
    "_meta",
    "id",
    "row_id",
    "paper_id",
    "document_id",
    "documentid",
    "pmid",
    "provenance",
    "evidence",
    "note",
    "notes",
}


@dataclass(frozen=True)
class FieldValue:
    concept: str
    value_key: str
    field: str
    value: Any
    row_index: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.concept, self.value_key)


def _is_nullish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip().lower() in _NULLISH


def _normalize_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return f"{float(value):.10g}"
    text = str(value).strip().lower()
    text = text.replace("≥", ">=").replace("≤", "<=")
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    text = text.replace("×", "x")
    text = re.sub(r"\bgrade\s*>?=\s*(\d)", r"grade>=\1", text)
    text = re.sub(r"\bgrades?\s+(\d)\s*-\s*(\d)", r"grade \1-\2", text)
    text = re.sub(r"\s+", " ", text)
    try:
        decimal = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        decimal = None
    if decimal is not None and decimal.is_finite():
        return format(decimal.normalize(), "f")
    return text.strip()


def _field_values(
    row: dict[str, Any],
    *,
    row_index: int | None,
    field_map: dict[str, str],
) -> list[FieldValue]:
    values: list[FieldValue] = []
    for field, concept in field_map.items():
        value = row.get(field)
        if _is_nullish(value):
            continue
        if isinstance(value, (dict, list)):
            value_key = _normalize_value(json.dumps(value, sort_keys=True))
        else:
            value_key = _normalize_value(value)
        values.append(FieldValue(concept, value_key, field, value, row_index))
    return values


def _unique_by_key(values: list[FieldValue]) -> dict[tuple[str, str], FieldValue]:
    result: dict[tuple[str, str], FieldValue] = {}
    for value in values:
        result.setdefault(value.key, value)
    return result


def _concept_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _value_alias_lookup(value_aliases: dict[str, list[Any]] | None) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in (value_aliases or {}).items():
        canonical_key = _normalize_value(canonical)
        lookup[canonical_key] = canonical_key
        for alias in aliases or []:
            lookup[_normalize_value(alias)] = canonical_key
    return lookup


def _normalize_with_aliases(value: Any, value_aliases: dict[str, list[Any]] | None) -> str:
    value_key = _normalize_value(value)
    return _value_alias_lookup(value_aliases).get(value_key, value_key)


def _maps_with_aliases(
    field_aliases: dict[str, list[str]] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    gold_map = dict(_GOLD_FIELD_CONCEPTS)
    extraction_map = dict(_EXTRACTION_FIELD_CONCEPTS)
    for canonical, aliases in (field_aliases or {}).items():
        concept = (
            _GOLD_FIELD_CONCEPTS.get(canonical)
            or _EXTRACTION_FIELD_CONCEPTS.get(canonical)
            or _concept_token(canonical)
        )
        gold_map[canonical] = concept
        extraction_map[canonical] = concept
        for alias in aliases or []:
            extraction_map[alias] = concept
            gold_map[alias] = concept
    return gold_map, extraction_map


def _is_scored_field(
    field: str,
    concept: str,
    *,
    allowed_fields: set[str] | None,
    field_classes: dict[str, str] | None,
) -> bool:
    if allowed_fields is not None and field not in allowed_fields and concept not in allowed_fields:
        return False
    if _concept_token(field) in _NON_SCORED_FIELD_TOKENS:
        return False
    if field_classes:
        field_class = (
            field_classes.get(field)
            or field_classes.get(concept)
            or field_classes.get(_concept_token(field))
        )
        if field_class == "non_scored":
            return False
    return True


def _field_values_from_rows(
    rows: list[dict[str, Any]],
    *,
    field_map: dict[str, str],
    allowed_fields: set[str] | None,
    field_classes: dict[str, str] | None,
    value_aliases: dict[str, list[Any]] | None,
) -> list[FieldValue]:
    values: list[FieldValue] = []
    for row_index, row in enumerate(rows):
        for field, value in row.items():
            concept = field_map.get(field)
            if concept is None:
                if allowed_fields is not None:
                    continue
                concept = _concept_token(field)
            if not _is_scored_field(
                field,
                concept,
                allowed_fields=allowed_fields,
                field_classes=field_classes,
            ):
                continue
            if _is_nullish(value):
                continue
            if isinstance(value, (dict, list)):
                continue
            values.append(
                FieldValue(
                    concept=concept,
                    value_key=_normalize_with_aliases(value, value_aliases),
                    field=field,
                    value=value,
                    row_index=row_index,
                )
            )
    return values


def _source_supported(row: dict[str, Any], field: str) -> bool:
    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        return False
    entry = provenance.get(field)
    if not isinstance(entry, dict):
        return False
    sources = entry.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict) and any(
                source.get(name) for name in ("quote", "location", "page", "cell")
            ):
                return True
    if isinstance(sources, dict) and any(
        sources.get(name) for name in ("quote", "location", "page", "cell")
    ):
        return True
    return bool(entry.get("derivation_kind") or entry.get("evidence"))


def _key_from_adjudication(
    entry: dict[str, Any],
    *,
    side: str,
    rows: list[dict[str, Any]],
    field_map: dict[str, str],
    value_aliases: dict[str, list[Any]] | None,
) -> tuple[str, str] | None:
    direct_key = entry.get(f"{side}_key") or entry.get("field_value_key")
    if isinstance(direct_key, str) and "=" in direct_key:
        concept, value_key = direct_key.split("=", 1)
        return (_concept_token(concept), value_key)

    ref = entry.get(side) if isinstance(entry.get(side), dict) else entry
    row_index = ref.get("row_index")
    if row_index is None:
        row_index = ref.get(f"{side}_row_index")
    field = ref.get("field") or ref.get(f"{side}_field")
    value = ref.get("value")
    if field is None:
        return None
    concept = field_map.get(field, _concept_token(field))
    if value is None and row_index is not None:
        try:
            value = rows[int(row_index)].get(field)
        except (IndexError, TypeError, ValueError):
            value = None
    if value is None:
        return None
    return (concept, _normalize_with_aliases(value, value_aliases))


def _adjudication_index(
    entries: Any,
    *,
    side: str,
    rows: list[dict[str, Any]],
    field_map: dict[str, str],
    value_aliases: dict[str, list[Any]] | None,
) -> dict[tuple[str, str], dict[str, Any]]:
    if isinstance(entries, dict):
        entries = list(entries.values())
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        key = _key_from_adjudication(
            entry,
            side=side,
            rows=rows,
            field_map=field_map,
            value_aliases=value_aliases,
        )
        if key is not None:
            result[key] = entry
    return result


def _status_from_entry(entry: dict[str, Any], allowed: tuple[str, ...]) -> str:
    status = entry.get("status") or entry.get("label") or entry.get("verdict")
    if status in allowed:
        return str(status)
    if "gold_key" in entry and "present_in_gold" in allowed:
        return "present_in_gold"
    if "extraction_key" in entry and "represented_in_extraction" in allowed:
        return "represented_in_extraction"
    raise ValueError(f"Adjudication status must be one of {list(allowed)}, got {status!r}")


def _surface_counts(records: list[dict[str, Any]], statuses: tuple[str, ...]) -> dict[str, int]:
    return {status: sum(1 for record in records if record["status"] == status) for status in statuses}


def _record(value: FieldValue, *, status: str, adjudicated: bool, reasoning: Any = None) -> dict[str, Any]:
    body = {
        "status": status,
        "field_value_key": f"{value.concept}={value.value_key}",
        "concept": value.concept,
        "field": value.field,
        "value": value.value,
        "normalized_value": value.value_key,
        "row_index": value.row_index,
        "adjudicated": adjudicated,
    }
    if reasoning:
        body["reasoning"] = reasoning
    return body


def build_semantic_field_overlap(
    gold_rows: list[dict[str, Any]],
    extraction_rows: list[dict[str, Any]],
    *,
    paper_id: str = "paper",
    gold_fields: list[str] | None = None,
    extraction_fields: list[str] | None = None,
    field_aliases: dict[str, list[str]] | None = None,
    value_aliases: dict[str, list[Any]] | None = None,
    field_classes: dict[str, str] | None = None,
    adjudications: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the direct row-list semantic field-value overlap artifact.

    Explicit adjudications override deterministic normalized concept/value
    overlap. Without adjudication, extraction values that do not overlap gold
    remain `unadjudicated` rather than being assumed outside the benchmark.
    """

    gold_field_map, extraction_field_map = _maps_with_aliases(field_aliases)
    gold_values = _field_values_from_rows(
        gold_rows,
        field_map=gold_field_map,
        allowed_fields=set(gold_fields) if gold_fields is not None else None,
        field_classes=field_classes,
        value_aliases=value_aliases,
    )
    extraction_values = _field_values_from_rows(
        extraction_rows,
        field_map=extraction_field_map,
        allowed_fields=set(extraction_fields) if extraction_fields is not None else None,
        field_classes=field_classes,
        value_aliases=value_aliases,
    )
    gold_unique = _unique_by_key(gold_values)
    extraction_unique = _unique_by_key(extraction_values)

    adjudications = adjudications or {}
    x_to_g_adj = _adjudication_index(
        adjudications.get("extraction_to_gold"),
        side="extraction",
        rows=extraction_rows,
        field_map=extraction_field_map,
        value_aliases=value_aliases,
    )
    g_to_x_adj = _adjudication_index(
        adjudications.get("gold_to_extraction"),
        side="gold",
        rows=gold_rows,
        field_map=gold_field_map,
        value_aliases=value_aliases,
    )
    source_adj = _adjudication_index(
        adjudications.get("source_faithfulness"),
        side="extraction",
        rows=extraction_rows,
        field_map=extraction_field_map,
        value_aliases=value_aliases,
    )

    extraction_to_gold_records: list[dict[str, Any]] = []
    for key, value in sorted(extraction_unique.items()):
        if key in x_to_g_adj:
            entry = x_to_g_adj[key]
            status = _status_from_entry(entry, _EXTRACTION_TO_GOLD_STATUSES)
            extraction_to_gold_records.append(
                _record(value, status=status, adjudicated=True, reasoning=entry.get("reasoning"))
            )
        elif key in gold_unique:
            extraction_to_gold_records.append(
                _record(value, status="present_in_gold", adjudicated=False)
            )
        else:
            extraction_to_gold_records.append(
                _record(value, status="unadjudicated", adjudicated=False)
            )

    gold_to_extraction_records: list[dict[str, Any]] = []
    for key, value in sorted(gold_unique.items()):
        if key in g_to_x_adj:
            entry = g_to_x_adj[key]
            status = _status_from_entry(entry, _GOLD_TO_EXTRACTION_STATUSES)
            gold_to_extraction_records.append(
                _record(value, status=status, adjudicated=True, reasoning=entry.get("reasoning"))
            )
        elif key in extraction_unique:
            gold_to_extraction_records.append(
                _record(value, status="represented_in_extraction", adjudicated=False)
            )
        else:
            gold_to_extraction_records.append(
                _record(value, status="missing_from_extraction", adjudicated=False)
            )

    source_records: list[dict[str, Any]] = []
    for key, value in sorted(extraction_unique.items()):
        if key in source_adj:
            entry = source_adj[key]
            status = _status_from_entry(entry, _SOURCE_FAITHFULNESS_STATUSES)
            source_records.append(
                _record(value, status=status, adjudicated=True, reasoning=entry.get("reasoning"))
            )
            continue
        supported = any(
            other.key == key
            and other.row_index is not None
            and _source_supported(extraction_rows[other.row_index], other.field)
            for other in extraction_values
        )
        source_records.append(
            _record(
                value,
                status="source_supported" if supported else "unsupported_or_bad_provenance",
                adjudicated=False,
            )
        )

    return {
        "mode": "semantic_field_value_overlap",
        "papers": {
            paper_id: {
                "extraction_rows": len(extraction_rows),
                "gold_rows": len(gold_rows),
                "extraction_unique_scored_field_values": len(extraction_unique),
                "gold_unique_scored_field_values": len(gold_unique),
                "extraction_to_gold": _surface_counts(
                    extraction_to_gold_records, _EXTRACTION_TO_GOLD_STATUSES
                ),
                "gold_to_extraction": _surface_counts(
                    gold_to_extraction_records, _GOLD_TO_EXTRACTION_STATUSES
                ),
                "source_faithfulness": _surface_counts(
                    source_records, _SOURCE_FAITHFULNESS_STATUSES
                ),
                "details": {
                    "extraction_to_gold": extraction_to_gold_records,
                    "gold_to_extraction": gold_to_extraction_records,
                    "source_faithfulness": source_records,
                },
            }
        },
    }


def _extraction_rows(comparison: dict[str, Any]) -> list[dict[str, Any]]:
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
    return [rows[i] for i in sorted(rows)]


def _gold_values_by_row(comparison: dict[str, Any]) -> dict[int, list[FieldValue]]:
    by_row: dict[int, list[FieldValue]] = {}
    for pair in comparison.get("paired_rows", []) or []:
        idx = pair.get("gold_index")
        if not isinstance(idx, int):
            continue
        fields = pair.get("gold_fields") or {}
        if isinstance(fields, dict):
            by_row[idx] = _field_values(
                fields,
                row_index=idx,
                field_map=_GOLD_FIELD_CONCEPTS,
            )
    return by_row


def _finding_matches(finding_scores: dict[str, Any], paper_id: str) -> dict[int, dict[str, Any]]:
    paper = (finding_scores.get("papers") or {}).get(paper_id) or {}
    matches: dict[int, dict[str, Any]] = {}
    for finding in paper.get("findings", []) or []:
        gold_index = finding.get("gold_index")
        if isinstance(gold_index, int):
            matches[gold_index] = finding
    return matches


def _claim_faithfulness_by_paper(claim_faithfulness: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return claim_faithfulness.get("papers") or {}


def _source_faithfulness_for_paper(
    paper_id: str,
    claim_faithfulness: dict[str, Any],
) -> dict[str, Any]:
    paper = _claim_faithfulness_by_paper(claim_faithfulness).get(paper_id) or {}
    claims = paper.get("claims") or []
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        field = claim.get("field")
        value = claim.get("value")
        if not isinstance(field, str) or _is_nullish(value):
            continue
        key = (field, _normalize_value(value))
        unique.setdefault(key, claim)
    total = len(unique)
    supported = sum(1 for c in unique.values() if c.get("status") == "quote_found")
    return {
        "basis": "claim_faithfulness.unique_field_values",
        "unique_extracted_scored_field_values_with_required_provenance": total,
        "unique_extracted_scored_field_values_source_supported": supported,
        "unsupported_or_bad_provenance": total - supported,
        "source_faithfulness": round(supported / total, 6) if total else None,
    }


def build_paper_overlap(
    comparison: dict[str, Any],
    *,
    finding_scores: dict[str, Any] | None = None,
    claim_faithfulness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paper_id = comparison["paper_id"]
    finding_scores = finding_scores or {}
    claim_faithfulness = claim_faithfulness or {}

    gold_by_row = _gold_values_by_row(comparison)
    gold_unique = _unique_by_key([v for values in gold_by_row.values() for v in values])

    extraction_values = [
        value
        for row in _extraction_rows(comparison)
        for value in _field_values(
            row,
            row_index=row.get("extraction_index"),
            field_map=_EXTRACTION_FIELD_CONCEPTS,
        )
    ]
    extraction_unique = _unique_by_key(extraction_values)

    exact_overlap = set(gold_unique) & set(extraction_unique)

    findings = _finding_matches(finding_scores, paper_id)
    llm_represented_gold: set[tuple[str, str]] = set()
    llm_matched_extraction_rows: set[int] = set()
    llm_gold_concepts_by_extraction_row: dict[int, set[str]] = {}
    for gold_index, finding in findings.items():
        completeness = finding.get("completeness")
        accuracy = finding.get("accuracy")
        extraction_index = finding.get("extraction_index")
        if (
            isinstance(completeness, (int, float))
            and isinstance(accuracy, (int, float))
            and completeness > 0
            and accuracy > 0
        ):
            llm_represented_gold.update(v.key for v in gold_by_row.get(gold_index, []))
            if isinstance(extraction_index, int):
                llm_matched_extraction_rows.add(extraction_index)
                llm_gold_concepts_by_extraction_row.setdefault(
                    extraction_index, set()
                ).update(v.concept for v in gold_by_row.get(gold_index, []))

    # Extraction values from LLM-matched rows are semantically in-scope for gold,
    # but only exact concept/value overlap is counted as present_in_gold. The
    # remainder is flagged as row-adjudicated but not field-value aligned.
    llm_matched_exact_extraction_keys = {
        value.key
        for value in extraction_values
        if value.row_index in llm_matched_extraction_rows
    }
    llm_matched_concept_extraction_keys = {
        value.key
        for value in extraction_values
        if value.row_index in llm_gold_concepts_by_extraction_row
        and value.concept in llm_gold_concepts_by_extraction_row[value.row_index]
    }

    present_in_gold = (
        exact_overlap
        | (llm_matched_exact_extraction_keys & set(gold_unique))
        | llm_matched_concept_extraction_keys
    )
    represented_in_extraction = exact_overlap | (llm_represented_gold & set(gold_unique))

    extraction_outside = set(extraction_unique) - present_in_gold
    gold_missing = set(gold_unique) - represented_in_extraction

    source = _source_faithfulness_for_paper(paper_id, claim_faithfulness)

    extraction_total = len(extraction_unique)
    gold_total = len(gold_unique)
    return {
        "paper_id": paper_id,
        "gold_rows": comparison.get("gold_rows"),
        "extraction_rows": comparison.get("extraction_rows"),
        "basis": {
            "deterministic_semantic_key_overlap": True,
            "llm_row_adjudications_loaded": bool(findings),
            "claim_faithfulness_loaded": bool(source.get("basis")),
        },
        "extraction_unique_scored_field_values": extraction_total,
        "gold_unique_scored_field_values": gold_total,
        "extraction_to_gold": {
            "present_in_gold": len(present_in_gold),
            "outside_gold_surface": len(extraction_outside),
            "contradicts_gold": 0,
            "unadjudicated": 0,
            "overlap": round(len(present_in_gold) / extraction_total, 6)
            if extraction_total
            else None,
        },
        "gold_to_extraction": {
            "represented_in_extraction": len(represented_in_extraction),
            "missing_from_extraction": len(gold_missing),
            "coverage": round(len(represented_in_extraction) / gold_total, 6)
            if gold_total
            else None,
        },
        "source_faithfulness": source,
        "samples": {
            "extraction_outside_gold_surface": [
                {
                    "concept": extraction_unique[key].concept,
                    "field": extraction_unique[key].field,
                    "value": extraction_unique[key].value,
                }
                for key in sorted(extraction_outside)[:10]
            ],
            "gold_missing_from_extraction": [
                {
                    "concept": gold_unique[key].concept,
                    "field": gold_unique[key].field,
                    "value": gold_unique[key].value,
                }
                for key in sorted(gold_missing)[:10]
            ],
        },
    }


def build_overlap_report(
    *,
    comparisons_dir: Path,
    finding_scores_path: Path | None = None,
    claim_faithfulness_path: Path | None = None,
) -> dict[str, Any]:
    finding_scores = (
        json.loads(finding_scores_path.read_text())
        if finding_scores_path and finding_scores_path.exists()
        else {}
    )
    claim_faithfulness = (
        json.loads(claim_faithfulness_path.read_text())
        if claim_faithfulness_path and claim_faithfulness_path.exists()
        else {}
    )

    papers: dict[str, Any] = {}
    for path in sorted(comparisons_dir.glob("*.json")):
        comparison = json.loads(path.read_text())
        if "error" in comparison:
            papers[path.stem] = {"error": comparison["error"]}
            continue
        papers[comparison["paper_id"]] = build_paper_overlap(
            comparison,
            finding_scores=finding_scores,
            claim_faithfulness=claim_faithfulness,
        )

    totals = {
        "extraction_unique_scored_field_values": 0,
        "extraction_present_in_gold": 0,
        "gold_unique_scored_field_values": 0,
        "gold_represented_in_extraction": 0,
        "source_supported": 0,
        "source_total": 0,
    }
    for body in papers.values():
        if "error" in body:
            continue
        totals["extraction_unique_scored_field_values"] += body[
            "extraction_unique_scored_field_values"
        ]
        totals["extraction_present_in_gold"] += body["extraction_to_gold"][
            "present_in_gold"
        ]
        totals["gold_unique_scored_field_values"] += body[
            "gold_unique_scored_field_values"
        ]
        totals["gold_represented_in_extraction"] += body["gold_to_extraction"][
            "represented_in_extraction"
        ]
        src = body["source_faithfulness"]
        totals["source_supported"] += src.get(
            "unique_extracted_scored_field_values_source_supported", 0
        )
        totals["source_total"] += src.get(
            "unique_extracted_scored_field_values_with_required_provenance", 0
        )

    aggregate = {
        **totals,
        "extraction_to_gold_overlap": round(
            totals["extraction_present_in_gold"]
            / totals["extraction_unique_scored_field_values"],
            6,
        )
        if totals["extraction_unique_scored_field_values"]
        else None,
        "gold_to_extraction_coverage": round(
            totals["gold_represented_in_extraction"]
            / totals["gold_unique_scored_field_values"],
            6,
        )
        if totals["gold_unique_scored_field_values"]
        else None,
        "source_faithfulness": round(
            totals["source_supported"] / totals["source_total"], 6
        )
        if totals["source_total"]
        else None,
    }

    return {
        "mode": "semantic_field_value_overlap",
        "definition": (
            "Unique normalized scored field-value concepts compared in both "
            "directions, with source faithfulness measured over quote-traced "
            "high-leverage extracted claims."
        ),
        "aggregate": aggregate,
        "papers": papers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparisons-dir", required=True)
    parser.add_argument("--finding-scores")
    parser.add_argument("--claim-faithfulness")
    parser.add_argument("--output")
    args = parser.parse_args()

    report = build_overlap_report(
        comparisons_dir=Path(args.comparisons_dir),
        finding_scores_path=Path(args.finding_scores) if args.finding_scores else None,
        claim_faithfulness_path=Path(args.claim_faithfulness)
        if args.claim_faithfulness
        else None,
    )
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
