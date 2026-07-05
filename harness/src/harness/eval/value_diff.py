"""Deterministic value-against-gold comparison (review note / R2-03).

The eval composite was 70% structural counting; the only value-correctness
signal was a selectively-run LLM judge. This module adds an always-on,
deterministic numeric diff: for every gold row that reports a numeric
effect/precision/count, it finds the matching extraction candidate and flags
any value that differs beyond tolerance (or flips sign). The mismatch count
feeds `composite.gating_composite` as a hard convergence gate.

It consumes the output of `prepare_comparison.prepare_comparison` (gold rows
paired to extraction candidates by canonical entity token) so it reuses the
existing deterministic identity resolution rather than re-implementing it.

R2-03 hardening (the value gate was satisfiable *silently*):

  * **100% recall gate** — `unmatched_gold_with_values`: gold rows that carry a
    mapped numeric value but paired to *no* extraction candidate. Previously
    these were skipped ("a coverage gap, not a value error"), so a clean
    `mismatch_count` was fully compatible with the extraction having missed
    every numeric finding. This count is now emitted as a second hard input to
    the convergence gate: convergence is impossible while any gold finding
    lacks an extraction counterpart.
  * **Ambiguous pairings** — `ambiguous_pairings`: pairs where more than one
    candidate's effect estimate fell within tolerance of the gold effect, so
    "nearest effect" could not uniquely disambiguate. The eval agent must
    adjudicate these rather than trust the nearest-effect heuristic.
  * **Adjudication path** — mismatches matching a `gold_error` entry in
    `eval_config/gold_adjudications.json` are excluded from `mismatch_count`
    and reported separately in `adjudicated_gold_errors` with their evidence
    attached, so the gate stays hard while the gold standard stays corrigible.
  * **Config-driven matching** — the column map, the gold effect/outcome column
    names, and the stopword list now come from `GoldConfig`, not hardcoded
    literals. Defaults ship generic English stopwords only, so an education
    corpus's "math posttest" vs "reading posttest" no longer collapse to ∅.

Scope/limits (documented honestly): pairing is by entity token, then by
nearest effect size. It catches a transposed digit / wrong CI bound / sign
flip when the extraction's value for that finding is clearly different. A clean
diff is necessary-but-not-sufficient — it is the deterministic floor under the
judge, not a replacement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from harness.eval.gold_config import GoldConfig


# Legacy default gold-column -> core-field map, retained so callers that pass
# neither gold_config nor column_map preserve the original behavior.
DEFAULT_COLUMN_MAP = {
    "Effect Size": "effect_estimate",
    "Lower 95 CI": "ci_lower",
    "Upper 95 CI": "ci_upper",
    "Subjects with Entity": "n_total",
}

FIELD_ALIASES = {
    "effect_estimate": ("effect_estimate", "effect_size", "effect_size_value"),
    "effect_size": ("effect_size", "effect_estimate", "effect_size_value"),
    "effect_size_value": ("effect_size_value", "effect_estimate", "effect_size"),
    "ci_lower": ("ci_lower", "effect_size_ci_lower"),
    "effect_size_ci_lower": ("effect_size_ci_lower", "ci_lower"),
    "ci_upper": ("ci_upper", "effect_size_ci_upper"),
    "effect_size_ci_upper": ("effect_size_ci_upper", "ci_upper"),
    "n_total": (
        "n_total",
        "total_n",
        "group_n",
        "reference_n",
    ),
    "n_events": (
        "n_events",
        "group_events",
        "reference_events",
    ),
}

SIGN_SENSITIVE = {
    "effect_estimate", "effect_size", "effect_size_value",
    "ci_lower", "effect_size_ci_lower",
    "ci_upper", "effect_size_ci_upper",
}

_EFFECT_FIELD = "effect_estimate"


def _outcome_tokens(text, stopwords: frozenset[str]) -> set[str]:
    import re
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", str(text).lower())
    return {w for w in words if w not in stopwords and len(w) > 2}


@dataclass
class ValueMismatch:
    gold_index: int
    field: str
    gold_value: float
    extraction_value: float
    rel_diff: float
    sign_flip: bool
    extraction_index: int | None


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(str(v).strip().replace(",", ""))
    except (ValueError, TypeError):
        return None
    # pandas NaN
    if f != f:
        return None
    return f


def _candidate_value(candidate: dict, field: str):
    """Read a configured field with generic aliases before falling back to None."""
    for name in FIELD_ALIASES.get(field, (field,)):
        if name in candidate and candidate.get(name) is not None:
            return candidate.get(name)
    return None


def _narrow_candidates(
    candidates: list[dict],
    gold_outcome_tokens: set[str] | None,
    stopwords: frozenset[str],
) -> list[dict]:
    """Narrow candidates to those whose outcome text overlaps the gold outcome."""
    if not gold_outcome_tokens:
        return candidates
    narrowed = [
        c for c in candidates
        if _outcome_tokens(c.get("outcome_raw"), stopwords) & gold_outcome_tokens
        or _outcome_tokens(c.get("outcome_id"), stopwords) & gold_outcome_tokens
        or _outcome_tokens(c.get("outcome_canonical"), stopwords) & gold_outcome_tokens
    ]
    return narrowed or candidates


def _narrow_by_identity_pairs(
    candidates: list[dict],
    gold: dict,
    narrow_pairs: tuple[tuple[str, str], ...],
    stopwords: frozenset[str],
) -> list[dict]:
    """Sequentially soft-narrow candidates by configured identity pairs.

    Each (gold_column, extraction_field) pair keeps candidates whose
    extraction_field text token-overlaps the gold column's text. A pair that
    would empty the pool is skipped (soft) — narrowing must never *create*
    a no-candidate state, only sharpen selection. This pins the compared
    candidate by identity (timepoint, operation, metric, subgroup) instead of
    leaving disambiguation to value proximity.
    """
    pool = candidates
    for gcol, efield in narrow_pairs:
        gtok = _outcome_tokens(gold.get(gcol), stopwords)
        if not gtok:
            continue
        kept = [c for c in pool if _outcome_tokens(c.get(efield), stopwords) & gtok]
        if kept:
            pool = kept
    return pool


def _best_candidate(
    gold_effect: float | None,
    candidates: list[dict],
    gold_outcome_tokens: set[str] | None,
    stopwords: frozenset[str],
) -> dict | None:
    """Pick the candidate that represents this finding.

    Narrow by outcome-token overlap, then pick the candidate whose
    effect_estimate is nearest the gold effect (review note)."""
    if not candidates:
        return None
    pool = _narrow_candidates(candidates, gold_outcome_tokens, stopwords)
    if gold_effect is None:
        return pool[0]
    best, best_d = None, None
    for c in pool:
        ce = _to_float(_candidate_value(c, _EFFECT_FIELD))
        if ce is None:
            continue
        d = abs(ce - gold_effect)
        if best_d is None or d < best_d:
            best, best_d = c, d
    return best if best is not None else pool[0]


def _ambiguous_within_tol(
    gold_effect: float | None,
    candidates: list[dict],
    gold_outcome_tokens: set[str] | None,
    stopwords: frozenset[str],
    rel_tol: float,
) -> list[int]:
    """Candidate extraction_index list whose effect is within rel_tol of gold.

    Returns the indices only when MORE than one candidate qualifies — i.e.
    nearest-effect could not uniquely disambiguate this pairing."""
    if gold_effect is None:
        return []
    pool = _narrow_candidates(candidates, gold_outcome_tokens, stopwords)
    denom = max(abs(gold_effect), 1e-9)
    within = []
    for c in pool:
        ce = _to_float(_candidate_value(c, _EFFECT_FIELD))
        if ce is None:
            continue
        if abs(ce - gold_effect) / denom <= rel_tol:
            within.append(c.get("extraction_index"))
    return within if len(within) > 1 else []


def _is_adjudicated_gold_error(
    adjudications: list[dict] | None,
    document_id,
    gold_index: int,
    *fields: str,
) -> dict | None:
    """Return the matching gold_error adjudication entry, or None."""
    if not adjudications:
        return None
    field_set = {f for f in fields if f}
    for adj in adjudications:
        if adj.get("resolution") != "gold_error":
            continue
        if (
            adj.get("document_id") == document_id
            and adj.get("gold_index") == gold_index
            and adj.get("field") in field_set
        ):
            return adj
    return None


def compute_value_diff(
    comparison: dict,
    *,
    gold_config: GoldConfig | None = None,
    column_map: dict[str, str] | None = None,
    rel_tol: float = 0.05,
    adjudications: list[dict] | None = None,
) -> dict:
    """Deterministic numeric diff of paired gold vs extraction values.

    Returns a dict with the mismatch gate plus the R2-03 recall/ambiguity/
    adjudication signals (see module docstring and the returned `note`).

    `column_map` (if given) overrides the config's value_column_map for
    back-compat; otherwise the map comes from `gold_config` (or the legacy
    default when neither is supplied).
    """
    cfg = gold_config or GoldConfig()
    cmap = column_map or cfg.value_column_map
    stopwords = cfg.stopwords
    effect_col = cfg.gold_effect_column
    outcome_cols = cfg.outcome_text_columns
    document_id = comparison.get("document_id")

    mismatches: list[ValueMismatch] = []
    adjudicated_gold_errors: list[dict] = []
    ambiguous_pairings: list[dict] = []
    unmatched_detail: list[dict] = []
    unmatched_with_values = 0
    compared = 0

    for pair in comparison.get("paired_rows", []):
        gold = pair.get("gold_fields", {}) or {}
        cands = pair.get("extraction_candidates", []) or []
        gold_index = pair.get("gold_index", -1)
        gold_effect = _to_float(gold.get(effect_col))
        gtok: set[str] = set()
        for oc in outcome_cols:
            gtok |= _outcome_tokens(gold.get(oc), stopwords)

        # 100% recall gate: a gold row with mapped numeric values but no
        # extraction candidate is an unmatched gold finding, not a benign gap.
        if not cands:
            fields_with_values = [
                efield for gcol, efield in cmap.items()
                if _to_float(gold.get(gcol)) is not None
            ]
            if fields_with_values:
                unmatched_with_values += 1
                unmatched_detail.append({
                    "gold_index": gold_index,
                    "fields_with_values": fields_with_values,
                })
            continue

        # R3-15: identity-based narrowing BEFORE any value-proximity selection.
        cands = _narrow_by_identity_pairs(cands, gold, cfg.narrow_columns, stopwords)

        # Flag pairings nearest-effect cannot uniquely disambiguate.
        amb = _ambiguous_within_tol(gold_effect, cands, gtok, stopwords, rel_tol)
        if amb:
            ambiguous_pairings.append({
                "gold_index": gold_index,
                "candidate_indices": amb,
            })

        cand = _best_candidate(gold_effect, cands, gtok, stopwords)
        if cand is None:
            continue
        for gcol, efield in cmap.items():
            gv = _to_float(gold.get(gcol))
            ev = _to_float(_candidate_value(cand, efield))
            if gv is None or ev is None:
                continue
            compared += 1
            denom = max(abs(gv), 1e-9)
            rel = abs(gv - ev) / denom
            sign_flip = efield in SIGN_SENSITIVE and (gv * ev < 0)
            if rel > rel_tol or sign_flip:
                adj = _is_adjudicated_gold_error(
                    adjudications, document_id, gold_index, gcol, efield
                )
                if adj is not None:
                    # Excluded from the gate count; surfaced with its evidence.
                    adjudicated_gold_errors.append({
                        "gold_index": gold_index,
                        "field": efield,
                        "gold_value": gv,
                        "extraction_value": ev,
                        "rel_diff": round(rel, 4),
                        "sign_flip": sign_flip,
                        "extraction_index": cand.get("extraction_index"),
                        "adjudication": adj,
                    })
                    continue
                mismatches.append(ValueMismatch(
                    gold_index=gold_index,
                    field=efield,
                    gold_value=gv,
                    extraction_value=ev,
                    rel_diff=round(rel, 4),
                    sign_flip=sign_flip,
                    extraction_index=cand.get("extraction_index"),
                ))

    return {
        "mismatches": [asdict(m) for m in mismatches],
        "mismatch_count": len(mismatches),
        "fields_compared": compared,
        "unmatched_gold_with_values": unmatched_with_values,
        "unmatched_gold_detail": unmatched_detail,
        "ambiguous_pairings": ambiguous_pairings,
        "adjudicated_gold_errors": adjudicated_gold_errors,
        "note": (
            "Deterministic numeric diff of paired gold vs extraction values "
            "(config-driven matching, review note). A mismatch flags a value "
            "differing > rel_tol or flipping sign on a best-matched candidate; "
            "mismatch_count is a hard convergence gate. unmatched_gold_with_values "
            "is the 100% recall gate — gold findings with values and no extraction "
            "counterpart. ambiguous_pairings need agent adjudication "
            "(nearest-effect was not unique). adjudicated_gold_errors are "
            "mismatches overruled as gold errors with documented evidence, "
            "excluded from the gate."
        ),
    }
