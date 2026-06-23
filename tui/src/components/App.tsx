/**
 * App component - root layout for the UltimateCoders TUI.
 *
 * Layout (v3 — single-column vertical, reference Claude Code / Codex):
 *   ╭─ UC v0.1.0 ──────────────────────────────────────────────╮
 *   │ [12:00] ▎ fix bug              (user message, full width) │
 *   │ [12:00] ⚙ Task created: abc1   (system message)          │
 *   │ [12:00] 📋 1/3 ✅ │ 2 ⏳       (subtask summary inline)  │
 *   │ [12:00] ⚙ Read(file.ts) (2s)   (tool call, collapsed)    │
 *   │ [12:00] 📋 3/3 ✅               (subtask summary updated) │
 *   │───────────────────────────────────────────────────────────│
 *   │ ⠋ Working (12s)  Esc cancel                              │
 *   │ > type task description and press Enter...                │
 *   │ ◆ UC │ ● grpc │ P 0/3 │ F Input                         │
 *   ╰───────────────────────────────────────────────────────────╯
 *
 * Focus model (v3):
 *   focusedArea: which area receives keyboard events (input | chat)
 *   No split panes — ChatLog is full-width.
 *   SubtaskTree shown as overlay via Ctrl+T.
 *   Shift+Tab cycles focus. Esc returns to input.
 *
 * State management: useReducer (TuiState + tuiReducer).
 * All state transitions go through dispatch — no setState in render.
 * Keyboard: global useInput, shortcuts defined in keymap.ts.
 */
import React, {useReducer, useCallback, useEffect, useRef, useMemo} from 'react';
import {Box, Text, useApp, useInput, useStdout, useStdin} from 'ink';
import ChatLog, {
  type ChatMessage,
  createUserMessage,
  createSystemMessage,
  createTaskSummaryMessage,
  type ScrollCommand,
} from './ChatLog.js';
import SubtaskTree, {
  type SubtaskItem,
  type SubtaskStatusType,
} from './SubtaskTree.js';
import TaskInput from './TaskInput.js';
import StatusBar from './StatusBar.js';
import StatusIndicator from './StatusIndicator.js';
import LogoBanner, {getLogoHeight} from './LogoBanner.js';
import useGrpcClient from '../hooks/useGrpcClient.js';
import useTaskEvents from '../hooks/useTaskEvents.js';
import {
  tuiReducer,
  INITIAL_TUI_STATE,
  type TuiAction,
  type FocusedArea,
  nextEventFilter,
} from '../reducer.js';
import {formatTaskEvents} from '../formatters.js';
import {getSymbols} from '../symbols.js';
import {getCommandsForArea} from '../keymap.js';
import {getLayoutMode} from '../statusbar-utils.js';
import type {SubtaskProto, TaskProto} from '../grpc/types.js';
import {mapSubtaskStatus, mapTaskStatus} from '../grpc/types.js';
import {parseCommand, isCommandInput, formatHelpText, matchCommands, COMMANDS} from '../commands.js';
import type {SlashCommand} from '../commands.js';
import {buildTaskListText} from '../task-list-utils.js';
import {TaskListOverlay} from './TaskListOverlay.js';
import WorkerPanel from './WorkerPanel.js';
import CommandPalette from './CommandPalette.js';

// ── Constants ───────────────────────────────────────────────

const VERSION = '0.1.0';
const SUBMIT_TIMEOUT_MS = 30_000;

/** Stable ID for the mutable subtask summary message in ChatLog. */
const SUBTASK_SUMMARY_ID = '__subtask_summary__';

/** Build subtask summary text for inline display. */
function buildSubtaskSummaryText(subtasks: SubtaskItem[]): string {
  if (subtasks.length === 0) return '';
  const completed = subtasks.filter((s) => s.status === 'completed').length;
  const inProgress = subtasks.filter((s) => s.status === 'in_progress').length;
  const failed = subtasks.filter((s) => s.status === 'failed').length;
  const total = subtasks.length;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

  // ponytail: ANSI block progress bar + percentage
  const barWidth = 8;
  const filled = Math.round((completed / total) * barWidth);
  const bar = '█'.repeat(Math.max(0, filled)) + '░'.repeat(Math.max(0, barWidth - filled));

  const parts: string[] = [`${bar} ${pct}%`];
  if (completed === total) {
    parts.push('✅');
  } else {
    if (inProgress > 0) parts.push(`${inProgress} ⏳`);
    if (failed > 0) parts.push(`${failed} ✗`);
  }
  return `📋 ${parts.join(' ')}`;
}

// ── App Component ───────────────────────────────────────────

