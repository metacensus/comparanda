"""PMID → PDF path resolution.

The corpus convention is that PDF filenames embed the PubMed ID inside the
last bracket group, e.g.:

    Zhang 2007 (Med Oncol) [17848752].pdf
    Amstutz 2018 (Clin Pharmacol Ther) [29152729 CPIC].pdf

Some files have no bracket (e.g. preprints, papers with DOI-only ids). Those
are skipped — callers that need them must locate them another way.

Use this module from any batch script or pipeline that needs to find a PDF
by PMID. Do NOT reinvent the resolution with shell globs (the brackets are
shell character classes and you will get the wrong file).
"""

from __future__ import annotations

import re
import sys
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


def resolve_pdf(pdf_dir: Path, pmid: int) -> Path:
    """Resolve a single PMID to its PDF path. Raises KeyError if missing."""
    index = build_pdf_index(pdf_dir)
    if pmid not in index:
        raise KeyError(f"No PDF found for PMID {pmid} in {pdf_dir}")
    return index[pmid]


def _main() -> int:
    """CLI: `python -m paper_prep.pdf_index <pdf_dir> [pmid]`

    With no PMID: print the full index as `<pmid>\\t<path>` lines.
    With a PMID: print just the resolved path, exit 1 if not found.
    """
    if len(sys.argv) < 2:
        print("usage: python -m paper_prep.pdf_index <pdf_dir> [pmid]", file=sys.stderr)
        return 2

    pdf_dir = Path(sys.argv[1])
    if len(sys.argv) == 2:
        for pmid, path in sorted(build_pdf_index(pdf_dir).items()):
            print(f"{pmid}\t{path}")
        return 0

    pmid = int(sys.argv[2])
    try:
        print(resolve_pdf(pdf_dir, pmid))
        return 0
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
