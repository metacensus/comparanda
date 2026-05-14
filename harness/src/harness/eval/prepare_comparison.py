"""Prepare structured comparison data for eval agent detailed mode.

Loads gold standard and extraction rows for a single paper, pairs them by
normalized variant + zygosity, and outputs a readable JSON file the eval agent
can reason about. No scoring, no judgment — just data alignment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from harness.eval.aggregate import _load_extraction, _normalize_variant_name
from harness.eval.gold_standard import load_gold_standard, load_pmid_map


def _extract_genotype_hint(variant_name: str) -> str | None:
    """Extract genotype hint from gold variant name for pairing context."""
    name = variant_name.strip().lower()
    if "het/hom" in name or "het/ho" in name:
        return "any_carrier"
    if "(heterozygous)" in name or name.endswith(" het"):
        return "heterozygous"
    if "(homozygous)" in name or name.endswith(" hom"):
        return "homozygous"
    if name.endswith(" wt"):
        return "wildtype"
    # Diplotype suffix
    m = re.search(r'\s+([ACGT]{2})$', variant_name.strip())
    if m:
        geno = m.group(1).upper()
        return "homozygous_ref_or_alt" if geno[0] == geno[1] else "heterozygous"
    return None


def _gold_row_to_dict(row: pd.Series) -> dict:
    """Convert a gold standard row to a clean dict with key fields."""
    result = {}
    for col in row.index:
        val = row[col]
        if pd.isna(val):
            result[col] = None
        elif isinstance(val, (int, float)):
            result[col] = val if not pd.isna(val) else None
        else:
            result[col] = str(val)
    return result


def prepare_comparison(
    paper_id: str,
    extractions_dir: Path,
    gold_standard_path: Path,
    pmid_map_path: Path,
) -> dict:
    """Prepare a structured comparison for one paper.

    Returns a dict with paired rows, unmatched rows, and metadata.
    """
    gold_df = load_gold_standard(gold_standard_path)
    pmid_map = load_pmid_map(pmid_map_path)

    # Resolve PMID
    pmid = pmid_map.get(paper_id)
    if pmid is None:
        for map_key, map_pmid in pmid_map.items():
            if paper_id.lower().startswith(map_key.lower().split()[0].lower()):
                parts = paper_id.split("_")
                embedded_pmid = None
                for part in parts:
                    if part.isdigit() and len(part) >= 7:
                        embedded_pmid = int(part)
                        break
                if embedded_pmid is not None and embedded_pmid != map_pmid:
                    continue
                pmid = map_pmid
                break

    if pmid is None:
        return {"error": f"No PMID found for paper_id={paper_id}"}

    # Load data
    extraction_path = extractions_dir / f"{paper_id}.jsonl"
    if not extraction_path.exists():
        return {"error": f"No extraction file at {extraction_path}"}

    extraction_rows = _load_extraction(extraction_path)
    paper_gold = gold_df[gold_df["PMID"] == pmid].reset_index(drop=True)

    if len(paper_gold) == 0:
        return {"error": f"No gold standard rows for PMID {pmid}"}

    # Build extraction lookup by normalized variant
    ext_by_variant: dict[str, list[tuple[int, dict]]] = {}
    for i, row in enumerate(extraction_rows):
        variants = set()
        for vfield in ("variant_canonical", "variant_rsid", "variant_raw", "variant"):
            v = row.get(vfield)
            if v:
                variants.add(_normalize_variant_name(str(v)))
        for nv in variants:
            ext_by_variant.setdefault(nv, []).append((i, row))

    # Pair gold rows to extraction candidates
    paired = []
    used_ext = set()

    for gi in range(len(paper_gold)):
        gold_row = paper_gold.iloc[gi]
        gold_variant = str(gold_row.get("Variant", ""))
        gold_norm = _normalize_variant_name(gold_variant)
        gold_genotype = _extract_genotype_hint(gold_variant)

        candidates = ext_by_variant.get(gold_norm, [])

        # Build candidate list with context
        candidate_list = []
        for ei, ext_row in candidates:
            candidate_list.append({
                "extraction_index": ei,
                "variant_raw": ext_row.get("variant_raw"),
                "variant_canonical": ext_row.get("variant_canonical"),
                "variant_rsid": ext_row.get("variant_rsid"),
                "zygosity_group": ext_row.get("zygosity_group"),
                "toxicity_type": ext_row.get("toxicity_type"),
                "toxicity_grade_threshold": ext_row.get("toxicity_grade_threshold"),
                "effect_size": ext_row.get("effect_size"),
                "effect_measure": ext_row.get("effect_measure"),
                "ci_lower": ext_row.get("ci_lower"),
                "ci_upper": ext_row.get("ci_upper"),
                "n_carriers": ext_row.get("n_carriers"),
                "n_wildtype": ext_row.get("n_wildtype"),
                "n_events_carrier": ext_row.get("n_events_carrier"),
                "n_events_wildtype": ext_row.get("n_events_wildtype"),
                "already_used": ei in used_ext,
            })

        # Mark best candidate as used (simple: first unused match)
        best_unused = next((c for c in candidate_list if not c["already_used"]), None)
        if best_unused:
            used_ext.add(best_unused["extraction_index"])

        paired.append({
            "gold_index": gi,
            "gold_variant": gold_variant,
            "gold_variant_normalized": gold_norm,
            "gold_genotype_hint": gold_genotype,
            "gold_fields": {
                "Outcome Description": gold_row.get("Outcome Description"),
                "Grade Clean": gold_row.get("Grade Clean"),
                "Outcome Category": gold_row.get("Outcome Category"),
                "Effect Size": gold_row.get("Effect Size"),
                "Lower 95 CI": gold_row.get("Lower 95 CI"),
                "Upper 95 CI": gold_row.get("Upper 95 CI"),
                "P Value": gold_row.get("P Value"),
                "Patients with Variant": gold_row.get("Patients with Variant"),
                "Patients with Variant and Outcome": gold_row.get("Patients with Variant and Outcome"),
                "Comparison Type": gold_row.get("Comparison Type"),
            },
            "extraction_candidates": candidate_list,
            "has_match": len(candidate_list) > 0,
        })

    # Unmatched extraction rows
    all_ext_indices = set(range(len(extraction_rows)))
    matched_ext_indices = set()
    for p in paired:
        for c in p["extraction_candidates"]:
            matched_ext_indices.add(c["extraction_index"])

    unmatched_ext = []
    for ei in sorted(all_ext_indices - matched_ext_indices):
        row = extraction_rows[ei]
        unmatched_ext.append({
            "extraction_index": ei,
            "variant_raw": row.get("variant_raw"),
            "variant_canonical": row.get("variant_canonical"),
            "zygosity_group": row.get("zygosity_group"),
            "toxicity_type": row.get("toxicity_type"),
            "toxicity_grade_threshold": row.get("toxicity_grade_threshold"),
            "n_carriers": row.get("n_carriers"),
        })

    # Normalization status
    norm_status = {
        "total_extraction_rows": len(extraction_rows),
        "has_canonical": sum(1 for r in extraction_rows if r.get("variant_canonical")),
        "has_rsid": sum(1 for r in extraction_rows if r.get("variant_rsid")),
        "raw_only": sum(
            1 for r in extraction_rows
            if r.get("variant_raw") and not r.get("variant_canonical") and not r.get("variant_rsid")
        ),
    }

    # Clean NaN values for JSON serialization
    def clean_for_json(obj):
        if isinstance(obj, dict):
            return {k: clean_for_json(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean_for_json(v) for v in obj]
        if isinstance(obj, float) and (obj != obj):  # NaN check
            return None
        try:
            if pd.isna(obj):
                return None
        except (TypeError, ValueError):
            pass
        return obj

    return clean_for_json({
        "paper_id": paper_id,
        "pmid": pmid,
        "gold_rows": len(paper_gold),
        "extraction_rows": len(extraction_rows),
        "paired_count": sum(1 for p in paired if p["has_match"]),
        "unmatched_gold_count": sum(1 for p in paired if not p["has_match"]),
        "unmatched_extraction_count": len(unmatched_ext),
        "normalization_status": norm_status,
        "paired_rows": paired,
        "unmatched_extraction_rows": unmatched_ext,
    })


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prepare gold vs extraction comparison for eval agent")
    parser.add_argument("--paper-id", required=True, help="Paper ID (extraction filename stem)")
    parser.add_argument("--extractions-dir", required=True, help="Path to extractions directory")
    parser.add_argument("--gold-standard", required=True, help="Path to gold standard spreadsheet")
    parser.add_argument("--pmid-map", required=True, help="Path to paper_pmid_map.json")
    parser.add_argument("--output", help="Output JSON path (defaults to stdout)")
    args = parser.parse_args()

    result = prepare_comparison(
        paper_id=args.paper_id,
        extractions_dir=Path(args.extractions_dir),
        gold_standard_path=Path(args.gold_standard),
        pmid_map_path=Path(args.pmid_map),
    )

    output_json = json.dumps(result, indent=2, default=str)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_json)
        print(f"Comparison written to {args.output}")
        print(f"  Gold rows: {result.get('gold_rows', 0)}")
        print(f"  Extraction rows: {result.get('extraction_rows', 0)}")
        print(f"  Paired: {result.get('paired_count', 0)}")
        print(f"  Unmatched gold: {result.get('unmatched_gold_count', 0)}")
        print(f"  Unmatched extraction: {result.get('unmatched_extraction_count', 0)}")
    else:
        print(output_json)
