# Comparanda — top-level orchestration

This repo holds the Comparanda extraction system. When asked to do something,
route to the right place:

- **"Run the system" / "extract from these papers" / "iterate the schema"** →
  `cd comparanda/` and follow `comparanda/CLAUDE.md`.

- **"What's the architecture" / "where's the spec" / "show me the diagrams"** →
  read `docs/` (specifically `docs/specs/`, `docs/presentation/`,
  `docs/research/`).

`comparanda/` has its own `CLAUDE.md`, `.claude/agents/`, and `pyproject.toml`.
Treat it as a self-contained project — `cd` into it before invoking agents or
running tests.

## Application data is not in this repo

Per-domain application state (schemas, observation logs, extraction outputs,
domain references, PDFs) lives in sibling per-domain repos. The system code
here is built to operate against an external `projects/<domain>/` tree; it does
not ship with one.
