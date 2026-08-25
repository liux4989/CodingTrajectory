import { useMemo } from "react";
import type { ApexOptions } from "apexcharts";
import { ApexChart, useApexTheme } from "@/components/ui/apex-chart";
import type { HourlyDensity } from "@/api";

function formatHour(h: number): string {
  return `${String(h).padStart(2, "0")}:00`;
}

type Props = {
  data: HourlyDensity[];
};

export function DailyDistributionChart({ data }: Props) {
  const theme = useApexTheme();
  const { categories, projectNames, series } = useMemo(() => {
    const projects = new Set<string>();
    for (const d of data) {
      for (const p of Object.keys(d.by_project)) projects.add(p);
    }
    const names = Array.from(projects);
    const hours = data.map((d) => formatHour(d.hour));

    const perProject = names.map((name) => ({
      name,
      data: data.map((d) => d.by_project[name] ?? 0),
    }));

    return {
      categories: hours,
      projectNames: names,
      series: [...perProject, { name: "Total", data: data.map((d) => d.density) }],
    };
  }, [data]);

  const currentHour = new Date().getHours();
  const nowLabel = `${String(currentHour).padStart(2, "0")}:${String(new Date().getMinutes()).padStart(2, "0")}`;

  const options = useMemo<ApexOptions>(() => {
    const colors = [...projectNames.map((_, i) => theme.palette[(i + 1) % theme.palette.length]), theme.primary];
    return {
      colors,
      chart: { stacked: false },
      stroke: {
        curve: "smooth",
        width: [...projectNames.map(() => 1.2), 2.5],
      },
      fill: {
        type: "gradient",
        gradient: {
          shadeIntensity: 0,
          opacityFrom: 0.25,
          opacityTo: 0.02,
          stops: [5, 95],
        },
      },
      dataLabels: { enabled: false },
      xaxis: {
        categories,
        tickAmount: 8,
        labels: { style: { fontSize: "11px", fontFamily: theme.monoFont } },
        axisBorder: { show: true, color: theme.grid },
        axisTicks: { show: false },
      },
      yaxis: {
        labels: { style: { fontSize: "11px" } },
      },
      legend: { show: projectNames.length > 0, position: "bottom", horizontalAlign: "left" },
      annotations: {
        xaxis: [
          {
            x: formatHour(currentHour),
            borderColor: theme.ember,
            strokeDashArray: 4,
            label: {
              text: nowLabel,
              borderColor: theme.ember,
              style: { color: theme.card, background: theme.ember, fontFamily: theme.monoFont, fontSize: "11px" },
            },
          },
        ],
      },
      tooltip: { shared: true, intersect: false },
    };
  }, [categories, projectNames, theme, currentHour, nowLabel]);

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

      <ApexChart
        type="area"
        series={series}
        options={options}
        height={256}
        ariaLabel="Daily coding distribution by hour"
      />
    </div>
  );
}
