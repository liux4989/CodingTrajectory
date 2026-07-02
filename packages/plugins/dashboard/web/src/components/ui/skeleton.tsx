import * as React from "react";
import { cn } from "@/lib/utils";

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      className={cn("animate-shimmer rounded-md bg-muted", className)}
      {...props}
    />
  );
}

function MetricSkeleton() {
  return (
    <div className="grid gap-2 rounded-2xl border border-border-soft bg-card p-4">
      <Skeleton className="h-3.5 w-[60%]" />
      <Skeleton className="h-10 w-[40%]" />
      <Skeleton className="h-3.5 w-[60%]" />
    </div>
  );
}

function TableSkeleton({ rows = 5, cols = 3 }: { rows?: number; cols?: number }) {
  return (
    <div className="grid overflow-hidden rounded-2xl border border-border-soft">
      {Array.from({ length: rows }, (_, row) => (
        <div key={row} className="grid grid-cols-3 gap-4 border-b border-border-subtle p-3.5 last:border-b-0">
          {Array.from({ length: cols }, (_, col) => (
            <Skeleton key={col} className="h-5 w-[80%]" />
          ))}
        </div>
      ))}
    </div>
  );
}

export { Skeleton, MetricSkeleton, TableSkeleton };
