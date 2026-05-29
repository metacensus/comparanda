#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""PreToolUse hook: gate Comparanda extraction writes at the boundary.

Fires on every Bash tool call. Fast-paths to allow for any command that
isn't a `write_extraction.py` invocation. For matching commands:

  1. Parses the CLI args (--rows-file, --meta-file, --schema, --bundle,
     --prior).
  2. Loads the staging payloads and the schema.
  3. Runs the shared validator (extraction_validator.py).
  4. On clean: allows the call (exit 0, no stdout).
  5. On failure: emits a PreToolUse deny with the rendered failure list as
     the reason. The extractor sees the deny in-loop, fixes the staging
     files, and re-invokes Bash with corrected args.

This replaces the legacy `validate_provenance_quotes.py` SubagentStop hook.
The legacy hook was blind to rows that omitted the `provenance` key
entirely (the actual contract-drift bug); the new gate enforces "every
non-null non-exempt field has a well-formed provenance entry" before any
file is written.

Fire-trace appends to .claude/hooks/.state/extraction-write-trace.log for
post-hoc inspection.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = HOOK_DIR.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Lazy-import the validator only after we've decided this Bash call is in
# scope — avoids a yaml import (and the uv resolve) for every unrelated
# Bash command.


WRITE_SCRIPT_PATTERN = re.compile(r"write_extraction\.py\b")


def project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    return HOOK_DIR.parent.parent


def state_dir() -> Path:
    d = project_root() / ".claude" / "hooks" / ".state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fire_trace(payload: dict) -> None:
    try:
        log = state_dir() / "extraction-write-trace.log"
        with log.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                **payload,
            }) + "\n")
    except OSError:
        pass


def allow() -> int:
    """No-op exit — the call proceeds."""
    return 0


