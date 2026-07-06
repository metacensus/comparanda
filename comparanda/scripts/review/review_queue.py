#!/usr/bin/env python3
"""Risk-ranked, field-level human review queue (review note).

The audit found the design had the vocabulary of human review but no mechanism.
This builds the mechanism: a queue of (paper, row, field) items a reviewer should
check, sorted by risk, feeding off the confidence/abstention tags (F4) and the
bundle's per-table validation (F2). Full review is impossible; this triages scarce
human time to the values most likely to be wrong AND most consequential (the
effect/precision/denominator fields that drive pooling).

Risk tiers (highest first; ranks match TIER_RANK, 0-4):
  0. abstained            — the pipeline explicitly could not read it
  1. ambiguous            — concrete uncertainty (uncorroborated table, figure, conflict)
  2. uncorroborated_table — value cites a single_source/unresolved_conflict table
  3. derived/inferred high-leverage — effect/CI/SE/counts that were computed/figure-read
  4. normalizer_dispute   — entity/outcome left unnormalized

Corrections are applied by writing the corrected value with a provenance entry
carrying `reviewed_by`/`reviewed_at`; write_extraction.py then protects that field
from being overwritten on the next iteration (F17 durability).

Usage:
  python3 scripts/review/review_queue.py --extractions-dir projects/<d>/extractions \
      --bundles-dir projects/<d>/data/processed --output projects/<d>/review_queue.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Hardcoded identity fallback (used when --schema is not supplied), matching
# the pre-R2-08 behavior.
DEFAULT_IDENTITY_FIELDS = ("entity_raw", "outcome_raw", "comparison_group")

HIGH_LEVERAGE = {
    "effect_estimate", "effect_size", "effect_size_value",
    "ci_lower", "effect_size_ci_lower",
    "ci_upper", "effect_size_ci_upper",
    "se", "p_value",
    "n_total", "total_n", "group_n", "reference_n",
    "n_events", "group_events", "reference_events", "mean", "sd",
}

TIER_RANK = {
    "abstained": 0,
    "ambiguous": 1,
    "uncorroborated_table": 2,
    "derived_high_leverage": 3,
    "inferred_high_leverage": 3,
    "normalizer_dispute": 4,
}


def _table_validation(bundle_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    manifest = bundle_dir / "manifest.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text())
        except json.JSONDecodeError:
            return out
        for t in m.get("tables", []):
            out[t.get("id", "")] = t.get("validation", "corroborated")
    return out


def _loc_is_uncorroborated(sources, tbl_validation) -> bool:
    import re
    for s in sources or []:
        loc = (s or {}).get("location", "")
        mm = re.search(r"table[_\s]?(\w+)", loc, re.IGNORECASE)
        if mm and tbl_validation.get(f"table_{mm.group(1)}") in {"single_source", "unresolved_conflict"}:
            return True
    return False


def _first_cell(sources) -> dict | None:
    """The structured cell anchor from the first source that carries one
    (review note) — gives the reviewer claim → located evidence."""
    for s in sources or []:
        if isinstance(s, dict) and isinstance(s.get("cell"), dict):
            return s["cell"]
    return None


def _load_disputed_mappings(normalization_dir: Path | None) -> dict[str, dict]:
    """Disputed raw→canonical mappings from the normalization tables.

    The normalizer's contract: sub-0.7-confidence mappings are logged as
    disputes and the canonical id is left null. Previously those disputes
    dead-ended in the observation log — TIER_RANK declared a
    `normalizer_dispute` tier that no code ever assigned. This loads every
    normalization table and returns {raw_value: table_entry} for entries with
    confidence < 0.7 (a deliberate null at confidence 1.0 is NOT a dispute).

    Table files are agent-written; tolerate the common shapes: a top-level
    list of entries, {"entries": [...]}, or {raw: entry}.
    """
    disputed: dict[str, dict] = {}
    if normalization_dir is None or not normalization_dir.exists():
        return disputed
    for tf in sorted(normalization_dir.glob("*.json")):
        try:
            payload = json.loads(tf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
            entries = payload["entries"]
        elif isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            entries = [
                ({"raw": k, **v} if "raw" not in v else v)
                for k, v in payload.items() if isinstance(v, dict)
            ]
        else:
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            raw = e.get("raw")
            conf = e.get("confidence")
            if not isinstance(raw, str) or not isinstance(conf, (int, float)):
                continue
            if conf < 0.7:
                e = dict(e)
                e["_table"] = tf.stem
                disputed[raw] = e
    return disputed


def build_queue(
    extractions_dir: Path,
    bundles_dir: Path | None,
    identity_fields: tuple[str, ...] = DEFAULT_IDENTITY_FIELDS,
    normalization_dir: Path | None = None,
) -> list[dict]:
    disputed = _load_disputed_mappings(normalization_dir)
    items: list[dict] = []
    for jf in sorted(extractions_dir.glob("*.jsonl")):
        paper_id = jf.stem
        tbl_validation = _table_validation(bundles_dir / paper_id) if bundles_dir else {}
        # row_index counts DATA rows (blank lines and the _meta_commentary line
        # excluded) so it addresses the same row apply_review.py will address
        # (R3-06 — the two previously used different indexing schemes).
        ri = -1
        for line in jf.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("_meta_commentary"):
                continue
            ri += 1
            li = ri
            prov = row.get("provenance") or {}

            # R3-03: normalizer disputes — a `_raw` value whose mapping was
            # disputed (confidence < 0.7) and whose paired `_id` is still null
            # is an unresolved canonical assignment a human must adjudicate.
            for key, val in row.items():
                if not (isinstance(key, str) and key.endswith("_raw")):
                    continue
                if not isinstance(val, str) or val not in disputed:
                    continue
                id_field = key[: -len("_raw")] + "_id"
                if id_field not in row or row.get(id_field) is not None:
                    continue
                d = disputed[val]
                raw_prov = prov.get(key) if isinstance(prov.get(key), dict) else {}
                if isinstance(raw_prov.get("reviewed_by"), str):
                    continue
                items.append({
                    "paper_id": paper_id,
                    "row_index": li,
                    "field": id_field,
                    "value": None,
                    "risk_tier": "normalizer_dispute",
                    "rank": TIER_RANK["normalizer_dispute"],
                    "high_leverage": False,
                    "sources": raw_prov.get("sources"),
                    "cell": _first_cell(raw_prov.get("sources")),
                    "reasoning": (
                        f"normalization dispute on {key}={val!r} "
                        f"(table {d.get('_table')}, confidence {d.get('confidence')}): "
                        f"{d.get('rationale') or 'no rationale recorded'}"
                    ),
                    "identity": {k: row.get(k) for k in identity_fields if k in row},
                })
            for field, entry in prov.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("reviewed_by"):
                    continue  # already human-verified
                certainty = entry.get("extraction_certainty")
                dkind = entry.get("derivation_kind")
                tier = None
                if certainty == "abstained":
                    tier = "abstained"
                elif certainty == "ambiguous":
                    tier = "ambiguous"
                elif _loc_is_uncorroborated(entry.get("sources"), tbl_validation):
                    tier = "uncorroborated_table"
                elif dkind in {"derived", "inferred"} and field in HIGH_LEVERAGE:
                    tier = f"{dkind}_high_leverage"
                if tier is None:
                    continue
                items.append({
                    "paper_id": paper_id,
                    "row_index": li,
                    "field": field,
                    "value": row.get(field),
                    "risk_tier": tier,
                    "rank": TIER_RANK.get(tier, 9),
                    "high_leverage": field in HIGH_LEVERAGE,
                    "sources": entry.get("sources"),
                    # R2-01: claim → located evidence → rationale in one artifact.
                    "cell": _first_cell(entry.get("sources")),
                    "reasoning": entry.get("reasoning"),
                    "identity": {k: row.get(k) for k in identity_fields if k in row},
                })
    # high-leverage breaks ties within a tier
    items.sort(key=lambda x: (x["rank"], not x["high_leverage"]))
    return items


def _identity_fields_from_schema(schema_path: Path) -> tuple[str, ...]:
    """Derive the per-item identity context from the schema's
    identity_dimensions (review note), replacing the hardcoded triple."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extraction"))
    import extraction_validator as ev  # noqa: E402

    schema = ev.Schema.load(schema_path)
    dims = tuple(schema.identity_dimensions)
    return dims or DEFAULT_IDENTITY_FIELDS


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a risk-ranked human review queue")
    ap.add_argument("--extractions-dir", required=True)
    ap.add_argument("--bundles-dir", default=None)
    ap.add_argument("--normalization-dir", default=None,
                    help="projects/<domain>/normalization — enables the "
                         "normalizer_dispute tier")
    ap.add_argument("--output", default=None)
    ap.add_argument(
        "--schema", default=None,
        help="Path to the rich schema YAML. When given, per-item identity "
             "context is derived from the schema's identity_dimensions; "
             "otherwise the hardcoded entity_raw/outcome_raw/comparison_group "
             "triple is used as a fallback.",
    )
    a = ap.parse_args()
    identity_fields = (
        _identity_fields_from_schema(Path(a.schema)) if a.schema
        else DEFAULT_IDENTITY_FIELDS
    )
    q = build_queue(
        Path(a.extractions_dir),
        Path(a.bundles_dir) if a.bundles_dir else None,
        identity_fields=identity_fields,
        normalization_dir=Path(a.normalization_dir) if a.normalization_dir else None,
    )
    summary = {"total_items": len(q),
               "by_tier": {t: sum(1 for i in q if i["risk_tier"] == t)
                           for t in {i["risk_tier"] for i in q}},
               "queue": q}
    text = json.dumps(summary, indent=2, default=str)
    if a.output:
        Path(a.output).write_text(text)
    print(f"{len(q)} review items; by tier: {summary['by_tier']}")


if __name__ == "__main__":
    main()
