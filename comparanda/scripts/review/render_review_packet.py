#!/usr/bin/env python3
"""Render a committee-reviewer-facing review packet from an extraction.

The extraction JSONL already carries everything a reviewer needs — verbatim
quotes, bundle locations, page numbers, cell anchors, derivations, reasoning,
certainty tags, abstentions, review decisions — but as a machine-shaped dict.
This renders it as a per-paper Markdown packet in which every claim is
self-explaining: *what it claims*, *the evidence in context* (quote + which
table, which row/column, which section, which page), and *the reasoning that
connects evidence to claim*. A reviewer who has never seen the codebase can
verify any value against the cited cell without opening the JSONL.

Usage:
  python3 scripts/review/render_review_packet.py \
      --extractions-dir projects/<d>/extractions --paper-id <pid> \
      [--bundles-dir projects/<d>/data/processed] \
      [--normalization-dir projects/<d>/normalization] \
      [--schema projects/<d>/schema/vNNN.yaml] \
      [--output projects/<d>/review_packets/<pid>.md]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import review_queue as rq  # noqa: E402

CERTAINTY_BADGE = {
    "clean": "clean",
    "ambiguous": "AMBIGUOUS — needs review",
    "abstained": "ABSTAINED — pipeline could not read this value",
    "reviewed_rejected": "REJECTED by reviewer",
}

MISSING_LABEL = {
    "not_found": "Not found in supplied material",
    "explicitly_unavailable": "Explicitly not reported / not measured",
    "referenced_source_unavailable": "Referenced source unavailable",
    "definition_mismatch": "Available information does not fit this field",
    "unreadable": "Source value unreadable",
    "conflicting_evidence": "Conflicting evidence",
    "not_applicable": "Not applicable",
    None: "Missing — reason not categorized",
}


def _load_rows(jsonl_path: Path) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    meta: dict = {}
    for line in jsonl_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("_meta_commentary"):
            meta = obj
        else:
            rows.append(obj)
    return rows, meta


def _table_context(bundles_dir: Path | None, paper_id: str) -> dict[str, dict]:
    """table file stem -> {title, validation} from the bundle manifest."""
    out: dict[str, dict] = {}
    if bundles_dir is None:
        return out
    manifest = bundles_dir / paper_id / "manifest.json"
    if not manifest.exists():
        return out
    try:
        m = json.loads(manifest.read_text())
    except json.JSONDecodeError:
        return out
    for t in m.get("tables", []):
        stem = f"table_{t.get('id')}"
        out[stem] = {"title": t.get("title"), "validation": t.get("validation")}
    return out


def _identity_fields(meta: dict, schema_path: Path | None) -> list[str]:
    idv = meta.get("identity_dimension_values")
    if isinstance(idv, dict) and idv:
        return list(idv)
    if schema_path is not None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extraction"))
            import extraction_validator as ev  # noqa: E402
            return list(ev.Schema.load(schema_path).identity_dimensions)
        except Exception:
            pass
    return []


def _fmt_location(src: dict, tables: dict[str, dict]) -> str:
    loc = src.get("location", "?")
    parts = [f"`{loc}`"]
    stem = Path(str(loc)).stem
    ctx = tables.get(stem)
    if ctx:
        if ctx.get("title"):
            parts.append(f"(“{ctx['title']}”)")
        if ctx.get("validation") and ctx["validation"] != "corroborated":
            parts.append(f"[table is {ctx['validation']} — uncorroborated]")
    if src.get("page") is not None:
        parts.append(f"p.{src['page']}")
    cell = src.get("cell")
    if isinstance(cell, dict):
        parts.append(f"— row “{cell.get('row')}” × column “{cell.get('column')}”")
    return " ".join(parts)


def _render_field(name: str, value, entry: dict, tier: str | None,
                  tables: dict[str, dict]) -> list[str]:
    lines: list[str] = []
    certainty = entry.get("extraction_certainty")
    kind = entry.get("derivation_kind") or ("derived" if entry.get("type") == "derived" else "verbatim")

    if value is None and "missing_reason" in entry:
        reason = entry["missing_reason"]
        label = MISSING_LABEL.get(reason, f"Missing — {reason}")
        head = f"- **{name} — {label}**"
    elif certainty == "abstained":
        head = f"- **{name} — ABSTAINED** (the paper reports a value the pipeline could not read reliably; routed to human review)"
    else:
        badge_bits = [kind]
        if certainty:
            badge_bits.append(CERTAINTY_BADGE.get(certainty, certainty))
        head = f"- **{name} = {json.dumps(value)}**  — {', '.join(badge_bits)}"
    if tier:
        head += f"  *(review tier: {tier})*"
    lines.append(head)

    for src in entry.get("sources") or []:
        if not isinstance(src, dict):
            continue
        quote = src.get("quote")
        where = _fmt_location(src, tables)
        if quote:
            lines.append(f"  - Evidence: “{quote}” — {where}")
        else:
            lines.append(f"  - Location of unreadable value: {where}")
    if entry.get("derivation"):
        lines.append(f"  - Derivation: `{entry['derivation']}`")
    if entry.get("reasoning"):
        lines.append(f"  - Reasoning: {entry['reasoning']}")
    if entry.get("reviewed_by"):
        lines.append(
            f"  - Human review: **{entry.get('review_action')}** by "
            f"{entry['reviewed_by']} at {entry.get('reviewed_at')} — "
            f"“{entry.get('review_note')}”"
        )
        if entry.get("review_superseded"):
            lines.append(
                f"    - Superseded value: {json.dumps(entry['review_superseded'].get('old_value'))}"
            )
        for h in entry.get("review_history") or []:
            lines.append(
                f"    - Earlier review: {h.get('review_action')} by {h.get('reviewed_by')} "
                f"at {h.get('reviewed_at')} — “{h.get('review_note')}”"
            )
    return lines


def render_packet(
    extractions_dir: Path,
    paper_id: str,
    bundles_dir: Path | None = None,
    normalization_dir: Path | None = None,
    schema_path: Path | None = None,
) -> str:
    jsonl_path = extractions_dir / f"{paper_id}.jsonl"
    rows, meta = _load_rows(jsonl_path)
    tables = _table_context(bundles_dir, paper_id)
    id_fields = _identity_fields(meta, schema_path)

    # Per-(row, field) risk tier from the same triage the review CLI uses.
    identity_fields = tuple(id_fields) if id_fields else rq.DEFAULT_IDENTITY_FIELDS
    tier_by_rowfield: dict[tuple[int, str], str] = {}
    for item in rq.build_queue(extractions_dir, bundles_dir,
                               identity_fields=identity_fields,
                               normalization_dir=normalization_dir):
        if item["paper_id"] == paper_id:
            tier_by_rowfield[(item["row_index"], item["field"])] = item["risk_tier"]

    out: list[str] = []
    out.append(f"# Review packet — {paper_id}")
    out.append("")
    out.append(
        f"{len(rows)} extracted rows · schema {meta.get('schema_version', '?')} · "
        f"generated from `{jsonl_path.name}`"
    )
    out.append("")
    out.append("## How to read this packet")
    out.append("")
    out.append(
        "Each row below is one atomic finding, identified by its identity tuple "
        "(the heading). Every non-null value shows: the **claim** (field = value), "
        "the **evidence in context** (a verbatim quote plus the bundle file, page, "
        "and — for table values — the row label and column header the value sits "
        "under), and, wherever the value was computed, read from a figure, "
        "uncertain, or assembled from multiple sources, the **reasoning** that "
        "connects the evidence to the claim. Values marked *verbatim, clean* were "
        "transcribed directly from a cross-validated source; the cited cell is the "
        "complete justification. **AMBIGUOUS**/**ABSTAINED** values are queued for "
        "human review. New missing values show their absence reason, explanation "
        "and available evidence rather than a transcribed-value label. Legacy "
        "nulls without provenance remain listed separately."
    )
    out.append("")

    for i, row in enumerate(rows):
        prov = row.get("provenance") or {}
        identity = {f: row.get(f) for f in id_fields if f in row} if id_fields else {}
        ident_str = " × ".join(f"{k}={json.dumps(v)}" for k, v in identity.items()) or f"row {i}"
        out.append(f"## Row {i} — {ident_str}")
        out.append("")

        nulls: list[str] = []
        for name, value in row.items():
            if name in ("provenance", "_meta_commentary"):
                continue
            entry = prov.get(name) if isinstance(prov.get(name), dict) else None
            if value is None and entry is None:
                nulls.append(name)
                continue
            tier = tier_by_rowfield.get((i, name))
            out.extend(_render_field(name, value, entry or {}, tier, tables))
        if nulls:
            out.append(f"- *Not reported by the paper:* {', '.join(sorted(nulls))}")
        out.append("")

    out.append("## Paper-level extraction notes")
    out.append("")
    if meta.get("estimated_completeness") is not None:
        out.append(f"- Extractor's estimated completeness: {meta['estimated_completeness']}")
    for su in meta.get("sources_used") or []:
        out.append(f"- Source used: `{su.get('source')}` ({su.get('role')})")
    for sn in meta.get("sources_not_used") or []:
        out.append(f"- Source NOT used: `{sn.get('source')}` — {sn.get('reason')}")
    for gap in meta.get("schema_vocabulary_gaps") or []:
        out.append(f"- **Schema vocabulary gap:** {json.dumps(gap)}")
    for amb in meta.get("ambiguities") or []:
        out.append(f"- Ambiguity noted by extractor: {amb}")
    for cc in meta.get("confidence_concerns") or []:
        out.append(f"- Confidence concern: {cc}")
    if meta.get("gap_explanation"):
        out.append(f"- Cartesian-gap explanation: {meta['gap_explanation']}")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a reviewer-facing packet for one paper")
    ap.add_argument("--extractions-dir", required=True)
    ap.add_argument("--paper-id", required=True)
    ap.add_argument("--bundles-dir", default=None)
    ap.add_argument("--normalization-dir", default=None)
    ap.add_argument("--schema", default=None)
    ap.add_argument("--output", default=None, help="output .md path (default: stdout)")
    a = ap.parse_args()
    text = render_packet(
        Path(a.extractions_dir), a.paper_id,
        bundles_dir=Path(a.bundles_dir) if a.bundles_dir else None,
        normalization_dir=Path(a.normalization_dir) if a.normalization_dir else None,
        schema_path=Path(a.schema) if a.schema else None,
    )
    if a.output:
        out = Path(a.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"[render_review_packet] wrote {out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
