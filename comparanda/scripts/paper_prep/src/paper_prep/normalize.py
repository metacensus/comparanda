"""Cell normalization for ground-truth construction and scoring.

Two modes:
- strict: preserves numeric precision ('0.05' != '0.050'), case, and meaningful structure
- loose_numeric: collapses trailing zeros on decimals (reports side-by-side score)

Both collapse unicode, whitespace, and common null-token variants to None.
"""

from __future__ import annotations

import re
import unicodedata

# Values treated as equivalent to missing data
NULL_TOKENS = frozenset(
    {
        "",
        "-",
        "--",
        "—",
        "–",
        "N/A",
        "n/a",
        "NA",
        "na",
        "None",
        "none",
        "null",
        "NULL",
        "N.A.",
        "·",
    }
)

_DASH_CHARS = ("‐", "‑", "‒", "–", "—", "−", "­")
_WHITESPACE_RE = re.compile(r"\s+")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_UNDERSCORE_BOLD_RE = re.compile(r"__(.+?)__")
_MD_ITALIC_RE = re.compile(r"(?<!\w)_(.+?)_(?!\w)")
# Single-asterisk italic. Must run AFTER _MD_BOLD_RE so it doesn't see
# the inner asterisks of `**bold**`. Common case: gene names rendered as
# `*UGT1A1*` per scientific convention.
#
# Restrictions, to avoid eating star-allele notation like `*28/*28` (where
# the asterisks are part of the data, not markdown):
#  - Inner content must start with a LETTER (gene symbols start with letters;
#    star-allele numbers start with digits, so `*28...*` won't match).
#  - Outside the opening `*`: not a word char and not another `*` (so we
#    don't bleed into bold).
#  - Outside the closing `*`: not a word char and not another `*`.
_MD_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*([A-Za-z][^*]*?)\*(?![\w*])")
_TRAILING_ZERO_RE = re.compile(r"(\d+\.\d*?)0+\b")


def _basic_normalize(value: str | None) -> str | None:
    """Shared normalization: NFKC, whitespace collapse, markdown strip, OCR fixes."""
    if value is None:
        return None
    s = str(value)
    # Do OCR-specific char fixes BEFORE NFKC (which would expand '¼' to '1⁄4')
    s = s.replace("¼", "=")
    s = unicodedata.normalize("NFKC", s).strip()
    if not s:
        return None

    # Unify dashes to ASCII hyphen-minus
    for ch in _DASH_CHARS:
        s = s.replace(ch, "-")

    # Strip markdown emphasis. Order matters: bold first (consumes `**...**`),
    # then italic-star, so the single-asterisk regex only sees lone `*`s.
    s = _MD_BOLD_RE.sub(r"\1", s)
    s = _MD_UNDERSCORE_BOLD_RE.sub(r"\1", s)
    s = _MD_ITALIC_STAR_RE.sub(r"\1", s)
    s = _MD_ITALIC_RE.sub(r"\1", s)

    # Collapse whitespace
    s = _WHITESPACE_RE.sub(" ", s).strip()

    if s in NULL_TOKENS:
        return None
    return s


def normalize_strict(value: str | None) -> str | None:
    """Normalize preserving numeric precision. Returns None for null-equivalent values."""
    return _basic_normalize(value)


def normalize_loose_numeric(value: str | None) -> str | None:
    """Normalize and additionally collapse trailing decimal zeros (0.050 -> 0.05)."""
    s = _basic_normalize(value)
    if s is None:
        return None
    s = _TRAILING_ZERO_RE.sub(lambda m: m.group(1).rstrip("0").rstrip("."), s)
    # '5.' -> '5'
    s = re.sub(r"(\d+)\.\B", r"\1", s)
    return s or None


def cells_equal(a: str | None, b: str | None, *, loose_numeric: bool = False) -> bool:
    """Compare two raw cell strings under the chosen normalization."""
    fn = normalize_loose_numeric if loose_numeric else normalize_strict
    return fn(a) == fn(b)
