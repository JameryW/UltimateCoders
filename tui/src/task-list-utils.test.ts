import {describe, it, expect} from 'vitest';
import {buildTaskListText, formatTaskList, formatRelativeTime} from './task-list-utils.js';
import type {TaskProto} from './grpc/types.js';

describe('task-list-utils', () => {
  describe('formatRelativeTime', () => {
    it('returns "just now" for recent timestamps', () => {
      expect(formatRelativeTime(Date.now() - 30_000)).toBe('just now');
    });

    it('returns minutes ago', () => {
      expect(formatRelativeTime(Date.now() - 120_000)).toBe('2m ago');
    });

    it('returns hours ago', () => {
      expect(formatRelativeTime(Date.now() - 7_200_000)).toBe('2h ago');
    });

    it('returns days ago', () => {
      expect(formatRelativeTime(Date.now() - 172_800_000)).toBe('2d ago');
    });

    it('returns "now" for future timestamps', () => {
      expect(formatRelativeTime(Date.now() + 1000)).toBe('now');
    });
  });

  describe('formatTaskList', () => {
    it('formats empty list', () => {
      expect(formatTaskList([])).toEqual([]);
    });

    it('formats tasks with status icons', () => {
      const tasks: TaskProto[] = [{
        id: 'abc12345-6789-def0',
        description: 'fix login bug',
        status: 'Completed',
        projectId: 'default',
        subtaskCount: 3,
        createdAt: Date.now() - 120_000,
        updatedAt: Date.now() - 60_000,
        subtasks: [],
      }];
      const rows = formatTaskList(tasks);
      expect(rows).toHaveLength(1);
      expect(rows[0].shortId).toBe('abc12345');
      expect(rows[0].statusIcon).toBe('●');
      expect(rows[0].statusColor).toBe('green');
    });

    it('truncates long descriptions', () => {
      const tasks: TaskProto[] = [{
        id: 'test-id',
        description: 'a'.repeat(100),
        status: 'InProgress',
        projectId: 'default',
        subtaskCount: 0,
        createdAt: Date.now(),
        updatedAt: Date.now(),
        subtasks: [],
      }];
      const rows = formatTaskList(tasks);
      expect(rows[0].description.length).toBeLessThanOrEqual(43); // 40 + '...'
    });
  });

  describe('buildTaskListText', () => {
    it('shows "No tasks found" for empty list', () => {
      expect(buildTaskListText([])).toEqual(['No tasks found.']);
    });

    it('includes header with count', () => {
      const result = buildTaskListText([{
        id: 'test',
        description: 'test task',
        status: 'InProgress',
        projectId: 'default',
        subtaskCount: 0,
        createdAt: Date.now(),
        updatedAt: Date.now(),
        subtasks: [],
      }]);
      expect(result[0]).toContain('Tasks (1 total)');
    });
  });
});
