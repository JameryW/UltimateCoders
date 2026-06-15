/**
 * TUI Reducer — single source of truth for all TUI state.
 *
 * Replaces the scattered useState + render-side-effects in App.tsx.
 * Every state transition goes through dispatch(action).
 * Render path contains zero setState calls.
 *
 * Focus model (v2):
 *   focusedArea: which area receives keyboard events (input | chat | subtask)
 *   activeMainPane: which pane occupies the main area in narrow mode (chat | subtask)
 *   These are independent — input is always visible, and the main area always shows
 *   content regardless of which area has focus.
 *
 * Scroll offset is NOT stored here — it is managed locally by ChatLog
 * because the offset must be relative to the filtered message list,
 * which the reducer cannot compute. Instead, the reducer tracks:
 * - followLog: whether auto-follow is active
 * - scrollTick: monotonically increasing counter for scroll commands
 * The ChatLog component reads scrollTick and applies the scroll direction
 * to its own local offset.
 *
 * Unread count: when followLog is false, new messages increment unreadCount
 * instead of forcing the view to scroll. When followLog re-enables (user
 * scrolls to bottom or presses End), unreadCount resets to 0.
 */
import type {ChatMessage} from './components/ChatLog.js';
import type {SubtaskItem, SubtaskStatusType} from './components/SubtaskTree.js';
import type {SymbolMode} from './symbols.js';

// ── Focus & Layout Types ────────────────────────────────────

/** Which area receives keyboard events. */
export type FocusedArea = 'input' | 'chat' | 'subtask';

/** Which pane occupies the main area in narrow (<80 cols) mode. */
export type ActiveMainPane = 'chat' | 'subtask';

/** Order for Shift+Tab focus cycling. */
const FOCUS_CYCLE: FocusedArea[] = ['input', 'chat', 'subtask'];

/** Cycle focus area forward (Shift+Tab). */
export function nextFocusArea(current: FocusedArea): FocusedArea {
  const idx = FOCUS_CYCLE.indexOf(current);
  return FOCUS_CYCLE[(idx + 1) % FOCUS_CYCLE.length];
}

/** Cycle focus area backward (for completeness). */
export function prevFocusArea(current: FocusedArea): FocusedArea {
  const idx = FOCUS_CYCLE.indexOf(current);
  return FOCUS_CYCLE[(idx - 1 + FOCUS_CYCLE.length) % FOCUS_CYCLE.length];
}

/** Swap activeMainPane between chat and subtask. */
export function swapMainPane(current: ActiveMainPane): ActiveMainPane {
  return current === 'chat' ? 'subtask' : 'chat';
}

// ── Event Filter ────────────────────────────────────────────

export type EventFilter = 'all' | 'task' | 'subtask' | 'tool' | 'error';

/** Cycle through filter modes. */
export function nextEventFilter(current: EventFilter): EventFilter {
  const order: EventFilter[] = ['all', 'task', 'subtask', 'tool', 'error'];
  const idx = order.indexOf(current);
  return order[(idx + 1) % order.length];
}

/** Get display label for filter mode. */
export function eventFilterLabel(filter: EventFilter): string {
  switch (filter) {
    case 'all': return 'All';
    case 'task': return 'Task';
    case 'subtask': return 'Subtask';
    case 'tool': return 'Tool';
    case 'error': return 'Error';
  }
}

// ── Backward Compatibility ──────────────────────────────────

/** @deprecated Use FocusedArea instead. Kept for gradual migration. */
export type SelectedPane = FocusedArea;

// ── State ───────────────────────────────────────────────────

export interface TuiState {
  /** Chat log messages. */
  messages: ChatMessage[];

  /** Subtask items (single source of truth — no separate local copy). */
  subtasks: SubtaskItem[];

  /** Progress derived from subtasks. */
  progress: {completed: number; total: number};

  /** Currently active task ID (null if no task submitted). */
  activeTaskId: string | null;

  /** Whether ChatLog auto-follows the bottom. */
  followLog: boolean;

