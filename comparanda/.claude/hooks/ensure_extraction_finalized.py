#!/usr/bin/env python3
"""SubagentStop hook: enforce that the extractor produced a validated JSONL.

Fires when a subagent attempts to stop. The SubagentStop hook input gives us
the *parent* session's transcript_path, not the subagent's. The subagent
transcripts live at:

  <parent_dir>/<parent_session_id>/subagents/agent-<id>.jsonl

with a sibling `agent-<id>.meta.json` carrying `agentType` (e.g.
"extractor"). We scope to the most-recently-modified extractor subagent
transcript and check for the script's success marker:

  1. Find the just-stopped extractor subagent transcript.
  2. If none found → not an extractor stop, allow.
  3. Scan that transcript for `[write_extraction iter1|itern] wrote N rows`.
  4. If found → allow the stop.
  5. If not found → block with a structured reason instructing the agent to
     invoke the script (or use an empty-diff itern invocation for legitimate
     no-op cases).

Includes a silent escape hatch: after MAX_ATTEMPTS consecutive blocks for the
same subagent transcript, allow the stop to prevent infinite loops. The
escape hatch is invisible to the agent — its block reason is never altered.

Trace appends to .claude/hooks/.state/extraction-finalize-trace.log.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent

# Agent type as declared in the subagent meta.json sidecar.
EXTRACTOR_AGENT_TYPE = "extractor"

# How long ago (seconds) the subagent transcript must have been touched to
# count as the just-stopped one. Generous window because extraction runs can
# pause for minutes on Bash subprocesses.
RECENT_WINDOW_SECONDS = 300

# Success marker emitted by scripts/write_extraction.py on a clean write.
SUCCESS_MARKER = re.compile(
    r"\[write_extraction (iter1|itern)\] wrote \d+ rows? to "
)

# Silent escape hatch — after this many blocked stops for the same transcript,
# allow the stop. Prevents infinite loops if the agent truly cannot recover.
MAX_ATTEMPTS = 3

BLOCK_REASON = (
    "Extractor cannot stop without producing a validated JSONL via "
    "scripts/write_extraction.py. Either:\n"
    "  (a) invoke scripts/write_extraction.py iter1|itern and observe a "
    "`[write_extraction ...] wrote N rows to <path>` result in stderr, or\n"
    "  (b) for an iter-n with zero field/row changes, invoke "
    "scripts/write_extraction.py itern with a rows-file containing "
    '{"schema_version": "...", "identity_dimensions": [...], "rows": [], '
    '"removed_fields": [], "removed_rows": []} to confirm the no-op '
    "explicitly.\n"
    "The script is the sole gate — do not write the JSONL manually."
)


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
        log = state_dir() / "extraction-finalize-trace.log"
        with log.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                **payload,
            }) + "\n")
    except OSError:
        pass


import time


def find_extractor_subagent_transcript(parent_transcript: Path) -> Path | None:
    """Find the most-recently-modified extractor subagent transcript adjacent
    to the parent session's transcript. Returns None if no extractor stopped
    recently (treat as "not an extractor stop")."""
    # Parent transcript is at <dir>/<session_id>.jsonl; subagent dir is
    # <dir>/<session_id>/subagents/.
    session_dir = parent_transcript.parent / parent_transcript.stem
    subagents_dir = session_dir / "subagents"
    if not subagents_dir.is_dir():
        return None

    now = time.time()
    best: Path | None = None
    best_mtime = -1.0
    for meta in subagents_dir.glob("*.meta.json"):
        try:
            data = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("agentType") != EXTRACTOR_AGENT_TYPE:
            continue
        log = meta.parent / meta.name.replace(".meta.json", ".jsonl")
        if not log.exists():
            continue
        try:
            m = log.stat().st_mtime
        except OSError:
            continue
        if now - m > RECENT_WINDOW_SECONDS:
            continue
        if m > best_mtime:
            best_mtime = m
            best = log
    return best


def transcript_has_success(transcript_path: Path) -> bool:
    """Scan transcript lines (covers tool_result content and any text)
    for the script's success marker."""
    try:
        with transcript_path.open() as f:
            for line in f:
                if SUCCESS_MARKER.search(line):
                    return True
    except OSError:
        return False
    return False


def attempt_counter_path(transcript_path: Path) -> Path:
    key = hashlib.sha256(str(transcript_path).encode()).hexdigest()[:16]
    return state_dir() / f"finalize-attempts-{key}.txt"


def read_attempts(p: Path) -> int:
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return 0


def write_attempts(p: Path, n: int) -> None:
    try:
        p.write_text(str(n))
    except OSError:
        pass


def allow() -> int:
    return 0


def block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        return allow()

    if hook_input.get("hook_event_name") not in (None, "SubagentStop"):
        return allow()

    parent_transcript_str = hook_input.get("transcript_path")
    if not isinstance(parent_transcript_str, str):
        fire_trace({"decision": "allow", "reason": "no_transcript_path"})
        return allow()

    parent_transcript = Path(parent_transcript_str)
    if not parent_transcript.exists():
        fire_trace({
            "decision": "allow",
            "reason": "parent_transcript_missing",
            "parent_transcript": parent_transcript_str,
        })
        return allow()

    subagent_transcript = find_extractor_subagent_transcript(parent_transcript)
    if subagent_transcript is None:
        fire_trace({
            "decision": "allow",
            "reason": "no_recent_extractor_subagent",
            "parent_transcript": parent_transcript_str,
        })
        return allow()

    if transcript_has_success(subagent_transcript):
        counter_path = attempt_counter_path(subagent_transcript)
        try:
            counter_path.unlink()
        except OSError:
            pass
        fire_trace({
            "decision": "allow",
            "reason": "success_marker_found",
            "subagent_transcript": str(subagent_transcript),
        })
        return allow()

    counter_path = attempt_counter_path(subagent_transcript)
    attempts = read_attempts(counter_path) + 1
    write_attempts(counter_path, attempts)

    if attempts >= MAX_ATTEMPTS:
        fire_trace({
            "decision": "allow",
            "reason": "max_attempts_escape",
            "attempts": attempts,
            "subagent_transcript": str(subagent_transcript),
        })
        return allow()

    fire_trace({
        "decision": "block",
        "attempts": attempts,
        "subagent_transcript": str(subagent_transcript),
    })
    return block(BLOCK_REASON)


if __name__ == "__main__":
    raise SystemExit(main())