def deny(reason: str) -> int:
    """Block the Bash call. The agent receives `reason` in-context and
    can retry."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


def parse_write_extraction_args(argv: list[str]) -> dict:
    """Pull the args we care about out of a write_extraction.py argv slice.
    Returns dict with keys: mode, paper_id, schema, bundle, out, rows_file,
    meta_file, prior (None for iter1). Missing keys → None."""
    out: dict[str, str | None] = {
        "mode": None, "paper_id": None, "schema": None, "bundle": None,
        "out": None, "rows_file": None, "meta_file": None, "prior": None,
    }
    # First positional after the script path is the subcommand.
    expect_value_for: str | None = None
    seen_subcommand = False
    flag_to_key = {
        "--paper-id": "paper_id", "--schema": "schema",
        "--bundle": "bundle", "--out": "out",
        "--rows-file": "rows_file", "--meta-file": "meta_file",
        "--prior": "prior",
    }
    for tok in argv:
        if expect_value_for is not None:
            out[expect_value_for] = tok
            expect_value_for = None
            continue
        if tok in ("iter1", "itern") and not seen_subcommand:
            out["mode"] = tok
            seen_subcommand = True
            continue
        if tok in flag_to_key:
            expect_value_for = flag_to_key[tok]
            continue
        # Support --flag=value form too.
        if "=" in tok:
            k, _, v = tok.partition("=")
            if k in flag_to_key:
                out[flag_to_key[k]] = v
                continue
    return out


def find_write_extraction_argv(command: str) -> list[str] | None:
    """Tokenize the bash command and locate the argv for a write_extraction.py
    invocation. Returns the argv slice starting at the subcommand, or None
    if the command isn't a write_extraction.py call."""
    if not WRITE_SCRIPT_PATTERN.search(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Unparseable; let the hook allow and let Bash error out itself.
        return None
    for i, tok in enumerate(tokens):
        if WRITE_SCRIPT_PATTERN.search(tok):
            return tokens[i + 1 :]
    return None


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        return allow()

    if hook_input.get("hook_event_name") not in (None, "PreToolUse"):
        return allow()
    if hook_input.get("tool_name") != "Bash":
        return allow()

    command = (hook_input.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return allow()

    argv = find_write_extraction_argv(command)
    if argv is None:
        return allow()

    args = parse_write_extraction_args(argv)
    mode = args["mode"]

    if mode not in ("iter1", "itern"):
        return deny(
            "write_extraction.py invoked without a valid subcommand "
            "(expected 'iter1' or 'itern')."
        )

    missing = [
        flag for flag, key in (
            ("--paper-id", "paper_id"), ("--schema", "schema"),
            ("--bundle", "bundle"), ("--out", "out"),
            ("--rows-file", "rows_file"), ("--meta-file", "meta_file"),
        ) if not args[key]
    ]
    if mode == "itern" and not args["prior"]:
        missing.append("--prior")
    if missing:
        return deny(
            f"write_extraction.py {mode} missing required flag(s): "
            f"{', '.join(missing)}"
        )

    # Now import the validator — only after we've confirmed this call is
    # in scope and well-formed enough to validate.
    try:
        import extraction_validator as ev  # type: ignore
    except Exception as e:
        return deny(
            f"extraction_validator import failed: {type(e).__name__}: {e}"
        )

    try:
        schema = ev.Schema.load(Path(args["schema"]))
    except Exception as e:
        return deny(f"failed to load schema {args['schema']!r}: {e}")

    try:
        bundle_text = ev.load_bundle_text(Path(args["bundle"]))
    except Exception as e:
        return deny(f"failed to load bundle {args['bundle']!r}: {e}")
    if not bundle_text:
        return deny(
            f"bundle at {args['bundle']!r} produced no text — cannot "
            f"validate quotes"
        )

    try:
        with open(args["rows_file"]) as f:
            rows_payload = json.load(f)
    except Exception as e:
        return deny(f"failed to load rows-file {args['rows_file']!r}: {e}")
    try:
        with open(args["meta_file"]) as f:
            meta_payload = json.load(f)
    except Exception as e:
        return deny(f"failed to load meta-file {args['meta_file']!r}: {e}")

    failures: list = []

    if mode == "itern":
        try:
            prior_path = Path(args["prior"])
            prior_lines: list[dict] = []
            with prior_path.open() as f:
                for ln, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    prior_lines.append(json.loads(line))
        except Exception as e:
            return deny(f"failed to load prior {args['prior']!r}: {e}")

        prior_rows = [r for r in prior_lines if not r.get("_meta_commentary")]
        prior_meta = next(
            (r for r in prior_lines if r.get("_meta_commentary")), None,
        )

        declared_prior = rows_payload.get("prior_schema_version")
        if declared_prior is not None and prior_meta is not None:
            actual_prior = prior_meta.get("schema_version")
            if declared_prior != actual_prior:
                failures.append(ev.Failure(
                    "prior_schema_version_mismatch", "rows-file",
                    f"rows.prior_schema_version={declared_prior!r} but "
                    f"prior JSONL declares schema_version={actual_prior!r}",
                ))

        failures.extend(ev.validate_prior_quotes(
            prior_rows, bundle_text=bundle_text,
        ))

    failures.extend(ev.validate_rows_payload(
        rows_payload, schema=schema, bundle_text=bundle_text, mode=mode,
    ))
    failures.extend(ev.validate_meta_payload(meta_payload, mode=mode))

    fire_trace({
        "paper_id": args["paper_id"],
        "mode": mode,
        "rows_file": args["rows_file"],
        "schema": args["schema"],
        "failure_count": len(failures),
        "decision": "deny" if failures else "allow",
    })

    if failures:
        return deny(ev.render_failures(failures))

    print(
        f"[validate_extraction_write] allowed write_extraction.py {mode} "
        f"for paper {args['paper_id']} (0 failures)",
        file=sys.stderr,
    )
    return allow()


if __name__ == "__main__":
    raise SystemExit(main())
