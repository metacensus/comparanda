"""First-pass per-table extractor using `claude -p` (Sonnet).

Subprocess wrapper that asks Claude to extract ONE table from the source
PDF and write `sonnet_tables/<docling_idx>.md`. Per-table direct dispatch
keeps each Sonnet call's context tiny — avoids the 32K output cap that
killed the Phase 4 verify-and-patch architecture on long DPYD papers.

The first-pass extracts tables only. Prose flows from Docling raw into
the bundle (see scripts/build_bundle.py). Figure decoration filtering
happens at extractor time via Comparanda's survey `figure_inventory`.

History: Phase 4 (verify-and-patch with per-section edit JSONs + figure
descriptions + prose patches) is preserved on branch
`phase4-section-pipeline-paused`. This module is the pivot to a faster /
cheaper minimal first-pass — see `docs/phase4_findings.md` (May-13 update).

Idempotent at per-table granularity: re-runs only fill in tables that
don't yet have `sonnet_tables/<docling_idx>.md` written.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


_PROMPT_TEMPLATE = """\
You are extracting ONE table from a scientific paper PDF for paper-prep.

PDF: {pdf_path}
Paper ID: {paper_id}

## Target table

This table was detected by Docling at index {docling_idx} on page {page}.
- Docling's row/col estimate: {num_rows} rows × {num_cols} cols
- Docling's caption hint (often empty or unreliable, may not be the
  actual caption): {docling_caption!r}

## Your task

1. Read the PDF for pages {page_range} using the Read tool's `pages:`
   parameter. The target table is on page {page}; the 1-page buffer
   covers captions that may be on the previous page and tables that
   may spill to the next page.
2. Locate the target table. Identify the canonical paper-assigned ID
   (e.g. "1", "2", "S1", "S2", "A1A"). Tables are typically labeled
   "Table N", "Supplementary Table N", or similar.
3. Extract the table exactly as the paper renders it.
4. Write the result to `sonnet_tables/{docling_idx}.md` in the working
   directory.

If the PDF actually shows that this Docling "table" is NOT a real table
(e.g. a figure misclassified as a table, page chrome, a code block, an
equation), write a file with just one line: `# NOT A TABLE`. The bundle
producer will skip it.

If two adjacent Docling-detected tables turn out to be the same logical
table split by Docling, render it as one continuous table in YOUR file
and note in the post-write report.

## Output format

Write `sonnet_tables/{docling_idx}.md`. The file MUST start with the
metadata comment block shown below — this is how downstream code links
your output back to Docling's table without parsing the caption:

```
<!-- paper-prep-meta
docling_idx: {docling_idx}
canonical_id: <the canonical ID you determined, e.g. "1", "S2", "A1A">
page: <the page number where the table starts in the PDF>
-->
# {paper_id} — Table <canonical_id> (Sonnet first-pass)

**Table <canonical_id>.** <caption verbatim from PDF>

| col 1 | col 2 | col 3 |
| --- | --- | --- |
| ... | ... | ... |
```

The `docling_idx` value must echo {docling_idx} verbatim (it's the key
the orchestrator uses to find your file). The `canonical_id` is what the
paper calls the table (no "Table " prefix, no period — just the ID).
The `page` is where the table starts.

## Content rules (apply verbatim — these are validated downstream)

- Multi-row headers stay multi-row.
- Grouped row headers (e.g. `"Diarrhea ≥ Grade 3"`) are represented with
  the group label as a row by itself, followed by sub-rows.
- Do NOT add columns to make rows self-describing.
- Preserve numbers, Greek / ≥ / ≤ chars, parenthetical percents exactly.
- If a table spans multiple PDF pages, render it as one continuous table.
- Use the paper's exact wording for caption + cells; no paraphrasing.

## Final report

