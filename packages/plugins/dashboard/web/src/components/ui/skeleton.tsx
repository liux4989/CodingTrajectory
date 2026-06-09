import * as React from "react";
import { cn } from "@/lib/utils";

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn("animate-pulse rounded-md bg-muted", className)}
      {...props}
    />
  );
}

function MetricSkeleton() {
  return (
    <div className="grid gap-2 rounded-[1.4rem] border border-foreground/13 bg-card p-4 dark:border-[rgb(255_255_255/8%)]">
      <Skeleton className="h-3.5 w-[60%]" />
      <Skeleton className="h-10 w-[40%]" />
      <Skeleton className="h-3.5 w-[60%]" />
    </div>
  );
}

function TableSkeleton({ rows = 5, cols = 3 }: { rows?: number; cols?: number }) {
  return (
    <div className="grid overflow-hidden rounded-[1.2rem] border border-foreground/13 dark:border-[rgb(255_255_255/8%)]">
      {Array.from({ length: rows }, (_, row) => (
        <div key={row} className="grid grid-cols-3 gap-4 border-b border-foreground/6 p-3.5 last:border-b-0 dark:border-[rgb(255_255_255/4%)]">
          {Array.from({ length: cols }, (_, col) => (
            <Skeleton key={col} className="h-5 w-[80%]" />
          ))}
        </div>
      ))}
    </div>
  );
}

export { Skeleton, MetricSkeleton, TableSkeleton };
