import * as React from "react";
import { cn } from "@/lib/utils";

const headerSurface =
  "border border-border-soft bg-card p-[clamp(1rem,2.5vw,1.75rem)] shadow-sm";

type RouteHeaderProps = {
  eyebrow: string;
  title: string;
  action?: React.ReactNode;
};

export function RouteHeader({ eyebrow, title, action }: RouteHeaderProps) {
  return (
    <header
      className={cn("flex items-start justify-between gap-4 rounded-2xl", headerSurface)}
    >
      <div>
        <p className="eyebrow mb-1 text-muted-foreground">
          {eyebrow}
        </p>
        <h2
          className="m-0 max-w-[26ch] font-display text-[clamp(1.375rem,2.5vw,1.875rem)] font-semibold leading-tight tracking-tight text-wrap-balance"
        >
          {title}
        </h2>
      </div>
      {action}
    </header>
  );
}
