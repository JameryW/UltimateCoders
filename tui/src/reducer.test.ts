import {describe, it, expect} from 'vitest';
import {
  tuiReducer,
  INITIAL_TUI_STATE,
  nextFocusArea,
  type TuiAction,
  type FocusedArea,
} from './reducer.js';
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

  it('increments unreadCount when followLog is off', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_FOLLOW_LOG', follow: false});
    state = tuiReducer(state, {type: 'ADD_MESSAGES', messages: [sysMsg('a'), sysMsg('b')]});
    expect(state.unreadCount).toBe(2);
  });

  it('keeps unreadCount at 0 when followLog is on', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: [sysMsg('a')]});
    expect(state.unreadCount).toBe(0);
  });
});

// ── UPDATE_MESSAGE ────────────────────────────────────────

describe('tuiReducer: UPDATE_MESSAGE', () => {
  it('updates a message by id', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: [sysMsg('original')]});
    const messageId = state.messages[0].id;
    state = tuiReducer(state, {type: 'UPDATE_MESSAGE', messageId, text: 'updated'});
    expect(state.messages[0].text).toBe('updated');
  });

  it('leaves other messages unchanged', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: [sysMsg('a'), sysMsg('b')]});
    const messageId = state.messages[0].id;
    state = tuiReducer(state, {type: 'UPDATE_MESSAGE', messageId, text: 'updated'});
    expect(state.messages[0].text).toBe('updated');
    expect(state.messages[1].text).toBe('b');
  });

  it('is no-op for unknown messageId', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: [sysMsg('a')]});
    state = tuiReducer(state, {type: 'UPDATE_MESSAGE', messageId: 'nonexistent', text: 'x'});
    expect(state.messages[0].text).toBe('a');
  });
});

// ── REMOVE_MESSAGE ────────────────────────────────────────

describe('tuiReducer: REMOVE_MESSAGE', () => {
  it('removes a message by id', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: [sysMsg('a'), sysMsg('b')]});
    const messageId = state.messages[0].id;
    state = tuiReducer(state, {type: 'REMOVE_MESSAGE', messageId});
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].text).toBe('b');
  });

  it('is no-op for unknown messageId', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: [sysMsg('a')]});
    state = tuiReducer(state, {type: 'REMOVE_MESSAGE', messageId: 'nonexistent'});
    expect(state.messages).toHaveLength(1);
  });

  it('removes only the matching message', () => {
    const msgs = [sysMsg('a'), sysMsg('b'), sysMsg('c')];
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: msgs});
    const messageId = state.messages[1].id;
    state = tuiReducer(state, {type: 'REMOVE_MESSAGE', messageId});
    expect(state.messages).toHaveLength(2);
    expect(state.messages.map((m) => m.text)).toEqual(['a', 'c']);
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

  it('SET_SUBTASKS resets subtask selection', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: [subtask('a'), subtask('b')]});
    state = tuiReducer(state, {type: 'SELECT_SUBTASK', index: 0});
    expect(state.selectedSubtaskIndex).toBe(0);
    state = tuiReducer(state, {type: 'SET_SUBTASKS', subtasks: [subtask('c')]});
    expect(state.selectedSubtaskIndex).toBe(-1);
    expect(state.selectedSubtaskId).toBeNull();
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

  it('resets unreadCount and re-enables followLog', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_FOLLOW_LOG', follow: false});
    state = tuiReducer(state, {type: 'ADD_MESSAGES', messages: [sysMsg('a')]});
    expect(state.unreadCount).toBe(1);
    state = tuiReducer(state, {type: 'SET_EVENT_FILTER', filter: 'task'});
    expect(state.unreadCount).toBe(0);
    expect(state.followLog).toBe(true);
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

  it('CLEAR_TASK resets subtask selection and closes overlay', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: [subtask('a')]});
    state = tuiReducer(state, {type: 'SELECT_SUBTASK', index: 0});
    state = tuiReducer(state, {type: 'TOGGLE_SUBTASK_OVERLAY'});
    state = tuiReducer(state, {type: 'CLEAR_TASK'});
    expect(state.selectedSubtaskIndex).toBe(-1);
    expect(state.selectedSubtaskId).toBeNull();
    expect(state.subtaskOverlayOpen).toBe(false);
  });

  it('CLEAR_LOG resets messages, followLog, and unreadCount', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'ADD_MESSAGES', messages: [sysMsg('a')]});
    state = tuiReducer(state, {type: 'SET_FOLLOW_LOG', follow: false});
    state = tuiReducer(state, {type: 'ADD_MESSAGES', messages: [sysMsg('b')]});
    state = tuiReducer(state, {type: 'CLEAR_LOG'});
    expect(state.messages).toHaveLength(0);
    expect(state.followLog).toBe(true);
    expect(state.unreadCount).toBe(0);
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

