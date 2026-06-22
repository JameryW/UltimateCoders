# Research: Dashboard UI Audit

- **Query**: Audit current dashboard UI for optimization needs
- **Scope**: Internal (all dashboard source files)
- **Date**: 2026-06-21

## Findings

### Files Found

| File Path | Description |
|---|---|
| `dashboard/src/App.tsx` | Main layout, grid composition, auth/loading/error gates |
| `dashboard/src/index.css` | CSS variables, theme definitions, utility classes |
| `dashboard/src/components/ui/card.tsx` | Card, CardHeader, CardTitle components |
| `dashboard/src/components/ui/badge.tsx` | Badge component with variant prop |
| `dashboard/src/components/ui/toast.tsx` | Toast notification system |
| `dashboard/src/components/ui/confirm-dialog.tsx` | Confirm dialog modal |
| `dashboard/src/components/ui/ErrorBoundary.tsx` | Panel-level error boundary |
| `dashboard/src/components/layout/Header.tsx` | Top header with nav, connection status, theme toggle |
| `dashboard/src/components/layout/ConnectionIndicator.tsx` | Fixed bottom-right connection status |
| `dashboard/src/components/forms/TaskSubmitForm.tsx` | Task submission form |
| `dashboard/src/components/panels/HealthPanel.tsx` | Engine health display |
| `dashboard/src/components/panels/WorkersPanel.tsx` | Workers list with expand |
| `dashboard/src/components/panels/TasksPanel.tsx` | Tasks list with filter + expand |
| `dashboard/src/components/panels/CircuitBreakerPanel.tsx` | Circuit breaker + rate limiter |
| `dashboard/src/components/panels/EventLogPanel.tsx` | Event log with filter + search |
| `dashboard/src/components/panels/SchedulerPanel.tsx` | Scheduler status + jobs |
| `dashboard/src/components/panels/SearchPanel.tsx` | Code search via gRPC-Web |
| `dashboard/src/components/panels/FileBrowser.tsx` | Multi-repo file browser with syntax highlighting |
| `dashboard/src/components/panels/TaskDetail.tsx` | Expanded task detail (progress, timeline, subtasks, DAG, interaction log) |
| `dashboard/src/components/panels/InteractionLog.tsx` | LLM/tool interaction timeline |
| `dashboard/src/components/charts/TaskTrendChart.tsx` | Custom SVG bar chart for task activity |
| `dashboard/src/lib/utils.ts` | Utility functions (cn, formatUptime, statusBadgeClass, etc.) |
| `dashboard/src/types/dashboard.ts` | All TypeScript type definitions |

---

## Concrete Issues

### P1: Broken / Ugly

#### 1. FileBrowser not wrapped in Card, inconsistent with all other panels
- **File**: `dashboard/src/components/panels/FileBrowser.tsx:190-284`
- The FileBrowser component renders raw `<div>` elements instead of using the `<Card>` / `<CardHeader>` / `<CardTitle>` pattern that every other panel uses.
- In `App.tsx:407`, the outer `<div id="files">` does not add a Card wrapper either.
- The FileBrowser has no title header ("Files" or "File Browser"), no stale indicator, and no visual card boundary.
- **Fix**: Wrap FileBrowser content in `<Card>` / `<CardHeader>` / `<CardTitle>` like other panels.

#### 2. TaskTrendChart does not use Card component
- **File**: `dashboard/src/components/charts/TaskTrendChart.tsx:85`
- Uses a raw `<div>` with `className="rounded-lg border border-[var(--border-color)] bg-[var(--bg-surface)] p-4 relative"` instead of the `<Card>` component.
- Has its own duplicate stale-badge rendering (lines 87-90) that duplicates Card's stale logic.
- Has its own custom title (`<h2>`) instead of using `<CardTitle>`.
- **Fix**: Refactor to use `<Card>` / `<CardHeader>` / `<CardTitle>`; remove duplicate stale handling.

