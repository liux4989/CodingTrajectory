import * as React from "react";
import { ResponsiveContainer, Tooltip } from "recharts";

import { cn } from "@/lib/utils";

function ChartContainer({ className, children, ...props }: React.ComponentProps<"div"> & { children: React.ReactElement }) {
  return (
    <div
      data-slot="chart"
      className={cn("flex min-h-64 w-full justify-center text-xs [&_.recharts-cartesian-axis-tick_text]:fill-muted-foreground [&_.recharts-cartesian-grid_line]:stroke-border/60 [&_.recharts-surface]:outline-hidden", className)}
      {...props}
    >
      <ResponsiveContainer initialDimension={{ width: 640, height: 320 }}>{children}</ResponsiveContainer>
    </div>
  );
}

const ChartTooltip = Tooltip;

type TooltipEntry = {
  dataKey?: string | number;
  name?: string | number;
  value?: number;
  payload?: unknown;
};

type ChartTooltipContentProps = {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: unknown;
  formatter?: (value: number, name: string, item: TooltipEntry, index: number, payload: unknown) => React.ReactNode;
};

function ChartTooltipContent({ active, payload, label, formatter }: ChartTooltipContentProps) {
  if (!active || !payload?.length) return null;
  return (
    <div className="grid min-w-36 gap-2 rounded-lg border border-border bg-background px-3 py-2 text-xs shadow-lg">
      <p className="m-0 font-display font-semibold">{String(label ?? "")}</p>
      {payload.map((item) => (
        <div key={String(item.dataKey)} className="flex items-center justify-between gap-4">
          <span className="text-muted-foreground">{item.name}</span>
          <span className="font-mono font-medium tabular-nums">{formatter ? formatter(Number(item.value), String(item.name), item, 0, item.payload) : Number(item.value).toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

export { ChartContainer, ChartTooltip, ChartTooltipContent };