// ── Focus & Layout (v3 — single-column) ──────────────────

describe('tuiReducer: focus and layout actions', () => {
  it('initial state has focusedArea=input, activeMainPane=chat', () => {
    expect(INITIAL_TUI_STATE.focusedArea).toBe('input');
    expect(INITIAL_TUI_STATE.activeMainPane).toBe('chat');
  });

  it('SET_FOCUS changes focusedArea and selectedPane', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_FOCUS', area: 'chat'});
    expect(state.focusedArea).toBe('chat');
    expect(state.selectedPane).toBe('chat');
  });

  it('CYCLE_FOCUS cycles through input→chat→input', () => {
    let state = INITIAL_TUI_STATE;
    state = tuiReducer(state, {type: 'CYCLE_FOCUS'});
    expect(state.focusedArea).toBe('chat');
    state = tuiReducer(state, {type: 'CYCLE_FOCUS'});
    expect(state.focusedArea).toBe('input');
  });

  it('SWAP_MAIN_PANE is a deprecated no-op', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SWAP_MAIN_PANE'});
    expect(state).toEqual(INITIAL_TUI_STATE);
  });

  it('ESC_TO_MAIN from input focuses chat', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'ESC_TO_MAIN'});
    expect(state.focusedArea).toBe('chat');
  });

  it('ESC_TO_MAIN from chat focuses input', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_FOCUS', area: 'chat'});
    state = tuiReducer(state, {type: 'ESC_TO_MAIN'});
    expect(state.focusedArea).toBe('input');
  });

  it('ESC_TO_MAIN with subtaskOverlayOpen closes overlay', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_FOCUS', area: 'chat'});
    state = tuiReducer(state, {type: 'TOGGLE_SUBTASK_OVERLAY'});
    expect(state.subtaskOverlayOpen).toBe(true);
    state = tuiReducer(state, {type: 'ESC_TO_MAIN'});
    expect(state.subtaskOverlayOpen).toBe(false);
  });

  it('SET_SELECTED_PANE (deprecated) maps to SET_FOCUS', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SELECTED_PANE', pane: 'chat'});
    expect(state.focusedArea).toBe('chat');
    expect(state.selectedPane).toBe('chat');
  });
});

// ── Unread Count ──────────────────────────────────────────

describe('tuiReducer: unread count actions', () => {
  it('INCREMENT_UNREAD adds to count', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'INCREMENT_UNREAD', count: 3});
    expect(state.unreadCount).toBe(3);
  });

  it('RESET_UNREAD sets count to 0', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'INCREMENT_UNREAD', count: 5});
    state = tuiReducer(state, {type: 'RESET_UNREAD'});
    expect(state.unreadCount).toBe(0);
  });

  it('SET_FOLLOW_LOG true resets unreadCount', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'INCREMENT_UNREAD', count: 5});
    state = tuiReducer(state, {type: 'SET_FOLLOW_LOG', follow: true});
    expect(state.unreadCount).toBe(0);
  });

  it('SET_FOLLOW_LOG false preserves unreadCount', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'INCREMENT_UNREAD', count: 5});
    state = tuiReducer(state, {type: 'SET_FOLLOW_LOG', follow: false});
    expect(state.unreadCount).toBe(5);
  });
});

// ── Subtask Navigation ────────────────────────────────────

