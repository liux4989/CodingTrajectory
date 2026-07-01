import * as React from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { BarChart3 } from "lucide-react";
import { fetchModelUsage, type ModelUsagePayload, type UsageBuckets } from "@/api";
import { MetricCard } from "@/components/metric-card";
import { RefreshButton } from "@/components/refresh-button";
import { RouteHeader } from "@/components/route-header";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { StateBlock } from "@/components/state-block";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { MetricSkeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const ALL_PROJECTS = "__all_projects__";
const TIME_OPTIONS = [7, 14, 30, 90];

export function ModelUsageRoute() {
  const search = useSearch({ from: "/model-usage" });
  const navigate = useNavigate({ from: "/model-usage" });
  const sinceDays = search.sinceDays ?? 7;
  const projectName = search.projectName ?? null;
  const query = useQuery({
    queryKey: ["model-usage", sinceDays, projectName],
    queryFn: () => fetchModelUsage({ sinceDays, projectName }),
  });

  const setSinceDays = (value: string) => {
    void navigate({ search: (current) => ({ ...current, sinceDays: Number(value) }) });
  };
  const setProjectName = (value: string) => {
    void navigate({
      search: (current) => ({
        ...current,
        projectName: value === ALL_PROJECTS ? undefined : value,
      }),
    });
  };

  if (query.isPending) {
    return (
      <div className="mx-auto grid max-w-[96rem] gap-5">
        <RouteHeader eyebrow="Model economics" title="Loading model usage" />
        <section className="grid grid-cols-4 gap-4 max-lg:grid-cols-1">
          {Array.from({ length: 4 }, (_, i) => <MetricSkeleton key={i} />)}
        </section>
      </div>
    );
  }

  if (query.isError) {
    return <StateBlock title="Model usage unavailable" detail={query.error.message} />;
  }

  const data = query.data;

  return (
    <div className="mx-auto grid w-full min-w-0 max-w-[96rem] gap-5 overflow-hidden">
      <RouteHeader
        eyebrow="Model economics"
        title="Model usage and relative session cost"
        action={<RefreshButton queries={["model-usage"]} />}
      />

      <Card className="min-w-0">
        <CardContent className="flex flex-wrap items-center gap-3 pt-6">
          <FilterLabel label="Time limit">
            <Select value={String(sinceDays)} onValueChange={setSinceDays}>
              <SelectTrigger className="min-w-[10rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TIME_OPTIONS.map((days) => (
                  <SelectItem key={days} value={String(days)}>
                    Last {days} days
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FilterLabel>
          <FilterLabel label="Project">
            <Select value={projectName ?? ALL_PROJECTS} onValueChange={setProjectName}>
              <SelectTrigger className="min-w-[18rem] max-w-[26rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_PROJECTS}>All projects</SelectItem>
                {data.project_options.map((project) => (
                  <SelectItem key={project.name} value={project.name}>
                    {project.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FilterLabel>
        </CardContent>
      </Card>

      <SummaryCards data={data} />
      <ModelTable data={data} />
      <TimeBuckets data={data} />
      <SessionTable data={data} />
      <TurnTable data={data} />
    </div>
  );
}

function FilterLabel({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-1 text-caption font-semibold uppercase tracking-wide text-muted-foreground">
      {label}
      {children}
    </label>
  );
}

function SummaryCards({ data }: { data: ModelUsagePayload }) {
  return (
    <section className="grid min-w-0 grid-cols-4 gap-4 max-xl:grid-cols-2 max-md:grid-cols-1">
      <MetricCard
        label="Estimated Cost"
        value={formatCost(data.summary.estimated_cost_usd)}
        detail={`${data.summary.sessions.toLocaleString()} sessions in ${data.filters.since_days} days`}
      />
      <MetricCard
        label="Turns"
        value={compactNumber(data.summary.turns)}
        detail={`${compactNumber(data.summary.total_tokens)} observed tokens`}
      />
      <MetricCard
        label="Models"
        value={data.summary.models}
        detail={data.summary.top_model_by_cost ? `Top cost: ${data.summary.top_model_by_cost}` : "No model usage"}
      />
      <MetricCard
        label="Pricing Gaps"
        value={data.summary.missing_price_count}
        detail={data.summary.top_model_by_sessions ? `Most sessions: ${data.summary.top_model_by_sessions}` : "No sessions"}
      />
    </section>
  );
}

function ModelTable({ data }: { data: ModelUsagePayload }) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="font-display text-xl tracking-tight">Model Mix</CardTitle>
        <CardDescription>Cost is estimated in the dashboard from observed core usage buckets.</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="overflow-auto rounded-lg border border-border-subtle">
          <Table>
            <TableHeader className="bg-table-head font-display text-caption uppercase">
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Sessions</TableHead>
                <TableHead>Turns</TableHead>
                <TableHead className="text-right">Tokens</TableHead>
                <TableHead className="text-right">Total Cost</TableHead>
                <TableHead className="text-right">Avg Session</TableHead>
                <TableHead className="text-right">Avg Turn</TableHead>
                <TableHead>Pricing</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.models.map((row) => (
                <TableRow key={row.model_key}>
                  <TableCell className="font-medium">{row.model_key}</TableCell>
                  <TableCell>{row.sessions.toLocaleString()}</TableCell>
                  <TableCell>{row.turns.toLocaleString()}</TableCell>
                  <TableCell className="text-right">{compactNumber(totalTokens(row.usage))}</TableCell>
                  <TableCell className="text-right">{formatCost(row.estimated_cost_usd)}</TableCell>
                  <TableCell className="text-right">{formatCost(row.avg_session_cost_usd)}</TableCell>
                  <TableCell className="text-right">{formatCost(row.avg_turn_cost_usd)}</TableCell>
                  <TableCell>
                    <Badge variant={row.pricing.confidence === "estimated" ? "default" : "secondary"}>
                      {row.pricing.confidence === "estimated" ? "estimated" : "missing price"}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))}
              {!data.models.length ? (
                <TableRow>
                  <TableCell colSpan={8}>No model usage found for this scope.</TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

function TimeBuckets({ data }: { data: ModelUsagePayload }) {
  const [grain, setGrain] = React.useState("daily");
  const rows = data.time_buckets[grain] ?? [];
  const topRows = rows.slice(-18);
  const maxCost = Math.max(...topRows.map((row) => row.estimated_cost_usd), 0);

  return (
    <Card className="min-w-0">
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle className="font-display text-xl tracking-tight">Cost Over Time</CardTitle>
          <CardDescription>Grouped by model and selected time grain.</CardDescription>
        </div>
        <Select value={grain} onValueChange={setGrain}>
          <SelectTrigger className="min-w-[10rem]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="five_hour">Per 5 hours</SelectItem>
            <SelectItem value="daily">Per day</SelectItem>
            <SelectItem value="weekly">Per week</SelectItem>
            <SelectItem value="monthly">Per month</SelectItem>
          </SelectContent>
        </Select>
      </CardHeader>
      <CardContent className="grid gap-3">
        {topRows.length ? (
          topRows.map((row) => (
            <div key={`${row.bucket}-${row.model_key}`} className="grid gap-1">
              <div className="flex flex-wrap items-center justify-between gap-2 text-body-sm">
                <span className="font-medium">{row.bucket}</span>
                <span className="text-muted-foreground">
                  {row.model_key} · {row.turns} turns · {formatCost(row.estimated_cost_usd)}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded bg-muted">
                <div
                  className="h-full rounded bg-primary"
                  style={{ width: `${maxCost ? Math.max(4, (row.estimated_cost_usd / maxCost) * 100) : 0}%` }}
                />
              </div>
            </div>
          ))
        ) : (
          <StateBlock title="No time buckets" detail="No turn timestamps were available in this scope." />
        )}
      </CardContent>
    </Card>
  );
}

function SessionTable({ data }: { data: ModelUsagePayload }) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="font-display text-xl tracking-tight">Sessions</CardTitle>
        <CardDescription>Progressive drilldown from session cost to dominant model and context usage.</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="overflow-auto rounded-lg border border-border-subtle">
          <Table>
            <TableHeader className="bg-table-head font-display text-caption uppercase">
              <TableRow>
                <TableHead>Session</TableHead>
                <TableHead>Project</TableHead>
                <TableHead>Dominant Model</TableHead>
                <TableHead className="text-right">Context</TableHead>
                <TableHead className="text-right">Tokens</TableHead>
                <TableHead className="text-right">Cost</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.sessions.slice(0, 50).map((session) => (
                <TableRow key={session.id}>
                  <TableCell className="max-w-[24rem]">
                    <SessionLink sessionId={session.id}>
                      {session.title || shortSessionId(session.id)}
                    </SessionLink>
                  </TableCell>
                  <TableCell>{session.project ?? "unknown"}</TableCell>
                  <TableCell>{modelLabel(session.dominant_model)}</TableCell>
                  <TableCell className="text-right">{formatPercent(session.context?.max_used_percent)}</TableCell>
                  <TableCell className="text-right">{compactNumber(totalTokens(session.usage))}</TableCell>
                  <TableCell className="text-right">{formatCost(session.estimated_cost_usd)}</TableCell>
                </TableRow>
              ))}
              {!data.sessions.length ? (
                <TableRow>
                  <TableCell colSpan={6}>No sessions found for this scope.</TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

function TurnTable({ data }: { data: ModelUsagePayload }) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 font-display text-xl tracking-tight">
          <BarChart3 size={18} /> Expensive Turns
        </CardTitle>
        <CardDescription>Top turns by estimated cost, capped to the first 200 rows from the backend.</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="overflow-auto rounded-lg border border-border-subtle">
          <Table>
            <TableHeader className="bg-table-head font-display text-caption uppercase">
              <TableRow>
                <TableHead>Turn</TableHead>
                <TableHead>Session</TableHead>
                <TableHead>Model</TableHead>
                <TableHead className="text-right">Tokens</TableHead>
                <TableHead className="text-right">Context</TableHead>
                <TableHead className="text-right">Cost</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.turns.slice(0, 30).map((turn) => (
                <TableRow key={turn.turn_id}>
                  <TableCell className="font-mono text-body-sm">#{turn.sequence}</TableCell>
                  <TableCell>
                    <SessionLink sessionId={turn.session_id}>
                      {turn.session_title || shortSessionId(turn.session_id)}
                    </SessionLink>
                  </TableCell>
                  <TableCell>{turn.model_key}</TableCell>
                  <TableCell className="text-right">{compactNumber(totalTokens(turn.usage))}</TableCell>
                  <TableCell className="text-right">{formatPercent(turn.context?.final_used_percent)}</TableCell>
                  <TableCell className="text-right">{formatCost(turn.estimated_cost_usd)}</TableCell>
                </TableRow>
              ))}
              {!data.turns.length ? (
                <TableRow>
                  <TableCell colSpan={6}>No turns found for this scope.</TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

function modelLabel(value: ModelUsagePayload["sessions"][number]["dominant_model"]) {
  if (!value) return "unknown";
  if (value.provider && value.model) return `${value.provider}/${value.model}`;
  return value.model ?? value.provider ?? "unknown";
}

function totalTokens(usage: UsageBuckets) {
  return usage.total_tokens ?? 0;
}

function compactNumber(value: number) {
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatCost(value: number) {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: value < 0.01 && value > 0 ? 4 : 2,
  }).format(value);
}

function formatPercent(value?: number | null) {
  if (value == null) return "-";
  return `${value.toFixed(1)}%`;
}
