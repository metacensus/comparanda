# Contributing to Comparanda

Thanks for your interest. Comparanda is research software under active
development, so interfaces and agent instructions still change often. Small,
focused contributions with tests are the easiest to review.

## What belongs in this repository

This repository holds **system code only**: the inner-loop agents, hooks, and
scripts (`comparanda/`), the calibration harness (`harness/`), the paper-prep
preprocessor (`comparanda/scripts/paper_prep/`), and cross-cutting docs
(`docs/`).

In scope:

- Bug fixes and tests for scripts, hooks, the harness, and paper-prep.
- Domain-independent improvements to agent instructions in
  `comparanda/.claude/agents/` and `harness/.claude/agents/`. A useful check:
  if your domain disappeared, would the change still help?
- Documentation fixes, especially for the setup path in
  [`docs/getting-started.md`](./docs/getting-started.md).

Out of scope:

- **Domain data.** Schemas, surveys, extractions, normalization tables,
  critiques, gold standards, PDFs, bundles, and run history belong in a domain
  superproject (see [`docs/domain-superproject.md`](./docs/domain-superproject.md)),
  not here.
- Domain-specific vocabulary or rules in the shared agent instructions. Those
  belong in a domain's own `domain/*_ref.md` files.
- Changes that move Comparanda past its
  [responsibility boundary](./README.md#responsibility-boundary), such as
  choosing research questions, making eligibility decisions, or selecting
  statistical models.

## Setup

You need the tools listed under [Requirements](./README.md#requirements):
uv, Python 3.12+, and jq, plus the Claude Code CLI if you want to run the
agents. Then:

```bash
git clone <comparanda-repo-url>
cd comparanda
(cd harness && uv sync)
(cd comparanda/scripts/paper_prep && uv sync)
```

The inner-loop scripts in `comparanda/` do not have their own project file;
their tests run through `uv run --with ...` as shown below.

## Running tests

Run the suite for each component you touched:

```bash
cd comparanda && uv run --with pytest --with pymupdf pytest tests
cd harness && uv run pytest
cd comparanda/scripts/paper_prep && uv run pytest
```

These suites do not call a model. If you change agent instructions, also
describe in your pull request how you exercised the change (for example, which
phase you reran and on what kind of corpus), since prompt behavior is not
covered by unit tests.

## Style

- Python, managed with uv. Do not add `pip install` steps to docs or scripts.
- Tests use pytest and live next to the component (`comparanda/tests/`,
  `harness/tests/`, `comparanda/scripts/paper_prep/tests/`).
- Follow the style of the surrounding code: type hints, `pathlib.Path`, small
  functions, and clear module docstrings.
- Tools take explicit paths. Do not hardcode or infer domain project
  locations; resolve them through `comparanda/scripts/project_layout.py` or
  the `COMPARANDA_*` environment variables.
- Never commit secrets, API keys, or personal data.

## Branches and pull requests

1. Fork the repository and branch from `main`, using a descriptive branch
   name (for example `fix/normalizer-reconcile-empty-table`).
2. Keep each pull request to one logical change, and add or update tests
   for behavior changes.
3. Make sure the relevant test suites pass locally.
4. In the description, explain what changed and why, how you tested it, and
   any effect on file contracts (`AGENT_INDEX.md`) or docs. Update those docs
   in the same pull request.
5. A maintainer will review it. Expect questions about generality for any
   change to agent instructions.

The `harness-commit` console script is optional maintainer tooling for legacy
domain forks. Contributors do not need it.

## Reporting bugs and requesting features

Open a GitHub issue in this repository. Include:

- what you ran (command, phase, or agent), and from which directory;
- what you expected and what happened, with relevant log or error output;
- your versions of `claude`, `uv`, and Python, and your OS.

Do not attach copyrighted PDFs or private domain data. A minimal synthetic
example is better. Report security problems privately as described in
[`SECURITY.md`](./SECURITY.md), not in a public issue.

## Contributor terms and license

License: to be announced before the first tagged release. Contributor terms
will be announced with the license.