const App: React.FC = () => {
  const {exit} = useApp();
  const {stdout} = useStdout();

  // ── Reducer (single source of truth) ────────────────────
  const [state, dispatch] = useReducer(tuiReducer, INITIAL_TUI_STATE);

  // ── gRPC client hook ────────────────────────────────────
  const {
    connectionState,
    submitTask: grpcSubmitTask,
    listTasks: grpcListTasks,
    reconnect,
    pauseTask,
    resumeTask,
    client,
    retryCount,
    nextRetryAt,
    serverAddr,
  } = useGrpcClient();

  // ── Track sync-required count (incremented by sync_required events and reconnect) ──
  const needsSyncCountRef = useRef(0);
  // Mirror as state so useEffect dependency tracking works
  const [needsSyncTick, setNeedsSyncTick] = React.useState(0);

  // ── Ref for activeTaskId to avoid stale closure in sync effects ──
  const activeTaskIdRef = useRef(state.activeTaskId);
  activeTaskIdRef.current = state.activeTaskId;

  // ── onSyncRequired callback for useTaskEvents ────────────
  const onSyncRequired = useCallback((_reason: string, _skipped: number) => {
    needsSyncCountRef.current += 1;
    setNeedsSyncTick((t) => t + 1);
  }, []);

  // ── Task events hook (receives stream updates) ──────────
  const {
    task,
    subtasks: streamSubtasks,
    events,
    isStreaming,
    setSubtasksFromSubmit,
    updateSubtaskStatus,
    clearTask: clearStreamTask,
    markStreamingFinished,
  } = useTaskEvents(client, connectionState, state.activeTaskId, onSyncRequired);

  // ── Track processed events to avoid re-formatting ───────
  const processedEventCount = useRef(0);

  // ── Submit timeout ref ──────────────────────────────────
  const submitTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── Track whether offline message has been shown ────────
  const hasShownOfflineMsg = useRef(false);

  // ── Track previous connection state for transition detection ──
  const prevConnectionStateRef = useRef(connectionState);

  // ── Track whether subtask summary message has been inserted ──
  const hasSubtaskSummaryRef = useRef(false);

  // ── Auto-dismiss welcome banner after 3s ──
  useEffect(() => {
    if (!state.welcomeBannerVisible) return;
    const timer = setTimeout(() => {
      dispatch({type: 'DISMISS_WELCOME_BANNER'});
    }, 3000);
    return () => clearTimeout(timer);
  }, [state.welcomeBannerVisible, dispatch]);

  // ── Rotate StatusBar hint every 5s ──
  useEffect(() => {
    const timer = setInterval(() => {
      dispatch({type: 'ROTATE_HINT'});
    }, 5000);
    return () => clearInterval(timer);
  }, [dispatch]);

  // ── Connection state change notifications ───────────────
  useEffect(() => {
    const prev = prevConnectionStateRef.current;
    prevConnectionStateRef.current = connectionState;

    if (prev && prev !== connectionState) {
      if (connectionState === 'connected') {
        addMessage(createSystemMessage(`Connected to ${serverAddr}`, {color: 'green'}));
      } else if (connectionState === 'error' && prev === 'connected') {
        addMessage(createSystemMessage(`Connection lost to ${serverAddr}. Retrying...`, {color: 'yellow'}));
      } else if (connectionState === 'connecting' && prev === 'error') {
        addMessage(createSystemMessage(`Reconnecting to ${serverAddr}...`, {color: 'yellow'}));
      }
    }

    if (connectionState === 'connected') {
      hasShownOfflineMsg.current = false;
    } else if (prev === 'connected') {
      hasShownOfflineMsg.current = false;
    }
  }, [connectionState]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Handle sync_required from broadcast lag — re-sync via gRPC ──
  // Same pattern as Dashboard: decrement counter, call listTasks, dispatch SYNC_TASKS.
  useEffect(() => {
    if (needsSyncCountRef.current <= 0) return;
    needsSyncCountRef.current -= 1;
    if (connectionState === 'connected') {
      grpcListTasks({}).then((data) => {
        if (data && data.available) {
          dispatch({type: 'SYNC_TASKS', response: data, activeTaskId: activeTaskIdRef.current});
        }
      }).catch(() => { /* ignore */ });
    }
  }, [needsSyncTick, connectionState]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Reconnect sync: when connectionState transitions to connected, sync state ──
  const reconnectPrevRef = useRef(connectionState);
  useEffect(() => {
    const prev = reconnectPrevRef.current;
    reconnectPrevRef.current = connectionState;
    // Only trigger on transition from non-connected to connected
    if (connectionState === 'connected' && prev && prev !== 'connected') {
      grpcListTasks({}).then((data) => {
        if (data && data.available) {
          dispatch({type: 'SYNC_TASKS', response: data, activeTaskId: activeTaskIdRef.current});
        }
      }).catch(() => { /* ignore */ });
    }
  }, [connectionState]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Side effect: stream events → chat messages ─────────
  useEffect(() => {
    if (events.length <= processedEventCount.current) return;
    const newEvents = events.slice(processedEventCount.current);
    processedEventCount.current = events.length;
    const newMessages = formatTaskEvents(newEvents);
    if (newMessages.length > 0) {
      dispatch({type: 'ADD_MESSAGES', messages: newMessages});
    }
    // ponytail: dispatch StatusBar notification for key events
    const notifyTypes = new Set(['subtask_failed', 'task_completed', 'task_failed']);
    const notifyEvent = [...newEvents].reverse().find(e => notifyTypes.has(e.type));
    if (notifyEvent) {
      const color = notifyEvent.type === 'task_completed' ? 'green' : 'red';
      const text = notifyEvent.type === 'task_completed' ? 'Task completed!'
        : notifyEvent.type === 'task_failed' ? 'Task failed!'
        : 'Subtask failed!';
      dispatch({type: 'SET_NOTIFICATION', text, color});
    }
    // ponytail: generate task summary card on task_completed
    const completedEvent = [...newEvents].reverse().find(e => e.type === 'task_completed');
    if (completedEvent && state.subtasks.length > 0) {
      const summaryMsg = createTaskSummaryMessage(state.subtasks);
      dispatch({type: 'ADD_MESSAGES', messages: [summaryMsg]});
    }
    // ponytail: stop streaming spinner on terminal task events
    const hasTerminalEvent = newEvents.some(
      (e) => e.type === 'task_completed' || e.type === 'task_failed',
    );
    if (hasTerminalEvent) {
      markStreamingFinished();
    }
  }, [events.length]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Reset processed event count when events are cleared ─
  useEffect(() => {
    if (events.length === 0 && processedEventCount.current > 0) {
      processedEventCount.current = 0;
    }
  }, [events.length]);

  // ── Side effect: sync stream subtasks into reducer ──────
  const prevStreamSubtasksRef = useRef<SubtaskItem[]>([]);
  useEffect(() => {
    if (streamSubtasks.length === 0) return;

    const prev = prevStreamSubtasksRef.current;
    const changed =
      prev.length !== streamSubtasks.length ||
      streamSubtasks.some((st, i) => {
        const p = prev[i];
        if (!p) return true;
        if (p.id !== st.id || p.status !== st.status || p.assignedWorker !== st.assignedWorker || p.errorSummary !== st.errorSummary) return true;
        const dA = p.dependsOn ?? [];
        const dB = st.dependsOn ?? [];
        if (dA.length !== dB.length) return true;
        if (dA.some((d, j) => d !== dB[j])) return true;
        return false;
      });

    if (changed) {
      prevStreamSubtasksRef.current = streamSubtasks;
      dispatch({type: 'SET_SUBTASKS', subtasks: streamSubtasks});
    }
  }, [streamSubtasks]);

  // ── Side effect: update mutable subtask summary line ────
  // When subtasks change, insert or update the single summary line in ChatLog.
  // Uses a stable ID (SUBTASK_SUMMARY_ID) so the message can be updated in place.
  useEffect(() => {
    if (state.subtasks.length === 0) {
      // When subtasks are cleared, remove the summary message if it exists
      if (hasSubtaskSummaryRef.current) {
        hasSubtaskSummaryRef.current = false;
        dispatch({type: 'REMOVE_MESSAGE', messageId: SUBTASK_SUMMARY_ID});
      }
      return;
    }

    const summaryText = buildSubtaskSummaryText(state.subtasks);
    if (!summaryText) return;

    if (hasSubtaskSummaryRef.current) {
      // Update existing summary message
      dispatch({type: 'UPDATE_MESSAGE', messageId: SUBTASK_SUMMARY_ID, text: summaryText});
    } else {
      // Insert new summary message (or update if a stale one exists from a previous task)
      const alreadyExists = state.messages.some((m) => m.id === SUBTASK_SUMMARY_ID);
      if (alreadyExists) {
        dispatch({type: 'UPDATE_MESSAGE', messageId: SUBTASK_SUMMARY_ID, text: summaryText});
      } else {
        dispatch({
          type: 'ADD_MESSAGES',
          messages: [{
            id: SUBTASK_SUMMARY_ID,
            timestamp: new Date().toTimeString().slice(0, 5),
            text: summaryText,
            isUser: false,
            eventType: 'subtask_summary',
            dim: true,
          }],
        });
      }
      hasSubtaskSummaryRef.current = true;
    }
  }, [state.subtasks, state.progress]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Side effect: clear offline timers on unmount ────────
  useEffect(() => {
    return () => {
      for (const tid of state.offlineTimerIds) {
        clearTimeout(tid);
      }
      if (submitTimeoutRef.current) {
        clearTimeout(submitTimeoutRef.current);
      }
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Helper: add a single message ───────────────────────
  const addMessage = useCallback((msg: ChatMessage) => {
    dispatch({type: 'ADD_MESSAGES', messages: [msg]});
  }, []);

  // ── Stable callbacks for child props (prevent re-renders) ──
  const addFollowLog = useCallback((follow: boolean) => {
    dispatch({type: 'SET_FOLLOW_LOG', follow});
  }, []);

  const addHistoryIndex = useCallback((index: number) => {
    dispatch({type: 'SET_HISTORY_INDEX', index});
  }, []);

  // ponytail: use ref for commandSuggestions to avoid dep on state
  const commandSuggestionsRef = useRef(state.commandSuggestions);
  commandSuggestionsRef.current = state.commandSuggestions;
  const handleInputChange = useCallback((val: string) => {
    const trimmed = val.trimStart();
    if (trimmed.startsWith('/')) {
      const prefix = trimmed.slice(1).split(' ')[0];
      const matched = matchCommands(prefix);
      dispatch({type: 'SET_COMMAND_SUGGESTIONS', suggestions: matched.length > 0 ? matched : null});
    } else if (commandSuggestionsRef.current) {
      dispatch({type: 'SET_COMMAND_SUGGESTIONS', suggestions: null});
    }
  }, []);

  // ── Helper: apply submit response ──────────────────────
  const applySubmitResponse = useCallback(
    (protoSubtasks: SubtaskProto[], taskProto?: TaskProto) => {
      const items: SubtaskItem[] = protoSubtasks.map((st, idx) => ({
        id: st.id,
        index: idx + 1,
        description: st.description,
        status: mapSubtaskStatus(st.status),
        assignedWorker: st.assignedWorker,
        dependsOn: st.dependsOn,
      }));
      dispatch({type: 'SET_SUBTASKS', subtasks: items});
      setSubtasksFromSubmit(protoSubtasks, taskProto);
    },
    [setSubtasksFromSubmit],
  );

  // ── Helper: update a single subtask status ─────────────
  const updateSubtask = useCallback(
    (subtaskId: string, status: SubtaskStatusType) => {
      dispatch({type: 'UPDATE_SUBTASK_STATUS', subtaskId, status});
      updateSubtaskStatus(subtaskId, status);
    },
    [updateSubtaskStatus],
  );

  // ── Derive display info ────────────────────────────────
  const workerId = connectionState === 'connected' ? 'grpc-worker' : 'offline';
  const backend = connectionState === 'connected' ? 'grpc' : 'disconnected';
  const terminalWidth = stdout?.columns ?? 80;

  // ponytail: derive multi-worker summary from subtasks — no new gRPC call needed
  const workerSummary = useMemo(() => {
    if (connectionState !== 'connected') return undefined;
    const workerMap = new Map<string, number>();
    for (const st of state.subtasks) {
      if (st.assignedWorker) {
        const count = workerMap.get(st.assignedWorker) ?? 0;
        workerMap.set(st.assignedWorker, count + 1);
      }
    }
    const entries = [...workerMap.entries()]
      .map(([workerId, activeSubtaskCount]) => ({workerId, activeSubtaskCount}))
      .sort((a, b) => b.activeSubtaskCount - a.activeSubtaskCount);
    const activeCount = entries.filter(e => e.activeSubtaskCount > 0).length;
    return {activeCount, totalCount: entries.length, entries};
  }, [state.subtasks, connectionState]);

  // ── Calculate ChatLog visible lines ────────────────────
  // logo height adapts to terminal width (6 / 1 / 0) + separator(1) + statusIndicator(1) + input(1) + status(1) + borders(2)
  const logoHeight = getLogoHeight(terminalWidth);
  const fixedLines = logoHeight + 6;
  const visibleLines = Math.max(
    5,
    (stdout?.rows ?? 24) - fixedLines,
  );

  // ── Build scroll command for ChatLog ────────────────────
  // ponytail: useMemo to avoid new object reference each render
  const scrollCommand: ScrollCommand | undefined = useMemo(() => state.scrollDirection
    ? {direction: state.scrollDirection, lines: state.scrollLines, tick: state.scrollTick}
    : undefined, [state.scrollDirection, state.scrollLines, state.scrollTick]);

  // ── Handle task submission ─────────────────────────────
  const handleSubmit = useCallback(
    async (description: string) => {
      if (state.isSubmitting) return;

      // ── Slash command interception ──
      if (isCommandInput(description)) {
        const parsed = parseCommand(description);
        if (!parsed) {
          addMessage(createSystemMessage(`Unknown command: ${description}`, {color: 'red'}));
          return;
        }
        dispatch({type: 'ADD_INPUT_HISTORY', text: description});
        handleSlashCommand(parsed.command, parsed.args, {
          addMessage, dispatch, connectionState, client, serverAddr, reconnect, exit,
          listTasks: grpcListTasks, activeTaskId: state.activeTaskId,
          subtasks: state.subtasks, isStreaming, symbolMode: state.symbolMode,
          messages: state.messages,
        });
        return;
      }

      addMessage(createUserMessage(description));
      dispatch({type: 'ADD_INPUT_HISTORY', text: description});

      for (const tid of state.offlineTimerIds) {
        clearTimeout(tid);
      }
      dispatch({type: 'CLEAR_OFFLINE_TIMERS'});

      if (connectionState !== 'connected' || !client) {
        if (!hasShownOfflineMsg.current) {
          hasShownOfflineMsg.current = true;
          addMessage(
            createSystemMessage(`gRPC server not connected (${serverAddr}). Using offline mode. Ctrl+R to reconnect.`, {
              color: 'yellow',
            }),
          );
        }

        dispatch({type: 'SET_SUBMITTING', submitting: true});
        simulateOfflineSubmit(
          description,
          addMessage,
          applySubmitResponse,
          updateSubtask,
          dispatch,
        );
        return;
      }

      dispatch({type: 'SET_SUBMITTING', submitting: true});

      submitTimeoutRef.current = setTimeout(() => {
        dispatch({type: 'SET_SUBMITTING', submitting: false});
      }, SUBMIT_TIMEOUT_MS);

      clearStreamTask();
      dispatch({type: 'CLEAR_TASK'});
      addMessage(
        createSystemMessage(`Submitting task: ${description}`, {bold: true}),
      );
      addMessage(
        createSystemMessage('Decomposing via Orchestrator...', {dim: true}),
      );

      const response = await grpcSubmitTask({
        description,
        projectId: 'default',
      });

      if (submitTimeoutRef.current) {
        clearTimeout(submitTimeoutRef.current);
        submitTimeoutRef.current = null;
      }
      dispatch({type: 'SET_SUBMITTING', submitting: false});

      if (response && response.success) {
        dispatch({type: 'SET_ACTIVE_TASK', taskId: response.taskId});
        addMessage(
          createSystemMessage(
            `Task created: ${response.taskId.slice(0, 8)}... (status: ${response.status})`,
            {color: 'cyan'},
          ),
        );

        if (response.subtaskCount > 0) {
          applySubmitResponse(response.subtasks);
          addMessage(
            createSystemMessage(
              `Decomposed into ${response.subtaskCount} subtasks`,
              {color: 'cyan'},
            ),
          );
        }
      } else {
        const errMsg = response?.error ?? 'unknown error';
        dispatch({type: 'SET_LAST_ERROR', error: errMsg});
        addMessage(
          createSystemMessage(
            `Failed to submit task: ${errMsg}`,
            {color: 'red'},
          ),
        );
      }
    },
    [connectionState, client, addMessage, grpcSubmitTask, clearStreamTask, applySubmitResponse, updateSubtask, state.offlineTimerIds, state.isSubmitting],
  );

  // ── Auto-clear notification after 3s ──
  useEffect(() => {
    if (!state.notification) return;
    const timer = setTimeout(() => dispatch({type: 'CLEAR_NOTIFICATION'}), 3000);
    return () => clearTimeout(timer);
  }, [state.notification?.timestamp]);

  // ── Global keyboard handler ────────────────────────────
  useInput((input, key) => {
    // ── Ctrl+C / Ctrl+Q: quit (with confirmation if active task) ──
    if (key.ctrl && (input === 'c' || input === 'q')) {
      if (state.exitConfirmPending) {
        // Second Ctrl+C — confirm exit
        exit();
        return;
      }
      // No active task/streaming — exit immediately
      if (!state.activeTaskId && !state.isSubmitting && !isStreaming) {
        exit();
        return;
      }
      dispatch({type: 'REQUEST_EXIT'});
      return;
    }

    // Dismiss exit confirm on any other key
    if (state.exitConfirmPending) {
      dispatch({type: 'DISMISS_EXIT_CONFIRM'});
      return;
    }

    // ── Ctrl+X: cancel active task ──
    if (key.ctrl && input === 'x') {
      if (state.activeTaskId || state.isSubmitting || isStreaming) {
        dispatch({type: 'CANCEL_TASK'});
        for (const tid of state.offlineTimerIds) clearTimeout(tid);
        dispatch({type: 'CLEAR_OFFLINE_TIMERS'});
        dispatch({type: 'SET_SUBMITTING', submitting: false});
        dispatch({type: 'CLEAR_TASK'});
        clearStreamTask();
        addMessage(createSystemMessage('Task cancelled.', {color: 'yellow'}));
      } else {
        addMessage(createSystemMessage('No active task to cancel.', {color: 'gray'}));
      }
      return;
    }

    // Ctrl+R: reconnect gRPC
    if (key.ctrl && input === 'r') {
      if (connectionState !== 'connecting') {
        addMessage(createSystemMessage(`Reconnecting to ${serverAddr}...`, {color: 'yellow'}));
      }
      reconnect();
      return;
    }

    // Ctrl+O: pause/resume task (with error feedback)
    if (key.ctrl && input === 'o') {
      if (state.activeTaskId) {
        const taskStatus = task?.status ? mapTaskStatus(task.status) : undefined;
        if (taskStatus === 'in_progress' || taskStatus === 'planning') {
          const result = pauseTask({taskId: state.activeTaskId});
          if (result === null || result === undefined) {
            addMessage(createSystemMessage(`Pause request sent to server.`, {color: 'yellow'}));
          } else {
            addMessage(createSystemMessage(`Pausing task: ${state.activeTaskId.slice(0, 8)}...`, {color: 'yellow'}));
          }
        } else if (taskStatus === 'paused') {
          const result = resumeTask({taskId: state.activeTaskId});
          if (result === null || result === undefined) {
            addMessage(createSystemMessage(`Resume request sent to server.`, {color: 'yellow'}));
          } else {
            addMessage(createSystemMessage(`Resuming task: ${state.activeTaskId.slice(0, 8)}...`, {color: 'cyan'}));
          }
        } else {
          addMessage(createSystemMessage(`Cannot pause/resume task in ${taskStatus ?? 'unknown'} state`, {color: 'gray'}));
        }
      } else {
        addMessage(createSystemMessage('No active task to pause/resume.', {color: 'gray'}));
      }
      return;
    }

    // Ctrl+F: cycle event filter
    if (key.ctrl && input === 'f') {
      dispatch({type: 'SET_EVENT_FILTER', filter: nextEventFilter(state.eventFilter)});
      return;
    }

    // Ctrl+T: toggle subtask overlay
    if (key.ctrl && input === 't') {
      dispatch({type: 'TOGGLE_SUBTASK_OVERLAY'});
      return;
    }

    // Shift+Tab / Ctrl+W: cycle focus (input→chat→input)
    if ((key.shift && key.tab) || (key.ctrl && input === 'w' && !key.shift)) {
      dispatch({type: 'CYCLE_FOCUS'});
      return;
    }

    // Ctrl+Shift+W: toggle worker detail expansion in StatusBar
    if (key.ctrl && key.shift && input === 'W') {
      dispatch({type: 'TOGGLE_WORKERS_EXPANDED'});
      return;
    }

    // Ctrl+P: toggle command palette
    if (key.ctrl && input === 'p' && !key.shift) {
      dispatch({type: 'TOGGLE_COMMAND_PALETTE'});
      return;
    }

    // Ctrl+S: toggle search mode (chat focus only)
    if (key.ctrl && input === 's' && state.focusedArea === 'chat') {
      dispatch({type: 'SET_SEARCH_ACTIVE', active: !state.searchActive});
      return;
    }

    // Ctrl+G: toggle input history search (input focus only)
    if (key.ctrl && input === 'g' && state.focusedArea === 'input') {
      dispatch({type: 'SET_HISTORY_SEARCH', active: !state.historySearchActive});
      return;
    }

    // Esc: context-dependent escape
    if (key.escape) {
      // Close search mode first if active
      if (state.searchActive) {
        dispatch({type: 'SET_SEARCH_ACTIVE', active: false});
        return;
      }
      // Cancel submitting task if Esc pressed during submission
      if (state.isSubmitting) {
        dispatch({type: 'CANCEL_TASK'});
        for (const tid of state.offlineTimerIds) clearTimeout(tid);
        dispatch({type: 'CLEAR_OFFLINE_TIMERS'});
        dispatch({type: 'SET_SUBMITTING', submitting: false});
        dispatch({type: 'CLEAR_TASK'});
        clearStreamTask();
        addMessage(createSystemMessage('Task submission cancelled.', {color: 'yellow'}));
        return;
      }
      // Close subtask detail first if open
      if (state.subtaskOverlayOpen && state.subtaskDetailOpen) {
        dispatch({type: 'TOGGLE_SUBTASK_DETAIL'});
        return;
      }
      // Close subtask overlay if open
      if (state.subtaskOverlayOpen) {
        dispatch({type: 'TOGGLE_SUBTASK_OVERLAY'});
        return;
      }
      // Close help overlay if open
      if (state.helpOverlayOpen) {
        dispatch({type: 'TOGGLE_HELP_OVERLAY'});
        return;
      }
      // Close task list overlay if open
      if (state.taskListOverlayOpen) {
        dispatch({type: 'TOGGLE_TASK_LIST_OVERLAY'});
        return;
      }
      dispatch({type: 'ESC_TO_MAIN'});
      return;
    }

    // ?: toggle help overlay
    if (input === '?' && !key.ctrl && !key.meta && (state.helpOverlayOpen || state.focusedArea !== 'input')) {
      dispatch({type: 'TOGGLE_HELP_OVERLAY'});
      return;
    }

    // ── Subtask overlay shortcuts ────────────────────────
    if (state.subtaskOverlayOpen) {
      if (key.upArrow) {
        // If no selection yet, select first item; otherwise wrap
        const nextIdx = state.selectedSubtaskIndex < 0
          ? 0
          : state.selectedSubtaskIndex === 0
            ? state.subtasks.length - 1
            : state.selectedSubtaskIndex - 1;
        dispatch({type: 'SELECT_SUBTASK', index: nextIdx});
        return;
      }
      if (key.downArrow) {
        const nextIdx = state.selectedSubtaskIndex < 0
          ? 0
          : state.selectedSubtaskIndex >= state.subtasks.length - 1
            ? 0
            : state.selectedSubtaskIndex + 1;
        dispatch({type: 'SELECT_SUBTASK', index: nextIdx});
        return;
      }
      if (key.pageUp) {
        const step = Math.max(1, Math.floor(visibleLines / 2));
        const nextIdx = Math.max(0, state.selectedSubtaskIndex - step);
        dispatch({type: 'SELECT_SUBTASK', index: nextIdx});
        return;
      }
      if (key.pageDown) {
        const step = Math.max(1, Math.floor(visibleLines / 2));
        const nextIdx = Math.min(state.subtasks.length - 1, state.selectedSubtaskIndex + step);
        dispatch({type: 'SELECT_SUBTASK', index: nextIdx});
        return;
      }
      if (key.return) {
        dispatch({type: 'TOGGLE_SUBTASK_DETAIL'});
        return;
      }
      if (input === 'r' && !key.ctrl && !key.meta) {
        // R: retry failed subtask
        const selected = state.subtasks[state.selectedSubtaskIndex];
        if (selected && selected.status === 'failed') {
          dispatch({type: 'RETRY_SUBTASK', subtaskId: selected.id});
          updateSubtask(selected.id, 'pending');
          addMessage(createSystemMessage(`Retrying subtask: ${selected.description.slice(0, 50)}`, {color: 'yellow'}));

          // Offline: simulate retry progress with timers
          if (connectionState !== 'connected') {
            const startTimer = setTimeout(() => {
              updateSubtask(selected.id, 'in_progress');
              addMessage(createSystemMessage(`▶ Subtask started: ${selected.description.slice(0, 50)}`, {color: 'cyan'}));
            }, 1000);
            dispatch({type: 'ADD_OFFLINE_TIMER', timerId: startTimer});

            const completeTimer = setTimeout(() => {
              updateSubtask(selected.id, 'completed');
              addMessage(createSystemMessage(`✓ Subtask completed: ${selected.description.slice(0, 50)}`, {color: 'green'}));
            }, 2500);
            dispatch({type: 'ADD_OFFLINE_TIMER', timerId: completeTimer});
          }
        }
        return;
      }
      // In overlay, block other keys from reaching focus-area handlers
      return;
    }

    // ── Task list overlay shortcuts ───────────────────────
    if (state.taskListOverlayOpen) {
      if (key.upArrow) {
        const nextIdx = state.selectedTaskListIndex <= 0
          ? state.taskList.length - 1
          : state.selectedTaskListIndex - 1;
        dispatch({type: 'SELECT_TASK_LIST', index: nextIdx});
        return;
      }
      if (key.downArrow) {
        const nextIdx = state.selectedTaskListIndex >= state.taskList.length - 1
          ? 0
          : state.selectedTaskListIndex + 1;
        dispatch({type: 'SELECT_TASK_LIST', index: nextIdx});
        return;
      }
      if (key.pageUp) {
        const step = Math.max(1, Math.floor(visibleLines / 2));
        const nextIdx = Math.max(0, state.selectedTaskListIndex - step);
        dispatch({type: 'SELECT_TASK_LIST', index: nextIdx});
        return;
      }
      if (key.pageDown) {
        const step = Math.max(1, Math.floor(visibleLines / 2));
        const nextIdx = Math.min(state.taskList.length - 1, state.selectedTaskListIndex + step);
        dispatch({type: 'SELECT_TASK_LIST', index: nextIdx});
        return;
      }
      if (key.return) {
        const selectedTask = state.taskList[state.selectedTaskListIndex];
        if (selectedTask) {
          dispatch({type: 'TOGGLE_TASK_LIST_OVERLAY'});
          dispatch({type: 'SET_ACTIVE_TASK', taskId: selectedTask.id});
          dispatch({type: 'CLEAR_TASK'});
          addMessage(createSystemMessage(`Switched to task: ${selectedTask.id.slice(0, 8)}... — ${selectedTask.description.slice(0, 40)}`, {color: 'cyan'}));
        }
        return;
      }
      // Block other keys
      return;
    }

    // ── Focus-area-specific shortcuts ────────────────────
    switch (state.focusedArea) {
      case 'chat': {
        if (key.pageUp) {
          dispatch({type: 'SCROLL_UP', lines: visibleLines});
          return;
        }
        if (key.pageDown) {
          dispatch({type: 'SCROLL_DOWN', lines: visibleLines});
          return;
        }
        // Up/Down/Enter handled by ChatLog internally (message selection + expand)
        if ((key as any).home) {
          dispatch({type: 'SCROLL_UP', lines: state.messages.length});
          return;
        }
        if ((key as any).end) {
          dispatch({type: 'SET_FOLLOW_LOG', follow: true});
          return;
        }
        if (key.ctrl && input === 'l') {
          dispatch({type: 'CLEAR_LOG'});
          return;
        }
        // Search navigation: n = next match, N = prev match
        if (state.searchActive && !key.ctrl && !key.meta) {
          if (input === 'n' && !key.shift) {
            dispatch({type: 'SEARCH_NEXT'});
            return;
          }
          if (input === 'N' || (input === 'n' && key.shift)) {
            dispatch({type: 'SEARCH_PREV'});
            return;
          }
        }
        // Search query input: when search active, printable chars and backspace update query
        if (state.searchActive) {
          if (key.backspace || key.delete) {
            dispatch({type: 'SET_SEARCH_QUERY', query: state.searchQuery.slice(0, -1)});
            return;
          }
          if (!key.ctrl && !key.meta && input.length === 1) {
            dispatch({type: 'SET_SEARCH_QUERY', query: state.searchQuery + input});
            return;
          }
        }
        // Enter handled by ChatLog internally (per-message expand)
        // A: toggle expand all messages; Shift+A: collapse all
        if (input === 'A' && key.shift && !key.ctrl && !key.meta) {
          dispatch({type: 'COLLAPSE_ALL'});
          return;
        }
        if (input === 'a' && !key.shift && !key.ctrl && !key.meta) {
          dispatch({type: 'TOGGLE_EXPAND_ALL'});
          return;
        }
        break;
      }

      case 'input': {
        // ponytail: intercept keyboard when history search is active
        if (state.historySearchActive) {
          if (key.escape) {
            dispatch({type: 'SET_HISTORY_SEARCH', active: false});
            return;
          }
          if (key.backspace || key.delete) {
            dispatch({type: 'SET_HISTORY_SEARCH_QUERY', query: state.historySearchQuery.slice(0, -1)});
            return;
          }
          if (key.return) {
            // Select first matching history entry and apply to input
            const filtered = state.inputHistory.filter(h =>
              h.toLowerCase().includes(state.historySearchQuery.toLowerCase()),
            );
            if (filtered.length > 0) {
              dispatch({type: 'SET_HISTORY_SEARCH', active: false});
              // ponytail: find the index of the first match in inputHistory to set historyIndex
              const matchIdx = state.inputHistory.indexOf(filtered[0]);
              if (matchIdx >= 0) dispatch({type: 'SET_HISTORY_INDEX', index: matchIdx});
            }
            return;
          }
          if (!key.ctrl && !key.meta && input.length === 1) {
            dispatch({type: 'SET_HISTORY_SEARCH_QUERY', query: state.historySearchQuery + input});
            return;
          }
        }
        break;
      }
    }
  });

  // ── Raw input handler for Home/End (Ink v5 Key type lacks these) ──
  // Ink's useInput doesn't expose home/end. Detect raw escape sequences instead.
  // Home: \x1b[H or \x1b[1~  |  End: \x1b[F or \x1b[4~
  // ponytail: remove this workaround when upgrading to Ink v6.6+ (needs React 19)
  const {stdin: rawStdin, internal_eventEmitter: rawEmitter} = useStdin();
  // ponytail: refs to avoid re-registering listener on every message/focus change
  const focusedAreaRef = useRef(state.focusedArea);
  focusedAreaRef.current = state.focusedArea;
  const messagesLengthRef = useRef(state.messages.length);
  messagesLengthRef.current = state.messages.length;
  useEffect(() => {
    if (!rawStdin || !rawEmitter) return;
    const onRawInput = (data: string) => {
      if (focusedAreaRef.current === 'chat') {
        if (data === '\x1b[H' || data === '\x1b[1~') {
          dispatch({type: 'SCROLL_UP', lines: messagesLengthRef.current});
        }
        else if (data === '\x1b[F' || data === '\x1b[4~') {
          dispatch({type: 'SET_FOLLOW_LOG', follow: true});
        }
      }
    };
    rawEmitter.on('input', onRawInput);
    return () => {
      rawEmitter.off('input', onRawInput);
    };
  }, [rawStdin, rawEmitter, dispatch]);

  // ── Symbols ─────────────────────────────────────────────
  const S = getSymbols(state.symbolMode);

  // ── Render: Overlay mode (help or subtask) ─────────────
  // Help overlay
  if (state.helpOverlayOpen) {
    const commands = getCommandsForArea(state.focusedArea);
    const globalCmds = commands.filter((c) => c.global);
    const areaCmds = commands.filter((c) => !c.global);
    return (
      <Box flexDirection="column" borderStyle="round" borderColor="cyan" padding={1}>
        <Box marginBottom={1}>
          <Text bold color="cyan">{'Keyboard Shortcuts'}</Text>
          <Text dimColor>{' (? or Esc to close)'}</Text>
          <Text dimColor>{` — Focus: ${state.focusedArea}`}</Text>
        </Box>
        <Text bold>{'Global:'}</Text>
        {globalCmds.map((cmd) => (
          <Box key={cmd.id}>
            <Text color="yellow">{`  ${cmd.key.padEnd(14)}`}</Text>
            <Text>{cmd.label}</Text>
          </Box>
        ))}
        {areaCmds.length > 0 && (
          <>
            <Box marginTop={1}>
              <Text bold>{`${state.focusedArea.charAt(0).toUpperCase() + state.focusedArea.slice(1)}:`}</Text>
            </Box>
            {areaCmds.map((cmd) => (
              <Box key={cmd.id}>
                <Text color="yellow">{`  ${cmd.key.padEnd(14)}`}</Text>
                <Text>{cmd.label}</Text>
              </Box>
            ))}
          </>
        )}
      </Box>
    );
  }

  // Subtask overlay (Ctrl+T)
  if (state.subtaskOverlayOpen) {
    return (
      <Box flexDirection="column" borderStyle="round" borderColor="cyan" padding={1}>
        <Box marginBottom={1}>
          <Text bold color="cyan">{'Subtasks'}</Text>
          {state.progress.total > 0 && (
            <>
              <Text dimColor>{` [${state.progress.completed}/${state.progress.total}]`}</Text>
              <Text dimColor>{` ${'█'.repeat(Math.round(state.progress.completed / state.progress.total * 10))}${'░'.repeat(10 - Math.round(state.progress.completed / state.progress.total * 10))}`}</Text>
            </>
          )}
          <Text dimColor>{' (Ctrl+T or Esc to close · ↑↓ navigate · Enter detail · R retry)'}</Text>
        </Box>
        <SubtaskTree
          subtasks={state.subtasks}
          taskDescription={task?.description ?? 'No task'}
          progress={state.progress}
          isFocused={true}
          maxWidth={terminalWidth - 4}
          symbols={S}
          selectedIndex={state.selectedSubtaskIndex}
          detailOpen={state.subtaskDetailOpen}
          maxVisibleLines={(stdout?.rows ?? 24) - 5}
          scrollOffset={state.overlayScrollOffset}
        />
      </Box>
    );
  }

  // Task list overlay (/tasks then T)
  if (state.taskListOverlayOpen) {
    return (
      <Box flexDirection="column" borderStyle="round" borderColor="cyan" padding={1}>
        <Box marginBottom={1}>
          <Text bold color="cyan">{'Tasks'}</Text>
          <Text dimColor>{` (${state.taskList.length} total)`}</Text>
          <Text dimColor>{' (Esc to close · ↑↓ navigate · Enter switch)'}</Text>
        </Box>
        <TaskListOverlay
          tasks={state.taskList}
          selectedIndex={state.selectedTaskListIndex}
          maxWidth={terminalWidth - 4}
          maxVisibleLines={(stdout?.rows ?? 24) - 5}
        />
      </Box>
    );
  }

  // Command palette overlay (Ctrl+P)
  if (state.commandPaletteOpen) {
    return (
      <CommandPalette
        query={state.commandPaletteQuery}
        selectedIndex={state.selectedCommandIndex}
        onSelect={(cmd, args) => handleSlashCommand(cmd, args, {
          addMessage, dispatch, connectionState, client, serverAddr, reconnect, exit,
          listTasks: grpcListTasks, activeTaskId: state.activeTaskId, subtasks: state.subtasks,
          isStreaming, symbolMode: state.symbolMode, messages: state.messages,
        })}
        onQueryChange={(q) => dispatch({type: 'SET_COMMAND_PALETTE_QUERY', query: q})}
        onSelectIndex={(i) => dispatch({type: 'SELECT_COMMAND_PALETTE', index: i})}
        onClose={() => dispatch({type: 'TOGGLE_COMMAND_PALETTE'})}
      />
    );
  }

  // ── Render: Main layout ──────────────────────────────
  const layoutMode = getLayoutMode(terminalWidth);
  const isWideMode = layoutMode === 'wide' && state.subtasks.length > 0;
  const chatWidth = isWideMode ? Math.floor(terminalWidth * 0.62) - 3 : terminalWidth - 3;
  const subtaskWidth = isWideMode ? terminalWidth - chatWidth - 5 : 0;

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" padding={0}>
      {/* ── Logo banner ── */}
      <LogoBanner
        terminalWidth={terminalWidth}
        brandChar={S.brand}
        version={VERSION}
        welcomeVisible={state.welcomeBannerVisible}
      />

      {/* ── Content: ChatLog + optional SubtaskTree side-by-side ── */}
      {isWideMode ? (
        <Box flexDirection="row">
          <Box flexDirection="column" width={chatWidth}>
            <ChatLog
              messages={state.messages}
              followLog={state.followLog}
              visibleLines={visibleLines}
              isFocused={state.focusedArea === 'chat'}
              eventFilter={state.eventFilter}
              scrollCommand={scrollCommand}
              unreadCount={state.unreadCount}
              onSetFollowLog={addFollowLog}
              terminalWidth={chatWidth}
              messagesTruncated={state.messagesTruncated}
              searchQuery={state.searchQuery}
              searchActive={state.searchActive}
              searchMatchIndex={state.searchMatchIndex}
              expandAll={state.expandAll}
            />
          </Box>
          <Box flexDirection="column" width={1}>
            <Text color="gray">{S.verticalSep}</Text>
          </Box>
          <Box flexDirection="column" width={subtaskWidth}>
            <SubtaskTree
              subtasks={state.subtasks}
              taskDescription={task?.description ?? 'No task'}
              progress={state.progress}
              isFocused={false}
              maxWidth={subtaskWidth}
            />
            {state.workersExpanded && (
              <WorkerPanel subtasks={state.subtasks} maxWidth={subtaskWidth} symbols={S} />
            )}
          </Box>
        </Box>
      ) : (
        <ChatLog
          messages={state.messages}
          followLog={state.followLog}
          visibleLines={visibleLines}
          isFocused={state.focusedArea === 'chat'}
          eventFilter={state.eventFilter}
          scrollCommand={scrollCommand}
          unreadCount={state.unreadCount}
          onSetFollowLog={addFollowLog}
          terminalWidth={terminalWidth}
          messagesTruncated={state.messagesTruncated}
          searchQuery={state.searchQuery}
          searchActive={state.searchActive}
          searchMatchIndex={state.searchMatchIndex}
          expandAll={state.expandAll}
        />
      )}

      {/* ── Separator ────────────────────────────────── */}
      <Box width="100%">
        <Text color="gray">{S.divider.repeat(Math.max(1, Math.floor(terminalWidth / S.divider.length) - 2))}</Text>
      </Box>

      {/* ── Command suggestion panel (between separator and input) ── */}
      {state.commandSuggestions && state.commandSuggestions.length > 0 && (
        <Box paddingX={1}>
          <Text dimColor>{'Commands: '}</Text>
          {state.commandSuggestions.map((cmd, i) => (
            <React.Fragment key={cmd.name}>
              <Text color="cyan">{`/${cmd.name}`}</Text>
              <Text dimColor>{i < state.commandSuggestions!.length - 1 ? '  ' : ''}</Text>
            </React.Fragment>
          ))}
        </Box>
      )}

      {/* ── Exit confirmation prompt ── */}
      {state.exitConfirmPending && (
        <Box paddingX={1}>
          <Text color="red" bold>{'⚠ Task active — Ctrl+C again to confirm exit, any other key to dismiss'}</Text>
        </Box>
      )}

      {/* ── Status indicator (spinner + elapsed) ────── */}
      <StatusIndicator
        isSubmitting={state.isSubmitting}
        isStreaming={isStreaming}
        startedAt={state.startedAt}
        progress={state.progress}
      />

      {/* ── Input (fixed at bottom) ─────────────────── */}
      <TaskInput
        onSubmit={handleSubmit}
        isFocused={state.focusedArea === 'input'}
        isSubmitting={state.isSubmitting}
        inputHistory={state.inputHistory}
        historyIndex={state.historyIndex}
        onHistoryIndexChange={addHistoryIndex}
        isOffline={connectionState !== 'connected'}
        commandSuggestions={state.commandSuggestions}
        onValueChange={handleInputChange}
        historySearchActive={state.historySearchActive}
        historySearchQuery={state.historySearchQuery}
      />

      {/* ── Status bar ──────────────────────────────── */}
      {/* Worker panel above StatusBar when expanded (non-wide mode) */}
      {state.workersExpanded && !isWideMode && state.subtasks.length > 0 && (
        <WorkerPanel subtasks={state.subtasks} maxWidth={terminalWidth - 4} symbols={S} />
      )}
      <StatusBar
        workerId={workerId}
        workerSummary={workerSummary}
        workersExpanded={state.workersExpanded}
        backend={backend}
        progress={state.progress}
        connectionState={connectionState}
        isStreaming={isStreaming}
        focusedArea={state.focusedArea}
        terminalWidth={terminalWidth}
        retryCount={retryCount}
        nextRetryAt={nextRetryAt}
        notification={state.notification}
        hintRotationIndex={state.hintRotationIndex}
      />
    </Box>
  );
};

// ── Slash Command Handler ────────────────────────────────────

function handleSlashCommand(
  command: SlashCommand,
  args: string,
  deps: {
    addMessage: (msg: ChatMessage) => void;
    dispatch: React.Dispatch<TuiAction>;
    connectionState: string;
    client: import('../grpc/client.js').TaskServiceClient | null;
    serverAddr: string;
    reconnect: () => void;
    exit: () => void;
    listTasks: (req: {}) => Promise<import('../grpc/types.js').ListTasksResponse | null>;
    activeTaskId: string | null;
    subtasks: SubtaskItem[];
    isStreaming: boolean;
    symbolMode: string;
    messages: ChatMessage[];
  },
): void {
  const {addMessage, dispatch, connectionState, client, serverAddr, reconnect, exit, listTasks, activeTaskId, subtasks, isStreaming, symbolMode, messages} = deps;

  switch (command.name) {
    case 'help':
      addMessage(createSystemMessage(formatHelpText(), {eventType: 'command_result'}));
      break;
    case 'clear':
      dispatch({type: 'CLEAR_LOG'});
      break;
    case 'cancel':
      if (activeTaskId || isStreaming) {
        dispatch({type: 'CANCEL_TASK'});
        dispatch({type: 'SET_SUBMITTING', submitting: false});
        dispatch({type: 'CLEAR_TASK'});
        addMessage(createSystemMessage('Task cancelled.', {color: 'yellow'}));
      } else {
        addMessage(createSystemMessage('No active task to cancel.', {color: 'gray'}));
      }
      break;
    case 'reconnect':
      addMessage(createSystemMessage(`Reconnecting to ${serverAddr}...`, {color: 'yellow'}));
      reconnect();
      break;
    case 'quit':
      exit();
      break;
    case 'status': {
      const lines: string[] = [];
      lines.push(`**Connection:** ${connectionState === 'connected' ? '● Connected' : connectionState === 'connecting' ? '◌ Connecting' : '○ Disconnected'}`);
      lines.push(`**Server:** ${serverAddr}`);
      if (activeTaskId) {
        lines.push(`**Active task:** ${activeTaskId.slice(0, 8)}...`);
        const completed = subtasks.filter((s) => s.status === 'completed').length;
        lines.push(`**Progress:** ${completed}/${subtasks.length}`);
      } else {
        lines.push(`**Active task:** none`);
      }
      lines.push(`**Streaming:** ${isStreaming ? 'yes' : 'no'}`);
      addMessage(createSystemMessage(lines.join('\n'), {eventType: 'command_result'}));
      break;
    }
    case 'tasks': {
      if (connectionState !== 'connected' || !client) {
        addMessage(createSystemMessage('Not connected. Use /reconnect first.', {color: 'yellow'}));
        break;
      }
      dispatch({type: 'SET_TASK_LIST_LOADING', loading: true});
      addMessage(createSystemMessage('Fetching task list...', {dim: true}));
      listTasks({}).then((response) => {
        dispatch({type: 'SET_TASK_LIST_LOADING', loading: false});
        if (response && response.available) {
          dispatch({type: 'SET_TASK_LIST', tasks: response.tasks});
          const taskLines = buildTaskListText(response.tasks);
          addMessage(createSystemMessage(taskLines.join('\n'), {eventType: 'task_list'}));
          // Open interactive overlay for navigation
          if (response.tasks.length > 0) {
            dispatch({type: 'TOGGLE_TASK_LIST_OVERLAY'});
          }
        } else if (response && !response.available) {
          addMessage(createSystemMessage('Task store not available.', {color: 'yellow'}));
        } else {
          addMessage(createSystemMessage('Failed to fetch task list.', {color: 'red'}));
        }
      });
      break;
    }
    case 'task': {
      if (!args) {
        addMessage(createSystemMessage('Usage: /task <task-id>', {color: 'yellow'}));
        break;
      }
      dispatch({type: 'SET_ACTIVE_TASK', taskId: args});
      dispatch({type: 'CLEAR_TASK'});
      addMessage(createSystemMessage(`Switched to task: ${args.slice(0, 8)}...`, {color: 'cyan'}));
      break;
    }
    case 'symbols': {
      const validModes = ['unicode', 'ascii', 'auto'];
      const mode = args.trim().toLowerCase();
      if (!validModes.includes(mode)) {
        addMessage(createSystemMessage(`Usage: /symbols <unicode|ascii|auto>\nCurrent: ${deps.symbolMode}`, {color: 'yellow'}));
        break;
      }
      dispatch({type: 'SET_SYMBOL_MODE', mode: mode as any});
      addMessage(createSystemMessage(`Symbol mode set to: ${mode}`, {color: 'cyan'}));
      break;
    }
    case 'export': {
      const exportPath = args.trim() || 'uc-export';
      const jsonPath = exportPath.endsWith('.json') ? exportPath : exportPath + '.json';
      const txtPath = exportPath.endsWith('.txt') ? exportPath : exportPath + '.txt';
      try {
        const fs = require('fs') as typeof import('fs');
        const path = require('path') as typeof import('path');
        const resolvedJson = path.resolve(jsonPath);
        const resolvedTxt = path.resolve(txtPath);
        const data = deps.messages.map((m: any) => ({
          id: m.id, timestamp: m.timestamp, text: m.text,
          isUser: m.isUser, eventType: m.eventType,
        }));
        fs.writeFileSync(resolvedJson, JSON.stringify(data, null, 2));
        const plain = deps.messages.map((m: any) => `[${m.timestamp}] ${m.isUser ? '>' : ' '} ${m.text}`).join('\n');
        fs.writeFileSync(resolvedTxt, plain);
        addMessage(createSystemMessage(`Exported to ${resolvedJson} + ${resolvedTxt}`, {color: 'green'}));
      } catch (e: any) {
        addMessage(createSystemMessage(`Export failed: ${e.message}`, {color: 'red'}));
      }
      break;
    }
    default:
      addMessage(createSystemMessage(`Unknown command: /${command.name}`, {color: 'red'}));
  }
}

// ── Offline Simulation ──────────────────────────────────────

/**
 * Offline fallback: simulate task submission without gRPC server.
 * Timer IDs are tracked in reducer state for cleanup.
 */
function simulateOfflineSubmit(
  description: string,
  addMessage: (msg: ChatMessage) => void,
  applySubmitResponse: (subtasks: SubtaskProto[], task?: TaskProto) => void,
  updateSubtask: (subtaskId: string, status: SubtaskStatusType) => void,
  dispatch: React.Dispatch<TuiAction>,
): void {
  const lines = description
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  if (lines.length <= 1) {
    lines.push(description);
  }

  const baseId = `offline-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

  const mockSubtasks: SubtaskProto[] = lines.map((line, idx) => {
    const cleaned = line
      .replace(/^\d+[\.\)]\s*/, '')
      .trim();
    return {
      id: `${baseId}-st-${idx}`,
      description: cleaned || line,
      status: idx === 0 ? 'InProgress' : 'Pending',
      dependsOn: idx > 0 ? [`${baseId}-st-${idx - 1}`] : [],
    };
  });

  const mockTask: TaskProto = {
    id: `${baseId}-task`,
    description,
    status: 'InProgress',
    projectId: 'default',
    subtaskCount: mockSubtasks.length,
    createdAt: Date.now(),
    updatedAt: Date.now(),
    subtasks: mockSubtasks,
  };

  dispatch({type: 'SET_ACTIVE_TASK', taskId: mockTask.id});
  applySubmitResponse(mockSubtasks, mockTask);

  addMessage(
    createSystemMessage(
      `Decomposed into ${mockSubtasks.length} subtasks (offline)`,
      {color: 'yellow'},
    ),
  );

  // Simulate subtask progress
  const pendingSubtasks = mockSubtasks.filter((s) => s.status === 'Pending');
  pendingSubtasks.forEach((st, idx) => {
    const startDelay = 1000 + idx * 1500;
    const completeDelay = 2500 + idx * 1500;

    const startTimer = setTimeout(() => {
      updateSubtask(st.id, 'in_progress');
      addMessage(
        createSystemMessage(
          `▶ Subtask started: ${st.description.slice(0, 50)}`,
          {color: 'cyan'},
        ),
      );
    }, startDelay);
    dispatch({type: 'ADD_OFFLINE_TIMER', timerId: startTimer});

    const completeTimer = setTimeout(() => {
      updateSubtask(st.id, 'completed');
      addMessage(
        createSystemMessage(
          `✓ Subtask completed: ${st.description.slice(0, 50)}`,
          {color: 'green'},
        ),
      );
    }, completeDelay);
    dispatch({type: 'ADD_OFFLINE_TIMER', timerId: completeTimer});
  });
}

export default App;
