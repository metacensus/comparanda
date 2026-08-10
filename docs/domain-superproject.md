# Domain superproject contract

Comparanda methodology, domain working state, and deployable reviewer data have
different ownership and release cadences. They therefore live in three separate
repositories composed with Git submodules.

```text
metacensus/comparanda                 reusable engine + harness

metacensus/comparanda-<domain>        single-domain superproject
├── comparanda/                       pinned engine submodule
├── export/                           pinned comparanda-<domain>-data submodule
├── schema/, surveys/, extractions/, observations/
├── normalization/, critiques/, domain/, data/
├── benchmark/eval_config/
├── benchmark/gold_standard/
├── iterations/
└── comparanda-project.json

comparanda-<domain>-data   immutable deployable runs
└── runs/<run-id>/
```

The superproject root is the project root. It contains exactly one domain and
must not add a `projects/<domain>` layer.

## Path resolution

Tools continue to accept explicit paths. Agents and orchestration resolve the
same paths through these variables:

| Meaning | Environment variable | Default from project root |
|---|---|---|
| project state | `COMPARANDA_PROJECT_DIR` | required |
| run history | `COMPARANDA_ITERATIONS_DIR` | `iterations/` |
| eval config | `COMPARANDA_EVAL_CONFIG_DIR` | `benchmark/eval_config/` |
| gold standard | `COMPARANDA_GOLD_STANDARD_DIR` | `benchmark/gold_standard/` |
| data-repo run root | `COMPARANDA_EXPORT_RUNS_DIR` | config `export.runs` |
| one export destination | `COMPARANDA_EXPORT_DIR` | `<export-runs>/<run-id>` |

`comparanda-project.json` records portable relative paths. The pure-standard-
library resolver is `comparanda/scripts/project_layout.py`. A legacy
in-repository project remains usable when its path is explicitly passed.

## Git ownership

- Engine prompts, hooks, scripts, and harness code are committed inside the
  `comparanda/` submodule on a methodology feature branch.
- Domain inputs, editable state, benchmark configuration, and run history are
  committed in the domain superproject.
- Reviewer-facing output is committed under a new immutable run ID in the data
  repository. The exporter refuses to replace an existing output unless the
  caller explicitly passes `--force`.
- The old two-branch `harness-commit` router is a legacy-fork tool. In a
  superproject, commit in the repository that owns the changed path; no router
  can atomically redirect one staged change set across a repository boundary.

The superproject and Demo may intentionally pin different data commits. This
allows the domain workspace to test a candidate while Demo stays on the last
approved release.

## Exporting a reviewer run

From the inner-loop engine directory:

```bash
export COMPARANDA_PROJECT_DIR=/absolute/path/to/comparanda-<domain>
uv run --no-project --with pymupdf \
  python scripts/review/portal_export.py \
  --run-id <new-run-id> \
  --domain <domain> \
  --eval-dir "$COMPARANDA_PROJECT_DIR/iterations/<iteration>"
```

With `export.runs` configured, the output defaults to
`export/runs/<new-run-id>`. Pass `--out` to override it. The exporter discovers
gold adjudications, gold config, and bundle status from the configured
benchmark eval-config directory when present.
