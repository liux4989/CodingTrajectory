import { useMemo, useState } from "react";
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

function intensityColor(seconds: number, maxSeconds: number): string {
  if (!seconds || !maxSeconds) return "var(--muted-color)";
  const ratio = Math.min(seconds / maxSeconds, 1);
  const alpha = 0.15 + ratio * 0.85;
  return `color-mix(in srgb, var(--primary) ${Math.round(alpha * 100)}%, var(--muted-color))`;
}

export function ProjectTrendChart({ data }: Props) {
  const [hovered, setHovered] = useState<{
    project: string;
    date: string;
    seconds: number;
    x: number;
    y: number;
  } | null>(null);

  const { allDates, maxSeconds } = useMemo(() => {
    const dates = new Set<string>();
    let max = 0;
    for (const p of data) {
      for (const d of p.days) {
        dates.add(d.date);
        if (d.seconds > max) max = d.seconds;
      }
    }
    return { allDates: Array.from(dates).sort(), maxSeconds: max };
  }, [data]);

  const dateTicks = useMemo(() => {
    if (allDates.length <= 8) return allDates;
    const step = Math.ceil(allDates.length / 8);
    const ticks: string[] = [];
    for (let i = 0; i < allDates.length; i += step) ticks.push(allDates[i]);
    return ticks;
  }, [allDates]);

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

      <div className="relative overflow-x-auto">
        <div className="min-w-[600px]">
          {data.map((project) => {
            const dayMap = new Map(
              project.days.map((d) => [d.date, d.seconds]),
            );
            return (
              <div key={project.project_name} className="flex items-center gap-2 mb-1">
                <div className="w-32 shrink-0 truncate text-right text-caption font-mono text-foreground">
                  {project.project_name}
                </div>
                <div className="flex flex-1 gap-px">
                  {allDates.map((date) => {
                    const seconds = dayMap.get(date) ?? 0;
                    return (
                      <div
                        key={date}
                        className="h-5 flex-1 min-w-[3px] rounded-[2px] cursor-pointer transition-opacity hover:opacity-80"
                        style={{
                          backgroundColor: intensityColor(seconds, maxSeconds),
                        }}
                        onMouseEnter={(e) => {
                          const rect = e.currentTarget.getBoundingClientRect();
                          setHovered({
                            project: project.project_name,
                            date,
                            seconds,
                            x: rect.left + rect.width / 2,
                            y: rect.top,
                          });
                        }}
                        onMouseLeave={() => setHovered(null)}
                      />
                    );
                  })}
                </div>
              </div>
            );
          })}

          <div className="flex items-center gap-2 mt-2">
            <div className="w-32 shrink-0" />
            <div className="flex flex-1 justify-between">
              {dateTicks.map((date) => (
                <span
                  key={date}
                  className="text-[10px] text-muted-foreground font-mono"
                >
                  {formatDate(date)}
                </span>
              ))}
            </div>
          </div>
        </div>

        {hovered && (
          <div
            className="fixed z-50 pointer-events-none rounded-lg border border-border bg-popover p-3 shadow-lg text-caption font-mono"
            style={{
              left: hovered.x,
              top: hovered.y - 8,
              transform: "translate(-50%, -100%)",
            }}
          >
            <p className="font-semibold">{hovered.project}</p>
            <p className="text-muted-foreground">
              Duration: {formatDuration(hovered.seconds)}
            </p>
            <p className="text-muted-foreground">
              Date: {formatDate(hovered.date)}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