describe('tuiReducer: subtask navigation actions', () => {
  it('SELECT_SUBTASK sets index and id', () => {
    const items = [subtask('a'), subtask('b'), subtask('c')];
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: items});
    state = tuiReducer(state, {type: 'SELECT_SUBTASK', index: 1});
    expect(state.selectedSubtaskIndex).toBe(1);
    expect(state.selectedSubtaskId).toBe('b');
  });

  it('SELECT_SUBTASK with invalid index clears selection', () => {
    const items = [subtask('a')];
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: items});
    state = tuiReducer(state, {type: 'SELECT_SUBTASK', index: 5});
    expect(state.selectedSubtaskIndex).toBe(-1);
    expect(state.selectedSubtaskId).toBeNull();
  });

  it('SELECT_SUBTASK with negative index clears selection', () => {
    const items = [subtask('a')];
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: items});
    state = tuiReducer(state, {type: 'SELECT_SUBTASK', index: -1});
    expect(state.selectedSubtaskIndex).toBe(-1);
    expect(state.selectedSubtaskId).toBeNull();
  });

  it('TOGGLE_SUBTASK_OVERLAY toggles overlay open/closed', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'TOGGLE_SUBTASK_OVERLAY'});
    expect(state.subtaskOverlayOpen).toBe(true);
    state = tuiReducer(state, {type: 'TOGGLE_SUBTASK_OVERLAY'});
    expect(state.subtaskOverlayOpen).toBe(false);
  });

  it('TOGGLE_SUBTASK_OVERLAY works independently of subtask selection', () => {
    // Overlay can be opened even without selecting a subtask
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'TOGGLE_SUBTASK_OVERLAY'});
    expect(state.subtaskOverlayOpen).toBe(true);
  });

  it('JUMP_TO_FAILED_SUBTASK selects next failed after current', () => {
    const items = [subtask('a', 'completed'), subtask('b', 'failed'), subtask('c', 'completed'), subtask('d', 'failed')];
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: items});
    // Start from index 0, next failed should be index 1
    state = tuiReducer(state, {type: 'SELECT_SUBTASK', index: 0});
    state = tuiReducer(state, {type: 'JUMP_TO_FAILED_SUBTASK'});
    expect(state.selectedSubtaskIndex).toBe(1);
    expect(state.selectedSubtaskId).toBe('b');
    // From index 1, next failed after 1 should be index 3
    state = tuiReducer(state, {type: 'JUMP_TO_FAILED_SUBTASK'});
    expect(state.selectedSubtaskIndex).toBe(3);
    expect(state.selectedSubtaskId).toBe('d');
    // From index 3, wraps back to first failed (index 1)
    state = tuiReducer(state, {type: 'JUMP_TO_FAILED_SUBTASK'});
    expect(state.selectedSubtaskIndex).toBe(1);
  });

  it('JUMP_TO_FAILED_SUBTASK is no-op when no failed subtasks', () => {
    const items = [subtask('a', 'completed'), subtask('b', 'pending')];
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: items});
    state = tuiReducer(state, {type: 'SELECT_SUBTASK', index: 0});
    state = tuiReducer(state, {type: 'JUMP_TO_FAILED_SUBTASK'});
    expect(state.selectedSubtaskIndex).toBe(0); // unchanged
  });

  it('JUMP_TO_FAILED_SUBTASK is no-op with empty subtasks array', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: []});
    const next = tuiReducer(state, {type: 'JUMP_TO_FAILED_SUBTASK'});
    expect(next.selectedSubtaskIndex).toBe(-1); // default, unchanged
  });

  it('JUMP_TO_FAILED_SUBTASK from default selectedSubtaskIndex=-1 finds first failed', () => {
    const items = [subtask('a', 'completed'), subtask('b', 'failed'), subtask('c', 'pending')];
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBTASKS', subtasks: items});
    // selectedSubtaskIndex is -1 by default, startIdx = -1 + 1 = 0
    const next = tuiReducer(state, {type: 'JUMP_TO_FAILED_SUBTASK'});
    expect(next.selectedSubtaskIndex).toBe(1);
    expect(next.selectedSubtaskId).toBe('b');
  });
});

// ── SET_SUBMITTING ────────────────────────────────────────

describe('tuiReducer: SET_SUBMITTING', () => {
  it('sets isSubmitting to true', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBMITTING', submitting: true});
    expect(state.isSubmitting).toBe(true);
  });

  it('sets isSubmitting to false', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBMITTING', submitting: true});
    state = tuiReducer(state, {type: 'SET_SUBMITTING', submitting: false});
    expect(state.isSubmitting).toBe(false);
  });

  it('sets startedAt when submitting=true, clears when submitting=false', () => {
    const state1 = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_SUBMITTING', submitting: true});
    expect(state1.startedAt).toBeTypeOf('number');
    expect(state1.startedAt).toBeGreaterThan(0);
    const state2 = tuiReducer(state1, {type: 'SET_SUBMITTING', submitting: false});
    expect(state2.startedAt).toBeNull();
  });
});

