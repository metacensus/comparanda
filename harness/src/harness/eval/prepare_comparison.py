"""Prepare structured comparison data for the eval agent's detailed mode.

Loads gold standard and extraction rows for a single paper, resolves each row
to a canonical entity token via the identity-resolution chain (see
`identity.py`), pairs gold rows to extraction candidates that share a token,
and emits a readable JSON the eval agent reasons over. No scoring, no judgment
— just data alignment.

Design guarantees:
  - Every extraction row is emitted with its FULL field set, whether it paired
    or not. There is no second-class "unmatched" projection that drops
    effect_size / ci_lower / ci_upper — a stripped projection previously caused
    the agent to report CIs as absent when they were present.
  - Entity pairing is deterministic when the gold row resolves to a canonical
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
from harness.eval.gold_config import load_gold_config
from harness.eval.gold_standard import load_gold_standard, load_document_id_map
from harness.eval.identity import EntityResolver, load_crosswalk_config


def _extract_category_hint(entity_name: str) -> str | None:
    """Extract a category-qualifier hint from a gold entity name for pairing context.

    Gold rows sometimes append a category qualifier to the entity string. This
    surfaces that qualifier so the agent can use it when pairing. The base
    implementation only looks for a parenthetical qualifier; richer,
    domain-specific qualifier parsing plugs in here.

    # Domain-specific identity rules plug in here.
    """
    m = re.search(r"\(([^()]+)\)\s*$", entity_name.strip())
    if m:
        return m.group(1).strip().lower() or None
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


def _embedded_document_id(paper_id: str) -> int | None:
    """The document id embedded in a paper_id filename stem, if any."""
    for part in paper_id.split("_"):
        if part.isdigit() and len(part) >= 7:
            return int(part)
    return None


def _resolve_document_id(paper_id: str, document_id_map: dict) -> int | str | None:
    """Resolve a paper_id to a document id.

    Tries, in order: exact map key; the document id embedded in the filename
    stem; then a stem-prefix fallback. The embedded id is accepted even when it
    is absent from the configured map so a paper with extraction rows but no
    gold rows can be reported as `benchmark_status=no_gold_rows` instead of a
    generic comparison error.
    """
    document_id = document_id_map.get(paper_id)
    if document_id is not None:
        return document_id
    embedded = _embedded_document_id(paper_id)
    if embedded is not None:
        return embedded
    for map_key, map_document_id in document_id_map.items():
        if paper_id.lower().startswith(map_key.lower().split()[0].lower()):
            if embedded is not None and embedded != map_document_id:
                continue
            return map_document_id
    return None


def _status_result(
    *,
    paper_id: str,
    document_id: int | str | None,
    benchmark_status: str,
    gold_scored: bool,
    precision_only: bool = False,
    gold_config_loaded: bool = False,
    gold_missing_columns: list[str] | None = None,
    gold_rows: int = 0,
    extraction_rows: int = 0,
    reason: str,
    unmatched_extraction_rows: list[dict] | None = None,
) -> dict:
    """Structured non-scored comparison status.

    These states are expected benchmark surfaces, not exceptions. Downstream
    eval code can exclude non-gold-scored papers from gold denominators while
    still using extraction rows for precision/faithfulness surfaces.
    """
    return {
        "paper_id": paper_id,
        "document_id": document_id,
        "benchmark_status": benchmark_status,
        "gold_scored": gold_scored,
        "precision_only": precision_only,
        "gold_config_loaded": gold_config_loaded,
        "gold_missing_columns": gold_missing_columns or [],
        "gold_rows": gold_rows,
        "extraction_rows": extraction_rows,
        "paired_count": 0,
        "unmatched_gold_count": gold_rows,
        "unmatched_extraction_count": extraction_rows,
        "paired_rows": [],
        "unmatched_extraction_rows": unmatched_extraction_rows or [],
        "reason": reason,
    }


def prepare_comparison(
    paper_id: str,
    extractions_dir: Path,
    gold_standard_path: Path,
    document_id_map_path: Path,
) -> dict:
    """Prepare a structured gold-vs-extraction comparison for one paper."""
    # Gold-standard schema config lives alongside the document id map in
    # eval_config/ (review note). Defaults preserve legacy behavior.
    eval_config_dir = document_id_map_path.parent
    gold_config = load_gold_config(eval_config_dir)
    gold_config_loaded = (eval_config_dir / "gold_config.json").exists()

    gold_df = load_gold_standard(gold_standard_path, gold_config=gold_config)
    document_id_map = load_document_id_map(document_id_map_path)

    document_id = _resolve_document_id(paper_id, document_id_map)
    if document_id is None:
        return _status_result(
            paper_id=paper_id,
            document_id=None,
            benchmark_status="comparison_error",
            gold_scored=False,
            gold_config_loaded=gold_config_loaded,
            reason=f"No document_id found for paper_id={paper_id}",
        )

    extraction_path = extractions_dir / f"{paper_id}.jsonl"
    if not extraction_path.exists():
        doc_col = gold_config.document_id_column
        paper_gold = gold_df[gold_df[doc_col] == document_id].reset_index(drop=True)
        return _status_result(
            paper_id=paper_id,
            document_id=document_id,
            benchmark_status="no_extraction_file",
            gold_scored=False,
            gold_config_loaded=gold_config_loaded,
            gold_rows=len(paper_gold),
            reason=f"No extraction file at {extraction_path}",
        )

    # Loud surface for configured gold columns absent from the spreadsheet —
    # otherwise these become silent nulls and a vacuously clean value diff.
    expected_cols = set(gold_config.gold_fields) | set(gold_config.value_column_map)
    gold_missing_columns = sorted(expected_cols - set(gold_df.columns))

    doc_col = gold_config.document_id_column
    extraction_rows = _load_extraction(extraction_path)
    paper_gold = gold_df[gold_df[doc_col] == document_id].reset_index(drop=True)
    if len(paper_gold) == 0:
        return _status_result(
            paper_id=paper_id,
            document_id=document_id,
            benchmark_status="no_gold_rows",
            gold_scored=False,
            precision_only=True,
            gold_config_loaded=gold_config_loaded,
            gold_missing_columns=gold_missing_columns,
            gold_rows=0,
            extraction_rows=len(extraction_rows),
            unmatched_extraction_rows=[
                _candidate_view(i, row, None, False)
                for i, row in enumerate(extraction_rows)
            ],
            reason="PMID absent from the configured gold sheet",
        )

    # First configured entity column present in the spreadsheet — the display field.
    gold_entity_field = next(
        (c for c in gold_config.entity_columns if c in gold_df.columns),
        gold_config.entity_columns[0],
    )

    # Build the entity identity resolver. The crosswalk config (if any) lives
    # alongside the document id map in the eval_config directory — domain data.
    crosswalk_config = load_crosswalk_config(eval_config_dir)
    resolver = EntityResolver.build(gold_standard_path, crosswalk_config)
    resolver.entity_fields = tuple(gold_config.entity_columns)
    resolver.extraction_fields = tuple(gold_config.extraction_entity_fields)  # R3-13

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
        gold_entity = str(gold_row.get(gold_entity_field, ""))
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
            "gold_entity": gold_entity,
            "gold_resolved_token": gold_token,
            "resolution_rung": rung,
            "gold_category_hint": _extract_category_hint(gold_entity),
            "gold_fields": {
                col: _clean_cell(gold_row.get(col))
                for col in gold_config.gold_fields
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
        "has_canonical": sum(1 for r in extraction_rows if r.get("entity_canonical")),
        "has_canonical_id": sum(1 for r in extraction_rows if r.get("entity_canonical_id")),
    }

    return {
        "paper_id": paper_id,
        "document_id": document_id,
        "benchmark_status": "gold_scored",
        "gold_scored": True,
        "precision_only": False,
        "gold_config_loaded": gold_config_loaded,
        "gold_missing_columns": gold_missing_columns,
        "gold_rows": len(paper_gold),
        "extraction_rows": len(extraction_rows),
        "paired_count": sum(1 for p in paired if p["has_match"]),
        "unmatched_gold_count": sum(1 for p in paired if not p["has_match"]),
        "unmatched_extraction_count": len(unmatched_ext),
        "entity_resolution": {
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
    parser.add_argument("--document-id-map", required=True, help="Path to document_id_map.json")
    parser.add_argument("--output", help="Output JSON path (defaults to stdout)")
    args = parser.parse_args()

    result = prepare_comparison(
        paper_id=args.paper_id,
        extractions_dir=Path(args.extractions_dir),
        gold_standard_path=Path(args.gold_standard),
        document_id_map_path=Path(args.document_id_map),
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
        if "entity_resolution" in result:
            print(f"  gold_rung_counts: {result['entity_resolution']['gold_rung_counts']}")
    else:
        print(output_json)
