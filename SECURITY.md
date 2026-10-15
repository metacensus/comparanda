# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub's private vulnerability reporting: open this repository's **Security** tab and choose **Report a vulnerability**.
Do not open a public issue, pull request, or discussion for a suspected vulnerability.

Include what you found, the affected files or commands, steps to reproduce, and the impact you expect.
We will acknowledge the report, keep you informed while we investigate, and credit you in the fix if you wish.

Comparanda is research software without tagged releases yet, so fixes land on `main`.

## What to know before running Comparanda

Comparanda is not a sandbox.
Read this before running it.

- **It runs LLM agents that execute tools on your machine.**
  The inner loop (`comparanda/`) and the harness (`harness/`) are Claude Code sessions whose agents read and write files and run commands, subject to your Claude Code permission settings and the repository's hooks.
  The hooks enforce workflow rules, such as forcing extraction writes through validated finalizers and keeping the harness from editing code.
  They are not a security boundary.
- **Paper content is untrusted input.**
  Text, tables, and figures from PDFs are fed to models.
  A crafted document could try to steer an agent (prompt injection).
  Run Comparanda only on PDFs you trust, such as papers obtained from reputable publishers or repositories, and review agent output before acting on it.
- **paper-prep invokes `claude -p` with scoped permissions.**
  Each per-table and arbiter call runs with `--restricted`, `--strict-mcp-config` and no MCP servers, built-in tools limited to `Read,Write,Edit`, `--permission-mode dontAsk`, and writes allowed only to that call's single output file, with a per-call `--max-budget-usd` cap (see `comparanda/scripts/paper_prep/src/paper_prep/claude_cli.py`).
  This limits, but does not eliminate, what a malicious PDF could cause.
- **Docling parses PDFs locally.**
  As with any PDF parser, malformed files can trigger bugs in the parsing stack.
- **Costs are real.**
  Agent runs spend against your Claude Code subscription or API key.
  Use the budget flags described in [`docs/getting-started.md`](./docs/getting-started.md).

Recommended practice: run Comparanda as an unprivileged user, in a dedicated directory or a disposable VM/container when processing documents from sources you do not control, and do not keep unrelated secrets in the working directories its agents can read.

## Scope

In scope: code, hooks, agent instructions, and default configuration in this repository, including ways the documented guardrails can be bypassed.

Out of scope: vulnerabilities in Claude Code, Docling, or other third-party dependencies (report those to their maintainers), and issues that require an attacker to already control your machine or your Claude Code configuration.