  /** Which area receives keyboard events. */
  focusedArea: FocusedArea;

  /** Which pane occupies the main area in narrow mode. */
  activeMainPane: ActiveMainPane;

  /** Monotonically increasing tick for scroll commands.
   *  ChatLog reads this to detect new scroll events. */
  scrollDirection: 'up' | 'down' | null;
  scrollLines: number;
  scrollTick: number;

  /** Submitted task descriptions for Up/Down history. */
  inputHistory: string[];

  /** Index into inputHistory for browsing (-1 = not browsing). */
  historyIndex: number;

  /** Last gRPC/stream error message. */
  lastError: string | null;

  /** Offline simulation timer IDs (for cleanup). */
  offlineTimerIds: ReturnType<typeof setTimeout>[];

  /** Event type filter for ChatLog. */
  eventFilter: EventFilter;

  /** Symbol rendering mode (unicode/ascii/auto). */
  symbolMode: SymbolMode;

  /** Unread message count when followLog is off.
   *  Reset to 0 when followLog re-enables. */
  unreadCount: number;

  /** Whether a task submission is in progress (prevents duplicate submits). */
  isSubmitting: boolean;

  /** Currently selected subtask index for keyboard navigation (-1 = none). */
  selectedSubtaskIndex: number;

  /** Currently selected subtask ID (null = none, synced with index). */
  selectedSubtaskId: string | null;

  /** Whether subtask detail panel is open. */
  subtaskDetailOpen: boolean;

  /** @deprecated Use focusedArea instead. Kept for gradual migration. */
  selectedPane: FocusedArea;
}

export const INITIAL_TUI_STATE: TuiState = {
  messages: [],
  subtasks: [],
  progress: {completed: 0, total: 0},
  activeTaskId: null,
  followLog: true,
  focusedArea: 'input',
  activeMainPane: 'chat',
  scrollDirection: null,
  scrollLines: 0,
  scrollTick: 0,
  inputHistory: [],
  historyIndex: -1,
  lastError: null,
  offlineTimerIds: [],
  eventFilter: 'all',
  symbolMode: 'auto',
  unreadCount: 0,
  isSubmitting: false,
  selectedSubtaskIndex: -1,
  selectedSubtaskId: null,
  subtaskDetailOpen: false,
  // Backward compat: selectedPane mirrors focusedArea
  selectedPane: 'input',
};

// ── Actions ─────────────────────────────────────────────────

export type TuiAction =
  | {type: 'ADD_MESSAGES'; messages: ChatMessage[]}
  | {type: 'SET_SUBTASKS'; subtasks: SubtaskItem[]}
  | {type: 'UPDATE_SUBTASK_STATUS'; subtaskId: string; status: SubtaskStatusType}
  | {type: 'SET_ACTIVE_TASK'; taskId: string | null}
  | {type: 'SET_FOLLOW_LOG'; follow: boolean}
  // ── Focus & layout (v2) ──
  | {type: 'SET_FOCUS'; area: FocusedArea}
  | {type: 'CYCLE_FOCUS'}
  | {type: 'SET_ACTIVE_MAIN_PANE'; pane: ActiveMainPane}
  | {type: 'SWAP_MAIN_PANE'}
  | {type: 'ESC_TO_MAIN'} // Esc: from input, focus returns to last main pane
  // ── Deprecated (maps to SET_FOCUS) ──
  | {type: 'SET_SELECTED_PANE'; pane: SelectedPane}
  // ── Scroll ──
  | {type: 'SCROLL_UP'; lines: number}
  | {type: 'SCROLL_DOWN'; lines: number}
  // ── Input ──
  | {type: 'ADD_INPUT_HISTORY'; text: string}
  | {type: 'SET_HISTORY_INDEX'; index: number}
  | {type: 'SET_SUBMITTING'; submitting: boolean}
  // ── Error ──
  | {type: 'SET_LAST_ERROR'; error: string | null}
  // ── Clear ──
  | {type: 'CLEAR_TASK'}
  | {type: 'CLEAR_LOG'}
  // ── Offline timers ──
  | {type: 'ADD_OFFLINE_TIMER'; timerId: ReturnType<typeof setTimeout>}
  | {type: 'CLEAR_OFFLINE_TIMERS'}
  // ── Filter ──
  | {type: 'SET_EVENT_FILTER'; filter: EventFilter}
  // ── Unread ──
  | {type: 'INCREMENT_UNREAD'; count: number}
  | {type: 'RESET_UNREAD'}
  // ── Subtask navigation ──
  | {type: 'SELECT_SUBTASK'; index: number}
  | {type: 'TOGGLE_SUBTASK_DETAIL'}
  | {type: 'CLOSE_SUBTASK_DETAIL'}
  // ── Subtask retry (placeholder) ──
  | {type: 'RETRY_SUBTASK'; subtaskId: string};

