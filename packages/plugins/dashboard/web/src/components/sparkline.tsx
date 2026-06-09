import * as React from "react";
import { cn } from "@/lib/utils";

type SparklineProps = {
  entries: Array<{ label: string; value: number }>;
  className?: string;
};

export function Sparkline({ entries, className }: SparklineProps) {
  if (!entries.length) return null;
  const max = Math.max(...entries.map((e) => e.value), 1);

  return (
    <div className={cn("mt-2 flex h-[2.4rem] items-end gap-1", className)} role="img" aria-label="Value distribution">
      {entries.map((entry) => (
        <div key={entry.label} className="flex flex-1 flex-col items-center gap-0.5" style={{ height: "100%", justifyContent: "flex-end" }}>
          <div
            className="w-full min-h-[2px] rounded-sm bg-primary opacity-60"
            style={{ height: `${Math.max((entry.value / max) * 100, 8)}%` }}
          />
          <span className="font-mono text-[0.6rem] text-muted-foreground">{entry.label}</span>
        </div>
      ))}
    </div>
  );
}
