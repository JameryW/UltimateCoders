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
 * - Markdown rendering: system messages with markdown are rendered via marked-terminal
 */
import React, {useState, useEffect, useRef} from 'react';
import {Box, Text} from 'ink';
import type {EventFilter} from '../reducer.js';
import {eventFilterLabel} from '../reducer.js';
import {renderMarkdown, hasMarkdown} from '../markdown.js';

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
  followLog: boolean;
  visibleLines: number;
  isFocused: boolean;
  eventFilter?: EventFilter;
  scrollCommand?: ScrollCommand;
  onSetFollowLog?: (follow: boolean) => void;
  unreadCount?: number;
  expandAll?: boolean;
  terminalWidth?: number;
}

function formatTime(): string {
  const now = new Date();
  return now.toTimeString().slice(0, 5);
}

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
  options?: {color?: string; bold?: boolean; dim?: boolean; eventType?: string},
): ChatMessage {
  return {
    id: `sys-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    timestamp: formatTime(),
    text,
    isUser: false,
    ...options,
  };
}

export function filterMessages(messages: ChatMessage[], eventFilter: EventFilter): ChatMessage[] {
  if (eventFilter === 'all') return messages;
  return messages.filter((msg) => {
    if (msg.isUser) return true;
    if (!msg.eventType) return true;
    const et = msg.eventType;
    if (eventFilter === 'task') return et.startsWith('task_');
    if (eventFilter === 'subtask') return et.startsWith('subtask_');
    if (eventFilter === 'tool') return et.startsWith('tool_');
    if (eventFilter === 'error') return et === 'subtask_failed' || et === 'task_failed';
    return true;
  });
}

const COLLAPSE_THRESHOLD = 3;
const TOOL_EVENT_TYPES = new Set(['tool_call', 'tool_result', 'file_modified']);

export const EVENT_ICONS: Record<string, string> = {
  task_submitted: '📋',
  task_completed: '✓',
  task_failed: '✗',
  subtask_assigned: '◌',
  subtask_started: '▶',
  subtask_completed: '✓',
  subtask_failed: '✗',
  subtask_summary: '📋',
  tool_call: '⚙',
  tool_result: '📄',
  file_modified: '✏',
  task_list: '📋',
  command_result: '▸',
};

export function getEventIcon(eventType?: string): string {
  if (!eventType) return '';
  return EVENT_ICONS[eventType] ? `${EVENT_ICONS[eventType]} ` : '';
}

const ChatMessageItem: React.FC<{msg: ChatMessage; expandAll?: boolean; terminalWidth?: number}> = ({msg, expandAll, terminalWidth}) => {
  const [expanded, setExpanded] = useState(false);
  const lines = msg.text.split('\n');

  const isToolEvent = !msg.isUser && !!msg.eventType && TOOL_EVENT_TYPES.has(msg.eventType);
  const isLong = !msg.isUser && (isToolEvent || lines.length > COLLAPSE_THRESHOLD);

  useEffect(() => {
    setExpanded(expandAll ?? false);
  }, [expandAll]);

  const statusColor = msg.color ?? (
    msg.eventType === 'subtask_completed' || msg.eventType === 'task_completed'
      ? 'green'
      : msg.eventType === 'subtask_failed' || msg.eventType === 'task_failed'
        ? 'red'
        : msg.eventType === 'subtask_started' || msg.eventType === 'subtask_assigned'
          ? 'cyan'
          : undefined
  );

  const visibleLines = (isLong && !expanded) ? lines.slice(0, 1) : lines;

  // ponytail: render markdown for system messages that contain markdown syntax
  const shouldRenderMarkdown = !msg.isUser && !isToolEvent && hasMarkdown(msg.text);
  const renderedText = shouldRenderMarkdown
    ? renderMarkdown(msg.text, terminalWidth)
    : visibleLines.join('\n');

  const renderText = () => {
    if (msg.isUser) {
      return (
        <>
          <Text bold color="cyan">{'> '}</Text>
          <Text bold>{visibleLines[0]}</Text>
          {visibleLines.slice(1).map((line, i) => (
            <Text key={i}>{'\n  ' + line}</Text>
          ))}
        </>
      );
    }
    return (
      <Text color={statusColor} bold={msg.bold} dimColor={msg.dim ? true : isToolEvent}>
        {msg.eventType ? getEventIcon(msg.eventType) : ''}{renderedText}
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
  terminalWidth,
}) => {
  const [localOffset, setLocalOffset] = useState(0);
  const lastScrollTick = useRef(0);
  const prevFilterRef = useRef(eventFilter);
  const filteredMessages = filterMessages(messages, eventFilter);
  const totalMessages = filteredMessages.length;
  const maxOffset = Math.max(0, totalMessages - visibleLines);

  useEffect(() => {
    if (eventFilter !== prevFilterRef.current) {
      prevFilterRef.current = eventFilter;
      setLocalOffset(0);
      if (!followLog) onSetFollowLog?.(true);
    }
  }, [eventFilter, followLog, onSetFollowLog]);

  useEffect(() => {
    if (!scrollCommand || scrollCommand.tick <= lastScrollTick.current) return;
    lastScrollTick.current = scrollCommand.tick;
    if (scrollCommand.direction === 'up') {
      setLocalOffset((prev) => Math.max(0, prev - scrollCommand.lines));
      onSetFollowLog?.(false);
    } else {
      setLocalOffset((prev) => {
        const newOffset = Math.min(maxOffset, prev + scrollCommand.lines);
        if (newOffset >= maxOffset) onSetFollowLog?.(true);
        return newOffset;
      });
    }
  }, [scrollCommand, maxOffset, onSetFollowLog]);

  const effectiveOffset = followLog ? maxOffset : Math.min(localOffset, maxOffset);

  if (totalMessages === 0) {
    return (
      <Box flexDirection="column" flexGrow={2} paddingX={1}>
        <Text dimColor>{'No messages yet. Type a task below.'}</Text>
      </Box>
    );
  }

  const endIdx = Math.min(effectiveOffset + visibleLines, totalMessages);
  const visibleMessages = filteredMessages.slice(effectiveOffset, endIdx);

  const atTop = effectiveOffset === 0;
  const atBottom = effectiveOffset >= maxOffset;
  const scrollIndicator = atTop && atBottom ? '' : atTop ? ' ↓' : atBottom ? ' ↑' : ` ↑${effectiveOffset + 1}-${endIdx}/${totalMessages}↓`;
  const followIndicator = followLog ? '' : ' [paused]';
  const unreadIndicator = unreadCount > 0 ? ` [+${unreadCount} new]` : '';
  const filterIndicator = eventFilter !== 'all' ? ` [filter:${eventFilterLabel(eventFilter)} ${totalMessages}/${messages.length}]` : '';

  return (
    <Box flexDirection="column" flexGrow={2} paddingX={1}>
      {filterIndicator && <Text color="yellow">{filterIndicator}</Text>}
      {followIndicator && <Text color="yellow">{followIndicator}</Text>}
      {unreadIndicator && <Text color="red" bold>{unreadIndicator}</Text>}
      {scrollIndicator && <Text dimColor>{scrollIndicator}</Text>}
      {visibleMessages.map((msg) => (
        <ChatMessageItem key={msg.id} msg={msg} expandAll={expandAll} terminalWidth={terminalWidth} />
      ))}
    </Box>
  );
};

export default ChatLog;
