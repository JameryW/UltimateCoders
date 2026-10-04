# Support OpenCode, oh-my-pi, and MiMo Code agents

## Goal

Allow UltimateCoders workers to select OpenCode, oh-my-pi, or MiMo Code as a coding agent alongside the existing Worker adapters.

## Background

- Worker coding agents are subprocess adapters registered through `AgentAdapterRegistry`; CLI agents are advertised only when their executable is available on `PATH`.
- Each adapter must build a sandbox subprocess request (`command`, `args`, timeout, work directory, filtered environment) and translate its output/exit state to `AgentOutput`.
- The worker Docker image currently installs the supported CLI executables. The user confirmed the three new CLIs should also be installed in the standard image.
- UltimateCoders' existing oh-my-pi integration is a local terminal extension host, not a Worker adapter; this work adds a separate adapter and preserves the existing `run-omp.sh` flow.
- MiMo Code is a coding-agent CLI distinct from the MiMo model/API used by UC task planning.
- GitHub CLI authentication is invalid, so planning artifacts use the local Markdown tracker at `.scratch/`.

## Requirements

- Register canonical `opencode`, `oh-my-pi`, and `mimo-code` Worker agents, with executable aliases `omp` and `mimo` where applicable.
- Invoke each CLI in a one-shot, headless mode from the assigned task worktree, honor the Worker timeout, and report useful output/errors.
- Parse their JSONL/event output defensively; process exit code and timeout remain the primary success signals.
- Map the existing generic agent configuration (`tools`, allow/deny rules, MCP configuration, prompt append, and agent name) to each CLI where supported; report unsupported fields explicitly rather than silently ignoring them.
- Advertise an agent only when its CLI is installed, using the registry's existing CLI probe behavior.
- Install the three CLI dependencies in the standard Worker Docker image and document selection, credentials, and provider/model configuration.
- Extend the sandbox's per-agent environment allowlist without exposing unrelated host secrets to child processes.
- Configure each new CLI to default to MiMo v2.6 Flash; configure DeepSeek Flash failover only where the CLI natively supports it.
- Keep task planning's `UC_LLM_*` configuration independent from each coding-agent CLI's provider configuration.

## Model and Fallback Decisions

- All three adapters explicitly select MiMo v2.6 Flash as their primary model; they do not inherit the separate UC task-planning model setting.
- Configure DeepSeek Flash failover only through each CLI's own native model fallback feature. Current research confirms this for oh-my-pi; OpenCode's retry feature retries a request but does not switch models, and MiMo Code's retry policy retries the selected model. Therefore only oh-my-pi receives an automatic DeepSeek Flash fallback. Do not add whole-process retries that could repeat code edits.
- Use provider-qualified selectors/configuration appropriate to each CLI. Current researched selectors are `xiaomi/mimo-v2.6-flash` for OMP, `mimo/mimo-v2.6-flash` for MiMo Code's direct API-key provider, and `mimo-direct/mimo-v2.6-flash` for OpenCode's direct OpenAI-compatible MiMo endpoint. OMP's DeepSeek fallback selector is `deepseek/deepseek-v4-flash`; DeepSeek currently routes that legacy selector to its Flash model.
- The repository's vendored OMP 16.1.16 supports per-run settings overlays and custom model entries, so it can select MiMo v2.6 Flash without upgrading the local `run-omp.sh` extension host.

## Acceptance Criteria

- [x] `UC_CODING_AGENT` can select each adapter using its documented canonical name.
- [x] Worker capability discovery includes each adapter only when its CLI is present.
- [x] Each adapter invokes its CLI in one-shot mode from the assigned worktree, parses JSONL output defensively, and reports timeout/non-zero exit failures through the Worker contract.
- [x] Existing generic agent configuration is mapped when the CLI supports it; unsupported fields are surfaced clearly.
- [x] MiMo v2.6 Flash is the new adapters' default model; DeepSeek Flash fallback is configured only for CLIs with a supported native failover mechanism, and limitations are documented.
- [x] The Worker Docker image installs all three CLIs and its build/version checks identify the executables.
- [x] OpenCode uses a worker-scoped global config and private server; it warns when project config files are present because V2 may merge and override matching worker settings.
- [x] OMP fails closed for empty or unmappable explicit tool allowlists, limits MCP access to selected servers for `mcp__server__*` rules, and skips servers when it cannot enforce a per-tool rule.
- [x] README and Docker env example document canonical names, aliases, and required auth/config.
- [x] The UC LLM planning default and its DeepSeek fallback remain governed by `UC_LLM_*` settings.

## Out of Scope

- Changing Grok Build as the default coding agent.
- Replacing the existing oh-my-pi terminal extension or its launcher.
- Changing MiMo v2.6 Flash / DeepSeek Flash task-planning defaults.
- Implementing remote OpenCode/ACP services; this adapter runs a local CLI subprocess inside the Worker.

## Technical Notes

- Existing registration and subprocess contracts: `python/ultimate_coders/agent/registry.py`, `python/ultimate_coders/agent/sandbox.py`, `python/ultimate_coders/agent/harness_deepseek.py`, and `python/ultimate_coders/agent/harness_local_loop.py`.
- Per-adapter child-process secret boundary: `ADAPTER_ENV_ALLOWLIST` in `python/ultimate_coders/agent/sandbox.py`.
- Worker CLI installs: `docker/Dockerfile`; current agent selection docs: `README.md` and `docker/.env.example`.
- Vendor CLI research:
  - [`research/opencode.md`](research/opencode.md) — current V2 install/CLI, standalone headless JSONL runs, direct MiMo provider configuration, and V2 permission handling.
  - [`research/oh-my-pi.md`](research/oh-my-pi.md) — `omp -p`/JSON mode, `--cwd`, approvals, providers, and distinction from the existing extension launcher.
  - [`research/mimo-code.md`](research/mimo-code.md) — `mimo run --format json`, MiMo Code auth/provider setup, and distinction from MiMo models/API.

## Implementation Shape

- Add one adapter per CLI and register canonical `opencode`, `oh-my-pi`, and `mimo-code` names (aliases `omp` and `mimo`) through the existing plugin registry.
- Keep each CLI's provider settings isolated to its subprocess. OpenCode receives a worker-scoped HOME/XDG config and private standalone server, but V2 also merges project config discovered from the assigned worktree and ancestors; matching provider or permission values may override worker settings, and the adapter warns when those files exist. OMP receives a private model catalog and a temporary settings overlay; MiMo Code receives a task-scoped `MIMOCODE_HOME`, direct MiMo API-key config, and explicit provider/model selection.
- Pin and verify the CLI packages in the standard Worker image, using OpenCode's current V2 `@opencode/cli` package. Install Bun for the oh-my-pi runtime; do not upgrade the repository's vendored OMP submodule or alter `run-omp.sh`.
- Extend each adapter's environment allowlist only for the MiMo and DeepSeek credentials it needs. Do not forward unrelated host credentials.
- Update README and `docker/.env.example` with agent selection, per-CLI credentials, model defaults, and the OMP-only native DeepSeek fallback.
