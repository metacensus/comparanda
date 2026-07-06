"""Entity identity resolution for eval — a domain-agnostic resolution chain.

Both the extraction and the gold standard carry entity identifiers, but in
different namespaces. The extraction is normalized by Comparanda to canonical
ids (`entity_canonical_id` / `entity_canonical`) — it is self-describing. The
gold standard may:

  - carry a canonical id inline in an entity field         (direct match),
  - ship a *crosswalk* mapping its local codes/strings
    to canonical ids                                        (crosswalk match),
  - carry neither                                           (unresolved — the
                                                             caller defers the
                                                             pairing to the
                                                             eval agent).

The crosswalk, when one exists, is described by a per-domain config file
(`benchmark/eval_config/entity_crosswalk.json` — a per-domain config the user
supplies). This module contains no domain-specific knowledge: which sheet,
which columns, and which gold/extraction fields to use all come from that
config. A domain whose gold standard ships no crosswalk simply omits the file;
resolution then falls back to inline-identifier matching, and anything still
unresolved is left for the agent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

_NULLISH = {"", "n/a", "na", "none", "nan", "-"}


def _canonical_token(value: object) -> str | None:
    """Normalize a value to a canonical comparison token, or None.

    Generic identity resolution: a value is reduced to a lowercased,
    whitespace-collapsed token. If the value carries a recognizable inline
    canonical id (per the configured crosswalk's canonical convention) it is
    treated as authoritative; this base implementation simply treats any
    non-null value as its own token. Domains that need stricter recognition
    (so that non-canonical free text routes through the crosswalk instead of
    self-matching) plug their rules in below.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in _NULLISH:
        return None
    # Domain-specific identity rules plug in here.
    return re.sub(r"\s+", "", s).lower()


def _match_key(value: object) -> str | None:
    """Normalize a messy free-text entity string into a crosswalk lookup key.

    Parenthetical annotations are dropped, whitespace is collapsed, and the
    result is lower-cased. Category qualifiers carried as suffixes are left
    intact, because a crosswalk may enumerate qualifier variants as distinct
    rows.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s in _NULLISH:
        return None
    s = re.sub(r"\([^()]*\)", "", s)        # drop parentheticals
    # Domain-specific identity rules plug in here.
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _split_combination(value: str) -> list[str]:
    """Split a combination-group entity string into components.

    Some studies report effect sizes only for combined multi-entity groups,
    encoded as `entity_raw="E1|E2|E3 (3 combined entities)"` or
    "E1 or E2". A trailing group-level parenthetical is stripped before
    splitting so it doesn't pollute the last component.
    """
    base = re.sub(r"\s*\([^()]*\)\s*$", "", value).strip()
    parts = re.split(r"\s*\|\s*|\s+or\s+", base)
    return [p.strip() for p in parts if p.strip()]


def _id_key(value: object) -> str | None:
    """Normalize an integer-ish code key (gold stores e.g. '2.0' as a float)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in _NULLISH:
        return None
    try:
        return str(int(float(s)))
    except (ValueError, TypeError):
        return s.lower()


@dataclass
class CrosswalkConfig:
    """Per-domain description of a gold-standard entity crosswalk."""

    sheet: str
    canonical_column: str
    text_key_columns: tuple[str, ...] = ()
    id_key_column: str | None = None
    header_row: int = 0
    gold_text_field: str | None = None
    gold_id_field: str | None = None
    extraction_canonical_fields: tuple[str, ...] = (
        "entity_canonical_id",
        "entity_canonical",
        "entity_raw",
    )

    @classmethod
    def from_dict(cls, d: dict) -> "CrosswalkConfig":
        ecf = d.get("extraction_canonical_fields")
        # Accept either a single text_key_column or a list of text_key_columns.
        tkc = d.get("text_key_columns")
        if tkc is None and d.get("text_key_column"):
            tkc = [d["text_key_column"]]
        return cls(
            sheet=d["sheet"],
            canonical_column=d["canonical_column"],
            text_key_columns=tuple(tkc) if tkc else (),
            id_key_column=d.get("id_key_column"),
            header_row=int(d.get("header_row", 0)),
            gold_text_field=d.get("gold_text_field"),
            gold_id_field=d.get("gold_id_field"),
            extraction_canonical_fields=tuple(ecf) if ecf else cls.extraction_canonical_fields,
        )


def load_crosswalk_config(eval_config_dir: Path) -> CrosswalkConfig | None:
    """Load the entity crosswalk config for a domain, or None if absent."""
    path = Path(eval_config_dir) / "entity_crosswalk.json"
    if not path.exists():
        return None
    return CrosswalkConfig.from_dict(json.loads(path.read_text()))


@dataclass
class EntityResolver:
    """Resolves gold and extraction rows to a comparable canonical token.

    Build with `EntityResolver.build(...)`. When no crosswalk config is given
    the resolver still works — it resolves the extraction side from its
    normalized fields and the gold side from any inline canonical id, leaving
    the rest unresolved for the agent.
    """

    config: CrosswalkConfig | None = None
    _by_text: dict[str, str] = field(default_factory=dict)
    _by_id: dict[str, str] = field(default_factory=dict)
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)
    # Gold entity field names tried (in order) for the inline-id / direct-token
    # fallbacks. Defaults preserve legacy behavior; set per-domain from
    # gold_config.entity_columns (review note).
    entity_fields: tuple[str, ...] = ("Entity", "entity", "entity_raw")
    # R3-13: extraction-side field list override (set from
    # gold_config.extraction_entity_fields by callers). Precedence:
    # this attribute > crosswalk config > legacy default.
    extraction_fields: tuple[str, ...] | None = None

    @classmethod
    def build(
        cls,
        gold_spreadsheet_path: Path | None,
        config: CrosswalkConfig | None,
    ) -> "EntityResolver":
        resolver = cls(config=config)
        if config is not None and gold_spreadsheet_path is not None:
            resolver._load_crosswalk(Path(gold_spreadsheet_path))
        return resolver

    def _load_crosswalk(self, gold_spreadsheet_path: Path) -> None:
        cfg = self.config
        assert cfg is not None
        df = pd.read_excel(
            gold_spreadsheet_path, sheet_name=cfg.sheet, header=cfg.header_row
        )
        # Collapse internal whitespace in column names — crosswalk sheets often
        # carry stray double-spaces / newlines in their headers.
        df.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]

        def _norm_col(name: str) -> str:
            return re.sub(r"\s+", " ", name).strip()

        canon_col = _norm_col(cfg.canonical_column)
        text_cols = [_norm_col(c) for c in cfg.text_key_columns]
        id_col = _norm_col(cfg.id_key_column) if cfg.id_key_column else None

        for _, row in df.iterrows():
            canon = _canonical_token(row.get(canon_col))
            if canon is None:
                continue
            for text_col in text_cols:
                k = _match_key(row.get(text_col))
                if k:
                    self._register(self._by_text, k, canon)
            if id_col:
                k = _id_key(row.get(id_col))
                if k:
                    self._register(self._by_id, k, canon)

    def _register(self, table: dict[str, str], key: str, canon: str) -> None:
        existing = table.get(key)
        if existing is not None and existing != canon:
            self.conflicts.append((key, existing, canon))
            return
        table[key] = canon

    @property
    def crosswalk_loaded(self) -> bool:
        return bool(self._by_text or self._by_id)

    def _resolve_value(self, value: object) -> str | None:
        """Resolve a single entity string to a token.

        Resolution order:
        1. Reduce the value to a canonical token.
        2. Consult the crosswalk if loaded: look the match-key up directly.
        3. Fall back to whatever ``_canonical_token`` recognized — better than
           ``None`` when the crosswalk has no entry, since two equivalent
           canonical strings from different studies will still compare equal.

        # Domain-specific identity rules plug in here.
        """
        tok = _canonical_token(value)
        if self._by_text:
            k = _match_key(value)
            if k and k in self._by_text:
                return self._by_text[k]
        return tok

    def resolve_extraction(self, row: dict) -> str | None:
        """Resolve an extraction row to its primary canonical token."""
        tokens = self.resolve_extraction_all(row)
        return tokens[0] if tokens else None

    def resolve_extraction_all(self, row: dict) -> list[str]:
        """All canonical tokens for an extraction row.

        Combination-group entity strings (pipe- or 'or'-delimited) are
        expanded into components and each component resolved independently, so
        a multi-entity group row participates in matching for every entity
        it represents.
        """
        if self.extraction_fields is not None:
            fields = self.extraction_fields
        elif self.config is not None:
            fields = self.config.extraction_canonical_fields
        else:
            fields = ("entity_canonical_id", "entity_canonical", "entity_raw")
        tokens: list[str] = []
        for f in fields:
            val = row.get(f)
            if not val:
                continue
            for component in _split_combination(str(val)):
                tok = self._resolve_value(component)
                if tok and tok not in tokens:
                    tokens.append(tok)
        return tokens

    def resolve_gold(self, row) -> tuple[str | None, str]:
        """Resolve a gold row to a canonical token.

        Returns (token, rung) where rung is one of: 'crosswalk_id',
        'crosswalk_text', 'direct_id', 'unresolved'. A None token always
        pairs with rung 'unresolved' and signals the caller to defer.
        """
        cfg = self.config
        if cfg is not None:
            if cfg.gold_id_field and self._by_id:
                k = _id_key(_row_get(row, cfg.gold_id_field))
                if k and k in self._by_id:
                    return self._by_id[k], "crosswalk_id"
            if cfg.gold_text_field and self._by_text:
                k = _match_key(_row_get(row, cfg.gold_text_field))
                if k and k in self._by_text:
                    return self._by_text[k], "crosswalk_text"
        # Fallback: a canonical id written inline in a gold entity field.
        # Domain-specific identity rules plug in here — without a crosswalk,
        # a token is only emitted if a gold field actually carries one.
        for f in self.entity_fields:
            tok = _canonical_token(_row_get(row, f))
            if tok and cfg is not None and self._by_text and tok in self._by_text.values():
                return tok, "direct_id"
        # Symmetric direct-tokenization fallback (review note): when no crosswalk
        # resolves the gold row, tokenize its entity string the SAME way the
        # extraction side is tokenized (resolve_extraction_all also falls back to
        # `_canonical_token`). This lets gold and a normalized extraction match on
        # equal entity strings without a per-domain crosswalk, instead of
        # dropping every gold row to 'unresolved'. The eval agent still re-checks
        # ambiguous pairings in detailed mode.
        for f in self.entity_fields:
            tok = _canonical_token(_row_get(row, f))
            if tok:
                return tok, "direct_token"
        return None, "unresolved"


def _row_get(row, key: str):
    """Field access that works for both dicts and pandas Series."""
    try:
        val = row.get(key)
    except AttributeError:
        val = row[key] if key in getattr(row, "index", []) else None
    return val
