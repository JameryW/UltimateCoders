/**
 * App component - root layout for the UltimateCoders TUI.
 *
 * Layout:
 *   ┌─ LogoHeader ──────────────────────────────────────────┐
 *   │ ┌─ ChatLog (2fr) ──────┐ ┌─ SubtaskTree (1fr) ─────┐ │
 *   │ │                       │ │                          │ │
 *   │ └───────────────────────┘ └──────────────────────────┘ │
 *   ├─ TaskInput ────────────────────────────────────────────┤
 *   └─ StatusBar ────────────────────────────────────────────┘
 *
 * State management uses React useState for now (mock data).
 * gRPC integration will be added in PR2.
 */
import React, {useState, useCallback} from 'react';
import {Box, useApp, useInput} from 'ink';
import LogoHeader from './LogoHeader.js';
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

// ── Mock data for PR1 ──────────────────────────────────────

const MOCK_SUBTASKS: SubtaskItem[] = [
  {id: 'st-1', index: 1, description: 'Analyze codebase structure', status: 'completed'},
  {id: 'st-2', index: 2, description: 'Identify bug in auth module', status: 'in_progress'},
  {id: 'st-3', index: 3, description: 'Write unit tests for fix', status: 'pending'},
  {id: 'st-4', index: 4, description: 'Update documentation', status: 'pending'},
];

// ── App Component ──────────────────────────────────────────

const App: React.FC = () => {
  const {exit} = useApp();

  // Chat log state
  const [messages, setMessages] = useState<ChatMessage[]>([]);

  // Subtask state
  const [subtasks, setSubtasks] = useState<SubtaskItem[]>([]);
  const [taskDescription, setTaskDescription] = useState<string>('No task');

  // Status state
  const [workerId] = useState('local-sandbox-worker');
  const [backend] = useState('subprocess');
  const [progress, setProgress] = useState({completed: 0, total: 0});

  // Handle Ctrl+C / Ctrl+Q to quit
  useInput((input, key) => {
    if (key.ctrl && (input === 'c' || input === 'q')) {
      exit();
    }
  });

  // Helper to append messages
  const addMessage = useCallback((msg: ChatMessage) => {
    setMessages((prev) => [...prev, msg]);
  }, []);

  // Simulate task submission with mock data (PR1: no gRPC yet)
  const handleSubmit = useCallback(
    (description: string) => {
      // Add user message
      addMessage(createUserMessage(description));

      // Clear previous subtasks
      setSubtasks([]);
      setTaskDescription(description);

      // Simulate task submission
      addMessage(
        createSystemMessage(`Submitting task: ${description}`, {bold: true}),
      );
      addMessage(
        createSystemMessage('Decomposing via Claude Code...', {dim: true}),
      );

      // Simulate decomposition result after a brief delay
      setTimeout(() => {
        const mockSubtasks: SubtaskItem[] = MOCK_SUBTASKS.map((st) => ({
          ...st,
          id: `st-${Date.now()}-${st.index}`,
          status: (st.index <= 2 ? 'completed' : 'pending') as SubtaskStatusType,
        }));
        setSubtasks(mockSubtasks);
        setTaskDescription(description);

        const completed = mockSubtasks.filter(
          (s) => s.status === 'completed',
        ).length;
        setProgress({completed, total: mockSubtasks.length});

        addMessage(
          createSystemMessage(
            `Decomposed into ${mockSubtasks.length} subtasks`,
            {color: 'cyan'},
          ),
        );
        addMessage(
          createSystemMessage(
            `Executing ${mockSubtasks.length} subtasks...`,
            {color: 'cyan'},
          ),
        );

        // Simulate subtask progress
        simulateProgress(mockSubtasks, addMessage, setSubtasks, setProgress);
      }, 500);
    },
    [addMessage],
  );

  return (
    <Box flexDirection="column" height="100%">
      <LogoHeader />
      <Box flexDirection="row" flexGrow={1}>
        <ChatLog messages={messages} />
        <SubtaskTree
          subtasks={subtasks}
          taskDescription={taskDescription}
          progress={progress}
        />
      </Box>
      <TaskInput onSubmit={handleSubmit} />
      <StatusBar workerId={workerId} backend={backend} progress={progress} />
    </Box>
  );
};

/**
 * Simulate subtask progress updates (mock for PR1).
 * In PR2 this will be replaced by gRPC event streaming.
 */
function simulateProgress(
  initialSubtasks: SubtaskItem[],
  addMessage: (msg: ChatMessage) => void,
  setSubtasks: React.Dispatch<React.SetStateAction<SubtaskItem[]>>,
  setProgress: React.Dispatch<React.SetStateAction<{completed: number; total: number}>>,
): void {
  const pending = initialSubtasks.filter((s) => s.status === 'pending');

  pending.forEach((subtask, idx) => {
    // Start subtask
    setTimeout(() => {
      setSubtasks((prev) =>
        prev.map((s) =>
          s.id === subtask.id ? {...s, status: 'in_progress' as SubtaskStatusType} : s,
        ),
      );
      addMessage(
        createSystemMessage(
          `Subtask started: ${subtask.description.slice(0, 60)}`,
          {color: 'cyan'},
        ),
      );
    }, 1000 + idx * 1500);

    // Complete subtask
    setTimeout(() => {
      setSubtasks((prev) =>
        prev.map((s) =>
          s.id === subtask.id ? {...s, status: 'completed' as SubtaskStatusType} : s,
        ),
      );
      addMessage(
        createSystemMessage(
          `Subtask completed: ${subtask.description.slice(0, 60)}`,
          {color: 'green'},
        ),
      );

      // Update progress
      setSubtasks((current) => {
        const completed = current.filter(
          (s) => s.status === 'completed',
        ).length;
        setProgress({completed, total: current.length});

        // Check if all done
        if (completed === current.length) {
          addMessage(
            createSystemMessage('All subtasks completed!', {bold: true, color: 'green'}),
          );
        }

        return current;
      });
    }, 2500 + idx * 1500);
  });
}

export default App;
