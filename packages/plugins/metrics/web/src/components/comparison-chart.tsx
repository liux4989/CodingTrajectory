import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";

import type { ChartPoint } from "@/api";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { formatMetricValue, metricFormatForChart, seriesLabels } from "@/lib/format";

type ComparisonChartProps = {
  category: "tokens" | "cost" | "execution";
  chart: string;
  points: ChartPoint[];
};

export function ComparisonChart({ category, chart, points }: ComparisonChartProps) {
  const labels = seriesLabels(category, chart);
  const format = metricFormatForChart(category, chart);
  return (
    <ChartContainer className="h-[min(34rem,max(22rem,calc(var(--point-count)*2.75rem)))]" style={{ "--point-count": points.length } as React.CSSProperties} aria-label={`${chart} comparison chart`}>
      <BarChart accessibilityLayer data={points} layout="vertical" margin={{ left: 8, right: 24, top: 8, bottom: 8 }}>
        <CartesianGrid horizontal={false} />
        <XAxis type="number" tickFormatter={(value) => formatMetricValue(Number(value), format, true)} />
        <YAxis type="category" dataKey="label" width={170} tickLine={false} axisLine={false} />
        <ChartTooltip content={<ChartTooltipContent formatter={(value) => formatMetricValue(Number(value), format)} />} />
        <Bar dataKey="primary" name={labels[0]} fill="var(--chart-1)" radius={4} />
        {labels[1] ? <Bar dataKey="secondary" name={labels[1]} fill="var(--chart-2)" radius={4} /> : null}
        {labels[2] ? <Bar dataKey="tertiary" name={labels[2]} fill="var(--chart-3)" radius={4} /> : null}
      </BarChart>
    </ChartContainer>
  );
}
