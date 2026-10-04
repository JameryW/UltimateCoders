# Research: Official OpenCode CLI integration contract

- **Query**: Research official OpenCode CLI execution for a UC_CODING_AGENT adapter: non-interactive command/options, output modes, working directory, tool/permission controls, config/provider authentication, and repository integration fit.
- **Scope**: mixed
- **Date**: 2026-09-29

## Findings

### Files Found

| File Path | Description |
|---|---|
| python/ultimate_coders/agent/registry.py:69 | Adapter plugin spec and built-in registrations; carries CLI probe and optional API-key env mapping. |
| python/ultimate_coders/agent/sandbox.py:912 | Adapter interface: name, request builder, and output parser. |
| python/ultimate_coders/agent/sandbox.py:1539 | Claude CLI JSONL request/parser precedent. |
| python/ultimate_coders/agent/sandbox.py:1740 | Codex CLI request/config precedent. |
| python/ultimate_coders/nats_worker.py:1477 | Reads UC_CODING_AGENT and passes selection into sandbox configuration. |
| .trellis/spec/backend/agent-capability-spec.md:260 | Existing contract for subtask-level tool restrictions, MCP, prompts, custom agents, and capability advertising. |

### Code Patterns

- UC resolves adapters through AgentPluginSpec. A spec has canonical name, aliases, factory, optional api_key_env, cli_probe, and description; built-ins register in registry.py:191-252. External plugins can register through Python entry points or UC_AGENT_PLUGINS (registry.py:22-27, 252-367).
- build_request returns a subprocess request with command, argv, timeout_secs, working_dir, env_vars, and optional temporary files. parse_output receives stdout, stderr, exit code, and timeout information via ExecResult, then returns AgentOutput (sandbox.py:330-373, 560-615, 912-928).
- UC’s generic agent config includes tools, allowed_tools, disallowed_tools, MCP config, and agent name/prompt fields. Claude maps some of these to CLI flags; the Codex adapter warns that global allow/deny lists are unsupported there (sandbox.py:1549-1586, 1740-1860; agent-capability-spec.md:260-314, 361-393). OpenCode’s permission model is config-based and needs separate mapping.
- Registry cli_probe is the executable name used for CLI capability probing. Upstream OpenCode’s executable is opencode (registry.py:73-93; Codex registration example at 236-239).

### OpenCode CLI Contract (current V2 docs)

**Install/package.** The current V2 install page documents the npm package @opencode/cli, which installs the native opencode binary via postinstall. On Windows, OpenCode says package managers are unsupported and provides standalone binaries. The documented npm install is npm install -g @opencode/cli. Treat V2 as the current documented CLI interface. The V2 migration page states that OpenCode 1 and OpenCode 2 both use the opencode command; no beta-only qualification appears on the current V2 intro or migration page.

**Headless command.** V2 documents opencode run as a non-interactive prompt command. A worker-shaped call is:

    opencode run --standalone --model xiaomi/mimo-v2.6-flash --format json "Implement the requested change"

--standalone creates a private server for the run; V2 CI docs specifically recommend it so the private server receives provider credentials from the job environment. --model provider/model chooses a model for that run. --format json emits newline-delimited JSON for scripts. V2 docs also show --agent, --file, and --continue; the command's own --help is documented as the full flag list. Do not pass V1-only flags based on the old CLI reference: current V2 pages do not document --dir or --auto.

**Working directory.** Launch the subprocess with its cwd set to the task workspace. V2 describes running the CLI in a project; project configuration is discovered from the current directory toward the filesystem root and precedence follows the nearest project config. The top-level CLI can be given a project path for interactive use, but the V2 run command examples do not document a --dir switch. The documented separate project/session option --directory applies to opencode session import, not run. Therefore use the OS process cwd for the worker workspace and verify with opencode models or opencode debug config in that cwd.

**Output contract.** V2 CLI docs explicitly promise newline-delimited JSON with --format json, but do not publish a stable event schema. Parse complete JSON lines defensively, preserve unknown event types for diagnostics, and use process exit status / timeout as the authoritative execution result. The event fields and names previously listed in this file came from the V1 development implementation and are not a V2 contract.

