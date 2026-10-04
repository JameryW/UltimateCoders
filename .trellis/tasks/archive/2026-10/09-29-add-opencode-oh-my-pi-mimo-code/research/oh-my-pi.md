# Research: Oh My Pi CLI as a non-interactive coding worker

- **Query**: Research official/current oh-my-pi (oh-my-pi / OMP) CLI contract for using it as a non-interactive coding agent: CLI executable and commands/options, output mode, working directory, permission/sandbox model, and config/provider auth. Distinguish existing vendored OMP extension usage from invoking OMP as a worker agent.
- **Scope**: mixed
- **Date**: 2026-09-29

## Findings

### Files Found

| File Path | Description |
|---|---|
| `run-omp.sh:134-136,302-304` | Launches the vendored OMP CLI with the UC extension and forwards OMP arguments. |
| `scripts/omp-workspace.sh:17` | Sets `OMP_ENTRY` to `vendor/oh-my-pi/packages/coding-agent/src/cli.ts`. |
| `packages/uc-orchestrator/src/extension.ts:1-24,53-58` | Defines the OMP-hosted UC slash commands, LLM tools, and extension startup. |
| `packages/uc-orchestrator/package.json:7-11,24-34` | Extension dev command explicitly invokes OMP with `--extension`; package metadata declares peer version `^13`. |
| `README.md:11,200-202,397` | Describes the optional OMP extension separately from worker agents; lists the current worker CLI adapters/default. |
| `vendor/oh-my-pi/packages/coding-agent/package.json:4,30-31` | Vendored OMP package version is `16.1.16`; its executable is `omp` mapped to `src/cli.ts`. |
| `vendor/oh-my-pi/packages/coding-agent/src/cli/args.ts:201-215` | Parses print mode, JSON mode, extension-discovery, approval, and other launch flags. |
| `vendor/oh-my-pi/packages/coding-agent/src/cli/flag-tables.ts:94-112,131-165,194-202` | Declares cwd, mode, provider/model, API-key, tools, and approval-mode arguments. |
| `vendor/oh-my-pi/packages/coding-agent/src/modes/print-mode.ts:1-12,20-31,51-61,69-114` | Defines one-shot text and JSON event-stream semantics, including stdout/stderr and error handling. |
| `vendor/oh-my-pi/docs/approval-mode.md:13-23,38-55` | Documents approval tiers, modes, overrides, and the fact approval policy is not OS containment. |
| `vendor/oh-my-pi/docs/providers.md:199-231,255-270,343-350` | Documents provider/model availability, credential precedence, login, env keys, and `.env` discovery. |
| `vendor/oh-my-pi/docs/settings.md:190,390-391` | Shows `modelRoles` configuration and retry/fallback settings. |
| `vendor/oh-my-pi/docs/non-compaction-retry-policy.md:43-48,75,171-172` | Documents eligible request failures and when model fallback is attempted. |
| `vendor/oh-my-pi/packages/catalog/src/models.json:7190-7218,24399-24401` | Bundled MiMo v2 Flash/v2.5 and DeepSeek V4 Flash model entries; no bundled MiMo v2.6 Flash entry. |
| `vendor/oh-my-pi/packages/catalog/src/provider-models/descriptors.ts:396-417` | Xiaomi and regional token-plan provider IDs/defaults/API-key environment variables. |
| `vendor/oh-my-pi/packages/coding-agent/src/session/agent-session.ts:3130,6156,10661,10942` | Waits for post-prompt retries, resolves per-role fallback chains, and applies model fallback. |

### Code Patterns

**Executable / invocation.** Upstream’s npm package exposes the `omp` executable (`package.json` maps `omp` to `src/cli.ts`). The upstream README shows a one-shot prompt as `omp -p "..."`, and current source supports `--cwd <path>`, `--provider <id>`, `--model <selector>`, `--api-key <key>`, `--tools <comma-list>`, `--max-time <seconds>`, `--no-session`, `--no-extensions`, `--no-skills`, `--no-rules`, and approval flags. For a worker, the basic contract is a process launched with its worktree as cwd (or `--cwd <worktree>`) and the task brief as the positional prompt. (`--mode` accepts `text`, `json`, `rpc`, `acp`, or `rpc-ui` in the vendored parser.)

