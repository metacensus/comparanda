"""Aggregate evaluation — fast row-level metrics comparing extraction vs gold standard.

This is the primary eval tool. Run after every comparanda iteration.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from harness.eval.gold_standard import (
    gold_row_count,
    gold_variants_for_paper,
    load_gold_standard,
    load_pmid_map,
)


@dataclass
class PaperEval:
    """Evaluation results for a single paper."""

    paper_id: str
    pmid: int
    gold_rows: int
    extracted_rows: int
    row_coverage: float  # extracted / gold
    gold_variants: list[str]
    extracted_variants: list[str]
    matched_variants: list[str]
    missing_variants: list[str]
    extra_variants: list[str]
    variant_coverage: float  # matched / gold
    field_fill_rates: dict[str, float] = field(default_factory=dict)
    identity_dim_completeness: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


@dataclass
class AggregateEval:
    """Evaluation results across all papers."""

    papers: list[PaperEval]
    mean_row_coverage: float
    mean_variant_coverage: float
    total_gold_rows: int
    total_extracted_rows: int
    overall_row_coverage: float
    papers_below_50pct: list[str]  # paper_ids with < 50% row coverage
    papers_below_70pct: list[str]  # paper_ids with < 70% row coverage
    composite_score: float


def _load_extraction(extraction_path: Path) -> list[dict]:
    """Load extraction JSONL, filtering out meta-commentary."""
    rows = []
    with open(extraction_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if not obj.get("_meta_commentary"):
                    rows.append(obj)
            except json.JSONDecodeError:
                continue
    return rows


def _extract_variants(rows: list[dict]) -> set[str]:
    """Extract unique variant names from extraction rows."""
    variants = set()
    for row in rows:
        v = row.get("variant_raw") or row.get("variant")
        if v:
            variants.add(str(v))
    return variants


def _compute_fill_rates(rows: list[dict]) -> dict[str, float]:
    """Compute non-null fill rate per field."""
    if not rows:
        return {}
    all_fields = set()
    for row in rows:
        all_fields.update(row.keys())

    fill_rates = {}
    for field_name in sorted(all_fields):
        non_null = sum(1 for r in rows if r.get(field_name) is not None)
        fill_rates[field_name] = non_null / len(rows)
    return fill_rates


def _compute_id_dim_completeness(
    rows: list[dict],
    identity_dimensions: list[str],
) -> dict[str, float]:
    """Compute completeness of identity dimension fields."""
    if not rows:
        return {}
    result = {}
    for dim in identity_dimensions:
        non_null = sum(1 for r in rows if r.get(dim) is not None)
        result[dim] = non_null / len(rows)
    return result


def _normalize_variant_name(name: str) -> str:
    """Rough normalization for variant matching.

    Strips whitespace, lowercases, removes common prefixes.
    This is approximate — for real matching, use the normalizer.
    """
    name = name.strip().lower()
    # Remove genotype suffixes for matching (gold has "rs3918290 GA", extraction has "rs3918290")
    # Split on space, take first token if it looks like an rs number
    parts = name.split()
    if parts and parts[0].startswith("rs"):
        return parts[0]
    return name


def evaluate_paper(
    paper_id: str,
    extraction_path: Path,
    gold_df: pd.DataFrame,
    pmid: int,
    identity_dimensions: list[str] | None = None,
) -> PaperEval:
    """Evaluate a single paper's extraction against gold standard."""
    rows = _load_extraction(extraction_path)
    gold_n = gold_row_count(gold_df, pmid)
    gold_vars = gold_variants_for_paper(gold_df, pmid)
    ext_vars = _extract_variants(rows)

    # Normalize for matching
    gold_vars_norm = {_normalize_variant_name(v): v for v in gold_vars}
    ext_vars_norm = {_normalize_variant_name(v): v for v in ext_vars}

    matched_keys = set(gold_vars_norm.keys()) & set(ext_vars_norm.keys())
    missing_keys = set(gold_vars_norm.keys()) - set(ext_vars_norm.keys())
    extra_keys = set(ext_vars_norm.keys()) - set(gold_vars_norm.keys())

    row_coverage = len(rows) / gold_n if gold_n > 0 else 0.0
    variant_coverage = len(matched_keys) / len(gold_vars_norm) if gold_vars_norm else 0.0

    fill_rates = _compute_fill_rates(rows)
    id_completeness = (
        _compute_id_dim_completeness(rows, identity_dimensions)
        if identity_dimensions
        else {}
    )

    flags = []
    if row_coverage < 0.5:
        flags.append(f"CRITICAL: row coverage {row_coverage:.0%} < 50%")
    elif row_coverage < 0.7:
        flags.append(f"WARNING: row coverage {row_coverage:.0%} < 70%")
    if row_coverage > 1.5:
        flags.append(f"WARNING: over-extraction {row_coverage:.0%} > 150%")
    if missing_keys:
        flags.append(f"Missing {len(missing_keys)} gold variants: {sorted(missing_keys)[:5]}")

    return PaperEval(
        paper_id=paper_id,
        pmid=pmid,
        gold_rows=gold_n,
        extracted_rows=len(rows),
        row_coverage=round(row_coverage, 3),
        gold_variants=sorted(gold_vars),
        extracted_variants=sorted(ext_vars),
        matched_variants=sorted(gold_vars_norm[k] for k in matched_keys),
        missing_variants=sorted(gold_vars_norm[k] for k in missing_keys),
        extra_variants=sorted(ext_vars_norm[k] for k in extra_keys),
        variant_coverage=round(variant_coverage, 3),
        field_fill_rates=fill_rates,
        identity_dim_completeness=id_completeness,
        flags=flags,
    )


