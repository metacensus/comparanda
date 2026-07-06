"""Domain-agnostic eval batch planning and artifact merging.

This module formalizes the useful part of the eval agent's helper-script
methodology: compact planning and deterministic merging. It does not encode
paper-specific or domain-specific matching logic.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _comparison_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _context_proxy(gold_rows: int, extraction_rows: int, size_bytes: int) -> int:
    # Conservative, transparent proxy used only for batch splitting.
    return int(size_bytes / 4) + gold_rows * 250 + extraction_rows * 80


def plan_batches(
    *,
    comparisons_dir: Path,
    max_gold_rows: int = 250,
    max_comparison_bytes: int = 5_000_000,
    max_context_proxy: int = 250_000,
) -> dict[str, Any]:
    papers: list[dict[str, Any]] = []
    for path in sorted(Path(comparisons_dir).glob("*.json")):
        body = json.loads(path.read_text())
        gold_rows = int(body.get("gold_rows") or 0)
        extraction_rows = int(body.get("extraction_rows") or 0)
        size = _comparison_size(path)
        papers.append({
            "paper_id": body.get("paper_id") or path.stem,
            "comparison_path": str(path),
            "benchmark_status": body.get("benchmark_status", "gold_scored"),
            "gold_rows": gold_rows,
            "extraction_rows": extraction_rows,
            "comparison_bytes": size,
            "context_proxy": _context_proxy(gold_rows, extraction_rows, size),
        })

    batches: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_gold = 0
    current_bytes = 0
    current_proxy = 0

    def flush() -> None:
        nonlocal current, current_gold, current_bytes, current_proxy
        if not current:
            return
        batches.append({
            "batch_id": f"batch_{len(batches) + 1:03d}",
            "paper_ids": [p["paper_id"] for p in current],
            "gold_rows": current_gold,
            "comparison_bytes": current_bytes,
            "context_proxy": current_proxy,
            "requires_split": any(
                p["gold_rows"] > max_gold_rows
                or p["comparison_bytes"] > max_comparison_bytes
                or p["context_proxy"] > max_context_proxy
                for p in current
            ),
        })
        current = []
        current_gold = 0
        current_bytes = 0
        current_proxy = 0

    for paper in sorted(papers, key=lambda p: (p["gold_rows"], p["comparison_bytes"]), reverse=True):
        paper_exceeds = (
            paper["gold_rows"] > max_gold_rows
            or paper["comparison_bytes"] > max_comparison_bytes
            or paper["context_proxy"] > max_context_proxy
        )
        would_exceed = (
            current_gold + paper["gold_rows"] > max_gold_rows
            or current_bytes + paper["comparison_bytes"] > max_comparison_bytes
            or current_proxy + paper["context_proxy"] > max_context_proxy
        )
        if current and (would_exceed or paper_exceeds):
            flush()
        current.append(paper)
        current_gold += paper["gold_rows"]
        current_bytes += paper["comparison_bytes"]
        current_proxy += paper["context_proxy"]
        if paper_exceeds:
            flush()
    flush()

    return {
        "mode": "eval_batch_plan",
        "thresholds": {
            "max_gold_rows": max_gold_rows,
            "max_comparison_bytes": max_comparison_bytes,
            "max_context_proxy": max_context_proxy,
        },
        "paper_count": len(papers),
        "batch_count": len(batches),
        "papers": papers,
        "batches": batches,
    }


def _merge_papers(files: list[Path], key: str) -> dict[str, Any]:
    merged: dict[str, Any] = {"papers": {}}
    for path in files:
        body = json.loads(path.read_text())
        papers = body.get("papers")
        if not isinstance(papers, dict):
            raise ValueError(f"{path}: missing papers object")
        overlap = set(merged["papers"]) & set(papers)
        if overlap:
            raise ValueError(f"{path}: duplicate papers in {key}: {sorted(overlap)}")
        merged["papers"].update(papers)
    return merged


def merge_batch_artifacts(
    *,
    batch_dirs: list[Path],
    output_dir: Path,
) -> dict[str, Any]:
    finding_files: list[Path] = []
    overlap_files: list[Path] = []
    claim_files: list[Path] = []
    for directory in batch_dirs:
        finding_files.extend(sorted(directory.glob("finding_scores*.json")))
        overlap_files.extend(sorted(directory.glob("semantic_field_value_overlap*.json")))
        claim_files.extend(sorted(directory.glob("claim_faithfulness*.json")))

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "mode": "eval_batch_merge",
        "batch_dirs": [str(p) for p in batch_dirs],
        "outputs": {},
    }
    if finding_files:
        merged = _merge_papers(finding_files, "finding_scores")
        path = output_dir / "finding_scores.json"
        path.write_text(json.dumps(merged, indent=2) + "\n")
        manifest["outputs"]["finding_scores"] = str(path)
    if claim_files:
        merged = _merge_papers(claim_files, "claim_faithfulness")
        path = output_dir / "claim_faithfulness.json"
        path.write_text(json.dumps(merged, indent=2) + "\n")
        manifest["outputs"]["claim_faithfulness"] = str(path)
    if overlap_files:
        papers: dict[str, Any] = {}
        for path_in in overlap_files:
            body = json.loads(path_in.read_text())
            for paper_id, paper in (body.get("papers") or {}).items():
                if paper_id in papers:
                    raise ValueError(f"{path_in}: duplicate overlap paper {paper_id}")
                papers[paper_id] = paper
        path = output_dir / "semantic_field_value_overlap.papers.json"
        path.write_text(json.dumps({"papers": papers}, indent=2) + "\n")
        manifest["outputs"]["semantic_field_value_overlap_papers"] = str(path)

    manifest_path = output_dir / "batch_merge_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--comparisons-dir", required=True)
    plan.add_argument("--max-gold-rows", type=int, default=250)
    plan.add_argument("--max-comparison-bytes", type=int, default=5_000_000)
    plan.add_argument("--max-context-proxy", type=int, default=250_000)
    plan.add_argument("--output")

    merge = sub.add_parser("merge")
    merge.add_argument("--batch-dir", action="append", required=True)
    merge.add_argument("--output-dir", required=True)

    args = parser.parse_args()
    if args.command == "plan":
        report = plan_batches(
            comparisons_dir=Path(args.comparisons_dir),
            max_gold_rows=args.max_gold_rows,
            max_comparison_bytes=args.max_comparison_bytes,
            max_context_proxy=args.max_context_proxy,
        )
        payload = json.dumps(report, indent=2)
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload + "\n")
        else:
            print(payload)
        return 0

    manifest = merge_batch_artifacts(
        batch_dirs=[Path(p) for p in args.batch_dir],
        output_dir=Path(args.output_dir),
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
