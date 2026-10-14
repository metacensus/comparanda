"""Stage-1 deterministic merge + Stage-2 LLM arbiter for two extractor outputs.

Combines the table-pair alignment, the diff-guide builder, and the merge
orchestration that used to live in three sibling scripts (compare_tables.py,
generate_diff_guide.py, merge_extractions.py).

Public API: `merge_and_arbitrate(...)`. The bundle builder calls this once
per Sonnet-detected table.

Pipeline per table:
1. Parse both extractors' markdown to recover the Table.
2. If only one side has it → passthrough (write arbited markdown directly,
   no LLM call).
3. If both → Stage-1 deterministic merge: value-based row alignment
   (Needleman-Wunsch on cell-bag Jaccard) + greedy column alignment +
   per-cell classification with neighborhood-consistency check.
4. Write merged.json + conflicts.json.
5. If `do_arbitrate` and there are conflicts → build a diff guide, call
   `claude -p` (Sonnet) with the PDF + diff guide + both extractions to
   resolve conflicts, write the arbited markdown.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from paper_prep.claude_cli import build_claude_argv
from paper_prep.markdown_parser import parse_extractor_markdown
from paper_prep.normalize import normalize_strict
from paper_prep.schema import Table


# ────────────────────────────────────────────────────────────────────────────
# Row + column alignment (was compare_tables.py)
# ────────────────────────────────────────────────────────────────────────────


def _cell_bag(row: list[str]) -> Counter:
    bag: Counter[str] = Counter()
    for c in row:
        s = normalize_strict(c)
        if s is None or s == "":
            continue
        bag[s] += 1
    return bag


def _row_score(bag_a: Counter, bag_b: Counter) -> float:
    """Intersection / max(|A|,|B|). Rewards rows that share many values."""
    if not bag_a or not bag_b:
        return 0.0
    inter = sum((bag_a & bag_b).values())
    denom = max(sum(bag_a.values()), sum(bag_b.values()))
    return inter / denom if denom else 0.0


def _align_rows(
    table_a: Table,
    table_b: Table,
    threshold: float = 0.25,
    gap_penalty: float = -0.1,
) -> dict[int, int]:
    """Monotonic row alignment via Needleman-Wunsch global sequence alignment.

    NW preserves row order — two rows with identical content in different
    gene-groups can't get paired across the table, because that would break
    monotonicity. O(n_a * n_b), tiny for sub-300-row tables.
    """
    bags_a = [_cell_bag(r) for r in table_a.rows]
    bags_b = [_cell_bag(r) for r in table_b.rows]
    n_a, n_b = len(bags_a), len(bags_b)

    def pair_score(i: int, j: int) -> float:
        return _row_score(bags_a[i], bags_b[j]) - threshold

    dp = [[0.0] * (n_b + 1) for _ in range(n_a + 1)]
    for i in range(1, n_a + 1):
        dp[i][0] = i * gap_penalty
    for j in range(1, n_b + 1):
        dp[0][j] = j * gap_penalty
    for i in range(1, n_a + 1):
        for j in range(1, n_b + 1):
            dp[i][j] = max(
                dp[i - 1][j - 1] + pair_score(i - 1, j - 1),
                dp[i - 1][j] + gap_penalty,
                dp[i][j - 1] + gap_penalty,
            )

    i, j = n_a, n_b
    row_map: dict[int, int] = {}
    while i > 0 and j > 0:
        diag = dp[i - 1][j - 1] + pair_score(i - 1, j - 1)
        up = dp[i - 1][j] + gap_penalty
        if dp[i][j] == diag:
            overlap = _row_score(bags_a[i - 1], bags_b[j - 1])
            # NW may "align" weak pairs when neighbors boost the DP score —
            # only accept mappings that clear the raw overlap threshold.
            if overlap >= threshold:
                row_map[i - 1] = j - 1
            i -= 1
            j -= 1
        elif dp[i][j] == up:
            i -= 1
        else:
            j -= 1
    return row_map


def _align_cols(
    table_a: Table,
    table_b: Table,
    row_map: dict[int, int],
    min_matches: int = 2,
) -> dict[int, int]:
    """Greedy column alignment using value equality across mapped row pairs."""
    match_counts: defaultdict[tuple[int, int], int] = defaultdict(int)
    for ra, rb in row_map.items():
        row_a = table_a.rows[ra]
        row_b = table_b.rows[rb]
        seen_a: dict[int, str] = {}
        seen_b: dict[int, str] = {}
        for ca, v in enumerate(row_a):
            n = normalize_strict(v)
            if n:
                seen_a[ca] = n
        for cb, v in enumerate(row_b):
            n = normalize_strict(v)
            if n:
                seen_b[cb] = n
        for ca, va in seen_a.items():
            for cb, vb in seen_b.items():
                if va == vb:
                    match_counts[(ca, cb)] += 1

    ranked = sorted(match_counts.items(), key=lambda kv: -kv[1])
    used_a: set[int] = set()
    used_b: set[int] = set()
    col_map: dict[int, int] = {}
    for (ca, cb), cnt in ranked:
        if cnt < min_matches:
            break
        if ca in used_a or cb in used_b:
            continue
        col_map[ca] = cb
        used_a.add(ca)
        used_b.add(cb)
    return col_map


def _strip_ws(s: str | None) -> str | None:
    """Collapse all whitespace in `s`. Used for ws-equivalence detection only —
    storage normalization (paper_prep.normalize) is intentionally lossless."""
    if s is None:
        return None
    return "".join(s.split())


# ────────────────────────────────────────────────────────────────────────────
# Stage-1 deterministic merge (was merge_extractions.py)
# ────────────────────────────────────────────────────────────────────────────


def find_table(md_path: Path, extractor: str, paper_id: str, table_id: str) -> Table | None:
    if not md_path.exists():
        return None
    out = parse_extractor_markdown(md_path, paper_id, extractor)
    for t in out.tables:
        if (t.table_id or "").lower() == table_id.lower():
            return t
    return None


def _render_table_markdown(table: Table) -> str:
    """Render a Table back to GitHub-flavored markdown for passthrough output."""
    headers = list(table.headers)
    if not headers:
        n_cols = max((len(r) for r in table.rows), default=0)
        headers = [f"col_{i}" for i in range(n_cols)]
    lines: list[str] = []
    lines.append("| " + " | ".join(h or "" for h in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in table.rows:
        cells = list(row) + [""] * (len(headers) - len(row))
        cells = cells[: len(headers)]
        cells = [(c or "").replace("|", "\\|") for c in cells]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _passthrough_only(
    table_a: Table | None,
    table_b: Table | None,
    a_label: str,
    b_label: str,
) -> dict:
    """One-sided merge: only one extractor has the table."""
    if (table_a is None) == (table_b is None):
        raise ValueError("_passthrough_only requires exactly one side present")

    side = "a" if table_a is not None else "b"
    table = table_a if table_a is not None else table_b
    label = a_label if side == "a" else b_label
    provenance = f"{side}_only_passthrough"

    headers = list(table.headers)
    grid: list[list[dict]] = []
    n_cols = len(headers) if headers else max((len(r) for r in table.rows), default=0)
    counts: Counter = Counter()
    for r, row in enumerate(table.rows):
        merged_row: list[dict] = []
        for c in range(n_cols):
            val = row[c] if c < len(row) else ""
            cell = {
                "row": r,
                "col": c,
                "value": val if val != "" else None,
                "provenance": provenance,
            }
            counts[provenance] += 1
            merged_row.append(cell)
        grid.append(merged_row)

    return {
        "headers": headers,
        "grid": grid,
        "conflicts": [],
        "counts": dict(counts),
        "row_map": {},
        "col_map": {},
        "passthrough_side": side,
        "passthrough_label": label,
    }


def _deterministic_merge(table_a: Table, table_b: Table) -> dict:
    """Stage-1: merged grid + conflicts list using value-based alignment.

    Per cell at the aligned position:
    - both equal (incl. both null) → take value, mark "both_agree" / "both_null"
    - one null, one not → take the non-null, mark "{a|b}_only"
    - both non-null differ only by whitespace → take A (LLM), mark "ws_merge"
    - both non-null and differ → CONFLICT, candidates kept for stage 2
    - row or col not aligned → unmapped (left as None with provenance)
    """
    row_map = _align_rows(table_a, table_b)
    col_map = _align_cols(table_a, table_b, row_map)

    anchor_rows = table_a.rows
    anchor_headers = table_a.headers
    n_anchor_rows = len(anchor_rows)
    n_anchor_cols = max((len(r) for r in anchor_rows), default=0)

    merged_grid: list[list[dict]] = []
    conflicts: list[dict] = []
    counts: Counter = Counter()

    for r in range(n_anchor_rows):
        merged_row: list[dict] = []
        partner_r = row_map.get(r)
        for c in range(n_anchor_cols):
            partner_c = col_map.get(c)
            a_val = anchor_rows[r][c] if c < len(anchor_rows[r]) else ""
            a_norm = normalize_strict(a_val)
            a_null = a_norm is None or a_norm == ""

            cell: dict = {"row": r, "col": c, "value": None, "provenance": "unmapped"}

            if partner_r is None or partner_c is None:
                cell["value"] = a_val if not a_null else None
                cell["provenance"] = "a_unmapped" if not a_null else "both_null_unmapped"
            else:
                b_row = table_b.rows[partner_r] if partner_r < len(table_b.rows) else []
                b_val = b_row[partner_c] if partner_c < len(b_row) else ""
                b_norm = normalize_strict(b_val)
                b_null = b_norm is None or b_norm == ""

                if a_null and b_null:
                    cell["provenance"] = "both_null"
                    cell["value"] = None
                elif a_norm == b_norm:
                    cell["provenance"] = "both_agree"
                    cell["value"] = a_val
                elif a_null and not b_null:
                    cell["provenance"] = "b_only"
                    cell["value"] = b_val
                elif b_null and not a_null:
                    cell["provenance"] = "a_only"
                    cell["value"] = a_val
                elif _strip_ws(a_norm) == _strip_ws(b_norm):
                    # Whitespace-only difference (e.g. "C/T + T/T" vs
                    # "C/T+T/T"). Not a real conflict — pick A (LLM)
                    # deterministically. Does NOT go to the arbiter.
                    cell["provenance"] = "ws_merge"
                    cell["value"] = a_val
                    cell["candidates"] = {"a": a_val, "b": b_val}
                else:
                    cell["provenance"] = "conflict"
                    cell["value"] = None
                    cell["candidates"] = {"a": a_val, "b": b_val}
                    conflicts.append({
                        "row": r,
                        "col": c,
                        "row_label": (anchor_rows[r][0] if anchor_rows[r] else "").strip(),
                        "col_header": anchor_headers[c] if c < len(anchor_headers) else f"col_{c}",
                        "a_val": a_val,
                        "b_val": b_val,
                        "partner_r": partner_r,
                        "partner_c": partner_c,
                    })

            counts[cell["provenance"]] += 1
            merged_row.append(cell)
        merged_grid.append(merged_row)

    return {
        "headers": anchor_headers,
        "grid": merged_grid,
        "conflicts": conflicts,
        "counts": dict(counts),
        "row_map": {str(k): v for k, v in row_map.items()},
        "col_map": {str(k): v for k, v in col_map.items()},
    }


# ────────────────────────────────────────────────────────────────────────────
# Diff guide for Stage-2 arbiter (was generate_diff_guide.py)
# ────────────────────────────────────────────────────────────────────────────


_HEADER_PATTERN = re.compile(r"^\s*\*\*[^*]+\*\*\s*$")  # **Bold text** alone


def _looks_like_header(value: str | None) -> bool:
    """Bold text in col 0 strongly suggests a group-header row."""
    if not value:
        return False
    return bool(_HEADER_PATTERN.match(value.strip()))


def _extract_first_pass_rows(md_path: Path, table_id: str) -> list[list[str]]:
    """Re-parse the first-pass markdown to recover its raw row contents."""
    out = parse_extractor_markdown(md_path, "x", "x")
    for t in out.tables:
        if (t.table_id or "").lower() == table_id.lower():
            return list(t.rows)
    return []


def _write_diff_guide(
    merged: dict,
    first_pass_md: Path,
    out_path: Path,
    a_label: str,
    b_label: str,
    paper_id: str,
    table_id: str,
) -> None:
    """Write a structured diff guide markdown that the Stage-2 arbiter reads."""
    headers = merged["headers"]
    grid = merged["grid"]

    rows_a = _extract_first_pass_rows(first_pass_md, table_id)
    if not rows_a:
        raise RuntimeError(
            f"Could not re-parse {first_pass_md} for Table {table_id}"
        )

    conflicts: list[dict] = []
    a_unmapped_by_row: dict[int, list[int]] = {}
    a_only_cells: list[dict] = []
    suspect_header_rows: list[dict] = []

    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            prov = cell.get("provenance")
            if prov == "conflict":
                conflicts.append({
                    "row": r,
                    "col": c,
                    "row_label": (rows_a[r][0] if r < len(rows_a) and rows_a[r] else "").strip(),
                    "col_header": headers[c] if c < len(headers) else f"col_{c}",
                    "a_val": cell["candidates"]["a"],
                    "b_val": cell["candidates"]["b"],
                })
            elif prov == "a_unmapped":
                a_unmapped_by_row.setdefault(r, []).append(c)
            elif prov == "a_only":
                a_only_cells.append({
                    "row": r,
                    "col": c,
                    "row_label": (rows_a[r][0] if r < len(rows_a) and rows_a[r] else "").strip(),
                    "col_header": headers[c] if c < len(headers) else f"col_{c}",
                    "a_val": cell["value"],
                })

    for r, row_cells in enumerate(rows_a):
        if not row_cells or not _looks_like_header(row_cells[0]):
            continue
        non_col0_data = [
            (c, v) for c, v in enumerate(row_cells)
            if c > 0 and v and v.strip()
        ]
        if non_col0_data:
            suspect_header_rows.append({
                "row": r,
                "label": row_cells[0].strip(),
                "data_cells": [
                    {
                        "col": c,
                        "col_header": headers[c] if c < len(headers) else f"col_{c}",
                        "value": v.strip(),
                    }
                    for c, v in non_col0_data
                ],
            })

    lines: list[str] = []
    lines.append(f"# Diff guide: `{a_label}` (LLM first-pass) vs `{b_label}` on {paper_id} Table {table_id}")
    lines.append("")
    lines.append(f"This guide highlights places where `{a_label}` and `{b_label}` disagree, "
                 "to focus the review of the LLM first-pass against the source PDF. "
                 "Use this alongside the first-pass markdown and the PDF.")
    lines.append("")

    lines.append(f"## CONFLICTS ({len(conflicts)})")
    lines.append("")
    lines.append("Cells where both extractors have non-null values that disagree.")
    lines.append("")
    if conflicts:
        for c in conflicts:
            lines.append(f"- **row {c['row']}, col {c['col']}** — \"{c['row_label']}\" / \"{c['col_header']}\"")
            lines.append(f"  - `{a_label}`: {c['a_val']!r}")
            lines.append(f"  - `{b_label}`: {c['b_val']!r}")
        lines.append("")
    else:
        lines.append("(none)")
        lines.append("")

    lines.append(f"## LLM HAS ROWS DOCLING LACKS ({len(a_unmapped_by_row)} rows)")
    lines.append("")
    lines.append(f"Rows present in `{a_label}` with no counterpart in `{b_label}`. "
                 "These often indicate sub-rows of a multi-row group that Docling "
                 "collapsed into 1-2 rows, which is a structural-divergence signal.")
    lines.append("")
    if a_unmapped_by_row:
        for r in sorted(a_unmapped_by_row.keys()):
            label = (rows_a[r][0] if r < len(rows_a) and rows_a[r] else "").strip()
            lines.append(f"- row {r}: \"{label}\"")
        lines.append("")
    else:
        lines.append("(none)")
        lines.append("")

    lines.append(f"## SUSPECT HEADER ROWS WITH DATA ({len(suspect_header_rows)})")
    lines.append("")
    lines.append("Rows where col 0 is a bold/category label AND non-first columns "
                 "contain data. **Production rule:** group-header rows should be "
                 "label-only — values placed there are typically misassigned from "
                 "an adjacent sub-row (often the reference / first sub-row of the group).")
    lines.append("")
    if suspect_header_rows:
        for s in suspect_header_rows:
            lines.append(f"- **row {s['row']}** — `{s['label']}` has data in non-col-0 cells:")
            for d in s["data_cells"]:
                lines.append(f"  - col {d['col']} (\"{d['col_header']}\"): {d['value']!r}")
        lines.append("")
    else:
        lines.append("(none — no suspect header rows found)")
        lines.append("")

    lines.append(f"## OTHER A-ONLY CELLS ({len(a_only_cells)})")
    lines.append("")
    lines.append(f"Cells where `{a_label}` has a value at a position where `{b_label}` "
                 "is null at the mapped position. Lower-priority — usually OK if "
                 f"the value is plausibly in the PDF and `{b_label}` simply missed it.")
    lines.append("")
    if a_only_cells:
        for c in a_only_cells:
            lines.append(f"- row {c['row']}, col {c['col']} — \"{c['row_label']}\" / "
                         f"\"{c['col_header']}\": {c['a_val']!r}")
        lines.append("")
    else:
        lines.append("(none)")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))


# ────────────────────────────────────────────────────────────────────────────
# Stage-2 LLM arbiter (was merge_extractions.py --arbitrate branch)
# ────────────────────────────────────────────────────────────────────────────


def _build_arbiter_prompt(
    paper_id: str,
    table_id: str,
    first_pass_md: Path,
    docling_md: Path,
    pdf_path: Path,
    diff_guide_path: Path,
    arbited_path: Path,
) -> str:
    return f"""You are running the **Stage-2 arbitration pass** for paper-prep table extraction.

