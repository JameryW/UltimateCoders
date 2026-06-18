import * as React from "react";
import { cn } from "@/lib/utils";

const Card = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & { stale?: boolean }
>(({ className, stale, children, ...props }, ref) => (
  <div
    ref={ref}
    className={cn(
      "rounded-lg border border-[var(--border-color)] bg-[var(--bg-surface)] p-4 relative",
      stale && "opacity-70",
      className,
    )}
    {...props}
  >
    {stale && (
      <div className="stale-badge absolute top-2 left-2 text-[10px] px-1.5 py-0.5 rounded font-medium z-10">
        STALE
      </div>
    )}
    {children}
  </div>
));
Card.displayName = "Card";

const CardHeader = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn(
      "flex items-center justify-between mb-3",
      className,
    )}
    {...props}
  />
));
CardHeader.displayName = "CardHeader";

const CardTitle = React.forwardRef<
  HTMLHeadingElement,
  React.HTMLAttributes<HTMLHeadingElement>
>(({ className, ...props }, ref) => (
  <h2
    ref={ref}
    className={cn(
      "text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide",
      className,
    )}
    {...props}
  />
));
CardTitle.displayName = "CardTitle";

export { Card, CardHeader, CardTitle };
