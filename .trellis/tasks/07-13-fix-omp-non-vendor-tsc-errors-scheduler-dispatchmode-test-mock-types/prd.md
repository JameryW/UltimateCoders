# Fix OMP Non-Vendor TSC Errors

## Background

OMP `bun run check` shows 11 non-vendor TS errors (beyond vendor/oh-my-pi
pre-existing). Dashboard lint is 0/0; these are the remaining OMP tsc issues.

## Errors (verified this session)

1. `scheduler.ts:568` — `Type 'string' is not assignable to type '"remote" | "local" | "prefer_remote"'`. A `mode` variable is typed `string` but returned as a `DispatchMode` union. Fix: cast or narrow.
2. `uc-rpc-server.test.ts:24-81` — 10 errors, all `Property 'X' does not exist on type '{}'`. Test mocks return `{}` but access `.tasks`/`.task`/`.ok`/`.status`. Fix: type the mock returns.

## Decisions (locked)

- **D1**: `scheduler.ts:568` — read the `mode` variable's source. If it's a
  `DispatchMode` already, the type annotation is too narrow upstream. If
  it's genuinely `string`, cast `mode as DispatchMode` (or import + use the
  union type). Minimal fix.
- **D2**: `uc-rpc-server.test.ts` — type the mock function returns. The mock
  likely returns `{} as SomeType` or plain `{}`. Add a return type annotation
  matching the real RPC response shape (e.g. `{ tasks: TaskSync[]; ok: boolean;
  status: string; task: TaskSync }`). Read the mock to determine the shape.
- **Out of scope**: vendor/oh-my-pi errors (pre-existing, upstream submodule).

## Acceptance Criteria

- [ ] `cd packages/uc-orchestrator && bun run check` shows 0 non-vendor errors.
- [ ] `bun test` passes (test mocks still work).
- [ ] No behavior change (type annotations + casts only).

## Technical Approach

1. `scheduler.ts:568` — read context, fix the type (cast or narrow).
2. `uc-rpc-server.test.ts` — read the mock function(s), add return types.
3. Verify: `bun run check` non-vendor 0 errors, `bun test` green.

## Risk

- **Low**: type annotations + casts. No runtime change. Test mocks just
  get proper types.
