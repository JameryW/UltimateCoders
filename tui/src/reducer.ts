/**
 * TUI Reducer — single source of truth for all TUI state.
 *
 * Replaces the scattered useState + render-side-effects in App.tsx.
 * Every state transition goes through dispatch(action).
 * Render path contains zero setState calls.
 *
 * Scroll offset is NOT stored here — it is managed locally by ChatLog
 * because the offset must be relative to the filtered message list,
 * which the reducer cannot compute. Instead, the reducer tracks:
 * - followLog: whether auto-follow is active
 * - scrollTick: monotonically increasing counter for scroll commands
 * The ChatLog component reads scrollTick and applies the scroll direction
 * to its own local offset.
 */
import type {ChatMessage} from './components/ChatLog.js';
import type {SubtaskItem, SubtaskStatusType} from './components/SubtaskTree.js';
import type {SymbolMode} from './symbols.js';

// ── Pane Selection ──────────────────────────────────────────

export type SelectedPane = 'input' | 'chat' | 'subtask';

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

  /** Current keyboard focus pane. */
  selectedPane: SelectedPane;

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
}

export const INITIAL_TUI_STATE: TuiState = {
  messages: [],
  subtasks: [],
  progress: {completed: 0, total: 0},
  activeTaskId: null,
  followLog: true,
  selectedPane: 'input',
  scrollDirection: null,
  scrollLines: 0,
  scrollTick: 0,
  inputHistory: [],
  historyIndex: -1,
  lastError: null,
  offlineTimerIds: [],
  eventFilter: 'all',
  symbolMode: 'auto',
};

// ── Actions ─────────────────────────────────────────────────

export type TuiAction =
  | {type: 'ADD_MESSAGES'; messages: ChatMessage[]}
  | {type: 'SET_SUBTASKS'; subtasks: SubtaskItem[]}
  | {type: 'UPDATE_SUBTASK_STATUS'; subtaskId: string; status: SubtaskStatusType}
  | {type: 'SET_ACTIVE_TASK'; taskId: string | null}
  | {type: 'SET_FOLLOW_LOG'; follow: boolean}
  | {type: 'SET_SELECTED_PANE'; pane: SelectedPane}
  | {type: 'SCROLL_UP'; lines: number}
  | {type: 'SCROLL_DOWN'; lines: number}
  | {type: 'ADD_INPUT_HISTORY'; text: string}
  | {type: 'SET_HISTORY_INDEX'; index: number}
  | {type: 'SET_LAST_ERROR'; error: string | null}
  | {type: 'CLEAR_TASK'}
  | {type: 'CLEAR_LOG'}
  | {type: 'ADD_OFFLINE_TIMER'; timerId: ReturnType<typeof setTimeout>}
  | {type: 'CLEAR_OFFLINE_TIMERS'}
  | {type: 'SET_EVENT_FILTER'; filter: EventFilter};

// ── Reducer ─────────────────────────────────────────────────

export function tuiReducer(state: TuiState, action: TuiAction): TuiState {
  switch (action.type) {
    case 'ADD_MESSAGES': {
      const newMessages = [...state.messages, ...action.messages];
      // Cap at 2000 messages
      const messages = newMessages.length > 2000
        ? newMessages.slice(newMessages.length - 2000)
        : newMessages;
      return {...state, messages};
    }

    case 'SET_SUBTASKS': {
      const completed = action.subtasks.filter((s) => s.status === 'completed').length;
      return {
        ...state,
        subtasks: action.subtasks,
        progress: {completed, total: action.subtasks.length},
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

    case 'SET_FOLLOW_LOG':
      return {...state, followLog: action.follow};

    case 'SET_SELECTED_PANE':
      return {...state, selectedPane: action.pane};

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

    case 'SET_LAST_ERROR':
      return {...state, lastError: action.error};

    case 'CLEAR_TASK':
      return {
        ...state,
        subtasks: [],
        progress: {completed: 0, total: 0},
        activeTaskId: null,
      };

    case 'CLEAR_LOG':
      return {...state, messages: [], followLog: true};

    case 'ADD_OFFLINE_TIMER':
      return {...state, offlineTimerIds: [...state.offlineTimerIds, action.timerId]};

    case 'CLEAR_OFFLINE_TIMERS':
      return {...state, offlineTimerIds: []};

    case 'SET_EVENT_FILTER':
      return {...state, eventFilter: action.filter};

    default:
      return state;
  }
}
