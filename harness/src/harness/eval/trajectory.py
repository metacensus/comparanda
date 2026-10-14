"""Trajectory tracker — records composite score + instruction sizes across iterations.

Tracks the full history of System 2 interventions for auditing and convergence detection.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class IterationRecord:
    """One System 2 iteration."""

    iteration: int
    composite_score: float
    row_coverage: float
    entity_coverage: float
    # finding_accuracy is None when detailed mode has NOT been run this
    # iteration (the old `1.0 if not yet run` default flattered
    # un-judged iterations and biased keep/revert toward "keep").
    finding_accuracy: float | None
    field_fill_rate: float
    instruction_sizes: dict[str, int]  # agent_name → word count
    total_instruction_words: int
    change_applied: str | None  # description of change, or None
    change_target: str | None  # which agent .md was modified
    change_type: str | None  # add_principle | modify_guidance | remove_rule | simplify
    decision: str  # keep | revert | skip (no change proposed)
    rationale: str
    overfitting_test_passed: bool | None = None
    papers_evaluated: list[str] = field(default_factory=list)
    # --- Audit fields: make every keep/revert reproducible ---
    composite_mode: str = "full"          # "full" | "aggregate_only"
    raw_composite: float | None = None     # code-computed from raw metrics
    adjusted_composite: float | None = None  # if agent_adjusted reported
    value_mismatch_count: int = 0          # deterministic value-diff gate
    value_gate_passed: bool = True
    schema_version: str | None = None      # the schema scored against
    subset_ids: list[str] = field(default_factory=list)
    gold_hash: str | None = None           # pins composite to a gold version
    papers_detailed_this_iter: list[str] = field(default_factory=list)
    attribution_confounded: bool = False   # True if schema changed mid-iteration
    # --- Round-2 audit fields ---
    # 100% recall gate: gold findings with values but no extraction match.
    unmatched_gold_with_values: int = 0
    coverage_gate_passed: bool = True
    # "per_finding" (code-recomputed) | "unvalidated_scalar" | None.
    finding_accuracy_source: str | None = None
    # Run-to-run noise: composite scores from repeat runs of identical
    # instructions on the fixed panel; sigma derived from these.
    replicate_scores: list[float] = field(default_factory=list)
    noise_sigma: float | None = None


@dataclass
class Trajectory:
    """Full history of System 2 iterations."""

    iterations: list[IterationRecord] = field(default_factory=list)

    @property
    def latest(self) -> IterationRecord | None:
        return self.iterations[-1] if self.iterations else None

    @property
    def _full_iters(self) -> list[IterationRecord]:
        # Only full-mode iterations are comparable watermarks:
        # an aggregate-only composite must not set best/converge.
        # Attribution-confounded iterations (schema changed mid-iteration)
        # are likewise excluded — CLAUDE.md Step 8 said so, the code didn't.
        return [
            r for r in self.iterations
            if r.composite_mode == "full" and not r.attribution_confounded
        ]

    @property
    def best_composite(self) -> float:
        full = self._full_iters
        if not full:
            return 0.0
        return max(r.composite_score for r in full)

    @property
    def best_iteration(self) -> IterationRecord | None:
        full = self._full_iters
        if not full:
            return None
        return max(full, key=lambda r: r.composite_score)

    def is_converged(self, window: int = 3, threshold: float | None = None) -> bool:
        """Plateau over the last N *full-mode, unconfounded* iterations, with
        no open value gate and no open recall gate
        (a gold finding with values but no extraction match blocks
        convergence — 100% recall against gold is a hard requirement).
        The plateau threshold defaults to the noise-calibrated delta when
        sigma is known, else 0.01."""
        full = self._full_iters
        if len(full) < window:
            return False
        recent = full[-window:]
        if any(not r.value_gate_passed for r in recent):
            return False
        if not recent[-1].coverage_gate_passed:
            return False
        if recent[-1].finding_accuracy_source == "unvalidated_scalar":
            return False
        if threshold is None:
            threshold = self.delta_required()
        scores = [r.composite_score for r in recent]
        return max(scores) - min(scores) < threshold

    # --- Noise-calibrated minimum effect size for keep/revert ---

    @property
    def sigma(self) -> float | None:
        """Most recent known run-to-run noise sigma, from explicit
        noise_sigma or computed from a record's replicate_scores (>=3)."""
        for r in reversed(self.iterations):
            if r.noise_sigma is not None:
                return r.noise_sigma
            if len(r.replicate_scores) >= 3:
                xs = r.replicate_scores
                m = sum(xs) / len(xs)
                var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
                return var ** 0.5
        return None

    def delta_required(self, floor: float = 0.01) -> float:
        """delta >= 2*sigma (CLAUDE.md Step 8), never below the floor."""
        s = self.sigma
        return max(2 * s, floor) if s is not None else floor

    def meets_delta(self, composite_before: float, composite_after: float,
                    floor: float = 0.01) -> bool:
        """Keep-eligibility: improvement must exceed the noise-calibrated
        minimum effect size. 'Any amount' is not enough."""
        return (composite_after - composite_before) >= self.delta_required(floor)

    def instruction_size_trend(self) -> list[int]:
        """Get total instruction word count across iterations."""
        return [r.total_instruction_words for r in self.iterations]

    def add(self, record: IterationRecord) -> None:
        self.iterations.append(record)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            [asdict(r) for r in self.iterations],
            indent=2,
            default=str,
        ))

    @classmethod
    def load(cls, path: Path) -> Trajectory:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        iterations = [IterationRecord(**r) for r in data]
        return cls(iterations=iterations)


def measure_instruction_sizes(agents_dir: Path) -> dict[str, int]:
    """Measure word count of each agent .md file."""
    sizes = {}
    for agent_file in sorted(agents_dir.glob("*.md")):
        text = agent_file.read_text()
        # Count words after frontmatter
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                text = parts[2]
        word_count = len(text.split())
        sizes[agent_file.stem] = word_count
    return sizes


def format_trajectory(trajectory: Trajectory) -> str:
    """Format trajectory as a human-readable summary."""
    lines = ["# System 2 Trajectory", ""]
    lines.append(f"Iterations: {len(trajectory.iterations)}")
    if trajectory.latest:
        lines.append(f"Latest composite: {trajectory.latest.composite_score:.3f}")
        lines.append(f"Best composite: {trajectory.best_composite:.3f}")
        lines.append(f"Converged: {trajectory.is_converged()}")
    lines.append("")

    lines.append("| Iter | Composite | Row Cov | Ent Cov | Words | Change | Decision |")
    lines.append("|------|-----------|---------|---------|-------|--------|----------|")
    for r in trajectory.iterations:
        change = r.change_applied[:30] + "..." if r.change_applied and len(r.change_applied) > 30 else (r.change_applied or "none")
        lines.append(
            f"| {r.iteration} | {r.composite_score:.3f} | {r.row_coverage:.0%} | "
            f"{r.entity_coverage:.0%} | {r.total_instruction_words} | {change} | {r.decision} |"
        )

    size_trend = trajectory.instruction_size_trend()
    if len(size_trend) >= 2:
        delta = size_trend[-1] - size_trend[0]
        direction = "grew" if delta > 0 else "shrank" if delta < 0 else "unchanged"
        lines.append(f"\nInstruction size {direction} by {abs(delta)} words over {len(size_trend)} iterations")

    return "\n".join(lines)
