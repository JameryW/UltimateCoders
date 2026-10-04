# Research: Xiaomi MiMo Code non-interactive integration contract

- **Query**: Research official Xiaomi MiMo Code product/CLI integration contract for use as a non-interactive coding agent: official executable, command line or SDK entrypoint, output mode, workdir, tool/permission controls, auth/config and installation docs. Confirm whether it is distinct from MiMo API/model.
- **Scope**: external (official Xiaomi MiMo docs, official XiaomiMiMo GitHub repository/source, and its npm packages)
- **Date**: 2026-09-29

## Findings

### Product boundary

MiMo Code is a terminal-native coding-agent product/harness, published separately from the MiMo foundation models/API. The official repository describes the agent as an OpenCode fork and says it supports mainstream LLM provider APIs; it also notes MiMo models can be used in other agents such as Cursor, Cline, and Zed. Thus `mimo` is the agent executable, while MiMo models/API are provider/model choices the agent may use. The MiMo Code CLI can also use non-Xiaomi providers.

### Official executable and install

The supported command is `mimo`, installed as the `@mimo-ai/cli` npm package or using Xiaomi's install scripts. The official quick start gives:

```sh
# macOS / Linux
curl -fsSL https://mimo.xiaomi.com/install | bash

# Windows PowerShell
powershell -ep Bypass -c "irm https://mimo.xiaomi.com/install.ps1 | iex"

# all platforms
npm install -g @mimo-ai/cli
```

The interactive setup supports Xiaomi MiMo Platform OAuth, Codex OAuth, migrating Claude Code credentials, catalog providers, and custom OpenAI-compatible endpoints.

### Non-interactive entrypoints and output

1. **One-shot CLI:** `mimo run [message..]`. `--format json` selects raw JSON events, one JSON object per line (JSONL). The official repository's built-in `drive-mimo` skill documents events such as `step_start`, `text`, `reasoning`, `tool_use`, `step_finish`, and `error`; fields include `type`, `timestamp`, `sessionID`, and typically nested `part`. It says the process exit code is the reliable completion signal, not a terminal event. The current source emits JSONL to stdout; logs can be requested on stderr with global `--print-logs`.

   The CLI also accepts prompts piped on stdin when stdin is not a TTY: its source appends `Bun.stdin.text()` to the message. A non-interactive invocation may therefore pass the prompt as an argument or stdin.

2. **Native ACP server:** `mimo acp` starts an Agent Client Protocol server over stdin/stdout using NDJSON transport. `--cwd` selects the working directory (defaults to current directory) in the official CLI reference/source. The ACP adapter advertises prompt images, load/resume/fork/list session capabilities and passes tool-call / permission updates through ACP. This is a persistent protocol server, rather than a one-shot process call.

3. **TypeScript SDK:** Xiaomi publishes `@mimo-ai/sdk`, described in the package manifest as a TypeScript SDK for the MiMoCode API, with package exports for client/server and v2 client/server. It is the MiMoCode local/server API client surface, not the MiMo inference/model API. The package root source's convenience API is `createOpencode({ ... })`, returning `{ client, server }`. Its current source retains OpenCode naming and its server helper launches `opencode serve`; treat this as an API for embedding/controlling the server and verify the installed binary/package pairing before choosing it as a CLI-run wrapper. Official docs do not present a distinct `mimo run` SDK function.

### Working directory

- `mimo run --dir PATH ...` sets the process working directory for a local run; source calls `process.chdir(args.dir)`. If attaching to a remote server, `--dir` is the path on that remote server.
- Official CLI docs show `--dir` for `attach` but omit it from the `run` flags table. The official repository's command source and built-in `drive-mimo` skill both explicitly use/document `mimo run --dir`, so current source is the more complete contract for the headless CLI.
- `mimo acp --cwd PATH` is the ACP server's working-directory option. Starting in a project directory is also the documented interactive workflow.

### Model selection, auth, and configuration