#### 3. SearchPanel does not use Card stale prop
- **File**: `dashboard/src/components/panels/SearchPanel.tsx:54`
- Uses `<Card className="md:col-span-2">` but never passes `stale` prop.
- Other panels pass `stale={stale}` but SearchPanel has no `stale` prop in its interface.
- When gRPC is disconnected, the panel shows no visual indicator that data may be stale.
- **Fix**: Add `stale` prop to SearchPanel and pass it through.

#### 4. FileBrowser has no loading state for repo list
- **File**: `dashboard/src/components/panels/FileBrowser.tsx:38-45`
- The `useEffect` that loads repos has a `.catch(() => { /* ignore */ })` with no error or loading state displayed.
- If the repo list request fails silently, the user sees an empty repo dropdown with no feedback.
- **Fix**: Add loading/error states for the initial repo fetch.

#### 5. Toast component hardcodes dark-mode colors
- **File**: `dashboard/src/components/ui/toast.tsx:44-46`
- Uses `bg-green-900 border-green-500 text-green-200` for success and `bg-red-900 border-red-500 text-red-200` for error.
- No light-theme override exists in `index.css`.
- In light mode, the dark-background toasts look jarring and potentially unreadable.
- **Fix**: Use CSS variable-based or theme-aware classes, or add light-theme overrides.

#### 6. Confirm dialog "Confirm" button always uses destructive red styling
- **File**: `dashboard/src/components/ui/confirm-dialog.tsx:79`
- Uses `bg-red-800 text-red-200 hover:bg-red-700` for the confirm button regardless of action.
- Actions like "Flush Pending Tasks" or "Trigger Job" are not destructive, but the red button implies danger.
- **Fix**: Accept a variant prop or use a neutral/blue confirm style for non-destructive actions.

---

### P2: Inconsistent

#### 7. Inconsistent Card title styling between panels
- **File**: `dashboard/src/components/ui/card.tsx:49`
- `CardTitle` uses `text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide`.
- `TaskSubmitForm.tsx:62` duplicates this exact class string on a raw `<h2>` instead of using `<CardTitle>`.
- `TaskTrendChart.tsx:93` has its own `<h2>` with `text-sm font-semibold text-[var(--text-primary)] uppercase tracking-wide` (uses `--text-primary` not `--text-secondary`).
- **Fix**: Use `<CardTitle>` consistently everywhere; or if TaskTrendChart needs different color, use a className override.

#### 8. Inconsistent "unavailable" messaging across panels
- HealthPanel (line 46): `"Engine not available"`
- WorkersPanel (line 83): `"Workers not available"`
- TasksPanel (line 74): `"Tasks not available"`
- CircuitBreakerPanel (line 37): `"Circuit Breaker not available"`
- SchedulerPanel (line 39): `"Scheduler not available"`
- Some use `<Badge variant="unavailable">`, some don't. Workers and Tasks panels show no Badge when unavailable.
- **Fix**: Standardize: always show Badge + consistent message format.

#### 9. Inconsistent expand/collapse patterns
- WorkersPanel uses `useState<string | null>` for `expandedWorkerId` (line 67).
- TasksPanel uses `useState<string | null>` for `expandedTaskId` (line 32).
- Both have the same pattern but duplicate the toggle logic instead of a shared hook.
- **Fix**: Extract a `useToggleExpanded` hook for consistency.

#### 10. Inconsistent Card className application
- TasksPanel (line 56): `<Card className="md:col-span-2" stale={stale}>` -- applies col-span on Card itself.
- SearchPanel (line 54): `<Card className="md:col-span-2">` -- same.
- HealthPanel, CircuitBreaker, Workers: col-span is applied on the outer `<div>` in App.tsx (lines 383, 386, 389).
- Some panels get their col-span from App.tsx, others apply it internally. This is inconsistent.
- **Fix**: Apply all grid layout classes in App.tsx consistently, or all internally consistently.

