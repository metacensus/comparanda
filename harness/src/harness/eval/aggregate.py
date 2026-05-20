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
from harness.eval.identity import VariantResolver, load_crosswalk_config


@dataclass
class PaperEval:
    """Evaluation results for a single paper."""

    paper_id: str
    pmid: int | str
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
    normalization: dict = field(default_factory=dict)  # detailed normalization status
    flags: list[str] = field(default_factory=list)


@dataclass
class AggregateEval:
    """Evaluation results across all papers."""

    papers: list[PaperEval]
    mean_row_coverage: float
    mean_variant_coverage: float
    mean_finding_accuracy: float | None  # None = not evaluated (requires detailed mode)
    total_gold_rows: int
    total_extracted_rows: int
    overall_row_coverage: float
    papers_below_50pct: list[str]  # paper_ids with < 50% row coverage
    papers_below_70pct: list[str]  # paper_ids with < 70% row coverage
    schema_contradictions: list[dict] = field(default_factory=list)  # required fields with <100% fill
    composite_score: float = 0.0


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


def _expand_pipe_delimited(variant: str) -> list[str]:
    """Split combination-group variant strings (pipe-delimited) into components.

    Some papers (e.g., Milano 2016) only report effect sizes for combined
    multi-variant carrier groups, not per individual variant. Extraction
    encodes this as `variant_raw="*2A|D949V|I560S (3 consensual SNPs)"`. For
    matching against per-variant gold rows, each component must participate
    independently. A group-level trailing parenthetical annotation is stripped
    before splitting so it doesn't pollute the last component.
    """
    import re
    if "|" not in variant:
        return [variant]
    base = re.sub(r"\s*\([^()]*\)\s*$", "", variant).strip()
    return [c.strip() for c in base.split("|") if c.strip()]


def _extract_variants(rows: list[dict]) -> set[str]:
    """Extract unique variant identifiers from extraction rows.

    Uses all available variant fields (canonical, rsid, raw) so that
    matching against gold standard works regardless of naming convention.
    Pipe-delimited combination groups are expanded into components.
    """
    variants = set()
    for row in rows:
        # Prefer canonical (normalizer output), then rsid, then raw
        for field in ("variant_canonical", "variant_rsid", "variant_raw", "variant"):
            v = row.get(field)
            if v:
                for component in _expand_pipe_delimited(str(v)):
                    variants.add(component)
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


def _compute_normalization_status(rows: list[dict]) -> dict:
    """Compute detailed normalization status for extraction rows.

    Reports what the normalizer actually produced, not what it could have produced.
    Surfaces enough detail for the diagnostician to trace problems to specific agents.
    """
    if not rows:
        return {"total_rows": 0}

    total = len(rows)
    has_canonical = 0
    has_rsid = 0
    raw_only = 0
    raw_only_examples: list[str] = []

    for row in rows:
        v_raw = row.get("variant_raw")
        v_canon = row.get("variant_canonical")
        v_rsid = row.get("variant_rsid")

        if v_canon:
            has_canonical += 1
        if v_rsid:
            has_rsid += 1
        if v_raw and not v_canon and not v_rsid:
            raw_only += 1
            if len(raw_only_examples) < 10:
                raw_only_examples.append(str(v_raw))

    return {
        "total_rows": total,
        "has_variant_canonical": has_canonical,
        "has_variant_rsid": has_rsid,
        "raw_only_no_canonical_no_rsid": raw_only,
        "canonical_rate": round(has_canonical / total, 3) if total else 0.0,
        "unnormalized_rate": round(raw_only / total, 3) if total else 0.0,
        "unnormalized_examples": sorted(set(raw_only_examples)),
    }


def _load_schema_field_metadata(schema_dir: Path | None = None) -> dict[str, dict]:
    """Load field metadata from the latest schema version.

    Returns dict of field_name → {"required": bool, "extraction_guidance": str, "removed": bool}.
    Used to split fill rates by required/optional and flag contradictions.
    """
    if schema_dir is None:
        schema_dir = Path(__file__).parent.parent.parent.parent.parent / "comparanda" / "projects" / "dpyd" / "schema"

    if not schema_dir.exists():
        return {}

    import yaml
    schemas = sorted(schema_dir.glob("v*.yaml"))
    if not schemas:
        return {}

    with open(schemas[-1]) as f:
        schema = yaml.safe_load(f)

    result = {}
    for field_def in schema.get("fields", []):
        name = field_def.get("name", "")
        result[name] = {
            "required": bool(field_def.get("required", False)),
            "extraction_guidance": str(field_def.get("extraction_guidance", ""))[:500],
            "removed": bool(field_def.get("removal_rationale")),
        }
    return result


