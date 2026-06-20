/**
 * TUI Reducer — single source of truth for all TUI state.
 *
 * Focus model (v3 — single-column vertical):
 *   focusedArea: which area receives keyboard events (input | chat)
 *   No split panes — ChatLog is full-width, subtasks shown as overlay (Ctrl+T).
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
import type {SlashCommand} from './commands.js';
import type {TaskProto} from './grpc/types.js';

// ── Focus & Layout Types ────────────────────────────────────

/** Which area receives keyboard events. */
export type FocusedArea = 'input' | 'chat';

/** Order for Shift+Tab focus cycling. */
const FOCUS_CYCLE: FocusedArea[] = ['input', 'chat'];

/** Cycle focus area forward (Shift+Tab). */
export function nextFocusArea(current: FocusedArea): FocusedArea {
  const idx = FOCUS_CYCLE.indexOf(current);
  return FOCUS_CYCLE[(idx + 1) % FOCUS_CYCLE.length];
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

  /** Whether older messages were truncated (to show a hint in ChatLog). */
  messagesTruncated: boolean;

  /** Currently selected subtask index for keyboard navigation (-1 = none). */
  selectedSubtaskIndex: number;

  /** Currently selected subtask ID (null = none, synced with index). */
  selectedSubtaskId: string | null;

  /** Whether the subtask overlay is showing (Ctrl+T). */
  subtaskOverlayOpen: boolean;

  /** Whether the help overlay is showing. */
  helpOverlayOpen: boolean;

  /** Whether the selected subtask's detail panel is open (in overlay). */
  subtaskDetailOpen: boolean;

  /** Timestamp (ms) when the current task submission started. Null when idle. */
  startedAt: number | null;

  /** Task list from /tasks command. */
  taskList: TaskProto[];

  /** Whether a listTasks request is in progress. */
  taskListLoading: boolean;

  /** Whether the task list overlay is showing. */
  taskListOverlayOpen: boolean;

  /** Currently selected task index in task list overlay (-1 = none). */
  selectedTaskListIndex: number;

  /** Whether a task cancellation is pending (shows confirm prompt). */
  exitConfirmPending: boolean;

  /** Whether the active task has been cancelled by user. */
  taskCancelled: boolean;

  /** Currently matching slash commands for autocomplete (null = no suggestion). */
  commandSuggestions: SlashCommand[] | null;

  /** Overlay scroll offset (for subtask/task-list overlays when content exceeds terminal). */
  overlayScrollOffset: number;

  /** Search mode state. */
  searchQuery: string;
  searchActive: boolean;
  searchMatchIndex: number;
}

export const INITIAL_TUI_STATE: TuiState = {
  messages: [],
  subtasks: [],
  progress: {completed: 0, total: 0},
  activeTaskId: null,
  followLog: true,
  focusedArea: 'input',
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
  messagesTruncated: false,
  selectedSubtaskIndex: -1,
  selectedSubtaskId: null,
  subtaskOverlayOpen: false,
  helpOverlayOpen: false,
  subtaskDetailOpen: false,
  startedAt: null,
  taskList: [],
  taskListLoading: false,
  taskListOverlayOpen: false,
  selectedTaskListIndex: -1,
  commandSuggestions: null,
  exitConfirmPending: false,
  taskCancelled: false,
  overlayScrollOffset: 0,
  searchQuery: '',
  searchActive: false,
  searchMatchIndex: 0,
};

// ── Actions ─────────────────────────────────────────────────

