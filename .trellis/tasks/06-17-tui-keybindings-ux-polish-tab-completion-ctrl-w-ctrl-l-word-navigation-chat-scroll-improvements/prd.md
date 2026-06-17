# TUI Keybindings UX Polish

## Goal

Polish TUI keyboard interaction to be consistent, discoverable, and feature-complete: Tab completes slash commands, Ctrl+W cycles focus alongside Shift+Tab, word navigation works in input, and all keymap-defined shortcuts actually function.

## What I already know

* **Tab**: CjkTextInput always inserts 2 spaces (line 125-131); `commandSuggestions` state + `matchCommands` + `onValueChange` infrastructure exists but no Tab-accept logic
* **Ctrl+W**: Missing from both keymap.ts and App.tsx useInput; commit 331bba8 mentioned it but it was never added to COMMANDS
* **Word navigation**: Ctrl+Left/Ctrl+Right NOT in keymap COMMANDS array, NOT implemented in CjkTextInput (only single-grapheme arrow left/right)
* **Ctrl+L (clear log)**: Works in chat focus (App.tsx:572) — matches keymap definition
* **Home/End**: Chat uses raw stdin workaround `(key as any).home`; input uses Ctrl+A/Ctrl+E only
* **Undo/Redo/Paste**: NOT in keymap COMMANDS array, NOT implemented — aspirational only
* **Help overlay + StatusBar**: Derive from keymap.ts — adding to COMMANDS auto-propagates
* **cjk-input-utils.ts**: Pure functions (insertAtCursor, deleteBackward, deleteToEnd, renderInputWithCursor, cursorDisplayCol) — no word-boundary utilities yet

## Assumptions (temporary)

* Tab should complete when slash command suggestions are visible, indent otherwise
* Ctrl+W should be added as global alias for Shift+Tab cycle-focus
* Word navigation needs both keymap entry AND CjkTextInput implementation + cjk-input-utils word-boundary helper

## Open Questions

* Tab behavior: complete > indent when suggestions visible, indent otherwise — is this correct?

## Requirements (evolving)

* Tab completes slash command when suggestions visible, inserts indent otherwise
* Ctrl+W cycles focus (global, same as Shift+Tab)
* Ctrl+Left/Ctrl+Right move cursor by word in input
* All keymap-defined shortcuts verified functional
* Help overlay and status bar reflect all shortcuts accurately

## Acceptance Criteria (evolving)

* [ ] Typing `/he` then Tab completes to `/help`
* [ ] Tab with no suggestion inserts 2-space indent
* [ ] Ctrl+W cycles focus input→chat→input
* [ ] Ctrl+Left/Ctrl+Right move by word boundary in input
* [ ] Help overlay shows Ctrl+W and word navigation shortcuts
* [ ] Status bar help text reflects current shortcuts
* [ ] All existing keymap shortcuts still work
* [ ] keymap.test.ts updated for new commands

## Definition of Done

* Lint / typecheck / CI green
* Keymap test updated for new commands
* No dead keymap entries (every defined shortcut works)

## Out of Scope

* Ink v6 migration (Home/End raw stdin workaround stays)
* Bracketed paste mode (noted as TODO in CjkTextInput)
* Undo/Redo implementation (too complex for this polish pass)
* Paste implementation (needs bracketed paste, Ink 6+)
* New features beyond keybinding polish

## Technical Notes

* keymap.ts COMMANDS is single source of truth — StatusBar + help overlay derive from it
* CjkTextInput handles its own useInput with {isActive: focus} — input shortcuts live here
* App.tsx useInput handles global + chat-area shortcuts
* Need word-boundary helper in cjk-input-utils.ts (split on whitespace/CJK boundary)
* Raw stdin workaround in CjkTextInput for Home/End (ponytail: remove when Ink v6.6+)
