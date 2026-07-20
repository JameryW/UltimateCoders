# ci-dashboard-tsc-noop

## Goal

`ci-dashboard.yml` runs `tsc --noEmit` from the dashboard root, but the root
`tsconfig.json` is a project-references shell (`files: []` + `references`).
A bare `tsc --noEmit` (no `-p`, no `--build`) does NOT type-check the
referenced projects' `src` — it silently exits 0. Confirmed empirically:
injected `const x: number = "str"` into `src/` → CI command returned 0 with no
output; `tsc -p tsconfig.app.json --noEmit` caught it. So type errors in
`dashboard/src` slip through CI green.

Fix the CI command to actually type-check `src/` (+ node config).

## What I already know

- Root tsconfig: `dashboard/tsconfig.json` → `files: []`, references app + node.
- `dashboard/tsconfig.app.json` → `include: ["src"]`, strict, noEmit.
- `dashboard/tsconfig.node.json` → vite config typing.
- Injected-error probe: `tsc --noEmit` exit 0 / silent; `tsc -p tsconfig.app.json --noEmit` exit 0 only when clean, reports errors otherwise.
- Current src is CLEAN — both `-p` checks pass exit 0 today. No follow-up cleanup needed.

## Requirements

- Change ci-dashboard.yml typecheck step from `pnpm exec tsc --noEmit` to check both referenced projects:
  `pnpm exec tsc -p tsconfig.app.json --noEmit && pnpm exec tsc -p tsconfig.node.json --noEmit`.
- `vite build` job already runs real build (catches some errors) — keep as-is.

## Acceptance Criteria

- [ ] Injected type error in `dashboard/src/*.tsx` FAILS the typecheck job.
- [ ] Clean src PASSES (current state).
- [ ] Both tsconfig.app.json + tsconfig.node.json checked.

## Definition of Done

- CI command changed.
- PR opened + CI green (on clean src) + merged.

## Out of Scope

- Migrating to `tsc --build` (composite) — larger change, not needed.
- Dashboard src type-error cleanup (none exist).

## Technical Notes

- `.github/workflows/ci-dashboard.yml` typecheck job, last step.
- `dashboard/tsconfig.json` (root shell), `tsconfig.app.json` (src), `tsconfig.node.json`.
- Memory: dashboard-ci-tsc-noop.
