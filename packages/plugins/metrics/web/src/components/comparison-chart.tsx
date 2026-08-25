import * as React from "react";
import type { ApexOptions } from "apexcharts";

import type { ChartPoint } from "@/api";
import { ApexChart } from "@/components/ui/apex-chart";
import { formatMetricValue, metricFormatForChart, seriesLabels } from "@/lib/format";

type ComparisonChartProps = {
  category: "tokens" | "cost" | "execution";
  chart: string;
  points: ChartPoint[];
};

export function ComparisonChart({ category, chart, points }: ComparisonChartProps) {
  const labels = seriesLabels(category, chart);
  const format = metricFormatForChart(category, chart);

  const series = React.useMemo(
    () =>
      [
        { name: labels[0], data: points.map((point) => point.primary) },
        labels[1] ? { name: labels[1], data: points.map((point) => point.secondary ?? 0) } : null,
        labels[2] ? { name: labels[2], data: points.map((point) => point.tertiary ?? 0) } : null,
      ].filter((entry): entry is { name: string; data: number[] } => entry !== null),
    [labels, points],
  );

  const options = React.useMemo<ApexOptions>(
    () => ({
      plotOptions: { bar: { horizontal: true, borderRadius: 4, barHeight: "62%" } },
      dataLabels: { enabled: false },
      xaxis: {
        categories: points.map((point) => point.label),
        labels: { formatter: (value) => formatMetricValue(Number(value), format, true) },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: { labels: { style: { fontSize: "11px" }, maxWidth: 220 } },
      legend: { show: series.length > 1, position: "bottom", horizontalAlign: "left" },
      tooltip: {
        shared: true,
        intersect: false,
        y: { formatter: (value) => (value == null ? "Unavailable" : formatMetricValue(Number(value), format)) },
      },
    }),
    [points, format, series.length],
  );

  return (
    <ApexChart
      type="bar"
      series={series}
      options={options}
      height={Math.min(544, Math.max(352, points.length * 44))}
      ariaLabel={`${chart} comparison chart`}
    />
  );
}
