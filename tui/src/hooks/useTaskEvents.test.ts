import {describe, it, expect} from 'vitest';
import {processEvent, protoSubtasksToItems} from './useTaskEvents.js';
import type {TaskEventProto, SubtaskProto} from '../grpc/types.js';
import type {SubtaskItem} from '../components/SubtaskTree.js';

// ── Helpers ──────────────────────────────────────────────────

function makeSubtaskItem(overrides: Partial<SubtaskItem> = {}): SubtaskItem {
  return {
    id: 'sub-1',
    index: 1,
    description: 'Test subtask',
    status: 'pending',
    assignedWorker: undefined,
    dependsOn: [],
    ...overrides,
  };
}

function makeEvent(overrides: Partial<TaskEventProto> = {}): TaskEventProto {
  return {
    timestamp: new Date().toISOString(),
    type: 'subtask_assigned',
    taskId: 'task-1',
    subtaskId: 'sub-1',
    data: {},
    ...overrides,
  };
}

// ── processEvent ─────────────────────────────────────────────

describe('processEvent', () => {
  it('updates subtask to "assigned" on subtask_assigned', () => {
    const map = new Map([['sub-1', makeSubtaskItem()]]);
    const event = makeEvent({
      type: 'subtask_assigned',
      subtaskId: 'sub-1',
      data: {worker_id: 'worker-1'},
    });
    const result = processEvent(event, map);
    expect(result.get('sub-1')?.status).toBe('assigned');
    expect(result.get('sub-1')?.assignedWorker).toBe('worker-1');
  });

  it('updates subtask to "in_progress" on subtask_started', () => {
    const map = new Map([['sub-1', makeSubtaskItem({status: 'assigned', assignedWorker: 'worker-1'})]]);
    const event = makeEvent({
      type: 'subtask_started',
      subtaskId: 'sub-1',
      data: {worker_id: 'worker-2'},
    });
    const result = processEvent(event, map);
    expect(result.get('sub-1')?.status).toBe('in_progress');
    // Updates worker_id from event data
    expect(result.get('sub-1')?.assignedWorker).toBe('worker-2');
  });

  it('keeps existing worker_id on subtask_started if event has no worker_id', () => {
    const map = new Map([['sub-1', makeSubtaskItem({status: 'assigned', assignedWorker: 'worker-1'})]]);
    const event = makeEvent({
      type: 'subtask_started',
      subtaskId: 'sub-1',
      data: {},
    });
    const result = processEvent(event, map);
    expect(result.get('sub-1')?.assignedWorker).toBe('worker-1');
  });

  it('updates subtask to "completed" on subtask_completed', () => {
    const map = new Map([['sub-1', makeSubtaskItem({status: 'in_progress'})]]);
    const event = makeEvent({
      type: 'subtask_completed',
      subtaskId: 'sub-1',
    });
    const result = processEvent(event, map);
    expect(result.get('sub-1')?.status).toBe('completed');
  });

  it('updates subtask to "failed" on subtask_failed with error summary', () => {
    const map = new Map([['sub-1', makeSubtaskItem({status: 'in_progress'})]]);
    const event = makeEvent({
      type: 'subtask_failed',
      subtaskId: 'sub-1',
      data: {error_summary: 'Build failed'},
    });
    const result = processEvent(event, map);
    expect(result.get('sub-1')?.status).toBe('failed');
    expect(result.get('sub-1')?.errorSummary).toBe('Build failed');
  });

  it('falls back to error field if error_summary is absent', () => {
    const map = new Map([['sub-1', makeSubtaskItem({status: 'in_progress'})]]);
    const event = makeEvent({
      type: 'subtask_failed',
      subtaskId: 'sub-1',
      data: {error: 'Timeout'},
    });
    const result = processEvent(event, map);
    expect(result.get('sub-1')?.errorSummary).toBe('Timeout');
  });

  it('does not modify map on task_submitted event', () => {
    const original = makeSubtaskItem();
    const map = new Map([['sub-1', original]]);
    const event = makeEvent({type: 'task_submitted', subtaskId: undefined});
    const result = processEvent(event, map);
    expect(result.get('sub-1')).toEqual(original);
  });

  it('does not modify map for unknown event type', () => {
    const original = makeSubtaskItem();
    const map = new Map([['sub-1', original]]);
    const event = makeEvent({type: 'tool_call'});
    const result = processEvent(event, map);
    expect(result.get('sub-1')).toEqual(original);
  });

  it('handles event with subtaskId not in map gracefully', () => {
    const map = new Map<string, SubtaskItem>();
    const event = makeEvent({
      type: 'subtask_assigned',
      subtaskId: 'sub-999',
      data: {worker_id: 'worker-1'},
    });
    const result = processEvent(event, map);
    // No crash, map unchanged
    expect(result.size).toBe(0);
  });

  it('does not mutate the original map (immutability)', () => {
    const original = makeSubtaskItem();
    const map = new Map([['sub-1', original]]);
    const event = makeEvent({
      type: 'subtask_completed',
      subtaskId: 'sub-1',
    });
    const result = processEvent(event, map);
    // Original map should not be modified
    expect(map.get('sub-1')?.status).toBe('pending');
    expect(result.get('sub-1')?.status).toBe('completed');
    expect(result).not.toBe(map);
  });
});

// ── protoSubtasksToItems ─────────────────────────────────────

describe('protoSubtasksToItems', () => {
  it('converts a single subtask with 1-based index', () => {
    const protos: SubtaskProto[] = [{
      id: 'sub-1',
      description: 'Fix bug',
      status: 'Pending',
      dependsOn: [],
    }];
    const result = protoSubtasksToItems(protos);
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({
      id: 'sub-1',
      index: 1,
      description: 'Fix bug',
      status: 'pending',
      assignedWorker: undefined,
      dependsOn: [],
    });
  });

  it('converts multiple subtasks with incrementing indices', () => {
    const protos: SubtaskProto[] = [
      {id: 'sub-1', description: 'A', status: 'Pending', dependsOn: []},
      {id: 'sub-2', description: 'B', status: 'InProgress', dependsOn: ['sub-1']},
    ];
    const result = protoSubtasksToItems(protos);
    expect(result[0].index).toBe(1);
    expect(result[1].index).toBe(2);
  });

  it('maps status via mapSubtaskStatus', () => {
    const protos: SubtaskProto[] = [{
      id: 'sub-1',
      description: 'Test',
      status: 'Completed',
      dependsOn: [],
    }];
    const result = protoSubtasksToItems(protos);
    expect(result[0].status).toBe('completed');
  });

  it('preserves assignedWorker', () => {
    const protos: SubtaskProto[] = [{
      id: 'sub-1',
      description: 'Test',
      status: 'Assigned',
      dependsOn: [],
      assignedWorker: 'worker-1',
    }];
    const result = protoSubtasksToItems(protos);
    expect(result[0].assignedWorker).toBe('worker-1');
  });

  it('preserves dependsOn array', () => {
    const protos: SubtaskProto[] = [{
      id: 'sub-2',
      description: 'B',
      status: 'Pending',
      dependsOn: ['sub-1'],
    }];
    const result = protoSubtasksToItems(protos);
    expect(result[0].dependsOn).toEqual(['sub-1']);
  });

  it('returns empty array for empty input', () => {
    expect(protoSubtasksToItems([])).toEqual([]);
  });
});
