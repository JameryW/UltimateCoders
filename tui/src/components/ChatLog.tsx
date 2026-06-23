/**
 * ChatLog component - conversational-style message log with virtual rendering.
 *
 * Renders chat messages in the left panel. No border — the parent
 * App component provides the unified outer frame.
 *
 * Features:
 * - Virtual rendering: only renders visible messages (O(viewport))
 * - Pixel-based scrolling: offset tracked in terminal rows, not message indices
 * - Auto-follow: when followLog=true, automatically scrolls to bottom
 * - Scroll indicator: shows position in message history
 * - Event filtering: filter by event type (task/subtask/tool/error)
 * - Unread count: when followLog is off, shows "+N new" in header
 * - Home/End: jump to top/bottom (handled by parent via scrollCommand)
 * - Message selection: Up/Down navigates, Enter expands selected message
 * - Markdown rendering: system messages with markdown are rendered via marked-terminal
 * - Tool call summary: collapsed tool calls show ⚙ ToolName(arg) duration
 * - ANSI progress bar: subtask progress as █████░░░ 67%
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
  /** Whether all long messages should be force-expanded (A key toggle). */
  expandAll?: boolean;
  /** Set of bookmarked message IDs. */
  bookmarkedIds?: Set<string>;
  /** Target message ID to jump to (consumed by ChatLog, then cleared). */
  jumpToMessageId?: string | null;
  /** Called when user presses B on a message to toggle bookmark. */
  onToggleBookmark?: (messageId: string) => void;
}

function formatTime(): string {
  const now = new Date();
  return now.toTimeString().slice(0, 8);
}

