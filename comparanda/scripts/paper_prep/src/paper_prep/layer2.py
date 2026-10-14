"""Build a paper-prep manifest bundle from the tables-only minimal pipeline outputs.

Reads Docling's section split directly for prose (no prose verification step)
and wires each per-table Sonnet output into `paper_prep.arbitrate` for
Stage-1 deterministic merge + optional Stage-2 LLM arbiter cross-validation.

Inputs (per paper, under <run_root>/):
    paper_runs/<paper>/sonnet_tables/<docling_idx>.md   (per-table Sonnet first-pass)
    paper_runs/<paper>/docling_pictures.json + docling_pictures/*.png
    paper_runs/<paper>/docling_sections.json
    arbiter_inputs/<paper>/docling.md                   (whole-paper Docling)
    arbiter_inputs/<paper>/docling_tables.json
    arbiter_inputs/<paper>/docling_sections_full/<NN>_<slug>.md

Outputs (under <output_root>/<paper_id>/):
    manifest.json
    sections/<slug>.md           ← Docling raw (no prose verification)
    tables/table_<canonical>.{json,md}
    figures/figure_<N>.png       ← filtered via _is_decorative
    _provenance/
        docling_tables.json
        docling_pictures.json
        sonnet.md                ← concatenated per-table Sonnet outputs
        sonnet_tables/<docling_idx>.md
        merged_table_<id>.json
        merged_table_<id>.conflicts.json
        merged_table_<id>_arbitration/diff_guide.md   (if --arbitrate)
        arbitration_notes_table_<id>.md               (if --arbitrate)

Arbitration: --arbitrate is ON by default. Use --no-arbitrate for a fast
smoke that skips the LLM step (deterministic merge only — still gives
dual-extractor cross-validation, just no Sonnet conflict resolution).

Usage:
    uv run python -m paper_prep.layer2 <paper_id> \\
        --run-root <run_root> \\
        --output-root <bundle_root> \\
        --pdf <pdf_path> \\
        [--arbitrate / --no-arbitrate]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Any

from paper_prep import arbitrate
from paper_prep.prep import (
    _figure_label,
    _is_decorative,
    _parse_table_from_arbited,
    _split_arbitration_notes,
    _table_id_sort_key,
)


_NOT_A_TABLE_MARKER = "# NOT A TABLE"

# Structured metadata block at the top of every sonnet_tables/<idx>.md.
# Format:
#   <!-- paper-prep-meta
#   docling_idx: 5
#   canonical_id: S2
#   page: 11
#   -->
_META_BLOCK_RE = re.compile(
    r"<!--\s*paper-prep-meta\s*(.*?)\s*-->",
    re.DOTALL | re.IGNORECASE,
)


def _read_table_meta(md_path: Path) -> dict[str, str]:
    """Read the paper-prep-meta comment block at the top of a sonnet table
    file. Returns {} if absent or malformed."""
    if not md_path.exists():
        return {}
    text = md_path.read_text()
    m = _META_BLOCK_RE.search(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip()
    return out


def _strip_meta_and_h1(body: str) -> str:
    """Drop the meta block + Sonnet's `# paper_id — Table N (Sonnet first-pass)`
    H1 line + any leading blank lines from a sonnet table file."""
    body = _META_BLOCK_RE.sub("", body, count=1)
    lines = body.splitlines()
    start = 0
    while start < len(lines):
        s = lines[start].strip()
        if not s:
            start += 1
            continue
        if s.startswith("# ") and "Sonnet first-pass" in s:
            start += 1
            continue
        break
    return "\n".join(lines[start:])


# Match the first `**(Supplementary )?Table N.**` caption line so we can
# rewrite the prefix to use the canonical ID from the meta block. This
# only handles caption STRUCTURE (regex on a tight format) — the canonical
# ID itself comes from the meta block, not from this match.
_CAPTION_REWRITE_RE = re.compile(
    r"^(\s*)\*\*\s*(?:Supplementary\s+)?Table\s+(?:\d+[A-Za-z]?|[A-Za-z]\d+[A-Za-z]?)\s*\.\s*\*\*\s*(.*)$",
    re.IGNORECASE,
)


def _rewrite_caption_inline(body: str, canonical_id: str) -> str:
    """Rewrite the first caption line to `Table <canonical_id>. <rest>` (NOT bold)
    inline so parse_extractor_markdown's caption capture is clean. The non-bold
    form avoids a parser quirk where the closing `**` of a bold marker ends up
    in the captured caption text."""
    out: list[str] = []
    rewritten = False
    for line in body.splitlines():
        if not rewritten:
            m = _CAPTION_REWRITE_RE.match(line)
            if m:
                lead, rest = m.group(1), m.group(2)
                line = f"{lead}Table {canonical_id}. {rest}".rstrip()
                rewritten = True
        out.append(line)
    return "\n".join(out)


def _is_not_a_table(md_path: Path) -> bool:
    if not md_path.exists():
        return False
    head = md_path.read_text().splitlines()[:1]
    return bool(head) and head[0].strip().upper() == _NOT_A_TABLE_MARKER.upper()


def _build_combined_sonnet_md(
    sonnet_tables_dir: Path,
    out_path: Path,
    paper_id: str,
) -> dict[str, str]:
    """Concat every valid `sonnet_tables/<idx>.md` into one synthetic
    extractor-style markdown that `parse_extractor_markdown` can read.
    Each table gets a canonical `Table <canonical_id>.` caption line
    constructed from the meta block.

    Returns {canonical_id: docling_idx} mapping for valid tables.
    """
    id_map: dict[str, str] = {}
    parts: list[str] = [f"# {paper_id} (Sonnet first-pass extraction)\n"]
    for md_path in sorted(
        sonnet_tables_dir.glob("*.md"),
        key=lambda p: int(p.stem) if p.stem.isdigit() else 999,
    ):
        if _is_not_a_table(md_path):
            continue
        meta = _read_table_meta(md_path)
        canon = meta.get("canonical_id")
        if not canon:
            print(f"  [warn] missing canonical_id in meta block: {md_path.name} — skipping")
            continue
        if canon in id_map:
            print(f"  [warn] duplicate canonical ID {canon!r} — keeping {id_map[canon]}, dropping {md_path.stem}")
            continue
        id_map[canon] = md_path.stem

        body = _strip_meta_and_h1(md_path.read_text())
        body = _rewrite_caption_inline(body, canon)
        parts.append(body.lstrip("\n"))
        parts.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(parts))
    return id_map


def build(
    paper_id: str,
    run_root: Path,
    output_root: Path,
    pdf_path: Path,
    do_arbitrate: bool = True,
) -> Path:
    """Build the bundle for one paper. Returns the manifest path.

    The output bundle replaces any existing bundle at <output_root>/<paper_id>/.
    """
    run_root = Path(run_root).resolve()
    output_root = Path(output_root).resolve()
    pdf_path = Path(pdf_path).resolve()

    paper_runs_dir = run_root / "paper_runs" / paper_id
    arbiter_inputs_dir = run_root / "arbiter_inputs" / paper_id

    if not (paper_runs_dir / "docling_sections.json").exists():
        raise FileNotFoundError(
            f"docling_sections.json not found under {paper_runs_dir} — run step_docling first"
        )
    docling_md = arbiter_inputs_dir / "docling.md"
    if not docling_md.exists():
        raise FileNotFoundError(f"docling.md not found at {docling_md}")

    bundle_dir = output_root / paper_id
    sections_dir = bundle_dir / "sections"
    tables_dir = bundle_dir / "tables"
    figures_dir = bundle_dir / "figures"
    prov_dir = bundle_dir / "_provenance"
    prov_sonnet_dir = prov_dir / "sonnet_tables"

    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    for d in (sections_dir, tables_dir, figures_dir, prov_dir, prov_sonnet_dir):
        d.mkdir(parents=True, exist_ok=True)

    # ── Stage 0: concatenate per-table Sonnet outputs into one md ────────
    sonnet_tables_dir = paper_runs_dir / "sonnet_tables"
    sonnet_combined = paper_runs_dir / "sonnet_combined.md"
    canon_to_idx = _build_combined_sonnet_md(sonnet_tables_dir, sonnet_combined, paper_id)
    print(f"[{paper_id}] {len(canon_to_idx)} Sonnet tables: {sorted(canon_to_idx.keys(), key=_table_id_sort_key)}")

    # ── Stage 1+2: merge + arbitrate per table (library call) ────────────
    arbited_md_by_id: dict[str, Path] = {}
    for canonical_id in sorted(canon_to_idx.keys(), key=_table_id_sort_key):
        merged_out = paper_runs_dir / f"merged_table_{canonical_id}.json"
        arbited_out = paper_runs_dir / f"arbited_table_{canonical_id}.md"
        diff_guide_out = (
            paper_runs_dir / f"merged_table_{canonical_id}_arbitration" / "diff_guide.md"
        )
        print(f"  [table {canonical_id}] {'merge + arbiter' if do_arbitrate else 'merge only'}...", flush=True)
        try:
            result = arbitrate.merge_and_arbitrate(
                paper_id=paper_id,
                table_id=canonical_id,
                sonnet_md=sonnet_combined,
                docling_md=docling_md,
                pdf_path=pdf_path,
                merged_out=merged_out,
                arbited_out=arbited_out,
                diff_guide_out=diff_guide_out,
                do_arbitrate=do_arbitrate,
            )
        except Exception as e:  # noqa: BLE001
            print(f"    FAIL: {type(e).__name__}: {e}")
            continue

        counts = result.get("counts") or {}
        summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        print(f"    counts: {summary or '(empty)'}; conflicts={len(result.get('conflicts') or [])}")
        if "arbiter_cost_usd" in result and result["arbiter_cost_usd"] is not None:
            print(f"    arbiter cost: ${result['arbiter_cost_usd']:.4f}")

        if arbited_out.exists():
            arbited_md_by_id[canonical_id] = arbited_out

    # ── Bundle: sections from Docling raw (no prose verification) ────────
    docling_sections_full_dir = arbiter_inputs_dir / "docling_sections_full"
    docling_sections_index = json.loads(
        (paper_runs_dir / "docling_sections.json").read_text()
    )
    title = docling_sections_index.get("title")
    section_entries: list[dict[str, Any]] = []
    for sec in docling_sections_index.get("sections", []):
        slug = sec["slug"]
        nn = int(sec["index"])
        src = docling_sections_full_dir / f"{nn:02d}_{slug}.md"
        if not src.exists():
            print(f"  [warn] missing section file: {src.name}")
            continue
        body = src.read_text()
        out_md = sections_dir / f"{slug}.md"
        # If two sections share a slug, suffix with the index.
        if out_md.exists():
            out_md = sections_dir / f"{slug}__sec{nn:02d}.md"
        out_md.write_text(body)
        rel = out_md.relative_to(bundle_dir).as_posix()
        heading = (sec.get("headings") or [None])[-1]
        section_entries.append({
            "name": out_md.stem,
            "path": rel,
            "heading": heading,
            "page_range": sec.get("page_range"),
        })

    # ── Bundle: tables from arbited outputs ──────────────────────────────
    table_entries: list[dict[str, Any]] = []
    docling_tables_path = arbiter_inputs_dir / "docling_tables.json"
    docling_tables_blob = (
        json.loads(docling_tables_path.read_text()) if docling_tables_path.exists() else {}
    )
    docling_tables = docling_tables_blob.get("tables") or []
    docling_by_idx = {int(t.get("index")): t for t in docling_tables}

    for canonical_id in sorted(arbited_md_by_id.keys(), key=_table_id_sort_key):
        arb_path = arbited_md_by_id[canonical_id]
        arb_md = arb_path.read_text()
        body_md, notes_md = _split_arbitration_notes(arb_md)
        headers, rows, title_from_arb = _parse_table_from_arbited(body_md, canonical_id)

        docling_idx_str = canon_to_idx.get(canonical_id)
        docling_idx = int(docling_idx_str) if docling_idx_str and docling_idx_str.isdigit() else None
        page = None
        if docling_idx is not None and docling_idx in docling_by_idx:
            page = docling_by_idx[docling_idx].get("page")

        table_json = {
            "id": canonical_id,
            "title": title_from_arb,
            "page": page,
            "headers": headers,
            "rows": rows,
        }
        (tables_dir / f"table_{canonical_id}.json").write_text(
            json.dumps(table_json, indent=2, ensure_ascii=False)
        )
        (tables_dir / f"table_{canonical_id}.md").write_text(body_md)

        table_entries.append({
            "id": canonical_id,
            "title": title_from_arb,
            "page": page,
            "path": f"tables/table_{canonical_id}.json",
            "markdown_path": f"tables/table_{canonical_id}.md",
            "cross_validated": True,
            "docling_idx": docling_idx,
        })

        merged_out = paper_runs_dir / f"merged_table_{canonical_id}.json"
        if merged_out.exists():
            shutil.copy2(merged_out, prov_dir / merged_out.name)
        conflicts_path = merged_out.with_suffix(".conflicts.json")
        if conflicts_path.exists():
            shutil.copy2(conflicts_path, prov_dir / conflicts_path.name)
        arbitration_bundle = paper_runs_dir / f"merged_table_{canonical_id}_arbitration"
        if arbitration_bundle.exists():
            shutil.copytree(
                arbitration_bundle,
                prov_dir / arbitration_bundle.name,
                dirs_exist_ok=True,
            )
        if notes_md:
            (prov_dir / f"arbitration_notes_table_{canonical_id}.md").write_text(notes_md)

    # ── Bundle: figures from Docling, filtered via _is_decorative ───────
    docling_pictures_blob = json.loads(
        (paper_runs_dir / "docling_pictures.json").read_text()
    )
    pictures = docling_pictures_blob if isinstance(docling_pictures_blob, list) else docling_pictures_blob.get("pictures", [])
    pictures_src_dir = paper_runs_dir / "docling_pictures"

    figure_entries: list[dict[str, Any]] = []
    kept_idx = 0
    for p in pictures:
        idx = int(p.get("index"))
        page = p.get("page")
        bbox = p.get("bbox")
        caption = p.get("caption") or ""
        if _is_decorative(caption, page, bbox):
            continue
        src = pictures_src_dir / f"picture_{idx}.png"
        if not src.exists():
            continue
        dst = figures_dir / f"figure_{kept_idx}.png"
        shutil.copy2(src, dst)
        figure_entries.append({
            "id": kept_idx,
            "label": _figure_label(caption, kept_idx + 1),
            "page": page,
            "caption": caption.strip() or None,
            "path": f"figures/figure_{kept_idx}.png",
            "docling_idx": idx,
        })
        kept_idx += 1

    # ── Provenance ───────────────────────────────────────────────────────
    shutil.copy2(arbiter_inputs_dir / "docling_tables.json", prov_dir / "docling_tables.json")
    shutil.copy2(paper_runs_dir / "docling_pictures.json", prov_dir / "docling_pictures.json")
    for st in sonnet_tables_dir.glob("*.md"):
        shutil.copy2(st, prov_sonnet_dir / st.name)
    shutil.copy2(sonnet_combined, prov_dir / "sonnet.md")

    # ── Manifest ─────────────────────────────────────────────────────────
    manifest = {
        "paper_id": paper_id,
        "source_pdf": str(pdf_path),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "title": title,
        "sections": section_entries,
        "tables": table_entries,
        "figures": figure_entries,
        "pipeline": "paper-prep-minimal-v0",
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    print(
        f"[{paper_id}] bundle ready at {bundle_dir} "
        f"({len(section_entries)} sections, {len(table_entries)} tables, "
        f"{len(figure_entries)} figures kept)"
    )
    return manifest_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paper_id")
    ap.add_argument("--run-root", required=True, type=Path)
    ap.add_argument("--output-root", required=True, type=Path)
    ap.add_argument("--pdf", required=True, type=Path)
    ap.add_argument(
        "--arbitrate",
        dest="arbitrate",
        action="store_true",
        default=True,
        help="Run Stage-2 LLM arbiter per table (default)",
    )
    ap.add_argument(
        "--no-arbitrate",
        dest="arbitrate",
        action="store_false",
        help="Skip Stage-2 arbiter; deterministic merge only",
    )
    args = ap.parse_args()

    build(
        paper_id=args.paper_id,
        run_root=args.run_root,
        output_root=args.output_root,
        pdf_path=args.pdf,
        do_arbitrate=args.arbitrate,
    )


if __name__ == "__main__":
    main()
