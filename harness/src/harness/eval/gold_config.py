"""Per-domain gold-standard schema configuration.

The eval pipeline used to hardcode one gold standard's shape: sheet name,
document-id and entity column literals, the twelve projected gold fields, the
value-column map, the effect/outcome column names — and, worst of all,
*education-domain stopwords* baked into the supposedly domain-agnostic outcome
matcher (`value_diff._STOPWORDS`). A new domain whose spreadsheet used different
headers got silent nulls, which made the value gate vacuously clean.

This module centralizes that schema into one dataclass, loadable per-domain
from `eval_config/gold_config.json`. The built-in defaults describe an
example spreadsheet layout (an "Individual Outcomes" sheet keyed by
"DocumentID" with "Effect Size" / CI / count columns); override any of them
in gold_config.json to match your own gold standard. The defaults ship only **generic English
stopwords** — no domain vocabulary — so the outcome-narrowing step never
deletes the tokens that distinguish outcomes ("math posttest" vs
"reading posttest").

It also loads `eval_config/gold_adjudications.json` (adjudication
path): per-mismatch resolutions (`gold_error` | `extraction_error`) with
mandatory evidence for any overruled gold value, so the value gate stays hard
while the gold standard stays corrigible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields as dataclass_fields
from pathlib import Path


# Example layout: the gold fields projected into each comparison row.
# Override with `gold_fields` in gold_config.json.
_DEFAULT_GOLD_FIELDS = (
    "Entity ID", "Composite Category ID", "Outcome Description",
    "Grade Clean", "Outcome Category", "Effect Size",
    "Lower 95 CI", "Upper 95 CI", "P Value",
    "Subjects with Entity", "Subjects with Entity and Outcome",
    "Comparison Type",
)

# Example layout: gold column -> extraction core field compared numerically.
# Override with `value_column_map` in gold_config.json.
_DEFAULT_VALUE_COLUMN_MAP = {
    "Effect Size": "effect_estimate",
    "Lower 95 CI": "ci_lower",
    "Upper 95 CI": "ci_upper",
    "Subjects with Entity": "n_total",
}

# Generic English stopwords ONLY. The education-domain words that previously
# leaked into value_diff._STOPWORDS ("posttest", "standardized", "early",
# "achievement", "math", "reading", "score", "scores", "test") are deliberately
# absent — they would collapse the outcome-narrowing step on an education
# corpus.
_DEFAULT_STOPWORDS = (
    "the", "a", "an", "of", "and", "or", "in", "on", "for", "with", "to",
    "at", "by",
)

_VALID_RESOLUTIONS = {"gold_error", "extraction_error"}


@dataclass
class GoldConfig:
    """The per-domain gold-standard shape.

    Defaults describe an example spreadsheet layout; every field can be
    overridden per domain in `eval_config/gold_config.json`.
    """

    sheet: str = "Individual Outcomes"
    document_id_column: str = "DocumentID"
    entity_columns: tuple[str, ...] = ("Entity", "entity", "entity_raw")
    gold_fields: tuple[str, ...] = _DEFAULT_GOLD_FIELDS
    value_column_map: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_VALUE_COLUMN_MAP)
    )
    gold_effect_column: str = "Effect Size"
    outcome_text_columns: tuple[str, ...] = (
        "Outcome Description", "Outcome Category",
    )
    stopwords: frozenset[str] = field(
        default_factory=lambda: frozenset(_DEFAULT_STOPWORDS)
    )
    # Extraction-side fields tried (in order) when resolving an
    # extraction row to an entity token. The legacy default assumes the schema
    # named its entity axis entity_raw/_canonical — a domain whose discovered
    # schema uses a different axis (e.g. intervention_raw for a mixed-design
    # corpus) configures it here instead of silently scoring 0% entity
    # coverage.
    extraction_entity_fields: tuple[str, ...] = (
        "entity_canonical_id", "entity_canonical", "entity_raw",
    )
    # Ordered (gold_column, extraction_field) narrowing pairs applied
    # by the value-diff BEFORE nearest-effect candidate selection. Each pair
    # soft-narrows the candidate pool to rows whose extraction_field text
    # token-overlaps the gold column's text (no overlap anywhere → pool kept).
    # This pins the compared candidate by IDENTITY (timepoint, operation,
    # metric) rather than by value proximity — value proximity must never be
    # the thing that selects the value comparison. Empty default = legacy
    # outcome-token narrowing only.
    narrow_columns: tuple[tuple[str, str], ...] = ()


def _coerce(field_name: str, value: object) -> object:
    """Coerce a JSON value to the field's expected container type."""
    tuple_fields = {
        "entity_columns", "gold_fields", "outcome_text_columns",
        "extraction_entity_fields",
    }
    if field_name in tuple_fields:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"gold_config field '{field_name}' must be a list")
        return tuple(value)
    if field_name == "stopwords":
        if not isinstance(value, (list, tuple)):
            raise ValueError("gold_config field 'stopwords' must be a list")
        return frozenset(str(v).lower() for v in value)
    if field_name == "value_column_map":
        if not isinstance(value, dict):
            raise ValueError("gold_config field 'value_column_map' must be an object")
        return dict(value)
    if field_name == "narrow_columns":
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(p, (list, tuple)) and len(p) == 2 for p in value
        ):
            raise ValueError(
                "gold_config field 'narrow_columns' must be a list of "
                "[gold_column, extraction_field] pairs"
            )
        return tuple((str(g), str(e)) for g, e in value)
    return value