## Inputs

- LLM first-pass extraction (primary candidate): `{first_pass_md}`
  Find Table {table_id} in this file.
- Docling extraction (secondary, for cross-validation): `{docling_md}`
  Same Table {table_id}.
- Source PDF: `{pdf_path}`
  Read the relevant pages with the Read tool's `pages:` parameter.
- Pre-computed diff guide: `{diff_guide_path}`
  Read this first. It surfaces 4 categories of disagreement.

## Procedure

1. Read the diff guide and locate Table {table_id} in the LLM first-pass.
2. Read the relevant PDF pages with the Read tool's `pages:` parameter.
3. For each item in the diff guide, apply the patterns below and decide.
4. Write the corrected extraction.

## Known patterns (apply these explicitly)

These are the failure modes we've seen across papers. For each suspect-header case in the diff guide, identify which pattern fits, then act accordingly.

### Pattern A — Sparse row-spanning value misplaced on header row (MOVE)

**Symptoms in the table structure:**
- Several bold category-header rows (a row whose only filled cell is col 0, holding the category name in bold).
- Each header has SUB-ROWS below it for its category levels (e.g. levels of a categorical predictor: low / medium / high).
- A particular column (typically a per-group summary statistic such as "Overall P", "Group P", "Trend P") has values **only on the header rows, not on the sub-rows**, and at most ONE value per group — sparse.
- The PDF visually shows that one value once per group, often centered vertically across the sub-rows so the eye can attribute it equally to all of them.

