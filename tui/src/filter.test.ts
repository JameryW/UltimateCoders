import {describe, it, expect} from 'vitest';
import {createSystemMessage, type ChatMessage} from './components/ChatLog.js';

// Replicate filter logic from ChatLog component
type EventFilter = 'all' | 'task' | 'subtask' | 'tool' | 'error';

function filterMessages(messages: ChatMessage[], filter: EventFilter): ChatMessage[] {
  if (filter === 'all') return messages;
  return messages.filter((msg) => {
    if (msg.isUser) return true;
    if (!msg.eventType) return true;
    const et = msg.eventType;
    if (filter === 'task') return et.startsWith('task_');
    if (filter === 'subtask') return et.startsWith('subtask_');
    if (filter === 'tool') return et.startsWith('tool_');
    if (filter === 'error') return et === 'subtask_failed' || et === 'task_failed';
    return true;
  });
}

function msg(isUser: boolean, eventType?: string): ChatMessage {
  return {
    id: `id-${Math.random()}`,
    timestamp: '12:00:00',
    text: isUser ? 'user msg' : 'system msg',
    isUser,
    eventType,
  };
}

describe('ChatLog filter logic', () => {
  it('all filter shows all messages', () => {
    const messages = [
      msg(false, 'task_submitted'),
      msg(false, 'subtask_started'),
      msg(false, 'tool_call'),
      msg(false, 'subtask_failed'),
      msg(true),
    ];
    const filtered = filterMessages(messages, 'all');
    expect(filtered).toHaveLength(5);
  });

  it('task filter shows only task_ events + user messages', () => {
    const messages = [
      msg(false, 'task_submitted'),
      msg(false, 'subtask_started'),
      msg(true),
      msg(false, 'task_completed'),
      msg(false, 'tool_call'),
    ];
    const filtered = filterMessages(messages, 'task');
    expect(filtered).toHaveLength(3); // task_submitted, user msg, task_completed
    expect(filtered.every((m) =>
      m.isUser || !m.eventType || m.eventType!.startsWith('task_'),
    )).toBe(true);
  });

  it('subtask filter shows only subtask_ events + user messages', () => {
    const messages = [
      msg(false, 'task_submitted'),
      msg(false, 'subtask_started'),
      msg(false, 'subtask_completed'),
      msg(true),
    ];
    const filtered = filterMessages(messages, 'subtask');
    expect(filtered).toHaveLength(3); // subtask_started, subtask_completed, user msg
  });

  it('tool filter shows only tool_ events + user messages', () => {
    const messages = [
      msg(false, 'tool_call'),
      msg(false, 'tool_result'),
      msg(false, 'subtask_started'),
      msg(true),
    ];
    const filtered = filterMessages(messages, 'tool');
    expect(filtered).toHaveLength(3); // tool_call, tool_result, user msg
  });

  it('error filter shows only _failed events + user messages', () => {
    const messages = [
      msg(false, 'subtask_failed'),
      msg(false, 'task_failed'),
      msg(false, 'subtask_completed'),
      msg(true),
      msg(false, 'task_submitted'),
    ];
    const filtered = filterMessages(messages, 'error');
    expect(filtered).toHaveLength(3); // subtask_failed, task_failed, user msg
  });

  it('user messages always pass filter', () => {
    const messages = [msg(true), msg(true), msg(true)];
    for (const filter of ['all', 'task', 'subtask', 'tool', 'error'] as EventFilter[]) {
      expect(filterMessages(messages, filter)).toHaveLength(3);
    }
  });

  it('messages without eventType pass all filters', () => {
    const messages = [msg(false)]; // no eventType
    for (const filter of ['all', 'task', 'subtask', 'tool', 'error'] as EventFilter[]) {
      expect(filterMessages(messages, filter)).toHaveLength(1);
    }
  });
});
