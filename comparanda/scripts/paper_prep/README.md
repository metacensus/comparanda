# paper-prep

Turns scientific-paper PDFs into structured "bundles" (`manifest.json`,
`sections/`, `tables/`, `figures/`, `_provenance/`) that Comparanda's
extraction agents read.

Pipeline per paper:

1. **Docling** converts the PDF (TableFormer accurate mode) into markdown,
   per-section files, table JSON and figure PNGs.
2. **Per-table first pass**: one `claude -p` (Sonnet) call per
   Docling-detected table re-extracts that table from the PDF pages.
3. **Stage-1 merge**: a deterministic row/column alignment of the LLM and
   Docling versions of each table, classifying every cell as agreeing,
   one-sided or conflicting.
4. **Stage-2 arbiter** (optional): for tables with disagreements, one
   `claude -p` (Sonnet) call reads the PDF, both extractions and a diff
   guide, and writes the corrected table. The output is then parsed in
   Python to check it.
5. **Bundle assembly**: prose from Docling, tables from the arbited output,
   and non-decorative figures.

## Requirements

- [uv](https://docs.astral.sh/uv/) and Python 3.12+.
- The [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI
  (`claude`) on `PATH`, installed and logged in. `claude -p` uses your
  Claude Code login, either a subscription or an API key, and LLM usage is
  billed to that account.
- Cost is capped per call: `--per-table-budget-usd` (default 1.50) for each
  first-pass table, and 0.75 for each arbiter call. A paper with many
  tables makes many calls; `batch` also has `--total-budget-usd`.
- Run from a regular terminal, not from inside a Claude Code session.
  Nested `claude -p` calls are slow and unreliable.
- Docling downloads its layout and table models from Hugging Face on first
  use.

## Usage

```bash
cd comparanda/scripts/paper_prep
uv sync

# One paper
uv run python -m paper_prep <paper_id> \
    --pdf /abs/path/to/paper.pdf \
    --output-root /abs/path/to/data/processed \
    [--run-root /tmp/paper_prep_run] \
    [--per-table-budget-usd 1.50] [--table-batch-size 4] \
    [--no-arbitrate]

# A corpus
uv run python -m paper_prep.batch \
    --subset examples/example_batch_subset.json \
    --pdfs-dir /abs/path/to/pdfs \
    --bundle-out /abs/path/to/data/processed \
    [--total-budget-usd 120] [--max-concurrent 1] [--no-arbitrate]
```

The bundle lands at `<output-root>/<paper_id>/`. Intermediates go under
`--run-root` (by default a timestamped directory in `/tmp`). Re-running with
the same `--run-root` skips first-pass tables already on disk, and `batch`
skips papers whose bundle already exists. `--no-arbitrate` skips the arbiter
(no Stage-2 LLM calls). See `--help` on either command for all options.

### Batch subset and PDF filenames

`batch` takes a subset JSON (see `examples/example_batch_subset.json`):

```json
{"papers": [{"pmid": 12345678, "survey_key": "smith_2024_12345678"}]}
```

Each `pmid` is matched against the PDFs in `--pdfs-dir` by filename. The
filename must contain the PubMed ID as the leading digits of its **last**
`[...]` group, for example:

```
Smith 2024 (J Example) [12345678].pdf
Jones 2025 (Example Rev) [23456789 guideline].pdf
```

Files with no such bracket group are ignored. Two files with the same PMID
are an error. `survey_key` becomes the bundle's `paper_id`. The
single-paper command takes any `--pdf` path and does not use this
convention.

## Permission scoping for `claude -p`

Neither LLM call skips permission checks. Both are built by
`paper_prep.claude_cli.build_claude_argv` and run with:

| Flag | Effect |
|---|---|
| `--restricted` | No Bash or other code-running tools and no WebFetch. User and project settings files are ignored, and the file tools are confined to the working directories. |
| `--strict-mcp-config` | No MCP servers. |
| `--tools Read,Write,Edit` | The only built-in tools available. |
| `--permission-mode dontAsk` | Anything not pre-approved is denied rather than prompted, so calls never hang. |
| `--allowedTools Read,Edit(./<output file>)` | Reads within the working directories. Writes go to the one output file only. |
| `--add-dir <dir>` | Extra read-only directories. |

- **First pass**: cwd is `<run-root>/paper_runs/<paper_id>/`, plus
  `--add-dir` for the PDF's directory. The only writable file is
  `sonnet_tables/<idx>.md`. `arbiter_inputs/` (Docling's version of the
  tables) is not readable, which keeps the first pass independent of it.
- **Arbiter**: cwd is the output file's directory (`paper_runs/<paper_id>/`),
  plus `--add-dir` for the PDF's directory and `arbiter_inputs/<paper_id>/`.
  The only writable file is `arbited_table_<id>.md`.

## Tests

```bash
uv run pytest -q
```
