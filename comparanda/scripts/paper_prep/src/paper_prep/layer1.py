"""Per-paper pipeline runner (tables-only minimal first-pass).

Orchestrates, for one paper:

    1. Docling extraction (split layout — preserves Phase 4's extract_split)
        paper_runs/<paper>/{docling_pictures, docling_pictures.json,
                            docling_sections/, docling_sections.json}
        arbiter_inputs/<paper>/{docling.md, docling_tables.json,
                                  docling_sections_full/}
    2. Sonnet first-pass — one `claude -p` subprocess PER Docling table,
       fanned out in parallel batches of N (default 4). Each worker
       writes its assigned table's `sonnet_tables/<docling_idx>.md`. No
       prose verification, no figure descriptions, no edit JSONs.
    3. Run status -> paper_runs/<paper>/run_status.json

Per-table dispatch (smallest possible per-call context) avoids the
32K output cap that killed Phase 4 verify-and-patch on long papers.
See `docs/phase4_findings.md` for the trajectory + revival roadmap.

Each step is idempotent. The first-pass resumes at per-table granularity
(tables with a `sonnet_tables/<docling_idx>.md` already on disk are
skipped). Failed tables are retried once before being reported as failures.

Steps 3+ (per-table arbitration + bundle build) get wired in by P5 + P6.

Usage:
    uv run python scripts/run_paper.py <paper_id> \\
        --pdf <path> \\
        --run-root <path> \\
        [--table-batch-size 4] \\
        [--per-table-budget-usd 1.50] \\
        [--per-table-timeout 900]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import sys
from pathlib import Path

from paper_prep.extractors import claude_extractor, docling_extractor

TABLE_BATCH_SIZE = 4
PER_TABLE_BUDGET_USD = 1.50
PER_TABLE_TIMEOUT_S = 900


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def step_docling(pdf: Path, paper_runs_dir: Path, arbiter_inputs_dir: Path) -> dict:
    """Idempotent at the docling_sections.json level."""
    sections_index = paper_runs_dir / "docling_sections.json"
    if sections_index.exists():
        return {"ok": True, "skipped": True}
    try:
        docling_extractor.extract_split(pdf, paper_runs_dir, arbiter_inputs_dir)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "skipped": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": sections_index.exists(), "skipped": False}


def parallel_table_extract(
    pdf: Path,
    paper_runs_dir: Path,
    arbiter_inputs_dir: Path,
    paper_id: str,
    batch_size: int = TABLE_BATCH_SIZE,
    per_table_budget_usd: float = PER_TABLE_BUDGET_USD,
    per_table_timeout: int = PER_TABLE_TIMEOUT_S,
) -> dict:
    """Fan out per-table claude -p subprocesses with up to `batch_size`
    concurrent workers. Retry failed tables once. Return aggregate result.
    """
    missing = claude_extractor._missing_table_indexes(paper_runs_dir, arbiter_inputs_dir)
    if not missing:
        return {
            "ok": True,
            "cost_usd": 0.0,
            "per_table": [],
            "tables_to_process": [],
            "tables_remaining": [],
        }

    print(
        f"[{paper_id}] first-pass: {len(missing)} tables, "
        f"batch_size={batch_size}, per-table budget ${per_table_budget_usd:.2f}",
        flush=True,
    )

    def _run_one(docling_idx: int) -> tuple[int, dict]:
        try:
            res = claude_extractor.extract_table(
                pdf_path=pdf,
                paper_runs_dir=paper_runs_dir,
                arbiter_inputs_dir=arbiter_inputs_dir,
                paper_id=paper_id,
                docling_idx=docling_idx,
                max_budget_usd=per_table_budget_usd,
                timeout=per_table_timeout,
            )
        except Exception as e:  # noqa: BLE001 — never let one worker kill the batch
            res = {
                "ok": False,
                "cost_usd": None,
                "stdout": "",
                "stderr": f"{type(e).__name__}: {e}",
                "returncode": -2,
                "docling_idx": docling_idx,
            }
        cost = res.get("cost_usd")
        status = "OK" if res.get("ok") else f"FAIL(rc={res.get('returncode')})"
        cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "?"
        print(f"  [table {docling_idx:>2}] {status} cost={cost_str}", flush=True)
        return docling_idx, res

    def _drain(futs: dict) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for fut in concurrent.futures.as_completed(futs):
            try:
                idx, res = fut.result()
            except Exception as e:  # noqa: BLE001
                idx = futs[fut]
                res = {
                    "ok": False, "cost_usd": None,
                    "stdout": "", "stderr": f"future raised {type(e).__name__}: {e}",
                    "returncode": -3, "docling_idx": idx,
                }
                print(f"  [table {idx:>2}] FAIL(future-raise) {e}", flush=True)
            out[idx] = res
        return out

    results: dict[int, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
        futs = {ex.submit(_run_one, idx): idx for idx in missing}
        results.update(_drain(futs))

    failed = [i for i, r in results.items() if not r.get("ok")]
    if failed:
        print(f"[{paper_id}] retrying {len(failed)} failed tables: {failed}", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
            futs = {ex.submit(_run_one, idx): idx for idx in failed}
            retry_results = _drain(futs)
        for idx, res in retry_results.items():
            prev_cost = results[idx].get("cost_usd") or 0.0
            this_cost = res.get("cost_usd") or 0.0
            res["cost_usd"] = prev_cost + this_cost
            results[idx] = res

    total_cost = sum((r.get("cost_usd") or 0.0) for r in results.values())
    tables_remaining = claude_extractor._missing_table_indexes(paper_runs_dir, arbiter_inputs_dir)
    per_table = [
        {
            "docling_idx": i,
            "ok": results[i].get("ok"),
            "returncode": results[i].get("returncode"),
            "cost_usd": (
                round(results[i].get("cost_usd"), 4)
                if isinstance(results[i].get("cost_usd"), (int, float))
                else None
            ),
        }
        for i in sorted(results.keys())
    ]

    return {
        "ok": not tables_remaining,
        "cost_usd": round(total_cost, 4),
        "per_table": per_table,
        "tables_to_process": missing,
        "tables_remaining": tables_remaining,
    }


def run(
    paper_id: str,
    pdf: Path,
    run_root: Path,
    batch_size: int = TABLE_BATCH_SIZE,
    per_table_budget_usd: float = PER_TABLE_BUDGET_USD,
    per_table_timeout: int = PER_TABLE_TIMEOUT_S,
) -> dict:
    pdf = Path(pdf).resolve()
    run_root = Path(run_root).resolve()
    paper_runs_dir = run_root / "paper_runs" / paper_id
    arbiter_inputs_dir = run_root / "arbiter_inputs" / paper_id
    paper_runs_dir.mkdir(parents=True, exist_ok=True)
    arbiter_inputs_dir.mkdir(parents=True, exist_ok=True)

    started_at = _now_iso()
    errors: list[str] = []
    first_pass_cost = 0.0

    if not pdf.exists():
        return _write_status(
            paper_runs_dir, paper_id, pdf, started_at, _now_iso(),
            first_pass_cost, [f"PDF not found: {pdf}"], None,
        )

    print(f"[{paper_id}] step 1: docling split extraction")
    d_res = step_docling(pdf, paper_runs_dir, arbiter_inputs_dir)
    if not d_res.get("ok"):
        errors.append(f"docling: {d_res.get('error', 'failed')}")
        return _write_status(
            paper_runs_dir, paper_id, pdf, started_at, _now_iso(),
            first_pass_cost, errors, None,
        )

    print(f"[{paper_id}] step 2: sonnet first-pass (per-table parallel)")
    fp_res = parallel_table_extract(
        pdf=pdf,
        paper_runs_dir=paper_runs_dir,
        arbiter_inputs_dir=arbiter_inputs_dir,
        paper_id=paper_id,
        batch_size=batch_size,
        per_table_budget_usd=per_table_budget_usd,
        per_table_timeout=per_table_timeout,
    )
    first_pass_cost = float(fp_res.get("cost_usd") or 0.0)
    if not fp_res.get("ok"):
        errors.append(
            f"first_pass: tables_remaining={fp_res.get('tables_remaining')}"
        )

    return _write_status(
        paper_runs_dir, paper_id, pdf, started_at, _now_iso(),
        first_pass_cost, errors, fp_res,
    )


def _write_status(
    paper_runs_dir: Path,
    paper_id: str,
    pdf: Path,
    started_at: str,
    ended_at: str,
    first_pass_cost: float,
    errors: list[str],
    fp_res: dict | None,
) -> dict:
    status = {
        "paper": paper_id,
        "pdf": str(pdf),
        "started_at": started_at,
        "ended_at": ended_at,
        "first_pass_cost_usd": round(first_pass_cost, 4),
        "errors": errors,
    }
    if fp_res is not None:
        status["first_pass"] = {
            "ok": fp_res.get("ok"),
            "tables_to_process": fp_res.get("tables_to_process"),
            "tables_remaining": fp_res.get("tables_remaining"),
            "per_table": fp_res.get("per_table"),
        }
    paper_runs_dir.mkdir(parents=True, exist_ok=True)
    out = paper_runs_dir / "run_status.json"
    out.write_text(json.dumps(status, indent=2, ensure_ascii=False))
    print(f"[{paper_id}] wrote {out}")
    return status


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paper_id", help="Stable identifier for the paper (used as directory name)")
    ap.add_argument("--pdf", required=True, type=Path, help="Path to source PDF")
    ap.add_argument(
        "--run-root",
        required=True,
        type=Path,
        help="Run root (paper_runs/<paper_id>/ and arbiter_inputs/<paper_id>/ written underneath)",
    )
    ap.add_argument(
        "--table-batch-size",
        type=int,
        default=TABLE_BATCH_SIZE,
        help=f"Max concurrent per-table claude -p workers (default: {TABLE_BATCH_SIZE})",
    )
    ap.add_argument(
        "--per-table-budget-usd",
        type=float,
        default=PER_TABLE_BUDGET_USD,
        help=f"Per-table claude -p budget cap (default: {PER_TABLE_BUDGET_USD})",
    )
    ap.add_argument(
        "--per-table-timeout",
        type=int,
        default=PER_TABLE_TIMEOUT_S,
        help=f"Per-table subprocess timeout in seconds (default: {PER_TABLE_TIMEOUT_S})",
    )
    args = ap.parse_args()

    status = run(
        paper_id=args.paper_id,
        pdf=args.pdf,
        run_root=args.run_root,
        batch_size=args.table_batch_size,
        per_table_budget_usd=args.per_table_budget_usd,
        per_table_timeout=args.per_table_timeout,
    )

    if status.get("errors"):
        sys.exit(1)


if __name__ == "__main__":
    main()
