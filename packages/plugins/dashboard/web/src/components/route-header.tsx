import * as React from "react";
import { cn } from "@/lib/utils";

const headerSurface =
  "border border-border-soft bg-[image:var(--gradient-header)] p-[clamp(1rem,3vw,2.2rem)] shadow-popover";

type RouteHeaderProps = {
  eyebrow: string;
  title: string;
  action?: React.ReactNode;
};

export function RouteHeader({ eyebrow, title, action }: RouteHeaderProps) {
  return (
    <header
      className={cn("flex items-start justify-between gap-4 rounded-3xl", headerSurface)}
    >
      <div>
        <p className="eyebrow mb-1 text-primary">
          {eyebrow}
        </p>
        <h2
          className="m-0 max-w-[22ch] font-display text-[clamp(1.5rem,3.5vw,2.75rem)] leading-tight tracking-tight text-wrap-balance"
        >
          {title}
        </h2>
      </div>
      {action}
    </header>
  );
}
