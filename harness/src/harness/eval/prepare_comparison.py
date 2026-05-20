"""Prepare structured comparison data for the eval agent's detailed mode.

Loads gold standard and extraction rows for a single paper, resolves each row
to a canonical variant token via the identity-resolution chain (see
`identity.py`), pairs gold rows to extraction candidates that share a token,
and emits a readable JSON the eval agent reasons over. No scoring, no judgment
— just data alignment.

Design guarantees:
  - Every extraction row is emitted with its FULL field set, whether it paired
    or not. There is no second-class "unmatched" projection that drops
    effect_size / ci_lower / ci_upper — a stripped projection previously caused
    the agent to report CIs as absent when they were present.
  - Variant pairing is deterministic when the gold row resolves to a canonical
    token (directly or via the domain crosswalk). Gold rows that cannot be
    resolved are emitted with `resolution_rung: "unresolved"` so the agent
    knows to pair them semantically itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from harness.eval.aggregate import _load_extraction
from harness.eval.gold_standard import load_gold_standard, load_pmid_map
from harness.eval.identity import VariantResolver, load_crosswalk_config


def _extract_genotype_hint(variant_name: str) -> str | None:
    """Extract a zygosity hint from a gold variant name for pairing context."""
    name = variant_name.strip().lower()
    if "het/hom" in name or "het/ho" in name:
        return "any_carrier"
    if "(heterozygous)" in name or name.endswith(" het"):
        return "heterozygous"
    if "(homozygous)" in name or name.endswith(" hom"):
        return "homozygous"
    if name.endswith(" wt"):
        return "wildtype"
    m = re.search(r"\s+([ACGT]{2})$", variant_name.strip())
    if m:
        geno = m.group(1).upper()
        return "homozygous_ref_or_alt" if geno[0] == geno[1] else "heterozygous"
    return None


def _clean_cell(val: object) -> object:
    """Coerce a pandas/JSON cell to a JSON-safe scalar."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, (int, float, bool, str)):
        return val
    return str(val)


def _candidate_view(index: int, row: dict, token: str | None, used: bool) -> dict:
    """Full-field view of an extraction row. Used for paired AND unmatched rows
    alike — no field is ever dropped."""
    view = {"extraction_index": index, "resolved_token": token, "already_used": used}
    for k, v in row.items():
        if k not in view:
            view[k] = _clean_cell(v)
    return view


def _embedded_pmid(paper_id: str) -> int | None:
    """The PMID embedded in a paper_id filename stem, if any."""
    for part in paper_id.split("_"):
        if part.isdigit() and len(part) >= 7:
            return int(part)
    return None


def _resolve_pmid(paper_id: str, pmid_map: dict) -> int | str | None:
    """Resolve a paper_id to a PMID.

    Tries, in order: exact map key; the PMID embedded in the filename stem
    (matched against map values — robust to hyphen/underscore and casing
    differences in the author portion); then a stem-prefix fallback.
    """
    pmid = pmid_map.get(paper_id)
    if pmid is not None:
        return pmid
    embedded = _embedded_pmid(paper_id)
    if embedded is not None and embedded in set(pmid_map.values()):
        return embedded
    for map_key, map_pmid in pmid_map.items():
        if paper_id.lower().startswith(map_key.lower().split()[0].lower()):
            if embedded is not None and embedded != map_pmid:
                continue
            return map_pmid
    return None


