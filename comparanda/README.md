# Comparanda extraction pipeline

This directory holds the extraction pipeline: the Claude Code agents, hooks
and scripts that survey a collection of papers, discover an extraction schema,
extract source-linked rows, normalize them, and test whether the result is
ready for meta-analysis. For what Comparanda is and how to run it, start with
the [repository README](../README.md) and
[`docs/getting-started.md`](../docs/getting-started.md).

Run the pipeline from this directory, so Claude Code picks up its agents,
hooks and settings:

```bash
cd comparanda
claude
```

[`CLAUDE.md`](./CLAUDE.md) is the orchestrator the session follows. It needs
an external domain repository; see
[`docs/domain-superproject.md`](../docs/domain-superproject.md).

## Contents

```
CLAUDE.md            phase-by-phase orchestrator
AGENT_INDEX.md       agent scopes, file contracts, scripts, hooks, resume behavior
.claude/
  agents/            survey, subset-selector, schema-proposer, extractor,
                     extraction-qc, normalizer, ma-critique
  hooks/             extraction write and finalization guards
  skills/            pipeline background Claude Code can load
  settings.json      hook registration
docs/                extraction SDK and normalization API contracts
scripts/             extraction, normalization, review, export, paper_prep;
                     see scripts/README.md
tests/               script and hook tests (no model calls)
```

Run the tests from this directory:

```bash
uv run --with pytest --with pymupdf pytest tests
```