**Convention:** the value applies to the entire group but should sit on the **first sub-row (the reference / baseline category)**, not on the bold header. The header is label-only.

**Action:** MOVE the value from the bold header row to the first sub-row of that group. Apply consistently to all groups that fit the pattern in the same table.

**Illustrative example (synthetic):** A clinical risk-factor table has groups `**Smoking status**` → never / former / current and `**Family history**` → no / yes. Each group has one "Overall P" value visually centered across its sub-rows. The LLM first-pass placed these on the bold header rows. Move each value down onto the first sub-row of its group ("never" and "no" respectively), since those are the reference categories the Overall P is computed against.

### Pattern B — Dense per-header values (KEEP, table convention)

**Symptoms in the table structure:**
- Many bold category-header rows (often dozens — one per item in a long enumerated list of factors, variants, or conditions).
- Each header row has values in the SAME set of non-col-0 columns — a regular, dense pattern repeating across all headers.
- The PDF shows the values directly aligned with the bold-text rows — same vertical position as the bold text, not centered between sub-rows.

**Convention:** the values genuinely belong on the bold header rows. This is the table's convention; the bold row is itself a data row labelled in bold. Sub-rows below carry per-level breakdowns of additional fields (counts, frequencies, etc.).

**Action:** KEEP the values in place.

