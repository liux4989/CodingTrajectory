import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { fetchOverview, type OverviewPayload } from "@/api";
import { HeaderLabel, RightCell } from "@/components/table-cells";
import { ProjectLink } from "@/components/project-link";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { useDateRange } from "@/hooks/use-date-range";
import { DataTable } from "@/components/data-table";
import { LoadingShell } from "@/components/loading-shell";
import { RouteHeader } from "@/components/route-header";
import { MetricCard } from "@/components/metric-card";
import { StaggerGroup } from "@/components/stagger-group";
import { StateBlock } from "@/components/state-block";
import { MiniBarChart } from "@/components/charts";
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
    return <LoadingShell eyebrow="Usage activity" title="Loading dashboard data" variant="metrics" />;
  }

  if (overview.isError) return <StateBlock title="Dashboard unavailable" detail={overview.error.message} onRetry={() => overview.refetch()} />;

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
          value={compactNumber(usage.processed_tokens)}
          detail={`${formatCost(usage.cost_usd)} known cost${usage.missing_cost_count ? `, ${usage.missing_cost_count} partial` : ""}`}
        />
        </StaggerGroup>
      </section>
      <section className="grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(22rem,0.8fr)] gap-4 max-xl:grid-cols-1">
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Recent Activity by Project</CardTitle>
            <CardDescription className="break-words">Session volume, vendor mix, runtime, tokens, and known cost.</CardDescription>
          </CardHeader>
          <CardContent>
            {data.sessions.top_projects.length ? (
              <div className="grid gap-3">
                {data.sessions.top_projects.map((project) => (
                  <div
                    key={project.project}
                    className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3 panel max-sm:grid-cols-1"
                  >
                    <div className="min-w-0">
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <ProjectLink
                          name={project.project}
                          className="truncate font-display text-base font-extrabold"
                        />
                        <Badge>{project.count} sessions</Badge>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        {Object.entries(project.vendors).map(([vendor, count]) => (
                          <Badge key={vendor} variant="secondary">
                            {vendor} <strong>{count}</strong>
                          </Badge>
                        ))}
                      </div>
                    </div>
                    <div className="grid min-w-[12rem] gap-1 text-right text-body-sm text-muted-foreground max-sm:text-left">
                      <span>{formatDuration(project.execution_seconds)} runtime</span>
                      <span>{compactNumber(project.processed_tokens)} tokens</span>
                      <span>{project.known_cost_count ? formatCost(project.cost_usd) : "Cost unavailable"}</span>
                    </div>
                  </div>
                ))}
              </div>
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
              <MiniBarChart
                layout="horizontal"
                data={vendorEntries.map(([label, value]) => ({ label, value }))}
                ariaLabel="Vendor coverage"
                className="h-48"
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
                    <CardDescription>Sessions ranked by token usage.</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <TopSessionsTable sessions={data.sessions.top_sessions} />
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
            <CardDescription>Sessions ranked by token usage.</CardDescription>
          </CardHeader>
          <CardContent>
            <TopSessionsTable sessions={data.sessions.top_sessions} />
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

type TopSession = OverviewPayload["sessions"]["top_sessions"][number];

function TopSessionsTable({ sessions }: { sessions: TopSession[] }) {
  const columns = React.useMemo<ColumnDef<TopSession>[]>(
    () => [
      {
        id: "session",
        header: () => <HeaderLabel>Session</HeaderLabel>,
        cell: ({ row }) => {
          const session = row.original;
          return (
            <div className="flex max-w-[26rem] items-baseline gap-2">
              <span className="min-w-0 flex-1 truncate font-medium">
                {session.id ? (
                  <SessionLink sessionId={session.id}>
                    {session.title || shortSessionId(session.id)}
                  </SessionLink>
                ) : (
                  session.title || "Untitled session"
                )}
              </span>
              <span className="shrink-0 whitespace-nowrap text-caption text-muted-foreground">
                {formatWhen(session.started_at)}
              </span>
            </div>
          );
        },
      },
      {
        id: "project",
        accessorFn: (row) => row.project ?? "unknown",
        header: () => <HeaderLabel>Project</HeaderLabel>,
        cell: ({ row, getValue }) => {
          const project = row.original.project;
          return project ? (
            <ProjectLink name={project} />
          ) : (
            getValue<string>()
          );
        },
      },
      {
        accessorKey: "vendor",
        header: () => <HeaderLabel>Vendor</HeaderLabel>,
      },
      {
        id: "runtime",
        accessorFn: (row) => row.execution_seconds,
        header: () => <HeaderLabel align="right">Runtime</HeaderLabel>,
        cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
      },
      {
        id: "tokens",
        accessorFn: (row) => row.processed_tokens,
        header: () => <HeaderLabel align="right">Tokens</HeaderLabel>,
        cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
      },
    ],
    [],
  );

  const table = useReactTable({
    data: sessions,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });
  return (
    <DataTable
      table={table}
      columnCount={columns.length}
      emptyMessage="No sessions with token usage in this window."
      emptyHint="Try expanding the date range using the toggle in the header."
    />
  );
}

function compactNumber(value: number) {
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatCost(value: number) {
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(value);
}

function formatDuration(seconds: number) {
  if (!seconds) return "0m";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  return `${Math.max(1, minutes)}m`;
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
