import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { Loader2, Send, Sparkles, Square, X } from "lucide-react";
import { fetchOverview, type AgentTurnResult, type OverviewPayload } from "@/api";
import { HeaderLabel, RightCell } from "@/components/table-cells";
import { ProjectLink } from "@/components/project-link";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { useAgentTurn } from "@/hooks/use-agent-turn";
import { formatElapsed } from "@/hooks/use-elapsed-timer";
import { DataTable } from "@/components/data-table";
import { MetricSkeleton } from "@/components/ui/skeleton";
import { RouteHeader } from "@/components/route-header";
import { MetricCard } from "@/components/metric-card";
import { StaggerGroup } from "@/components/stagger-group";
import { StateBlock } from "@/components/state-block";
import { MiniBarChart } from "@/components/charts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function OverviewRoute() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: fetchOverview });

  if (overview.isPending) {
    return (
      <div className="route-container">
        <RouteHeader eyebrow="Operational scan" title="Loading dashboard data" />
        <section className="stat-grid">
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
  const issueAgentContext = issueCount ? overviewIssueContext(data) : "";
  const issueAgentPrompt = issueCount ? overviewIssuePrompt(issueAgentContext) : "";

  return (
    <div className="route-container w-full min-w-0 overflow-hidden">
      <RouteHeader
        eyebrow="Usage activity"
        title="Recent project and session activity from today."
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
                          sinceDays={data.sessions.window_days}
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

      <Card className="min-w-0">
        <CardHeader>
          <CardTitle className="title-card">Top Token-Cost Sessions</CardTitle>
          <CardDescription>Sessions ranked by token usage.</CardDescription>
        </CardHeader>
        <CardContent>
          <TopSessionsTable sessions={data.sessions.top_sessions} windowDays={data.sessions.window_days} />
        </CardContent>
      </Card>

      {issueCount ? (
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
      ) : null}
      {issueCount ? (
        <OverviewIssueAgent prompt={issueAgentPrompt} />
      ) : null}
    </div>
  );
}

function OverviewIssueAgent({ prompt }: { prompt: string }) {
  const agent = useAgentTurn("overview-issue-analysis");
  const [followUp, setFollowUp] = React.useState("");
  const running = agent.status === "pending" || agent.status === "running";
  const canFollowUp = !running && followUp.trim().length > 0;
  return (
    <Card className="min-w-0">
      <CardHeader className="items-start gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0">
          <CardTitle className="title-card">Agent Fix Analysis</CardTitle>
          <CardDescription>
            Codex analyzes the collected dashboard issues and can continue while this page is open.
          </CardDescription>
        </div>
        <Button
          type="button"
          size="sm"
          disabled={running || !prompt.trim()}
          onClick={() => agent.run(prompt, { newSession: true })}
        >
          {running ? <Square size={13} className="fill-current" /> : <Sparkles size={15} />}
          {agent.result ? "Start new analysis" : "Run agent"}
        </Button>
      </CardHeader>
      <CardContent className="grid gap-3">
        {running ? (
          <div className="panel flex items-center gap-3">
            <Loader2 size={18} className="shrink-0 animate-spin text-primary" />
            <div className="min-w-0 flex-1">
              <p className="m-0 title-state">Running issue analysis</p>
              <p className="m-0 text-body-sm text-muted-foreground">
                Codex is reviewing the overview warnings and errors.
              </p>
              {agent.progress ? (
                <p className="m-0 mt-1 text-caption text-muted-foreground">{agent.progress}</p>
              ) : null}
            </div>
            {agent.elapsedMs > 0 ? (
              <span className="shrink-0 mono text-caption text-muted-foreground">
                {formatElapsed(agent.elapsedMs)}
              </span>
            ) : null}
            <Button size="icon-sm" variant="ghost" onClick={agent.cancel} aria-label="Cancel overview issue analysis">
              <X size={15} />
            </Button>
          </div>
        ) : null}
        {agent.status === "error" ? (
          <div role="alert" className="alert alert-destructive text-body-sm text-destructive">
            {agent.error}
          </div>
        ) : null}
        <OverviewIssueAgentResponse result={agent.result} />
        {agent.result ? (
          <form
            className="grid gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (!canFollowUp) return;
              agent.run(overviewIssueFollowUpPrompt(followUp));
              setFollowUp("");
            }}
          >
            <label className="eyebrow-soft text-muted-foreground" htmlFor="overview-issue-agent-follow-up">
              Follow up on this issue analysis
            </label>
            <textarea
              id="overview-issue-agent-follow-up"
              name="overview_issue_agent_follow_up"
              value={followUp}
              onChange={(event) => setFollowUp(event.target.value)}
              placeholder="Ask Codex to refine, inspect a likely cause, or turn the analysis into a fix plan."
              disabled={running}
              className="min-h-24 resize-y rounded-md border border-input bg-background px-3 py-2 text-body-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
            />
            <div className="flex justify-end">
              <Button type="submit" size="sm" disabled={!canFollowUp}>
                {running ? <Square size={13} className="fill-current" /> : <Send size={15} />}
                Send follow-up
              </Button>
            </div>
          </form>
        ) : null}
      </CardContent>
    </Card>
  );
}

