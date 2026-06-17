import {describe, it, expect} from 'vitest';
import {getCommand, getCommandsForArea, getStatusBarHelp} from './keymap.js';
import type {FocusedArea} from './reducer.js';

describe('keymap: getCommand', () => {
  it('returns a command by id', () => {
    const cmd = getCommand('cycleFocus');
    expect(cmd).toBeDefined();
    expect(cmd!.id).toBe('cycleFocus');
    expect(cmd!.key).toBe('Shift+Tab / Ctrl+W');
    expect(cmd!.global).toBe(true);
  });

  it('returns undefined for unknown command', () => {
    expect(getCommand('nonexistent')).toBeUndefined();
  });
});

describe('keymap: getCommandsForArea', () => {
  it('includes global commands for any area', () => {
    const areas: FocusedArea[] = ['input', 'chat'];
    for (const area of areas) {
      const cmds = getCommandsForArea(area);
      const globalIds = cmds.filter((c) => c.global).map((c) => c.id);
      // All global commands should be present
      expect(globalIds).toContain('cycleFocus');
      expect(globalIds).toContain('subtaskOverlay');
      expect(globalIds).toContain('quit');
    }
  });

  it('includes area-specific commands', () => {
    const inputCmds = getCommandsForArea('input');
    expect(inputCmds.some((c) => c.id === 'indent')).toBe(true);

    const chatCmds = getCommandsForArea('chat');
    expect(chatCmds.some((c) => c.id === 'scrollUp')).toBe(true);
  });

  it('does not include other area commands', () => {
    const inputCmds = getCommandsForArea('input');
    expect(inputCmds.some((c) => c.id === 'scrollUp')).toBe(false);

    const chatCmds = getCommandsForArea('chat');
    expect(chatCmds.some((c) => c.id === 'indent')).toBe(false);
  });
});

describe('keymap: getStatusBarHelp', () => {
  it('returns at least one shortcut on narrow terminals', () => {
    const help = getStatusBarHelp('input', 60);
    expect(help).toContain('S-Tab');
    expect(help.length).toBeGreaterThan(0);
  });

  it('returns more shortcuts on wider terminals', () => {
    const narrow = getStatusBarHelp('input', 60);
    const wide = getStatusBarHelp('input', 200);
    // Wider terminal should have more or equal shortcuts
    expect(wide.length).toBeGreaterThanOrEqual(narrow.length);
  });

  it('includes focus and help shortcuts', () => {
    const help = getStatusBarHelp('input', 160);
    expect(help).toContain('focus');
    expect(help).toContain('help');
  });

  it('includes quit on wide terminals', () => {
    const help = getStatusBarHelp('input', 200);
    expect(help).toContain('quit');
  });
});
