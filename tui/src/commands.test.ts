import {describe, it, expect} from 'vitest';
import {parseCommand, matchCommands, formatHelpText, isCommandInput, COMMANDS} from './commands.js';

describe('commands', () => {
  describe('parseCommand', () => {
    it('returns null for non-command input', () => {
      expect(parseCommand('hello world')).toBeNull();
      expect(parseCommand('')).toBeNull();
    });

    it('parses /help', () => {
      const result = parseCommand('/help');
      expect(result).not.toBeNull();
      expect(result!.command.name).toBe('help');
      expect(result!.args).toBe('');
    });

    it('parses /tasks', () => {
      const result = parseCommand('/tasks');
      expect(result).not.toBeNull();
      expect(result!.command.name).toBe('tasks');
    });

    it('parses /task with args', () => {
      const result = parseCommand('/task abc123');
      expect(result).not.toBeNull();
      expect(result!.command.name).toBe('task');
      expect(result!.command.hasArgs).toBe(true);
      expect(result!.args).toBe('abc123');
    });

    it('parses /clear', () => {
      expect(parseCommand('/clear')!.command.name).toBe('clear');
    });

    it('parses /status', () => {
      expect(parseCommand('/status')!.command.name).toBe('status');
    });

    it('parses /reconnect', () => {
      expect(parseCommand('/reconnect')!.command.name).toBe('reconnect');
    });

    it('parses /quit', () => {
      expect(parseCommand('/quit')!.command.name).toBe('quit');
    });

    it('returns null for unknown command', () => {
      expect(parseCommand('/unknown')).toBeNull();
    });

    it('is case-insensitive', () => {
      expect(parseCommand('/HELP')!.command.name).toBe('help');
      expect(parseCommand('/Tasks')!.command.name).toBe('tasks');
    });

    it('trims whitespace', () => {
      expect(parseCommand('  /help  ')!.command.name).toBe('help');
    });
  });

  describe('matchCommands', () => {
    it('returns all commands for empty prefix', () => {
      expect(matchCommands('')).toHaveLength(COMMANDS.length);
    });

    it('matches "h" to help', () => {
      const matches = matchCommands('h');
      expect(matches).toHaveLength(1);
      expect(matches[0].name).toBe('help');
    });

    it('matches "t" to tasks and task', () => {
      const matches = matchCommands('t');
      expect(matches.length).toBeGreaterThanOrEqual(2);
      const names = matches.map((c) => c.name);
      expect(names).toContain('tasks');
      expect(names).toContain('task');
    });

    it('matches "qu" to quit', () => {
      const matches = matchCommands('qu');
      expect(matches).toHaveLength(1);
      expect(matches[0].name).toBe('quit');
    });
  });

  describe('formatHelpText', () => {
    it('includes all command names', () => {
      const text = formatHelpText();
      for (const cmd of COMMANDS) {
        expect(text).toContain(cmd.usage);
      }
    });
  });

  describe('isCommandInput', () => {
    it('detects slash prefix', () => {
      expect(isCommandInput('/help')).toBe(true);
      expect(isCommandInput('  /help')).toBe(true);
    });

    it('rejects non-slash input', () => {
      expect(isCommandInput('hello')).toBe(false);
      expect(isCommandInput('')).toBe(false);
    });
  });
});