- CLI `run` accepts `--model provider/model`, `--agent`, `--variant`, session continuation/fork flags, and file attachments (`--file` / `-f`). Its main agent modes documented in the repo are `build` (default), `plan` (read-only), and `compose`.
- `mimo auth login` configures provider credentials; official docs say credentials are stored in `~/.local/share/mimocode/auth.json` (Windows XDG paths are under `%LOCALAPPDATA%\mimocode\`). The home root can be overridden with `MIMOCODE_HOME`. Credentials may also be provided by environment variables or project `.env` files.
- For a MiMo Platform API-key configuration, the official model docs give provider `mimo`, base URL `https://api.xiaomimimo.com/v1`, `api-key` header sourced from `{env:MIMO_API_KEY}`, model IDs under `models`, and top-level `model: "mimo/<model-id>"`. The config lives globally under `~/.config/mimocode/` or at project scope under `.mimocode/`.
- MiMoCode can also log in with Xiaomi-hosted OAuth/Token Plan flows; this is a separate auth path from configuring a direct MiMo API key. There is no need to use MiMo as the model provider if another supported provider is configured.

### Tool and permission controls

- The default `build` agent has full development tools; `plan` is read-only; `compose` orchestrates specs-driven work. CLI `--agent` selects the agent.
- Permission config resolves actions to `allow`, `ask`, or `deny`. It supports catch-all and per-tool/pattern rules, including `bash`, `read`, `edit`, `glob`, `grep`, `task`, and `skill`; `external_directory` gates paths outside the selected working directory. Rules are ordered with the last matching rule winning.
- `mimo run --dangerously-skip-permissions` (alias `--yolo`) auto-approves permissions that were not explicitly denied. The documented `MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS=1` environment setting is another way to enable it. Explicit deny rules still win; this mode intentionally removes interactive permission confirmation and should only be used in trusted disposable/sandboxed environments.
- For automation, configure granular rules rather than assume `--dangerously-skip-permissions` is required. The CLI source also installs default deny rules for `question` and `plan_exit` in its one-shot run path.

### External references

- [Official MiMo Code repository](https://github.com/XiaomiMiMo/MiMo-Code) — product identity, install commands, supported auth/provider paths, agent modes, config, and dangerous-permission behavior.
- [Official MiMo Code CLI options](https://mimo.xiaomi.com/mimocode/cli-options) — `run`, `acp`, model/agent/output flags and CLI argument reference.
- [Official MiMo Code installation docs](https://mimo.xiaomi.com/mimocode/install) — install scripts and prerequisites.
- [Official MiMo Code permissions docs](https://mimo.xiaomi.com/mimocode/permissions) — permission actions, pattern rules, and external-directory policy.
- [Official MiMo Code model docs](https://mimo.xiaomi.com/mimocode/models-provider) — MiMo provider/API-key configuration and multiple provider support.
- [Official MiMo Code auth subcommands](https://mimo.xiaomi.com/mimocode/cli-subcommands) — `mimo auth login` and credential storage.
- [Official repo `run` command source](https://github.com/XiaomiMiMo/MiMo-Code/blob/main/packages/opencode/src/cli/cmd/run.ts) — exact headless flags, JSON emission, stdin prompt support, cwd change, and permission defaults.
- [Official repo ACP command source](https://github.com/XiaomiMiMo/MiMo-Code/blob/main/packages/opencode/src/cli/cmd/acp.ts) — ACP server's NDJSON stdin/stdout transport and `--cwd` option.
- [Official repo `drive-mimo` skill](https://github.com/XiaomiMiMo/MiMo-Code/blob/main/.mimocode/skills/drive-mimo/SKILL.md) — operational headless JSONL event contract and completion semantics.
- [Official `@mimo-ai/sdk` package](https://www.npmjs.com/package/@mimo-ai/sdk) — distinct TypeScript SDK for the MiMoCode API.
- [Official SDK package manifest](https://raw.githubusercontent.com/XiaomiMiMo/MiMo-Code/main/packages/sdk/js/package.json) and [server helper source](https://raw.githubusercontent.com/XiaomiMiMo/MiMo-Code/main/packages/sdk/js/src/server.ts) — package exports and current server-launch API.

## Caveats / Not Found

- The current official CLI options page lists --dir for mimo run; older page snapshots omitted it. Confirm against the pinned binary if integrating an older release.
- `--format json` is JSONL events, not a single JSON document. Build consumers should parse one JSON object per line; stdout may be protocol/data, while diagnostics can be routed to stderr.
- The source SDK API retains OpenCode names and its server helper launches `opencode serve`; current docs do not establish that `@mimo-ai/sdk` is a clean direct SDK for launching the renamed `mimo` binary. CLI `mimo run` and ACP `mimo acp` are the documented named entrypoints.
- MiMo Code docs and source identify ACP as Agent Client Protocol, and source confirms NDJSON over stdio; no standalone SDK for invoking a single coding task was found.
- Official repository source can evolve on `main`; the above CLI contract is based on the current documented/reference behavior seen on 2026-09-29, not a pinned version guarantee.

## Follow-up: Explicit MiMo V2.6 Flash and model failover

### Explicit model selection

Yes. `mimo run` accepts `--model provider/model` (short form `-m`), and MiMo Code's config has top-level `"model"` in the same `provider_id/model_id` form. The official MiMo Code repository's CLI report uses `xiaomi/mimo-v2.6-flash` as the selected model for a `mimo run` session, so the concrete CLI form is:

```sh
mimo run --model xiaomi/mimo-v2.6-flash --dir "$WORKSPACE" --format json "...prompt..."
```

This requires that the worker's MiMo Code installation has that provider/model registered and credentials configured. Xiaomi's docs support `mimo models [provider]` for checking the installed catalog; the `xiaomi` provider prefix is the observed official-repo CLI identifier for the V2.6 Flash run. A persistent default can be set using top-level `"model": "xiaomi/mimo-v2.6-flash"` in global or project MiMo Code JSONC. The explicit run flag overrides the configured/default model for that invocation.

### Fallback to a DeepSeek model on provider/model failure

No native cross-model failover setting or `fallback_models` chain is documented in the current MiMo Code CLI/config. The built-in runtime does have provider retry coordination, but the processor resolves retry policy from the selected provider ID and retries the same `streamInput` (which retains the same selected model); it does not select a second provider/model. Config `small_model` is documented in source as a model for lightweight tasks such as title generation, not as a failure fallback.

I found no official MiMo Code source/docs specifying a concrete model ID called “DeepSeek Flash.” The CLI supports selecting a registered DeepSeek model via `--model deepseek/<model-id>`, but that only selects it for the run; no native primary-to-DeepSeek failover chain is established by these docs/source.

### Follow-up references

- [MiMo Code CLI options](https://mimo.xiaomi.com/mimocode/cli-options) — documents `mimo run --model` (`provider/model`) and model listing.
- [MiMo Code `run` source](https://github.com/XiaomiMiMo/MiMo-Code/blob/main/packages/opencode/src/cli/cmd/run.ts) — defines `--model`, and routes the selected argument into the run invocation.
- [Official MiMo Code issue #2482](https://github.com/XiaomiMiMo/MiMo-Code/issues/2482) — records a MiMo V2.6 Flash CLI run using `xiaomi/mimo-v2.6-flash`.
- [MiMo Code config source](https://github.com/XiaomiMiMo/MiMo-Code/blob/main/packages/opencode/src/config/config.ts) — defines `model` and states `small_model` is for lightweight jobs such as title generation.
- [MiMo Code session processor source](https://github.com/XiaomiMiMo/MiMo-Code/blob/main/packages/opencode/src/session/processor.ts) — obtains retry policy by the chosen provider ID and retries the stream with the same `streamInput.model`; this is request retry, not provider/model failover.
- [MiMo Code v0.1.15 release](https://github.com/XiaomiMiMo/MiMo-Code/releases/tag/v0.1.15) — current release context at the research date; announced MiMo-V2.6 and the corresponding MiMo Code agent update.

### Caveat

The exact available `provider/model` string depends on the provider/auth route installed on the worker. Validate it on that worker with `mimo models xiaomi`; do not rely on an implicit default if every subprocess must use V2.6 Flash.

## Worker integration recommendation: direct MiMo API key, V2.6 Flash

For a noninteractive Worker that should use the direct MiMo Platform API key without a prior mimo auth login, define a custom provider mimo in a job-scoped MiMo Code profile and pass the key only in the child process environment. The exact provider/model selector is mimo/mimo-v2.6-flash: the model ID is mimo-v2.6-flash, and the provider docs explain that a custom provider entry named mimo uses the mimo/ prefix. The earlier xiaomi/mimo-v2.6-flash example in this note refers to the separate built-in Xiaomi provider route (typically OAuth/connect), not this direct custom-provider API-key setup.

Example <MIMOCODE_HOME>/config/mimocode.jsonc:

    {
      "$schema": "https://mimo.xiaomi.com/mimocode/config.json",
      "model": "mimo/mimo-v2.6-flash",
      "provider": {
        "mimo": {
          "name": "MiMo API",
          "npm": "@ai-sdk/openai-compatible",
          "options": {
            "baseURL": "https://api.xiaomimimo.com/v1",
            "headers": { "api-key": "{env:MIMO_API_KEY}" }
          },
          "models": {
            "mimo-v2.6-flash": { "name": "MiMo V2.6 Flash" }
          }
        }
      }
    }

Use a unique absolute MIMOCODE_HOME per concurrently running Worker task (for example <worker-state>/mimocode/<subtask-id>), create its config/ directory and put this file there. This isolates config, data/session DB, cache, and state. Set MIMOCODE_DISABLE_PROJECT_CONFIG=1 to prevent a checkout's .mimocode/mimocode.json[c] from merging over the controlled profile. Set MIMOCODE_MIMO_ONLY=0 explicitly: official env-var docs say pure-MiMo mode suppresses provider environment-key fallback; the current docs call it default-on, while the current source flag parser appears to use truthy-only semantics, so pinning it off removes that default ambiguity. Inject MIMO_API_KEY from the Worker secret store into the subprocess environment; do not write the secret into the config or command arguments. This explicit custom-provider config does not require OAuth login or an auth.json credential.

Run with an argv list (no shell interpolation), send the prompt on stdin, and parse one JSON event per stdout line:

    mimo run --dir <ABSOLUTE_WORKTREE> --model mimo/mimo-v2.6-flash --format json --dangerously-skip-permissions

Use --dangerously-skip-permissions only inside the existing restricted Worker sandbox; otherwise configure explicit noninteractive permission rules. The model flag pins every invocation even if a config default or prior session differs. The MiMo model page gives the direct API endpoint/model ID, and MiMo Code's provider docs supply the compatible-provider config/header contract.

For this repository's Worker, set <ABSOLUTE_WORKTREE> from workspace_handle.worktree_path (the same working_dir passed into SandboxManager.execute in python/ultimate_coders/agent/worker.py:1261-1264,1302-1306). If that handle is absent, pass the sandbox's designated checkout root; do not use the Worker process's unrelated default cwd. The Worker currently wraps sandbox execution with subtask.timeout_seconds or 600 seconds (worker.py:933-939; Subtask.timeout_seconds == 0 means default at types.py:354, and orchestrator config default is 600 seconds at types.py:702). MiMo CLI has no run-level timeout flag, so apply that existing deadline to the subprocess and terminate its process/process group when cancellation or timeout occurs. A task needing longer than ten minutes should set its per-subtask timeout rather than quietly letting the model child outlive the Worker deadline.

### References for the Worker recommendation

- [MiMo Code Models & Providers](https://mimo.xiaomi.com/mimocode/models-provider) — custom provider key mimo, API-compatible base URL/header, and the distinction between custom mimo and built-in xiaomi IDs.
- [MiMo V2.6 Flash model page](https://mimo.mi.com/models/en-US/mimo-v2.6-flash) — direct API sample with MIMO_API_KEY, base URL, and model ID mimo-v2.6-flash.
- [MiMo Code Environment Variables](https://mimo.xiaomi.com/mimocode/env-vars) — absolute per-profile MIMOCODE_HOME, MIMOCODE_DISABLE_PROJECT_CONFIG, and provider-env behavior of MIMOCODE_MIMO_ONLY.
- [MiMo Code Config Overrides](https://mimo.xiaomi.com/mimocode/config-overrides) — profile paths, config merge/precedence, and explicit config injection options.
- [MiMo Code Config Files](https://mimo.xiaomi.com/mimocode/config-files) — JSON/JSONC provider/model schema and {env:VAR} substitution.
- [MiMo Code CLI Options](https://mimo.xiaomi.com/mimocode/cli-options) — mimo run, --model, --format json, --dir, and permission skip flag.