def _compute_schema_contradictions(
    fill_rates: dict[str, float],
    schema_meta: dict[str, dict],
) -> list[dict]:
    """Find fields marked required: true but with <100% fill rate.

    These may indicate schema internal contradictions (required but guidance permits null)
    or genuine extraction gaps.
    """
    contradictions = []
    for field_name, rate in fill_rates.items():
        meta = schema_meta.get(field_name, {})
        if meta.get("required") and not meta.get("removed") and rate < 1.0:
            contradictions.append({
                "field": field_name,
                "fill_rate": round(rate, 3),
                "required": True,
                "extraction_guidance_excerpt": meta.get("extraction_guidance", "")[:200],
            })
    return sorted(contradictions, key=lambda x: x["fill_rate"])


def _normalize_variant_name(name: str) -> str:
    """Normalize variant name for matching between gold standard and extraction.

    Gold standard bundles variant + genotype (e.g., "rs3918290 GA", "IVS14+1G>A het").
    Extraction separates them (variant_raw + zygosity_group).
    This strips genotype suffixes and normalizes notation to enable matching.
    Does NOT use System 1's normalization table — the eval measures what System 1
    actually produced, not what it could have produced.
    """
    import re

    name = name.strip()

    # 1. Extract rsID if present anywhere — strongest match signal
    rsid_match = re.search(r'(rs\d+)', name, re.IGNORECASE)
    if rsid_match:
        return rsid_match.group(1).lower()

    name_lower = name.lower()

    # 2. Strip parenthetical genotype annotations first (before whitespace collapse)
    # "(HET)", "(WT)", "(heterozygous)", "(homozygous)", "(compound heterozygote)"
    # Also "MUT (het/ho)"
    cleaned = re.sub(
        r'\s*(?:MUT\s*)?\((?:het(?:/ho(?:m)?)?|hom|wt|heterozygous|homozygous|compound heterozygote)\)',
        '', name_lower, flags=re.IGNORECASE,
    ).strip()

    # 3. Strip trailing genotype suffixes (non-parenthetical)
    # "het/hom", "het/ho", "het", "hom", "ho", "WT", "wt"
    # Diplotype genotypes: "GG", "GA", "AA", "TT", "AT", "AT/TT"
    cleaned = re.sub(
        r'\s+(?:het/hom|het/ho|het|hom|ho|wt|[ACGT]{2}(?:/[ACGT]{2,})?)\s*$',
        '', cleaned, flags=re.IGNORECASE,
    ).strip()

    # 4. Strip parenthetical protein annotations: "(E412)", "(R357H)", "(S534N)"
    cleaned = re.sub(r'\s*\([A-Z]\d+[A-Z]?\)', '', cleaned, flags=re.IGNORECASE).strip()

    # 5. Strip leading gene prefix: "DPYD " → keep the rest
    cleaned = re.sub(r'^dpyd\s+', '', cleaned).strip()

    # 6. Normalize whitespace in HGVS notation: "c.1236 G>A" → "c.1236g>a"
    cleaned = re.sub(r'\s+', '', cleaned) if re.match(r'[ci]\.', cleaned) else cleaned
    # Also for IVS notation: "IVS14 + 1G>A" → "ivs14+1g>a"
    if cleaned.startswith('ivs'):
        cleaned = re.sub(r'\s+', '', cleaned)

    # 7. Remove trailing whitespace artifacts
    cleaned = cleaned.strip()

    if not cleaned:
        return name_lower

    # 8. Try "c." prefix for bare HGVS (e.g., "1236g>a" → "c.1236g>a")
    if re.match(r'\d', cleaned):
        return "c." + cleaned

    return cleaned


