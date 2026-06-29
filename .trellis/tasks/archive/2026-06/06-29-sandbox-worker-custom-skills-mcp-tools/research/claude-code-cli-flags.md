# Research: Claude Code CLI Customization Flags

- **Query**: What flags does Claude Code CLI support for customization (--tools, --allowedTools, --disallowedTools, --mcp-config, --append-system-prompt, --agent, --agents, --skill)? How do --agents, --mcp-config, and skills work?
- **Scope**: mixed (CLI introspection + local file inspection)
- **Date**: 2026-06-29

## Findings

### CLI Version

Claude Code v2.1.187

### Flag Summary Table

| Flag | Type | Description |
|---|---|---|
| `--tools <tools...>` | built-in tool filter | Specify available built-in tools. Use `""` to disable all built-in tools, `"default"` for all built-in tools, or list specific names (e.g. `"Bash,Edit,Read"`). |
| `--allowedTools, --allowed-tools <tools...>` | permission allowlist | Comma or space-separated list of tool names to allow (e.g. `"Bash(git *) Edit"`). This is additive/permission-based, not a strict replacement of `--tools`. |
| `--disallowedTools, --disallowed-tools <tools...>` | permission denylist | Comma or space-separated list of tool names to deny (e.g. `"Bash(git *) Edit"`). |
| `--mcp-config <configs...>` | MCP server config | Load MCP servers from JSON files or inline JSON strings (space-separated). |
| `--strict-mcp-config` | MCP restriction | Only use MCP servers from `--mcp-config`, ignoring all other MCP configurations (project `.mcp.json`, settings, plugins). |
| `--append-system-prompt <prompt>` | system prompt | Append a system prompt to the default system prompt. |
| `--system-prompt <prompt>` | system prompt | Replace the entire system prompt (not append). |
| `--system-prompt-file <path>` | system prompt | Load system prompt from a file (not shown in --help but referenced in --bare docs). |
| `--append-system-prompt-file <path>` | system prompt | Load appended system prompt from a file (referenced in --bare docs). |
| `--agent <agent>` | agent selection | Agent for the current session. Overrides the `agent` setting. Selects a named agent by its name. |
| `--agents <json>` | agent definition | JSON object defining custom agents for this session. |
| `--settings <file-or-json>` | settings | Path to a settings JSON file or a JSON string to load additional settings from. |
| `--disable-slash-commands` | skills | Disable all skills (slash commands). |
| `--bare` | minimal mode | Skip hooks, LSP, plugin sync, attribution, auto-memory, background prefetches, keychain reads, CLAUDE.md auto-discovery. Skills still resolve via `/skill-name`. |
| `--safe-mode` | safe mode | Start with all customizations disabled (CLAUDE.md, skills, plugins, hooks, MCP servers, custom commands and agents, output styles, workflows, custom themes, keybindings). Auth, model, built-in tools, permissions still work. |
| `--plugin-dir <path>` | plugins | Load a plugin from a directory or .zip for this session only (repeatable). |
| `--plugin-url <url>` | plugins | Fetch a plugin .zip from a URL for this session only (repeatable). |
| `--add-dir <directories...>` | directories | Additional directories to allow tool access to. |

### `--agents` Flag: JSON Format

The `--agents` flag accepts a JSON object where each key is an agent name and each value is an object with:

```json
{
  "reviewer": {
    "description": "Reviews code for bugs and security issues",
    "prompt": "You are a code reviewer. Focus on bugs and security issues."
  },
  "writer": {
    "description": "Writes documentation",
    "prompt": "You write clear documentation."
  }
}
```

**Fields per agent:**
- `description` (string) -- short description shown in agent picker/list
- `prompt` (string) -- full system prompt for the agent

**Verified behavior:**
- Custom agents defined via `--agents` appear in the agent list alongside built-in and plugin-provided agents.
- Example invocation: `claude --agents '{"reviewer": {"description": "Reviews code", "prompt": "You are a code reviewer."}}'`
- Combined with `--agent reviewer`, selects the custom agent for the session.

**Agent file format (`.claude/agents/<name>.md`):**

Agents can also be defined persistently in `.claude/agents/` directory as markdown files with YAML frontmatter:

```markdown
---
name: trellis-research
description: |
  Code and tech search expert. Finds files, patterns, and tech solutions.
tools: Read, Write, Glob, Grep, Bash, mcp__exa__web_search_exa
---

# Research Agent

You are the Research Agent in the Trellis workflow.
...
```

**Agent frontmatter fields:**
- `name` (string) -- agent identifier
- `description` (string) -- short description for agent picker
- `tools` (string, comma-separated) -- restrict which tools the agent can use
- `model` (string, optional) -- model override (e.g. "sonnet")
- `color` (string, optional) -- display color in UI (e.g. "green")

**Precedence:** `--agents` CLI flag > `.claude/agents/` project files > plugin-provided agents

### `--mcp-config` Flag: File Format

The `--mcp-config` flag accepts either:
1. A path to a JSON file
2. An inline JSON string
3. Multiple configs (space-separated)

**MCP config JSON format:**

```json
{
  "mcpServers": {
    "server-name": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp"],
      "env": {
        "API_KEY": "xxx"
      }
    },
    "another-server": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer token123"
      }
    },
    "sse-server": {
      "type": "sse",
      "url": "https://sse.example.com/events"
    }
  }
}
```

**Server types:**
- `stdio` -- spawns a subprocess, requires `command` + optional `args` + optional `env`
- `http` -- HTTP-based MCP server, requires `url`, optional `headers`
- `sse` -- Server-Sent Events MCP server, requires `url`

**`--strict-mcp-config` behavior:**
- When used with `--mcp-config`, ONLY the servers from `--mcp-config` are loaded.
- All other MCP configs (project `.mcp.json`, settings.json, plugins) are ignored.
- Example: `claude --mcp-config '{"mcpServers":{}}' --strict-mcp-config` results in zero MCP servers.
- Without `--strict-mcp-config`, `--mcp-config` servers are added on top of existing configs.

**Project-level MCP config (`.mcp.json`):**
- Placed at project root as `.mcp.json`
- Same JSON format as above
- Servers from `.mcp.json` require user approval on first use (shown as "Pending approval")
- Can be pre-approved via `enableAllProjectMcpServers: true` in `settings.local.json`

### `--tools` Flag Behavior

- `--tools ""` -- disables ALL built-in tools (only MCP tools remain)
- `--tools "default"` -- enables all built-in tools
- `--tools "Bash,Read,Glob,Grep"` -- enables only the listed built-in tools
- MCP tools are NOT affected by `--tools`; use `--strict-mcp-config` with empty `--mcp-config` to disable MCP tools
- To have only specific built-in tools AND no MCP tools: `--tools "Bash,Read" --mcp-config '{"mcpServers":{}}' --strict-mcp-config`

**Full list of built-in tool names:**
Bash, Read, Write, Edit, Glob, Grep, LSP, WebSearch, WebFetch, Agent, Workflow, Skill, NotebookEdit, NotebookRead, CronCreate, CronDelete, CronList, ScheduleWakeup, TaskCreate, TaskUpdate, TaskList, TaskGet, TodoWrite, KillShell, BashOutput, LS

### `--allowedTools` and `--disallowedTools`

These are permission-level controls, not tool availability controls:
- `--allowedTools "Bash(git *)"` -- allows Bash but only for git commands
- `--disallowedTools "Edit Write"` -- denies Edit and Write tools
- Supports glob patterns in parentheses: `Bash(git *)`, `Bash(cargo *)`
- These are additive to the default tool set -- they don't replace `--tools`

### Skills Mechanism

There is NO `--skill` flag. Skills are invoked via:
1. **Slash commands** -- `/skill-name` in interactive mode (e.g. `/commit`, `/deep-research`)
2. **Skill tool** -- The `Skill` tool can be invoked programmatically by agents
3. **Skills directory** -- `~/.claude/skills/<name>/SKILL.md` (user-level) or `.claude/skills/<name>/SKILL.md` (project-level)
4. **Plugin skills** -- Plugins can bundle skills in their `skills/` directory

**Skill file format (`SKILL.md`):**

```markdown
---
name: skill-name
description: When to trigger and what this skill does
metadata:
  author: Author Name
---

# Skill Title

Instructions for the skill...
```