After writing the file, print one line summary:
- Canonical ID extracted
- Number of rows × columns
- Any ambiguity in caption, content, or table boundary
"""


def _table_pages(table_meta: dict[str, Any], pdf_max_page: int | None = None) -> tuple[int, list[int]]:
    """Return (anchor_page, pages_to_read). pages_to_read is a small window
    around the anchor page for caption + spillover coverage."""
    page = int(table_meta.get("page") or 1)
    lo = max(1, page - 1)
    hi = page + 1
    if pdf_max_page is not None:
        hi = min(hi, pdf_max_page)
    return page, list(range(lo, hi + 1))


def build_prompt(
    pdf_path: Path,
    paper_id: str,
    docling_idx: int,
    table_meta: dict[str, Any],
) -> str:
    anchor, pages = _table_pages(table_meta)
    page_range = (
        f"{pages[0]}-{pages[-1]}" if len(pages) > 1 else str(pages[0])
    )
    return _PROMPT_TEMPLATE.format(
        pdf_path=str(pdf_path),
        paper_id=paper_id,
        docling_idx=docling_idx,
        page=anchor,
        page_range=page_range,
        num_rows=table_meta.get("num_rows", "?"),
        num_cols=table_meta.get("num_cols", "?"),
        docling_caption=table_meta.get("caption", ""),
    )


def _missing_table_indexes(
    paper_runs_dir: Path,
    arbiter_inputs_dir: Path,
) -> list[int]:
    """Docling table indexes whose `sonnet_tables/<idx>.md` is not yet written."""
    docling_tables_path = arbiter_inputs_dir / "docling_tables.json"
    if not docling_tables_path.exists():
        return []
    data = json.loads(docling_tables_path.read_text())
    tables = data if isinstance(data, list) else data.get("tables", [])
    sonnet_dir = paper_runs_dir / "sonnet_tables"
    missing: list[int] = []
    for t in tables:
        idx = int(t.get("index"))
        if not (sonnet_dir / f"{idx}.md").exists():
            missing.append(idx)
    return missing


def _load_table_meta(arbiter_inputs_dir: Path, docling_idx: int) -> dict[str, Any] | None:
    docling_tables_path = arbiter_inputs_dir / "docling_tables.json"
    if not docling_tables_path.exists():
        return None
    data = json.loads(docling_tables_path.read_text())
    tables = data if isinstance(data, list) else data.get("tables", [])
    for t in tables:
        if int(t.get("index")) == docling_idx:
            return t
    return None


def extract_table(
    pdf_path: Path,
    paper_runs_dir: Path,
    arbiter_inputs_dir: Path,
    paper_id: str,
    docling_idx: int,
    max_budget_usd: float = 1.50,
    timeout: int = 900,
) -> dict:
    """Invoke `claude -p` to extract one Docling-indexed table to
    `sonnet_tables/<docling_idx>.md`. Idempotent at the file level.

    Returns dict with: ok, cost_usd, stdout, stderr, returncode, docling_idx.
    """
    pdf_path = Path(pdf_path).resolve()
    paper_runs_dir = Path(paper_runs_dir).resolve()
    arbiter_inputs_dir = Path(arbiter_inputs_dir).resolve()
    (paper_runs_dir / "sonnet_tables").mkdir(parents=True, exist_ok=True)

    out_path = paper_runs_dir / "sonnet_tables" / f"{docling_idx}.md"
    if out_path.exists():
        return {
            "ok": True,
            "cost_usd": None,
            "stdout": f"skip: {out_path.name} already exists",
            "stderr": "",
            "returncode": 0,
            "docling_idx": docling_idx,
        }

    table_meta = _load_table_meta(arbiter_inputs_dir, docling_idx)
    if table_meta is None:
        return {
            "ok": False,
            "cost_usd": None,
            "stdout": "",
            "stderr": f"docling table index {docling_idx} not found in docling_tables.json",
            "returncode": -10,
            "docling_idx": docling_idx,
        }

    prompt = build_prompt(
        pdf_path=pdf_path,
        paper_id=paper_id,
        docling_idx=docling_idx,
        table_meta=table_meta,
    )

    # Sonnet's native 64K output cap (32K default in claude -p) — raise it
    # so a wide table extraction doesn't get truncated mid-write.
    env = os.environ.copy()
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")

    try:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
                "--model", "sonnet",
                "--effort", "medium",
                "--max-budget-usd", f"{max_budget_usd:.2f}",
                "--dangerously-skip-permissions",
                "--output-format", "json",
            ],
            cwd=str(paper_runs_dir),
            timeout=timeout,
            capture_output=True,
            text=True,
            env=env,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        returncode = proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) or ""
        stderr = (e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")) or ""
        stderr = (stderr + f"\nTimeoutExpired after {timeout}s").strip()
        returncode = -1

    cost_usd = _parse_cost(stdout) if stdout else None
    ok = out_path.exists() and returncode == 0

    return {
        "ok": ok,
        "cost_usd": cost_usd,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
        "docling_idx": docling_idx,
    }


def _parse_cost(stdout: str) -> float | None:
    """Pull `total_cost_usd` out of `claude -p --output-format json` stdout."""
    import json as _json
    import re

    if not stdout:
        return None

    s = stdout.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = _json.loads(s)
            v = obj.get("total_cost_usd")
            if isinstance(v, (int, float)):
                return float(v)
        except (ValueError, AttributeError):
            pass

    m = re.search(
        r'"total_cost_usd"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        stdout,
    )
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