#### 11. EventLogPanel uses CSS class-based event colors, other panels use inline Tailwind
- **File**: `dashboard/src/components/panels/EventLogPanel.tsx:7-33`
- Uses `eventTypeColor()` returning Tailwind classes (e.g., `"text-blue-500"`) and `eventTypeBg()` returning custom CSS classes (e.g., `"evt-submitted"`).
- The bg classes are defined in `index.css:110-128` with theme overrides.
- But the text color classes (`text-blue-500`, `text-green-500`, etc.) have no light-theme overrides and may be hard to read on light backgrounds.
- **Fix**: Use theme-aware CSS classes for both text and bg, or use CSS variables.

#### 12. Inconsistent input field styling
- SearchPanel (line 66): `bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm ... focus:border-blue-500 focus:outline-none`
- EventLogPanel search (line 109): `bg-[var(--bg-surface-alt)] border border-[var(--border-color)] rounded px-2 py-1 text-xs ... focus:outline-none focus:border-[var(--text-secondary)]`
- FileBrowser repo selector (line 163): `bg-[var(--bg-surface-alt)] text-[var(--text-primary)] border border-[var(--border-color)] rounded px-2 py-1`
- TaskSubmitForm textarea (line 75): `bg-[var(--bg-primary)] border border-[var(--border-color)] rounded-md px-3 py-2 text-sm ... focus:border-blue-500 focus:outline-none`
- Differences: `bg-primary` vs `bg-surface-alt`, `rounded` vs `rounded-md`, `py-2` vs `py-1`, `text-sm` vs `text-xs`, focus color `blue-500` vs `--text-secondary`.
- **Fix**: Create a shared input style class or component.

#### 13. TaskDetail subtask list has hardcoded max-width
- **File**: `dashboard/src/components/panels/TaskDetail.tsx:189`
- `truncate max-w-[200px]` on subtask description -- this is a fixed pixel width that won't adapt to panel width.
- **Fix**: Use `flex-1 min-w-0 truncate` or similar responsive approach.

#### 14. Event timeline uses emoji icons that may not render consistently
- **File**: `dashboard/src/components/panels/TaskDetail.tsx:143-147`
- `typeIcon` map uses emoji characters (📤, ▶️, 👤, ✅, ❌, 🔧, 📋, 🏁).
- These render differently across platforms and may not align well with text.
- **Fix**: Replace with SVG icons or consistent symbol characters.

---

### P3: Nice-to-Have

#### 15. FileBrowser uses emoji for file/folder icons
- **File**: `dashboard/src/components/panels/FileBrowser.tsx:268-269`
- Uses "📁" and "📄" emoji for directory/file icons.
- Renders inconsistently across platforms; not accessible to screen readers.
- **Fix**: Use SVG icons (e.g., Lucide folder/file icons).

#### 16. No skeleton/loading placeholders for panels
- HealthPanel, WorkersPanel, CircuitBreakerPanel, SchedulerPanel show no loading skeleton.
- Only FileBrowser has a `Loading...` text with `animate-pulse` (line 257).
- The App.tsx has a full-page spinner (lines 331-338), but once loaded, individual panels show no progressive loading state.
- **Fix**: Add skeleton placeholders to each panel for perceived performance.

#### 17. ConnectionIndicator uses Unicode symbols (●, ◐, ○, ↻, ⏳)
- **File**: `dashboard/src/components/layout/ConnectionIndicator.tsx:32-34,44-46`
- These symbols may not render consistently or be accessible.
- **Fix**: Use SVG indicators or ARIA-labeled spans.

#### 18. TaskTrendChart SVG has no ARIA label
- **File**: `dashboard/src/components/charts/TaskTrendChart.tsx:117`
- The `<svg>` element has no `aria-label` or `role="img"`.
- Screen readers cannot interpret the chart.
- **Fix**: Add `role="img"` and `aria-label="Task activity bar chart"`.

