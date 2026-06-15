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
 *
 * Scroll offset is managed locally because it must be relative to the
 * filtered message list. The reducer tracks followLog (auto-follow state)
 * and issues scroll commands (via scrollCommand prop) that ChatLog applies
 * to its local offset.
 *
 * Message format:
 * - User input: [HH:MM:SS] > message  (cyan > prefix)
 * - System output: [HH:MM:SS] message  (with optional color)
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
}

function formatTime(): string {
  const now = new Date();
  return now.toTimeString().slice(0, 8);
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

const ChatMessageItem: React.FC<{msg: ChatMessage}> = ({msg}) => {
  if (msg.isUser) {
    return (
      <Box>
        <Text dimColor>{`[${msg.timestamp}] `}</Text>
        <Text bold color="cyan">
          {'> '}
        </Text>
        <Text>{msg.text}</Text>
      </Box>
    );
  }

  return (
    <Box>
      <Text dimColor>{`[${msg.timestamp}] `}</Text>
      <Text
        color={msg.color}
        bold={msg.bold}
        dimColor={msg.dim}
      >
        {msg.text}
      </Text>
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
}) => {
  // Local scroll offset into the filtered message list.
  const [localOffset, setLocalOffset] = useState(0);

  // Track last processed scroll command tick to avoid double-processing
  const lastScrollTick = useRef(0);

  // Track previous eventFilter to reset scroll when filter changes
  const prevFilterRef = useRef(eventFilter);

  // Apply event filter
  const filteredMessages = eventFilter === 'all'
    ? messages
    : messages.filter((msg) => {
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

  return (
    <Box flexDirection="column" flexGrow={2} paddingX={1}>
      <Box marginBottom={1}>
        <Text bold color="cyan">{'Chat'}</Text>
        {isFocused && <Text dimColor>{' [focused]'}</Text>}
        {eventFilter !== 'all' && (
          <Text color="yellow">{` [filter:${eventFilterLabel(eventFilter)}]`}</Text>
        )}
        {followIndicator && <Text color="yellow">{followIndicator}</Text>}
        {scrollIndicator && <Text dimColor>{scrollIndicator}</Text>}
      </Box>
      {visibleMessages.map((msg) => (
        <ChatMessageItem key={msg.id} msg={msg} />
      ))}
    </Box>
  );
};

export default ChatLog;
