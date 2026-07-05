#!/usr/bin/env python3
"""PreToolUse gate (review note): the extraction JSONL may ONLY be produced by
`scripts/extraction/write_extraction.py` or the unified `scripts/extraction/extraction_builder.py`
finalization path, never by a direct Write/Edit.

The provenance/value contract (extraction_validator.py) is enforced inside
write_extraction.py. If an agent could Write or Edit `extractions/<paper>.jsonl`
directly it would bypass every check — quote-at-location, value-in-quote,
derivation arithmetic, the lot. This hook denies any Write/Edit/MultiEdit/
NotebookEdit whose target is an extraction JSONL (outside `.staging/`), and it
also denies Bash commands that attempt to write or mutate an extraction JSONL
without going through `write_extraction.py`.

This is the "primary gate" the validator/script docstrings reference; before
this file it did not exist (only the SubagentStop backstop did).
"""
from __future__ import annotations

import json
import re
import sys

# extractions/<something>.jsonl, but NOT under a .staging/ directory.
_EXTRACTION_JSONL = re.compile(r"(^|/)extractions/(?!.*/\.staging/)[^/]*\.jsonl$")
_EXTRACTION_JSONL_IN_COMMAND = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_./~:-]+/)?extractions/[^\s'\";]+\.jsonl)"
)
_WRITE_MODE = re.compile(r"['\"][wax+][bt+]*['\"]")
_ALLOWED_FINALIZERS = (
    "scripts/extraction/write_extraction.py",
    "scripts/extraction/extraction_builder.py",
    "scripts/extraction/run_extraction_build.py",
)


def _is_final_extraction_jsonl(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if "/extractions/.staging/" in normalized or normalized.startswith("extractions/.staging/"):
        return False
    return _EXTRACTION_JSONL.search(normalized) is not None


def _paths(tool_input: dict) -> list[str]:
    out: list[str] = []
    for key in ("file_path", "file", "notebook_path", "path"):
        v = tool_input.get(key)
        if isinstance(v, str):
            out.append(v)
    # MultiEdit-style {edits: [{file_path}...]} or {files:[...]}
    for key in ("edits", "files"):
        seq = tool_input.get(key)
        if isinstance(seq, list):
            for e in seq:
                if isinstance(e, dict) and isinstance(e.get("file_path"), str):
                    out.append(e["file_path"])
                elif isinstance(e, str):
                    out.append(e)
    return out


def _bash_direct_target(tool_input: dict) -> str | None:
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    if any(path in command for path in _ALLOWED_FINALIZERS):
        return None

    targets = [
        match.group("path")
        for match in _EXTRACTION_JSONL_IN_COMMAND.finditer(command)
        if _is_final_extraction_jsonl(match.group("path"))
    ]
    if not targets:
        return None

    # Allow read-only polling/inspection commands that mention extraction paths
    # (`wc -l`, `head`, `tail`, `rg`, Python open(..., "r"), etc.). Deny only
    # shell shapes that create, overwrite, append to, remove, or mutate final
    # extraction JSONL files.
    if re.search(r"(?:^|[\s;&|])(?:>|>>|1>|1>>)\s*[^\s'\";]*extractions/", command):
        return targets[0]
    if re.search(r"\btee\b(?:\s+-a)?[^\n;]*extractions/", command):
        return targets[0]
    if re.search(r"\b(?:cp|mv|rm|touch|install|truncate)\b[^\n;]*extractions/", command):
        return targets[0]
    if re.search(r"\b(?:sed\s+-i|perl\s+-pi)\b[^\n;]*extractions/", command):
        return targets[0]
    if re.search(r"\bpython(?:3)?\b", command):
        if re.search(r"\bwrite_(?:text|bytes)\s*\(", command):
            return targets[0]
        for target in targets:
            open_call = re.compile(
                r"open\s*\([^)]*"
                + re.escape(target)
                + r"[^)]*,[^)]*"
                + _WRITE_MODE.pattern
            )
            path_open_call = re.compile(
                r"Path\s*\([^)]*"
                + re.escape(target)
                + r"[^)]*\)\.open\s*\([^)]*"
                + _WRITE_MODE.pattern
            )
            if open_call.search(command) or path_open_call.search(command):
                return target
    return None


def _deny(path: str) -> int:
    msg = (
        "BLOCKED: extraction JSONL must be produced by "
        "scripts/extraction/write_extraction.py (iter1|itern) or "
        "scripts/extraction/extraction_builder.py finalize, never written directly. "
        "Direct writes bypass provenance + value validation "
        "(quote-at-location, value-in-quote, derivation arithmetic). "
        f"Attempted direct write to: {path}. Emit rows.json/meta.json staging "
        "files and invoke the sanctioned finalization script instead."
    )
    print(json.dumps({
        "hookSpecificOutput": {"permissionDecision": "deny"},
        "systemMessage": msg,
    }), file=sys.stderr)
    return 2


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool_input = hook_input.get("tool_input") or {}
    for p in _paths(tool_input):
        if _is_final_extraction_jsonl(p):
            return _deny(p)
    bash_target = _bash_direct_target(tool_input)
    if bash_target:
        return _deny(bash_target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
