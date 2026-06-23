import * as React from "react";
import { cn } from "@/lib/utils";

interface SidebarPanelProps {
  title: string;
  /** Short metric shown when collapsed, e.g. "3/5 online" */
  summary?: string;
  /** Badge variant for summary */
  summaryVariant?: "ok" | "degraded" | "error" | "unavailable" | "closed" | "open" | "half_open";
  collapsed?: boolean;
  onToggle?: () => void;
  stale?: boolean;
  children: React.ReactNode;
}

const SUMMARY_VARIANT_CLASS: Record<string, string> = {
  ok: "badge-ok",
  degraded: "badge-degraded",
  error: "badge-error",
  unavailable: "badge-unavailable",
  closed: "badge-closed",
  open: "badge-open",
  half_open: "badge-half_open",
};

export const SidebarPanel = React.forwardRef<HTMLDivElement, SidebarPanelProps>(
  ({ title, summary, summaryVariant, collapsed, onToggle, stale, children }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "rounded-xl border border-[var(--border-color)] bg-[var(--bg-surface)] shadow-sm transition-all duration-200 overflow-hidden",
          stale && "opacity-70",
        )}
      >
        {/* Header row — always visible */}
        <button
          onClick={onToggle}
          className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-[var(--bg-surface-alt)]/30 transition-colors duration-200"
          aria-expanded={!collapsed}
        >
          <span className="text-sm font-semibold text-[var(--text-primary)]">{title}</span>
          <div className="flex items-center gap-2">
            {summary && (
              <span
                className={cn(
                  "text-[11px] px-1.5 py-0.5 rounded font-medium",
                  summaryVariant ? SUMMARY_VARIANT_CLASS[summaryVariant] : "bg-[var(--bg-surface-alt)] text-[var(--text-secondary)]",
                )}
              >
                {summary}
              </span>
            )}
            <svg
              xmlns="http://www.w3.org/2000/svg"
              className={cn(
                "h-4 w-4 text-[var(--text-muted)] transition-transform duration-200",
                !collapsed && "rotate-180",
              )}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
            </svg>
          </div>
        </button>

        {/* Content — collapsible */}
        {!collapsed && (
          <div className="px-4 pb-4 pt-1">
            {stale && (
              <div className="stale-badge inline-block text-[10px] px-1.5 py-0.5 rounded font-medium mb-2">
                STALE
              </div>
            )}
            {children}
          </div>
        )}
      </div>
    );
  },
);
SidebarPanel.displayName = "SidebarPanel";
