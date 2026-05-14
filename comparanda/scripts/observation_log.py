"""Observation log — append-only structured log for cross-iteration learning.

All agents read from and write to this log. Categories:
  - schema_rationale: why fields were added/removed/modified
  - domain_pattern: domain-specific patterns discovered during processing
  - paper_flag: per-paper notes (unusual format, missing data, ambiguities)
  - normalization_note: terminology conflicts, new mappings
  - extraction_difficulty: challenges that may inform instruction updates
  - subset_selection: which papers were selected and why

Format: one JSON object per line (JSONL) at projects/{domain}/observations/log.jsonl
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


VALID_CATEGORIES = {
    "schema_rationale",
    "domain_pattern",
    "paper_flag",
    "normalization_note",
    "extraction_difficulty",
    "subset_selection",
}


@dataclass
class ObservationEntry:
    """A single observation log entry."""

    id: str
    iteration: int
    phase: str
    agent: str
    category: str
    content: str
    related_fields: list[str] = field(default_factory=list)
    paper_id: str | None = None
    timestamp: str = ""

    def __post_init__(self) -> None:
        if self.category not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid category '{self.category}'. Must be one of: {VALID_CATEGORIES}"
            )
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def next_id(log_path: Path) -> str:
    """Generate the next observation ID (OBS-001, OBS-002, ...)."""
    if not log_path.exists():
        return "OBS-001"
    count = sum(1 for line in open(log_path) if line.strip())
    return f"OBS-{count + 1:03d}"


def append(log_path: Path, entry: ObservationEntry) -> None:
    """Append an entry to the observation log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(asdict(entry), default=str) + "\n")


def read_all(log_path: Path) -> list[ObservationEntry]:
    """Read all entries from the observation log."""
    if not log_path.exists():
        return []
    entries = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(ObservationEntry(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
    return entries


def read_by_category(log_path: Path, category: str) -> list[ObservationEntry]:
    """Read entries filtered by category."""
    return [e for e in read_all(log_path) if e.category == category]


def read_by_agent(log_path: Path, agent: str) -> list[ObservationEntry]:
    """Read entries filtered by agent name."""
    return [e for e in read_all(log_path) if e.agent == agent]


def read_recent(log_path: Path, n_iterations: int = 2) -> list[ObservationEntry]:
    """Read entries from the last N iterations."""
    all_entries = read_all(log_path)
    if not all_entries:
        return []
    max_iter = max(e.iteration for e in all_entries)
    cutoff = max_iter - n_iterations + 1
    return [e for e in all_entries if e.iteration >= cutoff]


def read_schema_rationale(log_path: Path) -> list[ObservationEntry]:
    """Read all schema_rationale entries — critical for preventing cycles."""
    return read_by_category(log_path, "schema_rationale")


def _extract_pmid(paper_id: str) -> str | None:
    """Extract PMID (trailing digits) from a paper_id like 'Bozina_2022_34780066'."""
    parts = paper_id.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) >= 7:
        return parts[1]
    return None


def _extract_author_year(paper_id: str) -> tuple[str, str] | None:
    """Extract (author, year) from a paper_id like 'Bozina_2022_34780066'."""
    parts = paper_id.split("_")
    if len(parts) >= 2:
        author = parts[0].lower()
        year = parts[1] if parts[1].isdigit() and len(parts[1]) == 4 else None
        if year:
            return (author, year)
    return None


def _entry_matches_paper(entry: ObservationEntry, paper_id: str) -> bool:
    """Check if an entry is about a specific paper.

    Matches on: explicit paper_id field, PMID in content, or author+year in content.
    """
    # Exact field match (case-insensitive)
    if entry.paper_id and entry.paper_id.lower() == paper_id.lower():
        return True

    content_lower = entry.content.lower()

    # PMID match
    pmid = _extract_pmid(paper_id)
    if pmid and pmid in entry.content:
        return True

    # Author + year match in content
    author_year = _extract_author_year(paper_id)
    if author_year:
        author, year = author_year
        if author in content_lower and year in entry.content:
            return True

    return False


def query_for_extraction(
    log_path: Path,
    paper_id: str,
) -> list[ObservationEntry]:
    """Return observation log entries relevant to a specific extraction.

    Returns:
      - paper_flag entries for this paper
      - extraction_difficulty entries for this paper
      - ALL domain_pattern entries (cross-cutting knowledge)
    """
    all_entries = read_all(log_path)
    results = []
    for entry in all_entries:
        if entry.category == "domain_pattern":
            results.append(entry)
        elif entry.category in ("paper_flag", "extraction_difficulty"):
            if _entry_matches_paper(entry, paper_id):
                results.append(entry)
    return results


def summary(log_path: Path) -> dict:
    """Get a summary of the observation log."""
    entries = read_all(log_path)
    if not entries:
        return {"total": 0, "by_category": {}, "by_agent": {}, "iterations": []}
    categories: dict[str, int] = {}
    agents: dict[str, int] = {}
    iterations: set[int] = set()
    for e in entries:
        categories[e.category] = categories.get(e.category, 0) + 1
        agents[e.agent] = agents.get(e.agent, 0) + 1
        iterations.add(e.iteration)
    return {
        "total": len(entries),
        "by_category": categories,
        "by_agent": agents,
        "iterations": sorted(iterations),
    }


def main() -> None:
    """CLI entrypoint for querying the observation log."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Query the Comparanda observation log"
    )
    sub = parser.add_subparsers(dest="command")

    q = sub.add_parser(
        "query",
        help="Query entries relevant to a paper extraction",
    )
    q.add_argument("--log", required=True, help="Path to log.jsonl")
    q.add_argument("--paper-id", required=True, help="Paper ID (e.g. Bozina_2022_34780066)")

    s = sub.add_parser("summary", help="Show log summary")
    s.add_argument("--log", required=True, help="Path to log.jsonl")

    args = parser.parse_args()

    if args.command == "query":
        entries = query_for_extraction(Path(args.log), args.paper_id)
        for entry in entries:
            print(json.dumps(asdict(entry), default=str))
    elif args.command == "summary":
        print(json.dumps(summary(Path(args.log)), indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
