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
import React, {useReducer, useCallback, useEffect, useRef} from 'react';
import {Box, Text, useApp, useInput, useStdout, useStdin} from 'ink';
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
import type {SubtaskProto, TaskProto} from '../grpc/types.js';
import {mapSubtaskStatus} from '../grpc/types.js';
import {parseCommand, isCommandInput, formatHelpText, matchCommands, COMMANDS} from '../commands.js';
import type {SlashCommand} from '../commands.js';
import {buildTaskListText} from '../task-list-utils.js';

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

  const parts: string[] = [];
  parts.push(`${completed}/${total}`);
  if (completed === total) {
    parts.push('✅');
  } else {
    if (inProgress > 0) parts.push(`${inProgress} ⏳`);
    if (failed > 0) parts.push(`${failed} ✗`);
  }
  return `📋 ${parts.join(' │ ')}`;
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

  // ── Track whether subtask summary message has been inserted ──
  const hasSubtaskSummaryRef = useRef(false);

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

  // ── Calculate ChatLog visible lines ────────────────────
  // logo height adapts to terminal width (6 / 1 / 0) + separator(1) + statusIndicator(1) + input(1) + status(1) + borders(2)
  const logoHeight = getLogoHeight(terminalWidth);
  const fixedLines = logoHeight + 6;
  const visibleLines = Math.max(
    5,
    (stdout?.rows ?? 24) - fixedLines,
  );

  // ── Build scroll command for ChatLog ────────────────────
  const scrollCommand: ScrollCommand | undefined = state.scrollDirection
    ? {direction: state.scrollDirection, lines: state.scrollLines, tick: state.scrollTick}
    : undefined;

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
          subtasks: state.subtasks, isStreaming,
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

  // ── Global keyboard handler ────────────────────────────
  useInput((input, key) => {
    // ── Global shortcuts (work in any focus area) ────────
    if (key.ctrl && (input === 'c' || input === 'q')) {
      exit();
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

    // Ctrl+P: pause/resume task
    if (key.ctrl && input === 'p') {
      if (state.activeTaskId) {
        // ponytail: check task status (not subtask status) for pause/resume decision
        const taskStatus = task?.status?.toLowerCase();
        if (taskStatus === 'in_progress' || taskStatus === 'planning') {
          pauseTask({taskId: state.activeTaskId});
          addMessage(createSystemMessage(`Pausing task: ${state.activeTaskId.slice(0, 8)}...`, {color: 'yellow'}));
        } else if (taskStatus === 'paused') {
          resumeTask({taskId: state.activeTaskId});
          addMessage(createSystemMessage(`Resuming task: ${state.activeTaskId.slice(0, 8)}...`, {color: 'cyan'}));
        } else {
          addMessage(createSystemMessage(`Cannot pause/resume task in ${taskStatus ?? 'unknown'} state`, {color: 'gray'}));
        }
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
    if ((key.shift && key.tab) || (key.ctrl && input === 'w')) {
      dispatch({type: 'CYCLE_FOCUS'});
      return;
    }

    // Esc: context-dependent escape
    if (key.escape) {
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
        // If no selection yet, select first item; otherwise wrap
        const nextIdx = state.selectedSubtaskIndex < 0
          ? 0
          : state.selectedSubtaskIndex >= state.subtasks.length - 1
            ? 0
            : state.selectedSubtaskIndex + 1;
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
        // Enter handled by ChatLog internally (per-message expand)
        break;
      }

      case 'input': {
        break;
      }
    }
  });

  // ── Raw input handler for Home/End (Ink v5 Key type lacks these) ──
  // Ink's useInput doesn't expose home/end. Detect raw escape sequences instead.
  // Home: \x1b[H or \x1b[1~  |  End: \x1b[F or \x1b[4~
  // ponytail: remove this workaround when upgrading to Ink v6.6+ (needs React 19)
  const {stdin: rawStdin, internal_eventEmitter: rawEmitter} = useStdin();
  useEffect(() => {
    if (!rawStdin || !rawEmitter) return;
    const onRawInput = (data: string) => {
      if (state.focusedArea === 'chat') {
        // Home sequences: ESC[H or ESC[1~
        if (data === '\x1b[H' || data === '\x1b[1~') {
          dispatch({type: 'SCROLL_UP', lines: state.messages.length});
        }
        // End sequences: ESC[F or ESC[4~
        else if (data === '\x1b[F' || data === '\x1b[4~') {
          dispatch({type: 'SET_FOLLOW_LOG', follow: true});
        }
      }
    };
    rawEmitter.on('input', onRawInput);
    return () => {
      rawEmitter.off('input', onRawInput);
    };
  }, [rawStdin, rawEmitter, state.focusedArea, state.messages.length, dispatch]);

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
        />
      </Box>
    );
  }

  // ── Render: Main layout (single-column vertical) ──────
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" padding={0}>
      {/* ── Logo banner (full pixel-game UC logo) ── */}
      <LogoBanner
        terminalWidth={terminalWidth}
        brandChar={S.brand}
        version={VERSION}
      />

      {/* ── ChatLog (full-width, single column) ──────── */}
      <ChatLog
        messages={state.messages}
        followLog={state.followLog}
        visibleLines={visibleLines}
        isFocused={state.focusedArea === 'chat'}
        eventFilter={state.eventFilter}
        scrollCommand={scrollCommand}
        unreadCount={state.unreadCount}
        onSetFollowLog={(follow: boolean) => dispatch({type: 'SET_FOLLOW_LOG', follow})}
        terminalWidth={terminalWidth}
      />

      {/* ── Separator ────────────────────────────────── */}
      <Box>
        <Text color="gray">{S.divider.repeat(Math.max(1, terminalWidth - 2))}</Text>
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
        onHistoryIndexChange={(index: number) => dispatch({type: 'SET_HISTORY_INDEX', index})}
        isOffline={connectionState !== 'connected'}
        commandSuggestions={state.commandSuggestions}
        onValueChange={(val: string) => {
          const trimmed = val.trimStart();
          if (trimmed.startsWith('/')) {
            const prefix = trimmed.slice(1).split(' ')[0];
            const matched = matchCommands(prefix);
            dispatch({type: 'SET_COMMAND_SUGGESTIONS', suggestions: matched.length > 0 ? matched : null});
          } else if (state.commandSuggestions) {
            dispatch({type: 'SET_COMMAND_SUGGESTIONS', suggestions: null});
          }
        }}
      />

      {/* ── Status bar ──────────────────────────────── */}
      <StatusBar
        workerId={workerId}
        backend={backend}
        progress={state.progress}
        connectionState={connectionState}
        isStreaming={isStreaming}
        focusedArea={state.focusedArea}
        terminalWidth={terminalWidth}
        retryCount={retryCount}
        nextRetryAt={nextRetryAt}
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
  },
): void {
  const {addMessage, dispatch, connectionState, client, serverAddr, reconnect, exit, listTasks, activeTaskId, subtasks, isStreaming} = deps;

  switch (command.name) {
    case 'help':
      addMessage(createSystemMessage(formatHelpText(), {eventType: 'command_result'}));
      break;
    case 'clear':
      dispatch({type: 'CLEAR_LOG'});
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
