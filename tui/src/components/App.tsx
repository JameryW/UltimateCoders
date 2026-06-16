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
 * Focus model (v2):
 *   focusedArea: which area receives keyboard events (input | chat | subtask)
 *   activeMainPane: which pane occupies the main area in narrow mode (chat | subtask)
 *   Input is always visible. Narrow mode shows activeMainPane in the main area.
 *   Shift+Tab cycles focus. Ctrl+W swaps the main pane. Esc returns to main/input.
 *
 * State management: useReducer (TuiState + tuiReducer).
 * All state transitions go through dispatch — no setState in render.
 * Keyboard: global useInput, shortcuts defined in keymap.ts.
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
import StatusIndicator from './StatusIndicator.js';
import LogoBanner from './LogoBanner.js';
import useGrpcClient from '../hooks/useGrpcClient.js';
import useTaskEvents from '../hooks/useTaskEvents.js';
import {
  tuiReducer,
  INITIAL_TUI_STATE,
  type TuiAction,
  type FocusedArea,
  type ActiveMainPane,
  nextEventFilter,
} from '../reducer.js';
import {formatTaskEvents} from '../formatters.js';
import {getSymbols} from '../symbols.js';
import {getCommandsForArea} from '../keymap.js';
import type {SubtaskProto, TaskProto} from '../grpc/types.js';
import {mapSubtaskStatus} from '../grpc/types.js';

// ── Constants ───────────────────────────────────────────────

