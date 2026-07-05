#!/usr/bin/env python3
"""Apply human review decisions to extraction rows (review note).

`review_queue.py` triages which (paper, row, field) values a human should
check; this is the apply side — the backend interface a reviewer (today a CLI,
later a web UI calling the same operations) uses to record a decision. A
decision writes `reviewed_by` / `reviewed_at` / `review_action` / `review_note`
into the field's provenance entry, and `write_extraction.py itern`'s durability
guard then protects that entry from being clobbered on re-extraction (it skips
any field whose prior provenance carries `reviewed_by`).

Three actions, each an explicit, audited state:
  - confirmed : the value is correct as extracted (no value change).
  - corrected : the value is wrong; supply --new-value (and optionally a
                corrected quote/location/page). The old value is preserved in
                `review_superseded`.
  - rejected  : the value cannot be supported; the field is set to null with a
                reviewed abstention-style note (distinct from "not reported").

Subcommands:
  list   — render outstanding queue items (claim + located evidence + reasoning)
  apply  — record one decision on one (paper, row_index, field)
  status — outstanding tier-0/1 high-leverage counts (the Phase-6 gate input)

Every reviewer note is required: a correction with no rationale is not auditable.

Usage:
  python3 scripts/review/apply_review.py list   --extractions-dir projects/<d>/extractions \
      --bundles-dir projects/<d>/data/processed [--schema projects/<d>/schema/vNNN.yaml] [--tier 0,1]
  python3 scripts/review/apply_review.py apply  --extractions-dir projects/<d>/extractions \
      --paper-id <pid> --row-index <i> --field effect_estimate \
      --action corrected --new-value 0.42 \
      --quote "0.42 (0.20, 0.64)" --location tables/table_2.json --page 7 \
      --reviewer "jdoe" --note "Table 2 row 'Tutoring' col 'ES'; extractor read the control column."
  python3 scripts/review/apply_review.py status --extractions-dir projects/<d>/extractions \
      --bundles-dir projects/<d>/data/processed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# review_queue lives alongside this script.
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import review_queue as rq  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_value(raw: str):
    """Best-effort scalar coercion for a corrected value (int, float, else str)."""
    for caster in (int, float):
        try:
            return caster(raw)
        except (ValueError, TypeError):
            continue
    if raw.lower() in {"null", "none"}:
        return None
    return raw


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_list(a: argparse.Namespace) -> int:
    identity_fields = (
        rq._identity_fields_from_schema(Path(a.schema))
        if getattr(a, "schema", None) else rq.DEFAULT_IDENTITY_FIELDS
    )
    items = rq.build_queue(
        Path(a.extractions_dir),
        Path(a.bundles_dir) if a.bundles_dir else None,
        identity_fields=identity_fields,
    )
    tiers = set(int(t) for t in a.tier.split(",")) if a.tier else None
    shown = [it for it in items if tiers is None or it["rank"] in tiers]
    for it in shown:
        src = (it.get("sources") or [{}])[0]
        cell = it.get("cell")
        loc = src.get("location", "?")
        where = f"{loc}"
        if cell:
            where += f" [row '{cell.get('row')}' × col '{cell.get('column')}']"
        print(f"[{it['risk_tier']:<20}] {it['paper_id']} row {it['row_index']} "
              f"{it['field']} = {it['value']!r}")
        print(f"    claim:    {it['field']} = {it['value']!r}  ({it.get('identity', {})})")
        print(f"    evidence: \"{src.get('quote', '')}\"  @ {where} p{src.get('page', '?')}")
        if it.get("reasoning"):
            print(f"    reasoning: {it['reasoning']}")
    print(f"\n{len(shown)} item(s)"
          + (f" (tiers {sorted(tiers)})" if tiers else "")
          + f"; {len(items)} total in queue")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    identity_fields = (
        rq._identity_fields_from_schema(Path(a.schema))
        if getattr(a, "schema", None) else rq.DEFAULT_IDENTITY_FIELDS
    )
    items = rq.build_queue(
        Path(a.extractions_dir),
        Path(a.bundles_dir) if a.bundles_dir else None,
        identity_fields=identity_fields,
    )
    by_tier: dict[str, int] = {}
    hi_blocking = 0
    for it in items:
        by_tier[it["risk_tier"]] = by_tier.get(it["risk_tier"], 0) + 1
        if it["rank"] in (0, 1) and it["high_leverage"]:
            hi_blocking += 1
    out = {
        "total_outstanding": len(items),
        "by_tier": by_tier,
        "phase6_gate_outstanding": hi_blocking,
        "phase6_gate_passed": hi_blocking == 0,
        "note": ("Phase-6 gate: tier-0 (abstained) and tier-1 (ambiguous) "
                 "high-leverage items must be reviewed or waived before the "
                 "final commit (review note)."),
    }
    print(json.dumps(out, indent=2))
    # R3-02: the gate must be mechanically enforceable. A failing gate exits
    # non-zero so scripts/CI/orchestrators can actually block on it; pass
    # --informational to restore the old always-0 behavior for dashboards.
    if a.informational:
        return 0
    return 0 if hi_blocking == 0 else 1


def _validate_corrected_source(a: argparse.Namespace) -> str | None:
    """Validate a reviewer-supplied quote/location against the bundle.

    Human-entered provenance was the one class that bypassed the validator.
    Returns an error string, or None when the source checks out (or when no
    bundle is available to check against)."""
    if not (a.quote and a.location):
        return None
    if not getattr(a, "bundles_dir", None):
        return None  # no bundle supplied — caller opted out of validation
    bundle_dir = Path(a.bundles_dir) / a.paper_id
    if not bundle_dir.exists():
        return f"bundle dir {bundle_dir} not found (drop --bundles-dir to skip validation)"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extraction"))
    import extraction_validator as ev  # noqa: E402

    if ev.FIGURE_LOCATION_PATTERN.search(a.location):
        return None  # figure-located: exempt from verbatim check, same as extractor
    bundle = ev.BundleSources.load(bundle_dir)
    relpath, text = bundle.resolve_location(a.location)
    if relpath is None:
        return (f"--location {a.location!r} does not name a real bundle file "
                f"(use sections/<name>.md or tables/table_<id>.json/.md)")
    if not ev.find_quote_in_source(a.quote, text or ""):
        return (f"--quote not found verbatim at {relpath} — corrected provenance "
                f"must meet the same standard as extracted provenance")
    return None


def cmd_apply(a: argparse.Namespace) -> int:
    if not a.note or not a.note.strip():
        print("error: --note is required (a decision with no rationale is not "
              "auditable)", file=sys.stderr)
        return 2
    jf = Path(a.extractions_dir) / f"{a.paper_id}.jsonl"
    if not jf.exists():
        print(f"error: no extraction file at {jf}", file=sys.stderr)
        return 2
    rows = _read_jsonl(jf)
    data_rows = [r for r in rows if not r.get("_meta_commentary")]
    if not (0 <= a.row_index < len(data_rows)):
        print(f"error: row_index {a.row_index} out of range (0..{len(data_rows)-1})",
              file=sys.stderr)
        return 2
    row = data_rows[a.row_index]
    if a.field not in row:
        print(f"error: field {a.field!r} not present on row {a.row_index}",
              file=sys.stderr)
        return 2

    # R3-06: positional addressing is fragile across re-extractions. When the
    # reviewer supplies the identity tuple they saw in the queue item, verify
    # it against the addressed row and refuse on mismatch — a correction
    # landing on the wrong row is itself a silent wrong value.
    if a.expect_identity:
        try:
            expected = json.loads(a.expect_identity)
        except json.JSONDecodeError as e:
            print(f"error: --expect-identity is not valid JSON: {e}", file=sys.stderr)
            return 2
        mismatches = {
            k: {"expected": v, "actual": row.get(k)}
            for k, v in expected.items() if row.get(k) != v
        }
        if mismatches:
            print("error: identity mismatch — row at this index is not the row "
                  f"the reviewer saw (rows may have been re-extracted/reordered): "
                  f"{json.dumps(mismatches)}", file=sys.stderr)
            return 2

    if a.action == "corrected":
        err = _validate_corrected_source(a)
        if err:
            print(f"error: corrected source failed validation: {err}", file=sys.stderr)
            return 2

    prov = row.setdefault("provenance", {})
    prior = prov.get(a.field, {}) if isinstance(prov.get(a.field), dict) else {}
    review_meta = {
        "reviewed_by": a.reviewer,
        "reviewed_at": _now(),
        "review_action": a.action,
        "review_note": a.note,
    }
    # R3-06: re-reviewing a field must not destroy the prior review — the
    # disagreement trail IS the committee-relevant signal. Push the prior
    # review's metadata onto review_history before it is superseded.
    if prior.get("reviewed_by"):
        review_meta["review_history"] = (prior.get("review_history") or []) + [{
            k: prior.get(k)
            for k in ("reviewed_by", "reviewed_at", "review_action", "review_note")
        }]

    if a.action == "confirmed":
        entry = dict(prior)
        entry.update(review_meta)
        prov[a.field] = entry

    elif a.action == "corrected":
        if a.new_value is None:
            print("error: --new-value is required for --action corrected",
                  file=sys.stderr)
            return 2
        old_value = row.get(a.field)
        new_value = _coerce_value(a.new_value)
        row[a.field] = new_value
        sources = prior.get("sources", [])
        if a.quote or a.location:
            src = {"quote": a.quote, "location": a.location}
            if a.page is not None:
                src["page"] = a.page
            if a.cell_row or a.cell_column:
                src["cell"] = {"row": a.cell_row, "column": a.cell_column}
            sources = [src]
        entry = {
            "type": prior.get("type", "paper"),
            "sources": sources,
            "review_superseded": {"old_value": old_value,
                                  "old_provenance": prior or None},
            **review_meta,
        }
        prov[a.field] = entry

    elif a.action == "rejected":
        old_value = row.get(a.field)
        row[a.field] = None
        prov[a.field] = {
            "extraction_certainty": "reviewed_rejected",
            "sources": prior.get("sources", []),
            "review_superseded": {"old_value": old_value,
                                  "old_provenance": prior or None},
            **review_meta,
        }

    else:
        print(f"error: unknown action {a.action!r}", file=sys.stderr)
        return 2

    _atomic_write_jsonl(jf, rows)
    print(f"[apply_review] {a.action} {a.paper_id} row {a.row_index} {a.field} "
          f"by {a.reviewer} → {jf}", file=sys.stderr)
    print(json.dumps({"paper_id": a.paper_id, "row_index": a.row_index,
                      "field": a.field, "action": a.action,
                      "new_value": row.get(a.field)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="apply_review.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="render outstanding queue items")
    pl.add_argument("--extractions-dir", required=True)
    pl.add_argument("--bundles-dir", default=None)
    pl.add_argument("--schema", default=None)
    pl.add_argument("--tier", default=None, help="comma-separated ranks, e.g. 0,1")
    pl.set_defaults(handler=cmd_list)

    ps = sub.add_parser("status", help="Phase-6 gate counts")
    ps.add_argument("--extractions-dir", required=True)
    ps.add_argument("--bundles-dir", default=None)
    ps.add_argument("--schema", default=None)
    ps.add_argument("--informational", action="store_true",
                    help="always exit 0 (default exits 1 when the Phase-6 "
                         "gate is failing — R3-02)")
    ps.set_defaults(handler=cmd_status)

    pa = sub.add_parser("apply", help="record one review decision")
    pa.add_argument("--extractions-dir", required=True)
    pa.add_argument("--paper-id", required=True)
    pa.add_argument("--row-index", type=int, required=True)
    pa.add_argument("--field", required=True)
    pa.add_argument("--action", required=True,
                    choices=["confirmed", "corrected", "rejected"])
    pa.add_argument("--new-value", default=None,
                    help="required for --action corrected")
    pa.add_argument("--quote", default=None)
    pa.add_argument("--location", default=None)
    pa.add_argument("--page", type=int, default=None)
    pa.add_argument("--cell-row", default=None)
    pa.add_argument("--cell-column", default=None)
    pa.add_argument("--reviewer", required=True)
    pa.add_argument("--note", required=True)
    pa.add_argument("--expect-identity", default=None,
                    help="JSON object of identity-field values as shown in the "
                         "queue item; apply refuses if the addressed row does "
                         "not match (R3-06 — protects against index drift)")
    pa.add_argument("--bundles-dir", default=None,
                    help="when given, a corrected --quote/--location is "
                         "validated against the paper's bundle with the same "
                         "machinery as extracted provenance")
    pa.set_defaults(handler=cmd_apply)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
