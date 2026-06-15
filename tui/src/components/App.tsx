/**
 * App component - root layout for the UltimateCoders TUI.
 *
 * Layout (unified border frame):
 *   ╭─ UltimateCoders v0.1.0 ● connected ────────────────────╮
 *   │ Chat                    │ Subtasks [0/3 0%]            │
 *   │ [12:00] > fix bug      │ ◉ 1. Analyze                 │
 *   │ [12:00] Task created   │ ○ 2. Fix                     │
 *   │                         │ ○ 3. Test                    │
 *   │─────────────────────────────────────────────────────────│
 *   │ > type task description and press Enter...             │
 *   │ Worker: grpc │ Backend: grpc │ Progress: 0/3           │
 *   ╰─────────────────────────────────────────────────────────╯
 *
 * State management: useReducer (TuiState + tuiReducer).
 * All state transitions go through dispatch — no setState in render.
 * Keyboard: global useInput with Tab pane switching.
 *
 * Scroll offset is managed locally by ChatLog because it must be
 * relative to the filtered message list. The reducer tracks followLog
 * and emits scroll commands (via scrollTick) that ChatLog applies.
 */
import React, {useReducer, useCallback, useEffect, useRef} from 'react';
import {Box, Text, useApp, useInput, useStdout} from 'ink';
import ChatLog, {
  type ChatMessage,
  createUserMessage,
  createSystemMessage,
  type ScrollCommand,
} from './ChatLog.js';
import SubtaskTree, {
  type SubtaskItem,
  type SubtaskStatusType,
} from './SubtaskTree.js';
import TaskInput from './TaskInput.js';
import StatusBar from './StatusBar.js';
import useGrpcClient from '../hooks/useGrpcClient.js';
import useTaskEvents from '../hooks/useTaskEvents.js';
import {tuiReducer, INITIAL_TUI_STATE, type SelectedPane, type TuiAction, nextEventFilter, type EventFilter} from '../reducer.js';
import {formatTaskEvents} from '../formatters.js';
import {getSymbols} from '../symbols.js';
import type {SubtaskProto, TaskProto} from '../grpc/types.js';
import {mapSubtaskStatus} from '../grpc/types.js';

// ── Constants ───────────────────────────────────────────────

