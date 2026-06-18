# TUI Polish: Ctrl+W, Tab Completion, Keybinding Fixes

## Goal

Fix three concrete TUI keybinding gaps: Ctrl+W not cycling focus, Tab slash-command completion not wired end-to-end, and missing commandSuggestions state in App.

## What I already know

* `keymap.ts` defines `Ctrl+W` as `cycleFocus` alias for `Shift+Tab`
* `CjkTextInput` correctly passes `Ctrl+W` through to parent (line 224)
* App.tsx `useInput` only checks `key.shift && key.tab` — missing `Ctrl+W`
* `CjkTextInput` has `tabCompleteCommand` prop that completes slash commands on Tab
* `TaskInput` receives `commandSuggestions` and `onValueChange` props but never passes `tabCompleteCommand` to `CjkTextInput`
* App.tsx never passes `commandSuggestions`, `onValueChange`, or `tabCompleteCommand` to TaskInput
* `commands.ts` has `matchCommands()` for autocomplete matching

## Requirements

* Ctrl+W cycles focus (same as Shift+Tab) in all contexts
* Tab completes slash command when suggestions are visible, inserts indent otherwise
* App manages commandSuggestions state and wires it through TaskInput → CjkTextInput
* Help overlay and StatusBar already reflect these shortcuts (no changes needed there)

## Acceptance Criteria

* [ ] Ctrl+W cycles focus: input→chat→input (same as Shift+Tab)
* [ ] Typing `/h` then Tab completes to `/help `
* [ ] Typing `/` then Tab completes to first matching command
* [ ] Tab without slash prefix still inserts indent (2 spaces)
* [ ] Existing Shift+Tab behavior unchanged

## Definition of Done

* Manual test: run TUI, verify each keybinding
* No regressions in existing keybindings
* cargo check / tui build passes

## Out of Scope

* Paste handling (bracketed paste mode)
* Ink v6 migration (Home/End workaround stays)
* useAnimation migration for StatusBar/StatusIndicator
* Python Textual TUI

## Technical Notes

* Files: `tui/src/components/App.tsx`, `tui/src/components/TaskInput.tsx`
* `CjkTextInput.tsx` already correct — no changes needed
* `keymap.ts` already correct — no changes needed
* `commands.ts` `matchCommands()` already correct — no changes needed