**Key points:**
- `--disable-slash-commands` disables ALL skills (but the Skill tool may still exist)
- Skills are resolved even in `--bare` mode
- In print mode (`-p`), skills are not directly usable via slash commands, but the `Skill` built-in tool can invoke them
- The `Skill` tool takes `skill` (name) and optional `args` parameters

### `--agent` Flag

Selects an agent by name for the current session. Overrides the `agent` setting from settings.json.

```bash
claude --agent trellis-research
claude --agent reviewer  # if defined via --agents or .claude/agents/
```

### `--append-system-prompt` Flag

Appends text to the default system prompt (does not replace it).

```bash
claude --append-system-prompt "Always respond in JSON format."
claude -p --append-system-prompt "You are a code reviewer." "Review this code"
```

For file-based prompts: `--append-system-prompt-file <path>` (referenced in `--bare` docs but not in `--help`).

### `--settings` Flag

Load additional settings from a file or inline JSON. This can include any settings.json keys:

```bash
claude --settings /path/to/settings.json
claude --settings '{"permissions": {"allow": ["Bash(git *)"]}}'
```

### Combining Flags for Sandbox/Worker Customization

For a sandboxed worker that needs custom agents, restricted tools, and custom MCP servers:

```bash
claude -p \
  --tools "Bash,Read,Write,Edit,Glob,Grep" \
  --mcp-config '{"mcpServers":{"my-server":{"type":"stdio","command":"my-mcp-server"}}}' \
  --strict-mcp-config \
  --agents '{"worker":{"description":"Code worker","prompt":"You implement code changes."}}' \
  --agent worker \
  --append-system-prompt "You are a sandboxed worker. Only modify files in /workspace." \
  --dangerously-skip-permissions \
  "Implement the feature described in task.md"
```

### Additional Relevant Flags for Programmatic Use

| Flag | Purpose |
|---|---|
| `-p, --print` | Non-interactive mode, print response and exit |
| `--output-format <format>` | `text`, `json`, or `stream-json` (only with `--print`) |
| `--input-format <format>` | `text` or `stream-json` (only with `--print`) |
| `--json-schema <schema>` | JSON Schema for structured output validation |
| `--max-budget-usd <amount>` | Maximum dollar amount to spend (only with `--print`) |
| `--model <model>` | Model alias or full name |
| `--effort <level>` | `low`, `medium`, `high`, `xhigh`, `max` |
| `--permission-mode <mode>` | `acceptEdits`, `auto`, `bypassPermissions`, `default`, `dontAsk`, `plan` |
| `--dangerously-skip-permissions` | Bypass all permission checks |
| `--no-session-persistence` | Sessions not saved to disk (only with `--print`) |
| `--session-id <uuid>` | Use specific session ID |
| `--add-dir <dirs>` | Additional directories to allow tool access |
| `--bare` | Minimal mode, skip most customizations |
| `--safe-mode` | All customizations disabled |

## Files Found

| File Path | Description |
|---|---|
| `.mcp.json` | Project-level MCP server config (codegraph stdio) |
| `.claude/settings.json` | Project settings (hooks, plugins) |
| `.claude/settings.local.json` | Project local settings (permissions, MCP approvals) |
| `.claude/agents/trellis-implement.md` | Custom agent definition with tools restriction |
| `.claude/agents/trellis-check.md` | Custom agent definition with tools restriction |
| `.claude/agents/trellis-research.md` | Custom agent definition with tools restriction |
| `.claude/skills/tmux-ide/SKILL.md` | User-level skill example |
| `~/.claude/settings.json` | Global user settings (env, permissions, plugins) |
| `~/.claude/plugins/cache/.../agents/code-architect.md` | Plugin-provided agent with model/color fields |

## Caveats / Not Found

- `--append-system-prompt-file` and `--system-prompt-file` are referenced in the `--bare` flag description but do NOT appear in `--help` output. They may be undocumented or recently added flags.
- The exact `--agents` JSON schema is inferred from the `--help` example and live testing. Additional fields beyond `description` and `prompt` may exist (e.g. `tools`, `model`) but were not tested.
- The `--allowedTools` behavior appears additive rather than restrictive in testing (it adds permissions rather than restricting to only those tools). More precise behavior may depend on permission mode.
- No `--skill` flag exists. Skills can only be invoked via the `Skill` built-in tool or slash commands.
- The Skill tool's `args` parameter format is not documented in CLI help.
