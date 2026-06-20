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
 * - Message selection: Up/Down navigates, Enter expands selected message
 * - Markdown rendering: system messages with markdown are rendered via marked-terminal
 */
import React, {useState, useEffect, useRef, useCallback, useMemo} from 'react';
import {Box, Text, useInput} from 'ink';
import type {EventFilter} from '../reducer.js';
import {eventFilterLabel} from '../reducer.js';
import {renderMarkdown, hasMarkdown, isDiffText, colorDiff} from '../markdown.js';

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
  terminalWidth?: number;
  /** Whether older messages were truncated (show hint at top). */
  messagesTruncated?: boolean;
  /** Search query for highlighting matches. */
  searchQuery?: string;
  /** Whether search mode is active. */
  searchActive?: boolean;
  /** Current search match index. */
  searchMatchIndex?: number;
}

function formatTime(): string {
  const now = new Date();
  return now.toTimeString().slice(0, 5);
}

/** Parse HH:MM timestamp and return minutes since midnight. Returns -1 on parse failure. */
function parseHHMM(ts: string): number {
  const parts = ts.split(':');
  if (parts.length !== 2) return -1;
  const h = parseInt(parts[0], 10);
  const m = parseInt(parts[1], 10);
  if (isNaN(h) || isNaN(m)) return -1;
  return h * 60 + m;
}

