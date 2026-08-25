import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import type { ApexOptions } from "apexcharts";
import { fetchOverview, type OverviewPayload } from "@/api";
import { useDateRange } from "@/hooks/use-date-range";
import { formatCompactNumber, formatCostUsd, formatDuration } from "@/lib/format";
import { LoadingShell } from "@/components/loading-shell";
import { RouteHeader } from "@/components/route-header";
import { MetricCard } from "@/components/metric-card";
import { StaggerGroup } from "@/components/stagger-group";
import { StateBlock } from "@/components/state-block";
import { ApexChart, escapeHtml, tooltipRow, useApexTheme } from "@/components/ui/apex-chart";
import { DonutChart } from "@/components/charts";
import { SectionTabs } from "@/components/section-tabs";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function OverviewRoute() {
  const [activeTab, setActiveTab] = React.useState("sessions");
  const { days: sinceDays } = useDateRange();
  const overview = useQuery({
    queryKey: ["overview", sinceDays],
    queryFn: () => fetchOverview({ sinceDays }),
    placeholderData: (previous) => previous,
  });

  if (overview.isPending) {
    return <LoadingShell eyebrow="Usage activity" title="Loading datahub data" variant="metrics" />;
  }

  if (overview.isError) return <StateBlock title="Datahub unavailable" detail={overview.error.message} onRetry={() => overview.refetch()} />;

  const data = overview.data;
  const vendorEntries = Object.entries(data.projects.vendors);
  const runtime = data.sessions.runtime;
  const usage = data.sessions.usage;
  const issueCount = data.sessions.errors.length + data.sessions.warnings.length;

  return (
    <div className="route-container w-full min-w-0 overflow-hidden">
      <RouteHeader
        eyebrow="Usage activity"
        title={`Recent project and session activity from the last ${sinceDays} day${sinceDays === 1 ? "" : "s"}.`}
      />
      <section className="stat-grid min-w-0">
        <StaggerGroup className="contents">
        <MetricCard
          label="Projects"
          value={data.projects.count}
          detail={`${vendorEntries.length} active vendor source(s)`}
          sparklineEntries={vendorEntries.map(([label, value]) => ({ label: label.slice(0, 3), value }))}
        />
        <MetricCard
          label="Sessions"
          value={data.sessions.count}
          detail={`${runtime.turns.toLocaleString()} turns in ${data.sessions.window_days} day${data.sessions.window_days === 1 ? "" : "s"}`}
        />
        <MetricCard
          label="Runtime"
          value={Math.round(runtime.execution_seconds / 60)}
          detail={`${formatDuration(runtime.wait_seconds)} waiting, ${runtime.tool_calls.toLocaleString()} tool calls`}
          ratio={runtime.tool_calls ? runtime.failed_tool_calls / runtime.tool_calls : 0}
        />
        <MetricCard
          label="Tokens"
          value={formatCompactNumber(usage.processed_tokens)}
          detail={`${formatCostUsd(usage.cost_usd)} known cost${usage.missing_cost_count ? `, ${usage.missing_cost_count} partial` : ""}`}
        />
        </StaggerGroup>
      </section>
      <section className="grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(22rem,0.8fr)] gap-4 max-xl:grid-cols-1">
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Recent Activity by Project</CardTitle>
            <CardDescription className="break-words">Sessions per project, stacked by vendor. Select a project bar to drill into its usage.</CardDescription>
          </CardHeader>
          <CardContent>
            {data.sessions.top_projects.length ? (
              <>
                <ProjectActivityChart projects={data.sessions.top_projects} />
                <ul className="sr-only">
                  {data.sessions.top_projects.map((project) => (
                    <li key={project.project}>
                      {project.project}: {project.count} sessions, {formatDuration(project.execution_seconds)} runtime, {formatCompactNumber(project.processed_tokens)} tokens
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="text-muted-foreground">No recent session activity found.</p>
            )}
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Vendor Coverage</CardTitle>
            <CardDescription>Discovered project metadata grouped by agent vendor.</CardDescription>
          </CardHeader>
          <CardContent>
            {vendorEntries.length ? (
              <DonutChart
                data={vendorEntries.map(([label, value]) => ({ label, value }))}
                ariaLabel="Vendor coverage"
                centerLabel={String(data.projects.count)}
                centerSubLabel="Projects"
              />
            ) : (
              <p className="text-muted-foreground">No vendor metadata found.</p>
            )}
          </CardContent>
        </Card>
      </section>

      {issueCount ? (
        <SectionTabs
          activeTab={activeTab}
          onTabChange={setActiveTab}
          tabs={[
            {
              id: "sessions",
              label: "Top Sessions",
              content: (
                <Card className="min-w-0">
                  <CardHeader>
                    <CardTitle className="title-card">Top Token-Cost Sessions</CardTitle>
                    <CardDescription>Sessions ranked by token usage. Select a bar to open the session.</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <TopSessionsChart sessions={data.sessions.top_sessions} />
                  </CardContent>
                </Card>
              ),
            },
            {
              id: "issues",
              label: "Warnings & Errors",
              badge: issueCount,
              content: (
                <Card className="min-w-0 border-warning/40">
                  <CardHeader>
                    <CardTitle className="title-card">Warnings and Errors</CardTitle>
                    <CardDescription>Issues reported while collecting session metrics.</CardDescription>
                  </CardHeader>
                  <CardContent className="grid gap-3">
                    {data.sessions.errors.map((error, index) => (
                      <IssueRow key={`error-${index}`} label="Error" message={formatIssue(error)} />
                    ))}
                    {data.sessions.warnings.map((warning, index) => (
                      <IssueRow
                        key={`warning-${index}`}
                        label={warning.project}
                        message={warning.message}
                        detail={warning.session_id || undefined}
                      />
                    ))}
                  </CardContent>
                </Card>
              ),
            },
          ]}
        />
      ) : (
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Top Token-Cost Sessions</CardTitle>
            <CardDescription>Sessions ranked by token usage. Select a bar to open the session.</CardDescription>
          </CardHeader>
          <CardContent>
            <TopSessionsChart sessions={data.sessions.top_sessions} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function IssueRow({ label, message, detail }: { label: string; message: string; detail?: string }) {
  return (
    <div className="panel grid gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">{label}</Badge>
        {detail ? <span className="mono text-caption text-muted-foreground">{detail}</span> : null}
      </div>
      <p className="m-0 break-words text-body-sm">{message}</p>
    </div>
  );
}

type TopProject = OverviewPayload["sessions"]["top_projects"][number];

function truncateLabel(value: string, max = 22) {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

/**
 * Sessions per project, stacked by vendor. Tooltip carries runtime, token,
 * and cost context; clicking a bar drills into the project's model usage.
 */
function ProjectActivityChart({ projects }: { projects: TopProject[] }) {
  const theme = useApexTheme();
  const navigate = useNavigate();
  const vendors = React.useMemo(() => {
    const totals = new Map<string, number>();
    for (const project of projects) {
      for (const [vendor, count] of Object.entries(project.vendors)) {
        totals.set(vendor, (totals.get(vendor) ?? 0) + count);
      }
    }
    return [...totals.entries()].sort((left, right) => right[1] - left[1]).map(([vendor]) => vendor);
  }, [projects]);

  const options = React.useMemo<ApexOptions>(
    () => ({
      chart: {
        stacked: true,
        events: {
          dataPointSelection: (_event, _chartContext, config) => {
            const project = config ? projects[config.dataPointIndex] : undefined;
            if (project) {
              void navigate({
                to: "/compare",
                search: { projectName: project.project, modelKey: undefined, view: undefined, grain: undefined, unit: undefined },
              });
            }
          },
        },
      },
      plotOptions: { bar: { horizontal: true, barHeight: "62%", borderRadius: 3 } },
      dataLabels: { enabled: false },
      xaxis: {
        categories: projects.map((project) => truncateLabel(project.project)),
        labels: { formatter: (value) => formatCompactNumber(Number(value)) },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: { labels: { style: { fontSize: "11px" }, maxWidth: 180 } },
      legend: { show: vendors.length > 1, position: "bottom", horizontalAlign: "left" },
      tooltip: {
        custom: ({ dataPointIndex }) => {
          const project = projects[dataPointIndex];
          if (!project) return "";
          const rows = [
            tooltipRow("Sessions", project.count.toLocaleString(), theme.axis),
            tooltipRow("Runtime", formatDuration(project.execution_seconds), theme.axis),
            tooltipRow("Tokens", formatCompactNumber(project.processed_tokens), theme.axis),
            tooltipRow("Cost", project.known_cost_count ? formatCostUsd(project.cost_usd) : "Unavailable", theme.axis),
          ].join("");
          return `<div style="padding:10px 12px;min-width:200px"><div style="font-weight:700;margin-bottom:6px">${escapeHtml(project.project)}</div>${rows}</div>`;
        },
      },
    }),
    [projects, vendors.length, theme, navigate],
  );

  return (
    <ApexChart
      type="bar"
      series={vendors.map((vendor) => ({
        name: vendor,
        data: projects.map((project) => project.vendors[vendor] ?? 0),
      }))}
      options={options}
      height={Math.max(200, projects.length * 52)}
      ariaLabel="Sessions per project stacked by vendor"
    />
  );
}

type TopSession = OverviewPayload["sessions"]["top_sessions"][number];

/**
 * Top sessions ranked by processed tokens. Clicking a bar opens the session
 * detail; the tooltip carries project, vendor, runtime, and start time.
 */
function TopSessionsChart({ sessions }: { sessions: TopSession[] }) {
  const theme = useApexTheme();
  const navigate = useNavigate();
  const ranked = React.useMemo(
    () => [...sessions].sort((left, right) => right.processed_tokens - left.processed_tokens).slice(0, 10),
    [sessions],
  );

  if (!ranked.length) {
    return (
      <div className="grid gap-1 py-6 text-center">
        <p className="m-0 text-muted-foreground">No sessions with token usage in this window.</p>
        <p className="m-0 text-caption text-muted-foreground">No token usage was recorded in the last 7 days.</p>
      </div>
    );
  }

  const options: ApexOptions = {
    chart: {
      events: {
        dataPointSelection: (_event, _chartContext, config) => {
          const session = config ? ranked[config.dataPointIndex] : undefined;
          if (session?.id) {
            void navigate({ to: "/sessions/$sessionId", params: { sessionId: session.id } });
          }
        },
      },
    },
    plotOptions: { bar: { horizontal: true, barHeight: "62%", borderRadius: 4, distributed: true } },
    dataLabels: { enabled: false },
    xaxis: {
      categories: ranked.map((session) => truncateLabel(session.title || session.id || "Untitled session", 26)),
      labels: { formatter: (value) => formatCompactNumber(Number(value)) },
      axisBorder: { show: false },
      axisTicks: { show: false },
    },
    yaxis: { labels: { style: { fontSize: "11px" }, maxWidth: 220 } },
    legend: { show: false },
    tooltip: {
      custom: ({ dataPointIndex }) => {
        const session = ranked[dataPointIndex];
        if (!session) return "";
        const rows = [
          tooltipRow("Project", escapeHtml(session.project ?? "Unknown"), theme.axis),
          tooltipRow("Vendor", escapeHtml(session.vendor), theme.axis),
          tooltipRow("Runtime", formatDuration(session.execution_seconds), theme.axis),
          tooltipRow("Tokens", formatCompactNumber(session.processed_tokens), theme.axis),
          tooltipRow("Started", escapeHtml(formatWhen(session.started_at)), theme.axis),
        ].join("");
        return `<div style="padding:10px 12px;min-width:220px"><div style="font-weight:700;margin-bottom:6px">${escapeHtml(session.title || session.id || "Untitled session")}</div>${rows}</div>`;
      },
    },
  };

  return (
    <>
      <ApexChart
        type="bar"
        series={[{ name: "Tokens", data: ranked.map((session) => session.processed_tokens) }]}
        options={options}
        height={Math.max(220, ranked.length * 44)}
        ariaLabel="Top sessions ranked by processed tokens"
      />
      <ul className="sr-only">
        {ranked.map((session, index) => (
          <li key={session.id ?? index}>
            {session.title || session.id || "Untitled session"}: {formatCompactNumber(session.processed_tokens)} tokens, {formatDuration(session.execution_seconds)} runtime
          </li>
        ))}
      </ul>
    </>
  );
}

function formatWhen(value?: string | null) {
  if (!value) return "Start time unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatIssue(value: unknown) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && "message" in value) {
    return String((value as { message?: unknown }).message);
  }
  return JSON.stringify(value);
}
