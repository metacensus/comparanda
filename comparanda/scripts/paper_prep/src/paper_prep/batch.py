"""Batch runner for paper-prep: bundle a corpus of PDFs in one go.

RUN THIS FROM A REGULAR TERMINAL — NOT from inside a Claude Code session.
The Sonnet first-pass + arbiter steps invoke `claude -p` as subprocesses
which need OAuth keychain auth. Nested invocations from inside an outer
Claude Code session are slow and broken.

Usage:
    uv run python -m paper_prep.batch \\
        --subset examples/example_batch_subset.json \\
        --pdfs-dir /path/to/pdfs \\
        --bundle-out "$COMPARANDA_PROJECT_DIR/data/processed/" \\
        [--run-root /tmp/paper_prep_batch] \\
        [--per-table-budget-usd 1.50] \\
        [--table-batch-size 4] \\
        [--total-budget-usd 120.00] \\
        [--max-concurrent 1] \\
        [--no-arbitrate]

Subset JSON shape (one entry per paper):
    {
      "papers": [
        {"pmid": 19530960, "survey_key": "Amstutz_2009_19530960"},
        ...
      ]
    }

The `pmid` is resolved against `--pdfs-dir` by `paper_prep.pdf_index`,
which expects filenames with the PMID in the last `[...]` group (e.g.
`Amstutz 2009 (Pharmacogenomics) [19530960].pdf`). `survey_key` becomes
the paper_id used for the bundle directory name and for intermediates
under `--run-root`.

Outputs:
    <run-root>/paper_runs/<paper_id>/  (intermediates per paper)
    <run-root>/arbiter_inputs/<paper_id>/
    <bundle-out>/<paper_id>/  (final bundle)
    <bundle-out>/_run_summary.csv  (per-paper status row)
    <run-root>/logs/<paper_id>.log  (captured per-paper log)

Idempotency:
    Bundle-level: if <bundle-out>/<paper_id>/manifest.json already exists,
    the paper is skipped (status=skipped_existing).
    Per-table-Sonnet level: layer1 skips any sonnet_tables/<idx>.md
    already on disk.
    Bundle build level: layer2 wipes + rebuilds the bundle dir.

Graceful stop:
    `touch <run-root>/STOP`. Workers check this between papers; in-flight
    papers complete, queued papers exit fast, batch returns cleanly.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from paper_prep import layer1, layer2
from paper_prep.pdf_index import build_pdf_index


CSV_HEADER = [
    "paper_id", "pmid", "status",
    "first_pass_cost_usd", "wall_sec", "bundle_built", "error",
]

# Thread-safe primitives for shared state.
_csv_lock = threading.Lock()
_total_lock = threading.Lock()
_print_lock = threading.Lock()


def _append_csv(csv_path: Path, row: list) -> None:
    with _csv_lock, csv_path.open("a", newline="") as f:
        csv.writer(f).writerow(row)


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


class BatchState:
    def __init__(self) -> None:
        self.running_total = 0.0

    def add(self, cost: float) -> None:
        with _total_lock:
            self.running_total += cost

    def get(self) -> float:
        with _total_lock:
            return self.running_total


def _process_paper(
    *,
    paper: dict,
    pdf_index: dict[int, Path],
    run_root: Path,
    bundle_out: Path,
    log_dir: Path,
    summary_csv: Path,
    state: BatchState,
    stop_sentinel: Path,
    total_budget_usd: float,
    per_table_budget_usd: float,
    table_batch_size: int,
    do_arbitrate: bool,
) -> None:
    pmid = int(paper["pmid"])
    paper_id = paper["survey_key"]

    if (bundle_out / paper_id / "manifest.json").exists():
        _log(f"[skip-existing] {paper_id}")
        _append_csv(summary_csv, [paper_id, pmid, "skipped_existing", "", "", True, "bundle already present"])
        return

    if state.get() > total_budget_usd:
        _log(f"[budget-halt] {paper_id} (total ${state.get():.4f} > ${total_budget_usd:.2f})")
        _append_csv(summary_csv, [paper_id, pmid, "budget_halt", "", "", False, "batch budget exceeded"])
        return

    pdf_path = pdf_index.get(pmid)
    if pdf_path is None:
        _log(f"[no-pdf] {paper_id} (pmid {pmid})")
        _append_csv(summary_csv, [paper_id, pmid, "no_pdf", "", "", False, "PDF not found"])
        return

    log_file = log_dir / f"{paper_id}.log"
    _log(f"[start] {paper_id} (pmid {pmid}) — running total ${state.get():.4f}")

    started = time.monotonic()

    # Layer 1: Docling split + per-table Sonnet first-pass.
    buf = io.StringIO()
    fp_cost = 0.0
    fp_ok = False
    fp_err: str | None = None
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            status = layer1.run(
                paper_id=paper_id,
                pdf=pdf_path,
                run_root=run_root,
                batch_size=table_batch_size,
                per_table_budget_usd=per_table_budget_usd,
            )
        fp_cost = float(status.get("first_pass_cost_usd") or 0.0)
        fp_ok = not status.get("errors")
        if not fp_ok:
            fp_err = "; ".join(status.get("errors") or [])
    except Exception as e:  # noqa: BLE001
        fp_err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    finally:
        log_file.write_text(buf.getvalue())

    state.add(fp_cost)

    if not fp_ok:
        wall = int(time.monotonic() - started)
        _log(f"[FAIL layer1] {paper_id}")
        _append_csv(summary_csv, [paper_id, pmid, "first_pass_failed", fp_cost, wall, False, fp_err or "(unknown)"])
        return

    # Layer 2: deterministic merge + (optional) LLM arbiter + bundle assembly.
    bundle_buf = io.StringIO()
    bundle_err: str | None = None
    try:
        with redirect_stdout(bundle_buf), redirect_stderr(bundle_buf):
            layer2.build(
                paper_id=paper_id,
                run_root=run_root,
                output_root=bundle_out,
                pdf_path=pdf_path,
                do_arbitrate=do_arbitrate,
            )
    except Exception as e:  # noqa: BLE001
        bundle_err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    finally:
        with log_file.open("a") as f:
            f.write("\n--- layer2 (build_bundle) ---\n")
            f.write(bundle_buf.getvalue())

    wall = int(time.monotonic() - started)

    if bundle_err is not None:
        _log(f"[FAIL layer2] {paper_id}")
        _append_csv(summary_csv, [paper_id, pmid, "bundle_failed", fp_cost, wall, False, bundle_err])
        return

    if not (bundle_out / paper_id / "manifest.json").exists():
        _log(f"[FAIL no-manifest] {paper_id}")
        _append_csv(summary_csv, [paper_id, pmid, "no_manifest", fp_cost, wall, False, "manifest.json not produced"])
        return

    _log(f"[ok] {paper_id} — ${fp_cost:.4f} in {wall}s (running total ${state.get():.4f})")
    _append_csv(summary_csv, [paper_id, pmid, "ok", fp_cost, wall, True, ""])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subset", required=True, type=Path,
                    help="Path to subset JSON with `papers: [{pmid, survey_key}, ...]`")
    ap.add_argument("--pdfs-dir", required=True, type=Path,
                    help="Directory of source PDFs (filenames must contain `[PMID]`)")
    ap.add_argument("--bundle-out", required=True, type=Path,
                    help="Output dir for bundles (one subdir per paper_id)")
    ap.add_argument("--run-root", type=Path, default=None,
                    help="Intermediate dir for layer1/layer2 outputs (default: /tmp/paper_prep_batch_<ts>)")
    ap.add_argument("--per-table-budget-usd", type=float, default=1.50,
                    help="Per-table layer1 Sonnet first-pass budget (default 1.50)")
    ap.add_argument("--table-batch-size", type=int, default=4,
                    help="Max concurrent per-table claude -p workers inside layer1 (default 4)")
    ap.add_argument("--total-budget-usd", type=float, default=120.00,
                    help="Halt batch when cumulative cost exceeds this (default 120)")
    ap.add_argument("--max-concurrent", type=int, default=1,
                    help="Max papers in flight at once (default 1)")
    ap.add_argument("--no-arbitrate", dest="arbitrate", action="store_false", default=True,
                    help="Skip Stage-2 LLM arbiter in layer2 (deterministic merge only)")
    args = ap.parse_args()

    if args.max_concurrent < 1:
        print("--max-concurrent must be >= 1", file=sys.stderr)
        return 2

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = Path(args.run_root) if args.run_root else Path(f"/tmp/paper_prep_batch_{ts}")
    log_dir = run_root / "logs"
    stop_sentinel = run_root / "STOP"

    if stop_sentinel.exists():
        stop_sentinel.unlink()
        print(f"Cleared stale stop sentinel: {stop_sentinel}")

    run_root.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    args.bundle_out.mkdir(parents=True, exist_ok=True)

    summary_csv = args.bundle_out / "_run_summary.csv"
    if not summary_csv.exists():
        with summary_csv.open("w", newline="") as f:
            csv.writer(f).writerow(CSV_HEADER)

    print(f"Scanning {args.pdfs_dir} for PDFs...")
    pdf_index = build_pdf_index(args.pdfs_dir)
    print(f"  indexed {len(pdf_index)} PDFs with parseable PMIDs")

    subset = json.loads(args.subset.read_text())
    papers = subset["papers"]
    total = len(papers)
    print(f"Subset: {args.subset} ({total} papers)")
    print(f"Max concurrent: {args.max_concurrent}")
    print(f"Stop sentinel: touch {stop_sentinel} to finish in-flight and exit")
    print(f"Logs: {log_dir}")
    print()

    state = BatchState()
    paper_iter = iter(papers)
    stopped = False

    with ThreadPoolExecutor(max_workers=args.max_concurrent) as pool:
        def submit_next() -> bool:
            nonlocal stopped
            if stopped:
                return False
            if stop_sentinel.exists():
                _log(f"[stop] sentinel detected; not submitting more papers")
                stopped = True
                return False
            try:
                paper = next(paper_iter)
            except StopIteration:
                return False
            in_flight.add(pool.submit(
                _process_paper,
                paper=paper,
                pdf_index=pdf_index,
                run_root=run_root,
                bundle_out=args.bundle_out,
                log_dir=log_dir,
                summary_csv=summary_csv,
                state=state,
                stop_sentinel=stop_sentinel,
                total_budget_usd=args.total_budget_usd,
                per_table_budget_usd=args.per_table_budget_usd,
                table_batch_size=args.table_batch_size,
                do_arbitrate=args.arbitrate,
            ))
            return True

        in_flight: set = set()
        for _ in range(args.max_concurrent):
            submit_next()

        while in_flight:
            done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
            for f in done:
                f.result()
            submit_next()

    print()
    print("=" * 65)
    print(f"DONE. Summary: {summary_csv}")
    print(f"Per-paper logs: {log_dir}/")
    print(f"Total spent: ${state.get():.4f}")
    print("=" * 65)
    print()
    print("Status breakdown:")
    with summary_csv.open() as f:
        reader = csv.reader(f)
        next(reader)
        counts: dict[str, int] = {}
        for row in reader:
            if len(row) >= 3:
                counts[row[2]] = counts.get(row[2], 0) + 1
    for status, n in sorted(counts.items()):
        print(f"  {n:4d} {status}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
