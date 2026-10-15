# Getting started: your first domain

This walkthrough takes you from a handful of open-access PDFs to paper-prep bundles, an inner-loop run in Claude Code, and (optionally) a harness evaluation against a gold standard you curate yourself.
It condenses the bootstrap in [`CLAUDE.md`](../CLAUDE.md) and the path contract in [`domain-superproject.md`](./domain-superproject.md); those two files are authoritative if anything here disagrees.

Comparanda is research software under active development.
Expect to read agent output critically, rerun phases, and spend real money on model calls.

Throughout, replace `<your-domain>` with a short slug for your research area (for example `statin-myopathy`) and `<comparanda-repo-url>` with the URL you clone this repository from.

## 0. Before you start

Check the [Requirements](../README.md#requirements) in the README: the Claude Code CLI (installed and logged in), uv, Python 3.12+, and jq.
Confirm:

```bash
claude --version
uv --version
jq --version
```

**Costs and time.**
Every LLM step runs through your Claude Code login (subscription or API key) and is billed or rate-limited accordingly:

- paper-prep makes one `claude -p` call **per table** in each PDF, capped at `--per-table-budget-usd` (default `1.50`), plus a Stage-2 arbiter call (capped at $0.75 by default) for each table where the model and Docling disagree.
  A paper with 10 tables can therefore cost up to roughly $15 for the first pass alone.
  Each per-table call may run up to `--per-table-timeout` seconds (default 900).
- One inner-loop iteration (survey through MA critique) usually takes 30–60 minutes of agent time on a small subset, and most token spend happens in extraction.
- The harness's detailed evaluation uses strong models (`opus` for the eval agent) and costs more than the aggregate checks.

Start with 3–6 short papers to keep your first run cheap.

## 1. Create the domain superproject

Domain data never goes in this repository.
It lives in a separate **superproject** whose root is the project root and which pins Comparanda as a Git submodule:

```bash
mkdir comparanda-<your-domain> && cd comparanda-<your-domain>
git init
git submodule add <comparanda-repo-url> comparanda
```

Pin the submodule to a specific commit and advance it deliberately; do not make it track `main` automatically.

The full layout also includes an `export/` submodule that points to a separate `comparanda-<your-domain>-data` repository for reviewer exports.
You only need that if you plan to publish reviewer runs (step 7), so you can add it later:

```bash
git submodule add <your-data-repo-url> export
```

## 2. Create the project state and path contract

At the superproject root:

```bash
mkdir -p schema surveys extractions observations normalization critiques \
  domain data/raw/pdfs/phase0-references data/processed \
  benchmark/eval_config benchmark/gold_standard iterations
```

Do not add a `projects/<your-domain>/` layer: the superproject root is the project root and holds exactly one domain.

Add `comparanda-project.json` at the root.
Paths are relative to the root; the resolver is `comparanda/scripts/project_layout.py`:

```json
{
  "domain": "<your-domain>",
  "iterations": "iterations",
  "benchmark": {
    "eval_config": "benchmark/eval_config",
    "gold_standard": "benchmark/gold_standard"
  },
  "export": {"runs": "export/runs"}
}
```

Then export absolute paths in every shell you use for Comparanda:

```bash
export COMPARANDA_PROJECT_DIR="$(git rev-parse --show-toplevel)"
export COMPARANDA_ITERATIONS_DIR="$COMPARANDA_PROJECT_DIR/iterations"
export COMPARANDA_EVAL_CONFIG_DIR="$COMPARANDA_PROJECT_DIR/benchmark/eval_config"
export COMPARANDA_GOLD_STANDARD_DIR="$COMPARANDA_PROJECT_DIR/benchmark/gold_standard"
export COMPARANDA_EXPORT_RUNS_DIR="$COMPARANDA_PROJECT_DIR/export/runs"
```

## 3. Seed domain knowledge

Phase 0 of the inner loop expects these inputs:

- `domain/deep_research.md`: a background report on the domain.
  Cover the key entities, outcomes, measurement conventions, terminology that changed over time, and how prior meta-analyses in the field were structured.
- `domain/seed_knowledge.md`: shorter notes from someone who knows the field, such as known pitfalls, aliases, and which distinctions matter.
- `data/raw/pdfs/phase0-references/`: methodology papers and prior meta-analyses in the domain.
  Agents use these to write their per-agent domain reference files.
  Do not put them in the extraction corpus.

Put the papers you want extracted in `data/raw/pdfs/`.
Use only PDFs you are allowed to process, and treat each as untrusted input (see [`SECURITY.md`](../SECURITY.md)).
The superproject may track PDFs with Git LFS if you want them versioned.

### PDF filename convention

The batch runner finds each PDF by its PubMed ID (PMID), which `paper_prep/pdf_index.py` reads from the **last `[...]` group** in the filename.
The group must start with digits:

```text
Smith 2024 (J Example) [12345678].pdf
Jones 2025 (Example Rev) [23456789 guideline].pdf
```

Files without a parseable PMID are skipped by the batch runner, and two files with the same PMID are an error.
For papers without a PMID (preprints, DOI-only papers), use the single-paper command below, which takes an explicit `--pdf` path.

Choose a stable `<paper_id>` for each paper.
It becomes the bundle directory name and the key used in surveys and extractions.
The recommended form is `Author_Year_DocumentID`; the batch example uses lowercase keys such as `smith_2024_12345678`.

## 4. Build paper-prep bundles

Paper-prep turns each PDF into a bundle (`manifest.json`, `sections/`, `tables/`, `figures/`, `_provenance/`).
The extractor reads only bundles, and every quote it cites is validated against the bundle text.

**Run paper-prep from a regular terminal, not inside a Claude Code session.**
It starts `claude -p` subprocesses, and nested sessions are slow and unreliable.

One-time setup (installs Docling, which downloads its layout models on first use):

```bash
cd "$COMPARANDA_PROJECT_DIR/comparanda/comparanda/scripts/paper_prep"
uv sync
```

One paper:

```bash
uv run python -m paper_prep <paper_id> \
  --pdf "$COMPARANDA_PROJECT_DIR/data/raw/pdfs/<file>.pdf" \
  --output-root "$COMPARANDA_PROJECT_DIR/data/processed"
```

A corpus: write a subset file shaped like `examples/example_batch_subset.json`:

```json
{
  "papers": [
    {"pmid": 12345678, "survey_key": "smith_2024_12345678"},
    {"pmid": 23456789, "survey_key": "jones_2025_23456789"}
  ]
}
```

and run:

```bash
uv run python -m paper_prep.batch \
  --subset /absolute/path/to/my_subset.json \
  --pdfs-dir "$COMPARANDA_PROJECT_DIR/data/raw/pdfs" \
  --bundle-out "$COMPARANDA_PROJECT_DIR/data/processed" \
  --total-budget-usd 20
```

Useful flags (see `--help` on either command):

| Flag | Default | Effect |
|---|---|---|
| `--per-table-budget-usd` | `1.50` | Cost cap for each per-table `claude -p` call |
| `--table-batch-size` | `4` | Concurrent per-table workers |
| `--no-arbitrate` | off | Skip the Stage-2 arbiter and keep the deterministic merge only (per-table calls still run) |
| `--total-budget-usd` (batch) | `120.00` | Stop starting new papers once the running total passes this |
| `--max-concurrent` (batch) | `1` | Papers processed in parallel |

The batch runner skips papers whose bundle already has a `manifest.json`, writes `<bundle-out>/_run_summary.csv`, and stops cleanly if you `touch <run-root>/STOP`.
Spot-check a few bundles' `tables/*.md` against the PDFs before going further; bundle quality limits everything downstream.

## 5. Run the inner loop

Start Claude Code from the inner-loop directory so it picks up `comparanda/CLAUDE.md` (the orchestrator), the agents, and the extraction guard hooks:

```bash
cd "$COMPARANDA_PROJECT_DIR/comparanda/comparanda"
claude
```

Then ask it to start, for example:

```text
Project root is <absolute path of COMPARANDA_PROJECT_DIR>. This is a new
domain. Run Phase 0 (domain reference generation), Phase 0.5 (survey schema),
and Phase 1 (survey all bundles and select a subset), then stop.
```

Review `domain/*_ref.md`, `schema/survey_schema.json`, and `surveys/` before continuing, then ask it to run one inner-loop iteration (Phases 2–3.5).
Non-interactive form:

```bash
claude -p "Run one inner-loop iteration using project root $COMPARANDA_PROJECT_DIR"
```

What to expect:

- Phases and their agents are listed in the [README](../README.md#how-it-works) and documented in `comparanda/CLAUDE.md` and `comparanda/AGENT_INDEX.md`.
- Outputs land in your superproject: `schema/vNNN.yaml`, `surveys/<paper_id>.json`, `extractions/<paper_id>.jsonl`, `normalization/`, `critiques/`, and the append-only `observations/log.jsonl`.
- The orchestrator commits after each phase, so your superproject's Git history becomes the audit trail.
  Methodology changes (prompts, scripts) are committed inside the `comparanda/` submodule instead.
- The extractor cannot write extraction JSONL directly; hooks force it through the sanctioned finalizers, which reject any quote not found in the bundle.
- The loop repeats schema refinement until MA critique reports the substrate "ready" on the current subset, then rotates to new subsets (Phase 5) before a full-corpus extraction (Phase 6).
  Stop and inspect whenever the schema churns without progress.

## 6. (Optional) Evaluate against your own gold standard

The harness compares extractions with a gold standard you curate by hand and proposes changes to the general-purpose agent instructions.
You need:

- `benchmark/gold_standard/<file>.xlsx`: a spreadsheet with one row per expert-extracted finding and a document-ID column.
- `benchmark/eval_config/document_id_map.json`: maps each `<paper_id>` to the document ID used in the spreadsheet, e.g. `{"smith_2024_12345678": 12345678}`.
- `benchmark/eval_config/gold_config.json` (optional, but needed unless your spreadsheet matches the built-in example layout): sheet name, document-ID column, compared gold fields, and the gold-column-to-extraction-field map.
  The available keys and defaults are in `harness/src/harness/eval/gold_config.py`.

Quick deterministic check (no model calls):

```bash
cd "$COMPARANDA_PROJECT_DIR/comparanda/harness"
uv run python -m harness.eval.aggregate \
  --extractions-dir "$COMPARANDA_PROJECT_DIR/extractions" \
  --gold-standard "$COMPARANDA_GOLD_STANDARD_DIR/<file>.xlsx" \
  --document-id-map "$COMPARANDA_EVAL_CONFIG_DIR/document_id_map.json" \
  --schema-dir "$COMPARANDA_PROJECT_DIR/schema" \
  --output "$COMPARANDA_ITERATIONS_DIR/iter_1/eval_report.json"
```

Full calibration loop (monitor, evaluate, diagnose, propose one instruction fix, keep or revert), driven by `harness/CLAUDE.md`:

```bash
cd "$COMPARANDA_PROJECT_DIR/comparanda/harness"
claude
```

The harness treats your domain state as read-only, writes only under `$COMPARANDA_ITERATIONS_DIR`, and may edit only the universal instruction sections of `comparanda/.claude/agents/*.md` (enforced by a jq-based guard hook).
It measures evidence recovery and faithfulness, not whether a meta-analysis built on the output would be valid; see [`harness/README.md`](../harness/README.md#evaluation-claim-boundary).

## 7. (Optional) Export a reviewer run

With an `export/` submodule configured, the portal exporter writes an immutable run under `export/runs/<run-id>`.
See step 7 of [`CLAUDE.md`](../CLAUDE.md#7-export-a-reviewer-run) for the command; it needs PyMuPDF, which `uv run --with pymupdf` provides.

## Where to go next

- `comparanda/CLAUDE.md`: every phase, its inputs and outputs, and the critical rules the orchestrator follows.
- `comparanda/AGENT_INDEX.md` and `harness/AGENT_INDEX.md`: agent scopes, file contracts, scripts, and resume behavior.
- `docs/paper-prep-integration.md`: how bundles are built and validated.
- `docs/domain-superproject.md`: repository ownership and path resolution.