/** Parse HH:MM timestamp and return minutes since midnight. Returns -1 on parse failure. */
function parseHHMM(ts: string): number {
  const parts = ts.split(':');
  if (parts.length < 2) return -1;
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

/** Build a task summary card message when all subtasks are done. */
export function createTaskSummaryMessage(
  subtasks: Array<{index: number; description: string; status: string; elapsedMs?: number}>,
): ChatMessage {
  const completed = subtasks.filter(s => s.status === 'completed');
  const failed = subtasks.filter(s => s.status === 'failed');
  const totalMs = subtasks.reduce((sum, s) => sum + (s.elapsedMs ?? 0), 0);
  const totalSec = Math.round(totalMs / 1000);
  const timeLabel = totalSec >= 60 ? `${Math.floor(totalSec / 60)}m${totalSec % 60}s` : `${totalSec}s`;

  const lines: string[] = ['╭─ Task Summary ──────────────────────╮'];
  for (const st of subtasks) {
    const icon = st.status === 'completed' ? '✓' : '✗';
    const elapsed = st.elapsedMs != null ? (() => {
      const s = Math.round(st.elapsedMs / 1000);
      return s >= 60 ? `${Math.floor(s / 60)}m${s % 60}s` : `${s}s`;
    })() : '';
    const desc = st.description.length > 30 ? st.description.slice(0, 27) + '…' : st.description;
    lines.push(`│ ${icon} #${st.index} ${desc} ${elapsed}`);
  }
  lines.push('│ ─────────────────────────────────────');
  const modLabel = '';  // ponytail: modified files not available in subtask summary
  if (modLabel) lines.push(`│ Modified: ${modLabel}`);
  lines.push(`│ Total: ${timeLabel} │ ${completed.length}/${subtasks.length} done${failed.length > 0 ? ` │ ${failed.length} failed` : ''}`);
  lines.push('╰──────────────────────────────────────╯');

  return createSystemMessage(lines.join('\n'), {
    eventType: 'task_completed',
    color: failed.length > 0 ? 'yellow' : 'green',
    bold: true,
  });
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

const MAX_RENDERED_LINES = 50;

// ── Tool call summary extraction ──────────────────────────────

/** Extract a compact summary from a tool_call or tool_result message.
 *  Format: ⚙ Read(src/main.rs) 2.3s
 *  Falls back to first line if pattern doesn't match. */
function extractToolSummary(text: string, eventType?: string): string {
  const lines = text.split('\n');
  const firstLine = lines[0] ?? '';

  // tool_call: "Read(file.ts)" or "Write(file.ts, content)" pattern
  if (eventType === 'tool_call') {
    const match = firstLine.match(/^(\w+)\(([^)]*)\)/);
    if (match) {
      const toolName = match[1];
      const args = match[2];
      const firstArg = args.split(',')[0]?.trim() ?? args;
      const durationMatch = text.match(/(\d+\.?\d*)\s*(ms|s|sec|seconds?)/i);
      const duration = durationMatch ? ` ${durationMatch[1]}${durationMatch[2]}` : '';
      return `${toolName}(${firstArg})${duration}`;
    }
    const nameMatch = firstLine.match(/^(\w+)/);
    if (nameMatch) return `${nameMatch[1]}(…)`;
  }

  // tool_result: show first line truncated
  if (eventType === 'tool_result') {
    const durationMatch = text.match(/(\d+\.?\d*)\s*(ms|s|sec|seconds?)/i);
    const duration = durationMatch ? ` ${durationMatch[1]}${durationMatch[2]}` : '';
    const summary = firstLine.length > 40 ? firstLine.slice(0, 37) + '…' : firstLine;
    return `${summary}${duration}`;
  }

  // file_modified: extract filename
  if (eventType === 'file_modified') {
    const fileMatch = text.match(/([\w./\-]+\.\w+)/);
    if (fileMatch) return `✏ ${fileMatch[1]}`;
  }

  return firstLine.length > 50 ? firstLine.slice(0, 47) + '…' : firstLine;
}

// ── ANSI progress bar ─────────────────────────────────────────

/** Build an ANSI block progress bar.
 *  @param completed  Number of completed items
 *  @param total      Total number of items
 *  @param width      Bar width in characters (default 10)
 *  @returns  String like "█████░░░ 67%" */
export function buildProgressBar(completed: number, total: number, width = 10): string {
  if (total <= 0) return '';
  const pct = completed / total;
  const filled = Math.round(pct * width);
  const empty = width - filled;
  const bar = '█'.repeat(Math.max(0, filled)) + '░'.repeat(Math.max(0, empty));
  const pctStr = `${Math.round(pct * 100)}%`;
  return `${bar} ${pctStr}`;
}

/** Split text into segments, highlighting matches with inverse bold. */
function highlightSearchMatches(
  text: string,
  matches: Array<{pos: number}>,
  queryLen: number,
  isCurrent: boolean,
): React.ReactNode[] {
  if (!matches.length || queryLen <= 0) return [<Text key="t">{text}</Text>];
  const positions = [...new Set(matches.map(m => m.pos))].sort((a, b) => a - b);
  const nodes: React.ReactNode[] = [];
  let last = 0;
  for (const pos of positions) {
    if (pos < last) continue;
    if (pos > last) nodes.push(<Text key={`t${last}`}>{text.slice(last, pos)}</Text>);
    nodes.push(
      <Text key={`h${pos}`} inverse bold color={isCurrent ? 'yellow' : 'cyan'}>
        {text.slice(pos, pos + queryLen)}
      </Text>,
    );
    last = pos + queryLen;
  }
  if (last < text.length) nodes.push(<Text key="tail">{text.slice(last)}</Text>);
  return nodes;
}

// ── Virtual rendering: height estimation ──────────────────────

/** Estimate the rendered height (terminal rows) for a message. */
function estimateMsgHeight(
  msg: ChatMessage,
  isExpanded: boolean,
  prevMsg: ChatMessage | null,
  terminalWidth?: number,
): number {
  const lines = msg.text.split('\n');
  const isToolEvent = !msg.isUser && !!msg.eventType && TOOL_EVENT_TYPES.has(msg.eventType);
  const isLong = !msg.isUser && (isToolEvent || lines.length > COLLAPSE_THRESHOLD);

  let height = 0;
  if (prevMsg) {
    const gapMinutes = timeDiffMinutes(prevMsg.timestamp, msg.timestamp);
    if (gapMinutes >= 5) height += 1;
  }

  if (isLong && !isExpanded) {
    height += 2; // 1 content + 1 collapse hint
  } else {
    const visibleLineCount = isExpanded
      ? Math.min(lines.length, MAX_RENDERED_LINES)
      : lines.length;
    const wrapWidth = (terminalWidth ?? 80) - 12;
    let wrappedLines = 0;
    for (let i = 0; i < visibleLineCount; i++) {
      const lineLen = lines[i]?.length ?? 0;
      wrappedLines += Math.max(1, Math.ceil(lineLen / wrapWidth));
    }
    height += wrappedLines;
    if (isLong && isExpanded) height += 1;
    if (isExpanded && lines.length > MAX_RENDERED_LINES) height += 1;
  }

  return Math.max(1, height);
}

/** Build cumulative height array from filtered messages.
 *  cumulativeHeights[i] = total rows for messages 0..i-1 */
function buildCumulativeHeights(
  messages: ChatMessage[],
  expandedIds: Set<string>,
  terminalWidth?: number,
): number[] {
  const heights = new Array<number>(messages.length + 1);
  heights[0] = 0;
  let cumulative = 0;
  for (let i = 0; i < messages.length; i++) {
    const prevMsg = i > 0 ? messages[i - 1] : null;
    const h = estimateMsgHeight(messages[i], expandedIds.has(messages[i].id), prevMsg, terminalWidth);
    cumulative += h;
    heights[i + 1] = cumulative;
  }
  return heights;
}

/** Binary search: find the first message index whose cumulative height >= targetPixel. */
function findMsgIndexAtPixel(cumulativeHeights: number[], pixelOffset: number): number {
  if (pixelOffset <= 0) return 0;
  let lo = 0, hi = cumulativeHeights.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (cumulativeHeights[mid] < pixelOffset) lo = mid + 1;
    else hi = mid;
  }
  return Math.min(lo, cumulativeHeights.length - 2);
}