def evaluate_all(
    extractions_dir: Path,
    gold_standard_path: Path,
    pmid_map_path: Path,
    identity_dimensions: list[str] | None = None,
) -> AggregateEval:
    """Evaluate all extracted papers against gold standard."""
    gold_df = load_gold_standard(gold_standard_path)
    pmid_map = load_pmid_map(pmid_map_path)

    papers = []
    for extraction_file in sorted(extractions_dir.glob("*.jsonl")):
        paper_id = extraction_file.stem
        pmid = pmid_map.get(paper_id)
        if pmid is None:
            # Try matching by stem patterns
            for map_key, map_pmid in pmid_map.items():
                if paper_id.lower().startswith(map_key.lower().split()[0].lower()):
                    pmid = map_pmid
                    break
        if pmid is None:
            continue

        # Check if this paper has gold standard data
        if gold_row_count(gold_df, pmid) == 0:
            continue

        paper_eval = evaluate_paper(
            paper_id=paper_id,
            extraction_path=extraction_file,
            gold_df=gold_df,
            pmid=pmid,
            identity_dimensions=identity_dimensions,
        )
        papers.append(paper_eval)

    if not papers:
        return AggregateEval(
            papers=[],
            mean_row_coverage=0.0,
            mean_variant_coverage=0.0,
            total_gold_rows=0,
            total_extracted_rows=0,
            overall_row_coverage=0.0,
            papers_below_50pct=[],
            papers_below_70pct=[],
            composite_score=0.0,
        )

    total_gold = sum(p.gold_rows for p in papers)
    total_ext = sum(p.extracted_rows for p in papers)
    mean_row_cov = sum(p.row_coverage for p in papers) / len(papers)
    mean_var_cov = sum(p.variant_coverage for p in papers) / len(papers)
    mean_fill = sum(
        sum(p.field_fill_rates.values()) / len(p.field_fill_rates)
        for p in papers
        if p.field_fill_rates
    ) / len(papers) if papers else 0.0

    # Composite: 40% row coverage, 30% accuracy (placeholder 1.0), 20% variant, 10% fill
    composite = 0.4 * mean_row_cov + 0.3 * 1.0 + 0.2 * mean_var_cov + 0.1 * mean_fill

    return AggregateEval(
        papers=papers,
        mean_row_coverage=round(mean_row_cov, 3),
        mean_variant_coverage=round(mean_var_cov, 3),
        total_gold_rows=total_gold,
        total_extracted_rows=total_ext,
        overall_row_coverage=round(total_ext / total_gold, 3) if total_gold else 0.0,
        papers_below_50pct=[p.paper_id for p in papers if p.row_coverage < 0.5],
        papers_below_70pct=[p.paper_id for p in papers if p.row_coverage < 0.7],
        composite_score=round(composite, 3),
    )


def format_report(eval_result: AggregateEval) -> str:
    """Format eval results as a human-readable report."""
    lines = ["# Aggregate Evaluation Report", ""]
    lines.append(f"Papers evaluated: {len(eval_result.papers)}")
    lines.append(f"Total rows: {eval_result.total_extracted_rows} extracted / {eval_result.total_gold_rows} gold ({eval_result.overall_row_coverage:.0%})")
    lines.append(f"Mean row coverage: {eval_result.mean_row_coverage:.0%}")
    lines.append(f"Mean variant coverage: {eval_result.mean_variant_coverage:.0%}")
    lines.append(f"Composite score: {eval_result.composite_score:.3f}")
    lines.append("")

    if eval_result.papers_below_50pct:
        lines.append(f"⚠️  CRITICAL — papers below 50% row coverage: {eval_result.papers_below_50pct}")
    if eval_result.papers_below_70pct:
        below_70_not_50 = [p for p in eval_result.papers_below_70pct if p not in eval_result.papers_below_50pct]
        if below_70_not_50:
            lines.append(f"⚠️  WARNING — papers below 70% row coverage: {below_70_not_50}")
    lines.append("")

    lines.append("## Per-Paper Results")
    lines.append("")
    for p in eval_result.papers:
        flag_str = f" {'  '.join(p.flags)}" if p.flags else ""
        lines.append(f"### {p.paper_id} (PMID {p.pmid}){flag_str}")
        lines.append(f"  Rows: {p.extracted_rows} / {p.gold_rows} ({p.row_coverage:.0%})")
        lines.append(f"  Variants: {len(p.matched_variants)} matched / {len(p.gold_variants)} gold ({p.variant_coverage:.0%})")
        if p.missing_variants:
            lines.append(f"  Missing variants: {p.missing_variants[:10]}")
        if p.extra_variants:
            lines.append(f"  Extra variants (not in gold): {p.extra_variants[:10]}")
        lines.append("")

    return "\n".join(lines)


def save_report(eval_result: AggregateEval, output_path: Path) -> None:
    """Save eval results as JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(asdict(eval_result), indent=2, default=str))


# CLI entry point
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Aggregate evaluation of comparanda extractions")
    parser.add_argument("--extractions-dir", required=True, help="Path to extractions directory")
    parser.add_argument("--gold-standard", required=True, help="Path to gold standard spreadsheet")
    parser.add_argument("--pmid-map", required=True, help="Path to paper_pmid_map.json")
    parser.add_argument("--output", help="Output JSON path")
    parser.add_argument("--identity-dims", nargs="*", help="Identity dimension field names")
    args = parser.parse_args()

    result = evaluate_all(
        extractions_dir=Path(args.extractions_dir),
        gold_standard_path=Path(args.gold_standard),
        pmid_map_path=Path(args.pmid_map),
        identity_dimensions=args.identity_dims,
    )

    print(format_report(result))

    if args.output:
        save_report(result, Path(args.output))
        print(f"\nJSON saved to {args.output}")