**Illustrative example (synthetic):** A multi-factor association table reports a battery of test statistics for each of 30 candidate factors. Every bold factor row has values in five "outcome P" columns simultaneously. Sub-rows below carry per-level descriptive statistics for that factor. The bold rows are real data rows; do not move their values.

### How to discriminate A from B

Both look like "bold header has data in non-col-0 cells" at the diff-guide level. The discrimination is structural:

- **A is sparse + selective**: only one (or very few) of many possible non-col-0 columns is populated on each header, and at most one value per group. The other groups in the same table show the same sparse-on-headers pattern.
- **B is dense + regular**: every header row is populated in the same multiple non-col-0 columns. The pattern repeats across many groups.

Read the PDF for ALL the suspect-header rows together, not one by one. If they form a regular dense pattern across the whole table → case B → KEEP. If only one column is populated on the headers and there are sub-rows below → case A → MOVE.

**When in doubt, count:** if the suspect-header rows in the diff guide all populate ONE column and the sub-rows below them are populated in OTHER columns, that's pattern A → move. If they all populate the SAME multi-column set on every header → pattern B → keep.

### Pattern C — OCR encoding artifacts in Docling (TRUST LLM)

**Symptoms:** Docling's value contains null bytes (`\\x00`), Unicode replacement characters, ASCII fallbacks for non-ASCII glyphs (e.g. `þ` in place of `+`, mangled superscript/subscript sequences like `m \\x00 2` where the source has `m⁻²`).

