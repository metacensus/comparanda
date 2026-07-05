"""Bundle-substrate-specific metrics for the calibration harness.

Three metrics tracked alongside the composite when Comparanda runs on
paper-prep bundles (vs raw-PDF text):

1. **Per-paper token/cost delta** — extractor input/output token usage and
   cost from a Claude Code session JSONL log. Compared to a baseline (raw-
   PDF era) snapshot when one is supplied.

2. **Bundle utilization** — of `manifest.sections[]` + `manifest.tables[]` +
   `manifest.figures[]`, what fraction did the extractor cite in the
   extraction's `provenance[*].sources[*].location` field? Detects extractor
   laziness (under-utilization) or over-reading (publisher chrome).

3. **Manifest cross-ref hit rate** — of figure refs in the extraction's
   provenance, what fraction are in the survey's `figure_inventory`? The
   bundle-aware extractor instruction (Step 5 in its "How to use the bundle"
   section) says only treat figures as data-bearing if they appear in
   `survey.figure_inventory`. This metric measures compliance.

Usage from CLI:

    uv run python -m harness.eval.bundle_metrics \\
        --fork-root /path/to/comparanda-<domain> \\
        --domain <domain> \\
        --paper-ids Author_2022_0000001,Author_2018_0000002 \\
        --session-log-dir ~/.claude/projects/<encoded-cwd> \\
        --output iterations/iter_10_bundle_ab/bundle_metrics.json

In the fork layout, --fork-root points at the per-domain fork checkout (which
contains both system code in comparanda/ + harness/ and the domain data at
comparanda/projects/<domain>/). --app-repo is accepted as a deprecated alias
for one release.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


# ────────────────────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────────────────────


@dataclass
class BundleUtilization:
    """How much of the bundle did the extractor actually reference?"""

    paper_id: str
    sections_total: int
    sections_referenced: int
    tables_total: int
    tables_referenced: int
    figures_total: int
    figures_referenced: int
    sections_referenced_names: list[str] = field(default_factory=list)
    tables_referenced_ids: list[str] = field(default_factory=list)
    figures_referenced_ids: list[str] = field(default_factory=list)

    @property
    def sections_utilization(self) -> float:
        return self.sections_referenced / self.sections_total if self.sections_total else 0.0

    @property
    def tables_utilization(self) -> float:
        return self.tables_referenced / self.tables_total if self.tables_total else 0.0

    @property
    def figures_utilization(self) -> float:
        return self.figures_referenced / self.figures_total if self.figures_total else 0.0


@dataclass
class ManifestCrossRef:
    """Did extractor honor survey.figure_inventory when reading figures?"""

    paper_id: str
    figures_in_manifest: int
    figures_in_survey_inventory: int
    figures_cited_by_extraction: int
    figures_cited_and_in_survey: int
    figures_cited_but_not_in_survey: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        """Of figures cited in extraction, what fraction were in survey inventory?"""
        if self.figures_cited_by_extraction == 0:
            return 1.0  # Vacuous compliance — no figures cited at all
        return self.figures_cited_and_in_survey / self.figures_cited_by_extraction


@dataclass
class TokenCost:
    """Extractor session token + cost summary from Claude Code session log."""

    paper_id: str
    session_id: str | None
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float | None  # If usage events include cost; otherwise None
    n_turns: int  # Number of assistant turns in the session


@dataclass
class PaperBundleMetrics:
    paper_id: str
    bundle_utilization: BundleUtilization | None
    manifest_cross_ref: ManifestCrossRef | None
    token_cost: TokenCost | None


# ────────────────────────────────────────────────────────────────────
# Extraction provenance parsing
# ────────────────────────────────────────────────────────────────────


# Match patterns like "Table 2", "Figure 3", "Methods, paragraph 2",
# "Results section", "Section 3.1", "Materials & methods", etc.
TABLE_LOC_RE = re.compile(r"\btable\s+([a-z]?\d+[a-z]?)\b", re.IGNORECASE)
FIGURE_LOC_RE = re.compile(r"\bfig(?:ure)?\s+([a-z]?\d+[a-z]?)\b", re.IGNORECASE)


def parse_extraction_provenance(extraction_path: Path) -> dict[str, set[str]]:
    """Return distinct location references from an extraction's provenance.

    Returns a dict: {"sections": set[str], "tables": set[str], "figures": set[str]}.
    Section refs are kept as the full lowercased location string (we can't
    pre-normalize without the manifest section names).
    """
    refs = {"sections": set(), "tables": set(), "figures": set()}
    if not extraction_path.exists():
        return refs

    with extraction_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "_meta_commentary" in row:
                continue
            provenance = row.get("provenance", {})
            if not isinstance(provenance, dict):
                continue
            for _field_name, prov_entry in provenance.items():
                if not isinstance(prov_entry, dict):
                    continue
                for source in prov_entry.get("sources", []):
                    loc = source.get("location", "")
                    if not loc:
                        continue
                    loc_lower = loc.lower()
                    if t := TABLE_LOC_RE.search(loc_lower):
                        refs["tables"].add(t.group(1).upper())
                    elif f := FIGURE_LOC_RE.search(loc_lower):
                        refs["figures"].add(f.group(1).upper())
                    else:
                        # Treat as a section reference — keep the prefix.
                        # Examples: "Methods, paragraph 2" → "methods",
                        # "Results section, paragraph 1" → "results",
                        # "Introduction" → "introduction".
                        prefix = loc_lower.split(",")[0].strip()
                        prefix = re.sub(r"\s+(section|paragraph|sentence|p\.\s*\d+).*$", "", prefix)
                        if prefix:
                            refs["sections"].add(prefix)
    return refs


# ────────────────────────────────────────────────────────────────────
# Bundle utilization
# ────────────────────────────────────────────────────────────────────


def compute_bundle_utilization(
    paper_id: str, bundle_dir: Path, extraction_path: Path
) -> BundleUtilization:
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    sections = manifest.get("sections", [])
    tables = manifest.get("tables", [])
    figures = manifest.get("figures", [])

    refs = parse_extraction_provenance(extraction_path)

    # Section matching: an extraction "section" prefix matches a manifest section
    # if it appears (case-insensitive) in the section's name or heading.
    sec_ref_names: list[str] = []
    for sec in sections:
        name = (sec.get("name") or "").lower()
        heading = (sec.get("heading") or "").lower()
        for ref in refs["sections"]:
            if ref in name or ref in heading or name.startswith(ref) or heading.startswith(ref):
                sec_ref_names.append(sec.get("name") or sec.get("heading") or "?")
                break

    # Table matching: manifest tables have an "id" or "table_id" field.
    tab_ref_ids: list[str] = []
    for tab in tables:
        tid = str(tab.get("table_id") or tab.get("id") or tab.get("name") or "").upper()
        # Strip "TABLE_" / "TABLE " prefix if present.
        tid_clean = re.sub(r"^TABLE[_\s]*", "", tid)
        if tid_clean in refs["tables"] or tid in refs["tables"]:
            tab_ref_ids.append(tid)

    # Figure matching: manifest.figures usually has "figure_id" or "n".
    fig_ref_ids: list[str] = []
    for fig in figures:
        fid = str(fig.get("figure_id") or fig.get("id") or fig.get("n") or "").upper()
        fid_clean = re.sub(r"^FIGURE[_\s]*", "", fid)
        if fid_clean in refs["figures"] or fid in refs["figures"]:
            fig_ref_ids.append(fid)

    return BundleUtilization(
        paper_id=paper_id,
        sections_total=len(sections),
        sections_referenced=len(sec_ref_names),
        tables_total=len(tables),
        tables_referenced=len(tab_ref_ids),
        figures_total=len(figures),
        figures_referenced=len(fig_ref_ids),
        sections_referenced_names=sec_ref_names,
        tables_referenced_ids=tab_ref_ids,
        figures_referenced_ids=fig_ref_ids,
    )


# ────────────────────────────────────────────────────────────────────
# Manifest cross-ref (figure inventory compliance)
# ────────────────────────────────────────────────────────────────────


def compute_manifest_cross_ref(
    paper_id: str, bundle_dir: Path, survey_path: Path, extraction_path: Path
) -> ManifestCrossRef:
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    manifest_figs = manifest.get("figures", [])

    survey_inventory: list[str] = []
    if survey_path.exists():
        survey = json.loads(survey_path.read_text())
        # figure_inventory is typically a list of {figure_id, caption} or strings.
        inv = survey.get("figure_inventory") or survey.get("figures") or []
        if isinstance(inv, list):
            for entry in inv:
                if isinstance(entry, str):
                    survey_inventory.append(entry)
                elif isinstance(entry, dict):
                    survey_inventory.append(
                        str(entry.get("figure_id") or entry.get("id") or entry.get("number") or "")
                    )

    survey_inventory_norm = {
        re.sub(r"^FIGURE[_\s]*", "", s.upper()) for s in survey_inventory if s
    }

    refs = parse_extraction_provenance(extraction_path)
    figs_cited = refs["figures"]
    figs_cited_and_in_survey = figs_cited & survey_inventory_norm
    figs_cited_but_not_in_survey = sorted(figs_cited - survey_inventory_norm)

    return ManifestCrossRef(
        paper_id=paper_id,
        figures_in_manifest=len(manifest_figs),
        figures_in_survey_inventory=len(survey_inventory_norm),
        figures_cited_by_extraction=len(figs_cited),
        figures_cited_and_in_survey=len(figs_cited_and_in_survey),
        figures_cited_but_not_in_survey=figs_cited_but_not_in_survey,
    )


# ────────────────────────────────────────────────────────────────────
# Token / cost from session JSONL
# ────────────────────────────────────────────────────────────────────


def find_extractor_subagent_log(session_log_dir: Path, paper_id: str) -> Path | None:
    """Search session subagent logs for the extractor invocation for this paper.

    Claude Code stores subagent logs at
        ~/.claude/projects/<encoded-cwd>/<session_id>/subagents/<agent_id>.jsonl
    with a sibling <agent_id>.meta.json containing the agent type + description.

    We look for the most recent meta.json with agent type == "extractor" and
    a description mentioning the paper_id.
    """
    if not session_log_dir.exists():
        return None

    candidates: list[tuple[float, Path]] = []
    for meta in session_log_dir.glob("**/subagents/*.meta.json"):
        try:
            m = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if (m.get("agent_type") or m.get("type")) != "extractor":
            continue
        desc = (m.get("description") or "")
        # Match paper_id either fully or by author/year portion.
        author_year = "_".join(paper_id.split("_")[:2]).lower()
        if paper_id.lower() in desc.lower() or author_year in desc.lower():
            log = meta.with_suffix("").parent / (meta.stem.replace(".meta", "") + ".jsonl")
            log = meta.parent / (meta.stem[: -len(".meta")] + ".jsonl") if meta.stem.endswith(".meta") else log
            # Fallback derivation
            if not log.exists():
                log = meta.with_name(meta.name.replace(".meta.json", ".jsonl"))
            if log.exists():
                candidates.append((meta.stat().st_mtime, log))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def compute_token_cost(paper_id: str, log_path: Path | None) -> TokenCost:
    if log_path is None or not log_path.exists():
        return TokenCost(
            paper_id=paper_id,
            session_id=None,
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cost_usd=None,
            n_turns=0,
        )

    input_t = output_t = cache_create_t = cache_read_t = 0
    cost = 0.0
    n_turns = 0
    sid = None
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if sid is None:
                sid = ev.get("session_id") or ev.get("uuid")
            if ev.get("type") == "assistant":
                n_turns += 1
                msg = ev.get("message", {})
                usage = msg.get("usage", {}) if isinstance(msg, dict) else {}
                input_t += usage.get("input_tokens", 0) or 0
                output_t += usage.get("output_tokens", 0) or 0
                cache_create_t += usage.get("cache_creation_input_tokens", 0) or 0
                cache_read_t += usage.get("cache_read_input_tokens", 0) or 0
                if "cost_usd" in ev:
                    cost += ev["cost_usd"]

    return TokenCost(
        paper_id=paper_id,
        session_id=sid,
        input_tokens=input_t,
        output_tokens=output_t,
        cache_creation_tokens=cache_create_t,
        cache_read_tokens=cache_read_t,
        cost_usd=cost if cost > 0 else None,
        n_turns=n_turns,
    )


# ────────────────────────────────────────────────────────────────────
# Orchestration
# ────────────────────────────────────────────────────────────────────


def collect_paper_metrics(
    fork_root: Path,
    domain: str,
    paper_id: str,
    session_log_dir: Path | None,
) -> PaperBundleMetrics:
    bundle_dir = fork_root / "comparanda" / "projects" / domain / "data" / "processed" / paper_id
    extraction = fork_root / "comparanda" / "projects" / domain / "extractions" / f"{paper_id}.jsonl"
    survey = fork_root / "comparanda" / "projects" / domain / "surveys" / f"{paper_id}.json"

    util = None
    if bundle_dir.exists() and extraction.exists():
        util = compute_bundle_utilization(paper_id, bundle_dir, extraction)

    xref = None
    if bundle_dir.exists() and extraction.exists():
        xref = compute_manifest_cross_ref(paper_id, bundle_dir, survey, extraction)

    cost = None
    if session_log_dir is not None:
        log_path = find_extractor_subagent_log(session_log_dir, paper_id)
        cost = compute_token_cost(paper_id, log_path)

    return PaperBundleMetrics(
        paper_id=paper_id,
        bundle_utilization=util,
        manifest_cross_ref=xref,
        token_cost=cost,
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # --fork-root is the primary flag; --app-repo is a deprecated alias kept
    # for one release. Both write to the same dest (fork_root).
    p.add_argument(
        "--fork-root",
        type=Path,
        dest="fork_root",
        help="Path to per-domain fork checkout (contains comparanda/ and harness/)",
    )
    p.add_argument(
        "--app-repo",
        type=Path,
        dest="fork_root",
        help="DEPRECATED alias for --fork-root (will be removed in a future release)",
    )
    p.add_argument("--domain", required=True, help="Domain name (the per-domain project slug)")
    p.add_argument("--paper-ids", required=True, help="Comma-separated paper IDs")
    p.add_argument(
        "--session-log-dir",
        type=Path,
        default=None,
        help="Path to ~/.claude/projects/<encoded-cwd>/ for token/cost extraction",
    )
    p.add_argument("--output", type=Path, default=None, help="Write JSON report here")
    args = p.parse_args()

    if args.fork_root is None:
        p.error("one of --fork-root or --app-repo is required")
    # Detect --app-repo usage by inspecting sys.argv (since both flags share dest).
    if any(a.startswith("--app-repo") for a in sys.argv):
        print(
            "warning: --app-repo is deprecated; use --fork-root instead.",
            file=sys.stderr,
        )

    paper_ids = [pid.strip() for pid in args.paper_ids.split(",") if pid.strip()]
    results = [
        collect_paper_metrics(args.fork_root, args.domain, pid, args.session_log_dir)
        for pid in paper_ids
    ]

    report = {
        "domain": args.domain,
        "papers": [],
    }
    for r in results:
        entry = {"paper_id": r.paper_id}
        if r.bundle_utilization:
            entry["bundle_utilization"] = {
                **asdict(r.bundle_utilization),
                "sections_utilization": r.bundle_utilization.sections_utilization,
                "tables_utilization": r.bundle_utilization.tables_utilization,
                "figures_utilization": r.bundle_utilization.figures_utilization,
            }
        if r.manifest_cross_ref:
            entry["manifest_cross_ref"] = {
                **asdict(r.manifest_cross_ref),
                "hit_rate": r.manifest_cross_ref.hit_rate,
            }
        if r.token_cost:
            entry["token_cost"] = asdict(r.token_cost)
        report["papers"].append(entry)

    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
