import { useMemo } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import type { HourlyDensity } from "@/api";

const PROJECT_COLORS = [
  "#0d5c63",
  "#6d28d9",
  "#b45309",
  "#be185d",
  "#0e7490",
  "#4d7c0f",
  "#c2410c",
  "#7e22ce",
];

function formatHour(h: number): string {
  return `${String(h).padStart(2, "0")}:00`;
}

type Props = {
  data: HourlyDensity[];
};

export function DailyDistributionChart({ data }: Props) {
  const { chartData, projectNames, peakHour } = useMemo(() => {
    const projects = new Set<string>();
    for (const d of data) {
      for (const p of Object.keys(d.by_project)) projects.add(p);
    }
    const names = Array.from(projects);

    let peak = 0;
    let peakVal = 0;
    const rows = data.map((d) => {
      if (d.density > peakVal) {
        peakVal = d.density;
        peak = d.hour;
      }
      const row: Record<string, number | string> = {
        hour: formatHour(d.hour),
        hourNum: d.hour,
        density: d.density,
      };
      for (const p of names) {
        row[p] = d.by_project[p] ?? 0;
      }
      return row;
    });

    return { chartData: rows, projectNames: names, peakHour: peak };
  }, [data]);

  const currentHour = new Date().getHours();
  const currentMinute = new Date().getMinutes();
  const nowLabel = `${String(currentHour).padStart(2, "0")}:${String(currentMinute).padStart(2, "0")}`;

  return (
    <div className="rounded-xl border border-border bg-card p-5 shadow-sm">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="font-display text-body-sm font-medium tracking-wide">
            Daily Coding Distribution
          </h2>
          <p className="text-eyebrow text-muted-foreground">hour · density</p>
        </div>
      </div>

      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart
            data={chartData}
            margin={{ top: 8, right: 8, left: -20, bottom: 0 }}
          >
            <defs>
              <linearGradient id="densityFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#0d5c63" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#0d5c63" stopOpacity={0.02} />
              </linearGradient>
              {projectNames.map((name, i) => (
                <linearGradient
                  key={name}
                  id={`proj-${i}`}
                  x1="0"
                  y1="0"
                  x2="0"
                  y2="1"
                >
                  <stop
                    offset="5%"
                    stopColor={PROJECT_COLORS[i % PROJECT_COLORS.length]}
                    stopOpacity={0.15}
                  />
                  <stop
                    offset="95%"
                    stopColor={PROJECT_COLORS[i % PROJECT_COLORS.length]}
                    stopOpacity={0.01}
                  />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="var(--border)"
              vertical={false}
            />
            <XAxis
              dataKey="hour"
              tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              tickLine={false}
              axisLine={{ stroke: "var(--border)" }}
              interval={2}
            />
            <YAxis
              tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              tickLine={false}
              axisLine={false}
              width={40}
            />
            <Tooltip
              contentStyle={{
                background: "var(--card)",
                border: "1px solid var(--border)",
                borderRadius: "8px",
                fontSize: "12px",
                fontFamily: "var(--font-mono)",
              }}
              labelStyle={{ fontWeight: 600, marginBottom: 4 }}
            />
            <ReferenceLine
              x={nowLabel}
              stroke="var(--ember)"
              strokeDasharray="4 4"
              label={{
                value: nowLabel,
                position: "top",
                fill: "var(--ember)",
                fontSize: 11,
                fontFamily: "var(--font-mono)",
              }}
            />
            {projectNames.map((name, i) => (
              <Area
                key={name}
                type="monotone"
                dataKey={name}
                stackId={undefined}
                stroke={PROJECT_COLORS[i % PROJECT_COLORS.length]}
                strokeWidth={1.2}
                fill={`url(#proj-${i})`}
                fillOpacity={0.6}
              />
            ))}
            <Area
              type="monotone"
              dataKey="density"
              stroke="#0d5c63"
              strokeWidth={2.5}
              fill="url(#densityFill)"
              fillOpacity={1}
              name="Total"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
