import {describe, it, expect} from 'vitest';
import {getCommand, getCommandsForArea, getStatusBarHelp} from './keymap.js';
import type {FocusedArea} from './reducer.js';

describe('keymap: getCommand', () => {
  it('returns a command by id', () => {
    const cmd = getCommand('cycleFocus');
    expect(cmd).toBeDefined();
    expect(cmd!.id).toBe('cycleFocus');
    expect(cmd!.key).toBe('Shift+Tab');
    expect(cmd!.global).toBe(true);
  });

  it('returns undefined for unknown command', () => {
    expect(getCommand('nonexistent')).toBeUndefined();
  });
});

describe('keymap: getCommandsForArea', () => {
  it('includes global commands for any area', () => {
    const areas: FocusedArea[] = ['input', 'chat', 'subtask'];
    for (const area of areas) {
      const cmds = getCommandsForArea(area);
      const globalIds = cmds.filter((c) => c.global).map((c) => c.id);
      // All global commands should be present
      expect(globalIds).toContain('cycleFocus');
      expect(globalIds).toContain('swapPane');
      expect(globalIds).toContain('quit');
    }
  });

  it('includes area-specific commands', () => {
    const inputCmds = getCommandsForArea('input');
    expect(inputCmds.some((c) => c.id === 'indent')).toBe(true);

    const chatCmds = getCommandsForArea('chat');
    expect(chatCmds.some((c) => c.id === 'scrollUp')).toBe(true);

    const subtaskCmds = getCommandsForArea('subtask');
    expect(subtaskCmds.some((c) => c.id === 'navUp')).toBe(true);
  });

  it('does not include other area commands', () => {
    const inputCmds = getCommandsForArea('input');
    expect(inputCmds.some((c) => c.id === 'scrollUp')).toBe(false);

    const chatCmds = getCommandsForArea('chat');
    expect(chatCmds.some((c) => c.id === 'indent')).toBe(false);
  });
});

describe('keymap: getStatusBarHelp', () => {
  it('returns minimal help on narrow terminals', () => {
    const help = getStatusBarHelp('input', 60);
    expect(help).toContain('S-Tab');
    expect(help).toContain('quit');
    // Should not include swap or other medium/wide commands
    expect(help).not.toContain('C-W');
  });

  it('returns global shortcuts on medium terminals', () => {
    const help = getStatusBarHelp('input', 90);
    expect(help).toContain('S-Tab');
    expect(help).toContain('C-W');
    expect(help).toContain('C-Q');
  });

  it('includes area-specific commands on wide terminals', () => {
    const chatHelp = getStatusBarHelp('chat', 120);
    const inputHelp = getStatusBarHelp('input', 120);
    // Wide terminal should have more info than medium
    expect(chatHelp.length).toBeGreaterThan(getStatusBarHelp('chat', 90).length);
    // Input area help mentions scrolling on chat area
    expect(chatHelp).toContain('↑');
  });

  it('different areas produce different wide help text', () => {
    const chatHelp = getStatusBarHelp('chat', 120);
    const inputHelp = getStatusBarHelp('input', 120);
    // Chat help includes scroll commands; input help includes insert (indent)
    expect(chatHelp).toContain('scroll');
    expect(inputHelp).toContain('insert');
  });
});
