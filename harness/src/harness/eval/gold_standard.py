"""Gold standard loader — reads the gold_standard.xlsx.

across 47 studies). This module loads and normalizes it for comparison.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def load_gold_standard(
    spreadsheet_path: Path,
    sheet_name: str = "Individual Outcomes",
) -> pd.DataFrame:
    """Load the gold standard individual outcomes sheet."""
    df = pd.read_excel(spreadsheet_path, sheet_name=sheet_name)

    # Normalize column names to lowercase with underscores
    df.columns = [str(c).strip() for c in df.columns]

    return df


def load_pmid_map(pmid_map_path: Path) -> dict[str, int]:
    """Load paper_id → PMID mapping.

    Returns dict mapping paper filename stems to PMID integers.
    """
    raw = json.loads(pmid_map_path.read_text())
    result = {}
    for key, val in raw.items():
        if isinstance(val, dict):
            result[key] = int(val["pmid"])
        else:
            result[key] = int(val)
    return result


def filter_by_pmids(
    gold_df: pd.DataFrame,
    pmids: list[int],
    pmid_column: str = "PMID",
) -> pd.DataFrame:
    """Filter gold standard to specific PMIDs."""
    return gold_df[gold_df[pmid_column].isin(pmids)].reset_index(drop=True)


def gold_variants_for_paper(
    gold_df: pd.DataFrame,
    pmid: int,
    pmid_column: str = "PMID",
    variant_column: str = "Variant",
) -> set[str]:
    """Get the set of variant names in the gold standard for a paper."""
    subset = gold_df[gold_df[pmid_column] == pmid]
    return set(subset[variant_column].dropna().unique())


def gold_row_count(
    gold_df: pd.DataFrame,
    pmid: int,
    pmid_column: str = "PMID",
) -> int:
    """Get the number of gold standard rows for a paper."""
    return len(gold_df[gold_df[pmid_column] == pmid])
