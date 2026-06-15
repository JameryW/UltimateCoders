/**
 * App component - root layout for the UltimateCoders TUI.
 *
 * Layout (unified border frame):
 *   ┌─ UltimateCoders v0.1.0 ─────────────────────────────────┐
 *   │ Chat                    │ Subtasks [0/0 0%]             │
 *   │ > user message          │ ⏳ 1. Analyze code            │
 *   │   system response       │ 🔄 2. Fix bug                │
 *   │                         │ ✅ 3. Write tests             │
 *   ├─────────────────────────┴───────────────────────────────┤
 *   │ > type task description and press Enter...              │
 *   ├─── Worker: grpc │ Backend: grpc │ Progress: 0/0 ───────┤
 *   └─────────────────────────────────────────────────────────┘
 *
 * PR2: Replaced mock data with gRPC-backed real data flow.
 * - useGrpcClient: manages gRPC connection state
 * - useTaskEvents: subscribes to WatchTask event stream
 * - TaskInput submits via gRPC SubmitTask RPC
 * - SubtaskTree updates from WatchTask stream events
 * - ChatLog shows system messages from TaskEvent stream
 */
import React, {useState, useCallback} from 'react';
import {Box, Text, useApp, useInput} from 'ink';
import ChatLog, {
  type ChatMessage,
  createUserMessage,
  createSystemMessage,
} from './ChatLog.js';
import SubtaskTree, {
  type SubtaskItem,
  type SubtaskStatusType,
} from './SubtaskTree.js';
import TaskInput from './TaskInput.js';
import StatusBar from './StatusBar.js';
import useGrpcClient from '../hooks/useGrpcClient.js';
import useTaskEvents from '../hooks/useTaskEvents.js';
import type {SubtaskProto, TaskProto} from '../grpc/types.js';
import {mapSubtaskStatus} from '../grpc/types.js';

// ── App Component ──────────────────────────────────────────

