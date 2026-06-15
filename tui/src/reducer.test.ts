import {describe, it, expect} from 'vitest';
import {tuiReducer, INITIAL_TUI_STATE} from './reducer.js';
import type {TuiAction} from './reducer.js';
import {createSystemMessage, type ChatMessage} from './components/ChatLog.js';
import type {SubtaskItem} from './components/SubtaskTree.js';

// ── Helpers ────────────────────────────────────────────────

function sysMsg(text: string, eventType?: string): ChatMessage {
  return {...createSystemMessage(text), eventType};
}

function subtask(id: string, status: SubtaskItem['status'] = 'pending'): SubtaskItem {
  return {id, index: 1, description: `Subtask ${id}`, status};
}

// ── ADD_MESSAGES ──────────────────────────────────────────

describe('tuiReducer: ADD_MESSAGES', () => {
  it('appends messages to empty state', () => {
    const msgs = [sysMsg('hello')];
    const next = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: msgs});
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].text).toBe('hello');
  });

  it('appends to existing messages', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: [sysMsg('a')]});
    state = tuiReducer(state, {type: 'ADD_MESSAGES', messages: [sysMsg('b')]});
    expect(state.messages).toHaveLength(2);
  });

  it('caps at 2000 messages', () => {
    const msgs = Array.from({length: 2500}, (_, i) => sysMsg(`msg-${i}`));
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: msgs});
    expect(state.messages).toHaveLength(2000);
    expect(state.messages[0].text).toBe('msg-500'); // first 500 dropped
  });
});

// ── SET_SUBTASKS / UPDATE_SUBTASK_STATUS ──────────────────

describe('tuiReducer: subtask actions', () => {
  it('SET_SUBTASKS sets subtasks and derives progress', () => {
    const items = [subtask('a', 'completed'), subtask('b', 'in_progress'), subtask('c', 'pending')];
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: items});
    expect(state.subtasks).toHaveLength(3);
    expect(state.progress).toEqual({completed: 1, total: 3});
  });

  it('UPDATE_SUBTASK_STATUS updates a single subtask', () => {
    const items = [subtask('a', 'pending'), subtask('b', 'pending')];
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: items});
    state = tuiReducer(state, {type: 'UPDATE_SUBTASK_STATUS', subtaskId: 'a', status: 'completed'});
    expect(state.subtasks[0].status).toBe('completed');
    expect(state.subtasks[1].status).toBe('pending');
    expect(state.progress).toEqual({completed: 1, total: 2});
  });

  it('UPDATE_SUBTASK_STATUS on unknown id is no-op', () => {
    const items = [subtask('a', 'pending')];
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: items});
    state = tuiReducer(state, {type: 'UPDATE_SUBTASK_STATUS', subtaskId: 'z', status: 'completed'});
    expect(state.subtasks[0].status).toBe('pending');
  });
});

// ── SCROLL_UP / SCROLL_DOWN ───────────────────────────────

describe('tuiReducer: scroll actions', () => {
  it('SCROLL_UP disables followLog and increments scrollTick', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SCROLL_UP', lines: 5});
    expect(state.followLog).toBe(false);
    expect(state.scrollDirection).toBe('up');
    expect(state.scrollLines).toBe(5);
    expect(state.scrollTick).toBe(1);
  });

  it('SCROLL_DOWN increments scrollTick', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SCROLL_DOWN', lines: 3});
    expect(state.scrollDirection).toBe('down');
    expect(state.scrollLines).toBe(3);
    expect(state.scrollTick).toBe(1);
  });

  it('multiple scrolls increment tick', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SCROLL_UP', lines: 1});
    state = tuiReducer(state, {type: 'SCROLL_DOWN', lines: 1});
    expect(state.scrollTick).toBe(2);
  });
});

// ── ADD_INPUT_HISTORY ─────────────────────────────────────

describe('tuiReducer: ADD_INPUT_HISTORY', () => {
  it('adds entry and resets historyIndex', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_INPUT_HISTORY', text: 'fix bug'});
    expect(state.inputHistory).toEqual(['fix bug']);
    expect(state.historyIndex).toBe(-1);
  });

  it('deduplicates most recent entry', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_INPUT_HISTORY', text: 'fix bug'});
    state = tuiReducer(state, {type: 'ADD_INPUT_HISTORY', text: 'fix bug'});
    expect(state.inputHistory).toEqual(['fix bug']);
  });

  it('caps at 50 entries', () => {
    let state = INITIAL_TUI_STATE;
    for (let i = 0; i < 60; i++) {
      state = tuiReducer(state, {type: 'ADD_INPUT_HISTORY', text: `cmd-${i}`});
    }
    expect(state.inputHistory).toHaveLength(50);
  });
});

// ── SET_EVENT_FILTER ──────────────────────────────────────

describe('tuiReducer: SET_EVENT_FILTER', () => {
  it('changes eventFilter', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_EVENT_FILTER', filter: 'error'});
    expect(state.eventFilter).toBe('error');
  });
});

// ── CLEAR_TASK / CLEAR_LOG ────────────────────────────────

describe('tuiReducer: clear actions', () => {
  it('CLEAR_TASK resets subtasks and activeTaskId', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: [subtask('a')]});
    state = tuiReducer(state, {type: 'SET_ACTIVE_TASK', taskId: 'task-123'});
    state = tuiReducer(state, {type: 'CLEAR_TASK'});
    expect(state.subtasks).toHaveLength(0);
    expect(state.activeTaskId).toBeNull();
    expect(state.progress).toEqual({completed: 0, total: 0});
  });

  it('CLEAR_LOG resets messages and followLog', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: [sysMsg('a')]});
    state = tuiReducer(state, {type: 'CLEAR_LOG'});
    expect(state.messages).toHaveLength(0);
    expect(state.followLog).toBe(true);
  });
});

// ── OFFLINE_TIMER ─────────────────────────────────────────

describe('tuiReducer: offline timer actions', () => {
  it('ADD_OFFLINE_TIMER tracks timer IDs', () => {
    const fakeTimer = {} as ReturnType<typeof setTimeout>;
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_OFFLINE_TIMER', timerId: fakeTimer});
    expect(state.offlineTimerIds).toHaveLength(1);
  });

  it('CLEAR_OFFLINE_TIMERS empties the array', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_OFFLINE_TIMER', timerId: {} as ReturnType<typeof setTimeout>});
    state = tuiReducer(state, {type: 'CLEAR_OFFLINE_TIMERS'});
    expect(state.offlineTimerIds).toHaveLength(0);
  });
});

// ── SET_ACTIVE_TASK / SET_FOLLOW_LOG / SET_SELECTED_PANE ──

describe('tuiReducer: simple setters', () => {
  it('SET_ACTIVE_TASK', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_ACTIVE_TASK', taskId: 'abc'});
    expect(state.activeTaskId).toBe('abc');
  });

  it('SET_FOLLOW_LOG', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_FOLLOW_LOG', follow: false});
    expect(state.followLog).toBe(false);
  });

  it('SET_SELECTED_PANE', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SELECTED_PANE', pane: 'chat'});
    expect(state.selectedPane).toBe('chat');
  });
});