**Output.** `omp -p "prompt"` is one-shot and exits after a response. In text mode, stdout receives the final assistant text; a provider/agent error or abort is emitted on stderr and exits nonzero. `omp --mode json "prompt"` is also one-shot and writes newline-delimited JSON records: a session header when present followed by agent events. This is an event stream, not one JSON response object; an adapter must consume the stream and select/aggregate the relevant events. `--print-thoughts` can add thinking blocks to text output. `--mode rpc` is a different stdio protocol intended for a supervising process that sends JSON commands and receives frames, rather than a single prompt/result invocation.

**Working directory / project config.** `--cwd` explicitly sets the OMP project/session working directory. Otherwise use the process cwd when starting the worker. OMP discovers project settings/resources relative to that location, so a project `.omp/config.yml`, project `.env`, rules, skills, and extensions may affect behavior. In the repo launcher, `run-omp.sh` first changes to `$OMP_WORKSPACE` and then executes the vendored TypeScript CLI through Bun.

**Permissions and isolation.** OMP approval is tool-tier policy (`read`, `write`, `exec`), with modes `always-ask`, `write`, and `yolo`; current upstream docs describe `yolo` as default and `--yolo` / `--auto-approve` force it for the session. Explicit per-tool user policies still apply; `deny` blocks and `prompt` still requests interaction. Headless calls cannot satisfy a prompt. This is not a process/filesystem/network sandbox: an approved shell command retains the process’s ambient access. OMP’s `--tools` can restrict the named built-in tools, and `--no-extensions` disables extension discovery, but neither flag creates OS-level containment. An isolated worker worktree/container is a separate execution boundary.

**Auth / provider config.** Model selectors are provider-qualified (`provider/model-id`); `--provider` and `--model` can select them for a run. Auth can be supplied through provider environment variables or `.env`, `omp login <provider>` / interactive `/login`, stored credentials, or `--api-key` for a process-only override. Upstream’s documented credential order is runtime `--api-key`, custom-provider `models.yml` key, stored OAuth, login-stored API key, provider env var (including `.env`), other stored key, then custom fallback resolver. Default agent config and custom providers live under `~/.omp/agent/` (`config.yml`, `models.yml`, local auth store `agent.db`); `PI_CODING_AGENT_DIR` relocates that base. The repo’s vendored provider catalog recognizes direct Xiaomi MiMo (`xiaomi`, `XIAOMI_API_KEY`; regional `xiaomi-token-plan-ams|cn|sgp` keys) separately from OpenCode Go/Zen (`OPENCODE_API_KEY`). Do not treat “MiMo” as a provider ID: verify which provider/account owns the desired model, then use the provider-qualified model name or `omp models` discovery.

**Existing extension vs worker agent.** The UC extension is loaded inside OMP as an interactive host integration: `run-omp.sh` launches the vendored CLI with `--extension "$UC_EXT"`, while the extension registers `/uc` commands and `uc_*` LLM-callable tools that bridge to the UC engine. That launcher can also start the local gateway/dashboard/storage workflow. It is not the Python worker’s coding-agent adapter. The README identifies the Python worker’s default as Grok Build (`grok`) and current adapter choices as Grok, local harness, Claude Code, and Codex; no OMP worker adapter is listed. A new OMP worker adapter would therefore invoke `omp -p` or `omp --mode json` as its coding process and translate exit/output into the worker contract; it should not conflate that with launching the UC extension host.

### External References

