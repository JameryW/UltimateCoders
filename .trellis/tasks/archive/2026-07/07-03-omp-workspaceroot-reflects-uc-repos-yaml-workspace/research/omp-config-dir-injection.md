# Research: OMP Project Config Dir Injection (cwd=~/aiworks scenario)

- **Query**: How does OMP resolve project-level config dirs (.omp/, .claude/) and can it inject MULTIPLE or ADDITIONAL project config roots beyond the single cwd-derived getProjectDir()? Goal: launch OMP with cwd=~/aiworks while still discovering UltimateCoders/.claude/ and vendor/oh-my-pi/.omp/ configs.
- **Scope**: internal (OMP vendored source)
- **Date**: 2026-07-03

## Findings

### Files Found

| File Path | Description |
|---|---|
| `vendor/oh-my-pi/packages/utils/src/dirs.ts:175-186` | `projectDir` module global, `getProjectDir()`, `setProjectDir()` — the SINGLE project dir. `setProjectDir` calls `process.chdir()`. |
| `vendor/oh-my-pi/packages/coding-agent/src/config.ts:126-242` | `getConfigDirs()` (flat, cwd-only) + `findAllNearestProjectConfigDirs()` (walks UP from cwd). No extra-roots param. |
| `vendor/oh-my-pi/packages/coding-agent/src/cli/startup-cwd.ts:47-58` | `applyStartupCwd()` — where `--cwd` flag calls `setProjectDir`. |
| `vendor/oh-my-pi/packages/coding-agent/src/cli/flag-tables.ts:78-81,94-96,185` | `--cwd` (single string) and `--extension` (repeatable, pushes to `result.extensions`) flag parsing. |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/loader.ts:333-587` | Extension loading: `--extension` loads TS/JS modules, NOT `.omp/` config dirs. |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/types.ts:479-491,948-1178` | `ExtensionAPI` — no method to declare additional config/skill/command roots. `resources_discover` event returns `skillPaths/promptPaths/themePaths` only (no commands/agents/hooks). |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/extensions/runner.ts:766-807` | `emitResourcesDiscover()` — defined but has ZERO callers in the codebase (dead code / unimplemented wiring). |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/skills.ts:121-308` | `loadSkills()` — has `customDirectories` option (from `skills.customDirectories` setting) that scans ADDITIONAL skill dirs. Skills-only, not commands/agents/hooks. |
| `vendor/oh-my-pi/packages/coding-agent/src/config/settings-schema.ts:3976` | `skills.customDirectories` setting (array of paths). |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/custom-commands/loader.ts:106` | Custom commands loader uses `getConfigDirs("commands", ...)` only — NO extension hook, NO custom-directories setting. |
| `vendor/oh-my-pi/packages/coding-agent/src/discovery/builtin.ts:57-72` | Native provider's `getConfigDirs()` — checks only `ctx.cwd/.omp` (single project dir) + user agent dir. Walk-up is via `findNearestProjectConfigDir` (line 89-98) for SOME capabilities. |
| `vendor/oh-my-pi/packages/coding-agent/src/discovery/helpers.ts:27-108` | `SOURCE_PATHS` — fixed list of config dir names (.omp, .claude, .codex, ...). `getProjectPath()` joins `ctx.cwd` + `projectDir` + subpath. No extra-roots mechanism. |
| `vendor/oh-my-pi/packages/coding-agent/src/task/discovery.ts:61-76` | Agent discovery uses `findAllNearestProjectConfigDirs("agents", cwd)` — walk-up only, filters to `.omp` source. |

### How Config Discovery Works (the contract)

**There is ONE project dir: `getProjectDir()` (module global, defaults to `process.cwd()`).** Two discovery patterns consume it:

1. **Flat lookup** — `getConfigDirs(subpath, {cwd})` (`config.ts:126`): returns `[~/.omp/agent/<subpath>, ~/.claude/<subpath>, ..., <cwd>/.omp/<subpath>, <cwd>/.claude/<subpath>, ...]`. Project paths are `path.resolve(cwd, base, subpath)` — **only the single cwd**, no nesting.

2. **Walk-up lookup** — `findAllNearestProjectConfigDirs(subpath, cwd)` (`config.ts:213`): starts at `cwd`, walks UP via `path.dirname()` until root. For each config base (.omp, .claude, .codex, .gemini), returns the FIRST (nearest) directory found. **It never walks DOWN into subdirectories.** Used by agent discovery (`task/discovery.ts:71`).

**Implication for cwd=~/aiworks**: Starting at `~/aiworks`, walk-up finds nothing (no .omp/.claude at ~/aiworks or ~/). It MISSES `~/aiworks/UltimateCoders/.claude/` and `~/aiworks/UltimateCoders/vendor/oh-my-pi/.omp/` because those are BELOW cwd, and the walk only goes UP. Flat lookup also misses them because it only checks `~/aiworks/.omp` and `~/aiworks/.claude` (the single cwd).

### What's NOT Supported

| Mechanism | Supported? | Evidence |
|---|---|---|
| `--cwd` flag pointing at nested dir while process cwd stays elsewhere | NO (single value, calls `setProjectDir` which chdirs) | `startup-cwd.ts:48-49`: `setProjectDir(parsed.cwd)` mutates global + `process.chdir()` |
| Env var to add extra project config roots | NO | No `OMP_EXTRA_CONFIG_DIRS`, `OMP_PROJECT_PATHS`, etc. exist in dirs.ts or settings |
| `--extension` loading `.omp/` command/skill dirs | NO | `loader.ts:389-433`: resolves TS/JS entry files (index.ts/package.json manifest) only |
| `ExtensionAPI` declaring additional config roots | NO | `types.ts:948-1178`: no such method on `ExtensionAPI` |
| `resources_discover` event for injecting paths | PARTIAL (skills/prompts/themes only; AND the emitter is dead code) | `runner.ts:766` `emitResourcesDiscover` has zero callers; result type `ResourcesDiscoverResult` covers `skillPaths/promptPaths/themePaths` only (no commands/agents/hooks) |
| `skills.customDirectories` setting | YES (skills only) | `skills.ts:132,244-308` + `settings-schema.ts:3976` — scans additional dirs for SKILL.md files |
| Custom commands from extra dirs | NO | `custom-commands/loader.ts:106` uses `getConfigDirs("commands")` only |

### setProjectDir() — Cannot Be Used Pre-Launch Via Env

`setProjectDir()` (`dirs.ts:183`) is called from:
- `cli/startup-cwd.ts:49` — from `--cwd` flag (calls `process.chdir`)
- `modes/interactive-mode.ts:1008` — runtime `cd` command
- `slash-commands/builtin-registry.ts:1617` — `/cd` slash command
- `main.ts:1187` — session restore

There is **no env var** (`OMP_PROJECT_DIR`, `PI_PROJECT_DIR`, etc.) that calls `setProjectDir` at launch. The only CLI surface is `--cwd <single-dir>`, which sets the project dir to exactly one directory and chdir's into it.

### --extension Flag — Does NOT Load .omp/ Config Dirs

The `--extension` flag (`flag-tables.ts:78-81,185`) pushes paths to `result.extensions[]`. The loader (`loader.ts:285-587`):
1. Resolves each path relative to cwd (`resolvePath`, line 291)
2. If directory: reads `package.json` for `omp.extensions`/`pi.extensions` manifest entries, or falls back to `index.ts`/`index.js`, or scans one level for `*.ts`/`*.js` files (line 445-481)
3. Imports each as a TS/JS module and calls its factory function (line 305)

**It loads TypeScript/JavaScript extension code only.** It does NOT scan the extension's directory for `.omp/commands/`, `.omp/skills/`, `.omp/agents/`, `.omp/hooks/`, or `.claude/` subdirectories. Passing `--extension ../../packages/uc-orchestrator` loads `uc-orchestrator/src/extension.ts` (the TS module), not any `.omp/` config from that path.

`--extension` CAN be passed multiple times (it pushes to an array), but each invocation only loads TS/JS extension modules, never config-dir contents.

### resources_discover Event — Dead Code, Limited Scope

`ExtensionAPI.on("resources_discover", handler)` (`types.ts:971`) lets an extension return `{skillPaths?, promptPaths?, themePaths?}`. However:
1. The emitter `emitResourcesDiscover()` (`runner.ts:766`) has **ZERO callers** in the entire OMP codebase (grep confirms only the definition exists). It is not wired into `loadSkills()` or any other discovery path.
2. Even if wired, it only covers skills/prompts/themes — **NOT commands, agents, hooks, MCP configs, or settings**.

This is NOT a viable injection point.

### Extension Manifest — No Config-Root Declaration

`ExtensionManifest` (`loader.ts:359-363`) has fields: `extensions`, `themes`, `skills`. The `skills` field in a `package.json` `omp`/`pi` block declares skill entry paths for the EXTENSION itself (not additional scan roots). There is no manifest field for declaring additional project-config directories, command roots, or agent roots.

## Viable Approaches (ranked)

### Approach 1 (MOST VIABLE): Symlink nested `.omp`/`.claude` into the launch cwd

Since OMP's discovery is purely filesystem-path-based (no path allowlist or canonicalization that rejects symlinks — `resolveEquivalentPath` in `dirs.ts:146-153` actually calls `fs.realpathSync`), create symlinks at the launch cwd so the single-cwd discovery finds the nested configs:

```bash
# Launch cwd = ~/aiworks (or a dedicated wrapper dir)
mkdir -p ~/aiworks/.omp ~/aiworks/.claude
# Symlink the nested config contents UP into the launch root
ln -sf ~/aiworks/UltimateCoders/.claude/agents ~/aiworks/.claude/agents
ln -sf ~/aiworks/UltimateCoders/.claude/commands ~/aiworks/.claude/commands
ln -sf ~/aiworks/UltimateCoders/.claude/hooks ~/aiworks/.claude/hooks
ln -sf ~/aiworks/UltimateCoders/.claude/skills ~/aiworks/.claude/skills
ln -sf ~/aiworks/UltimateCoders/.claude/settings.json ~/aiworks/.claude/settings.json
ln -sf ~/aiworks/UltimateCoders/vendor/oh-my-pi/.omp/commands ~/aiworks/.omp/commands
ln -sf ~/aiworks/UltimateCoders/vendor/oh-my-pi/.omp/skills ~/aiworks/.omp/skills
```

Then launch OMP with `cwd=~/aiworks`. The flat `getConfigDirs()` and walk-up `findAllNearestProjectConfigDirs()` will both find `~/aiworks/.omp/` and `~/aiworks/.claude/` and follow the symlinks to the real files.

**Pros**: Works with ALL capability types (commands, skills, agents, hooks, MCP, settings). No OMP code changes. Survives the single-cwd constraint.
**Cons**: Must maintain symlinks if nested config structure changes. Two `.omp` sources (UltimateCoders has none; vendor/oh-my-pi has one) merge into one `.omp` at cwd — name collisions resolve by-symlink-order.

### Approach 2 (PARTIAL): `--cwd` at the nested repo + `skills.customDirectories` for extras

Launch with `--cwd ~/aiworks/UltimateCoders` (so `.claude/` is discovered natively), then use the `skills.customDirectories` user setting (`settings-schema.ts:3976`) to point at `vendor/oh-my-pi/.omp/skills` for the OMP-native skills.

**Pros**: No symlinks; uses supported settings.
**Cons**: Only covers SKILLS. Commands/agents/hooks from `vendor/oh-my-pi/.omp/` are still undiscovered. The UI workspace would show `UltimateCoders` not `~/aiworks` (which may be the actual goal per the task title "workspaceroot reflects uc.repos.yaml workspace").

### Approach 3 (NOT VIABLE without OMP patch): Fork OMP to add extra-roots

Patching `findAllNearestProjectConfigDirs` and `getConfigDirs` to accept an additional `extraRoots: string[]` param, plus a CLI flag / env var, would be the clean solution. But this requires modifying vendored OMP source (`vendor/oh-my-pi/packages/coding-agent/src/config.ts`, `discovery/builtin.ts`, `discovery/helpers.ts`, `task/discovery.ts`, `extensibility/custom-commands/loader.ts`, etc.) — a broad change to a submodule.

## Caveats / Not Found

- The task title mentions "workspaceroot reflects uc.repos.yaml workspace" — this research focused on OMP config-dir discovery, not on how the UC orchestrator extension sets the UI workspace name. The UC orchestrator may have its own workspace-display logic independent of OMP's `getProjectDir()`; that was not investigated here.
- `emitResourcesDiscover` being dead code was determined by grep across `packages/coding-agent/src/`; it is possible (but unlikely) that it is called from `packages/coding-agent/scripts/` or a test harness that I did not scan exhaustively.
- Whether OMP's `getProjectDir()`/`process.cwd()` is read by the UC orchestrator extension for its own workspace-root display was not checked (that would require reading `packages/uc-orchestrator/src/`).
- The `.trellis/spec/` directory was checked for relevant specs but none directly address OMP config-dir injection.
