import * as React from "react";
import { cn } from "../lib/utils";

type SparklineProps = {
  entries: Array<{ label: string; value: number }>;
  className?: string;
};

export function Sparkline({ entries, className }: SparklineProps) {
  if (!entries.length) return null;
  const max = Math.max(...entries.map((e) => e.value), 1);

  return (
    <div className={cn("sparkline", className)} role="img" aria-label="Value distribution">
      {entries.map((entry) => (
        <div key={entry.label} className="sparkline-bar-group">
          <div className="sparkline-bar" style={{ blockSize: `${Math.max((entry.value / max) * 100, 8)}%` }} />
          <span className="sparkline-label">{entry.label}</span>
        </div>
      ))}
    </div>
  );
}
