"""High-leverage extracted-claim faithfulness surface for portal review.

This is a deterministic companion to the LLM detailed evaluator. It scans
extracted rows for non-null, high-leverage numeric claims, follows their
provenance, and records whether the cited quote resolves inside the cited
bundle file. The result is intentionally simple and auditable: reviewers can
use it to prioritize claims whose value is consequential but not quote-traced.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


HIGH_LEVERAGE = {
    "effect_estimate", "effect_size", "effect_size_value",
    "ci_lower", "effect_size_ci_lower",
    "ci_upper", "effect_size_ci_upper",
    "se", "p_value",
    "n_total", "total_n", "group_n", "reference_n",
    "n_events", "group_events", "reference_events", "mean", "sd",
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            obj = json.loads(line)
            if isinstance(obj, dict) and not obj.get("_meta"):
                rows.append(obj)
    return rows


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _read_location(bundles_dir: Path, paper_id: str, location: str) -> tuple[Path | None, str | None]:
    loc = Path(location)
    candidates = []
    if loc.is_absolute():
        candidates.append(loc)
    else:
        candidates.extend([
            bundles_dir / paper_id / loc,
            bundles_dir / loc,
            loc,
        ])
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            try:
                return candidate, candidate.read_text(errors="ignore")
            except OSError:
                return candidate, None
    return None, None


def _source_status(bundles_dir: Path, paper_id: str, source: dict[str, Any]) -> dict[str, Any]:
    quote = str(source.get("quote") or "").strip()
    location = str(source.get("location") or "").strip()
    status = {
        "quote": quote,
        "location": location,
        "page": source.get("page"),
        "cell": source.get("cell"),
        "resolved_path": None,
        "status": "quote_found",
    }
    if not quote:
        status["status"] = "no_quote"
        return status
    if not location:
        status["status"] = "no_location"
        return status

    path, text = _read_location(bundles_dir, paper_id, location)
    if path is None:
        status["status"] = "location_not_found"
        return status
    status["resolved_path"] = str(path)
    if text is None:
        status["status"] = "location_unreadable"
        return status
    if _norm(quote) not in _norm(text):
        status["status"] = "quote_missing"
    return status


def assess_claims(extractions_dir: Path, bundles_dir: Path) -> dict[str, Any]:
    papers: dict[str, Any] = {}
    totals = {
        "claims": 0,
        "quote_found": 0,
        "quote_missing": 0,
        "insufficient_evidence": 0,
        "ambiguous": 0,
        "abstained_not_counted": 0,
    }

    for path in sorted(extractions_dir.glob("*.jsonl")):
        paper_id = path.stem
        claims = []
        for row_index, row in enumerate(_load_jsonl(path)):
            provenance = row.get("provenance") or {}
            if not isinstance(provenance, dict):
                provenance = {}
            for field, value in row.items():
                if field not in HIGH_LEVERAGE:
                    continue
                prov = provenance.get(field) or {}
                certainty = prov.get("extraction_certainty") if isinstance(prov, dict) else None
                if value is None:
                    if certainty == "abstained":
                        totals["abstained_not_counted"] += 1
                    continue

                totals["claims"] += 1
                sources = prov.get("sources") if isinstance(prov, dict) else None
                source_statuses = [
                    _source_status(bundles_dir, paper_id, s)
                    for s in sources or []
                    if isinstance(s, dict)
                ]
                statuses = {s["status"] for s in source_statuses}
                if "quote_found" in statuses:
                    status = "quote_found"
                    totals["quote_found"] += 1
                elif source_statuses:
                    status = "quote_missing"
                    totals["quote_missing"] += 1
                else:
                    status = "insufficient_evidence"
                    totals["insufficient_evidence"] += 1
                if certainty == "ambiguous":
                    totals["ambiguous"] += 1

                claims.append({
                    "row_index": row_index,
                    "field": field,
                    "value": value,
                    "derivation_kind": prov.get("derivation_kind") if isinstance(prov, dict) else None,
                    "extraction_certainty": certainty,
                    "status": status,
                    "sources": source_statuses,
                    "identity": {
                        "entity": row.get("entity_raw") or row.get("entity_id") or row.get("entity_grouping_raw"),
                        "outcome": row.get("outcome_raw") or row.get("outcome_id") or row.get("outcome_canonical"),
                        "comparison_group": row.get("comparison_group_raw") or row.get("comparison_group"),
                    },
                })
        papers[paper_id] = {
            "claims": claims,
            "claim_count": len(claims),
            "quote_found_claims": sum(1 for c in claims if c["status"] == "quote_found"),
        }

    denom = totals["claims"] or 1
    return {
        "metric": "extracted_claim_precision_faithfulness",
        "definition": (
            "Share of non-null high-leverage extracted claims whose provenance "
            "contains a quote that resolves inside the cited bundle file."
        ),
        "faithfulness_precision": totals["quote_found"] / denom,
        "totals": totals,
        "papers": papers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit high-leverage claim faithfulness JSON")
    parser.add_argument("--extractions-dir", type=Path, required=True)
    parser.add_argument("--bundles-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = assess_claims(args.extractions_dir, args.bundles_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
