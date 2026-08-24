import { useMemo } from "react";
import type { ApexOptions } from "apexcharts";
import { ApexChart, useApexTheme, withAlpha } from "@/components/ui/apex-chart";
import type { ProjectTrend } from "@/api";

type Props = {
  data: ProjectTrend[];
};

function formatDuration(seconds: number): string {
  if (!seconds) return "0m";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

const HEAT_STEPS = 5;

export function ProjectTrendChart({ data }: Props) {
  const theme = useApexTheme();

  const { allDates, maxSeconds, series } = useMemo(() => {
    const dates = new Set<string>();
    let max = 0;
    for (const p of data) {
      for (const d of p.days) {
        dates.add(d.date);
        if (d.seconds > max) max = d.seconds;
      }
    }
    const ordered = Array.from(dates).sort();
    const rows = data.map((project) => {
      const dayMap = new Map(project.days.map((d) => [d.date, d.seconds]));
      return {
        name: project.project_name,
        data: ordered.map((date) => ({ x: formatDate(date), y: dayMap.get(date) ?? 0 })),
      };
    });
    return { allDates: ordered, maxSeconds: max, series: rows };
  }, [data]);

  const options = useMemo<ApexOptions>(() => {
    // Even ranges from muted (no activity) to full primary, mirroring the
    // intensity ramp of the former hand-rolled grid.
    const ranges = [
      { from: 0, to: 0, color: theme.grid, name: "none" },
      ...Array.from({ length: HEAT_STEPS }, (_, index) => ({
        from: index === 0 ? 1 : Math.round((maxSeconds * index) / HEAT_STEPS),
        to: Math.round((maxSeconds * (index + 1)) / HEAT_STEPS),
        color: withAlpha(theme.primary, 0.2 + (0.8 * (index + 1)) / HEAT_STEPS),
        name: `level ${index + 1}`,
      })),
    ];
    return {
      chart: { type: "heatmap" },
      plotOptions: { heatmap: { radius: 2, enableShades: false, colorScale: { ranges } } },
      dataLabels: { enabled: false },
      stroke: { width: 2, colors: [theme.card] },
      xaxis: {
        type: "category",
        tickAmount: Math.min(8, allDates.length),
        labels: { style: { fontSize: "10px", fontFamily: theme.monoFont } },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: {
        labels: {
          maxWidth: 140,
          style: { fontSize: "11px", fontFamily: theme.monoFont, colors: theme.foreground },
        },
      },
      legend: { show: false },
      tooltip: {
        y: { formatter: (value) => formatDuration(Number(value)) },
      },
    };
  }, [allDates.length, maxSeconds, theme]);

  return (
    <div className="rounded-xl border border-border bg-card p-5 shadow-sm">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="font-display text-body-sm font-medium tracking-wide">
            Project Trend
          </h2>
          <p className="text-eyebrow text-muted-foreground">project · days</p>
        </div>
      </div>

      <ApexChart
        type="heatmap"
        series={series}
        options={options}
        height={Math.max(200, data.length * 34 + 80)}
        ariaLabel="Coding time heatmap by project and day"
      />
    </div>
  );
}
