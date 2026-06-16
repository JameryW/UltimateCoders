/**
 * ChatLog component - conversational-style message log with window slicing.
 *
 * Renders chat messages in the left panel. No border — the parent
 * App component provides the unified outer frame.
 *
 * Features:
 * - Window slicing: only renders filteredMessages[offset..offset+visibleLines]
 * - Auto-follow: when followLog=true, automatically scrolls to bottom
 * - Scroll indicator: shows position in message history
 * - Event filtering: filter by event type (task/subtask/tool/error)
 * - Unread count: when followLog is off, shows "+N new" in header
 * - Home/End: jump to top/bottom (handled by parent via scrollCommand)
 *
 * Scroll offset is managed locally because it must be relative to the
 * filtered message list. The reducer tracks followLog (auto-follow state)
 * and issues scroll commands (via scrollCommand prop) that ChatLog applies
 * to its local offset.
 *
 * Message format:
 * - User input: [HH:MM] > message  (cyan > prefix, bold)
 * - System output: [HH:MM] message  (with optional color)
 */
import React, {useState, useEffect, useRef} from 'react';
import {Box, Text} from 'ink';
import type {EventFilter} from '../reducer.js';
import {eventFilterLabel} from '../reducer.js';

export interface ChatMessage {
  id: string;
  timestamp: string;
  text: string;
  isUser: boolean;
  color?: string;
  bold?: boolean;
  dim?: boolean;
  /** Event type for filtering (null for user messages). */
  eventType?: string;
}

/** Scroll command issued by the parent. Monotonically increasing tick ensures
 *  each command is processed exactly once even if the values are identical. */
export interface ScrollCommand {
  direction: 'up' | 'down';
  lines: number;
  tick: number;
}

export interface ChatLogProps {
  messages: ChatMessage[];
  /** Whether auto-follow is active. */
  followLog: boolean;
  /** Number of visible lines for the message area. */
  visibleLines: number;
  /** Whether the chat pane is currently focused. */
  isFocused: boolean;
  /** Current event type filter. */
  eventFilter?: EventFilter;
  /** Scroll command from parent. Processed once per tick. */
  scrollCommand?: ScrollCommand;
  /** Callback to update followLog in reducer. */
  onSetFollowLog?: (follow: boolean) => void;
  /** Unread message count (when followLog is off). */
  unreadCount?: number;
  /** Whether all collapsed messages should be expanded (toggled by Enter in chat focus). */
  expandAll?: boolean;
}

function formatTime(): string {
  const now = new Date();
  return now.toTimeString().slice(0, 5); // HH:MM instead of HH:MM:SS
}

/** Helper to create a ChatMessage object. */
export function createUserMessage(text: string): ChatMessage {
  return {
    id: `user-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    timestamp: formatTime(),
    text,
    isUser: true,
  };
}

export function createSystemMessage(
  text: string,
  options?: {color?: string; bold?: boolean; dim?: boolean},
): ChatMessage {
  return {
    id: `sys-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    timestamp: formatTime(),
    text,
    isUser: false,
    ...options,
  };
}

/**
 * Pure function: filter messages by event type.
 * Exported for independent testing (filterMessages.test.ts).
 */
export function filterMessages(messages: ChatMessage[], eventFilter: EventFilter): ChatMessage[] {
  if (eventFilter === 'all') return messages;
  return messages.filter((msg) => {
    if (msg.isUser) return true; // Always show user messages
    if (!msg.eventType) return true; // Show messages without eventType
    // Map event types to filter categories
    const et = msg.eventType;
    if (eventFilter === 'task') return et.startsWith('task_');
    if (eventFilter === 'subtask') return et.startsWith('subtask_');
    if (eventFilter === 'tool') return et.startsWith('tool_');
    if (eventFilter === 'error') return et === 'subtask_failed' || et === 'task_failed';
    return true;
  });
}

const COLLAPSE_THRESHOLD = 3;

/** Tool-related event types that default to collapsed (summary only). */
const TOOL_EVENT_TYPES = new Set(['tool_call', 'tool_result', 'file_modified']);

/** Event type icon prefix mapping for visual scanning. */
export const EVENT_ICONS: Record<string, string> = {
  task_submitted: '📋',
  task_completed: '✓',
  task_failed: '✗',
  subtask_assigned: '◌',
  subtask_started: '▶',
  subtask_completed: '✓',
  subtask_failed: '✗',
  tool_call: '⚙',
  tool_result: '📄',
  file_modified: '✏',
};

/**
 * Get icon prefix for an event type. Returns empty string for unknown types.
 * Exported for testing.
 */
export function getEventIcon(eventType?: string): string {
  if (!eventType) return '';
  return EVENT_ICONS[eventType] ? `${EVENT_ICONS[eventType]} ` : '';
}

