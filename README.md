# Comparanda — MetaCensus

Agentic extraction engine for [MetaCensus](https://github.com/metacensus). Given a
domain and a corpus of scientific papers, Comparanda discovers a structured schema,
for meta-analysis.

## Layout

- `comparanda/` — the system. Agents, library, tests, build config.
  See `comparanda/CLAUDE.md` for orchestration and `comparanda/README.md` for an overview.
- `docs/` — architecture, specs, research, presentation diagrams. Cross-cutting docs
  that describe the system rather than implement it.

## Application data lives elsewhere

Per-domain application instances — actual schemas, observation logs, extraction
state, PDFs, domain references — live in separate repos so they can iterate
independently of system development. To use Comparanda on a new domain, build out
a sibling application repo following the same shape; do not add a
`projects/<domain>/` directory back into this repo.
