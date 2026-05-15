"""Variant identity resolution for eval — a domain-agnostic resolution chain.

Both the extraction and the gold standard carry variant identifiers, but in
different namespaces. The extraction is normalized by Comparanda to canonical
ids (`variant_rsid` / `variant_canonical`) — it is self-describing. The gold
standard may:

  - carry a canonical id inline in a variant field        (direct match),
  - ship a *crosswalk* mapping its local codes/strings
    to canonical ids                                       (crosswalk match),
  - carry neither                                          (unresolved — the
                                                            caller defers the
                                                            pairing to the
                                                            eval agent).

The crosswalk, when one exists, is described by a per-domain config file
(`benchmark/eval_config/variant_crosswalk.json` — domain data). This module
contains no domain-specific knowledge: which sheet, which columns, and which
gold/extraction fields to use all come from that config. A domain whose gold
standard ships no crosswalk simply omits the file; resolution then falls back
to inline-identifier matching, and anything still unresolved is left for the
agent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

_RSID_RE = re.compile(r"rs\d+", re.IGNORECASE)
_NULLISH = {"", "n/a", "na", "none", "nan", "-"}


def _canonical_token(value: object) -> str | None:
    """Normalize a value to a canonical comparison token, or None.

    A token is produced only for a *recognizable* canonical identifier:
    an rs-number, or an HGVS string (c./g./m./n./p.-prefixed, optionally with a
    gene prefix). Star-alleles, protein-change shorthand, and free text return
    None — the caller then routes them through the crosswalk. This strictness
    is what keeps `_resolve_value` from dead-ending on a non-canonical string.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in _NULLISH:
        return None
    m = _RSID_RE.search(s)
    if m:
        return m.group(0).lower()
    s2 = re.sub(r"\s+", "", s).lower()
    if re.match(r"(?:dpyd)?[cgmnp]\.", s2):
        return re.sub(r"^dpyd", "", s2)
    return None


def _match_key(value: object) -> str | None:
    """Normalize a messy free-text variant string into a crosswalk lookup key.

    Parenthetical annotations and a leading gene prefix are dropped, whitespace
    is collapsed, and the result is lower-cased. The crosswalk enumerates
    zygosity variants ("*2A het", "*2A wt", ...) as distinct rows, so zygosity
    suffixes are intentionally NOT stripped here.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s in _NULLISH:
        return None
    s = re.sub(r"\([^()]*\)", "", s)        # drop parentheticals
    s = re.sub(r"^dpyd\s+", "", s)          # drop a leading gene prefix
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _split_combination(value: str) -> list[str]:
    """Split a combination-group variant string into components.

    Some papers report effect sizes only for combined multi-variant carrier
    groups, encoded as `variant_raw="*2A|D949V|I560S (3 consensual SNPs)"` or
    "DPYD c.1905+1G>A or DPYD c.2846A>T". A trailing group-level parenthetical
    is stripped before splitting so it doesn't pollute the last component.
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
    """Per-domain description of a gold-standard variant crosswalk."""

    sheet: str
    canonical_column: str
    text_key_columns: tuple[str, ...] = ()
    id_key_column: str | None = None
    header_row: int = 0
    gold_text_field: str | None = None
    gold_id_field: str | None = None
    extraction_canonical_fields: tuple[str, ...] = (
        "variant_rsid",
        "variant_canonical",
        "variant_raw",
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
    """Load the variant crosswalk config for a domain, or None if absent."""
    path = Path(eval_config_dir) / "variant_crosswalk.json"
    if not path.exists():
        return None
    return CrosswalkConfig.from_dict(json.loads(path.read_text()))


@dataclass
class VariantResolver:
    """Resolves gold and extraction rows to a comparable canonical token.

    Build with `VariantResolver.build(...)`. When no crosswalk config is given
    the resolver still works — it resolves the extraction side from its
    normalized fields and the gold side from any inline rs-number, leaving the
    rest unresolved for the agent.
    """

    config: CrosswalkConfig | None = None
    _by_text: dict[str, str] = field(default_factory=dict)
    _by_id: dict[str, str] = field(default_factory=dict)
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        gold_spreadsheet_path: Path | None,
        config: CrosswalkConfig | None,
    ) -> "VariantResolver":
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
        """Resolve a single variant string to a token: inline identifier first,
        then a crosswalk text lookup."""
        tok = _canonical_token(value)
        if tok is not None:
            return tok
        if self._by_text:
            k = _match_key(value)
            if k and k in self._by_text:
                return self._by_text[k]
        return None

    def resolve_extraction(self, row: dict) -> str | None:
        """Resolve an extraction row to its primary canonical token."""
        tokens = self.resolve_extraction_all(row)
        return tokens[0] if tokens else None

    def resolve_extraction_all(self, row: dict) -> list[str]:
        """All canonical tokens for an extraction row.

        Combination-group variant strings (pipe- or 'or'-delimited) are
        expanded into components and each component resolved independently, so
        a multi-variant carrier row participates in matching for every variant
        it represents.
        """
        fields = (
            self.config.extraction_canonical_fields
            if self.config is not None
            else ("variant_rsid", "variant_canonical", "variant_raw")
        )
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
        'crosswalk_text', 'direct_rsid', 'unresolved'. A None token always
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
        # Fallback: an rs-number written inline in a gold variant field.
        for f in ("Variant", "variant", "variant_raw"):
            tok = _canonical_token(_row_get(row, f))
            if tok and tok.startswith("rs"):
                return tok, "direct_rsid"
        return None, "unresolved"


def _row_get(row, key: str):
    """Field access that works for both dicts and pandas Series."""
    try:
        val = row.get(key)
    except AttributeError:
        val = row[key] if key in getattr(row, "index", []) else None
    return val