const App: React.FC = () => {
  const {exit} = useApp();

  // gRPC client hook
  const {
    connectionState,
    submitTask: grpcSubmitTask,
    reconnect,
    client,
  } = useGrpcClient();

  // Task events hook (receives stream updates)
  const {
    task,
    subtasks,
    events,
    isStreaming,
    setSubtasksFromSubmit,
    clearTask,
  } = useTaskEvents(client, connectionState);

  // Chat log state
  const [messages, setMessages] = useState<ChatMessage[]>([]);

  // Status state
  const [progress, setProgress] = useState({completed: 0, total: 0});

  // Track active task ID for pause/resume
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  // Handle Ctrl+C / Ctrl+Q to quit, Ctrl+R to reconnect
  useInput((input, key) => {
    if (key.ctrl && (input === 'c' || input === 'q')) {
      exit();
    }
    if (key.ctrl && input === 'r') {
      reconnect();
    }
  });

  // Helper to append messages
  const addMessage = useCallback((msg: ChatMessage) => {
    setMessages((prev) => [...prev, msg]);
  }, []);

  // Derive progress from subtasks
  const updateProgress = useCallback((items: SubtaskItem[]) => {
    const completed = items.filter((s) => s.status === 'completed').length;
    setProgress({completed, total: items.length});
  }, []);

  // Convert proto subtasks to SubtaskItem[] and update state
  const applySubmitResponse = useCallback(
    (protoSubtasks: SubtaskProto[], taskProto?: TaskProto) => {
      setSubtasksFromSubmit(protoSubtasks, taskProto);

      // Convert to SubtaskItem for progress calculation
      const items: SubtaskItem[] = protoSubtasks.map((st, idx) => ({
        id: st.id,
        index: idx + 1,
        description: st.description,
        status: mapSubtaskStatus(st.status),
      }));
      updateProgress(items);
    },
    [setSubtasksFromSubmit, updateProgress],
  );

  // Handle task submission
  const handleSubmit = useCallback(
    async (description: string) => {
      // Add user message
      addMessage(createUserMessage(description));

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
        simulateOfflineSubmit(description, addMessage, applySubmitResponse, setActiveTaskId);
        return;
      }

      // Clear previous task state
      clearTask();
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
        setActiveTaskId(response.taskId);
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
        addMessage(
          createSystemMessage(
            `Failed to submit task: ${response?.error ?? 'unknown error'}`,
            {color: 'red'},
          ),
        );
      }
    },
    [connectionState, client, addMessage, grpcSubmitTask, clearTask, applySubmitResponse],
  );

  // Derive status bar info from connection state
  const workerId = connectionState === 'connected' ? 'grpc-worker' : 'offline';
  const backend = connectionState === 'connected' ? 'grpc' : 'disconnected';

  // Version for header
  const version = '0.1.0';

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" padding={0}>
      {/* ── Header ─────────────────────────────────────── */}
      <Box paddingX={1}>
        <Text bold color="cyan">{'╔═╗╦ ╦╔═╗╔═╗'}</Text>
        <Text>  </Text>
        <Text bold>UltimateCoders</Text>
        <Text dimColor> v{version}</Text>
        <Text>  </Text>
        {connectionState === 'connected' && <Text color="green">●</Text>}
        {connectionState === 'connecting' && <Text color="yellow">◌</Text>}
        {connectionState === 'disconnected' && <Text color="red">○</Text>}
        <Text dimColor>
          {connectionState === 'connected' ? ' connected' : ''}
          {connectionState === 'connecting' ? ' connecting...' : ''}
          {connectionState === 'disconnected' ? ' offline' : ''}
        </Text>
      </Box>

      {/* ── Main area: Chat + Subtasks ─────────────────── */}
      <Box flexDirection="row" flexGrow={1}>
        <ChatLog messages={messages} />
        <SubtaskTree
          subtasks={subtasks}
          taskDescription={task?.description ?? 'No task'}
          progress={progress}
        />
      </Box>

      {/* ── Separator ──────────────────────────────────── */}
      <Box>
        <Text color="gray">{'─'.repeat(80)}</Text>
      </Box>

      {/* ── Input ──────────────────────────────────────── */}
      <TaskInput onSubmit={handleSubmit} />

      {/* ── Status bar ─────────────────────────────────── */}
      <StatusBar workerId={workerId} backend={backend} progress={progress} />
    </Box>
  );
};

/**
 * Offline fallback: simulate task submission without gRPC server.
 * Provides basic functionality when the server is not running.
 */
function simulateOfflineSubmit(
  description: string,
  addMessage: (msg: ChatMessage) => void,
  applySubmitResponse: (subtasks: SubtaskProto[], task?: TaskProto) => void,
  setActiveTaskId: (id: string | null) => void,
): void {
  // Simple local decomposition
  const lines = description
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  if (lines.length <= 1) {
    // Single-task description: create one subtask
    lines.push(description);
  }

  // Use a unique base to avoid ID collisions when Date.now() returns the same value
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

  setActiveTaskId(mockTask.id);
  applySubmitResponse(mockSubtasks, mockTask);

  addMessage(
    createSystemMessage(
      `Decomposed into ${mockSubtasks.length} subtasks (offline)`,
      {color: 'yellow'},
    ),
  );

  // Simulate subtask progress in offline mode
  const pendingSubtasks = mockSubtasks.filter((s) => s.status === 'Pending');
  pendingSubtasks.forEach((st, idx) => {
    setTimeout(() => {
      addMessage(
        createSystemMessage(
          `Subtask started: ${st.description.slice(0, 60)}`,
          {color: 'cyan'},
        ),
      );
    }, 1000 + idx * 1500);

    setTimeout(() => {
      addMessage(
        createSystemMessage(
          `Subtask completed: ${st.description.slice(0, 60)}`,
          {color: 'green'},
        ),
      );
    }, 2500 + idx * 1500);
  });
}

export default App;
