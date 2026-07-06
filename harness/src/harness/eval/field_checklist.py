"""Generate field-level gold checklists for eval feasibility runs."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any


NULLISH = {"", "-", "—", "na", "n/a", "none", "not stated", "not_stated", "null"}


def is_nullish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, (dict, list)):
        return not value
    return str(value).strip().lower() in NULLISH


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "field"


def _role_hint(field: str, value: Any) -> str:
    """Return a weak, domain-general role hint for eval-agent review."""
    token = _slug(field)
    if any(part in token.split("_") for part in ("id", "type", "category", "group", "class")):
        return "identity_context"
    if token.endswith("_id") or token.endswith("_ids"):
        return "identity_context"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "value"
    if any(part in token for part in ("method", "assay", "instrument", "scale")):
        return "method_context"
    if any(part in token for part in ("setting", "window", "time", "scope")):
        return "scope_context"
    if any(part in token for part in ("note", "comment", "source", "paper", "document")):
        return "metadata"
    return "unknown"


def build_checklist_for_comparison(comparison: dict[str, Any]) -> list[dict[str, Any]]:
    paper_id = comparison.get("paper_id") or "paper"
    items: list[dict[str, Any]] = []
    for pair in comparison.get("paired_rows", []) or []:
        gold_index = pair.get("gold_index")
        if not isinstance(gold_index, int):
            continue
        candidates = pair.get("extraction_candidates") or []
        candidate_indices = [
            candidate.get("extraction_index")
            for candidate in candidates
            if isinstance(candidate.get("extraction_index"), int)
        ]
        gold_fields = pair.get("gold_fields") or {}
        field_position = 0
        for field, value in gold_fields.items():
            if is_nullish(value):
                continue
            item = {
                "checklist_id": f"{paper_id}:{gold_index}:{field_position}:{_slug(str(field))}",
                "paper_id": paper_id,
                "gold_index": gold_index,
                "gold_field_index": field_position,
                "gold_field": field,
                "gold_value": value,
                "role_hint": _role_hint(str(field), value),
                "candidate_extraction_indices": candidate_indices,
                "candidate_count": len(candidate_indices),
                "resolution_rung": pair.get("resolution_rung"),
                "gold_entity": pair.get("gold_entity"),
                "gold_resolved_token": pair.get("gold_resolved_token"),
                "has_match": pair.get("has_match"),
            }
            items.append(item)
            field_position += 1
    return items


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    )


def load_comparison(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--comparison")
    source.add_argument("--comparisons-dir")
    parser.add_argument("--output")
    parser.add_argument("--output-dir")
    args = parser.parse_args()

    if args.comparison:
        comparison_path = Path(args.comparison)
        rows = build_checklist_for_comparison(load_comparison(comparison_path))
        output = Path(args.output) if args.output else Path(f"gold_field_checklist.{comparison_path.stem}.jsonl")
        write_jsonl(output, rows)
        print(json.dumps({"output": str(output), "checklist_items": len(rows)}, indent=2))
        return 0

    output_dir = Path(args.output_dir or args.output or "field_checklists")
    reports = []
    for comparison_path in sorted(Path(args.comparisons_dir).glob("*.json")):
        comparison = load_comparison(comparison_path)
        rows = build_checklist_for_comparison(comparison)
        paper_id = comparison.get("paper_id") or comparison_path.stem
        output = output_dir / f"gold_field_checklist.{paper_id}.jsonl"
        write_jsonl(output, rows)
        reports.append({"paper_id": paper_id, "output": str(output), "checklist_items": len(rows)})
    print(json.dumps({"outputs": reports}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
