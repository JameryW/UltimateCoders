# Research: Ink Keyboard Handling (Home/End/Delete/Shift+Tab/PageUp/PageDown)

- **Query**: How does Ink 5 handle Home/End/Delete/Shift+Tab/PageUp/PageDown keys? What are the escape sequences and workarounds?
- **Scope**: Mixed (internal code + external Ink source/releases)
- **Date**: 2026-06-17

## Findings

### 1. Home/End Keys

**Ink v5.2.1 (current project version):** The `Key` type does NOT include `home` or `end` properties.

However, Ink's internal `parseKeypress` function DOES correctly parse these keys via its `keyName` lookup table:

| Escape Sequence | keyName | Key type property (v5) |
|---|---|---|
| `\x1b[H` | `'home'` | NOT EXPOSED |
| `\x1b[F` | `'end'` | NOT EXPOSED |
| `\x1b[1~` | `'home'` | NOT EXPOSED |
| `\x1b[4~` | `'end'` | NOT EXPOSED |
| `\x1b[7~` | `'home'` (rxvt) | NOT EXPOSED |
| `\x1b[8~` | `'end'` (rxvt) | NOT EXPOSED |

**Current workaround in project:** The App.tsx uses `(key as any).home` and `(key as any).end` (lines 564, 568). This works at runtime because `parseKeypress` sets `keypress.name = 'home'`/`'end'`, but `useInput` in v5 does not map these to the `Key` object. The `(key as any)` cast is accessing properties that don't exist on the TypeScript type but also don't exist on the runtime object.

**This means Home/End are currently BROKEN at runtime in v5.** The `useInput` hook constructs the `Key` object with only the properties it knows about (upArrow, downArrow, etc.), and `home`/`end` are NOT among them. The `(key as any).home` will always be `undefined`.

**Fix in Ink v6.6.0+**: `home` and `end` were added to the `Key` type in PR #829 (merged 2025-12-21, released in v6.6.0 on 2025-12-22). The `useInput` hook now maps:
```typescript
home: keypress.name === 'home',
end: keypress.name === 'end',
```

### 2. Delete Key (Forward Delete)

**Ink v5.2.1:** `\x7f` (Backspace key on modern terminals) is mapped to `key.delete` in `parseKeypress`. The actual forward Delete key sends `\x1b[3~`, which is correctly parsed as `keypress.name = 'delete'` via the `keyName` table entry `'[3~': 'delete'`.