def evaluate_paper(
    paper_id: str,
    extraction_path: Path,
    gold_df: pd.DataFrame,
    pmid: int | str,
    resolver: VariantResolver,
    identity_dimensions: list[str] | None = None,
) -> PaperEval:
    """Evaluate a single paper's extraction against gold standard.

    Variant matching uses the identity-resolution chain: gold rows and
    extraction rows are each resolved to a canonical token (rs-number via the
    domain crosswalk, or an inline identifier), and coverage is measured on
    those tokens rather than on raw-string heuristics.
    """
    rows = _load_extraction(extraction_path)
    gold_n = gold_row_count(gold_df, pmid)
    paper_gold = gold_df[gold_df["PMID"] == pmid]

    # Resolve gold rows to canonical tokens; keep a representative name per token.
    gold_token_to_name: dict[str, str] = {}
    gold_unresolved = 0
    for _, grow in paper_gold.iterrows():
        tok, _rung = resolver.resolve_gold(grow)
        if tok:
            gold_token_to_name.setdefault(tok, str(grow.get("Variant", "")).strip())
        else:
            gold_unresolved += 1

    # Resolve extraction rows to canonical tokens (combination-group rows
    # contribute every component they represent).
    ext_tokens: set[str] = set()
    for r in rows:
        ext_tokens.update(resolver.resolve_extraction_all(r))

    gold_tokens = set(gold_token_to_name)
    matched_keys = gold_tokens & ext_tokens
    missing_keys = gold_tokens - ext_tokens
    extra_keys = ext_tokens - gold_tokens

    row_coverage = len(rows) / gold_n if gold_n > 0 else 0.0
    variant_coverage = len(matched_keys) / len(gold_tokens) if gold_tokens else 0.0

    gold_vars = set(gold_token_to_name.values())
    gold_vars_norm = gold_token_to_name
    ext_vars_norm = {t: t for t in ext_tokens}
    ext_vars = ext_tokens

    fill_rates = _compute_fill_rates(rows)
    id_completeness = (
        _compute_id_dim_completeness(rows, identity_dimensions)
        if identity_dimensions
        else {}
    )
    norm_status = _compute_normalization_status(rows)

    flags = []
    if row_coverage < 0.5:
        flags.append(f"CRITICAL: row coverage {row_coverage:.0%} < 50%")
    elif row_coverage < 0.7:
        flags.append(f"WARNING: row coverage {row_coverage:.0%} < 70%")
    if row_coverage > 1.5:
        flags.append(f"WARNING: over-extraction {row_coverage:.0%} > 150%")
    if missing_keys:
        flags.append(f"Missing {len(missing_keys)} gold variants: {sorted(missing_keys)[:5]}")
    if gold_unresolved:
        flags.append(
            f"VARIANT RESOLUTION: {gold_unresolved} gold row(s) could not be resolved "
            f"to a canonical token (no crosswalk hit, no inline identifier) — "
            f"excluded from variant_coverage denominator."
        )
    if norm_status.get("unnormalized_rate", 0) > 0.2:
        flags.append(
            f"NORMALIZATION: {norm_status['raw_only_no_canonical_no_rsid']}/{norm_status['total_rows']} rows "
            f"({norm_status['unnormalized_rate']:.0%}) have variant_raw but no canonical/rsid. "
            f"Examples: {norm_status['unnormalized_examples'][:5]}"
        )

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
        normalization=norm_status,
        flags=flags,
    )


def evaluate_all(
    extractions_dir: Path,
    gold_standard_path: Path,
    pmid_map_path: Path,
    identity_dimensions: list[str] | None = None,
    schema_dir: Path | None = None,
) -> AggregateEval:
    """Evaluate all extracted papers against gold standard."""
    gold_df = load_gold_standard(gold_standard_path)
    pmid_map = load_pmid_map(pmid_map_path)
    schema_meta = _load_schema_field_metadata(schema_dir)

    # Build the variant identity resolver once. The crosswalk config (if the
    # domain ships one) lives alongside the pmid map in eval_config/.
    crosswalk_config = load_crosswalk_config(pmid_map_path.parent)
    resolver = VariantResolver.build(gold_standard_path, crosswalk_config)

    pmid_values = set(pmid_map.values())
    papers = []
    for extraction_file in sorted(extractions_dir.glob("*.jsonl")):
        paper_id = extraction_file.stem
        pmid = pmid_map.get(paper_id)
        if pmid is None:
            # The PMID embedded in the filename stem is the most robust key —
            # it survives hyphen/underscore and casing differences in the
            # author portion of the paper_id.
            embedded_pmid = None
            for part in paper_id.split("_"):
                if part.isdigit() and len(part) >= 7:
                    embedded_pmid = int(part)
                    break
            if embedded_pmid is not None and embedded_pmid in pmid_values:
                pmid = embedded_pmid
        if pmid is None:
            # Stem-prefix fallback for paper_ids with no embedded PMID.
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
            resolver=resolver,
            identity_dimensions=identity_dimensions,
        )
        papers.append(paper_eval)

    if not papers:
        return AggregateEval(
            papers=[],
            mean_row_coverage=0.0,
            mean_variant_coverage=0.0,
            mean_finding_accuracy=None,
            total_gold_rows=0,
            total_extracted_rows=0,
            overall_row_coverage=0.0,
            papers_below_50pct=[],
            papers_below_70pct=[],
            composite_score=0.0,
        )

    total_gold = sum(p.gold_rows for p in papers)
    total_ext = sum(p.extracted_rows for p in papers)
    # Cap row coverage at 1.0 per paper for composite (over-extraction shouldn't inflate score)
    mean_row_cov = sum(min(p.row_coverage, 1.0) for p in papers) / len(papers)
    mean_var_cov = sum(p.variant_coverage for p in papers) / len(papers)
    mean_fill = sum(
        sum(p.field_fill_rates.values()) / len(p.field_fill_rates)
        for p in papers
        if p.field_fill_rates
    ) / len(papers) if papers else 0.0

    # Schema contradiction detection: required fields with <100% fill across all papers
    all_fill_rates: dict[str, list[float]] = {}
    for p in papers:
        for fname, rate in p.field_fill_rates.items():
            all_fill_rates.setdefault(fname, []).append(rate)
    avg_fill_rates = {f: sum(rates) / len(rates) for f, rates in all_fill_rates.items()}
    schema_contradictions = _compute_schema_contradictions(avg_fill_rates, schema_meta) if schema_meta else []

    # Aggregate mode: completeness metrics only. finding_accuracy requires detailed mode
    # (the eval agent reasoning about matched rows, not a Python scoring function).
    # Composite excludes finding_accuracy — max possible is 0.7.
    composite = 0.4 * mean_row_cov + 0.2 * mean_var_cov + 0.1 * mean_fill

    return AggregateEval(
        papers=papers,
        mean_row_coverage=round(mean_row_cov, 3),
        mean_variant_coverage=round(mean_var_cov, 3),
        mean_finding_accuracy=None,  # requires eval agent detailed mode
        total_gold_rows=total_gold,
        total_extracted_rows=total_ext,
        overall_row_coverage=round(total_ext / total_gold, 3) if total_gold else 0.0,
        papers_below_50pct=[p.paper_id for p in papers if p.row_coverage < 0.5],
        papers_below_70pct=[p.paper_id for p in papers if p.row_coverage < 0.7],
        schema_contradictions=schema_contradictions,
        composite_score=round(composite, 3),
    )


