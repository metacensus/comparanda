#!/bin/bash
# Harness guardrail on Comparanda writes: prevent harness-side agents from
# modifying comparanda files except agent .md instructions and the inner-loop
# orchestrator.
#
# ALLOW: ../comparanda/.claude/agents/*.md  (universal agent instructions)
# ALLOW: ../comparanda/CLAUDE.md            (inner-loop orchestrator — phase sequence)
# DENY:  everything else under ../comparanda/ (extractions, schema, domain, observations, etc.)
# ALLOW: anything in the harness's own directory (iterations/, src/, etc.)

set -euo pipefail

input=$(cat)
file_path=$(echo "$input" | jq -r '.tool_input.file_path // .tool_input.file // empty')

# If no file path (e.g., Bash tool), allow — Bash is handled by a separate hook
if [ -z "$file_path" ]; then
  exit 0
fi

# Resolve to absolute path for reliable matching
abs_path=$(cd "$(dirname "$file_path" 2>/dev/null)" 2>/dev/null && echo "$(pwd)/$(basename "$file_path")" 2>/dev/null || echo "$file_path")

# Check if the path is under comparanda (the system under calibration)
comparanda_dir=$(cd "$CLAUDE_PROJECT_DIR/../comparanda" 2>/dev/null && pwd || echo "")

if [ -z "$comparanda_dir" ]; then
  # Can't resolve comparanda dir — allow (probably not targeting comparanda)
  exit 0
fi

# Is this file under comparanda/?
if [[ "$abs_path" == "$comparanda_dir"/* ]]; then
  # It's a comparanda file. Allow .claude/agents/*.md and the orchestrator CLAUDE.md.
  if [[ "$abs_path" == "$comparanda_dir/.claude/agents/"*.md ]] \
     || [[ "$abs_path" == "$comparanda_dir/CLAUDE.md" ]]; then
    # ALLOWED: agent instruction files + inner-loop orchestrator
    exit 0
  else
    # DENIED: any other comparanda file
    rel_path="${abs_path#$comparanda_dir/}"
    echo "{\"hookSpecificOutput\":{\"permissionDecision\":\"deny\"},\"systemMessage\":\"BLOCKED: System 2 may only modify comparanda agent instructions (.claude/agents/*.md) and the inner-loop orchestrator (CLAUDE.md). Attempted to modify: $rel_path\"}" >&2
    exit 2
  fi
fi

# Not a comparanda file — allow (harness's own files are fine)
exit 0