**The problem:** In v5, both Backspace (0x7F) and forward Delete (ESC[3~) produce `key.delete = true`. There is no way to distinguish them. The `key.backspace` property is only set for `\b` (Ctrl+H), which no modern terminal sends for the Backspace key.

**Current project behavior:** CjkTextInput.tsx line 241 treats `key.backspace || key.delete` identically -- both call `deleteBackward()`. This means pressing the actual Delete key (forward delete) deletes backward, which is incorrect.

**Fix in Ink v7.0.0**: The 0x7F byte is now mapped to `key.backspace` (not `key.delete`). The actual Delete key (ESC[3~) still maps to `key.delete`. This was a breaking change documented in the v7.0.0 release notes. PR #926 proposed this for v6 but was not merged; it was included in v7.0.0 (published 2026-04-08).

### 3. Shift+Tab

**Escape sequence:** `\x1b[Z` (ESC [ Z)

**Ink v5.2.1 handling:** `parseKeypress` maps `'[Z': 'tab'` in the keyName table, AND the `isShiftKey` function includes `'[Z'` in its list. This means:
- `keypress.name = 'tab'`
- `keypress.shift = true`

In `useInput`, this becomes:
- `key.tab = true` (because name === 'tab')
- `key.shift = true` (because keypress.shift is true)

So `key.shift && key.tab` correctly detects Shift+Tab in Ink v5.

**In tmux/screen:** The `\x1b[Z` escape sequence is passed through by both tmux and screen by default. Neither intercepts Shift+Tab. Ink's internal App component also checks for the raw string `'[Z'` (line 12 of App.js) for its built-in focus management.

**Current project behavior:** Works correctly. App.tsx line 458 checks `key.shift && key.tab` and dispatches `CYCLE_FOCUS`. CjkTextInput.tsx line 120 also checks `key.shift && key.tab` and returns early to let it bubble up.

**Potential issue:** Some older terminals or non-standard terminal emulators may not send `\x1b[Z` for Shift+Tab. In that case, an alternative like Ctrl+W could be used as a fallback.

### 4. PageUp/PageDown

**Ink v5.2.1:** The `Key` type DOES include `pageUp` and `pageDown` properties. They are mapped in `useInput`:

```typescript
pageDown: keypress.name === 'pagedown',
pageUp: keypress.name === 'pageup',
```

**Escape sequences handled by parseKeypress:**

| Escape Sequence | keyName |
|---|---|
| `\x1b[5~` | `'pageup'` |
| `\x1b[6~` | `'pagedown'` |
| `\x1b[[5~` | `'pageup'` (putty) |
| `\x1b[[6~` | `'pagedown'` (putty) |
| `\x1b[5$` | `'pageup'` (Shift+PageUp, rxvt) |
| `\x1b[6$` | `'pagedown'` (Shift+PageDown, rxvt) |
| `\x1b[5^` | `'pageup'` (Ctrl+PageUp, rxvt) |
| `\x1b[6^` | `'pagedown'` (Ctrl+PageDown, rxvt) |

**Current project behavior:** Works correctly. App.tsx lines 548-554 check `key.pageUp` and `key.pageDown` for chat scrolling.

**tmux caveat:** tmux may intercept PageUp/PageDown for its own scroll mode (copy-mode). If the TUI is running inside tmux, the user may need to configure tmux to pass these keys through, or use alternative keybindings (e.g., Ctrl+Up/Ctrl+Down).

### 5. Workaround Options for Ink v5

Since upgrading to Ink v6+ requires React 19 (v6) or Node.js 22 (v7), which may not be feasible immediately, here are workarounds:

#### Option A: Direct stdin listener (bypass useInput for missing keys)

Use `useStdin()` to get `internal_eventEmitter` and listen to raw `'input'` events, then call `parseKeypress` directly to access the full `keypress.name` including `home`, `end`, etc.

```typescript
import {useStdin} from 'ink';
import parseKeypress from 'ink/build/parse-keypress.js';

const {internal_eventEmitter} = useStdin();

useEffect(() => {
  const handler = (data: string) => {
    const keypress = parseKeypress(data);
    if (keypress.name === 'home') { /* handle Home */ }
    if (keypress.name === 'end') { /* handle End */ }
  };
  internal_eventEmitter?.on('input', handler);
  return () => { internal_eventEmitter?.removeListener('input', handler); };
}, []);
```

**Caveat:** This runs alongside `useInput`, so both handlers fire. Need to coordinate to avoid double-handling.

#### Option B: Extend the Key type with a custom hook

Create a wrapper around `useInput` that adds the missing properties by also listening to raw stdin:

```typescript
type ExtendedKey = Key & { home: boolean; end: boolean };

function useExtendedInput(handler: (input: string, key: ExtendedKey) => void) {
  const [lastKeypress, setLastKeypress] = useState<ParsedKey | null>(null);
  const {internal_eventEmitter} = useStdin();

  // Capture raw keypress for extended properties
  useEffect(() => {
    const handler = (data: string) => {
      setLastKeypress(parseKeypress(data));
    };
    internal_eventEmitter?.on('input', handler);
    return () => { internal_eventEmitter?.removeListener('input', handler); };
  }, []);

  useInput((input, key) => {
    const extendedKey: ExtendedKey = {
      ...key,
      home: lastKeypress?.name === 'home' ?? false,
      end: lastKeypress?.name === 'end' ?? false,
    };
    handler(input, extendedKey);
  });
}
```

**Caveat:** Race condition between the two event listeners. The `internal_eventEmitter` fires before `useInput`'s handler, so `lastKeypress` should be set by the time `useInput` fires, but React's state batching may delay it.

#### Option C: Upgrade to Ink v6.6.0+

This is the cleanest solution. Ink v6 requires:
- Node.js 20+
- React 19

The project currently uses React 18.3.1 and Node.js >=18. Upgrading React to 19 is the main blocker.

Ink v6.6.0 adds `home` and `end` to the `Key` type.
Ink v7.0.0 additionally fixes the Backspace/Delete confusion and adds Kitty keyboard protocol support.

### 6. Ink Version Summary

| Feature | Ink v5.2.1 (current) | Ink v6.6.0 | Ink v7.0.0+ |
|---|---|---|---|
| `key.home` / `key.end` | NO (broken) | YES | YES |
| `key.pageUp` / `key.pageDown` | YES | YES | YES |
| `key.backspace` vs `key.delete` | BROKEN (0x7F = delete) | BROKEN | FIXED (0x7F = backspace) |
| Shift+Tab (`key.shift && key.tab`) | YES | YES | YES |
| Kitty keyboard protocol | NO | YES (v6.2+) | YES |
| `key.super` / `key.hyper` | NO | NO (Kitty only in v6.2+) | YES |
| React version | 18 | 19 | 19.2+ |
| Node.js version | 18+ | 20+ | 22+ |

### 7. Delete Key (Forward Delete) Workaround for Ink v5

Since Ink v5 maps both Backspace (0x7F) and Delete (ESC[3~) to `key.delete`, the only way to distinguish them is to check the raw input string:

```typescript
useInput((input, key) => {
  if (key.delete) {
    // Check if this is actually the forward Delete key
    if (input === '\x1b[3~' || input.startsWith('\x1b[3')) {
      // Forward Delete - delete character after cursor
    } else {
      // Backspace (0x7F) - delete character before cursor
    }
  }
});
```

**Caveat:** In Ink v5, `useInput` sets `input = ''` for non-alphanumeric keys (line 68-70 of use-input.js), so the raw escape sequence is NOT available through the `input` parameter. The raw data is only available via `internal_eventEmitter`.

### Files Found

| File Path | Description |
|---|---|
| `tui/node_modules/ink/build/parse-keypress.js` | Ink v5 keypress parser - has full keyName table including home/end/delete/pageup/pagedown |
| `tui/node_modules/ink/build/hooks/use-input.js` | Ink v5 useInput hook - Key type missing home/end |
| `tui/node_modules/ink/build/hooks/use-input.d.ts` | Ink v5 Key type definition - no home/end |
| `tui/node_modules/ink/build/components/App.js` | Ink v5 App component - handles Shift+Tab via raw string '\x1B[Z' |
| `tui/src/components/App.tsx` | Project App - uses (key as any).home/.end (broken in v5) |
| `tui/src/components/CjkTextInput.tsx` | Project text input - treats backspace and delete identically |
| `tui/src/keymap.ts` | Project keymap - defines Home/End/PageUp/PageDown commands |

### External References

- [Ink PR #829](https://github.com/vadimdemedes/ink/pull/829) — Added `home` and `end` to Key type (merged 2025-12-21, released v6.6.0)
- [Ink PR #926](https://github.com/vadimdemedes/ink/pull/926) — Map 0x7F to backspace instead of delete (closed, not merged into v6; included in v7.0.0)
- [Ink Issue #634](https://github.com/vadimdemedes/ink/issues/634) — Original issue about 0x7F/delete confusion
- [Ink v6.6.0 Release](https://github.com/vadimdemedes/ink/releases/tag/v6.6.0) — First release with home/end support
- [Ink v7.0.0 Release](https://github.com/vadimdemedes/ink/releases/tag/v7.0.0) — Breaking: 0x7F now = backspace, Kitty protocol, React 19.2+, Node 22+
- [Kitty Keyboard Protocol](https://sw.kovidgoyal.net/kitty/keyboard-protocol/) — Enhanced keyboard input protocol supported in Ink v6.2+
- [ink-ui text-input](https://github.com/vadimdemedes/ink-ui/blob/main/source/components/text-input/use-text-input.ts) — Official Ink text input component (treats backspace/delete identically)

## Caveats / Not Found

1. **Home/End are currently broken at runtime** in the project. The `(key as any).home` cast in App.tsx accesses a property that does not exist on the runtime Key object in Ink v5. The `useInput` hook simply does not set `home` or `end` on the key object.

2. **Forward Delete is indistinguishable from Backspace** in Ink v5. Both set `key.delete = true`. The `input` parameter is empty for both, so the raw escape sequence cannot be used to distinguish them through the standard `useInput` API.

3. **Claude Code CLI source** is not publicly available for inspection. The npm package is a compiled binary (151KB). The GitHub repo contains only documentation and examples, not the actual TUI implementation.

4. **Ink v6 upgrade path** requires React 19, which is a significant dependency change. Ink v7 requires Node.js 22, which may not be available in all deployment environments.

5. **tmux scroll mode** may intercept PageUp/PageDown before they reach the Ink application. This is a terminal multiplexer configuration issue, not an Ink bug.