const VERSION = '0.1.0';
const SUBMIT_TIMEOUT_MS = 30_000;

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
    lastError: grpcError,
    retryCount,
    nextRetryAt,
    serverAddr,
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
  } = useTaskEvents(client, connectionState, state.activeTaskId);

  // ── Track processed events to avoid re-formatting ───────
  const processedEventCount = useRef(0);

  // ── Submit timeout ref ──────────────────────────────────
  const submitTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── Track whether offline message has been shown ────────
  const hasShownOfflineMsg = useRef(false);

  // ── Track previous connection state for transition detection ──
  const prevConnectionStateRef = useRef(connectionState);

  // ── Reset offline msg flag when connection is restored or re-lost ───
  // PRD P4: show message "when first entering offline or state changes
  // from connected→error". Reset on connected (so next offline shows msg)
  // and also on connected→non-connected transition (so a failed reconnect
  // produces a fresh message on the next submit).
  useEffect(() => {
    const prev = prevConnectionStateRef.current;
    prevConnectionStateRef.current = connectionState;

    // Connection state change notifications in ChatLog
    // Offline/unavailable is expected (not an error) → yellow, not red
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
      // Transition from connected to non-connected: allow next submit to
      // show the offline message again.
      hasShownOfflineMsg.current = false;
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
        if (!p) return true;
        if (p.id !== st.id || p.status !== st.status || p.assignedWorker !== st.assignedWorker || p.errorSummary !== st.errorSummary) return true;
        // Compare dependsOn arrays
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
    (stdout?.rows ?? 24) - 9, // header(2) + separator(1) + statusIndicator(1) + input(1) + status(1) + borders(3)
  );

  // ── Build scroll command for ChatLog ────────────────────
  const scrollCommand: ScrollCommand | undefined = state.scrollDirection
    ? {direction: state.scrollDirection, lines: state.scrollLines, tick: state.scrollTick}
    : undefined;

  // ── Handle task submission ─────────────────────────────
  const handleSubmit = useCallback(
    async (description: string) => {
      // Prevent duplicate submission
      if (state.isSubmitting) return;

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
        // Show offline message only once per offline session
        if (!hasShownOfflineMsg.current) {
          hasShownOfflineMsg.current = true;
          addMessage(
            createSystemMessage(`gRPC server not connected (${serverAddr}). Using offline mode. Ctrl+R to reconnect.`, {
              color: 'yellow',
            }),
          );
        }

        // Offline fallback: simulate locally
        dispatch({type: 'SET_SUBMITTING', submitting: true}); // ponytail: sets startedAt for StatusIndicator
        simulateOfflineSubmit(
          description,
          addMessage,
          applySubmitResponse,
          updateSubtask,
          dispatch,
        );
        return;
      }

      // Mark submitting
      dispatch({type: 'SET_SUBMITTING', submitting: true});

      // Safety timeout: unlock after SUBMIT_TIMEOUT_MS
      submitTimeoutRef.current = setTimeout(() => {
        dispatch({type: 'SET_SUBMITTING', submitting: false});
      }, SUBMIT_TIMEOUT_MS);

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

      // Clear timeout and unlock
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

  // ── Global keyboard handler ────────────────────────────
  useInput((input, key) => {
    // ── Global shortcuts (work in any focus area) ────────
    if (key.ctrl && (input === 'c' || input === 'q')) {
      exit();
      return;
    }

    // Ctrl+R: reconnect gRPC with feedback (deduplicate in connecting state)
    if (key.ctrl && input === 'r') {
      if (connectionState !== 'connecting') {
        addMessage(createSystemMessage(`Reconnecting to ${serverAddr}...`, {color: 'yellow'}));
      }
      reconnect();
      return;
    }

    // Ctrl+P: pause/resume task (global)
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

    // Ctrl+F: cycle event filter (global)
    if (key.ctrl && input === 'f') {
      dispatch({type: 'SET_EVENT_FILTER', filter: nextEventFilter(state.eventFilter)});
      return;
    }

    // Ctrl+W: swap activeMainPane (chat↔subtask)
    if (key.ctrl && input === 'w') {
      dispatch({type: 'SWAP_MAIN_PANE'});
      return;
    }

    // Shift+Tab: cycle focus (input→chat→subtask→input)
    if (key.shift && key.tab) {
      dispatch({type: 'CYCLE_FOCUS'});
      return;
    }

    // Esc: context-dependent escape
    if (key.escape) {
      // Close help overlay first if open
      if (state.helpOverlayOpen) {
        dispatch({type: 'TOGGLE_HELP_OVERLAY'});
        return;
      }
      dispatch({type: 'ESC_TO_MAIN'});
      return;
    }

    // ?: toggle help overlay (when help is open, always close; otherwise only in non-input focus)
    if (input === '?' && !key.ctrl && !key.meta && (state.helpOverlayOpen || state.focusedArea !== 'input')) {
      dispatch({type: 'TOGGLE_HELP_OVERLAY'});
      return;
    }

    // ── Focus-area-specific shortcuts ────────────────────
    switch (state.focusedArea) {
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
        // Home: jump to top (disable follow) — use Ctrl+Home or Ctrl+A
        if ((key as any).home) {
          dispatch({type: 'SCROLL_UP', lines: state.messages.length});
          return;
        }
        // End: jump to bottom (re-enable follow) — use Ctrl+End or Ctrl+E
        if ((key as any).end) {
          dispatch({type: 'SET_FOLLOW_LOG', follow: true});
          return;
        }
        // Ctrl+L: clear log
        if (key.ctrl && input === 'l') {
          dispatch({type: 'CLEAR_LOG'});
          return;
        }
        // Enter: toggle expand/collapse all long messages
        if (key.return) {
          dispatch({type: 'TOGGLE_EXPAND_ALL_MESSAGES'});
          return;
        }
        break;
      }

      case 'subtask': {
        // Up arrow: select previous subtask
        if (key.upArrow) {
          const nextIdx = state.selectedSubtaskIndex <= 0
            ? state.subtasks.length - 1
            : state.selectedSubtaskIndex - 1;
          dispatch({type: 'SELECT_SUBTASK', index: nextIdx});
          return;
        }
        // Down arrow: select next subtask
        if (key.downArrow) {
          const nextIdx = state.subtasks.length === 0
            ? -1
            : (state.selectedSubtaskIndex + 1) % state.subtasks.length;
          dispatch({type: 'SELECT_SUBTASK', index: nextIdx});
          return;
        }
        // Enter: toggle subtask detail
        if (key.return) {
          dispatch({type: 'TOGGLE_SUBTASK_DETAIL'});
          return;
        }
        // Ctrl+T: retry subtask (placeholder)
        if (key.ctrl && input === 't') {
          if (state.selectedSubtaskId) {
            dispatch({type: 'RETRY_SUBTASK', subtaskId: state.selectedSubtaskId});
          }
          return;
        }
        // Home: jump to first subtask
        if ((key as any).home) {
          if (state.subtasks.length > 0) {
            dispatch({type: 'SELECT_SUBTASK', index: 0});
          }
          return;
        }
        // End: jump to last subtask
        if ((key as any).end) {
          if (state.subtasks.length > 0) {
            dispatch({type: 'SELECT_SUBTASK', index: state.subtasks.length - 1});
          }
          return;
        }
        // f: jump to next failed subtask
        if (input === 'f' && !key.ctrl && !key.meta) {
          dispatch({type: 'JUMP_TO_FAILED_SUBTASK'});
          return;
        }
        break;
      }

      case 'input': {
        // Tab: handled by CjkTextInput (indentation)
        // Shift+Tab: handled above (cycle focus)
        // Other input-area keys are handled by CjkTextInput
        break;
      }
    }
  });

  // ── Derive display info ────────────────────────────────
  const workerId = connectionState === 'connected' ? 'grpc-worker' : 'offline';
  const backend = connectionState === 'connected' ? 'grpc' : 'disconnected';
  const mode = connectionState === 'connected' ? 'grpc live' : 'offline demo';
  const terminalWidth = stdout?.columns ?? 80;

  // ── Symbols (auto-detect unicode/ascii) ─────────────────
  const S = getSymbols(state.symbolMode);

  // ── Responsive layout ──────────────────────────────────
  // >=100 cols: dual pane (Chat + Subtasks)
  // 80-99 cols: dual pane but compressed right
  // <80 cols: single pane, shows activeMainPane
  const isNarrow = terminalWidth < 80;
  const isCompressed = terminalWidth >= 80 && terminalWidth < 100;
  const showDualPane = !isNarrow;

  // SubtaskTree max width based on terminal
  const subtaskMaxWidth = isCompressed ? 25 : 40;

  // FIX: In narrow mode, always show the activeMainPane (not focusedArea)
  // This fixes the blank screen when focusedArea=input on narrow terminals
  const showChat = showDualPane || state.activeMainPane === 'chat';
  const showSubtasks = showDualPane || state.activeMainPane === 'subtask';

  // ── Subtask detail (replaces main area when open) ──────
  const showSubtaskDetail = state.subtaskDetailOpen && state.selectedSubtaskId !== null;
  const selectedSubtask = showSubtaskDetail
    ? state.subtasks.find((s) => s.id === state.selectedSubtaskId)
    : null;

  // ── Render ─────────────────────────────────────────────
  // Help overlay: replaces entire UI
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

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" padding={0}>
      {/* ── Header ─────────────────────────────────────── */}
      <LogoBanner
        terminalWidth={terminalWidth}
        brandChar={S.brand}
        version={VERSION}
      />
      <Box paddingX={1}>
        {connectionState === 'connected' && <Text color="green">{S.connected}</Text>}
        {connectionState === 'connecting' && <Text color="yellow">{S.connecting}</Text>}
        {connectionState === 'disconnected' && <Text color="yellow">{S.disconnected}</Text>}
        {connectionState === 'error' && <Text color="yellow">{S.error}</Text>}
        <Text dimColor>
          {connectionState === 'connected' ? ' connected' : ''}
          {connectionState === 'connecting' ? ' connecting...' : ''}
          {connectionState === 'disconnected' ? ' offline' : ''}
          {connectionState === 'error' ? ' offline' : ''}
        </Text>
        {state.activeTaskId && (
          <Text dimColor>{` │ task:${state.activeTaskId.slice(0, 8)}`}</Text>
        )}
        {isNarrow && (
          <Text dimColor>{` │ ${terminalWidth}cols`}</Text>
        )}
      </Box>

      {/* ── Main area: Chat + Subtasks or Subtask Detail (responsive) ── */}
      {showSubtaskDetail && selectedSubtask ? (
        /* Subtask detail replaces the entire main area */
        <Box flexDirection="column" flexGrow={2} paddingX={1}>
          <Box marginBottom={1}>
            <Text bold color="cyan">{'Subtask Detail'}</Text>
            <Text dimColor>{' Esc to close'}</Text>
          </Box>
          <Text bold>{`${selectedSubtask.index}. ${selectedSubtask.description}`}</Text>
          <Box marginTop={1}>
            <Text dimColor>{'Status: '}</Text>
            <Text color={selectedSubtask.status === 'completed' ? 'green' : selectedSubtask.status === 'failed' ? 'red' : selectedSubtask.status === 'in_progress' ? 'cyan' : undefined}>
              {selectedSubtask.status}
            </Text>
          </Box>
          {selectedSubtask.assignedWorker && (
            <Box>
              <Text dimColor>{'Worker: '}</Text>
              <Text>{selectedSubtask.assignedWorker}</Text>
            </Box>
          )}
          {selectedSubtask.dependsOn && selectedSubtask.dependsOn.length > 0 && (
            <Box>
              <Text dimColor>{'Depends on: '}</Text>
              <Text>{selectedSubtask.dependsOn.map((id) => id.slice(0, 8)).join(', ')}</Text>
            </Box>
          )}
          {selectedSubtask.errorSummary && (
            <Box marginTop={1}>
              <Text color="red" bold>{'Error: '}</Text>
              <Text color="red">{selectedSubtask.errorSummary}</Text>
            </Box>
          )}
          {selectedSubtask.status === 'failed' && (
            <Box>
              <Text color="red">{'✗ Ctrl+T retry (coming soon) · f jump to next failed'}</Text>
            </Box>
          )}
          {/* Recent events related to this subtask */}
          {(() => {
            const relatedEvents = state.messages
              .filter((m) => !m.isUser && m.eventType && m.text.includes(selectedSubtask.id.slice(0, 8)))
              .slice(-5);
            if (relatedEvents.length === 0) return null;
            return (
              <Box flexDirection="column" marginTop={1}>
                <Text dimColor bold>{'Recent events:'}</Text>
                {relatedEvents.map((ev) => (
                  <Box key={ev.id}>
                    <Text dimColor>{`[${ev.timestamp}] `}</Text>
                    <Text dimColor>{ev.text.slice(0, 60)}</Text>
                  </Box>
                ))}
              </Box>
            );
          })()}
        </Box>
      ) : (
        <Box flexDirection="row" flexGrow={1}>
          {showChat && (
            <ChatLog
              messages={state.messages}
              followLog={state.followLog}
              visibleLines={visibleLines}
              isFocused={state.focusedArea === 'chat'}
              eventFilter={state.eventFilter}
              scrollCommand={scrollCommand}
              unreadCount={state.unreadCount}
              onSetFollowLog={(follow: boolean) => dispatch({type: 'SET_FOLLOW_LOG', follow})}
              expandAll={state.expandAllMessages}
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
              isFocused={state.focusedArea === 'subtask'}
              maxWidth={subtaskMaxWidth}
              symbols={S}
              selectedIndex={state.selectedSubtaskIndex}
              detailOpen={state.subtaskDetailOpen}
            />
          )}
        </Box>
      )}

      {/* ── Separator ──────────────────────────────────── */}
      <Box>
        <Text color="gray">{S.divider.repeat(Math.max(1, terminalWidth - 2))}</Text>
      </Box>

      {/* ── Status indicator (spinner + elapsed) ─────── */}
      <StatusIndicator
        isSubmitting={state.isSubmitting}
        isStreaming={isStreaming}
        startedAt={state.startedAt}
      />

      {/* ── Input (fixed at bottom) ──────────────────── */}
      <TaskInput
        onSubmit={handleSubmit}
        isFocused={state.focusedArea === 'input'}
        isSubmitting={state.isSubmitting}
        inputHistory={state.inputHistory}
        historyIndex={state.historyIndex}
        onHistoryIndexChange={(index: number) => dispatch({type: 'SET_HISTORY_INDEX', index})}
        isOffline={connectionState !== 'connected'}
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
        lastError={grpcError}
        mode={mode}
        focusedArea={state.focusedArea}
        activeMainPane={state.activeMainPane}
        eventFilter={state.eventFilter}
        terminalWidth={terminalWidth}
        retryCount={retryCount}
        nextRetryAt={nextRetryAt}
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
