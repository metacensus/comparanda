"""Single-paper paper-prep CLI: Docling extraction + per-table Sonnet first-pass
+ deterministic merge + (optional) Stage-2 LLM arbiter + bundle assembly.

RUN THIS FROM A REGULAR TERMINAL — NOT from inside a Claude Code session.
The Sonnet calls invoke `claude -p` as subprocesses which need OAuth
keychain auth. Nested invocations from inside an outer Claude Code
session are slow and broken.

Usage:
    uv run python -m paper_prep <paper_id> \\
        --pdf /path/to/paper.pdf \\
        --output-root "$COMPARANDA_PROJECT_DIR/data/processed/" \\
        [--run-root /tmp/paper_prep_<ts>] \\
        [--per-table-budget-usd 1.50] \\
        [--table-batch-size 4] \\
        [--no-arbitrate]

Outputs the bundle at <output-root>/<paper_id>/ (manifest.json,
sections/, tables/, figures/, _provenance/).

For batch runs over a corpus, see `python -m paper_prep.batch --help`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from paper_prep import layer1, layer2


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paper_id", help="Stable identifier for the paper (used as bundle dir name)")
    ap.add_argument("--pdf", required=True, type=Path, help="Source PDF path")
    ap.add_argument("--output-root", required=True, type=Path,
                    help="Output dir; bundle lands at <output-root>/<paper_id>/")
    ap.add_argument("--run-root", type=Path, default=None,
                    help="Intermediate dir (default: /tmp/paper_prep_<ts>)")
    ap.add_argument("--per-table-budget-usd", type=float, default=1.50,
                    help="Per-table Sonnet first-pass budget (default 1.50)")
    ap.add_argument("--table-batch-size", type=int, default=4,
                    help="Max concurrent per-table workers in layer1 (default 4)")
    ap.add_argument("--per-table-timeout", type=int, default=900,
                    help="Per-table subprocess timeout in seconds (default 900)")
    ap.add_argument("--arbitrate", dest="arbitrate", action="store_true", default=True,
                    help="Run Stage-2 LLM arbiter in layer2 (default)")
    ap.add_argument("--no-arbitrate", dest="arbitrate", action="store_false",
                    help="Skip Stage-2 arbiter; deterministic merge only")
    args = ap.parse_args()

    if not args.pdf.exists():
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 2

    if args.run_root is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_root = Path(f"/tmp/paper_prep_{ts}")
    else:
        run_root = args.run_root
    run_root.mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True)

    print(f"=== Layer 1: Docling split + per-table Sonnet first-pass ===")
    status = layer1.run(
        paper_id=args.paper_id,
        pdf=args.pdf,
        run_root=run_root,
        batch_size=args.table_batch_size,
        per_table_budget_usd=args.per_table_budget_usd,
        per_table_timeout=args.per_table_timeout,
    )
    if status.get("errors"):
        print(f"Layer 1 errors: {status['errors']}", file=sys.stderr)
        return 1

    print(f"\n=== Layer 2: merge + arbiter + bundle assembly ===")
    layer2.build(
        paper_id=args.paper_id,
        run_root=run_root,
        output_root=args.output_root,
        pdf_path=args.pdf,
        do_arbitrate=args.arbitrate,
    )

    bundle_dir = args.output_root / args.paper_id
    print(f"\nBundle: {bundle_dir}")
    print(f"Intermediates: {run_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