export type TuiAction =
  | {type: 'ADD_MESSAGES'; messages: ChatMessage[]}
  | {type: 'UPDATE_MESSAGE'; messageId: string; text: string}
  | {type: 'REMOVE_MESSAGE'; messageId: string}
  | {type: 'SET_SUBTASKS'; subtasks: SubtaskItem[]}
  | {type: 'UPDATE_SUBTASK_STATUS'; subtaskId: string; status: SubtaskStatusType}
  | {type: 'SET_ACTIVE_TASK'; taskId: string | null}
  | {type: 'SET_FOLLOW_LOG'; follow: boolean}
  // ── Focus ──
  | {type: 'SET_FOCUS'; area: FocusedArea}
  | {type: 'CYCLE_FOCUS'}
  | {type: 'ESC_TO_MAIN'}
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
  // ── Subtask overlay ──
  | {type: 'SELECT_SUBTASK'; index: number}
  | {type: 'TOGGLE_SUBTASK_OVERLAY'}
  | {type: 'TOGGLE_SUBTASK_DETAIL'}
  | {type: 'JUMP_TO_FAILED_SUBTASK'}
  | {type: 'TOGGLE_HELP_OVERLAY'}
  // ── Subtask retry ──
  | {type: 'RETRY_SUBTASK'; subtaskId: string}
  // ── Task list ──
  | {type: 'SET_TASK_LIST'; tasks: TaskProto[]}
  | {type: 'SET_TASK_LIST_LOADING'; loading: boolean}
  | {type: 'TOGGLE_TASK_LIST_OVERLAY'}
  | {type: 'SELECT_TASK_LIST'; index: number}
  // ── Command suggestions ──
  | {type: 'SET_COMMAND_SUGGESTIONS'; suggestions: SlashCommand[] | null}
  // ── Exit confirm ──
  | {type: 'REQUEST_EXIT'}
  | {type: 'DISMISS_EXIT_CONFIRM'}
  // ── Task cancel ──
  | {type: 'CANCEL_TASK'}
  | {type: 'CLEAR_CANCEL'}
  // ── Overlay scroll ──
  | {type: 'OVERLAY_SCROLL'; offset: number}
  | {type: 'RESET_OVERLAY_SCROLL'}
  // ── Search ──
  | {type: 'SET_SEARCH_ACTIVE'; active: boolean}
  | {type: 'SET_SEARCH_QUERY'; query: string}
  | {type: 'SEARCH_NEXT'}
  | {type: 'SEARCH_PREV'}
  // ── Symbol mode ──
  | {type: 'SET_SYMBOL_MODE'; mode: SymbolMode};

// ── Reducer ─────────────────────────────────────────────────

