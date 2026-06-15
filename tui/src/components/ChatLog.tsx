/**
 * ChatLog component - conversational-style message log.
 *
 * Renders chat messages in the left panel. No border — the parent
 * App component provides the unified outer frame.
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

const ChatLog: React.FC<ChatLogProps> = ({messages}) => {
  // Show last N messages that fit (avoid <Static> which breaks unified layout)
  const visibleMessages = messages.slice(-20);

  return (
    <Box flexDirection="column" flexGrow={2} paddingX={1}>
      <Box marginBottom={1}>
        <Text bold color="cyan">{'Chat'}</Text>
      </Box>
      {visibleMessages.map((msg) => (
        <ChatMessageItem key={msg.id} msg={msg} />
      ))}
      {messages.length === 0 && (
        <Text dimColor>No messages yet. Type a task below.</Text>
      )}
    </Box>
  );
};

export default ChatLog;