**Action:** Trust the LLM's Unicode rendering. Verify with PDF if uncertain.

### Pattern D — Docling collapsed multi-row groups (TRUST LLM)

**Symptoms:** Diff guide reports several consecutive sub-rows present in the LLM output but absent from Docling. Docling's representation has 1 (or very few) rows where the LLM has 3+ sub-rows under the same category.

**Action:** Trust the LLM's expansion; the sub-rows are real distinct rows in the PDF. Verify with PDF if you have time.

### Pattern E — LLM missed a sparse cell (TRUST DOCLING)

**Symptoms:** A specific cell where Docling has a value and the LLM is null at the mapped position. PDF inspection confirms the cell exists.

**Action:** Take Docling's value. The LLM occasionally misses sparse low-count cells (especially in dense tables with many "Missing" or zero-count entries) when those values blend visually into the row's whitespace.

## CONFLICTS handling

Direct value disagreements: usually Pattern C (OCR encoding) — trust LLM. Otherwise verify with PDF case by case.

## Output

Write the corrected extraction to: `{arbited_path}`

Format:
1. `# {paper_id} — Table {table_id} (Stage-2 arbited extraction)`
2. One sentence describing the procedure
3. `**Table {table_id}.**` caption line
4. The corrected table in GitHub-flavored markdown
5. `## Arbitration notes` section: bullet per change with row, col, what was wrong, what's correct, and **which Pattern** (A/B/C/D/E) triggered the action.

