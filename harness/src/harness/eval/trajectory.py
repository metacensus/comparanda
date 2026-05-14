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
    variant_coverage: float
    finding_accuracy: float  # from LLM judge, 1.0 if not yet run
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


@dataclass
class Trajectory:
    """Full history of System 2 iterations."""

    iterations: list[IterationRecord] = field(default_factory=list)

    @property
    def latest(self) -> IterationRecord | None:
        return self.iterations[-1] if self.iterations else None

    @property
    def best_composite(self) -> float:
        if not self.iterations:
            return 0.0
        return max(r.composite_score for r in self.iterations)

    @property
    def best_iteration(self) -> IterationRecord | None:
        if not self.iterations:
            return None
        return max(self.iterations, key=lambda r: r.composite_score)

    def is_converged(self, window: int = 3, threshold: float = 0.01) -> bool:
        """Check if composite score has plateaued over the last N iterations."""
        if len(self.iterations) < window:
            return False
        recent = [r.composite_score for r in self.iterations[-window:]]
        return max(recent) - min(recent) < threshold

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

    lines.append("| Iter | Composite | Row Cov | Var Cov | Words | Change | Decision |")
    lines.append("|------|-----------|---------|---------|-------|--------|----------|")
    for r in trajectory.iterations:
        change = r.change_applied[:30] + "..." if r.change_applied and len(r.change_applied) > 30 else (r.change_applied or "none")
        lines.append(
            f"| {r.iteration} | {r.composite_score:.3f} | {r.row_coverage:.0%} | "
            f"{r.variant_coverage:.0%} | {r.total_instruction_words} | {change} | {r.decision} |"
        )

    size_trend = trajectory.instruction_size_trend()
    if len(size_trend) >= 2:
        delta = size_trend[-1] - size_trend[0]
        direction = "grew" if delta > 0 else "shrank" if delta < 0 else "unchanged"
        lines.append(f"\nInstruction size {direction} by {abs(delta)} words over {len(size_trend)} iterations")

    return "\n".join(lines)
