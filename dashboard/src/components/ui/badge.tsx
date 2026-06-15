import * as React from "react";
import { cn } from "@/lib/utils";

const Badge = React.forwardRef<
  HTMLSpanElement,
  React.HTMLAttributes<HTMLSpanElement> & {
    variant?: "ok" | "degraded" | "error" | "unavailable" | "closed" | "open" | "half_open";
  }
>(({ className, variant, ...props }, ref) => (
  <span
    ref={ref}
    className={cn(
      "text-xs px-1.5 py-0.5 rounded",
      variant && `badge-${variant}`,
      className,
    )}
    {...props}
  />
));
Badge.displayName = "Badge";

export { Badge };
