import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchToday, type CodeTimeReport } from "@/api";
import { StatCard } from "@/components/stat-card";
import { ProjectTable } from "@/components/project-table";
import { DailyDistributionChart } from "@/components/daily-distribution-chart";
import { ProjectTrendChart } from "@/components/project-trend-chart";
import {
  generateSampleHourlyDensity,
  generateSampleProjectTrend,
} from "@/lib/sample-data";

function formatDuration(seconds: number): string {
  if (!seconds) return "-";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${seconds}s`;
}

function formatTokens(tokens: number): string {
  if (!tokens) return "-";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k`;
  return String(tokens);
}

function formatCost(cost: number | null): string {
  if (cost == null) return "-";
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

function WindowSelector({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const options = [
    { value: "today", label: "Today" },
    { value: "72h", label: "72h" },
    { value: "7d", label: "7 days" },
    { value: "30d", label: "30 days" },
  ];
  return (
    <div className="flex gap-1 rounded-lg border border-border bg-secondary/50 p-1">
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={`rounded-md px-3 py-1 text-caption font-display transition-colors ${
            value === opt.value
              ? "bg-primary text-primary-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

export function OverviewRoute() {
  const [window, setWindow] = React.useState("today");
  const { data, isLoading, error, refetch } = useQuery<CodeTimeReport>({
    queryKey: ["code-time", window],
    queryFn: () => fetchToday({ window }),
    refetchInterval: 60_000,
  });

  const totals = data?.totals;

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="font-display text-heading font-semibold tracking-tight">
            Code Time
          </h1>
          <p className="mt-1 text-caption text-muted-foreground">
            {data?.generated_at
              ? `Updated ${new Date(data.generated_at).toLocaleTimeString()}`
              : "Loading..."}
          </p>
        </div>
        <WindowSelector value={window} onChange={setWindow} />
      </div>

      {isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="h-24 animate-shimmer rounded-xl border border-border bg-card"
            />
          ))}
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-body-sm text-destructive">
          <p className="font-medium">Failed to load data</p>
          <p className="mt-1 text-caption">{String(error)}</p>
          <button
            onClick={() => refetch()}
            className="mt-2 rounded-md bg-destructive/10 px-3 py-1 text-caption hover:bg-destructive/20"
          >
            Retry
          </button>
        </div>
      )}

      {totals && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <StatCard
              label="Coding Time"
              value={formatDuration(totals.execution_seconds)}
              detail={`Wait: ${formatDuration(totals.wait_seconds)}`}
              className="animate-rise-in"
            />
            <StatCard
              label="Sessions"
              value={String(totals.session_count)}
              detail={`${totals.project_count} projects`}
              className="animate-rise-in [animation-delay:60ms]"
            />
            <StatCard
              label="Cost"
              value={formatCost(totals.cost_usd)}
              className="animate-rise-in [animation-delay:120ms]"
            />
            <StatCard
              label="Tokens"
              value={formatTokens(totals.tokens.total_tokens)}
              detail={`${formatTokens(totals.tokens.output_tokens)} output`}
              className="animate-rise-in [animation-delay:180ms]"
            />
            <StatCard
              label="Turns"
              value={String(totals.turns)}
              className="animate-rise-in [animation-delay:240ms]"
            />
            <StatCard
              label="Tool Calls"
              value={String(totals.tool_calls)}
              className="animate-rise-in [animation-delay:300ms]"
            />
          </div>

          <div className="mt-8 grid gap-6 lg:grid-cols-2">
            <DailyDistributionChart
              data={
                data.hourly_density ??
                generateSampleHourlyDensity(data.projects)
              }
            />
            <ProjectTrendChart
              data={
                data.project_trend ??
                generateSampleProjectTrend(data.projects)
              }
            />
          </div>

          <div className="mt-8 rounded-xl border border-border bg-card shadow-sm">
            <div className="border-b border-border px-5 py-3">
              <h2 className="font-display text-body-sm font-medium tracking-wide">
                Projects
              </h2>
            </div>
            <ProjectTable projects={data?.projects ?? []} />
          </div>
        </>
      )}
    </div>
  );
}