// ── ChatMessageItem ──────────────────────────────────────────

const ChatMessageItem: React.FC<{
  msg: ChatMessage; isExpanded: boolean; isSelected: boolean; terminalWidth?: number;
  isSearchMatch?: boolean; isCurrentMatch?: boolean;
  searchHighlights?: Array<{pos: number}>; searchQueryLen?: number;
  isBookmarked?: boolean;
}> = ({msg, isExpanded, isSelected, terminalWidth, isSearchMatch, isCurrentMatch, searchHighlights, searchQueryLen, isBookmarked}) => {
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

  // ── Collapsed tool event: show compact summary ──
  if (isToolEvent && !isExpanded) {
    const summary = extractToolSummary(msg.text, msg.eventType);
    const collapsedCount = lines.length - 1;
    return (
      <Box flexDirection="column">
        <Box>
          <Text dimColor>{`[${msg.timestamp}] `}</Text>
          {isBookmarked && <Text color="yellow">{'★'}</Text>}
          {!isBookmarked && <Text>{' '}</Text>}
          {isSelected && <Text color="yellow">{'▸'}</Text>}
          {!isSelected && <Text>{' '}</Text>}
          {isCurrentMatch && <Text color="yellow">{'◆'}</Text>}
          {isSearchMatch && !isCurrentMatch && <Text color="yellow">{'◇'}</Text>}
          {!isSearchMatch && !isCurrentMatch && <Text>{' '}</Text>}
          <Text color={statusColor} dimColor>{summary}</Text>
        </Box>
        <Box marginLeft={9}>
          <Text dimColor>{`[+${collapsedCount} lines — Enter to expand]`}</Text>
        </Box>
      </Box>
    );
  }

  const visibleLines = (isLong && !isExpanded) ? lines.slice(0, 1) : lines;
  const cappedLines = isExpanded && visibleLines.length > MAX_RENDERED_LINES
    ? visibleLines.slice(0, MAX_RENDERED_LINES) : visibleLines;
  const extraLines = isExpanded ? Math.max(0, visibleLines.length - MAX_RENDERED_LINES) : 0;

  const shouldRenderMarkdown = !msg.isUser && hasMarkdown(msg.text);
  const isDiff = !msg.isUser && (msg.eventType === 'file_modified' || isDiffText(msg.text));
  const joinedCapped = cappedLines.join('\n');
  const renderedText = isDiff
    ? colorDiff(joinedCapped)
    : shouldRenderMarkdown
      ? renderMarkdown(msg.text, terminalWidth)
      : joinedCapped;

  const canHighlight = searchHighlights && searchHighlights.length > 0 && searchQueryLen && searchQueryLen > 0
    && !shouldRenderMarkdown && !isDiff;

  const renderText = () => {
    if (msg.isUser) {
      if (canHighlight) {
        return (
          <>
            <Text bold color="cyan">{'> '}</Text>
            {highlightSearchMatches(cappedLines[0], searchHighlights!, searchQueryLen!, !!isCurrentMatch)}
            {cappedLines.slice(1).map((line, i) => (
              <Text key={i}>{'\n  '}{highlightSearchMatches(line, searchHighlights!, searchQueryLen!, !!isCurrentMatch)}</Text>
            ))}
          </>
        );
      }
      return (
        <>
          <Text bold color="cyan">{'> '}</Text>
          <Text bold>{cappedLines[0]}</Text>
          {cappedLines.slice(1).map((line, i) => (
            <Text key={i}>{'\n  ' + line}</Text>
          ))}
        </>
      );
    }
    if (canHighlight) {
      return (
        <Text color={statusColor} bold={msg.bold} dimColor={msg.dim ? true : isToolEvent && !isExpanded}>
          {msg.eventType ? getEventIcon(msg.eventType) : ''}
          {highlightSearchMatches(joinedCapped, searchHighlights!, searchQueryLen!, !!isCurrentMatch)}
        </Text>
      );
    }
    return (
      <Text color={statusColor} bold={msg.bold} dimColor={msg.dim ? true : isToolEvent && !isExpanded}>
        {msg.eventType ? getEventIcon(msg.eventType) : ''}{renderedText}
      </Text>
    );
  };

  const collapsedCount = isLong && !isExpanded ? lines.length - 1 : 0;

  return (
    <Box flexDirection="column">
      <Box>
        <Text dimColor>{`[${msg.timestamp}] `}</Text>
        {isBookmarked && <Text color="yellow">{'★'}</Text>}
        {!isBookmarked && <Text>{' '}</Text>}
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
      {isLong && isExpanded && collapsedCount === 0 && (
        <Box marginLeft={9}>
          <Text dimColor>{'[Enter to collapse]'}</Text>
        </Box>
      )}
      {extraLines > 0 && (
        <Box marginLeft={9}>
          <Text dimColor>{`[+${extraLines} more lines]`}</Text>
        </Box>
      )}
    </Box>
  );
};

