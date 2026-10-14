"""PMID → PDF path resolution.

The corpus convention is that PDF filenames embed the PubMed ID inside the
last bracket group, e.g.:

    Smith 2024 (J Example) [12345678].pdf
    Jones 2025 (Example Rev) [23456789 guideline].pdf

Some files have no bracket (e.g. preprints, papers with DOI-only ids). Those
are skipped — callers that need them must locate them another way.

Use this module from any batch script or pipeline that needs to find a PDF
by PMID. Do NOT reinvent the resolution with shell globs (the brackets are
shell character classes and you will get the wrong file).
"""

from __future__ import annotations

import re
from pathlib import Path

_BRACKET_PMID = re.compile(r"\[(\d+)[^\]]*\]")


def parse_pmid(filename: str) -> int | None:
    """Extract PMID from a corpus filename, or None if not parseable.

    Looks for the LAST `[...]` group whose contents start with digits and
    returns those digits as an int.
    """
    matches = _BRACKET_PMID.findall(filename)
    if not matches:
        return None
    return int(matches[-1])


def build_pdf_index(pdf_dir: Path) -> dict[int, Path]:
    """Scan pdf_dir for `*.pdf` files and return {pmid: Path}.

    Files without a parseable PMID are silently skipped. Two files claiming
    the same PMID raise ValueError — that's a corpus-curation bug the caller
    must fix before proceeding.
    """
    pdf_dir = Path(pdf_dir)
    index: dict[int, Path] = {}
    collisions: dict[int, list[Path]] = {}

    for pdf in sorted(pdf_dir.glob("*.pdf")):
        pmid = parse_pmid(pdf.name)
        if pmid is None:
            continue
        if pmid in index:
            collisions.setdefault(pmid, [index[pmid]]).append(pdf)
        else:
            index[pmid] = pdf

    if collisions:
        msgs = [
            f"  PMID {pmid}: {[str(p) for p in paths]}"
            for pmid, paths in collisions.items()
        ]
        raise ValueError(
            f"PDF index collisions in {pdf_dir}:\n" + "\n".join(msgs)
        )

    return index