- [Oh My Pi README](https://github.com/can1357/oh-my-pi) — current upstream entry points; documents interactive `omp`, one-shot `omp -p`, RPC/ACP, installation, provider families, and custom provider setup.
- [CLI flag definitions](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/cli/flag-tables.ts) — upstream source of current flag names and accepted values, including cwd, provider/model, API key, tools, and approval mode.
- [Print-mode implementation](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/modes/print-mode.ts) — authoritative source for one-shot text vs JSON event-stream behavior.
- [Tool approval modes](https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md) — approval tiers, mode defaults/overrides, headless subagent behavior, and ambient access caveat.
- [Provider and credential guide](https://github.com/can1357/oh-my-pi/blob/main/docs/providers.md) — provider IDs, env-var map, auth precedence, login, and custom `models.yml` providers.
- [Environment-variable reference](https://github.com/can1357/oh-my-pi/blob/main/docs/environment-variables.md) — provider env variables and OMP directory overrides.

### Related Specs

- Not searched; this request concerns an external CLI contract and existing launcher/worker documentation.

## Caveats / Not Found

- Upstream’s README documents the one-shot command, but flags and exact output details are best treated as versioned CLI-source behavior; `omp --help` and `omp <subcommand> --help` on the runtime binary remain the contract for that installed version.
- The repo’s vendored CLI source is version `16.1.16`, while the UC extension package metadata declares a `^13` coding-agent peer. The launcher executes the vendored source path, so the peer range should not be mistaken for the actual launched CLI version.
- Current upstream `main` may advance independently of this repository’s vendored checkout. Re-check the exact vendored source when implementing an adapter.

## MiMo default / DeepSeek fallback

Current upstream OMP supports this configuration shape in global `~/.omp/agent/config.yml`, project `.omp/config.yml`, or a one-process `--config` overlay: `modelRoles.default` selects the primary chat model, and `retry.fallbackChains.default` lists ordered provider-qualified fallback selectors. `retry.enabled` and `retry.modelFallback` are both `true` by default; set them explicitly if an unattended worker must make this behavior unambiguous.

```yaml
modelRoles:
  default: xiaomi/mimo-v2.6-flash
retry:
  enabled: true
  modelFallback: true
  fallbackChains:
    default:
      - deepseek/deepseek-v4-flash
```

Xiaomi’s official model list gives the API model ID `mimo-v2.6-flash`; qualify it with the OMP provider ID. For the regional China coding-plan account, the provider is `xiaomi-token-plan-cn`, so the selector is `xiaomi-token-plan-cn/mimo-v2.6-flash` and the documented key is `XIAOMI_TOKEN_PLAN_CN_API_KEY`; for standard Xiaomi API auth use provider `xiaomi`, `xiaomi/mimo-v2.6-flash`, and `XIAOMI_API_KEY`. OMP release `v18.3.2` (2026-09-26) includes curated MiMo V2.6 metadata for the China token-plan provider. `deepseek/deepseek-v4-flash` is the vendored catalog’s DeepSeek V4 Flash selector; its provider key is `DEEPSEEK_API_KEY`.

The repo’s vendored OMP `16.1.16` does **not** contain `mimo-v2.6-flash` in its bundled catalog; it has older `xiaomi/mimo-v2-flash` / `xiaomi/mimo-v2.5` entries. However, the built-in Xiaomi provider manager can discover live models from Xiaomi’s OpenAI-compatible `/models` endpoint when a Xiaomi key is configured (`vendor/oh-my-pi/packages/catalog/src/provider-models/openai-compat.ts:2708-2757`; provider descriptors at `vendor/oh-my-pi/packages/catalog/src/provider-models/descriptors.ts:396-417`). Thus a V2.6 ID returned by the account’s endpoint can be available without updating OMP’s static catalog; if that endpoint does not list it or startup discovery is unsuitable, explicitly register it in `models.yml` as below. The requested DeepSeek fallback selector is present in the vendored catalog.

Native fallback is supported for the main agent session: on eligible provider/request errors OMP consults the configured role chain and retries on the next authenticated, resolvable model. The current retry docs include usage/rate limits, overload, network/transport failures, and HTTP 429/5xx; context overflow follows its separate compaction/promotion path, and a partially streamed/tool-executed turn is not blindly replayed. One-shot `omp -p` uses this same `AgentSession.prompt()` path and waits for post-prompt recovery before printing the result. A configured chain is therefore automatic failover for eligible failures, not a guarantee that every error category or every tool-side one-shot will switch models. `retry.fallbackRevertPolicy` defaults to `cooldown-expiry`; use `never` if the session should remain on DeepSeek after switching.

### Additional sources

- [OMP settings: model roles and retry fallback chains](https://github.com/can1357/oh-my-pi/blob/main/docs/settings.md) — documents `modelRoles` and role-keyed `retry.fallbackChains` configuration.
- [OMP non-compaction retry policy](https://github.com/can1357/oh-my-pi/blob/main/docs/non-compaction-retry-policy.md) — eligible retry classes, fallback order, and failure exclusions.
- [OMP v18.3.2 release notes](https://github.com/can1357/oh-my-pi/releases/tag/v18.3.2) — current release notes list curated Xiaomi Token Plan (China) MiMo V2.6 metadata.
- [Xiaomi MiMo official model list API](https://mimo.mi.com/docs/en-US/api/model/list-models) — confirms the provider-native ID `mimo-v2.6-flash`.

## Vendored 16.1.16: per-run role/fallback and explicit MiMo registration

The vendored CLI accepts repeatable `--config` flags (`vendor/oh-my-pi/packages/coding-agent/src/cli/flag-tables.ts:97-99`). It passes these files as settings overlays; `Settings` resolves them relative to the working directory and deep-merges them after the persistent/project layers (`vendor/oh-my-pi/packages/coding-agent/src/config/settings.ts:227-230,616-647`). The vendored schema contains `modelRoles` (`vendor/oh-my-pi/packages/coding-agent/src/config/settings-schema.ts:454`) and `retry.modelFallback` / `retry.fallbackChains` (`vendor/oh-my-pi/packages/coding-agent/src/config/settings-schema.ts:1217-1228`), so the default role and model fallback chain are supported per invocation. A settings overlay is not the model catalog: custom model definitions are read from the separate `models.yml` at `~/.omp/agent/models.yml` (relocated with `PI_CODING_AGENT_DIR`), per `vendor/oh-my-pi/docs/models.md:15-27` and `vendor/oh-my-pi/docs/settings.md:18-29`.

For deterministic model resolution in a worker, place a minimal `models.yml` in a worker-owned agent directory, set `PI_CODING_AGENT_DIR` to that directory, and keep the primary/fallback settings in a separate temporary overlay passed with `--config`. For the standard Xiaomi API, the registration shape is:

```yaml
providers:
  xiaomi:
    baseUrl: https://api.xiaomimimo.com/v1
    api: openai-completions
    apiKey: XIAOMI_API_KEY
    models:
      - id: mimo-v2.6-flash
        name: MiMo V2.6 Flash
```

The `models.yml` schema requires provider `baseUrl`, provider/model `api`, and an `apiKey` unless `auth: none`; model `contextWindow` and `maxTokens` are optional (`vendor/oh-my-pi/docs/models.md:38-145`; `vendor/oh-my-pi/packages/coding-agent/src/config/models-config-schema.ts:121-142,209-224`). `apiKey` is first resolved as an environment-variable name, then treated as a literal only if that environment variable is absent (`vendor/oh-my-pi/docs/models.md:368-381`), so inject `XIAOMI_API_KEY` through the worker environment and never write the secret itself into the YAML. This entry registers the provider-qualified selector `xiaomi/mimo-v2.6-flash`. For a China token-plan key, use `xiaomi-token-plan-cn` and its endpoint `https://token-plan-cn.xiaomimimo.com/v1`, key env `XIAOMI_TOKEN_PLAN_CN_API_KEY`, and selector `xiaomi-token-plan-cn/mimo-v2.6-flash` (descriptor lines above). Avoid defining another provider alias unless necessary: matching built-in provider IDs preserve the correct auth/environment mapping.

Use the overlay settings:

```yaml
modelRoles:
  default: xiaomi/mimo-v2.6-flash
retry:
  enabled: true
  modelFallback: true
  fallbackChains:
    default:
      - deepseek/deepseek-v4-flash
```

The file is loaded with `omp --config <overlay.yml> ...`; DeepSeek credentials must also be available as `DEEPSEEK_API_KEY`. Native failover remains limited to OMP’s eligible retry failures described in the earlier section. Minimal isolation is worker-scoped `PI_CODING_AGENT_DIR` + worker-scoped `models.yml` + an untracked per-run settings overlay + env-injected provider keys; this separates OMP auth/config state but is not an OS sandbox.

### Vendored source references

- `vendor/oh-my-pi/packages/coding-agent/src/cli/flag-tables.ts:97-99` — repeatable `--config`.
- `vendor/oh-my-pi/packages/coding-agent/src/config/settings.ts:227-230,616-647` — overlay paths, precedence, strict parsing.
- `vendor/oh-my-pi/packages/coding-agent/src/config/settings-schema.ts:454,1217-1228` — role and retry/fallback settings.
- `vendor/oh-my-pi/packages/catalog/src/provider-models/openai-compat.ts:2706-2747` — Xiaomi runtime `/models` manager.
- `vendor/oh-my-pi/packages/catalog/src/provider-models/descriptors.ts:396-417` — Xiaomi provider IDs, endpoint region, key env variables.
- `vendor/oh-my-pi/packages/coding-agent/src/config/model-registry.ts:818-835,1160-1200,1649-1675` — custom-model merge and parsing.
- `vendor/oh-my-pi/docs/models.md:15-27,38-145,368-381` and `vendor/oh-my-pi/docs/settings.md:18-29,190,380-391` — config locations, schema, auth, overlays, and fallback keys.
