/**
 * ChatLog component - conversational-style scrollable log.
 *
 * Uses Ink's <Static> component for scrollback history (permanently
 * rendered above the interactive area) and a live message area for
 * the most recent messages.
 *
 * Message format:
 * - User input: [HH:MM:SS] > message  (cyan > prefix)
 * - System output: [HH:MM:SS] message  (with optional color)
 */
import React from 'react';
import {Box, Text, Static} from 'ink';

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
  // Use <Static> for previously rendered messages (scrollback).
  // <Static> renders items permanently above the interactive area.
  // The children function is called once per item (not per batch).
  const staticMessages = messages.slice(0, -1);
  const liveMessage = messages.length > 0 ? messages[messages.length - 1] : null;

  return (
    <Box flexDirection="column" flexGrow={2} borderStyle="single" borderColor="gray" paddingX={1}>
      <Box flexDirection="column">
        <Text bold>Chat</Text>
      </Box>
      {staticMessages.length > 0 && (
        <Static items={staticMessages}>
          {(msg: ChatMessage, index: number) => (
            <ChatMessageItem key={msg.id} msg={msg} />
          )}
        </Static>
      )}
      {liveMessage && <ChatMessageItem msg={liveMessage} />}
      {messages.length === 0 && (
        <Text dimColor>No messages yet. Type a task description below.</Text>
      )}
    </Box>
  );
};

export default ChatLog;