// ── TOGGLE_HELP_OVERLAY ──────────────────────────────────

describe('tuiReducer: TOGGLE_HELP_OVERLAY', () => {
  it('toggles helpOverlayOpen from false to true', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'TOGGLE_HELP_OVERLAY'});
    expect(state.helpOverlayOpen).toBe(true);
  });

  it('toggles helpOverlayOpen from true to false', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'TOGGLE_HELP_OVERLAY'});
    state = tuiReducer(state, {type: 'TOGGLE_HELP_OVERLAY'});
    expect(state.helpOverlayOpen).toBe(false);
  });

  it('initial state has helpOverlayOpen=false', () => {
    expect(INITIAL_TUI_STATE.helpOverlayOpen).toBe(false);
  });
});

// ── TOGGLE_EXPAND_ALL_MESSAGES ───────────────────────────

describe('tuiReducer: TOGGLE_EXPAND_ALL_MESSAGES', () => {
  it('toggles expandAllMessages from false to true', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'TOGGLE_EXPAND_ALL_MESSAGES'});
    expect(state.expandAllMessages).toBe(true);
  });

  it('toggles expandAllMessages from true to false', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'TOGGLE_EXPAND_ALL_MESSAGES'});
    state = tuiReducer(state, {type: 'TOGGLE_EXPAND_ALL_MESSAGES'});
    expect(state.expandAllMessages).toBe(false);
  });

  it('initial state has expandAllMessages=false', () => {
    expect(INITIAL_TUI_STATE.expandAllMessages).toBe(false);
  });
});

// ── RETRY_SUBTASK (placeholder) ───────────────────────────

describe('tuiReducer: RETRY_SUBTASK', () => {
  it('is a no-op (placeholder)', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'RETRY_SUBTASK', subtaskId: 'a'});
    // State unchanged (placeholder action)
    expect(state).toEqual(INITIAL_TUI_STATE);
  });
});

// ── Focus helper functions ────────────────────────────────

describe('focus helper functions', () => {
  it('nextFocusArea cycles input→chat→input', () => {
    expect(nextFocusArea('input')).toBe('chat');
    expect(nextFocusArea('chat')).toBe('input');
  });
});

// ── SET_ACTIVE_TASK / SET_FOLLOW_LOG ──────────────────────

describe('tuiReducer: simple setters', () => {
  it('SET_ACTIVE_TASK', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_ACTIVE_TASK', taskId: 'abc'});
    expect(state.activeTaskId).toBe('abc');
  });

  it('SET_FOLLOW_LOG', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_FOLLOW_LOG', follow: false});
    expect(state.followLog).toBe(false);
  });
});

// ── SET_HISTORY_INDEX ─────────────────────────────────────

describe('tuiReducer: SET_HISTORY_INDEX', () => {
  it('sets historyIndex to given value', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_HISTORY_INDEX', index: 2});
    expect(state.historyIndex).toBe(2);
  });

  it('sets historyIndex to -1 to exit history browsing', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_HISTORY_INDEX', index: 0});
    state = tuiReducer(state, {type: 'SET_HISTORY_INDEX', index: -1});
    expect(state.historyIndex).toBe(-1);
  });
});

// ── SET_LAST_ERROR ────────────────────────────────────────

describe('tuiReducer: SET_LAST_ERROR', () => {
  it('sets lastError to an error string', () => {
    const state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_LAST_ERROR', error: 'Connection refused'});
    expect(state.lastError).toBe('Connection refused');
  });

  it('clears lastError with null', () => {
    let state = tuiReducer(INITIAL_TUI_STATE, {type: 'SET_LAST_ERROR', error: 'some error'});
    state = tuiReducer(state, {type: 'SET_LAST_ERROR', error: null});
    expect(state.lastError).toBeNull();
  });

  it('initial state has lastError=null', () => {
    expect(INITIAL_TUI_STATE.lastError).toBeNull();
  });
});