const ChatMessageItem: React.FC<{msg: ChatMessage; expandAll?: boolean}> = ({msg, expandAll}) => {
  const [expanded, setExpanded] = useState(false);
  const lines = msg.text.split('\n');

  // Tool events always start collapsed (show summary only)
  const isToolEvent = !msg.isUser && !!msg.eventType && TOOL_EVENT_TYPES.has(msg.eventType);
  // Non-tool long messages collapse after COLLAPSE_THRESHOLD
  const isLong = !msg.isUser && (isToolEvent || lines.length > COLLAPSE_THRESHOLD);

  // When expandAll changes, sync local expanded state
  useEffect(() => {
    setExpanded(expandAll ?? false);
  }, [expandAll]);

  // Auto-color status change events based on eventType
  const statusColor = msg.color ?? (
    msg.eventType === 'subtask_completed' || msg.eventType === 'task_completed'
      ? 'green'
      : msg.eventType === 'subtask_failed' || msg.eventType === 'task_failed'
        ? 'red'
        : msg.eventType === 'subtask_started' || msg.eventType === 'subtask_assigned'
          ? 'cyan'
          : undefined
  );

  // Collapsed messages show only the first line
  const visibleLines = (isLong && !expanded)
    ? lines.slice(0, 1)
    : lines;

  const renderText = () => {
    if (msg.isUser) {
      return (
        <>
          <Text bold color="cyan">{'▎ '}</Text>
          <Text bold color="white">{visibleLines[0]}</Text>
          {visibleLines.slice(1).map((line, i) => (
            <Text key={i}>{'\n' + line}</Text>
          ))}
        </>
      );
    }
    // System/tool messages
    return (
      <Text
        color={statusColor}
        bold={msg.bold}
        dimColor={msg.dim ? true : isToolEvent}
      >
        {msg.eventType ? getEventIcon(msg.eventType) : ''}{visibleLines.join('\n')}
      </Text>
    );
  };

  const collapsedCount = isLong && !expanded ? lines.length - 1 : 0;

  return (
    <Box flexDirection="column">
      <Box>
        <Text dimColor>{`[${msg.timestamp}] `}</Text>
        {renderText()}
      </Box>
      {collapsedCount > 0 && (
        <Box marginLeft={7}>
          <Text dimColor>{isToolEvent ? `[+${collapsedCount} lines — Enter to expand]` : `[+${collapsedCount} more]`}</Text>
        </Box>
      )}
    </Box>
  );
};

const ChatLog: React.FC<ChatLogProps> = ({
  messages,
  followLog,
  visibleLines,
  isFocused,
  eventFilter = 'all',
  scrollCommand,
  onSetFollowLog,
  unreadCount = 0,
  expandAll = false,
}) => {
  // Local scroll offset into the filtered message list.
  const [localOffset, setLocalOffset] = useState(0);

  // Track last processed scroll command tick to avoid double-processing
  const lastScrollTick = useRef(0);

  // Track previous eventFilter to reset scroll when filter changes
  const prevFilterRef = useRef(eventFilter);

  // Apply event filter using pure function
  const filteredMessages = filterMessages(messages, eventFilter);

  const totalMessages = filteredMessages.length;
  const maxOffset = Math.max(0, totalMessages - visibleLines);

  // When filter changes, snap to bottom (re-enable follow)
  useEffect(() => {
    if (eventFilter !== prevFilterRef.current) {
      prevFilterRef.current = eventFilter;
      setLocalOffset(0);
      if (!followLog) {
        onSetFollowLog?.(true);
      }
    }
  }, [eventFilter, followLog, onSetFollowLog]);

  // Process scroll commands from parent
  useEffect(() => {
    if (!scrollCommand || scrollCommand.tick <= lastScrollTick.current) return;
    lastScrollTick.current = scrollCommand.tick;

    if (scrollCommand.direction === 'up') {
      setLocalOffset((prev) => Math.max(0, prev - scrollCommand.lines));
      onSetFollowLog?.(false);
    } else {
      setLocalOffset((prev) => {
        const newOffset = Math.min(maxOffset, prev + scrollCommand.lines);
        if (newOffset >= maxOffset) {
          onSetFollowLog?.(true);
        }
        return newOffset;
      });
    }
  }, [scrollCommand, maxOffset, onSetFollowLog]);

  // When followLog is true, offset is always at the bottom of the filtered list
  const effectiveOffset = followLog ? maxOffset : Math.min(localOffset, maxOffset);

  if (totalMessages === 0) {
    return (
      <Box flexDirection="column" flexGrow={2} paddingX={1}>
        <Box marginBottom={1}>
          <Text bold color="cyan">{'Chat'}</Text>
          {isFocused && <Text dimColor>{' [focused]'}</Text>}
        </Box>
        <Text dimColor>No messages yet. Type a task below.</Text>
      </Box>
    );
  }

  const endIdx = Math.min(effectiveOffset + visibleLines, totalMessages);
  const visibleMessages = filteredMessages.slice(effectiveOffset, endIdx);

  // Scroll indicator
  const atTop = effectiveOffset === 0;
  const atBottom = effectiveOffset >= maxOffset;
  const scrollIndicator = atTop && atBottom
    ? ''
    : atTop
      ? ' ↓'
      : atBottom
        ? ' ↑'
        : ` ↑${effectiveOffset + 1}-${endIdx}/${totalMessages}↓`;

  // Follow indicator
  const followIndicator = followLog ? '' : ' [paused]';

  // Unread indicator
  const unreadIndicator = unreadCount > 0 ? ` [+${unreadCount} new]` : '';

  // Filter count indicator
  const filterIndicator = eventFilter !== 'all'
    ? ` [filter:${eventFilterLabel(eventFilter)} ${totalMessages}/${messages.length}]`
    : '';

  return (
    <Box flexDirection="column" flexGrow={2} paddingX={1}>
      <Box marginBottom={1}>
        <Text bold color="cyan">{'Chat'}</Text>
        {isFocused && <Text dimColor>{' [focused]'}</Text>}
        {filterIndicator && <Text color="yellow">{filterIndicator}</Text>}
        {followIndicator && <Text color="yellow">{followIndicator}</Text>}
        {unreadIndicator && <Text color="red" bold>{unreadIndicator}</Text>}
        {scrollIndicator && <Text dimColor>{scrollIndicator}</Text>}
      </Box>
      {visibleMessages.map((msg) => (
        <ChatMessageItem key={msg.id} msg={msg} expandAll={expandAll} />
      ))}
    </Box>
  );
};

export default ChatLog;
