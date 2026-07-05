"""General eval workbench helpers for large comparison packets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_DROP_FIELDS = {
    "provenance",
    "source_components",
    "source_component",
    "trace",
    "raw_text",
    "quote",
    "quotes",
}


def _compact_value(value: Any, max_chars: int) -> Any:
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + "...[truncated]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_compact_value(v, max_chars) for v in value[:20]]
    if isinstance(value, dict):
        return {
            str(k): _compact_value(v, max_chars)
            for k, v in list(value.items())[:40]
            if k not in DEFAULT_DROP_FIELDS
        }
    return str(value)[:max_chars]


def compact_row(row: dict[str, Any], *, max_field_chars: int = 240) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in row.items():
        if key in DEFAULT_DROP_FIELDS:
            continue
        if value is None or value == "":
            continue
        compact[key] = _compact_value(value, max_field_chars)
    return compact


def distill_comparison(
    comparison: dict[str, Any],
    *,
    max_candidates_per_gold: int = 12,
    max_unmatched_extraction_rows: int = 80,
    max_field_chars: int = 240,
) -> dict[str, Any]:
    paired_rows: list[dict[str, Any]] = []
    candidate_overflow = 0
    for pair in comparison.get("paired_rows", []) or []:
        candidates = pair.get("extraction_candidates") or []
        if len(candidates) > max_candidates_per_gold:
            candidate_overflow += len(candidates) - max_candidates_per_gold
        paired_rows.append({
            "gold_index": pair.get("gold_index"),
            "gold_entity": pair.get("gold_entity"),
            "gold_resolved_token": pair.get("gold_resolved_token"),
            "resolution_rung": pair.get("resolution_rung"),
            "gold_category_hint": pair.get("gold_category_hint"),
            "gold_fields": compact_row(pair.get("gold_fields") or {}, max_field_chars=max_field_chars),
            "candidate_count": len(candidates),
            "extraction_candidates": [
                compact_row(candidate, max_field_chars=max_field_chars)
                for candidate in candidates[:max_candidates_per_gold]
            ],
            "has_match": pair.get("has_match"),
        })

    unmatched_rows = comparison.get("unmatched_extraction_rows") or []
    return {
        "mode": "comparison_workbench_distilled",
        "paper_id": comparison.get("paper_id"),
        "document_id": comparison.get("document_id"),
        "benchmark_status": comparison.get("benchmark_status", "gold_scored"),
        "gold_scored": comparison.get("gold_scored", True),
        "precision_only": comparison.get("precision_only", False),
        "reason": comparison.get("reason"),
        "gold_row_count": comparison.get("gold_rows", 0),
        "extraction_row_count": comparison.get("extraction_rows", 0),
        "gold_rows": comparison.get("gold_rows", 0),
        "extraction_rows": comparison.get("extraction_rows", 0),
        "paired_count": comparison.get("paired_count", 0),
        "unmatched_gold_count": comparison.get("unmatched_gold_count", 0),
        "unmatched_extraction_count": comparison.get("unmatched_extraction_count", 0),
        "entity_resolution": comparison.get("entity_resolution"),
        "normalization_status": comparison.get("normalization_status"),
        "paired_rows": paired_rows,
        "unmatched_extraction_rows": [
            compact_row(row, max_field_chars=max_field_chars)
            for row in unmatched_rows[:max_unmatched_extraction_rows]
        ],
        "truncation": {
            "max_candidates_per_gold": max_candidates_per_gold,
            "candidate_rows_omitted": candidate_overflow,
            "max_unmatched_extraction_rows": max_unmatched_extraction_rows,
            "unmatched_extraction_rows_omitted": max(
                0, len(unmatched_rows) - max_unmatched_extraction_rows
            ),
            "dropped_heavy_fields": sorted(DEFAULT_DROP_FIELDS),
            "note": (
                "This is a working view only. Final adjudications must cite "
                "gold_index and extraction_index from the full comparison packet."
            ),
        },
    }


def _row_without_candidate_payload(pair: dict[str, Any], *, max_field_chars: int) -> dict[str, Any]:
    candidates = pair.get("extraction_candidates") or []
    return {
        "gold_index": pair.get("gold_index"),
        "gold_entity": pair.get("gold_entity"),
        "gold_resolved_token": pair.get("gold_resolved_token"),
        "resolution_rung": pair.get("resolution_rung"),
        "gold_category_hint": pair.get("gold_category_hint"),
        "candidate_count": len(candidates),
        "candidate_indices": [
            candidate.get("extraction_index")
            for candidate in candidates
            if candidate.get("extraction_index") is not None
        ],
        "gold_fields": compact_row(pair.get("gold_fields") or {}, max_field_chars=max_field_chars),
        "has_match": pair.get("has_match"),
    }


def _iter_candidate_links(
    comparison: dict[str, Any],
    *,
    max_field_chars: int,
) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for pair in comparison.get("paired_rows", []) or []:
        for rank, candidate in enumerate(pair.get("extraction_candidates") or []):
            links.append(
                {
                    "gold_index": pair.get("gold_index"),
                    "candidate_rank": rank,
                    "extraction_index": candidate.get("extraction_index"),
                    "resolution_rung": pair.get("resolution_rung"),
                    "gold_entity": pair.get("gold_entity"),
                    "candidate": compact_row(candidate, max_field_chars=max_field_chars),
                }
            )
    return links


def _unique_extraction_rows(comparison: dict[str, Any], *, max_field_chars: int) -> list[dict[str, Any]]:
    by_index: dict[Any, dict[str, Any]] = {}
    for pair in comparison.get("paired_rows", []) or []:
        for candidate in pair.get("extraction_candidates") or []:
            index = candidate.get("extraction_index")
            if index is not None and index not in by_index:
                by_index[index] = compact_row(candidate, max_field_chars=max_field_chars)
    for row in comparison.get("unmatched_extraction_rows") or []:
        index = row.get("extraction_index")
        if index is not None and index not in by_index:
            by_index[index] = compact_row(row, max_field_chars=max_field_chars)

    def sort_key(row: dict[str, Any]) -> tuple[int, Any]:
        index = row.get("extraction_index")
        return (0, index) if isinstance(index, int) else (1, str(index))

    return sorted(by_index.values(), key=sort_key)


def _jsonl_bytes(rows: list[dict[str, Any]]) -> list[bytes]:
    return [
        (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for row in rows
    ]


def _write_jsonl_chunked(
    output_dir: Path,
    stem: str,
    rows: list[dict[str, Any]],
    *,
    max_bytes: int,
) -> dict[str, Any]:
    encoded = _jsonl_bytes(rows)
    chunks: list[list[bytes]] = [[]]
    chunk_sizes = [0]
    for line in encoded:
        if chunks[-1] and chunk_sizes[-1] + len(line) > max_bytes:
            chunks.append([])
            chunk_sizes.append(0)
        chunks[-1].append(line)
        chunk_sizes[-1] += len(line)

    files: list[dict[str, Any]] = []
    if len(chunks) == 1:
        path = output_dir / f"{stem}.jsonl"
        path.write_bytes(b"".join(chunks[0]))
        files.append({"path": path.name, "rows": len(rows), "bytes": path.stat().st_size})
    else:
        start = 0
        for idx, chunk in enumerate(chunks, start=1):
            path = output_dir / f"{stem}.part{idx:03d}.jsonl"
            path.write_bytes(b"".join(chunk))
            end = start + len(chunk)
            files.append(
                {
                    "path": path.name,
                    "rows": len(chunk),
                    "row_start": start,
                    "row_end_exclusive": end,
                    "bytes": path.stat().st_size,
                }
            )
            start = end

    manifest = {
        "stem": stem,
        "row_count": len(rows),
        "max_bytes": max_bytes,
        "files": files,
    }
    (output_dir / f"{stem}.manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    return manifest


def build_workbench_bundle(
    comparison: dict[str, Any],
    *,
    max_field_chars: int = 160,
    max_jsonl_bytes: int = 220_000,
) -> dict[str, Any]:
    paired_rows = [
        _row_without_candidate_payload(pair, max_field_chars=max_field_chars)
        for pair in comparison.get("paired_rows", []) or []
    ]
    extraction_rows = _unique_extraction_rows(comparison, max_field_chars=max_field_chars)
    unmatched_rows = [
        compact_row(row, max_field_chars=max_field_chars)
        for row in comparison.get("unmatched_extraction_rows") or []
    ]
    candidate_links = _iter_candidate_links(comparison, max_field_chars=max_field_chars)

    return {
        "summary": {
            "mode": "comparison_workbench_bundle",
            "paper_id": comparison.get("paper_id"),
            "document_id": comparison.get("document_id"),
            "benchmark_status": comparison.get("benchmark_status", "gold_scored"),
            "gold_scored": comparison.get("gold_scored", True),
            "precision_only": comparison.get("precision_only", False),
            "reason": comparison.get("reason"),
            "gold_row_count": comparison.get("gold_rows", 0),
            "extraction_row_count": comparison.get("extraction_rows", 0),
            "paired_count": comparison.get("paired_count", 0),
            "paired_row_index_count": len(paired_rows),
            "unique_extraction_row_index_count": len(extraction_rows),
            "candidate_link_count": len(candidate_links),
            "unmatched_gold_count": comparison.get("unmatched_gold_count", 0),
            "unmatched_extraction_count": comparison.get("unmatched_extraction_count", 0),
            "entity_resolution": comparison.get("entity_resolution"),
            "normalization_status": comparison.get("normalization_status"),
            "max_field_chars": max_field_chars,
            "max_jsonl_bytes": max_jsonl_bytes,
            "dropped_heavy_fields": sorted(DEFAULT_DROP_FIELDS),
            "usage_note": (
                "Use summary.json first, then the JSONL indexes. "
                "Use candidate_links only when a gold row has ambiguous candidates. "
                "Final adjudications must cite gold_index and extraction_index."
            ),
        },
        "paired_gold_rows": paired_rows,
        "extraction_row_index": extraction_rows,
        "unmatched_extraction_rows": unmatched_rows,
        "candidate_links": candidate_links,
    }


def write_workbench_bundle(
    comparison: dict[str, Any],
    output_dir: Path,
    *,
    max_field_chars: int = 160,
    max_jsonl_bytes: int = 220_000,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle = build_workbench_bundle(
        comparison,
        max_field_chars=max_field_chars,
        max_jsonl_bytes=max_jsonl_bytes,
    )
    manifests = {
        name: _write_jsonl_chunked(output_dir, name, rows, max_bytes=max_jsonl_bytes)
        for name, rows in bundle.items()
        if name != "summary"
    }
    summary = dict(bundle["summary"])
    summary["manifests"] = manifests
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    (output_dir / "README.md").write_text(
        "\n".join(
            [
                "# Eval Workbench Bundle",
                "",
                "Read `summary.json` first. The JSONL files are compact, domain-neutral indexes for eval adjudication.",
                "",
                "- `paired_gold_rows*.jsonl`: one gold row per line, with gold fields and candidate indices.",
                "- `extraction_row_index*.jsonl`: unique extraction rows, deduplicated by `extraction_index`.",
                "- `candidate_links*.jsonl`: gold-to-candidate links for ambiguous or resolver-limited cases.",
                "- `unmatched_extraction_rows*.jsonl`: extraction rows not paired by the deterministic resolver.",
                "",
                "Do not treat this as a substitute for the full comparison packet when provenance/source text is needed.",
                "",
            ]
        )
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison")
    parser.add_argument("--output", required=True)
    parser.add_argument("--bundle-dir")
    parser.add_argument("--max-candidates-per-gold", type=int, default=12)
    parser.add_argument("--max-unmatched-extraction-rows", type=int, default=80)
    parser.add_argument("--max-field-chars", type=int, default=240)
    parser.add_argument("--bundle-max-field-chars", type=int, default=160)
    parser.add_argument("--bundle-max-jsonl-bytes", type=int, default=220_000)
    args = parser.parse_args()

    comparison = json.loads(Path(args.comparison).read_text())
    distilled = distill_comparison(
        comparison,
        max_candidates_per_gold=args.max_candidates_per_gold,
        max_unmatched_extraction_rows=args.max_unmatched_extraction_rows,
        max_field_chars=args.max_field_chars,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(distilled, indent=2, ensure_ascii=False) + "\n")
    bundle_summary = None
    if args.bundle_dir:
        bundle_summary = write_workbench_bundle(
            comparison,
            Path(args.bundle_dir),
            max_field_chars=args.bundle_max_field_chars,
            max_jsonl_bytes=args.bundle_max_jsonl_bytes,
        )
    print(
        json.dumps(
            {
                "paper_id": distilled["paper_id"],
                "gold_row_count": distilled["gold_row_count"],
                "extraction_row_count": distilled["extraction_row_count"],
                "output": str(output),
                "bundle_dir": args.bundle_dir,
                "bundle_summary": bundle_summary,
                "truncation": distilled["truncation"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
