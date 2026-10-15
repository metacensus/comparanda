# Comparanda

Comparanda turns a collection of scientific papers into a structured,
source-linked dataset ready for meta-analysis. You give it a research area and
the PDFs. It works out what should be extracted, extracts every finding as its
own row, ties each value back to the exact passage or table cell it came from,
and reconciles terminology that drifted across decades of literature.

It runs as a set of [Claude Code](https://docs.claude.com/en/docs/claude-code)
agents with deterministic Python tooling around them, plus a calibration
harness that scores the agents against an expert-built gold standard and
proposes fixes to their instructions.

Comparanda is built by [MetaCensus](https://github.com/metacensus), a nonprofit
working on open, reusable evidence for meta-analysis.

## Why

Meta-analysis starts with the slowest step: reading dozens or hundreds of
papers and turning them into comparable data. Screening and extraction tools
such as Covidence and Elicit help with that work, but they start from a schema
someone has already designed. Deciding *which* fields matter, at what
granularity, and how to map a 2004 paper's terminology onto a 2023 one is
usually the hard part, and it is done by hand, separately, by every group that
asks a related question.

Comparanda treats that schema as something to discover. The extraction it
produces is granular enough to answer more than one research question, so the
work can be done once and reused.

## What it does

**Discovers the schema from the papers.** A fast survey agent catalogs what
each paper contains: entities, outcomes, study designs, tables, figures. A
schema agent turns those surveys and your domain notes into a versioned YAML
schema with extraction guidance for every field.

**Extracts one finding per row.** Each row is a single result for a single
group, outcome and measurement, with the reference or control group recorded
as its own row rather than left implicit.

**Links every value to its source, and checks the link.** The extractor
doesn't write output files directly. It writes a short build script against an
extraction SDK, citing evidence as a quote, a table cell, or a figure. A
validator then confirms that each quote exists at the cited location, that the
value appears in its quote or cell, and that any derived number has sourced
inputs and arithmetic that checks out. Claude Code hooks block direct writes
and stop the extractor from finishing until a validated finalization has run.

**Says how sure it is, and why something is missing.** Every value is marked
as verbatim, derived or inferred, and as clean or ambiguous. Figure-read
values can't claim to be clean. Missing values carry one of a fixed set of
reasons, and a reported zero is never treated as missing.

**Checks each extraction against the paper's survey.** A QC agent compares
every extraction with the survey of the same paper and the schema, and sends
back targeted repairs, which resume the original extractor session.

**Normalizes terminology without losing the original.** Raw values are
mapped to standardized IDs through a deterministic table lookup first and an
LLM pass second. The raw value stays in its own field and is never
overwritten. A reconciliation ledger keeps the mapping history, including
disputed mappings.

**Tests whether the data is fit for meta-analysis.** A readiness reviewer
asks whether the extracted data preserves what a downstream analyst needs and
proposes the smallest schema change that would fix any gap. The schema is then
retested on fresh subsets of papers until it holds up across three rotations
in a row, before the final full-corpus extraction.

**Supports human review.** A risk-ranked queue points reviewers at the fields
most likely to matter or be wrong. Markdown review packets go with it, and
reviewer corrections are recorded with an audit trail and survive
re-extraction. An exporter produces immutable snapshots for a reviewer portal,
with each source quote resolved to a highlight box on the PDF page.

**Prepares papers before extraction.** `paper_prep` converts each PDF into a
bundle of sections, tables and figures. Docling parses the layout, Claude
transcribes each table, a deterministic merge compares the two, and an
optional arbiter resolves conflicts. Each LLM call has a cost cap and runs
with tightly scoped permissions: it can read its inputs and write one output
file, nothing else.

**Calibrates itself against a gold standard.** The harness watches extraction
runs and scores them against a gold standard you supply, combining coverage,
fill rate and LLM-judged accuracy. It traces failures to the instruction that
caused them, then proposes one general instruction change at a time. Each
change must pass an overfitting check, and is kept or reverted on the score.

**Keeps domain knowledge out of the agents.** Agent prompts, scripts and hooks
are domain-independent. Everything specific to a research area lives in a
separate domain repository: reference notes, the schema, normalization tables,
the gold standard and the papers themselves.

## Status

Comparanda is research software under active development. It was built on its
first domain, DPYD pharmacogenomics (gene variants that predict severe
toxicity from fluoropyrimidine chemotherapy), and parts still show it.
Batch paper prep keys on PubMed IDs, and the default gold-standard layout
expects a biomedical effect-size spreadsheet. Agent instructions, file
contracts and evaluation metrics still change between versions.

A few limits to know about:

- Most of the pipeline's discipline, including phase order, convergence rules
  and keep-or-revert decisions, lives in agent instructions that a model
  follows. The provenance validator, finalizers and hooks are the parts
  enforced in code.
- Only `paper_prep` has cost caps. An extraction run costs whatever the agents
  spend, so watch the first runs closely.
- The harness measures how much of the gold standard's evidence was
  recovered. It doesn't measure whether a meta-analysis built on the output
  is valid.
- The reviewer portal's user interface is not part of this repository; only
  the exporter is.

Treat the output as material for expert review, not a finished dataset.

## Responsibility boundary

Comparanda prepares the evidence. It does not choose the research question,
decide which studies to include, pick a statistical model, run the
meta-analysis, or make guideline or policy recommendations. Those decisions
belong to the analyst. Its output is meant to carry what the analyst needs for
each of them:

1. exploring candidate research questions and choosing one;
2. deciding inclusion and exclusion;
3. choosing a suitable statistical model; and
4. building the model's inputs and running it.

Known analyst decisions can be used to test whether the extraction preserved
enough evidence and context for those steps. They are not outputs Comparanda
tries to reproduce. This boundary governs feature design, agent scopes and
evaluation claims.

## Requirements

- **[Claude Code](https://docs.claude.com/en/docs/claude-code) CLI**, installed
  and authenticated (`claude` on your `PATH`). The extraction pipeline and the
  harness run as Claude Code sessions, and `paper_prep` calls `claude -p` as a
  subprocess. Model usage is billed to your Claude Code account.
- **[uv](https://docs.astral.sh/uv/)** for every Python entry point.
- **Python 3.12+** (uv can install it). The extraction hooks run as `python3`
  scripts, so `python3` must be on your `PATH`.
- **jq**, used by the harness guard hook.
- **PyMuPDF**, only for the reviewer-portal exporter and its tests, pulled in
  on demand with `uv run --with pymupdf`.
- **Model access.** Most extraction agents use `claude-sonnet-5[1m]`
  (Sonnet with a 1M-token context window), set in the `model:` line of each
  file in `comparanda/.claude/agents/`. The survey agent uses `haiku`; the
  harness agents use `haiku`, `sonnet` or `opus`. If your account can't use a
  model, change that line. The harness's field-coverage eval defaults to
  `claude-sonnet-5[1m]` and can be changed with `COMPARANDA_EVAL_MODEL` or
  `--model`.

## Getting started

[`docs/getting-started.md`](./docs/getting-started.md) walks through a full
run: setting up a domain repository, turning a handful of open-access PDFs
into bundles, running the extraction pipeline, and optionally evaluating it
against your own gold standard.

To prepare papers on their own:

```bash
cd comparanda/scripts/paper_prep && uv sync

# One paper
uv run python -m paper_prep <paper_id> --pdf /abs/path/paper.pdf \
  --output-root /abs/path/data/processed

# A corpus: PDFs named like "Smith 2024 (J Example) [12345678].pdf"
uv run python -m paper_prep.batch --subset subset.json \
  --pdfs-dir /abs/path/pdfs --bundle-out /abs/path/data/processed
```

Run these from a regular terminal rather than inside a Claude Code session.
See [`comparanda/scripts/paper_prep/README.md`](./comparanda/scripts/paper_prep/README.md)
for the subset format, budgets and permission scoping.

Paper IDs take the form `author_year_documentid`, for example
`smith_2024_12345678`. For biomedical papers the document ID is usually the
PubMed ID, and it must match the harness's `document_id_map.json` if you
evaluate against a gold standard.

## How it works

Comparanda has two parts in this repository: the extraction pipeline in
[`comparanda/`](./comparanda/) and the calibration harness in
[`harness/`](./harness/).

### Extraction pipeline (`comparanda/`)

[`comparanda/CLAUDE.md`](./comparanda/CLAUDE.md) orchestrates the run and
hands each phase to an agent in
[`comparanda/.claude/agents/`](./comparanda/.claude/agents/).
[`comparanda/AGENT_INDEX.md`](./comparanda/AGENT_INDEX.md) is the reference
for agent scopes, file contracts, scripts, hooks and resume behavior.

| Phase | Agent | What happens |
|---|---|---|
| 0 | survey, schema-proposer, extractor, normalizer, ma-critique (reference mode) | One-time domain onboarding: each agent writes its own domain reference file from your notes. |
| 0.5 | `schema-proposer` | Writes the survey schema: what to catalog in each paper. |
| 1 | `survey`, then `subset-selector` | Surveys every paper, then picks 3–4 diverse papers for the first iteration. |
| 2 | `schema-proposer` | First iteration only: turns the surveys and domain notes into the extraction schema. |
| 3a | `extractor` | Extracts rows from each paper's bundle through the extraction SDK. |
| 3a-QC | `extraction-qc` | Checks each extraction against its survey and the schema; requests targeted repairs. |
| 3b | `normalizer` | Maps raw values to standardized IDs, deterministic lookup first, LLM second. |
| 3.5 | `ma-critique` | Judges meta-analysis readiness and decides the next action. |
| 4 | `schema-proposer` | Applies the readiness review's schema changes, then loops back to 3a. |
| 5 | `subset-selector` | Rotates to fresh papers to test whether the schema generalizes; converges after three clean rotations in a row. |
| 6 | `extractor`, `extraction-qc`, `normalizer` | Final full-corpus extraction in bounded batches. |

Besides the extraction table, a run leaves a versioned schema, normalization
tables, critiques and an append-only observation log that lets the next
session pick up where the last one stopped.

Supporting pieces:

- **Hooks** in `comparanda/.claude/hooks/`, registered in
  `comparanda/.claude/settings.json`: `validate_extraction_write.py` blocks
  direct writes to extraction files, and `ensure_extraction_finalized.py`
  keeps the extractor from stopping before a validated finalization. See
  [`AGENT_INDEX.md`](./comparanda/AGENT_INDEX.md#guardrails).
- **Scripts** in `comparanda/scripts/`: the extraction SDK and validator,
  the normalization API, review and export tools, the observation log, and
  `paper_prep`. Start with
  [`comparanda/scripts/README.md`](./comparanda/scripts/README.md).
- **Contracts** in `comparanda/docs/`: the extraction SDK and normalization
  API as the agents see them.
- **Skill** in `comparanda/.claude/skills/comparanda/`: background on the
  pipeline that Claude Code can load when it needs cross-phase context.

### Calibration harness (`harness/`)

The harness runs the pipeline, scores the result against a gold standard,
and improves the agents' instructions one change at a time. Its walkthrough is
[`harness/CLAUDE.md`](./harness/CLAUDE.md); its reference is
[`harness/AGENT_INDEX.md`](./harness/AGENT_INDEX.md).

| Agent | Role |
|---|---|
| `monitor` | Reads the pipeline's state and observation log and reports where the run stands. |
| `eval` | Scores extractions against the gold standard: fast aggregate metrics, and a detailed mode that judges accuracy. |
| `diagnostician` | Traces each failure to the instruction that caused it and decides whether an instruction change can fix it. |
| `critic` | Proposes one domain-independent instruction change as a proposal file. The harness orchestrator applies it, re-runs, and keeps or reverts it on the score. |

The composite score weights row coverage 0.30, entity coverage 0.15, field
fill rate 0.05 and judged accuracy 0.50 (`harness/src/harness/eval/composite.py`).

A guard hook (`harness/.claude/hooks/harness-guardrail-comparanda-writes.sh`)
limits the harness's file writes inside `comparanda/` to the agent prompt
files and the pipeline orchestrator `comparanda/CLAUDE.md`, and blocks writes
to domain data outside the configured iterations directory. It covers the
Write and Edit tools; the rule that the harness never changes Python or hook
code is an instruction, not an enforced check.

The gold standard and evaluation config come from your domain repository or
from explicit command-line arguments. This repository doesn't ship one.

`harness-commit`, a console script in `harness/pyproject.toml`, routes commits
for an older two-branch fork layout. You don't need it.

## Domain repositories

Comparanda's code stays in this repository. Each research area gets its own
repository, which includes Comparanda as a Git submodule and holds that
area's papers, schema, extractions and run history:

```text
comparanda-<domain>/
├── comparanda/                 submodule -> this repository
├── export/                     optional submodule -> reviewer-portal snapshots
├── schema/, surveys/, extractions/, observations/
├── normalization/, critiques/, domain/
├── data/raw/                   source PDFs (Git LFS works well)
├── data/processed/             paper_prep bundles
├── benchmark/                  evaluation config and gold standard
├── iterations/                 harness run history
└── comparanda-project.json     path contract
```

Changes to Comparanda itself are committed inside the submodule; domain data
and run state are committed in the domain repository. See
[`docs/domain-superproject.md`](./docs/domain-superproject.md) for the path
contract and [`CLAUDE.md`](./CLAUDE.md) for the setup walkthrough.

## Repository layout

```
CLAUDE.md            setup walkthrough and top-level routing for Claude Code
comparanda/          extraction pipeline
  CLAUDE.md          phase-by-phase orchestrator
  AGENT_INDEX.md     agent scopes, file contracts, scripts, hooks, resume
  .claude/           agents, hooks, skill, settings
  docs/              extraction SDK and normalization API contracts
  scripts/           extraction, normalization, review, export, paper_prep
  tests/
harness/             calibration harness
  CLAUDE.md          improvement-loop walkthrough
  AGENT_INDEX.md     agent scopes, eval artifacts, resume
  .claude/           agents, guard hook, settings
  src/harness/       evaluation, monitoring and commit tooling
  tests/
docs/                getting started, domain repositories, paper_prep, design notes
```

## Running tests

None of the test suites call a model.

```bash
# Extraction pipeline scripts and hooks
cd comparanda && uv run --with pytest --with pymupdf pytest tests

# Calibration harness
cd harness && uv run pytest

# paper_prep
cd comparanda/scripts/paper_prep && uv run pytest
```

## Contributing

Contributions to the system code are welcome; domain data belongs in domain
repositories, not here. See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for setup,
tests, pull requests and the DCO sign-off every commit needs.

## Security

Comparanda runs LLM agents that use tools on your machine and read untrusted
PDFs. `paper_prep` scopes its `claude -p` calls tightly, but the harness's
detailed evaluation launches its eval agent with
`--permission-mode bypassPermissions`. Read [`SECURITY.md`](./SECURITY.md)
before running it on inputs you don't trust, and report vulnerabilities
privately as described there.

## License

Comparanda is licensed under the [GNU Affero General Public License v3.0
only](./LICENSE) (`AGPL-3.0-only`). If you run a modified version as a network
service, you must offer its source to the service's users.
