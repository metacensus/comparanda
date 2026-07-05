"""Aggregate evaluation — fast row-level metrics comparing extraction vs gold standard.

This is the primary eval tool. Run after every comparanda iteration.
Catches completeness issues (e.g. a paper extracted at only 27% of its gold
rows) immediately without LLM calls.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from harness.eval.composite import gating_composite
from harness.eval.gold_config import load_gold_config
from harness.eval.gold_standard import (
    gold_row_count,
    gold_entities_for_paper,
    load_gold_standard,
    load_document_id_map,
)
from harness.eval.identity import EntityResolver, load_crosswalk_config


# High-leverage numeric fields whose uncertainty matters most for pooling.
# Mirror of comparanda/scripts/review_queue.py:32 (HIGH_LEVERAGE) — keep the
# two sets in sync (review note).
HIGH_LEVERAGE = {
    "effect_estimate", "effect_size", "effect_size_value",
    "ci_lower", "effect_size_ci_lower",
    "ci_upper", "effect_size_ci_upper",
    "se", "p_value",
    "n_total", "total_n", "group_n", "reference_n",
    "n_events", "group_events", "reference_events", "mean", "sd",
}


def _certainty_profile(rows: list[dict]) -> dict:
    """Per-paper certainty profile from each row's `provenance` (review note).

    Scans every provenance entry (`{field: {extraction_certainty, ...}}`):
      - clean / ambiguous: a provenance entry on a non-null field with that tag.
      - abstained: a provenance entry whose extraction_certainty == "abstained"
        (these sit on null fields — the explicit "couldn't read" state).
      - untagged: a provenance entry on a non-null field with no certainty tag.
      - high_leverage_*: counts restricted to the HIGH_LEVERAGE field set;
        high_leverage_uncertain = ambiguous + abstained on those fields.
    """
    profile = {
        "clean": 0, "ambiguous": 0, "abstained": 0, "untagged": 0,
        "high_leverage_uncertain": 0, "high_leverage_total": 0,
    }
    for row in rows:
        prov = row.get("provenance") or {}
        if not isinstance(prov, dict):
            continue
        for field_name, entry in prov.items():
            if not isinstance(entry, dict):
                continue
            certainty = entry.get("extraction_certainty")
            high_lev = field_name in HIGH_LEVERAGE
            field_is_null = row.get(field_name) is None

            if certainty == "abstained":
                profile["abstained"] += 1
            elif certainty == "clean":
                profile["clean"] += 1
            elif certainty == "ambiguous":
                profile["ambiguous"] += 1
            elif not field_is_null:
                # Non-null field with provenance but no certainty tag.
                profile["untagged"] += 1

            if high_lev:
                profile["high_leverage_total"] += 1
                if certainty in ("ambiguous", "abstained"):
                    profile["high_leverage_uncertain"] += 1
    return profile


@dataclass
class PaperEval:
    """Evaluation results for a single paper."""

    paper_id: str
    document_id: int | str
    gold_rows: int
    extracted_rows: int
    row_coverage: float  # extracted / gold
    gold_entities: list[str]
    extracted_entities: list[str]
    matched_entities: list[str]
    missing_entities: list[str]
    extra_entities: list[str]
    entity_coverage: float  # matched / gold
    field_fill_rates: dict[str, float] = field(default_factory=dict)
    identity_dim_completeness: dict[str, float] = field(default_factory=dict)
    normalization: dict = field(default_factory=dict)  # detailed normalization status
    certainty_profile: dict = field(default_factory=dict)  # review note
    flags: list[str] = field(default_factory=list)


@dataclass
class AggregateEval:
    """Evaluation results across all papers."""

    papers: list[PaperEval]
    mean_row_coverage: float
    mean_entity_coverage: float
    mean_finding_accuracy: float | None  # None = not evaluated (requires detailed mode)
    total_gold_rows: int
    total_extracted_rows: int
    overall_row_coverage: float
    papers_below_50pct: list[str]  # paper_ids with < 50% row coverage
    papers_below_70pct: list[str]  # paper_ids with < 70% row coverage
    schema_contradictions: list[dict] = field(default_factory=list)  # required fields with <100% fill
    certainty_rollup: dict = field(default_factory=dict)  # corpus certainty totals (review note)
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


def _expand_pipe_delimited(entity: str) -> list[str]:
    """Split combination-group entity strings (pipe-delimited) into components.

    Some studies only report effect sizes for combined multi-entity groups,
    not per individual entity. Extraction encodes this as
    `entity_raw="E1|E2|E3 (3 combined entities)"`. For matching against
    per-entity gold rows, each component must participate independently. A
    group-level trailing parenthetical annotation is stripped before splitting
    so it doesn't pollute the last component.
    """
    import re
    if "|" not in entity:
        return [entity]
    base = re.sub(r"\s*\([^()]*\)\s*$", "", entity).strip()
    return [c.strip() for c in base.split("|") if c.strip()]


def _extract_entities(rows: list[dict]) -> set[str]:
    """Extract unique entity identifiers from extraction rows.

    Uses all available entity fields (canonical, canonical id, raw) so that
    matching against gold standard works regardless of naming convention.
    Pipe-delimited combination groups are expanded into components.
    """
    entities = set()
    for row in rows:
        # Prefer canonical (normalizer output), then canonical id, then raw
        for field in ("entity_canonical", "entity_canonical_id", "entity_raw", "entity"):
            v = row.get(field)
            if v:
                for component in _expand_pipe_delimited(str(v)):
                    entities.add(component)
    return entities


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
    has_canonical_id = 0
    raw_only = 0
    raw_only_examples: list[str] = []

    for row in rows:
        v_raw = row.get("entity_raw")
        v_canon = row.get("entity_canonical")
        v_canon_id = row.get("entity_canonical_id")

        if v_canon:
            has_canonical += 1
        if v_canon_id:
            has_canonical_id += 1
        if v_raw and not v_canon and not v_canon_id:
            raw_only += 1
            if len(raw_only_examples) < 10:
                raw_only_examples.append(str(v_raw))

    return {
        "total_rows": total,
        "has_entity_canonical": has_canonical,
        "has_entity_canonical_id": has_canonical_id,
        "raw_only_no_canonical_no_canonical_id": raw_only,
        "canonical_rate": round(has_canonical / total, 3) if total else 0.0,
        "unnormalized_rate": round(raw_only / total, 3) if total else 0.0,
        "unnormalized_examples": sorted(set(raw_only_examples)),
    }


def _load_schema_field_metadata(schema_dir: Path | None = None) -> dict[str, dict]:
    """Load field metadata from the latest schema version.

    Returns dict of field_name → {"required": bool, "extraction_guidance": str, "removed": bool}.
    Used to split fill rates by required/optional and flag contradictions.

    Without `--schema-dir` there is no domain-agnostic way to locate the schema
    (the old `<domain>` placeholder path never resolved and silently disabled
    schema-contradiction detection — review note). Emit a loud warning and
    return {} rather than guessing.
    """
    if schema_dir is None:
        print(
            "WARNING: schema metadata unavailable — pass --schema-dir to enable "
            "schema-contradiction detection. Skipping (review note).",
            file=sys.stderr,
        )
        return {}

    if not schema_dir.exists():
        print(
            f"WARNING: schema dir {schema_dir} does not exist — skipping "
            f"schema-contradiction detection (review note).",
            file=sys.stderr,
        )
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


def evaluate_paper(
    paper_id: str,
    extraction_path: Path,
    gold_df: pd.DataFrame,
    document_id: int | str,
    resolver: EntityResolver,
    identity_dimensions: list[str] | None = None,
    document_id_column: str = "DocumentID",
    entity_columns: tuple[str, ...] = ("Entity", "entity", "entity_raw"),
) -> PaperEval:
    """Evaluate a single paper's extraction against gold standard.

    Entity matching uses the identity-resolution chain: gold rows and
    extraction rows are each resolved to a canonical token (a canonical id via
    the domain crosswalk, or an inline identifier), and coverage is measured on
    those tokens rather than on raw-string heuristics.
    """
    rows = _load_extraction(extraction_path)
    gold_n = gold_row_count(gold_df, document_id, document_id_column)
    paper_gold = gold_df[gold_df[document_id_column] == document_id]

    # First configured entity column present in the spreadsheet — display name.
    entity_col = next((c for c in entity_columns if c in gold_df.columns), entity_columns[0])

    # Resolve gold rows to canonical tokens; keep a representative name per token.
    gold_token_to_name: dict[str, str] = {}
    gold_unresolved = 0
    for _, grow in paper_gold.iterrows():
        tok, _rung = resolver.resolve_gold(grow)
        if tok:
            gold_token_to_name.setdefault(tok, str(grow.get(entity_col, "")).strip())
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
    entity_coverage = len(matched_keys) / len(gold_tokens) if gold_tokens else 0.0

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
    certainty_profile = _certainty_profile(rows)

    flags = []
    if row_coverage < 0.5:
        flags.append(f"CRITICAL: row coverage {row_coverage:.0%} < 50%")
    elif row_coverage < 0.7:
        flags.append(f"WARNING: row coverage {row_coverage:.0%} < 70%")
    if row_coverage > 1.5:
        # review note (design decision): richer-than-gold extraction is
        # acceptable — the gold may be incomplete. Coverage is still capped at
        # 1.0 for scoring, but this is a neutral review note, not a penalty.
        flags.append(
            f"NOTE: extraction is richer than gold ({row_coverage:.0%} of gold rows) "
            f"— review note, not penalized"
        )
    if missing_keys:
        flags.append(f"Missing {len(missing_keys)} gold entities: {sorted(missing_keys)[:5]}")
    if gold_unresolved:
        flags.append(
            f"ENTITY RESOLUTION: {gold_unresolved} gold row(s) could not be resolved "
            f"to a canonical token (no crosswalk hit, no inline identifier) — "
            f"excluded from entity_coverage denominator."
        )
    if norm_status.get("unnormalized_rate", 0) > 0.2:
        flags.append(
            f"NORMALIZATION: {norm_status['raw_only_no_canonical_no_canonical_id']}/{norm_status['total_rows']} rows "
            f"({norm_status['unnormalized_rate']:.0%}) have entity_raw but no canonical/canonical id. "
            f"Examples: {norm_status['unnormalized_examples'][:5]}"
        )

    return PaperEval(
        paper_id=paper_id,
        document_id=document_id,
        gold_rows=gold_n,
        extracted_rows=len(rows),
        row_coverage=round(row_coverage, 3),
        gold_entities=sorted(gold_vars),
        extracted_entities=sorted(ext_vars),
        matched_entities=sorted(gold_vars_norm[k] for k in matched_keys),
        missing_entities=sorted(gold_vars_norm[k] for k in missing_keys),
        extra_entities=sorted(ext_vars_norm[k] for k in extra_keys),
        entity_coverage=round(entity_coverage, 3),
        field_fill_rates=fill_rates,
        identity_dim_completeness=id_completeness,
        normalization=norm_status,
        certainty_profile=certainty_profile,
        flags=flags,
    )


def evaluate_all(
    extractions_dir: Path,
    gold_standard_path: Path,
    document_id_map_path: Path,
    identity_dimensions: list[str] | None = None,
    schema_dir: Path | None = None,
) -> AggregateEval:
    """Evaluate all extracted papers against gold standard."""
    # Gold-standard schema config (review note); defaults preserve legacy shape.
    eval_config_dir = document_id_map_path.parent
    gold_config = load_gold_config(eval_config_dir)
    doc_col = gold_config.document_id_column

    gold_df = load_gold_standard(gold_standard_path, gold_config=gold_config)
    document_id_map = load_document_id_map(document_id_map_path)
    schema_meta = _load_schema_field_metadata(schema_dir)

    # Build the entity identity resolver once. The crosswalk config (if the
    # domain ships one) lives alongside the document id map in eval_config/.
    crosswalk_config = load_crosswalk_config(eval_config_dir)
    resolver = EntityResolver.build(gold_standard_path, crosswalk_config)
    resolver.entity_fields = tuple(gold_config.entity_columns)
    resolver.extraction_fields = tuple(gold_config.extraction_entity_fields)  # R3-13

    document_id_values = set(document_id_map.values())
    papers = []
    for extraction_file in sorted(extractions_dir.glob("*.jsonl")):
        paper_id = extraction_file.stem
        document_id = document_id_map.get(paper_id)
        if document_id is None:
            # The document id embedded in the filename stem is the most robust
            # key — it survives hyphen/underscore and casing differences in the
            # author portion of the paper_id.
            embedded_document_id = None
            for part in paper_id.split("_"):
                if part.isdigit() and len(part) >= 7:
                    embedded_document_id = int(part)
                    break
            if embedded_document_id is not None and embedded_document_id in document_id_values:
                document_id = embedded_document_id
        if document_id is None:
            # Stem-prefix fallback for paper_ids with no embedded document id.
            for map_key, map_document_id in document_id_map.items():
                if paper_id.lower().startswith(map_key.lower().split()[0].lower()):
                    document_id = map_document_id
                    break
        if document_id is None:
            continue

        # Check if this paper has gold standard data
        if gold_row_count(gold_df, document_id, doc_col) == 0:
            continue

        paper_eval = evaluate_paper(
            paper_id=paper_id,
            extraction_path=extraction_file,
            gold_df=gold_df,
            document_id=document_id,
            resolver=resolver,
            identity_dimensions=identity_dimensions,
            document_id_column=doc_col,
            entity_columns=tuple(gold_config.entity_columns),
        )
        papers.append(paper_eval)

    if not papers:
        return AggregateEval(
            papers=[],
            mean_row_coverage=0.0,
            mean_entity_coverage=0.0,
            mean_finding_accuracy=None,
            total_gold_rows=0,
            total_extracted_rows=0,
            overall_row_coverage=0.0,
            papers_below_50pct=[],
            papers_below_70pct=[],
            certainty_rollup={},
            composite_score=0.0,
        )

    total_gold = sum(p.gold_rows for p in papers)
    total_ext = sum(p.extracted_rows for p in papers)
    # Cap row coverage at 1.0 per paper for composite (over-extraction shouldn't inflate score)
    mean_row_cov = sum(min(p.row_coverage, 1.0) for p in papers) / len(papers)
    mean_var_cov = sum(p.entity_coverage for p in papers) / len(papers)
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

    # Corpus certainty rollup (review note): sum the per-paper profiles.
    certainty_rollup = {
        k: sum(p.certainty_profile.get(k, 0) for p in papers)
        for k in ("clean", "ambiguous", "abstained", "untagged",
                  "high_leverage_uncertain", "high_leverage_total")
    }

    # Aggregate mode: completeness metrics only. finding_accuracy requires detailed
    # mode (the eval agent reasoning about matched rows). Use the single gating
    # composite (review note) in aggregate-only mode (finding_accuracy=None) so
    # this module and the gate never compute different formulas.
    composite = gating_composite(
        row_coverage=mean_row_cov,
        entity_coverage=mean_var_cov,
        field_fill_rate=mean_fill,
        finding_accuracy=None,
    )

    return AggregateEval(
        papers=papers,
        mean_row_coverage=round(mean_row_cov, 3),
        mean_entity_coverage=round(mean_var_cov, 3),
        mean_finding_accuracy=None,  # requires eval agent detailed mode
        total_gold_rows=total_gold,
        total_extracted_rows=total_ext,
        overall_row_coverage=round(total_ext / total_gold, 3) if total_gold else 0.0,
        papers_below_50pct=[p.paper_id for p in papers if p.row_coverage < 0.5],
        papers_below_70pct=[p.paper_id for p in papers if p.row_coverage < 0.7],
        schema_contradictions=schema_contradictions,
        certainty_rollup=certainty_rollup,
        composite_score=composite.composite,
    )


def format_report(eval_result: AggregateEval) -> str:
    """Format eval results as a human-readable report."""
    lines = ["# Aggregate Evaluation Report", ""]
    lines.append(f"Papers evaluated: {len(eval_result.papers)}")
    lines.append(f"Total rows: {eval_result.total_extracted_rows} extracted / {eval_result.total_gold_rows} gold ({eval_result.overall_row_coverage:.0%})")
    lines.append(f"Mean row coverage: {eval_result.mean_row_coverage:.0%}")
    lines.append(f"Mean identity-match coverage: {eval_result.mean_entity_coverage:.0%}")
    lines.append(f"Finding accuracy: NOT EVALUATED (requires eval agent detailed mode)")
    lines.append(f"Aggregate composite: {eval_result.composite_score:.3f} "
                 f"(aggregate-only, capped at 0.50 — excludes finding_accuracy)")
    lines.append("")

    # Certainty rollup (review note)
    cr = eval_result.certainty_rollup
    if cr:
        hl_total = cr.get("high_leverage_total", 0)
        hl_unc = cr.get("high_leverage_uncertain", 0)
        hl_rate = (hl_unc / hl_total) if hl_total else 0.0
        lines.append("## Certainty Profile (corpus)")
        lines.append(
            f"  clean: {cr.get('clean', 0)}  ambiguous: {cr.get('ambiguous', 0)}  "
            f"abstained: {cr.get('abstained', 0)}  untagged: {cr.get('untagged', 0)}"
        )
        lines.append(
            f"  high-leverage uncertain: {hl_unc}/{hl_total} ({hl_rate:.0%}) "
            f"(ambiguous+abstained on {sorted(HIGH_LEVERAGE)})"
        )
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
        lines.append("Papers where >10% of rows have entity_raw but no entity_canonical or entity_canonical_id:")
        lines.append("(This may indicate normalizer instruction gaps, schema contradictions, or intentionally un-normalizable rows)")
        lines.append("")
        for p in sorted(norm_papers, key=lambda x: x.normalization.get("unnormalized_rate", 0), reverse=True):
            n = p.normalization
            lines.append(
                f"  {p.paper_id}: {n['raw_only_no_canonical_no_canonical_id']}/{n['total_rows']} rows "
                f"({n['unnormalized_rate']:.0%}) unnormalized. "
                f"Examples: {n['unnormalized_examples'][:5]}"
            )
    lines.append("")

    lines.append("## Per-Paper Results")
    lines.append("")
    for p in eval_result.papers:
        flag_str = f" {'  '.join(p.flags)}" if p.flags else ""
        lines.append(f"### {p.paper_id} (DocumentID {p.document_id}){flag_str}")
        lines.append(f"  Rows: {p.extracted_rows} / {p.gold_rows} ({p.row_coverage:.0%})")
        lines.append(f"  Entities: {len(p.matched_entities)} matched / {len(p.gold_entities)} gold ({p.entity_coverage:.0%})")
        if p.missing_entities:
            lines.append(f"  Missing entities: {p.missing_entities[:10]}")
        if p.extra_entities:
            lines.append(f"  Extra entities (not in gold): {p.extra_entities[:10]}")
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
    parser.add_argument("--document-id-map", required=True, help="Path to document_id_map.json")
    parser.add_argument("--output", help="Output JSON path")
    parser.add_argument("--identity-dims", nargs="*", help="Identity dimension field names")
    parser.add_argument("--schema-dir", help="Schema directory (enables schema-contradiction detection)")
    args = parser.parse_args()

    result = evaluate_all(
        extractions_dir=Path(args.extractions_dir),
        gold_standard_path=Path(args.gold_standard),
        document_id_map_path=Path(args.document_id_map),
        identity_dimensions=args.identity_dims,
        schema_dir=Path(args.schema_dir) if args.schema_dir else None,
    )

    print(format_report(result))

    if args.output:
        save_report(result, Path(args.output))
        print(f"\nJSON saved to {args.output}")
