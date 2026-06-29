# Research: Codex CLI Capabilities -- MCP, Skills, Plugins, and Tool Extension

- **Query**: Does OpenAI Codex CLI support `--tools`, `--mcp-config`, `--allowedTools`, MCP servers, custom skills, or plugin/extension mechanisms?
- **Scope**: External (openai/codex GitHub repo + official docs)
- **Date**: 2026-06-29

## Findings

### 1. MCP Server Support (YES -- fully supported)

Codex CLI has **first-class MCP server support** with a dedicated `codex mcp` subcommand and rich configuration.

#### CLI Commands

| Command | Purpose |
|---------|---------|
| `codex mcp add <NAME> -- <COMMAND>...` | Add a stdio MCP server |
| `codex mcp add <NAME> --url <URL>` | Add a streamable HTTP MCP server |
| `codex mcp remove <NAME>` | Remove an MCP server |
| `codex mcp list [--json]` | List configured MCP servers |
| `codex mcp get <NAME> [--json]` | Show a single server config |
| `codex mcp login <NAME>` | Authenticate via OAuth |
| `codex mcp logout <NAME>` | Remove OAuth credentials |

Source: `codex-rs/cli/src/mcp_cmd.rs`

#### Config File Format (`~/.codex/config.toml`)

MCP servers are configured in the `[mcp_servers]` section of `config.toml`. Two transport types are supported:

**Stdio transport:**
```toml
[mcp_servers.my-server]
command = "npx"
args = ["-y", "@some/mcp-server"]
env = { API_KEY = "..." }
cwd = "/path/to/dir"
```

**Streamable HTTP transport:**
```toml
[mcp_servers.my-server]
url = "https://example.com/mcp"
bearer_token_env_var = "MY_API_KEY"
```

**Per-server options:**
- `enabled` (bool) -- enable/disable the server
- `required` (bool) -- `codex exec` errors if this server fails to initialize
- `supports_parallel_tool_calls` (bool) -- advertise tools as parallel-safe
- `startup_timeout_sec` / `startup_timeout_ms` -- startup timeout
- `tool_timeout_sec` -- per-call timeout
- `default_tools_approval_mode` -- "auto" | "prompt" | "approve"
- `enabled_tools` -- allow-list of tool names from this server
- `disabled_tools` -- deny-list of tool names
- `scopes` -- OAuth scopes
- `oauth.client_id` -- OAuth client identifier
- `oauth_resource` -- OAuth resource parameter (RFC 8707)
- `auth` -- "oauth" (default) or "chatgpt"
- `tools.<tool_name>.approval_mode` -- per-tool approval override

Source: `codex-rs/config/src/mcp_types.rs` (`McpServerConfig`, `RawMcpServerConfig`)

#### MCP as Codex Server

Codex can also **run as an MCP server itself**:
```bash
codex mcp-server | your_mcp_client
```
This exposes Codex's thread/turn/account/config APIs over the MCP protocol.

Source: `codex-rs/docs/codex_mcp_interface.md`

---

### 2. Skills System (YES -- fully supported)

Codex CLI has a **Skills** system that provides custom instructions, workflows, and tool dependencies.

#### Skill Discovery Paths

Skills are discovered from multiple roots (in priority order):

| Root | Scope | Path Pattern |
|------|-------|-------------|
| Project config | `Repo` | `<project>/.codex/skills/<name>/SKILL.md` |
| User config (deprecated) | `User` | `$CODEX_HOME/skills/<name>/SKILL.md` |
| User home | `User` | `$HOME/.agents/skills/<name>/SKILL.md` |
| System | `System` | `$CODEX_HOME/skills/.system/<name>/SKILL.md` |
| Admin | `Admin` | `/etc/codex/skills/<name>/SKILL.md` |
| Plugin | `User` | Via plugin skill roots |

Source: `codex-rs/core-skills/src/loader.rs` (lines 287-380)

#### Skill File Format (SKILL.md)

Each skill is a directory containing at minimum a `SKILL.md` file with YAML frontmatter:

```markdown
---
name: my-skill
description: Description of what this skill does
metadata:
  short-description: One-liner shown in UI
---

# Skill Title

## Objective
...instructions for the agent...
```

Optional additional files per skill:
- `agents/openai.yaml` -- Agent interface definition (display_name, short_description, default_prompt)
- `scripts/` -- Helper scripts the skill can invoke
- `references/` -- Reference docs the skill can read

Source: `.codex/skills/babysit-pr/SKILL.md`, `.codex/skills/code-review/SKILL.md`

#### Agent YAML Format (`agents/openai.yaml`)

```yaml
interface:
  display_name: "PR Babysitter"
  short_description: "Watch PR review comments, CI, and merge conflicts"
  default_prompt: "Babysit the current PR: ..."
```

Source: `.codex/skills/babysit-pr/agents/openai.yaml`

#### Skill Dependencies on MCP Servers

Skills can declare MCP server dependencies. When a skill is invoked, Codex will prompt to auto-install missing MCP servers.

The `SkillToolDependency` model supports:
```yaml
dependencies:
  tools:
    - type: mcp
      value: server-name
      transport: streamable_http  # or "stdio"
      url: https://example.com/mcp
      # OR for stdio:
      # command: npx
      description: "What this MCP server provides"
```

Source: `codex-rs/core-skills/src/model.rs` (`SkillToolDependency`), `codex-rs/core/src/mcp_skill_dependencies.rs`

---

### 3. Plugin System (YES -- supported)

Codex CLI has a **Plugin** system with marketplace support.

#### CLI Commands

| Command | Purpose |
|---------|---------|
| `codex plugin add <PLUGIN[@MARKETPLACE]>` | Install a plugin |
| `codex plugin list [--json] [--available]` | List installed/available plugins |
| `codex plugin remove <PLUGIN[@MARKETPLACE]>` | Uninstall a plugin |
| `codex plugin marketplace ...` | Manage plugin marketplaces |

Source: `codex-rs/cli/src/plugin_cmd.rs`

#### Plugin Sources

Plugins can come from:
- **Local** -- filesystem path
- **Git** -- git URL with optional ref/sha/subdirectory
- **Npm** -- npm package with optional version/registry

Source: `codex-rs/cli/src/plugin_cmd.rs` (`MarketplacePluginSource`)

#### Marketplace Config (in config.toml)

```toml
[marketplaces.my-marketplace]
source_type = "git"
source = "https://github.com/example/codex-plugins"
# OR
source_type = "local"
source = "/path/to/marketplace"
```

Source: `codex-rs/cli/src/plugin_cmd.rs` (`configured_marketplace_sources`)

Plugins provide skill roots and are namespaced by marketplace name. A plugin with skill directories is automatically discovered as additional skill roots.

---

### 4. CLI Flags -- No `--tools` / `--allowedTools` / `--mcp-config` Flags

The Codex CLI does **not** have direct `--tools`, `--allowedTools`, or `--mcp-config` command-line flags for extending tools at invocation time. Instead:

- MCP servers are configured via `config.toml` and managed via `codex mcp add/remove/list`
- Skills are discovered from filesystem paths (not CLI flags)
- Plugins are installed via `codex plugin add`

#### Available CLI Flags (SharedCliOptions)

| Flag | Purpose |
|------|---------|
| `--model` / `-m` | Select model |
| `--sandbox` / `-s` | Sandbox policy |
| `--profile` / `-p` | Config profile |
| `--cd` / `-C` | Working directory |
| `--add-dir` | Additional writable directories |
| `--oss` | Use open-source provider |
| `--local-provider` | Specify LM Studio / Ollama |
| `--image` / `-i` | Attach images |
| `--dangerously-bypass-approvals-and-sandbox` | Skip approvals |
| `--dangerously-bypass-hook-trust` | Skip hook trust |

Source: `codex-rs/utils/cli/src/shared_options.rs`

#### Exec-specific flags