def prepare_comparison(
    paper_id: str,
    extractions_dir: Path,
    gold_standard_path: Path,
    pmid_map_path: Path,
) -> dict:
    """Prepare a structured gold-vs-extraction comparison for one paper."""
    gold_df = load_gold_standard(gold_standard_path)
    pmid_map = load_pmid_map(pmid_map_path)

    pmid = _resolve_pmid(paper_id, pmid_map)
    if pmid is None:
        return {"error": f"No PMID found for paper_id={paper_id}"}

    extraction_path = extractions_dir / f"{paper_id}.jsonl"
    if not extraction_path.exists():
        return {"error": f"No extraction file at {extraction_path}"}

    extraction_rows = _load_extraction(extraction_path)
    paper_gold = gold_df[gold_df["PMID"] == pmid].reset_index(drop=True)
    if len(paper_gold) == 0:
        return {"error": f"No gold standard rows for PMID {pmid}"}

    # Build the variant identity resolver. The crosswalk config (if any) lives
    # alongside the pmid map in the eval_config directory — domain data.
    crosswalk_config = load_crosswalk_config(pmid_map_path.parent)
    resolver = VariantResolver.build(gold_standard_path, crosswalk_config)

    # Resolve every extraction row to a canonical token.
    ext_by_token: dict[str, list[int]] = {}
    ext_tokens: list[str | None] = []
    for i, row in enumerate(extraction_rows):
        toks = resolver.resolve_extraction_all(row)
        ext_tokens.append(toks[0] if toks else None)
        for t in toks:
            ext_by_token.setdefault(t, []).append(i)

    # Pair each gold row to extraction candidates sharing its token.
    paired = []
    used_ext: set[int] = set()
    rung_counts: dict[str, int] = {}

    for gi in range(len(paper_gold)):
        gold_row = paper_gold.iloc[gi]
        gold_variant = str(gold_row.get("Variant", ""))
        gold_token, rung = resolver.resolve_gold(gold_row)
        rung_counts[rung] = rung_counts.get(rung, 0) + 1

        cand_indices = ext_by_token.get(gold_token, []) if gold_token else []
        candidate_list = [
            _candidate_view(ei, extraction_rows[ei], ext_tokens[ei], ei in used_ext)
            for ei in cand_indices
        ]
        best_unused = next((c for c in candidate_list if not c["already_used"]), None)
        if best_unused:
            used_ext.add(best_unused["extraction_index"])

        paired.append({
            "gold_index": gi,
            "gold_variant": gold_variant,
            "gold_resolved_token": gold_token,
            "resolution_rung": rung,
            "gold_genotype_hint": _extract_genotype_hint(gold_variant),
            "gold_fields": {
                col: _clean_cell(gold_row.get(col))
                for col in (
                    "Variant ID", "Diplotype ID", "Outcome Description",
                    "Grade Clean", "Outcome Category", "Effect Size",
                    "Lower 95 CI", "Upper 95 CI", "P Value",
                    "Patients with Variant", "Patients with Variant and Outcome",
                    "Comparison Type",
                )
            },
            "extraction_candidates": candidate_list,
            "has_match": len(candidate_list) > 0,
        })

    # Every extraction row that paired with no gold row — emitted with FULL
    # fields, identical projection to paired candidates.
    matched_ext = {c["extraction_index"] for p in paired for c in p["extraction_candidates"]}
    unmatched_ext = [
        _candidate_view(i, extraction_rows[i], ext_tokens[i], i in used_ext)
        for i in sorted(set(range(len(extraction_rows))) - matched_ext)
    ]

    norm_status = {
        "total_extraction_rows": len(extraction_rows),
        "extraction_resolved": sum(1 for t in ext_tokens if t),
        "extraction_unresolved": sum(1 for t in ext_tokens if not t),
        "has_canonical": sum(1 for r in extraction_rows if r.get("variant_canonical")),
        "has_rsid": sum(1 for r in extraction_rows if r.get("variant_rsid")),
    }

    return {
        "paper_id": paper_id,
        "pmid": pmid,
        "gold_rows": len(paper_gold),
        "extraction_rows": len(extraction_rows),
        "paired_count": sum(1 for p in paired if p["has_match"]),
        "unmatched_gold_count": sum(1 for p in paired if not p["has_match"]),
        "unmatched_extraction_count": len(unmatched_ext),
        "variant_resolution": {
            "crosswalk_loaded": resolver.crosswalk_loaded,
            "crosswalk_conflicts": len(resolver.conflicts),
            "gold_rung_counts": rung_counts,
            "note": (
                "Gold rows with resolution_rung 'unresolved' could not be "
                "resolved to a canonical token; pair them semantically yourself."
            ),
        },
        "normalization_status": norm_status,
        "paired_rows": paired,
        "unmatched_extraction_rows": unmatched_ext,
    }


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
        for k in ("gold_rows", "extraction_rows", "paired_count",
                  "unmatched_gold_count", "unmatched_extraction_count"):
            print(f"  {k}: {result.get(k, 0)}")
        if "variant_resolution" in result:
            print(f"  gold_rung_counts: {result['variant_resolution']['gold_rung_counts']}")
    else:
        print(output_json)