// ── Reducer ─────────────────────────────────────────────────

export function tuiReducer(state: TuiState, action: TuiAction): TuiState {
  switch (action.type) {
    case 'ADD_MESSAGES': {
      const newMessages = [...state.messages, ...action.messages];
      // Cap at 2000 messages
      const messages = newMessages.length > 2000
        ? newMessages.slice(newMessages.length - 2000)
        : newMessages;

      // If followLog is off, increment unread count instead of forcing scroll
      const unreadCount = state.followLog
        ? 0 // followLog on: unread is always 0 (view is at bottom)
        : state.unreadCount + action.messages.length;

      return {...state, messages, unreadCount};
    }

    case 'SET_SUBTASKS': {
      const completed = action.subtasks.filter((s) => s.status === 'completed').length;
      // Reset subtask selection when subtasks change
      return {
        ...state,
        subtasks: action.subtasks,
        progress: {completed, total: action.subtasks.length},
        selectedSubtaskIndex: -1,
        selectedSubtaskId: null,
        subtaskDetailOpen: false,
      };
    }

    case 'UPDATE_SUBTASK_STATUS': {
      const subtasks = state.subtasks.map((st) =>
        st.id === action.subtaskId ? {...st, status: action.status} : st,
      );
      const completed = subtasks.filter((s) => s.status === 'completed').length;
      return {
        ...state,
        subtasks,
        progress: {completed, total: subtasks.length},
      };
    }

    case 'SET_ACTIVE_TASK':
      return {...state, activeTaskId: action.taskId};

    case 'SET_FOLLOW_LOG': {
      // When re-enabling follow, reset unread count
      const unreadCount = action.follow ? 0 : state.unreadCount;
      return {...state, followLog: action.follow, unreadCount};
    }

    // ── Focus & layout (v2) ──────────────────────────────────

    case 'SET_FOCUS':
      return {
        ...state,
        focusedArea: action.area,
        selectedPane: action.area, // backward compat
      };

    case 'CYCLE_FOCUS': {
      const next = nextFocusArea(state.focusedArea);
      return {
        ...state,
        focusedArea: next,
        selectedPane: next, // backward compat
      };
    }

    case 'SET_ACTIVE_MAIN_PANE':
      return {...state, activeMainPane: action.pane};

    case 'SWAP_MAIN_PANE':
      return {...state, activeMainPane: swapMainPane(state.activeMainPane)};

    case 'ESC_TO_MAIN': {
      // Esc from input: focus returns to the active main pane
      if (state.focusedArea === 'input') {
        const area: FocusedArea = state.activeMainPane;
        return {
          ...state,
          focusedArea: area,
          selectedPane: area, // backward compat
        };
      }
      // Esc from subtask detail: close detail, stay in subtask focus
      if (state.subtaskDetailOpen) {
        return {...state, subtaskDetailOpen: false};
      }
      // Esc from chat/subtask: focus returns to input
      return {
        ...state,
        focusedArea: 'input',
        selectedPane: 'input', // backward compat
      };
    }

    // ── Deprecated: SET_SELECTED_PANE maps to SET_FOCUS ──

    case 'SET_SELECTED_PANE':
      return {
        ...state,
        focusedArea: action.pane,
        selectedPane: action.pane,
      };

    // ── Scroll ───────────────────────────────────────────────

    case 'SCROLL_UP': {
      // Scrolling up disables auto-follow and emits a scroll command
      return {
        ...state,
        followLog: false,
        scrollDirection: 'up',
        scrollLines: action.lines,
        scrollTick: state.scrollTick + 1,
      };
    }

    case 'SCROLL_DOWN': {
      // Emit scroll command; ChatLog will re-enable followLog if at bottom
      return {
        ...state,
        scrollDirection: 'down',
        scrollLines: action.lines,
        scrollTick: state.scrollTick + 1,
      };
    }

    // ── Input ─────────────────────────────────────────────────

    case 'ADD_INPUT_HISTORY': {
      // Don't add duplicates of the most recent entry
      if (state.inputHistory.length > 0 && state.inputHistory[0] === action.text) {
        return state;
      }
      const inputHistory = [action.text, ...state.inputHistory].slice(0, 50);
      return {...state, inputHistory, historyIndex: -1};
    }

    case 'SET_HISTORY_INDEX':
      return {...state, historyIndex: action.index};

    case 'SET_SUBMITTING':
      return {...state, isSubmitting: action.submitting};

    // ── Error ─────────────────────────────────────────────────

    case 'SET_LAST_ERROR':
      return {...state, lastError: action.error};

    // ── Clear ─────────────────────────────────────────────────

    case 'CLEAR_TASK':
      return {
        ...state,
        subtasks: [],
        progress: {completed: 0, total: 0},
        activeTaskId: null,
        selectedSubtaskIndex: -1,
        selectedSubtaskId: null,
        subtaskDetailOpen: false,
      };

    case 'CLEAR_LOG':
      return {...state, messages: [], followLog: true, unreadCount: 0};

    // ── Offline timers ────────────────────────────────────────

    case 'ADD_OFFLINE_TIMER':
      return {...state, offlineTimerIds: [...state.offlineTimerIds, action.timerId]};

    case 'CLEAR_OFFLINE_TIMERS':
      return {...state, offlineTimerIds: []};

    // ── Filter ────────────────────────────────────────────────

    case 'SET_EVENT_FILTER': {
      // Changing filter re-enables follow and resets unread
      return {...state, eventFilter: action.filter, followLog: true, unreadCount: 0};
    }

    // ── Unread ────────────────────────────────────────────────

    case 'INCREMENT_UNREAD':
      return {...state, unreadCount: state.unreadCount + action.count};

    case 'RESET_UNREAD':
      return {...state, unreadCount: 0};

    // ── Subtask navigation ────────────────────────────────────

    case 'SELECT_SUBTASK': {
      const idx = action.index;
      if (idx < 0 || idx >= state.subtasks.length) {
        // Invalid index: clear selection
        return {
          ...state,
          selectedSubtaskIndex: -1,
          selectedSubtaskId: null,
          subtaskDetailOpen: false,
        };
      }
      return {
        ...state,
        selectedSubtaskIndex: idx,
        selectedSubtaskId: state.subtasks[idx].id,
      };
    }

    case 'TOGGLE_SUBTASK_DETAIL': {
      if (state.selectedSubtaskIndex < 0 || !state.selectedSubtaskId) {
        return state; // No selection → can't open detail
      }
      return {...state, subtaskDetailOpen: !state.subtaskDetailOpen};
    }

    case 'CLOSE_SUBTASK_DETAIL':
      return {...state, subtaskDetailOpen: false};

    // ── Subtask retry (placeholder) ───────────────────────────

    case 'RETRY_SUBTASK':
      // Placeholder: no actual retry logic yet. Just logs intent.
      // Future: dispatch gRPC retrySubtask call
      return state;

    default:
      return state;
  }
}