**Tool and permission controls.** V2 uses a permissions array of ordered rules with {action, resource, effect}; effects are allow, ask, and deny, and the last matching rule wins. Unmatched permissions resolve to ask. Current built-in actions include read, edit, shell, subagent, skill, webfetch, websearch, external_directory, MCP tool actions, and execute. Explicit deny remains enforced. The V2 permissions guide says non-interactive clients must decide how to handle approval requests. No V2 --auto, --allowed-tools, or --disallowed-tools run flag was found in the current command docs; configure explicit task-specific allow/deny rules in project or agent configuration and avoid an unattended rule set that leaves required actions at ask. V2 uses shell/subagent; V1 bash/task names are obsolete in V2.

**Config and provider auth.** V2 supports project opencode.json / opencode.jsonc or .opencode/opencode.json(c), plus global ~/.config/opencode/opencode.json(c); config sources merge, and the V2 docs specify that project files override matching global settings. The root model is a persistent default for new work, while --model overrides it for one run. Provider credentials can be connected through opencode auth login <provider> --method api-key, listed with opencode auth list, or supplied through provider environment variables. The official CI example passes a provider key in the subprocess environment and uses --standalone. V2 custom providers expose an env list for credential environment names and accept config settings such as baseURL. Important for worker isolation: the official project-config mechanism is an overlay, not a clean config sandbox—non-conflicting global values remain merged, and discovery walks ancestors from the process cwd. OpenCode's V2 environment-compatibility audit identifies OPENCODE_CONFIG_CONTENT as completely ignored in V2; do not use it to override .opencode/opencode.json. The same V2 audit says OPENCODE_CONFIG is ignored; issue #48853 independently reproduces this in V2.0.3. Its audited V2 source revision is ba1f3d3d32690879fbed156d53570b8652e254ed; the official v2.0.3 release is tag v2.0.3 at d44b52c. I did not find a v2.0.3 source-level reproduction specific to OPENCODE_CONFIG_CONTENT, so the strongest pinned-release evidence is the V2 audit (rather than a 2.0.3 content-specific report); treat it as unsupported for 2.0.3 unless verified otherwise. OPENCODE_CONFIG_DIR has changed semantics in V2: the audit says it changes the global config root, rather than adding another config source. The current V2 config docs do not document it as a worker-isolation contract. A worktree-local config is the documented way to override matching settings while keeping cwd at the worktree; it does not guarantee full isolation from inherited global/ancestor settings. Strict isolation therefore has no documented V2 CLI switch that preserves the assigned worktree cwd. Environment interpolation for the root model was not found in V2 docs; prefer argv --model or a worker-local project config for per-run model selection.

**Pinned `@opencode/cli@2.0.3` and `OPENCODE_CONFIG_CONTENT`.** Treat the variable as unsupported/ignored: the official V2 compatibility audit explicitly lists inline configuration injection as completely ignored. If ignored, it contributes no layer, so it cannot override `.opencode/opencode.json`; V2 config docs say discovered `.opencode` configuration overrides matching direct project config, while all config sources merge. Evidence limit: the audit names V2 source revision `ba1f3d3d32690879fbed156d53570b8652e254ed`; the official 2.0.3 release tag points to `d44b52c`. I could not inspect the exact 2.0.3 source archive in this research run and found no 2.0.3-specific reproduction for `OPENCODE_CONFIG_CONTENT`. Issue #48853 does reproduce the separate `OPENCODE_CONFIG` file-path override failure on 2.0.3. Thus the best primary-source answer is “V2 does not honor it,” with the exact-package caveat above.