/** Minutes between two HH:MM timestamps. Handles midnight wraparound. */
function timeDiffMinutes(earlier: string, later: string): number {
  const a = parseHHMM(earlier);
  const b = parseHHMM(later);
  if (a < 0 || b < 0) return 0;
  const diff = b - a;
  // Handle midnight wraparound (e.g., 23:55 → 00:05)
  return diff < 0 ? diff + 24 * 60 : diff;
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

const ChatMessageItem: React.FC<{msg: ChatMessage; isExpanded: boolean; isSelected: boolean; terminalWidth?: number; isSearchMatch?: boolean; isCurrentMatch?: boolean}> = ({msg, isExpanded, isSelected, terminalWidth, isSearchMatch, isCurrentMatch}) => {
  const lines = msg.text.split('\n');

  const isToolEvent = !msg.isUser && !!msg.eventType && TOOL_EVENT_TYPES.has(msg.eventType);
  const isLong = !msg.isUser && (isToolEvent || lines.length > COLLAPSE_THRESHOLD);

  const statusColor = msg.color ?? (
    msg.eventType === 'subtask_completed' || msg.eventType === 'task_completed'
      ? 'green'
      : msg.eventType === 'subtask_failed' || msg.eventType === 'task_failed'
        ? 'red'
        : msg.eventType === 'subtask_started' || msg.eventType === 'subtask_assigned'
          ? 'cyan'
          : undefined
  );

  const visibleLines = (isLong && !isExpanded) ? lines.slice(0, 1) : lines;

  // ponytail: render markdown for system messages that contain markdown syntax
  const shouldRenderMarkdown = !msg.isUser && !isToolEvent && hasMarkdown(msg.text);
  const isDiff = !msg.isUser && (msg.eventType === 'file_modified' || isDiffText(msg.text));
  const renderedText = isDiff
    ? colorDiff(visibleLines.join('\n'))
    : shouldRenderMarkdown
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

  const collapsedCount = isLong && !isExpanded ? lines.length - 1 : 0;

  return (
    <Box flexDirection="column">
      <Box>
        <Text dimColor>{`[${msg.timestamp}] `}</Text>
        {isSelected && <Text color="yellow">{'▸'}</Text>}
        {!isSelected && <Text>{' '}</Text>}
        {isCurrentMatch && <Text color="yellow">{'◆'}</Text>}
        {isSearchMatch && !isCurrentMatch && <Text color="yellow">{'◇'}</Text>}
        {!isSearchMatch && !isCurrentMatch && <Text>{' '}</Text>}
        {renderText()}
      </Box>
      {collapsedCount > 0 && (
        <Box marginLeft={9}>
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
  terminalWidth,
  messagesTruncated,
  searchQuery = '',
  searchActive = false,
  searchMatchIndex = 0,
}) => {
  const [localOffset, setLocalOffset] = useState(0);
  const [selectedVisibleIndex, setSelectedVisibleIndex] = useState(-1);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const lastScrollTick = useRef(0);
  const prevFilterRef = useRef(eventFilter);
  // ponytail: memoize filtered messages to prevent new array reference each render
  const filteredMessages = useMemo(() => filterMessages(messages, eventFilter), [messages, eventFilter]);
  const totalMessages = filteredMessages.length;
  const maxOffset = Math.max(0, totalMessages - visibleLines);

  // Search match computation
  const searchMatches = searchActive && searchQuery
    ? filteredMessages.reduce<Array<{msgIdx: number; pos: number}>>((acc, msg, idx) => {
        const lower = msg.text.toLowerCase();
        const query = searchQuery.toLowerCase();
        let pos = 0;
        while (true) {
          const found = lower.indexOf(query, pos);
          if (found < 0) break;
          acc.push({msgIdx: idx, pos: found});
          pos = found + 1;
        }
        return acc;
      }, [])
    : [];
  const totalSearchMatches = searchMatches.length;
  const currentMatchIdx = searchMatchIndex % Math.max(1, totalSearchMatches);
  const currentMatch = searchMatches[currentMatchIdx] ?? null;

  // Computed visible window
  const effectiveOffset = followLog ? maxOffset : Math.min(localOffset, maxOffset);
  const endIdx = Math.min(effectiveOffset + visibleLines, totalMessages);
  const visibleMessages = filteredMessages.slice(effectiveOffset, endIdx);
  const visibleCount = visibleMessages.length;

  useEffect(() => {
    if (eventFilter !== prevFilterRef.current) {
      prevFilterRef.current = eventFilter;
      setLocalOffset(0);
      setSelectedVisibleIndex(-1);
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

  // Auto-scroll to search match
  useEffect(() => {
    if (!currentMatch) return;
    const targetOffset = Math.max(0, currentMatch.msgIdx - Math.floor(visibleLines / 2));
    if (targetOffset !== localOffset) {
      setLocalOffset(targetOffset);
      onSetFollowLog?.(false);
    }
  }, [currentMatch, visibleLines, onSetFollowLog]); // eslint-disable-line react-hooks/exhaustive-deps

  // Handle Enter key locally: toggle expand on selected message
  useInput(useCallback((_input: string, key: {return?: boolean}) => {
    if (!isFocused || !key.return) return;
    // If no selection, select the last visible message
    if (selectedVisibleIndex < 0) {
      setSelectedVisibleIndex(visibleCount - 1);
      return;
    }
    const msg = visibleMessages[selectedVisibleIndex];
    if (msg) {
      setExpandedIds((prev) => {
        const next = new Set(prev);
        if (next.has(msg.id)) next.delete(msg.id);
        else next.add(msg.id);
        return next;
      });
    }
  }, [isFocused, selectedVisibleIndex, visibleMessages, visibleCount]));

  // Handle Up/Down to move selection (scrolls window to keep selection visible)
  useInput(useCallback((_input: string, key: {upArrow?: boolean; downArrow?: boolean}) => {
    if (!isFocused) return;
    if (key.upArrow) {
      setSelectedVisibleIndex((prev) => {
        if (prev < 0) return visibleCount > 0 ? visibleCount - 1 : -1;
        if (prev > 0) return prev - 1;
        // At top of visible window — scroll up
        onSetFollowLog?.(false);
        setLocalOffset((off) => Math.max(0, off - 1));
        return 0;
      });
      return;
    }
    if (key.downArrow) {
      setSelectedVisibleIndex((prev) => {
        if (prev < 0) return 0;
        if (prev < visibleCount - 1) return prev + 1;
        // At bottom of visible window — scroll down
        setLocalOffset((off) => {
          const newOff = Math.min(maxOffset, off + 1);
          if (newOff >= maxOffset) onSetFollowLog?.(true);
          return newOff;
        });
        return visibleCount - 1;
      });
      return;
    }
  }, [isFocused, visibleCount, maxOffset, onSetFollowLog]));

  if (totalMessages === 0) {
    return (
      <Box flexDirection="column" height={visibleLines} paddingX={1}>
        <Text dimColor>{'No messages yet. Type a task below.'}</Text>
      </Box>
    );
  }

  const atTop = effectiveOffset === 0;
  const atBottom = effectiveOffset >= maxOffset;
  const scrollIndicator = atTop && atBottom ? '' : atTop ? ' ↓' : atBottom ? ' ↑' : ` ↑${effectiveOffset + 1}-${endIdx}/${totalMessages}↓`;

  // Compact single-line indicator bar
  const parts: string[] = [];
  if (eventFilter !== 'all') parts.push(`filter:${eventFilterLabel(eventFilter)} ${totalMessages}/${messages.length}`);
  if (!followLog) parts.push('paused');
  if (unreadCount > 0) parts.push(`+${unreadCount} new`);
  if (scrollIndicator) parts.push(scrollIndicator.trim());
  const indicatorBar = parts.join(' │ ');

  // Search highlight: which messages match
  const searchMatchMsgIds = new Set(searchMatches.map((m) => filteredMessages[m.msgIdx]?.id));
  const currentMatchMsgId = currentMatch ? filteredMessages[currentMatch.msgIdx]?.id : null;

  return (
    <Box flexDirection="column" height={visibleLines} paddingX={1}>
      {indicatorBar && <Text color={eventFilter !== 'all' || !followLog ? 'yellow' : 'dim'} dimColor={eventFilter === 'all' && followLog}>{indicatorBar}</Text>}
      {searchActive && (
        <Box>
          <Text color="yellow" bold>{'Search: '}</Text>
          <Text color="yellow">{searchQuery || '(type to search)'}</Text>
          {totalSearchMatches > 0 && (
            <Text dimColor>{` [${currentMatchIdx + 1}/${totalSearchMatches}] N next · Shift+N prev · Esc exit`}</Text>
          )}
          {totalSearchMatches === 0 && searchQuery && (
            <Text dimColor>{' — no matches'}</Text>
          )}
        </Box>
      )}
      {messagesTruncated && effectiveOffset === 0 && (
        <Text dimColor>{'↕ Earlier messages truncated'}</Text>
      )}
      {visibleMessages.map((msg, i) => {
        const prevMsg = i > 0 ? visibleMessages[i - 1] : null;
        const showSeparator = prevMsg && timeDiffMinutes(prevMsg.timestamp, msg.timestamp) >= 5;
        const isSearchMatch = searchActive && searchMatchMsgIds.has(msg.id);
        const isCurrentMatch = searchActive && msg.id === currentMatchMsgId;
        return (
          <React.Fragment key={msg.id}>
            {showSeparator && (
              <Text dimColor>{`── ${msg.timestamp} ──`}</Text>
            )}
            <ChatMessageItem
              msg={msg}
              isExpanded={expandedIds.has(msg.id) || isCurrentMatch}
              isSelected={isFocused && i === selectedVisibleIndex}
              terminalWidth={terminalWidth}
              isSearchMatch={isSearchMatch}
              isCurrentMatch={isCurrentMatch}
            />
          </React.Fragment>
        );
      })}
    </Box>
  );
};

export default ChatLog;
