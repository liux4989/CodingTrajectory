import * as React from "react";

export function Skeleton({ className }: { className?: string }) {
  return <div className={`skeleton ${className ?? ""}`} />;
}

export function MetricSkeleton() {
  return (
    <div className="skeleton-metric">
      <Skeleton className="skeleton-line-sm" />
      <Skeleton className="skeleton-line-lg" />
      <Skeleton className="skeleton-line-sm" />
    </div>
  );
}

export function TableSkeleton({ rows = 5, cols = 3 }: { rows?: number; cols?: number }) {
  return (
    <div className="skeleton-table">
      {Array.from({ length: rows }, (_, row) => (
        <div key={row} className="skeleton-row">
          {Array.from({ length: cols }, (_, col) => (
            <Skeleton key={col} className="skeleton-cell" />
          ))}
        </div>
      ))}
    </div>
  );
}
