import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchOverview } from "@/api";
import { MetricSkeleton } from "@/components/ui/skeleton";
import { RouteHeader } from "@/components/route-header";
import { MetricCard } from "@/components/metric-card";
import { RefreshButton } from "@/components/refresh-button";
import { StateBlock } from "@/components/state-block";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export function OverviewRoute() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: fetchOverview });

  if (overview.isPending) {
    return (
      <div className="mx-auto grid max-w-[96rem] gap-5">
        <RouteHeader eyebrow="Operational scan" title="Loading dashboard data" />
        <section className="grid grid-cols-4 gap-4 max-lg:grid-cols-1">
          {Array.from({ length: 4 }, (_, i) => <MetricSkeleton key={i} />)}
        </section>
      </div>
    );
  }

  if (overview.isError) return <StateBlock title="Dashboard unavailable" detail={overview.error.message} />;

  const data = overview.data;
  const vendorEntries = Object.entries(data.projects.vendors);
  const runtime = data.sessions.runtime;
  const usage = data.sessions.usage;
  const issueCount = data.sessions.errors.length + data.sessions.warnings.length;

  return (
    <div className="mx-auto grid w-full min-w-0 max-w-[96rem] gap-5 overflow-hidden">
      <RouteHeader
        eyebrow="Usage activity"
        title="Recent project and session activity from the last 30 days."
        action={<RefreshButton queries={["overview"]} />}
      />
      <section className="grid min-w-0 grid-cols-4 gap-4 max-xl:grid-cols-2 max-md:grid-cols-1">
        <MetricCard
          label="Projects"
          value={data.projects.count}
          detail={`${vendorEntries.length} active vendor source(s)`}
          sparklineEntries={vendorEntries.map(([label, value]) => ({ label: label.slice(0, 3), value }))}
        />
        <MetricCard
          label="Sessions"
          value={data.sessions.count}
          detail={`${runtime.turns.toLocaleString()} turns in ${data.sessions.window_days} days`}
        />
        <MetricCard
          label="Runtime"
          value={Math.round(runtime.execution_seconds / 60)}
          detail={`${formatDuration(runtime.wait_seconds)} waiting, ${runtime.tool_calls.toLocaleString()} tool calls`}
          ratio={runtime.tool_calls ? runtime.failed_tool_calls / runtime.tool_calls : 0}
        />
        <MetricCard
          label="Tokens"
          value={compactNumber(usage.total_tokens)}
          detail={`${formatCost(usage.cost_usd)} known cost${usage.missing_cost_count ? `, ${usage.missing_cost_count} partial` : ""}`}
        />
      </section>
      <section className="grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(22rem,0.8fr)] gap-4 max-xl:grid-cols-1">
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="font-display text-xl tracking-tight">Recent Activity by Project</CardTitle>
            <CardDescription className="break-words">Session volume, vendor mix, runtime, tokens, and known cost.</CardDescription>
          </CardHeader>
          <CardContent>
            {data.sessions.top_projects.length ? (
              <div className="grid gap-3">
                {data.sessions.top_projects.map((project) => (
                  <div
                    key={project.project}
                    className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3 rounded-lg border border-border-subtle p-3 max-sm:grid-cols-1"
                  >
                    <div className="min-w-0">
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <p className="m-0 truncate font-display text-base font-extrabold">{project.project}</p>
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
                      <span>{compactNumber(project.total_tokens)} tokens</span>
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
            <CardTitle className="font-display text-xl tracking-tight">Vendor Coverage</CardTitle>
            <CardDescription>Discovered project metadata grouped by agent vendor.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            {vendorEntries.length ? (
              vendorEntries.map(([vendor, count]) => (
                <Badge key={vendor}>
                  {vendor} <strong>{count}</strong>
                </Badge>
              ))
            ) : (
              <p className="text-muted-foreground">No vendor metadata found.</p>
            )}
          </CardContent>
        </Card>
      </section>

      <Card className="min-w-0">
        <CardHeader>
          <CardTitle className="font-display text-xl tracking-tight">Top Known-Cost Sessions</CardTitle>
          <CardDescription>Only sessions with a reported cost are ranked here.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="overflow-auto rounded-lg border border-border-subtle">
            <Table>
              <TableHead className="bg-table-head font-display text-caption uppercase">
                <TableRow>
                  <TableHeader>Session</TableHeader>
                  <TableHeader>Project</TableHeader>
                  <TableHeader>Vendor</TableHeader>
                  <TableHeader className="text-right">Runtime</TableHeader>
                  <TableHeader className="text-right">Tokens</TableHeader>
                  <TableHeader className="text-right">Cost</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.sessions.top_sessions.map((session) => (
                  <TableRow key={session.id ?? `${session.project}-${session.title}`}>
                    <TableCell className="min-w-[18rem] max-w-[32rem]">
                      <p className="m-0 line-clamp-2 font-medium">{session.title || session.id || "Untitled session"}</p>
                      <p className="m-0 mt-1 text-caption text-muted-foreground">{formatWhen(session.started_at)}</p>
                    </TableCell>
                    <TableCell>{session.project || "unknown"}</TableCell>
                    <TableCell>{session.vendor}</TableCell>
                    <TableCell className="text-right">{formatDuration(session.execution_seconds)}</TableCell>
                    <TableCell className="text-right">{compactNumber(session.total_tokens)}</TableCell>
                    <TableCell className="text-right">{formatCost(session.cost_usd)}</TableCell>
                  </TableRow>
                ))}
                {!data.sessions.top_sessions.length ? (
                  <TableRow>
                    <TableCell colSpan={6}>No sessions with known cost in this window.</TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      {issueCount ? (
        <Card className="min-w-0 border-warning/40">
          <CardHeader>
            <CardTitle className="font-display text-xl tracking-tight">Warnings and Errors</CardTitle>
            <CardDescription>Issues reported by the session data bulk read.</CardDescription>
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
      ) : null}
    </div>
  );
}

function IssueRow({ label, message, detail }: { label: string; message: string; detail?: string }) {
  return (
    <div className="grid gap-1 rounded-lg border border-border-subtle p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">{label}</Badge>
        {detail ? <span className="font-mono text-caption text-muted-foreground">{detail}</span> : null}
      </div>
      <p className="m-0 break-words text-body-sm">{message}</p>
    </div>
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