const VERSION = '0.1.0';

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
    reconnect,
    pauseTask,
    resumeTask,
    client,
  } = useGrpcClient();

  // ── Task events hook (receives stream updates) ──────────
  const {
    task,
    subtasks: streamSubtasks,
    events,
    isStreaming,
    setSubtasksFromSubmit,
    updateSubtaskStatus,
    clearTask: clearStreamTask,
  } = useTaskEvents(client, connectionState);

  // ── Track processed events to avoid re-formatting ───────
  const processedEventCount = useRef(0);

  // ── Side effect: stream events → chat messages ─────────
  useEffect(() => {
    if (events.length <= processedEventCount.current) return;
    const newEvents = events.slice(processedEventCount.current);
    processedEventCount.current = events.length;
    const newMessages = formatTaskEvents(newEvents);
    if (newMessages.length > 0) {
      dispatch({type: 'ADD_MESSAGES', messages: newMessages});
    }
  }, [events.length]);

  // ── Reset processed event count when events are cleared ─
  useEffect(() => {
    if (events.length === 0 && processedEventCount.current > 0) {
      processedEventCount.current = 0;
    }
  }, [events.length]);

  // ── Side effect: sync stream subtasks into reducer ──────
  // Deep comparison: always sync when streamSubtasks changes,
  // not just when length changes.
  const prevStreamSubtasksRef = useRef<SubtaskItem[]>([]);
  useEffect(() => {
    if (streamSubtasks.length === 0) return;

    const prev = prevStreamSubtasksRef.current;
    const changed =
      prev.length !== streamSubtasks.length ||
      streamSubtasks.some((st, i) => {
        const p = prev[i];
        return !p || p.id !== st.id || p.status !== st.status || p.assignedWorker !== st.assignedWorker;
      });

    if (changed) {
      prevStreamSubtasksRef.current = streamSubtasks;
      dispatch({type: 'SET_SUBTASKS', subtasks: streamSubtasks});
    }
  }, [streamSubtasks]);

  // ── Side effect: clear offline timers on unmount ────────
  useEffect(() => {
    return () => {
      for (const tid of state.offlineTimerIds) {
        clearTimeout(tid);
      }
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Helper: add a single message ───────────────────────
  const addMessage = useCallback((msg: ChatMessage) => {
    dispatch({type: 'ADD_MESSAGES', messages: [msg]});
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
      }));
      dispatch({type: 'SET_SUBTASKS', subtasks: items});
      // Also update the stream hook's internal map
      setSubtasksFromSubmit(protoSubtasks, taskProto);
    },
    [setSubtasksFromSubmit],
  );

  // ── Helper: update a single subtask status ─────────────
  const updateSubtask = useCallback(
    (subtaskId: string, status: SubtaskStatusType) => {
      dispatch({type: 'UPDATE_SUBTASK_STATUS', subtaskId, status});
      // Also update the stream hook for consistency
      updateSubtaskStatus(subtaskId, status);
    },
    [updateSubtaskStatus],
  );

  // ── Calculate ChatLog visible lines ────────────────────
  const visibleLines = Math.max(
    5,
    (stdout?.rows ?? 24) - 8, // header(2) + separator(1) + input(1) + status(1) + borders(3)
  );

  // ── Build scroll command for ChatLog ────────────────────
  const scrollCommand: ScrollCommand | undefined = state.scrollDirection
    ? {direction: state.scrollDirection, lines: state.scrollLines, tick: state.scrollTick}
    : undefined;

  // ── Handle task submission ─────────────────────────────
  const handleSubmit = useCallback(
    async (description: string) => {
      // Add user message
      addMessage(createUserMessage(description));
      dispatch({type: 'ADD_INPUT_HISTORY', text: description});

      // Clear offline timers from previous task
      for (const tid of state.offlineTimerIds) {
        clearTimeout(tid);
      }
      dispatch({type: 'CLEAR_OFFLINE_TIMERS'});

      // Check if gRPC is connected
      if (connectionState !== 'connected' || !client) {
        addMessage(
          createSystemMessage('gRPC server not connected. Using offline mode.', {
            color: 'yellow',
          }),
        );
        addMessage(
          createSystemMessage('Press Ctrl+R to reconnect, or start the server with: cargo run -p uc-grpc-server', {
            dim: true,
          }),
        );

        // Offline fallback: simulate locally
        simulateOfflineSubmit(
          description,
          addMessage,
          applySubmitResponse,
          updateSubtask,
          dispatch,
        );
        return;
      }

      // Clear previous task state
      clearStreamTask();
      dispatch({type: 'CLEAR_TASK'});
      addMessage(
        createSystemMessage(`Submitting task: ${description}`, {bold: true}),
      );
      addMessage(
        createSystemMessage('Decomposing via Orchestrator...', {dim: true}),
      );

      // Submit via gRPC
      const response = await grpcSubmitTask({
        description,
        projectId: 'default',
      });

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
    [connectionState, client, addMessage, grpcSubmitTask, clearStreamTask, applySubmitResponse, updateSubtask, state.offlineTimerIds],
  );

  // ── Global keyboard handler ────────────────────────────
  useInput((input, key) => {
    // ── Global shortcuts (work in any pane) ────────────
    if (key.ctrl && (input === 'c' || input === 'q')) {
      exit();
      return;
    }

    if (key.ctrl && input === 'r') {
      reconnect();
      return;
    }

    // ── Tab: cycle panes (only when NOT in input mode) ────
    // In input mode, Tab inserts spaces for indentation
    if (key.tab && !key.shift && state.selectedPane !== 'input') {
      const panes: SelectedPane[] = ['input', 'chat', 'subtask'];
      const next = panes[(panes.indexOf(state.selectedPane) + 1) % panes.length];
      dispatch({type: 'SET_SELECTED_PANE', pane: next});
      return;
    }

    // ── Pane-specific shortcuts ─────────────────────────
    switch (state.selectedPane) {
      case 'input': {
        // Ctrl+P: pause/resume task
        if (key.ctrl && input === 'p') {
          if (state.activeTaskId) {
            const hasInProgress = state.subtasks.some((s) => s.status === 'in_progress');
            if (hasInProgress) {
              pauseTask({taskId: state.activeTaskId});
              addMessage(createSystemMessage(`Pausing task: ${state.activeTaskId.slice(0, 8)}...`, {color: 'yellow'}));
            } else {
              resumeTask({taskId: state.activeTaskId});
              addMessage(createSystemMessage(`Resuming task: ${state.activeTaskId.slice(0, 8)}...`, {color: 'cyan'}));
            }
          }
          return;
        }
        break;
      }

      case 'chat': {
        // PageUp: scroll up
        if (key.pageUp) {
          dispatch({type: 'SCROLL_UP', lines: visibleLines});
          return;
        }
        // PageDown: scroll down
        if (key.pageDown) {
          dispatch({type: 'SCROLL_DOWN', lines: visibleLines});
          return;
        }
        // Up arrow: scroll up one line
        if (key.upArrow) {
          dispatch({type: 'SCROLL_UP', lines: 1});
          return;
        }
        // Down arrow: scroll down one line
        if (key.downArrow) {
          dispatch({type: 'SCROLL_DOWN', lines: 1});
          return;
        }
        // Ctrl+L: clear log
        if (key.ctrl && input === 'l') {
          dispatch({type: 'CLEAR_LOG'});
          return;
        }
        // Ctrl+F: cycle event filter
        if (key.ctrl && input === 'f') {
          dispatch({type: 'SET_EVENT_FILTER', filter: nextEventFilter(state.eventFilter)});
          return;
        }
        break;
      }

      case 'subtask': {
        // Reserved for future navigation
        break;
      }
    }
  });

  // ── Derive display info ────────────────────────────────
  const serverAddr = process.env.GRPC_SERVER_ADDR ?? 'localhost:50051';
  const workerId = connectionState === 'connected' ? 'grpc-worker' : 'offline';
  const backend = connectionState === 'connected' ? 'grpc' : 'disconnected';
  const mode = connectionState === 'connected' ? 'grpc live' : 'offline demo';
  const terminalWidth = stdout?.columns ?? 80;

  // ── Symbols (auto-detect unicode/ascii) ─────────────────
  const S = getSymbols(state.symbolMode);

  // ── Responsive layout ──────────────────────────────────
  // >=100 cols: dual pane (Chat + Subtasks)
  // 80-99 cols: dual pane but compressed right
  // <80 cols: single pane, Tab switches
  const isNarrow = terminalWidth < 80;
  const isCompressed = terminalWidth >= 80 && terminalWidth < 100;
  const showDualPane = !isNarrow;

  // SubtaskTree max width based on terminal
  const subtaskMaxWidth = isCompressed ? 25 : 40;

  // In narrow mode, only show the selected pane
  const showChat = showDualPane || state.selectedPane === 'chat';
  const showSubtasks = showDualPane || state.selectedPane === 'subtask';

  // ── Render ─────────────────────────────────────────────
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" padding={0}>
      {/* ── Header ─────────────────────────────────────── */}
      <Box paddingX={1}>
        <Text bold color="cyan">{'UC'}</Text>
        <Text>  </Text>
        <Text bold>UltimateCoders</Text>
        <Text dimColor> v{VERSION}</Text>
        <Text>  </Text>
        {connectionState === 'connected' && <Text color="green">{S.connected}</Text>}
        {connectionState === 'connecting' && <Text color="yellow">{S.connecting}</Text>}
        {connectionState === 'disconnected' && <Text color="red">{S.disconnected}</Text>}
        {connectionState === 'error' && <Text color="red">{S.error}</Text>}
        <Text dimColor>
          {connectionState === 'connected' ? ' connected' : ''}
          {connectionState === 'connecting' ? ' connecting...' : ''}
          {connectionState === 'disconnected' ? ' offline' : ''}
          {connectionState === 'error' ? ' error' : ''}
        </Text>
        {state.activeTaskId && (
          <Text dimColor>{` │ task:${state.activeTaskId.slice(0, 8)}`}</Text>
        )}
        {isNarrow && (
          <Text dimColor>{` │ ${terminalWidth}cols`}</Text>
        )}
      </Box>

      {/* ── Main area: Chat + Subtasks (responsive) ────── */}
      <Box flexDirection="row" flexGrow={1}>
        {showChat && (
          <ChatLog
            messages={state.messages}
            followLog={state.followLog}
            visibleLines={visibleLines}
            isFocused={state.selectedPane === 'chat'}
            eventFilter={state.eventFilter}
            scrollCommand={scrollCommand}
            onSetFollowLog={(follow: boolean) => dispatch({type: 'SET_FOLLOW_LOG', follow})}
          />
        )}
        {showDualPane && showChat && showSubtasks && (
          /* Vertical separator */
          <Box flexDirection="column" justifyContent="center">
            <Text color="gray">{(S.verticalSep + '\n').repeat(visibleLines) + S.verticalSep}</Text>
          </Box>
        )}
        {showSubtasks && (
          <SubtaskTree
            subtasks={state.subtasks}
            taskDescription={task?.description ?? 'No task'}
            progress={state.progress}
            isFocused={state.selectedPane === 'subtask'}
            maxWidth={subtaskMaxWidth}
            symbols={S}
          />
        )}
      </Box>

      {/* ── Separator ──────────────────────────────────── */}
      <Box>
        <Text color="gray">{S.divider.repeat(Math.max(1, terminalWidth - 2))}</Text>
      </Box>

      {/* ── Input ──────────────────────────────────────── */}
      <TaskInput
        onSubmit={handleSubmit}
        isFocused={state.selectedPane === 'input'}
        inputHistory={state.inputHistory}
        historyIndex={state.historyIndex}
        onHistoryIndexChange={(index: number) => dispatch({type: 'SET_HISTORY_INDEX', index})}
      />

      {/* ── Status bar ─────────────────────────────────── */}
      <StatusBar
        workerId={workerId}
        backend={backend}
        progress={state.progress}
        serverAddr={serverAddr}
        connectionState={connectionState}
        isStreaming={isStreaming}
        activeTaskId={state.activeTaskId}
        lastError={state.lastError}
        mode={mode}
        selectedPane={state.selectedPane}
        eventFilter={state.eventFilter}
        terminalWidth={terminalWidth}
      />
    </Box>
  );
};

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
  // Simple local decomposition
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

  // Simulate subtask progress: each pending subtask starts then completes
  const pendingSubtasks = mockSubtasks.filter((s) => s.status === 'Pending');
  pendingSubtasks.forEach((st, idx) => {
    const startDelay = 1000 + idx * 1500;
    const completeDelay = 2500 + idx * 1500;

    // Start subtask
    const startTimer = setTimeout(() => {
      updateSubtask(st.id, 'in_progress');
      addMessage(
        createSystemMessage(
          `◉ Subtask started: ${st.description.slice(0, 50)}`,
          {color: 'cyan'},
        ),
      );
    }, startDelay);
    dispatch({type: 'ADD_OFFLINE_TIMER', timerId: startTimer});

    // Complete subtask
    const completeTimer = setTimeout(() => {
      updateSubtask(st.id, 'completed');
      addMessage(
        createSystemMessage(
          `● Subtask completed: ${st.description.slice(0, 50)}`,
          {color: 'green'},
        ),
      );
    }, completeDelay);
    dispatch({type: 'ADD_OFFLINE_TIMER', timerId: completeTimer});
  });
}

export default App;
