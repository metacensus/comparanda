"""argv construction for the scoped `claude -p` subprocess calls.

Both LLM steps (per-table first-pass, Stage-2 arbiter) need the same thing
from the Claude Code CLI: read a few input files, write exactly ONE output
file, and nothing else. Rather than skipping permission checks, each call
runs with:

- `--restricted`: no Bash / code-running tools / WebFetch, user + project
  settings files ignored (so a local allowlist can't widen the scope), and
  the file tools confined to the working directories (cwd + `--add-dir`).
- `--strict-mcp-config` (with no `--mcp-config`): no MCP servers.
- `--tools Read,Write,Edit`: the only built-in tools available.
- `--permission-mode dontAsk`: anything not pre-approved is denied rather
  than prompted for, so the call never blocks waiting for input.
- `--allowedTools Read,Edit(./<output>)`: reads within the working
  directories, and writes (Write/Edit) to the single output file only. The
  output path is given relative to the subprocess cwd, which must contain it.
  The rule is matched against the resolved path, so prompts must name the
  output by its resolved path (or relative to cwd): on macOS a write to
  `/tmp/...` is denied when the cwd resolves to `/private/tmp/...`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

ALLOWED_BUILTIN_TOOLS = "Read,Write,Edit"


def build_claude_argv(
    prompt: str,
    *,
    cwd: Path,
    output_path: Path,
    model: str,
    max_budget_usd: float,
    read_dirs: Iterable[Path] = (),
    effort: str | None = None,
) -> list[str]:
    """Return the argv for a scoped `claude -p` call that writes `output_path`.

    `cwd` must be the directory the subprocess runs in and must contain
    `output_path`. `read_dirs` are extra directories the model may read
    (passed via `--add-dir`); directories already under `cwd` are dropped.
    """
    cwd = Path(cwd).resolve()
    output_path = Path(output_path).resolve()
    try:
        rel_out = output_path.relative_to(cwd)
    except ValueError as e:
        raise ValueError(f"output_path {output_path} is not under cwd {cwd}") from e

    extra_dirs: list[str] = []
    for d in read_dirs:
        d = Path(d).resolve()
        if d == cwd or d.is_relative_to(cwd) or str(d) in extra_dirs:
            continue
        extra_dirs.append(str(d))

    argv = ["claude", "-p", prompt, "--model", model]
    if effort:
        argv += ["--effort", effort]
    argv += [
        "--max-budget-usd", f"{max_budget_usd:.2f}",
        "--output-format", "json",
        "--restricted",
        "--strict-mcp-config",
        "--tools", ALLOWED_BUILTIN_TOOLS,
        "--permission-mode", "dontAsk",
        "--allowedTools", f"Read,Edit(./{rel_out.as_posix()})",
    ]
    if extra_dirs:
        argv += ["--add-dir", *extra_dirs]
    return argv

