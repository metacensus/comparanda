"""Render a reviewer-facing eval explainer.

`score.json` carries the gates and the accuracy decomposition as bare
counters, and the per-finding semantic reasoning lives in a separate file
keyed by integer indexes — a committee reviewer cannot reconstruct *what was
confirmed deterministically vs judged semantically, and how the semantic
matching was reasoned* from any single artifact. This module renders exactly
that: per paper, every deterministic numeric comparison with its outcome,
every gold finding with its semantic score AND the eval agent's reasoning
inline, and the gates in plain language — including whether a gate actually
ENGAGED (a gate that compared nothing is reported as NOT ENGAGED, never as
passed).

Usage (after harness.eval.score has produced score.json):

    uv run python -m harness.eval.render_eval \
        --score iterations/iter_{N}/score.json \
        --extractions-dir <project-root>/extractions \
        --gold-standard benchmark/gold_standard/<gold>.xlsx \
        --document-id-map benchmark/eval_config/document_id_map.json \
        [--finding-scores iterations/iter_{N}/finding_scores.json] \
        --output iterations/iter_{N}/eval_explainer.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness.eval.gold_config import load_adjudications, load_gold_config
from harness.eval.prepare_comparison import prepare_comparison
from harness.eval.value_diff import compute_value_diff


def _gate_lines(score: dict) -> list[str]:
    out: list[str] = []
    dec = score.get("accuracy_decomposition", {})
    compared = dec.get("deterministic_fields_compared", 0)
    mism = score.get("value_mismatch_count", 0)
    vacuous = score.get("value_gate_vacuous", False)

    if vacuous:
        out.append(
            "- **Deterministic value gate: NOT ENGAGED.** Zero numeric fields were "
            "compared against the gold standard — this usually means the "
            "gold_config value_column_map does not match the gold sheet. A gate "
            "that never ran is not a passed gate; convergence is blocked."
        )
    elif score.get("value_gate_passed"):
        out.append(
            f"- **Deterministic value gate: PASSED.** {compared} numeric field "
            f"values were compared cell-against-gold in code; 0 differed beyond "
            f"tolerance or flipped sign."
        )
    else:
        out.append(
            f"- **Deterministic value gate: FAILED.** {mism} of {compared} compared "
            f"numeric values differ from gold beyond tolerance (detail per paper "
            f"below). A non-zero count blocks convergence regardless of score."
        )

    unmatched = score.get("unmatched_gold_with_values", 0)
    if score.get("coverage_gate_passed"):
        out.append(
            "- **Recall gate (100% vs gold): PASSED.** Every gold finding that "
            "reports numeric values has an extraction counterpart."
        )
    else:
        out.append(
            f"- **Recall gate (100% vs gold): FAILED.** {unmatched} gold findings "
            f"with numeric values have no extraction counterpart (listed per paper "
            f"below). Convergence is blocked until they are extracted or "
            f"adjudicated."
        )

    missing = score.get("value_map_columns_missing") or []
    if missing:
        out.append(
            f"- **Configuration warning:** gold_config maps columns that do not "
            f"exist in the gold sheet: {missing}. Values in those columns are "
            f"invisible to the deterministic gate."
        )

    gc = score.get("gold_consistency") or {}
    if gc.get("warning_count"):
        out.append(
            f"- **Gold-standard self-consistency: {gc['warning_count']} warning(s)** "
            f"(e.g. inverted CI bounds in gold itself). These are adjudication "
            f"inputs — the gold standard is corrigible with documented evidence."
        )
    else:
        out.append("- Gold-standard self-consistency: no warnings.")

    adj = score.get("adjudicated_gold_errors", 0)
    if adj:
        out.append(
            f"- {adj} value mismatch(es) were adjudicated as GOLD errors with "
            f"documented evidence and excluded from the gate (see "
            f"benchmark/eval_config/gold_adjudications.json)."
        )
    return out


def _composite_lines(score: dict) -> list[str]:
    comp = score.get("composite_score")
    mode = score.get("composite_mode")
    out = [f"**Composite: {comp}** ({mode} mode)."]
    if mode == "aggregate_only":
        out.append(
            "No semantic accuracy judgment was performed this iteration — the "
            "composite is capped at 0.50 and cannot support convergence."
        )
    src = score.get("finding_accuracy_source")
    fa = score.get("finding_accuracy")
    if src == "per_finding":
        n = score.get("accuracy_decomposition", {}).get("semantic_findings_scored", 0)
        out.append(
            f"finding_accuracy = {fa} was **recomputed in code** from {n} "
            f"per-finding records (one per gold row, each with required "
            f"reasoning) — not self-reported by the judging model."
        )
    elif src == "unvalidated_scalar":
        out.append(
            f"finding_accuracy = {fa} was supplied as a **bare scalar** — a "
            f"degraded mode that cannot support convergence."
        )
    out.append(
        f"Components: row_coverage {score.get('row_coverage')}, "
        f"field_value_recoverability_rate {score.get('field_value_recoverability_rate')}, "
        f"field_fill_rate {score.get('field_fill_rate')}."
    )
    if score.get("convergence_eligible"):
        out.append("This iteration is **convergence-eligible** (all code-checked clauses hold).")
    else:
        out.append("This iteration is **not convergence-eligible**.")
    return out


def _fmt_val(v):
    return "—" if v is None else v


def render_eval(
    score: dict,
    comparisons: dict[str, dict],
    value_diffs: dict[str, dict],
    finding_scores: dict | None,
) -> str:
    out: list[str] = []
    it = score.get("iteration")
    out.append(f"# Eval explainer — iteration {it}")
    out.append("")
    out.append(
        "This document explains the evaluation of the extraction against the "
        "gold standard so a reviewer can judge the methodology rather than "
        "trust an opaque number. Two independent layers produced the figures "
        "below: a **deterministic layer** (numeric values compared in code, "
        "cell against gold — no model judgment) and a **semantic layer** (a "
        "model judged, per gold finding, whether the extraction captures it; "
        "every judgment carries its reasoning, reproduced verbatim here)."
    )
    out.append("")
    out.append("## Gates")
    out.append("")
    out.extend(_gate_lines(score))
    out.append("")
    out.append("## Composite score")
    out.append("")
    out.extend(_composite_lines(score))
    out.append("")

    fs_papers = (finding_scores or {}).get("papers", {})

    for paper_id in score.get("papers_evaluated", []):
        out.append(f"## Paper: {paper_id}")
        out.append("")
        cmp = comparisons.get(paper_id)
        vd = value_diffs.get(paper_id)
        if not cmp or "error" in (cmp or {}):
            out.append(f"- No comparison available ({(cmp or {}).get('error', 'not prepared')}).")
            out.append("")
            continue

        out.append(
            f"{cmp.get('gold_rows')} gold findings · {cmp.get('extraction_rows')} "
            f"extraction rows · {cmp.get('paired_count')} gold findings paired "
            f"deterministically by entity identity."
        )
        out.append("")

        # --- deterministic layer ---
        out.append("### Deterministic numeric comparisons (no model judgment)")
        out.append("")
        mismatches = {(m["gold_index"], m["field"]): m for m in (vd or {}).get("mismatches", [])}
        compared = (vd or {}).get("fields_compared", 0)
        if compared == 0:
            out.append("*No numeric fields were comparable for this paper.*")
        else:
            out.append("| Gold finding | Field | Gold value | Extracted value | Status |")
            out.append("|---|---|---|---|---|")
            # Re-derive the per-pair comparisons for display from mismatches +
            # the paired rows (matches are implicit: compared - mismatched).
            for m in (vd or {}).get("mismatches", []):
                out.append(
                    f"| #{m['gold_index']} | {m['field']} | {m['gold_value']} | "
                    f"{m['extraction_value']} | **MISMATCH** (Δrel {m['rel_diff']}"
                    + (", sign flip" if m.get("sign_flip") else "") + ") |"
                )
            ok = compared - len(mismatches)
            out.append(
                f"\n{ok} of {compared} compared values matched gold within "
                f"tolerance; mismatches (if any) are listed above."
            )
        for u in (vd or {}).get("unmatched_gold_detail", []):
            out.append(
                f"- **Unmatched gold finding #{u['gold_index']}** — reports numeric "
                f"values ({', '.join(u['fields_with_values'])}) but paired to no "
                f"extraction row (recall-gate item)."
            )
        for adj in (vd or {}).get("adjudicated_gold_errors", []):
            out.append(
                f"- Gold finding #{adj['gold_index']} field {adj['field']}: mismatch "
                f"adjudicated as a GOLD error — {adj['adjudication'].get('note', '')}"
            )
        for amb in (vd or {}).get("ambiguous_pairings", []):
            out.append(
                f"- Ambiguous pairing at gold finding #{amb['gold_index']}: candidates "
                f"{amb['candidate_indices']} are all within tolerance — the semantic "
                f"layer adjudicates."
            )
        out.append("")

        # --- semantic layer ---
        findings = (fs_papers.get(paper_id) or {}).get("findings") or []
        out.append("### Semantic judgments (model-scored, reasoning verbatim)")
        out.append("")
        if not findings:
            out.append("*No per-finding semantic scores for this paper this iteration.*")
        paired_by_idx = {p["gold_index"]: p for p in cmp.get("paired_rows", [])}
        for f in findings:
            gi = f.get("gold_index")
            gold_entity = (paired_by_idx.get(gi) or {}).get("gold_entity", "?")
            score_str = (
                f"completeness {f.get('completeness')} × accuracy {f.get('accuracy')}"
            )
            target = (
                f"extraction row {f.get('extraction_index')}"
                if f.get("extraction_index") is not None else "no extraction row"
            )
            out.append(f"- **Gold finding #{gi}** ({gold_entity}) — {score_str} → {target}")
            out.append(f"  - Reasoning: {f.get('reasoning')}")
        out.append("")

    out.append("---")
    out.append(
        "*Generated by harness.eval.render_eval. The deterministic layer is "
        "reproducible from the extraction files and the gold sheet; the semantic "
        "layer's reasoning is the eval model's own, recorded at scoring time and "
        "validated for coverage (one record per gold row) in code.*"
    )
    out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a reviewer-facing eval explainer")
    ap.add_argument("--score", required=True, help="path to score.json")
    ap.add_argument("--extractions-dir", required=True)
    ap.add_argument("--gold-standard", required=True)
    ap.add_argument("--document-id-map", required=True)
    ap.add_argument("--finding-scores", default=None)
    ap.add_argument("--rel-tol", type=float, default=0.05)
    ap.add_argument("--output", default=None)
    a = ap.parse_args()

    score = json.loads(Path(a.score).read_text())
    eval_config_dir = Path(a.document_id_map).parent
    gold_config = load_gold_config(eval_config_dir)
    adjudications = load_adjudications(eval_config_dir)

    comparisons: dict[str, dict] = {}
    value_diffs: dict[str, dict] = {}
    for paper_id in score.get("papers_evaluated", []):
        cmp = prepare_comparison(
            paper_id, Path(a.extractions_dir), Path(a.gold_standard),
            Path(a.document_id_map),
        )
        comparisons[paper_id] = cmp
        if "error" not in cmp:
            value_diffs[paper_id] = compute_value_diff(
                cmp, gold_config=gold_config, rel_tol=a.rel_tol,
                adjudications=adjudications,
            )

    finding_scores = (
        json.loads(Path(a.finding_scores).read_text()) if a.finding_scores else None
    )
    text = render_eval(score, comparisons, value_diffs, finding_scores)
    if a.output:
        out = Path(a.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"[render_eval] wrote {out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
