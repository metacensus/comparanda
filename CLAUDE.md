# Comparanda — bootstrap and top-level orchestration

This repository contains reusable system code only. Domain state lives in a
single-domain superproject that pins this repository as a Git submodule.

## Routing

- **Run extraction or iterate a schema:** `cd comparanda/` and follow
  `comparanda/CLAUDE.md`. Use `comparanda/AGENT_INDEX.md` for agent contracts.
- **Calibrate against a gold standard:** `cd harness/` and follow
  `harness/CLAUDE.md`. Use `harness/AGENT_INDEX.md` for evaluation artifacts.
- **Understand repository composition:** read `docs/domain-superproject.md`.
- **Build paper bundles:** read `docs/paper-prep-integration.md`; the
  self-contained uv workspace is `comparanda/scripts/paper_prep/`.

Always `cd` into the inner loop or harness before invoking its agents. Pass
absolute external project paths; do not infer application state from the engine
checkout.

## System components

### Inner loop (`comparanda/`)

| Component | Role |
|---|---|
| `comparanda/CLAUDE.md` | Phase-by-phase orchestrator and project-root contract |
| `comparanda/AGENT_INDEX.md` | Agent scopes, file contracts, scripts, and resume behavior |
| `comparanda/.claude/agents/` | Survey, subset, schema, extraction, QC, normalization, and critique agents |
| `comparanda/.claude/hooks/` | Extraction write and finalization guardrails |
| `comparanda/scripts/` | Extraction, normalization, review/export, observation, and paper-prep tools |

The inner-loop sequence is survey → subset selection → schema proposal →
extraction → extraction QC → normalization → meta-analysis critique → schema
refinement.

### Outer loop (`harness/`)

| Component | Role |
|---|---|
| `harness/CLAUDE.md` | Calibration loop and external run-path contract |
| `harness/.claude/agents/monitor.md` | Read-only run monitoring |
| `harness/.claude/agents/eval.md` | Aggregate and detailed gold-standard evaluation |
| `harness/.claude/agents/diagnostician.md` | Instruction-level root-cause analysis |
| `harness/.claude/agents/critic.md` | One generalizable instruction proposal |
| `harness/src/harness/eval/` | Deterministic evaluation helpers |

The outer loop may update universal inner-loop instructions. It must not mutate
domain state; its external writes are restricted to the configured iterations
directory.

## Domain repository composition

```text
comparanda-<domain>/
├── comparanda/                 submodule -> metacensus/comparanda
├── export/                     submodule -> comparanda-<domain>-data
├── schema/, surveys/, extractions/, observations/
├── normalization/, critiques/, domain/, data/
├── benchmark/eval_config/
├── benchmark/gold_standard/
├── iterations/
└── comparanda-project.json
```

The domain repository root is the project root and contains exactly one domain.
Do not create `projects/<domain>/` inside it. Methodology commits belong inside
the Comparanda submodule; application and run commits belong in the
superproject; deployable reviewer runs belong in the export submodule.

## Bootstrap a domain superproject

### 1. Create the private repositories

Create private `comparanda-<domain>` and `comparanda-<domain>-data`
repositories. In the domain repository:

```bash
git submodule add git@github.com:metacensus/comparanda.git comparanda
git submodule add git@github.com:comparanda-<domain>-data.git export
```

Pin both submodules deliberately. Do not configure the engine submodule to
float with canonical `main`.

### 2. Create the flattened project state

At the superproject root, create:

```text
schema/  surveys/  extractions/  observations/  normalization/  critiques/
domain/  data/raw/  data/processed/  benchmark/eval_config/
benchmark/gold_standard/  iterations/
```

Add `comparanda-project.json` using the contract in
`docs/domain-superproject.md`. Set these absolute paths for a session:

```bash
export COMPARANDA_PROJECT_DIR="$(git rev-parse --show-toplevel)"
export COMPARANDA_ITERATIONS_DIR="$COMPARANDA_PROJECT_DIR/iterations"
export COMPARANDA_EVAL_CONFIG_DIR="$COMPARANDA_PROJECT_DIR/benchmark/eval_config"
export COMPARANDA_GOLD_STANDARD_DIR="$COMPARANDA_PROJECT_DIR/benchmark/gold_standard"
export COMPARANDA_EXPORT_RUNS_DIR="$COMPARANDA_PROJECT_DIR/export/runs"
```

### 3. Seed domain knowledge and inputs

Create `domain/deep_research.md` and `domain/seed_knowledge.md`. Put methodology
references in `data/raw/pdfs/phase0-references/` and the source corpus under
`data/raw/pdfs/`. Private domain repositories may track binary inputs with Git
LFS.

### 4. Build paper-prep bundles

```bash
cd "$COMPARANDA_PROJECT_DIR/comparanda/comparanda/scripts/paper_prep"
uv sync
uv run python -m paper_prep <paper_id> \
  --pdf "$COMPARANDA_PROJECT_DIR/data/raw/pdfs/<paper>.pdf" \
  --output-root "$COMPARANDA_PROJECT_DIR/data/processed"
```

For a corpus, use `python -m paper_prep.batch` with explicit `--pdfs-dir` and
`--bundle-out` paths. See `docs/paper-prep-integration.md`.

### 5. Run the inner loop

```bash
cd "$COMPARANDA_PROJECT_DIR/comparanda/comparanda"
claude -p "Run one inner-loop iteration using project root $COMPARANDA_PROJECT_DIR"
```

The orchestrator passes resolved project paths to every agent and tool.

### 6. Run the harness

```bash
cd "$COMPARANDA_PROJECT_DIR/comparanda/harness"
claude
```

The harness reads domain state through `COMPARANDA_PROJECT_DIR`, reads benchmark
configuration through the configured benchmark paths, and writes run artifacts
under `COMPARANDA_ITERATIONS_DIR`.

The legacy `harness-commit` two-branch router is not used in this composition.
Commit methodology in the Comparanda submodule and domain/run state in the
superproject.

### 7. Export a reviewer run

From `comparanda/comparanda/`:

```bash
uv run --no-project --with pymupdf \
  python scripts/review/portal_export.py \
  --run-id <new-run-id> \
  --domain <domain> \
  --eval-dir "$COMPARANDA_ITERATIONS_DIR/<iteration>"
```

The configured destination is `export/runs/<new-run-id>`. Published run IDs are
immutable; the exporter refuses to replace an existing run without `--force`.