Be consistent: if you decide a table is in Pattern A, apply the move to ALL fitting groups. If Pattern B, leave ALL of them. Don't hedge per-row unless the PDF clearly shows different conventions for different groups within the same table.
"""


def _run_arbiter(
    prompt: str,
    max_budget_usd: float,
    timeout_s: int,
    *,
    arbited_path: Path,
    read_dirs: list[Path],
) -> tuple[int, str, str]:
    """Invoke `claude -p` for Stage-2 arbitration. Returns (rc, stdout, stderr).

    Runs in the output file's directory; the model may read `read_dirs` and
    write only `arbited_path` (see `paper_prep.claude_cli`).
    """
    cwd = arbited_path.resolve().parent
    proc = subprocess.run(
        build_claude_argv(
            prompt,
            cwd=cwd,
            output_path=arbited_path,
            model="sonnet",
            max_budget_usd=max_budget_usd,
            read_dirs=read_dirs,
        ),
        cwd=str(cwd),
        timeout=timeout_s,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _arbited_parses(arbited_path: Path, paper_id: str, table_id: str) -> bool:
    """True if the arbited markdown parses and contains Table `table_id`."""
    if not arbited_path.exists():
        return False
    try:
        out = parse_extractor_markdown(arbited_path, paper_id, "arbited")
    except Exception:  # noqa: BLE001
        return False
    return any((t.table_id or "").lower() == table_id.lower() for t in out.tables)


def _write_passthrough_arbited(
    arbited_path: Path,
    paper_id: str,
    table_id: str,
    passthrough_side: str,
    passthrough_label: str,
    missing_label: str,
    passthrough_table: Table,
) -> None:
    """Write the arbited markdown for a one-sided passthrough (no LLM call)."""
    which_kept = "LLM-only" if passthrough_side == "a" else f"{passthrough_label}-only"
    body_lines = [
        f"# {paper_id} — Table {table_id} ({which_kept}, no cross-validation)",
        "",
        f"> {missing_label} did not extract Table {table_id};"
        f" Stage 2 arbitration was not performed because no second"
        f" extractor was available for cross-validation."
        f" This is the {passthrough_label} first-pass output verbatim.",
        "",
        f"**Table {table_id}.**"
        + (f" {passthrough_table.caption}" if passthrough_table.caption else ""),
        "",
        _render_table_markdown(passthrough_table),
    ]
    if passthrough_table.footnotes:
        body_lines.extend(["", "## Footnotes", ""])
        for fn in passthrough_table.footnotes:
            body_lines.append(f"- {fn}")
    body_lines.append("")
    arbited_path.parent.mkdir(parents=True, exist_ok=True)
    arbited_path.write_text("\n".join(body_lines))


# ────────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────────


def merge_and_arbitrate(
    *,
    paper_id: str,
    table_id: str,
    sonnet_md: Path,
    docling_md: Path,
    pdf_path: Path,
    merged_out: Path,
    arbited_out: Path,
    diff_guide_out: Path | None = None,
    conflicts_out: Path | None = None,
    do_arbitrate: bool = True,
    max_budget_usd: float = 0.75,
    arbiter_timeout_s: int = 600,
    a_label: str = "sonnet",
    b_label: str = "docling",
) -> dict:
    """Stage-1 deterministic merge + optional Stage-2 LLM arbitration for one table.

    Inputs are explicit file paths — no staging-dir / shootout-dir convention.
    Outputs:
    - `merged_out` always written (Stage-1 merge JSON)
    - `conflicts_out` always written (defaults to `<merged_out>.conflicts.json`)
    - `arbited_out` written when: (a) one-sided passthrough, or (b) two-sided
      with `do_arbitrate=True` and arbiter succeeded
    - `diff_guide_out` written when two-sided + `do_arbitrate=True` (defaults
      to `<merged_out.parent>/<merged_out.stem>_arbitration/diff_guide.md`)

    Returns the merge result dict (headers, grid, conflicts, counts, etc.),
    augmented with `arbiter_ok` and `arbiter_cost_usd` when arbitration ran.
    """
    # Resolved paths: the arbiter's write permission is matched against the
    # resolved output path (e.g. /tmp -> /private/tmp on macOS), so the
    # prompt must name the same resolved path.
    sonnet_md = Path(sonnet_md).resolve()
    docling_md = Path(docling_md).resolve()
    pdf_path = Path(pdf_path).resolve()
    merged_out = Path(merged_out).resolve()
    arbited_out = Path(arbited_out).resolve()

    table_a = find_table(sonnet_md, a_label, paper_id, table_id)
    table_b = find_table(docling_md, b_label, paper_id, table_id)

    if table_a is None and table_b is None:
        raise RuntimeError(
            f"No Table {table_id} in either {sonnet_md.name} or {docling_md.name}"
        )

    one_sided = (table_a is None) ^ (table_b is None)
    if one_sided:
        result = _passthrough_only(table_a, table_b, a_label, b_label)
    else:
        result = _deterministic_merge(table_a, table_b)
    result["paper"] = paper_id
    result["table_id"] = table_id
    result["a"] = a_label
    result["b"] = b_label

    merged_out.parent.mkdir(parents=True, exist_ok=True)
    merged_out.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    if conflicts_out is None:
        conflicts_out = merged_out.with_suffix(".conflicts.json")
    conflicts_out.write_text(json.dumps({
        "paper": paper_id,
        "table_id": table_id,
        "a": a_label,
        "b": b_label,
        "conflicts": result["conflicts"],
    }, indent=2, ensure_ascii=False))

    if one_sided:
        side = result["passthrough_side"]
        passthrough_label = result["passthrough_label"]
        passthrough_table = table_a if side == "a" else table_b
        missing_label = b_label if side == "a" else a_label
        _write_passthrough_arbited(
            arbited_out, paper_id, table_id, side,
            passthrough_label, missing_label, passthrough_table,
        )
        return result

    if not do_arbitrate:
        return result

    if diff_guide_out is None:
        diff_guide_out = (
            merged_out.parent / f"{merged_out.stem}_arbitration" / "diff_guide.md"
        )
    diff_guide_out = Path(diff_guide_out).resolve()
    _write_diff_guide(
        merged=result,
        first_pass_md=sonnet_md,
        out_path=diff_guide_out,
        a_label=a_label,
        b_label=b_label,
        paper_id=paper_id,
        table_id=table_id,
    )

    prompt = _build_arbiter_prompt(
        paper_id=paper_id,
        table_id=table_id,
        first_pass_md=sonnet_md,
        docling_md=docling_md,
        pdf_path=pdf_path,
        diff_guide_path=diff_guide_out,
        arbited_path=arbited_out,
    )

    arbited_out.parent.mkdir(parents=True, exist_ok=True)
    rc, stdout, stderr = _run_arbiter(
        prompt,
        max_budget_usd,
        arbiter_timeout_s,
        arbited_path=arbited_out,
        read_dirs=[p.resolve().parent for p in (sonnet_md, docling_md, pdf_path, diff_guide_out)],
    )

    cost: float | None = None
    if stdout:
        try:
            obj = json.loads(stdout.strip())
            v = obj.get("total_cost_usd")
            if isinstance(v, (int, float)):
                cost = float(v)
        except (ValueError, AttributeError):
            m = re.search(r'"total_cost_usd"\s*:\s*([0-9.]+)', stdout)
            if m:
                try:
                    cost = float(m.group(1))
                except ValueError:
                    pass

    # The arbiter has no shell, so the parse check the prompt used to ask
    # the model to run happens here instead.
    parses = _arbited_parses(arbited_out, paper_id, table_id)
    if arbited_out.exists() and not parses:
        print(f"[warn] {arbited_out.name} does not parse to Table {table_id}")
    result["arbiter_ok"] = parses and rc == 0
    result["arbiter_cost_usd"] = cost
    result["arbiter_returncode"] = rc

    if stdout:
        print("--- claude -p stdout ---")
        print(stdout[-4000:])
        print("--- end claude stdout ---")
    if rc != 0 and stderr:
        print(f"[error] claude -p stderr: {stderr[-2000:]}")

    return result