def load_gold_config(eval_config_dir: Path) -> GoldConfig:
    """Load `gold_config.json` from eval_config_dir, merged over defaults.

    Missing file → defaults (legacy behavior). Unknown top-level keys raise a
    loud ValueError rather than being silently ignored.
    """
    path = Path(eval_config_dir) / "gold_config.json"
    if not path.exists():
        return GoldConfig()

    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"gold_config.json must be a JSON object, got {type(raw).__name__}")

    valid_keys = {f.name for f in dataclass_fields(GoldConfig)}
    unknown = set(raw) - valid_keys
    if unknown:
        raise ValueError(
            f"Unknown key(s) in gold_config.json: {sorted(unknown)}. "
            f"Valid keys: {sorted(valid_keys)}"
        )

    overrides = {k: _coerce(k, v) for k, v in raw.items()}
    return GoldConfig(**overrides)


def load_adjudications(eval_config_dir: Path) -> list[dict]:
    """Load `gold_adjudications.json`.

    Shape::

        {"adjudications": [
            {"document_id": ..., "gold_index": ..., "field": ...,
             "resolution": "gold_error" | "extraction_error",
             "evidence": {"quote": ..., "location": ..., "page": ...},
             "adjudicated_by": ..., "adjudicated_at": ..., "note": ...}
        ]}

    Validation (loud):
      - `resolution` must be one of the two allowed values.
      - A `gold_error` entry REQUIRES a non-empty `evidence.quote` and
        `evidence.location` — an overruled gold value must be documented.

    Missing file → [].
    """
    path = Path(eval_config_dir) / "gold_adjudications.json"
    if not path.exists():
        return []

    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("gold_adjudications.json must be a JSON object")
    entries = raw.get("adjudications", [])
    if not isinstance(entries, list):
        raise ValueError("gold_adjudications.json 'adjudications' must be a list")

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"adjudication[{i}] must be an object")
        if not isinstance(entry.get("evidence"), dict):
            quote = entry.get("evidence_quote")
            location = entry.get("bundle_location")
            if quote or location:
                entry["evidence"] = {
                    "quote": quote or "",
                    "location": location or "",
                    "page": entry.get("page"),
                }
        resolution = entry.get("resolution")
        if resolution not in _VALID_RESOLUTIONS:
            raise ValueError(
                f"adjudication[{i}] resolution must be one of {sorted(_VALID_RESOLUTIONS)}, "
                f"got {resolution!r}"
            )
        if resolution == "gold_error":
            evidence = entry.get("evidence") or {}
            quote = (evidence.get("quote") or "").strip() if isinstance(evidence, dict) else ""
            location = (evidence.get("location") or "").strip() if isinstance(evidence, dict) else ""
            if not quote or not location:
                raise ValueError(
                    f"adjudication[{i}] resolution='gold_error' requires non-empty "
                    f"evidence.quote and evidence.location — an overruled gold value "
                    f"must carry documented evidence."
                )
    return entries