#### 19. No responsive behavior for TaskSubmitForm
- **File**: `dashboard/src/components/forms/TaskSubmitForm.tsx:69`
- Uses `flex-col md:flex-row` for the form, which is good.
- But the Project ID input has a fixed `md:w-40` width that may be too narrow for long IDs.
- **Fix**: Consider making it slightly wider or auto-expanding.

#### 20. WorkersPanel shows capabilities in both summary and detail
- **File**: `dashboard/src/components/panels/WorkersPanel.tsx:129-140` (summary) and `47-60` (detail)
- Capabilities are shown in the collapsed worker row AND again in the expanded detail.
- This is redundant and takes up space.
- **Fix**: Show capabilities only in the expanded detail view.

#### 21. No pagination or virtualization for long lists
- TasksPanel (line 101): `max-h-[600px] overflow-y-auto` -- DOM rendering for all tasks.
- EventLogPanel (line 119): `max-h-64 overflow-y-auto` -- same.
- With many items, this could cause performance issues.
- **Fix**: Consider virtualized lists for large datasets.

#### 22. Header nav section "CB" is cryptic
- **File**: `dashboard/src/components/layout/Header.tsx:29`
- `{ hash: "circuit-breaker", label: "CB" }` -- "CB" is not immediately understandable.
- **Fix**: Use "Breaker" or "Circuit" as a shorter but clearer label.

#### 23. App.tsx grid layout has unequal panel heights
- **File**: `dashboard/src/App.tsx:381`
- `grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4`
- HealthPanel spans 2 cols, CircuitBreaker 1, Workers 1.
- CircuitBreaker has substantial content (CB + rate limiter sections) but is squeezed into 1 col while Health spans 2.
- Workers also has expandable content but only 1 col.
- **Fix**: Consider giving CircuitBreaker 2 cols on xl, or adjusting the layout for better balance.

#### 24. Stale badge positioning overlaps with Card content
- **File**: `dashboard/src/components/ui/card.tsx:18`
- `absolute top-2 left-2` for stale badge overlaps with CardHeader content.
- The Card padding is `p-4`, so the badge sits in the same space as the title.
- **Fix**: Add top padding when stale, or position the badge differently (top-right, or inline with title).

#### 25. No focus-visible styles on interactive elements
- WorkersPanel expandable rows (line 101), TasksPanel expandable rows (line 117), EventLogPanel filter buttons (line 91).
- These use `role="button" tabIndex={0}` but have no visible focus indicator.
- Keyboard-only users have no way to see which element is focused.
- **Fix**: Add `focus:ring-2 focus:ring-blue-500 focus:outline-none` or similar.

#### 26. LoginModal input has no label element
- **File**: `dashboard/src/App.tsx:64-71`
- The password input uses `placeholder="Password"` but has no associated `<label>`.
- While `autoFocus` helps, screen readers benefit from an explicit label.
- **Fix**: Add `<label htmlFor>` or wrap with a visible label.

#### 27. Highlight.js applied only to first code line
- **File**: `dashboard/src/components/panels/FileBrowser.tsx:232`
- `ref={i === 0 ? codeRef : undefined}` means only the first line's `<code>` element gets highlighted.
- Subsequent lines render without syntax highlighting.
- This is a known limitation of `highlightElement` which works on a single element.
- **Fix**: Highlight the entire content block, or use `hljs.highlightAuto` on the full text and render as a single `<code>` block.

---

## Related Specs

No spec files found under `.trellis/spec/` specifically for the dashboard UI.

## Caveats / Not Found

- Could not verify mobile responsiveness issues without running the app; findings are based on code review only.
- The `highlight.js` language detection (`FileBrowser.tsx:234`) depends on `fileContent.language` being set correctly by the backend -- if not, no highlighting occurs.
- The light theme CSS overrides in `index.css` are thorough for badges/status/action buttons, but incomplete for toast notifications and some inline Tailwind color classes used in panels.
