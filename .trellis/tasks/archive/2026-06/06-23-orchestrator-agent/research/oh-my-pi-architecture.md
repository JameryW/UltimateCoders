# Research: oh-my-pi Architecture

- **Query**: Research the oh-my-pi project (https://github.com/can1357/oh-my-pi) -- kernel architecture, core abstractions, agent loop design, tool system, orchestration, prompt engineering, context/memory management, multi-step reasoning
- **Scope**: External
- **Date**: 2026-06-23

## Findings

### Project Overview

oh-my-pi (omp) is an open-source AI coding agent for the terminal, forked from Mario Zechner's [Pi](https://github.com/badlogic/pi-mono). It is described as "a coding agent with the IDE wired in" and ships 40+ providers, 32 built-in tools, 14 LSP ops, 28 DAP ops, and ~55k lines of Rust core. The runtime is Bun (TypeScript), with performance-critical operations in Rust (`crates/pi-natives`, `crates/pi-shell`, `crates/pi-iso`).

**Key packages:**

| Package | Description |
|---|---|
| `packages/agent` | Agent runtime: agent-loop, tool calling, state management, compaction |
| `packages/ai` | Multi-provider LLM client with streaming, dialect system, auth broker |
| `packages/catalog` | Model catalog, bundled models.json, provider descriptors, model identity |
| `packages/coding-agent` | Main CLI application (primary package) |
| `packages/tui` | Terminal UI library with differential rendering |
| `packages/natives` | Bindings for native text/image/grep operations |
| `crates/pi-natives` | Rust crate for performance-critical text/grep ops |
| `crates/pi-shell` | Shell integration: output minimizer, process management |
| `crates/pi-iso` | Filesystem isolation (overlayfs, APFS, btrfs, ZFS reflinks) |
| `crates/pi-ast` | AST operations (tree-sitter based) |

---

### 1. Main Agent Loop / Kernel Design

The core agent loop lives in `packages/agent/src/agent-loop.ts` (~2089 lines). It implements a **turn-based loop with streaming, tool execution, and steering**.

**Entry points:**
- `agentLoop(prompts, context, config, signal, streamFn)` -- starts a new loop with user prompts
- `agentLoopContinue(context, config, signal, streamFn)` -- continues from current context (for retries)
- `agentLoopDetailed(...)` / `agentLoopContinueDetailed(...)` -- variants exposing telemetry/coverage

**Loop structure (in `runLoopBody`):**

```
Outer loop (continues when follow-up/steering messages arrive):
  Inner loop (continues while tool calls or pending messages exist):
    1. Flush pending messages (steering, asides) into context
    2. Sync context before model call (config.syncContextBeforeModelCall)
    3. Resolve per-turn tool-choice directive (hard ToolChoice or SoftToolRequirement)
    4. Stream assistant response via streamAssistantResponse()
       - Converts AgentMessage[] -> Message[] via convertToLlm
       - Applies context transforms
       - Builds LLM context (system prompt + tools + messages)
       - Handles owned/in-band dialect if configured
       - Streams response with abort race
       - Detects Harmony (GPT-5) protocol leakage
    5. Execute tool calls via executeToolCalls() if stop reason is toolUse/stop
    6. Handle soft tool requirement gate (skip non-required tools, escalate)
    7. Handle pause_turn continuations (up to MAX_PAUSED_TURN_CONTINUATIONS=8)
    8. Emit turn_end event
    9. Poll for steering messages + asides
  Yield boundary:
    - onBeforeYield hook
    - Drain late steering + asides + follow-up messages
    - If any messages found, continue outer loop
    - Otherwise, exit
```

**Key design characteristics:**
- **EventStream architecture**: The loop returns an `EventStream<AgentEvent, AgentMessage[]>` that emits granular lifecycle events (agent_start/end, turn_start/end, message_start/update/end, tool_execution_start/update/end)
- **Abort-aware**: Every LLM call and tool execution races against an AbortSignal; mid-stream aborts produce well-formed truncated assistant messages
- **Steering**: User messages can interrupt mid-batch tool execution when `interruptMode="immediate"`; an `interruptState` flag + per-tool AbortController handles clean cutoff
- **Soft tool requirements**: A `SoftToolRequirement` injects a reminder first, then escalates to a forced `toolChoice` only if the model declines (up to MAX_SOFT_TOOL_ESCALATIONS=3), avoiding premature cache invalidation
- **Harmony leak mitigation**: Detects GPT-5 Harmony protocol leakage in assistant responses; truncates and resumes on first occurrence, abort-retries on second, escalates on third
- **Deadline support**: Absolute wall-clock deadline in epoch ms; timer-based AbortController merged with external signal
- **Yield control**: `yieldIfDue()` prevents busy-wait; `EventLoopKeepalive` prevents premature process exit

**The `Agent` class** (`packages/agent/src/agent.ts`) wraps the loop with a stateful session:
- Manages `AgentState` (system prompt, model, tools, messages, streaming state)
- Maintains steering queue, follow-up queue, aside message provider
- `prompt(input)` / `steer(message)` / `followUp(message)` as public API
- Configures `AgentLoopConfig` from session options and delegates to `agentLoop()`

**`AgentSession`** (`packages/coding-agent/src/session/agent-session.ts`) is the coding-agent's higher-level session manager that:
- Wraps `Agent` with compaction, bash execution, session persistence, model switching
- Manages advisor runtime, async jobs, hindsight/mnemopi memory
- Handles auto-continue, auto-thinking, plan mode, goal mode

---

### 2. Tool System

**Tool definition** (`packages/agent/src/types.ts`): `AgentTool<TParameters, TDetails, TTheme>` extends the base `Tool<TParameters>` with:

```typescript
interface AgentTool<TParameters, TDetails, TTheme> extends Tool<TParameters> {
  label: string;              // Human-readable UI label
  hidden?: boolean;           // Excluded unless explicitly listed
  deferrable?: boolean;       // Stages pending action needing resolve
  loadMode?: "essential" | "discoverable";  // When to load
  summary?: string;           // One-line for tool discovery
  concurrency?: "shared" | "exclusive" | ((args) => "shared" | "exclusive");
  lenientArgValidation?: boolean;
  interruptible?: boolean;    // May abort for steering mid-execution
  intent?: "omit" | "optional" | "require" | ((args) => string);
  matcherDigest?: (args) => string | undefined;
  approval?: ToolApproval;    // Tier declaration for approval gates
  execute: AgentToolExecFn;   // (id, params, signal, onUpdate, context) => Promise<AgentToolResult>
  renderCall?: (...) => unknown;
  renderResult?: (...) => unknown;
}
```

**Tool execution flow** (in `executeToolCalls`):
1. Map tool calls from assistant message to tool definitions (match by name or customWireName)
2. For each tool call, resolve concurrency mode (shared/exclusive)
3. Build dependency graph: exclusive tools wait for all prior tasks; shared tools wait only for last exclusive
4. Execute via `Promise.allSettled(tasks)` -- shared tools run concurrently
5. For each tool:
   - Validate arguments via `validateToolArguments`
   - Run `beforeToolCall` hook (can block execution)
   - Execute `tool.execute(id, args, signal, onUpdate, context)`
   - Coerce result via `coerceToolResult` (defensive against malformed results)
   - Run `afterToolCall` hook (can override result fields)
6. Check steering after each tool (if `interruptMode="immediate"`)
7. Emit tool_execution_start/update/end events

**Built-in tools** (32 total, defined in `packages/coding-agent/src/tools/`):

| Category | Tools |
|---|---|
| File ops | `read`, `write` (EditTool), `ast-edit`, `ast-grep` |
| Search | `search`, `find`, `search-tool-bm25` |
| Execution | `bash`, `eval` (Python/JS/Ruby/Julia kernels), `debug` (DAP) |
| LSP | `lsp` (14 operations: definition, references, hover, rename, code_actions...) |
| Memory | `memory-retain`, `memory-recall`, `memory-reflect`, `memory-edit` |
| Subagents | `task` (fan-out), `job` (poll/cancel async results) |
| GitHub | `github` (unified: issues, PRs, search) |
| Browser | `browser` (puppeteer-based) |
| Misc | `ask`, `checkpoint`, `rewind`, `resolve`, `todo`, `yield`, `learn`, `manage-skill`, `inspect-image`, `image-gen`, `irc` (inter-agent), `ssh` |

**Tool prompt engineering**: Each tool has a dedicated `.md` prompt file under `packages/coding-agent/src/prompts/tools/`. These follow a structured pattern:
- One-line purpose
- Parameter grammar
- Worked examples
- Failure shapes the agent owns
- Anti-patterns
- `<critical>` recap

**Tool discovery**: Tools have `loadMode` of "essential" (always loaded) or "discoverable" (activated by `search_tool_bm25`). A `DiscoverableToolSearchIndex` allows the agent to find tools at runtime.

**Custom tools**: Extensions can add tools via `packages/coding-agent/src/extensibility/custom-tools/` which wraps them as `AgentTool` instances.

**MCP tools**: `packages/coding-agent/src/mcp/tool-bridge.ts` bridges MCP servers as `AgentTool` instances.

---

### 3. Context / Memory Management

**Append-only context** (`packages/agent/src/append-only-context.ts`):
- `StablePrefix`: freezes system prompt + tool spec bytes once; reused across turns until `invalidate()` is called. Maximizes provider prefix cache hits (Anthropic/DeepSeek).
- `AppendOnlyLog`: messages only grow; prior turns are never re-serialized. Combined with stable prefix, only new message delta is a cache miss.
- `AppendOnlyContextManager`: orchestrates both. `build()` produces a `Context` with stable prefix + append-only messages. `syncMessages()` after `convertToLlm` keeps the log in sync.

**Compaction** (`packages/agent/src/compaction/`):
- Strategies: `context-full` (default), `handoff`, `shake`, `snapcompact`, `off`
- `context-full`: summarizes old messages into a compaction summary, keeps recent tokens (default 20k)
- `handoff`: generates a structured handoff document (Goal/Progress/Key Decisions/Next Steps/Critical Context)
- `shake`: removes superseded tool results and low-value content
- `snapcompact`: frame-based compaction via `@oh-my-pi/snapcompact`
- Threshold: percentage of context window or absolute token count
- Compaction tracks file operations (read/modified) across sessions via `CompactionDetails`
- Branch summaries preserve context across git branch switches

**Memory backends** (`packages/coding-agent/src/memory-backend/`):
- `hindsight`: session-scoped, batches retains with debounce timer
- `mnemopi`: entity extraction, scoped memory banks, importance scoring
- `off`: disabled
- Tools: `retain` (store), `recall` (search), `reflect` (summarize), `memory-edit` (modify)
- Memory is project-scoped by default

**Context transforms**:
- `transformContext`: operates on AgentMessage[] before convertToLlm (pruning, injection)
- `convertToLlm`: AgentMessage[] -> Message[] (filter LLM-compatible, convert attachments)
- `transformProviderContext`: operates on final provider Context after conversion
- Pruning: `pruneSupersededToolResults`, `pruneToolOutputs` remove low-value tool output

---

### 4. Planning and Reasoning

**Plan mode** (`packages/coding-agent/src/plan-mode/`):
- Activated via `resolve` tool with `action: "apply"`
- Agent becomes READ-ONLY; only writes to `local://<slug>-plan.md`
- Plan is an "execution spec, not a design doc" -- every choice is already made
- Must be self-contained: "a competent implementer who never saw this conversation executes the file top to bottom and makes ZERO design decisions"
- Plan structure: Context, Approach (ordered steps), Critical files & anchors, Verification, Assumptions & contingencies
- On approval: user picks execution mode (fresh context, compacted, or keep context)

**Thinking levels** (`packages/agent/src/thinking.ts`):
- `off`, `inherit`, `minimal`, `low`, `medium`, `high`, `xhigh`
- Mapped to `Effort` enum from `pi-ai`
- Auto-thinking: `classifyDifficulty()` in `packages/coding-agent/src/auto-thinking/classifier.ts` dynamically selects thinking level based on task difficulty

**Orchestration** (`packages/coding-agent/src/prompts/system/orchestrate-notice.md`):
- Injected as `<system-notice>` for orchestration requests
- Defines orchestrator role: decompose, dispatch, verify, iterate
- Rules: never yield until everything is closed; enumerate full surface before dispatching; parallelize maximally; each task assignment is self-contained; verify after every phase; respawn incomplete subagents rather than fixing inline
- Anti-patterns: doing parallelizable work yourself, wrapping trivial edits in task, yielding mid-phase, serializing what could be parallel

**Subagent system** (`packages/coding-agent/src/task/`):
- `task` tool spawns subagents with isolated worktrees
- Subagents get their own tool surface, system prompt, and conversation
- Support for batch spawning (multiple tasks in one call)
- IRC coordination between live agents
- Output schema validation: subagent results match a TypeScript interface
- Isolation mode: returns patches from isolated environments

---

### 5. Prompt Engineering Patterns

**System prompt** (`packages/coding-agent/src/prompts/system/system-prompt.md`):
- Handlebars-templated with conditional sections
- Sections: ROLE, RUNTIME (skills, rules, internal URLs, tool inventory), TOOL POLICY, EXECUTION WORKFLOW, DELIVERY CONTRACT
- RFC 2119 language: MUST/SHOULD/NEVER/AVOID for precise instruction
- Tool policy enforces specialized tools over shell equivalents (read over cat, search over grep, etc.)
- Exploration rules: "You NEVER open a file hoping. Hope is not a strategy."
- Delivery contract: inviolable rules about completeness, evidence, no fabrication, no scope shrink

**Key prompt patterns:**
1. **XML-tagged system directives**: `<system-conventions>`, `<system-notice>`, `<system-interrupt>` -- model treats these as authoritative, even inside user messages
2. **Conditional templating**: Handlebars `{{#if}}`, `{{#has tools "..."}}`, `{{#each}}` for dynamic prompt assembly
3. **Internal URL schemes**: `skill://`, `rule://`, `memory://`, `agent://`, `artifact://`, `history://`, `local://`, `issue://`, `pr://` -- unified resource access
4. **Tool prompt anatomy**: one-line purpose, input grammar, worked examples, failure shapes, anti-patterns, `<critical>` recap
5. **TTSR (Time-Traveling Stream Rules)**: regex-matched rules that abort the stream mid-token when violated, inject the rule as a system reminder, and retry from the same point
6. **Advisor prompt**: separate model watching every turn, injecting notes with severity levels (nit/concern/blocker)
7. **Subagent prompt**: self-contained assignment with `local://` shared context, no shared conversation history

**Prompt storage convention** (from AGENTS.md):
- Prompts live in static `.md` files, never built in code
- Dynamic content via Handlebars
- Import via `import content from "./prompt.md" with { type: "text" }`

---

### 6. Multi-Step Reasoning

**Loop-level mechanisms:**
- **Inner loop**: processes tool calls and steering messages until no more tool calls and no pending messages
- **Outer loop**: continues when follow-up or steering messages arrive after agent would stop
- **Pause-turn continuation**: when model ends with `pause_turn` (non-terminal stop), re-samples with assistant message replayed (up to 8 continuations)
- **Soft tool requirement lifecycle**: remind -> escalate to forced toolChoice -> give up after 3 escalations
- **Auto-continue**: compaction summary includes auto-continue prompt; new context resumes the task
- **Deadline enforcement**: absolute wall-clock deadline with timer-based abort

**Compaction for long sessions:**
- When context exceeds threshold, compaction summarizes old messages
- Handoff strategy generates structured handoff documents preserving exact technical state
- Snapcompact uses frame-based compression
- Post-compaction, auto-continue prompt re-engages the model with the summarized context

**Multi-agent coordination:**
- **Task tool**: spawns subagents (synchronous or async with job polling)
- **IRC tool**: real-time inter-agent messaging for coordination
- **Advisor runtime**: second model reads every primary turn, injects inline advice
- **Batch task spawning**: multiple tasks in one call, each with its own role and assignment
- **Isolated worktrees**: subagents work in git worktrees to avoid conflicts

---

### 7. Multi-Provider / Dialect System

**Dialect system** (`packages/ai/src/dialect/`):
- 12 dialects: `glm`, `hermes`, `kimi`, `xml`, `anthropic`, `deepseek`, `minimax`, `harmony`, `pi`, `qwen3`, `gemini`, `gemma`
- Each dialect defines: prompt template, in-band scanner (parses tool calls from text), tool call/result rendering, thinking markers
- **Owned (in-band) tool calling**: when a dialect is active, no native `tools` are sent; instead the catalog is rendered as text in the system prompt, and tool calls/results are encoded as text in messages
- The `wrapInbandToolStream` re-materializes text-based tool calls as native `toolCall` content blocks
- Fabricated tool result detection: when the model starts generating tool results in text, the provider abort is triggered

**Provider support** (40+):
- Anthropic, OpenAI (Responses + Chat), Google (Gemini + Vertex), Azure, AWS Bedrock
- DeepSeek, Kimi, MiniMax, Qwen, GLM, Fireworks, Together, OpenRouter
- Ollama, Cursor, Devin, GitHub Copilot, GitLab Duo
- Custom/synthetic providers

---

### 8. Rust Core

Three Rust crates provide performance-critical operations:
- **pi-natives**: text sanitization, image processing, grep (ripgrep-linked), font rendering, clipboard, crash handler
- **pi-shell**: shell output minimizer (tool-specific output filters for 50+ commands), bash integration via brush shell, process management
- **pi-iso**: filesystem isolation using OS-native copy-on-write (APFS, btrfs, ZFS, overlayfs, Windows Block Clone) for fast worktree creation
- **pi-ast**: AST operations via tree-sitter

---

### 9. Skills and Commands

**Skills** (`.omp/skills/`):
- Self-contained knowledge modules with `SKILL.md` frontmatter (name, description)
- Can include scripts (e.g., `tool-prompt-optimization/scripts/probe.ts`)
- Agent reads `skill://<name>` to load instructions before proceeding

**Commands** (`.omp/commands/`):
- Slash-command workflows defined in `.md` files
- Each command specifies arguments, step-by-step procedure, and rules
- Examples: `fix-issues.md` (parallel issue fixing with worktrees), `review-prs.md`, `release.md`, `triage.md`

---

### 10. Key Abstractions Summary

| Abstraction | Location | Purpose |
|---|---|---|
| `AgentTool` | `packages/agent/src/types.ts` | Tool definition with execute, approval, concurrency, intent |
| `AgentMessage` | `packages/agent/src/types.ts` | Union of LLM messages + custom message types (extensible via declaration merging) |
| `AgentContext` | `packages/agent/src/types.ts` | systemPrompt + messages + tools |
| `AgentLoopConfig` | `packages/agent/src/types.ts` | Full loop configuration (model, transforms, hooks, steering, tool choice, telemetry) |
| `AgentEvent` | `packages/agent/src/types.ts` | Lifecycle event types for UI/telemetry |
| `EventStream` | `@oh-my-pi/pi-ai` | Async event stream with terminal event and result extraction |
| `Dialect` | `packages/ai/src/dialect/` | In-band tool calling protocol for specific LLM providers |
| `StablePrefix` | `packages/agent/src/append-only-context.ts` | Frozen system prompt + tool spec for cache hit maximization |
| `AppendOnlyLog` | `packages/agent/src/append-only-context.ts` | Append-only message log for stable byte prefixes |
| `CompactionResult` | `packages/agent/src/compaction/` | Summary + file operation tracking after context compaction |
| `SoftToolRequirement` | `packages/agent/src/types.ts` | Remind-then-escalate tool requirement without cache invalidation |
| `AgentSession` | `packages/coding-agent/src/session/` | High-level session manager wrapping Agent + compaction + persistence |
| `AdvisorRuntime` | `packages/coding-agent/src/advisor/` | Second model watching primary agent turns |

---

### Files Found

| File Path | Description |
|---|---|
| `packages/agent/src/agent-loop.ts` | Core agent loop (2089 lines): streaming, tool execution, steering, Harmony mitigation |
| `packages/agent/src/agent.ts` | Agent class wrapping the loop with stateful session |
| `packages/agent/src/types.ts` | Core type definitions: AgentTool, AgentMessage, AgentContext, AgentLoopConfig, AgentEvent |
| `packages/agent/src/append-only-context.ts` | Stable prefix + append-only log for cache optimization |
| `packages/agent/src/thinking.ts` | Thinking level enum (off/inherit/minimal-low-med-high-xhigh) |
| `packages/agent/src/compaction/compaction.ts` | Context compaction logic (5 strategies) |
| `packages/agent/src/compaction/prompts/handoff-document.md` | Handoff document prompt template |
| `packages/agent/src/compaction/prompts/compaction-summary.md` | Compaction summary prompt template |
| `packages/ai/src/dialect/factory.ts` | Dialect factory (12 dialects) |
| `packages/ai/src/dialect/types.ts` | Dialect type definitions (InbandScanner, DialectDefinition) |
| `packages/coding-agent/src/session/agent-session.ts` | High-level session manager |
| `packages/coding-agent/src/tools/index.ts` | Tool registry and creation |
| `packages/coding-agent/src/memory-backend/runtime.ts` | Memory runtime (search/save/status) |
| `packages/coding-agent/src/tools/memory-retain.ts` | Retain tool implementation |
| `packages/coding-agent/src/advisor/runtime.ts` | Advisor runtime (second model watching turns) |
| `packages/coding-agent/src/prompts/system/system-prompt.md` | Main system prompt (Handlebars-templated) |
| `packages/coding-agent/src/prompts/system/plan-mode-active.md` | Plan mode system prompt |
| `packages/coding-agent/src/prompts/system/orchestrate-notice.md` | Orchestration system notice |
| `packages/coding-agent/src/prompts/system/ttsr-interrupt.md` | TTSR rule violation interrupt template |
| `packages/coding-agent/src/prompts/system/subagent-system-prompt.md` | Subagent system prompt template |
| `packages/coding-agent/src/prompts/system/auto-continue.md` | Auto-continue after compaction |
| `packages/coding-agent/src/prompts/advisor/system.md` | Advisor system prompt |
| `packages/coding-agent/src/prompts/agents/task.md` | Task agent (worker) prompt |
| `packages/coding-agent/src/prompts/tools/task.md` | Task tool description (Handlebars-templated) |
| `packages/coding-agent/src/prompts/tools/read.md` | Read tool description |
| `packages/coding-agent/src/prompts/tools/retain.md` | Retain tool description |
| `.omp/skills/tool-prompt-optimization/SKILL.md` | Skill definition example with probe scripts |
| `.omp/commands/fix-issues.md` | Command workflow example (parallel issue fixing) |
| `AGENTS.md` | Development rules and package structure |
| `README.md` | Project overview and feature list |

---

## Caveats / Not Found

- The repository is very large (~2000+ files); I focused on the core agent loop, tool system, prompt engineering, and memory/compaction subsystems. Many supporting systems (TUI rendering, DAP debugging, browser automation, specific tool implementations, CI/CD) were not deeply examined.
- The `packages/coding-agent/src/session/agent-session.ts` is very large and was only partially read; it contains the full integration of all subsystems.
- The `packages/ai/` provider implementations (40+ providers) were not individually examined; only the dialect system and streaming abstraction were studied.
- The Rust crates (`pi-natives`, `pi-shell`, `pi-iso`, `pi-ast`) were cataloged but not deeply analyzed for internal implementation.
- The specific `snapcompact` frame-based compaction algorithm was not examined in detail (it's a separate package `@oh-my-pi/snapcompact`).
- The `hindsight` and `mnemopi` memory backend implementations were not deeply examined; only the tool surface and runtime interface were studied.