// ── ChatLog (virtual rendering) ──────────────────────────────

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
  expandAll = false,
  bookmarkedIds = new Set(),
  jumpToMessageId = null,
  onToggleBookmark,
}) => {
  const [pixelOffset, setPixelOffset] = useState(0);
  const [selectedMsgId, setSelectedMsgId] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const lastScrollTick = useRef(0);
  const prevFilterRef = useRef(eventFilter);
  const filteredMessages = useMemo(() => filterMessages(messages, eventFilter), [messages, eventFilter]);
  const totalMessages = filteredMessages.length;

  const cumulativeHeights = useMemo(
    () => buildCumulativeHeights(filteredMessages, expandedIds, terminalWidth),
    [filteredMessages, expandedIds, terminalWidth],
  );
  const totalHeight = cumulativeHeights[cumulativeHeights.length - 1] ?? 0;
  const maxPixelOffset = Math.max(0, totalHeight - visibleLines);

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

  const effectivePixelOffset = followLog ? maxPixelOffset : Math.min(pixelOffset, maxPixelOffset);
  const firstVisibleIdx = findMsgIndexAtPixel(cumulativeHeights, effectivePixelOffset);
  const viewportBottom = effectivePixelOffset + visibleLines;
  let lastVisibleIdx = firstVisibleIdx;
  while (lastVisibleIdx < totalMessages - 1 && cumulativeHeights[lastVisibleIdx + 1] < viewportBottom) {
    lastVisibleIdx++;
  }
  // ponytail: overscan 2 messages above and below
  const overscanFirst = Math.max(0, firstVisibleIdx - 2);
  const overscanLast = Math.min(totalMessages - 1, lastVisibleIdx + 2);
  const visibleMessages = filteredMessages.slice(overscanFirst, overscanLast + 1);
  const visibleCount = visibleMessages.length;

  const overscanTopPixels = cumulativeHeights[overscanFirst];
  const clipTopPixels = effectivePixelOffset - overscanTopPixels;

  useEffect(() => {
    if (eventFilter !== prevFilterRef.current) {
      prevFilterRef.current = eventFilter;
      setPixelOffset(0);
      setSelectedMsgId(null);
      if (!followLog) onSetFollowLog?.(true);
    }
  }, [eventFilter, followLog, onSetFollowLog]);

  useEffect(() => {
    if (!scrollCommand || scrollCommand.tick <= lastScrollTick.current) return;
    lastScrollTick.current = scrollCommand.tick;
    if (scrollCommand.direction === 'up') {
      setPixelOffset((prev) => Math.max(0, prev - scrollCommand.lines));
      onSetFollowLog?.(false);
    } else {
      setPixelOffset((prev) => {
        const newOffset = Math.min(maxPixelOffset, prev + scrollCommand.lines);
        if (newOffset >= maxPixelOffset) onSetFollowLog?.(true);
        return newOffset;
      });
    }
  }, [scrollCommand, maxPixelOffset, onSetFollowLog]);

  useEffect(() => {
    if (!currentMatch) return;
    const msgStartPixel = cumulativeHeights[currentMatch.msgIdx];
    const msgHeight = cumulativeHeights[currentMatch.msgIdx + 1] - msgStartPixel;
    const targetOffset = Math.max(0, msgStartPixel - Math.floor((visibleLines - msgHeight) / 2));
    if (targetOffset !== pixelOffset) {
      setPixelOffset(targetOffset);
      onSetFollowLog?.(false);
    }
  }, [currentMatch, visibleLines, onSetFollowLog]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectedVisibleIndex = selectedMsgId
    ? visibleMessages.findIndex((m) => m.id === selectedMsgId)
    : -1;

  useInput(useCallback((_input: string, key: {return?: boolean}) => {
    if (!isFocused || !key.return) return;
    if (!selectedMsgId) {
      const lastMsg = visibleMessages[visibleCount - 1];
      if (lastMsg) setSelectedMsgId(lastMsg.id);
      return;
    }
    const msg = filteredMessages.find((m) => m.id === selectedMsgId);
    if (msg) {
      setExpandedIds((prev) => {
        const next = new Set(prev);
        if (next.has(msg.id)) next.delete(msg.id);
        else next.add(msg.id);
        return next;
      });
    }
  }, [isFocused, selectedMsgId, visibleMessages, visibleCount, filteredMessages]));

  // B: toggle bookmark on selected message
  useInput(useCallback((input: string, key: {shift?: boolean}) => {
    if (!isFocused) return;
    if (input === 'B' && !key.shift && onToggleBookmark && selectedMsgId) {
      onToggleBookmark(selectedMsgId);
    }
  }, [isFocused, selectedMsgId, onToggleBookmark]));

  // Jump to message when jumpToMessageId changes (bookmark nav / diagnostic jump)
  useEffect(() => {
    if (!jumpToMessageId) return;
    const idx = filteredMessages.findIndex(m => m.id === jumpToMessageId);
    if (idx < 0) return;
    setSelectedMsgId(jumpToMessageId);
    const targetPixel = cumulativeHeights[idx];
    setPixelOffset(Math.max(0, targetPixel));
    onSetFollowLog?.(false);
  }, [jumpToMessageId]); // eslint-disable-line react-hooks/exhaustive-deps

  useInput(useCallback((_input: string, key: {upArrow?: boolean; downArrow?: boolean}) => {
    if (!isFocused) return;
    const currentGlobalIdx = selectedMsgId
      ? filteredMessages.findIndex((m) => m.id === selectedMsgId)
      : -1;

    if (key.upArrow) {
      if (currentGlobalIdx < 0) {
        const last = filteredMessages[totalMessages - 1];
        if (last) setSelectedMsgId(last.id);
        return;
      }
      if (currentGlobalIdx > 0) {
        const prev = filteredMessages[currentGlobalIdx - 1];
        setSelectedMsgId(prev.id);
        const prevStartPixel = cumulativeHeights[currentGlobalIdx - 1];
        if (prevStartPixel < effectivePixelOffset) {
          setPixelOffset(prevStartPixel);
          onSetFollowLog?.(false);
        }
      }
      return;
    }

    if (key.downArrow) {
      if (currentGlobalIdx < 0) {
        const first = filteredMessages[0];
        if (first) setSelectedMsgId(first.id);
        return;
      }
      if (currentGlobalIdx < totalMessages - 1) {
        const next = filteredMessages[currentGlobalIdx + 1];
        setSelectedMsgId(next.id);
        const nextEndPixel = cumulativeHeights[currentGlobalIdx + 2];
        if (nextEndPixel > effectivePixelOffset + visibleLines) {
          setPixelOffset(Math.max(0, nextEndPixel - visibleLines));
          if (nextEndPixel - visibleLines >= maxPixelOffset) {
            onSetFollowLog?.(true);
          } else {
            onSetFollowLog?.(false);
          }
        }
      }
      return;
    }
  }, [isFocused, selectedMsgId, filteredMessages, totalMessages, cumulativeHeights,
      effectivePixelOffset, visibleLines, maxPixelOffset, onSetFollowLog]));

  if (totalMessages === 0) {
    return (
      <Box flexDirection="column" height={visibleLines} paddingX={1}>
        <Text dimColor>{'No messages yet. Type a task below.'}</Text>
      </Box>
    );
  }

  const atTop = effectivePixelOffset === 0;
  const atBottom = effectivePixelOffset >= maxPixelOffset;
  const firstVisibleNum = overscanFirst + 1;
  const lastVisibleNum = overscanLast + 1;
  const scrollIndicator = atTop && atBottom ? '' : atTop ? ' ↓' : atBottom ? ' ↑' : ` ↑${firstVisibleNum}-${lastVisibleNum}/${totalMessages}↓`;

  const parts: string[] = [];
  if (eventFilter !== 'all') parts.push(`filter:${eventFilterLabel(eventFilter)} ${totalMessages}/${messages.length}`);
  if (!followLog) parts.push('paused');
  if (unreadCount > 0) parts.push(`+${unreadCount} new`);
  if (scrollIndicator) parts.push(scrollIndicator.trim());
  const indicatorBar = parts.join(' │ ');

  const searchMatchMsgIds = new Set(searchMatches.map((m) => filteredMessages[m.msgIdx]?.id));
  const currentMatchMsgId = currentMatch ? filteredMessages[currentMatch.msgIdx]?.id : null;

  return (
    <Box flexDirection="column" height={visibleLines} paddingX={1} overflow="hidden">
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
      {messagesTruncated && effectivePixelOffset === 0 && (
        <Text dimColor>{'↕ Earlier messages truncated'}</Text>
      )}
      <Box flexDirection="column" marginTop={-clipTopPixels} overflow="hidden">
        {visibleMessages.map((msg, i) => {
          const globalIdx = overscanFirst + i;
          const prevMsg = globalIdx > 0 ? filteredMessages[globalIdx - 1] : null;
          const gapMinutes = prevMsg ? timeDiffMinutes(prevMsg.timestamp, msg.timestamp) : 0;
          const showSeparator = prevMsg && gapMinutes >= 5;
          const isSearchMatch = searchActive && searchMatchMsgIds.has(msg.id);
          const isCurrentMatch = searchActive && msg.id === currentMatchMsgId;
          const msgSearchHighlights = searchActive && searchQuery
            ? searchMatches.filter(m => filteredMessages[m.msgIdx]?.id === msg.id)
            : [];
          return (
            <React.Fragment key={msg.id}>
              {showSeparator && (() => {
                const gapLabel = gapMinutes >= 60
                  ? `${Math.floor(gapMinutes / 60)}h ${gapMinutes % 60}m`
                  : `${gapMinutes}m`;
                return <Text dimColor>{`── ${gapLabel} gap ──`}</Text>;
              })()}
              {/* Subtask group separator: shown before subtask_started events */}
              {msg.eventType === 'subtask_started' && (() => {
                const desc = msg.text.replace(/^▶\s*/, '').split('\n')[0] ?? '';
                const label = desc.length > 30 ? desc.slice(0, 27) + '…' : desc;
                const sepWidth = Math.max(3, (terminalWidth ?? 80) - label.length - 16);
                return <Text dimColor>{`\n── Subtask: ${label} ${'─'.repeat(sepWidth)}`}</Text>;
              })()}
              <ChatMessageItem
                msg={msg}
                isExpanded={expandAll || expandedIds.has(msg.id) || isCurrentMatch}
                isSelected={isFocused && msg.id === selectedMsgId}
                terminalWidth={terminalWidth}
                isSearchMatch={isSearchMatch}
                isCurrentMatch={isCurrentMatch}
                searchHighlights={msgSearchHighlights.length > 0 ? msgSearchHighlights : undefined}
                searchQueryLen={searchQuery.length || undefined}
              />
            </React.Fragment>
          );
        })}
      </Box>
    </Box>
  );
};

export default ChatLog;