def format_report(eval_result: AggregateEval) -> str:
    """Format eval results as a human-readable report."""
    lines = ["# Aggregate Evaluation Report", ""]
    lines.append(f"Papers evaluated: {len(eval_result.papers)}")
    lines.append(f"Total rows: {eval_result.total_extracted_rows} extracted / {eval_result.total_gold_rows} gold ({eval_result.overall_row_coverage:.0%})")
    lines.append(f"Mean row coverage: {eval_result.mean_row_coverage:.0%}")
    lines.append(f"Mean variant coverage: {eval_result.mean_variant_coverage:.0%}")
    lines.append(f"Finding accuracy: NOT EVALUATED (requires eval agent detailed mode)")
    lines.append(f"Aggregate composite: {eval_result.composite_score:.3f} (max 0.7 — excludes finding_accuracy)")
    lines.append("")

    if eval_result.papers_below_50pct:
        lines.append(f"CRITICAL — papers below 50% row coverage: {eval_result.papers_below_50pct}")
    if eval_result.papers_below_70pct:
        below_70_not_50 = [p for p in eval_result.papers_below_70pct if p not in eval_result.papers_below_50pct]
        if below_70_not_50:
            lines.append(f"WARNING — papers below 70% row coverage: {below_70_not_50}")

    # Schema contradictions
    if eval_result.schema_contradictions:
        lines.append("## Schema Contradictions")
        lines.append("Fields marked `required: true` in schema but with <100% fill rate across extractions:")
        lines.append("(May indicate schema internal inconsistency — check if extraction_guidance permits null)")
        lines.append("")
        for c in eval_result.schema_contradictions[:15]:
            lines.append(f"  {c['field']}: {c['fill_rate']:.0%} fill rate (required: true)")
            if c.get("extraction_guidance_excerpt"):
                lines.append(f"    guidance: {c['extraction_guidance_excerpt'][:150]}")
        lines.append("")

    # Normalization summary
    norm_papers = [p for p in eval_result.papers if p.normalization.get("unnormalized_rate", 0) > 0.1]
    if norm_papers:
        lines.append("")
        lines.append("## Normalization Gaps")
        lines.append("Papers where >10% of rows have variant_raw but no variant_canonical or variant_rsid:")
        lines.append("(This may indicate normalizer instruction gaps, schema contradictions, or intentionally un-normalizable rows)")
        lines.append("")
        for p in sorted(norm_papers, key=lambda x: x.normalization.get("unnormalized_rate", 0), reverse=True):
            n = p.normalization
            lines.append(
                f"  {p.paper_id}: {n['raw_only_no_canonical_no_rsid']}/{n['total_rows']} rows "
                f"({n['unnormalized_rate']:.0%}) unnormalized. "
                f"Examples: {n['unnormalized_examples'][:5]}"
            )
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
