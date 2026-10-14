"""Cross-domain regression gate.

Runs the code-computed composite on each frozen domain fixture and fails if any
regresses beyond its epsilon. An EMPTY suite is its own status — it does NOT
silently pass: a hard gate that auto-passes manufactures false assurance, and
with an empty suite the only anti-overfitting check left is the critic grading
itself. Convergence treats "empty" as UNSATISFIED unless explicitly waived
(record the waiver in trajectory.json). Wire into the methodology-PR CI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.eval.score import compute_score


def run_gate(suite_dir: Path) -> tuple[str, list[dict]]:
    """Returns (status, results) with status in {"passed", "failed", "empty"}."""
    results: list[dict] = []
    ok = True
    domains = [d for d in sorted(suite_dir.iterdir()) if d.is_dir()] if suite_dir.exists() else []
    if not domains:
        return "empty", results
    for d in domains:
        exp_path = d / "expected_composite.json"
        gold = d / "gold.xlsx"
        idmap = d / "document_id_map.json"
        bundles = d / "bundles"
        if not (exp_path.exists() and gold.exists() and idmap.exists()):
            continue
        exp = json.loads(exp_path.read_text())
        # extractions are expected under bundles/../extractions in a real fixture;
        # fixtures store a frozen extractions dir alongside.
        extractions = d / "extractions"
        if not extractions.exists():
            results.append({"domain": d.name, "status": "skipped_no_extractions"})
            continue
        score = compute_score(
            extractions_dir=extractions, gold_standard=gold, document_id_map=idmap,
            finding_accuracy=exp.get("finding_accuracy"), iteration=0,
        )
        baseline = exp["composite"]
        eps = exp.get("epsilon", 0.03)
        drop = baseline - score["composite_score"]
        regressed = drop > eps
        ok = ok and not regressed
        results.append({"domain": d.name, "baseline": baseline,
                        "now": score["composite_score"], "drop": round(drop, 4),
                        "epsilon": eps, "regressed": regressed,
                        "value_gate_passed": score["value_gate_passed"]})
    if not results:
        # Directories existed but none was a complete fixture.
        return "empty", results
    return ("passed" if ok else "failed"), results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="benchmark/regression")
    a = ap.parse_args()
    status, results = run_gate(Path(a.suite))
    print(json.dumps({"status": status, "passed": status == "passed",
                      "domains": results}, indent=2))
    if status == "failed":
        print("REGRESSION GATE FAILED: a frozen domain regressed beyond epsilon.", file=sys.stderr)
        sys.exit(1)
    if status == "empty":
        print(
            "REGRESSION SUITE EMPTY: no frozen domain fixtures found. The "
            "cross-domain overfitting check did NOT run — this gate is "
            "UNSATISFIED for convergence purposes. Seed benchmark/regression/ "
            "with a frozen fixture (see its README), or record an explicit "
            "waiver in trajectory.json.",
            file=sys.stderr,
        )
        sys.exit(2)
    print("Regression gate passed.")


if __name__ == "__main__":
    main()