function OverviewIssueAgentResponse({ result }: { result: AgentTurnResult | null }) {
  if (!result) return null;
  return (
    <section className="panel grid gap-3" aria-label="Overview issue agent response">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">Codex</Badge>
        <span className="mono text-caption text-muted-foreground">
          session {shortAgentId(result.agent_session_id)}
        </span>
        {result.app_server_turn_id ? (
          <span className="mono text-caption text-muted-foreground">
            turn {shortAgentId(result.app_server_turn_id)}
          </span>
        ) : null}
      </div>
      <div className="whitespace-pre-wrap break-words text-body-sm leading-relaxed">
        {result.response_text}
      </div>
    </section>
  );
}

function overviewIssuePrompt(taskContext: string) {
  return [
    "# Goal",
    "Analyze these dashboard warnings and errors. Identify likely causes, missing evidence, and concrete fix actions.",
    "",
    "# Context",
    taskContext,
    "",
    "# Response",
    "Return concise plain text. Separate direct observations from likely causes and next actions.",
  ].join("\n");
}

function overviewIssueFollowUpPrompt(value: string) {
  return [
    "# Follow-up",
    value,
    "",
    "# Response",
    "Continue the overview issue analysis. Keep the answer specific to the dashboard warnings and errors already in this conversation.",
  ].join("\n");
}

function shortAgentId(value: string) {
  return value.length > 10 ? value.slice(0, 8) : value;
}

function overviewIssueContext(data: OverviewPayload) {
  const lines = [
    "Dashboard: overview",
    `Window days: ${data.sessions.window_days}`,
    `Session count: ${data.sessions.count}`,
    `Error count: ${data.sessions.errors.length}`,
    `Warning count: ${data.sessions.warnings.length}`,
    "",
    "Errors:",
  ];
  if (data.sessions.errors.length) {
    data.sessions.errors.forEach((error, index) => {
      lines.push(`${index + 1}. ${formatIssue(error)}`);
    });
  } else {
    lines.push("- none");
  }
  lines.push("", "Warnings:");
  if (data.sessions.warnings.length) {
    data.sessions.warnings.forEach((warning, index) => {
      const session = warning.session_id ? ` session_id=${warning.session_id}` : "";
      lines.push(`${index + 1}. project=${warning.project}${session} message=${warning.message}`);
    });
  } else {
    lines.push("- none");
  }
  return lines.join("\n");
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

function TopSessionsTable({ sessions, windowDays }: { sessions: TopSession[]; windowDays: number }) {
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
            <ProjectLink name={project} sinceDays={windowDays} />
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
    [windowDays],
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