export function tuiReducer(state: TuiState, action: TuiAction): TuiState {
  switch (action.type) {
    case 'ADD_MESSAGES': {
      const newMessages = [...state.messages, ...action.messages];
      // Cap at 2000 messages
      const wasTruncated = newMessages.length > 2000;
      const messages = wasTruncated
        ? newMessages.slice(newMessages.length - 2000)
        : newMessages;

      // If followLog is off, increment unread count instead of forcing scroll
      const unreadCount = state.followLog
        ? 0 // followLog on: unread is always 0 (view is at bottom)
        : state.unreadCount + action.messages.length;

      return {...state, messages, unreadCount, messagesTruncated: state.messagesTruncated || wasTruncated};
    }

    case 'UPDATE_MESSAGE': {
      // Update a single message's text by id (used for mutable subtask summary line)
      const messages = state.messages.map((m) =>
        m.id === action.messageId ? {...m, text: action.text} : m,
      );
      return {...state, messages};
    }

    case 'REMOVE_MESSAGE': {
      // Remove a single message by id (used to clean up subtask summary)
      const messages = state.messages.filter((m) => m.id !== action.messageId);
      return {...state, messages};
    }

    case 'SET_SUBTASKS': {
      const completed = action.subtasks.filter((s) => s.status === 'completed').length;
      const total = action.subtasks.length;
      // ponytail: reuse progress object if values unchanged — prevents cascading re-renders
      const progress = state.progress.completed === completed && state.progress.total === total
        ? state.progress
        : {completed, total};
      // Reset subtask selection when subtasks change
      return {
        ...state,
        subtasks: action.subtasks,
        progress,
        selectedSubtaskIndex: -1,
        selectedSubtaskId: null,
      };
    }

    case 'UPDATE_SUBTASK_STATUS': {
      const subtasks = state.subtasks.map((st) =>
        st.id === action.subtaskId ? {...st, status: action.status} : st,
      );
      const completed = subtasks.filter((s) => s.status === 'completed').length;
      const total = subtasks.length;
      const progress = state.progress.completed === completed && state.progress.total === total
        ? state.progress
        : {completed, total};
      return {
        ...state,
        subtasks,
        progress,
      };
    }

    case 'SET_ACTIVE_TASK':
      return {...state, activeTaskId: action.taskId};

    case 'SET_FOLLOW_LOG': {
      // When re-enabling follow, reset unread count
      const unreadCount = action.follow ? 0 : state.unreadCount;
      return {...state, followLog: action.follow, unreadCount};
    }

    // ── Focus ──────────────────────────────────────────────────

    case 'SET_FOCUS':
      return {...state, focusedArea: action.area};

    case 'CYCLE_FOCUS': {
      const next = nextFocusArea(state.focusedArea);
      return {...state, focusedArea: next};
    }

    case 'ESC_TO_MAIN': {
      // Esc from input: focus returns to chat
      if (state.focusedArea === 'input') {
        return {...state, focusedArea: 'chat'};
      }
      // Esc from subtask overlay: close overlay (also resets detail)
      if (state.subtaskOverlayOpen) {
        return {...state, subtaskOverlayOpen: false, subtaskDetailOpen: false};
      }
      // Esc from chat: focus returns to input
      return {...state, focusedArea: 'input'};
    }

    // ── Scroll ───────────────────────────────────────────────

    case 'SCROLL_UP': {
      return {
        ...state,
        followLog: false,
        scrollDirection: 'up',
        scrollLines: action.lines,
        scrollTick: state.scrollTick + 1,
      };
    }

    case 'SCROLL_DOWN': {
      return {
        ...state,
        scrollDirection: 'down',
        scrollLines: action.lines,
        scrollTick: state.scrollTick + 1,
      };
    }

    // ── Input ─────────────────────────────────────────────────

    case 'ADD_INPUT_HISTORY': {
      if (state.inputHistory.length > 0 && state.inputHistory[0] === action.text) {
        return state;
      }
      const inputHistory = [action.text, ...state.inputHistory].slice(0, 50);
      return {...state, inputHistory, historyIndex: -1};
    }

    case 'SET_HISTORY_INDEX':
      return {...state, historyIndex: action.index};

    case 'SET_SUBMITTING':
      return {
        ...state,
        isSubmitting: action.submitting,
        startedAt: action.submitting ? Date.now() : null,
      };

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
        subtaskOverlayOpen: false,
        subtaskDetailOpen: false,
        startedAt: null,
      };

    case 'CLEAR_LOG':
      return {...state, messages: [], followLog: true, unreadCount: 0, messagesTruncated: false};

    // ── Offline timers ────────────────────────────────────────

    case 'ADD_OFFLINE_TIMER':
      return {...state, offlineTimerIds: [...state.offlineTimerIds, action.timerId]};

    case 'CLEAR_OFFLINE_TIMERS':
      return {...state, offlineTimerIds: []};

    // ── Filter ───────────────────────────────────────────────

    case 'SET_EVENT_FILTER': {
      return {...state, eventFilter: action.filter, followLog: true, unreadCount: 0};
    }

    // ── Unread ────────────────────────────────────────────────

    case 'INCREMENT_UNREAD':
      return {...state, unreadCount: state.unreadCount + action.count};

    case 'RESET_UNREAD':
      return {...state, unreadCount: 0};

    // ── Subtask overlay ────────────────────────────────────────

    case 'SELECT_SUBTASK': {
      const idx = action.index;
      if (idx < 0 || idx >= state.subtasks.length) {
        return {
          ...state,
          selectedSubtaskIndex: -1,
          selectedSubtaskId: null,
        };
      }
      return {
        ...state,
        selectedSubtaskIndex: idx,
        selectedSubtaskId: state.subtasks[idx].id,
      };
    }

    case 'TOGGLE_SUBTASK_OVERLAY': {
      const opening = !state.subtaskOverlayOpen;
      // Auto-select first subtask when opening (if any exist)
      const selectedSubtaskIndex = opening && state.subtasks.length > 0 ? 0 : -1;
      const selectedSubtaskId = opening && state.subtasks.length > 0 ? state.subtasks[0].id : null;
      return {
        ...state,
        subtaskOverlayOpen: opening,
        subtaskDetailOpen: false,
        selectedSubtaskIndex: opening ? selectedSubtaskIndex : state.selectedSubtaskIndex,
        selectedSubtaskId: opening ? selectedSubtaskId : state.selectedSubtaskId,
        overlayScrollOffset: 0,
      };
    }

    case 'TOGGLE_SUBTASK_DETAIL':
      return {...state, subtaskDetailOpen: !state.subtaskDetailOpen};

    case 'JUMP_TO_FAILED_SUBTASK': {
      if (state.subtasks.length === 0) return state;
      const failedIndices = state.subtasks
        .map((st, idx) => ({id: st.id, idx, status: st.status}))
        .filter((x) => x.status === 'failed');
      if (failedIndices.length === 0) return state;
      const startIdx = state.selectedSubtaskIndex + 1;
      const next = failedIndices.find((f) => f.idx >= startIdx) ?? failedIndices[0];
      return {
        ...state,
        selectedSubtaskIndex: next.idx,
        selectedSubtaskId: next.id,
      };
    }

    case 'TOGGLE_HELP_OVERLAY':
      return {...state, helpOverlayOpen: !state.helpOverlayOpen};

    // ── Subtask retry ─────────────────────────────────────────

    case 'RETRY_SUBTASK': {
      const subtasks = state.subtasks.map((st) =>
        st.id === action.subtaskId && st.status === 'failed'
          ? {...st, status: 'pending' as SubtaskStatusType, errorSummary: undefined}
          : st,
      );
      const completed = subtasks.filter((s) => s.status === 'completed').length;
      return {
        ...state,
        subtasks,
        progress: {completed, total: subtasks.length},
      };
    }

    // ── Task list ─────────────────────────────────────────────

    case 'SET_TASK_LIST':
      return {...state, taskList: action.tasks, taskListLoading: false};

    case 'SET_TASK_LIST_LOADING':
      return {...state, taskListLoading: action.loading};

    case 'TOGGLE_TASK_LIST_OVERLAY': {
      const opening = !state.taskListOverlayOpen;
      return {
        ...state,
        taskListOverlayOpen: opening,
        selectedTaskListIndex: opening ? 0 : -1,
        overlayScrollOffset: 0,
      };
    }

    case 'SELECT_TASK_LIST':
      return {...state, selectedTaskListIndex: action.index};

    // ── Command suggestions ───────────────────────────────────

    case 'SET_COMMAND_SUGGESTIONS':
      return {...state, commandSuggestions: action.suggestions};

    // ── Exit confirm ──────────────────────────────────────────

    case 'REQUEST_EXIT':
      // Always set confirm — App.tsx checks active task/streaming to decide behavior
      return {...state, exitConfirmPending: true};

    case 'DISMISS_EXIT_CONFIRM':
      return {...state, exitConfirmPending: false};

    // ── Task cancel ───────────────────────────────────────────

    case 'CANCEL_TASK':
      return {
        ...state,
        taskCancelled: true,
        isSubmitting: false,
        startedAt: null,
      };

    case 'CLEAR_CANCEL':
      return {...state, taskCancelled: false};

    // ── Overlay scroll ──────────────────────────────────────────

    case 'OVERLAY_SCROLL':
      return {...state, overlayScrollOffset: Math.max(0, action.offset)};

    case 'RESET_OVERLAY_SCROLL':
      return {...state, overlayScrollOffset: 0};

    // ── Search ──────────────────────────────────────────────────

    case 'SET_SEARCH_ACTIVE':
      return {
        ...state,
        searchActive: action.active,
        searchQuery: action.active ? state.searchQuery : '',
        searchMatchIndex: 0,
      };

    case 'SET_SEARCH_QUERY':
      return {...state, searchQuery: action.query, searchMatchIndex: 0};

    case 'SEARCH_NEXT':
      return {...state, searchMatchIndex: state.searchMatchIndex + 1};

    case 'SEARCH_PREV':
      return {...state, searchMatchIndex: Math.max(0, state.searchMatchIndex - 1)};

    // ── Symbol mode ─────────────────────────────────────────────

    case 'SET_SYMBOL_MODE':
      return {...state, symbolMode: action.mode};

    default:
      return state;
  }
}