| Flag | Purpose |
|------|---------|
| `--output-schema` | JSON Schema for structured output |
| `--json` | Print events as JSONL |
| `--output-last-message` / `-o` | Write last message to file |
| `--ephemeral` | Don't persist session |
| `--ignore-user-config` | Skip config.toml |
| `--ignore-rules` | Skip execpolicy rules |
| `--skip-git-repo-check` | Allow non-git directories |

Source: `codex-rs/exec/src/cli.rs`

**The `--full-auto` flag is deprecated.** It has been replaced by `--sandbox workspace-write`.

---

### 5. Relevant to Our Codex Adapter (`crates/uc-engine/src/sandbox/agents/codex.rs`)

Our current adapter uses:
```rust
args: vec![prompt.to_string(), "--full-auto".to_string()]
```

This needs updating because:
1. `--full-auto` is deprecated; should use `--sandbox workspace-write` or `codex exec` subcommand
2. MCP servers can be pre-configured in `$CODEX_HOME/config.toml` before launching codex
3. Skills can be placed in `<project>/.codex/skills/` for automatic discovery
4. The `codex exec` subcommand supports `--json` for structured output parsing

---

### Files Found (External -- openai/codex repo)

| File Path | Description |
|---|---|
| `codex-rs/cli/src/mcp_cmd.rs` | MCP CLI subcommand implementation |
| `codex-rs/config/src/mcp_types.rs` | MCP config types (McpServerConfig, transport, approval) |
| `codex-rs/core/src/mcp_skill_dependencies.rs` | Auto-install MCP dependencies for skills |
| `codex-rs/core-skills/src/model.rs` | SkillMetadata, SkillToolDependency models |
| `codex-rs/core-skills/src/loader.rs` | Skill discovery and loading from filesystem roots |
| `codex-rs/cli/src/plugin_cmd.rs` | Plugin CLI subcommand with marketplace support |
| `codex-rs/cli/src/main.rs` | Top-level CLI with all subcommands |
| `codex-rs/utils/cli/src/shared_options.rs` | Shared CLI flags |
| `codex-rs/exec/src/cli.rs` | Non-interactive exec CLI flags |
| `codex-rs/docs/codex_mcp_interface.md` | Codex MCP server interface docs |
| `.codex/skills/babysit-pr/SKILL.md` | Example skill with scripts, agents, references |
| `.codex/skills/code-review/SKILL.md` | Example skill (orchestrator pattern) |
| `.github/codex/home/config.toml` | Example config.toml |

### Files Found (Internal -- UltimateCoders repo)

| File Path | Description |
|---|---|
| `crates/uc-engine/src/sandbox/agents/codex.rs` | Current Codex adapter (uses deprecated `--full-auto`) |

### External References

- [OpenAI Codex CLI GitHub](https://github.com/openai/codex) -- Main repo
- [Codex Documentation](https://developers.openai.com/codex) -- Official docs (JS-rendered, hard to scrape)
- [Codex CLI MCP Servers config](https://developers.openai.com/codex/configuration/mcp) -- Official MCP config docs (JS-rendered)

## Caveats / Not Found

1. **No `--tools` / `--mcp-config` CLI flags exist.** Tool extension is done through config.toml + `codex mcp add`, not via command-line flags at invocation time. There is no way to pass MCP config inline at `codex exec` invocation.
2. **No `--allowedTools` flag.** Tool allow/deny lists are per-MCP-server config options in config.toml, not CLI flags.
3. **The official docs site** (developers.openai.com/codex) is JavaScript-rendered and could not be fully scraped. The nav structure shows pages for: MCP, Plugins, Skills, Config File, Config Reference, Sample Config. The repo source code is the authoritative reference.
4. **`--full-auto` is removed/deprecated.** The flag still exists as a hidden legacy trap that warns to use `--sandbox workspace-write` instead.
5. **Skill MCP dependency auto-install** is gated behind a feature flag (`SkillMcpDependencyInstall`) and only works for first-party clients currently.
6. **Config profiles** (`--profile` / `-p`) layer `$CODEX_HOME/<name>.config.toml` on top of the base config, which could be used to create named MCP+skill configurations, but this is not a direct `--mcp-config` flag.
