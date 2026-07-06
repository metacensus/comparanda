"""Gold standard loader — reads the gold-standard gold-standard workbook.

The gold standard is the human-verified extraction (a per-domain spreadsheet of
rows x columns across the studies in the corpus). This module loads and
normalizes it for comparison.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from harness.eval.gold_config import GoldConfig


def load_gold_standard(
    spreadsheet_path: Path,
    sheet_name: str = "Individual Outcomes",
    gold_config: GoldConfig | None = None,
) -> pd.DataFrame:
    """Load the gold standard individual outcomes sheet.

    When `gold_config` is supplied, its `sheet` overrides `sheet_name` and the
    configured `document_id_column` is required to be present — a missing
    document-id column is a loud error rather than silent downstream nulls
    (review note).
    """
    if gold_config is not None:
        sheet_name = gold_config.sheet
    df = pd.read_excel(spreadsheet_path, sheet_name=sheet_name)

    # Strip stray whitespace from column names.
    df.columns = [str(c).strip() for c in df.columns]

    if gold_config is not None:
        doc_col = gold_config.document_id_column
        if doc_col not in df.columns:
            raise ValueError(
                f"Gold standard sheet '{sheet_name}' is missing the configured "
                f"document_id_column '{doc_col}'. Present columns: {list(df.columns)} "
                f"(review note: loud column validation, not silent nulls)."
            )

    return df


def load_document_id_map(document_id_map_path: Path) -> dict[str, int | str]:
    """Load paper_id → document_id mapping.

    Returns dict mapping paper filename stems to document id integers (or
    strings for sources without a numeric document id).
    """
    raw = json.loads(document_id_map_path.read_text())
    result: dict[str, int | str] = {}
    for key, val in raw.items():
        document_id = val["document_id"] if isinstance(val, dict) else val
        try:
            result[key] = int(document_id)
        except (ValueError, TypeError):
            result[key] = str(document_id)
    return result


def filter_by_document_ids(
    gold_df: pd.DataFrame,
    document_ids: list[int | str],
    document_id_column: str = "DocumentID",
) -> pd.DataFrame:
    """Filter gold standard to specific document ids."""
    return gold_df[gold_df[document_id_column].isin(document_ids)].reset_index(drop=True)


def gold_entities_for_paper(
    gold_df: pd.DataFrame,
    document_id: int | str,
    document_id_column: str = "DocumentID",
    entity_column: str = "Entity",
) -> set[str]:
    """Get the set of entity names in the gold standard for a paper."""
    subset = gold_df[gold_df[document_id_column] == document_id]
    return set(subset[entity_column].dropna().unique())


def gold_row_count(
    gold_df: pd.DataFrame,
    document_id: int | str,
    document_id_column: str = "DocumentID",
) -> int:
    """Get the number of gold standard rows for a paper."""
    return len(gold_df[gold_df[document_id_column] == document_id])


def _num(v) -> float | None:
    """Coerce a cell to float, treating blanks and NaN as None."""
    if v is None:
        return None
    try:
        f = float(str(v).strip().replace(",", ""))
    except (ValueError, TypeError):
        return None
    if f != f:  # NaN
        return None
    return f


def check_gold_consistency(df: pd.DataFrame, gold_config: GoldConfig) -> list[dict]:
    """Internal-consistency WARNINGS on the gold standard itself (review note).

    These are gold-side warnings, never errors — a wrong gold value is as
    dangerous as a wrong extraction, but the gold is corrigible, so we surface
    rather than block. Three checks, each skipped when its inputs are absent:

      - `ci_bounds_inverted`: ci_lower > ci_upper.
      - `effect_outside_ci`: effect outside [ci_lower, ci_upper] beyond a slack
        of 5% of the CI width.
      - `events_exceed_total`: n_events > n_total (only when both columns are
        mapped via value_column_map targets n_events / n_total).

    Gold column names are derived from `gold_config` — the effect column from
    `gold_effect_column`, the CI/n columns by reverse-lookup of
    `value_column_map` so domains with different headers are handled.

    Each warning: {document_id, row_index, issue, values}.
    """
    cmap = gold_config.value_column_map
    # Reverse map: core field -> gold column.
    by_field: dict[str, str] = {}
    for gcol, core in cmap.items():
        by_field.setdefault(core, gcol)

    effect_col = gold_config.gold_effect_column
    lower_col = by_field.get("ci_lower")
    upper_col = by_field.get("ci_upper")
    events_col = by_field.get("n_events")
    total_col = by_field.get("n_total")
    doc_col = gold_config.document_id_column

    warnings: list[dict] = []
    cols = set(df.columns)

    for idx, row in df.iterrows():
        doc_id = row.get(doc_col) if doc_col in cols else None

        lo = _num(row.get(lower_col)) if lower_col in cols else None
        hi = _num(row.get(upper_col)) if upper_col in cols else None
        eff = _num(row.get(effect_col)) if effect_col in cols else None

        if lo is not None and hi is not None and lo > hi:
            warnings.append({
                "document_id": doc_id,
                "row_index": int(idx),
                "issue": "ci_bounds_inverted",
                "values": {"ci_lower": lo, "ci_upper": hi},
            })

        if eff is not None and lo is not None and hi is not None and lo <= hi:
            slack = 0.05 * (hi - lo)
            if eff < lo - slack or eff > hi + slack:
                warnings.append({
                    "document_id": doc_id,
                    "row_index": int(idx),
                    "issue": "effect_outside_ci",
                    "values": {"effect": eff, "ci_lower": lo, "ci_upper": hi,
                               "slack": round(slack, 6)},
                })

        if events_col in cols and total_col in cols:
            ne = _num(row.get(events_col))
            nt = _num(row.get(total_col))
            if ne is not None and nt is not None and ne > nt:
                warnings.append({
                    "document_id": doc_id,
                    "row_index": int(idx),
                    "issue": "events_exceed_total",
                    "values": {"n_events": ne, "n_total": nt},
                })

    return warnings
