/**
 * ChatLog component - conversational-style message log with window slicing.
 *
 * Renders chat messages in the left panel. No border — the parent
 * App component provides the unified outer frame.
 *
 * Features:
 * - Window slicing: only renders messages[logOffset..logOffset+visibleLines]
 * - Auto-follow: when followLog=true, automatically scrolls to bottom
 * - Scroll indicator: shows position in message history
 *
 * Message format:
 * - User input: [HH:MM:SS] > message  (cyan > prefix)
 * - System output: [HH:MM:SS] message  (with optional color)
 */
import React from 'react';
import {Box, Text} from 'ink';

export interface ChatMessage {
  id: string;
  timestamp: string;
  text: string;
  isUser: boolean;
  color?: string;
  bold?: boolean;
  dim?: boolean;
}

export interface ChatLogProps {
  messages: ChatMessage[];
  /** Scroll offset into the messages array. */
  logOffset: number;
  /** Whether auto-follow is active. */
  followLog: boolean;
  /** Number of visible lines for the message area. */
  visibleLines: number;
  /** Whether the chat pane is currently focused. */
  isFocused: boolean;
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
  logOffset,
  followLog,
  visibleLines,
  isFocused,
}) => {
  // Window slicing: render only the visible portion
  const totalMessages = messages.length;

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

  // Calculate the visible window
  const maxOffset = Math.max(0, totalMessages - visibleLines);
  const clampedOffset = Math.min(logOffset, maxOffset);
  const endIdx = Math.min(clampedOffset + visibleLines, totalMessages);
  const visibleMessages = messages.slice(clampedOffset, endIdx);

  // Scroll indicator
  const atTop = clampedOffset === 0;
  const atBottom = clampedOffset >= maxOffset;
  const scrollIndicator = atTop && atBottom
    ? ''
    : atTop
      ? ' ↓'
      : atBottom
        ? ' ↑'
        : ` ↑${clampedOffset + 1}-${endIdx}/${totalMessages}↓`;

  // Follow indicator
  const followIndicator = followLog ? '' : ' [paused]';

  return (
    <Box flexDirection="column" flexGrow={2} paddingX={1}>
      <Box marginBottom={1}>
        <Text bold color="cyan">{'Chat'}</Text>
        {isFocused && <Text dimColor>{' [focused]'}</Text>}
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
