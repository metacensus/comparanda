# claude-agent-sdk Reference

> Extracted 2026-04-03 from SDK research. Version: 0.1.55 (installed: 0.1.53)

## Overview

The `claude-agent-sdk` wraps the Claude Code CLI as a subprocess. Each agent/query spawns a `claude` CLI process. It is NOT the `anthropic` Python SDK — it's specifically for driving Claude Code programmatically.

## Key Finding

**The SDK does NOT auto-discover `.claude/agents/*.md` files.** Agent definitions are passed programmatically via `ClaudeAgentOptions.agents`. However, `setting_sources=["project"]` does load `.claude/settings.json` and `CLAUDE.md`.

## Two APIs

### `query()` — fire-and-forget, one-shot

```python
from claude_agent_sdk import query, ClaudeAgentOptions

async for message in query(
    prompt="Analyze this code for bugs",
    options=ClaudeAgentOptions(
        cwd="/path/to/project",
        permission_mode="bypassPermissions",
        setting_sources=["project"],
        model="sonnet",
        max_turns=10,
    )
):
    if hasattr(message, "result"):
        print(f"Result: {message.result}")
        print(f"Cost: ${message.total_cost_usd}")
```

### `ClaudeSDKClient` — multi-turn, interruptible

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

client = ClaudeSDKClient(options=ClaudeAgentOptions(
    permission_mode="bypassPermissions",
    cwd="/path/to/project",
))
await client.connect("Initial prompt here")
async for msg in client.receive_messages():
    pass
await client.query("Follow-up")
await client.disconnect()
```

## Agent Definitions (programmatic)

```python
from claude_agent_sdk import ClaudeAgentOptions, AgentDefinition

options = ClaudeAgentOptions(
    agents={
        "extractor": AgentDefinition(
            description="Extracts structured data from papers",
            prompt="You are a pharmacogenetics extraction agent...",
            tools=["Read", "Write", "Bash"],
            model="sonnet",
            maxTurns=15,
        ),
    },
    permission_mode="bypassPermissions",
    cwd="/path/to/project",
)
```

### AgentDefinition fields
- `description: str`
- `prompt: str` — system prompt
- `tools: list[str] | None` — e.g. `["Read", "Write", "Bash", "Glob", "Grep"]`
- `disallowedTools: list[str] | None`
- `model: str | None` — `"sonnet"`, `"opus"`, `"haiku"`, `"inherit"`, or full model ID
- `maxTurns: int | None`
- `skills: list[str] | None`
- `memory: "user" | "project" | "local" | None`
- `mcpServers: list[str | dict] | None`
- `initialPrompt: str | None`

## Structured Output

```python
options = ClaudeAgentOptions(
    output_format={
        "type": "json_schema",
        "schema": { ... }
    },
)

async for msg in query(prompt=prompt, options=options):
    if hasattr(msg, "structured_output"):
        result = msg.structured_output  # Already parsed
```

## Tool Access

Full Claude Code tool access by default (Read, Write, Edit, Bash, Glob, Grep). Controllable:
```python
tools=["Read", "Bash", "Grep"]           # whitelist
disallowed_tools=["Write", "Edit"]        # blacklist
permission_mode="bypassPermissions"       # no confirmation prompts
```

## Concurrency

Each `query()` spawns an independent subprocess. Use `asyncio.gather()`:
```python
results = await asyncio.gather(
    run_agent("extract", prompt1, cwd1),
    run_agent("review", prompt2, cwd2),
)
```

## Bridge Pattern: .md files → AgentDefinition

Since the SDK doesn't auto-discover `.claude/agents/`, build a loader:

```python
def load_agent_definition(project_dir: Path, agent_name: str) -> AgentDefinition:
    path = project_dir / ".claude" / "agents" / f"{agent_name}.md"
    frontmatter, body = parse_md_frontmatter(path.read_text())
    return AgentDefinition(
        description=frontmatter.get("description", ""),
        prompt=body,
        model=frontmatter.get("model", "sonnet"),
        tools=frontmatter.get("tools"),
        maxTurns=frontmatter.get("maxTurns", 20),
    )
```

## Custom MCP Tools (in-process Python)

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("score_extraction", "Score extraction against gold standard",
      {"extraction": dict, "gold": dict})
async def score_extraction(args):
    score = compute_f1(args["extraction"], args["gold"])
    return {"content": [{"type": "text", "text": json.dumps({"f1": score})}]}

server = create_sdk_mcp_server("tools", tools=[score_extraction])
options = ClaudeAgentOptions(mcp_servers={"comparanda": server})
```

## Session Management

```python
from claude_agent_sdk import list_sessions, get_session_messages
sessions = list_sessions(directory="/path/to/project", limit=10)
messages = get_session_messages(session_id="abc123")
```