**MiMo V2.6 Flash custom provider/model.** OpenCode's current model data lists Xiaomi and MiMo-V2.6-Flash, and V2 selectors use provider/model. The native catalog choice is therefore xiaomi/mimo-v2.6-flash (confirm the exact active catalog with opencode models). Xiaomi's official API documentation gives the model ID mimo-v2.6-flash, OpenAI-compatible base URL https://api.xiaomimimo.com/v1, and authentication by api-key or Bearer using MIMO_API_KEY. If the worker needs an explicit custom provider config instead of the catalog, use a unique custom provider ID so it does not collide with the built-in xiaomi provider:

    {
      "$schema": "https://opencode.ai/config.json",
      "model": "mimo-direct/mimo-v2.6-flash",
      "providers": {
        "mimo-direct": {
          "name": "Xiaomi MiMo direct",
          "env": ["MIMO_API_KEY"],
          "package": "@opencode/ai/providers/openai-compatible",
          "settings": { "baseURL": "https://api.xiaomimimo.com/v1" },
          "models": {
            "mimo-v2.6-flash": {
              "modelID": "mimo-v2.6-flash",
              "name": "MiMo-V2.6-Flash",
              "capabilities": {
                "tools": true,
                "input": ["text", "image"],
                "output": ["text"]
              },
              "limit": { "context": 1048576, "output": 131072 }
            }
          }
        }
      }
    }

Then pass MIMO_API_KEY to the OpenCode server process (with --standalone) and select mimo-direct/mimo-v2.6-flash. Custom model capabilities/limits must be accurate; OpenCode says fallback metadata for custom models is not detected automatically. For native catalog auth, connect the xiaomi provider and use xiaomi/mimo-v2.6-flash.

**DeepSeek Flash fallback.** DeepSeek's current API model identifier is deepseek-flash (DeepSeek V4.1 Flash); OpenCode's model data identifies it under provider deepseek, so the selector is deepseek/deepseek-flash. The older DeepSeek model IDs deepseek-v4-flash and deepseek-v4-flash-vision-exp have been retired and are compatibility-routed to the current Flash model. OpenCode has a V2 retry hook for provider failures, but it changes the retry decision/delay, keeps a hard attempt limit, and does not document changing the selected model. The root configured-model fallback is only to the newest available supported model when the configured model is unavailable at selection time. No native failover from a failed MiMo request to DeepSeek was found. UC must detect a failed run and start a separate run selecting deepseek/deepseek-flash; context/session replay is the adapter's responsibility.

### External References

- [OpenCode V2 install](https://opencode.ai/v2/docs) — current npm package, native binary, and Windows install caveat.
- [OpenCode V2 migration guide](https://opencode.ai/v2/docs/migrate-v1) — states V1 and V2 both use opencode and share supported config locations.
- [OpenCode V2 CLI commands](https://opencode.ai/v2/docs/cli/commands/) — run, --standalone, --model, JSONL, auth, and command-local help.
- [OpenCode V2 CLI introduction](https://opencode.ai/v2/docs/cli) — project cwd, run automation, and shared/private server behavior.
- [OpenCode V2 config](https://opencode.ai/v2/docs/config) — project/global config discovery, precedence, model field, and permissions field.
- [OpenCode V2 permissions](https://opencode.ai/v2/docs/permissions) — ordered allow/ask/deny rules, V2 action names, and non-interactive approval requirement.
- [OpenCode V2 models](https://opencode.ai/v2/docs/models) — provider/model syntax, per-run model choice, model catalog/default and availability behavior.
- [OpenCode V2 providers](https://opencode.ai/v2/docs/providers) — custom provider env/package/baseURL/models fields and credential setup.
- [OpenCode V2 retry hook](https://opencode.ai/v2/docs/build/plugins/) — retry policy semantics and built-in attempt cap.
- [OpenCode Xiaomi MiMo catalog entry](https://opencode.ai/data/xiaomi/mimo-v2-6-flash) — OpenCode provider/model identity represented in its current model data.
- [Xiaomi MiMo-V2.6-Flash model page](https://mimo.mi.com/models/en-US/mimo-v2.6-flash) — official model ID, API compatibility, endpoint, and sample key usage.
- [Xiaomi first API call](https://mimo.mi.com/docs/en-US/quick-start/summary/first-api-call) — official API base URL, key types, and protocol compatibility.
- [OpenCode DeepSeek Flash catalog entry](https://opencode.ai/data/deepseek/deepseek-flash) — OpenCode provider/model identity represented in its current model data.
- [DeepSeek API changelog](https://api-docs.deepseek.com/updates/) — current DeepSeek Flash API model name and retired-ID compatibility routing.
- [OpenCode V1 CLI reference](https://opencode.ai/docs/cli/) — explicitly legacy reference for flags such as --dir and --auto; do not apply these to V2 without confirming the installed binary version.
- [Main/dev config loader](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/config/config.ts) and [environment flag definitions](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/flag/flag.ts) — source contains OPENCODE_CONFIG / OPENCODE_CONFIG_CONTENT handling on the main/dev code line; do not treat it as V2 behavior.
- [Official V2.0.3 release](https://github.com/anomalyco/opencode/releases/tag/v2.0.3) — release tag and source commit short SHA `d44b52c`.
- [V2 environment compatibility audit](https://github.com/anomalyco/opencode/issues/36990) — says OPENCODE_CONFIG_CONTENT and OPENCODE_CONFIG are ignored in the audited V2 revision; also documents changed OPENCODE_CONFIG_DIR semantics.
- [V2.0.3 OPENCODE_CONFIG regression report](https://github.com/anomalyco/opencode/issues/48853) — reproduces the config-path override failure on 2.0.3; it does not specifically test OPENCODE_CONFIG_CONTENT.
- [Current V2 config docs](https://opencode.ai/v2/docs/config) — project/global config discovery and merge precedence; docs do not promise a full-isolation CLI flag.
### Related Specs

- .trellis/spec/backend/agent-capability-spec.md — registry/capability rules and subtask agent configuration, including tool restrictions, MCP, prompts, and custom agents.
- No repository spec dedicated to OpenCode was found.

## Caveats / Not Found

- OpenCode V2 docs are rolling and the model selector is project/location-scoped. Confirm provider availability and exact model IDs with opencode models from the worker workspace; configure/authenticate the same server that runs the task.
- V2 is the current documented interface: the migration page says V1 and V2 both use the opencode command. Do not use the stale search snippet that labeled V2 as beta.
- V2 --format json is documented as newline-delimited JSON, but a stable schema is not published. Do not depend on the V1 run.ts event names/fields quoted in older material in this file.
- V2 permission docs expressly require non-interactive clients to decide how to handle ask; the CLI command documentation did not identify a V2 --auto mode. For unattended worker runs, make the policy explicit and keep denied actions denied.
- The direct MiMo custom config uses a unique local provider ID, mimo-direct, with the OpenAI-compatible provider package and official Xiaomi endpoint; the selector is consequently mimo-direct/mimo-v2.6-flash. The catalog/native selector is xiaomi/mimo-v2.6-flash. The V2 catalog and API service can change independently.
- The main/dev-source `OPENCODE_CONFIG_CONTENT` loader is not evidence that V2 supports it: V2 uses a separate audited code path, and official issue #36990 lists inline content injection as ignored there. For pinned 2.0.3, the audit is V2-specific but not a content-only 2.0.3 reproduction; #48853's 2.0.3 reproduction covers OPENCODE_CONFIG only. Use no inline override unless verified against the exact binary.
- No documented V2 mechanism guarantees a completely isolated configuration while retaining the assigned worktree as cwd. Worktree-local project config is documented and overrides matching settings, but V2 merges config and keeps non-conflicting global/ancestor values. OPENCODE_CONFIG_DIR changes the global config root according to the V2 audit, but the docs do not define it as a full isolation switch and it does not stop project-config discovery.
- For `@opencode/cli@2.0.3`, the V2 audit supports “OPENCODE_CONFIG_CONTENT is ignored,” but the exact tagged source archive and a content-specific 2.0.3 reproduction were unavailable. The independently verified 2.0.3 issue covers OPENCODE_CONFIG, a different variable; keep that distinction explicit.
- A narrow search of worker source, Dockerfile, compose file, and README found no current OpenCode invocation or installation reference.
