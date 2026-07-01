import * as React from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { BarChart3 } from "lucide-react";
import {
  fetchModelUsage,
  type DistributionStats,
  type ModelUsageModel,
  type ModelUsagePayload,
  type ModelUsageSession,
  type ModelUsageTurn,
  type UsageBuckets,
} from "@/api";
import { DataTable } from "@/components/data-table";
import { MetricCard } from "@/components/metric-card";
import { RefreshButton } from "@/components/refresh-button";
import { RouteHeader } from "@/components/route-header";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { StateBlock } from "@/components/state-block";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { MetricSkeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const ALL_PROJECTS = "__all_projects__";
const ALL_MODELS = "__all_models__";
const TIME_OPTIONS = [7, 14, 30, 90];
type TokenBucketKey =
  | "total_tokens"
  | "input_tokens"
  | "cached_input_tokens"
  | "output_tokens"
  | "reasoning_output_tokens";
const TOKEN_BUCKET_DEFS = [
  { key: "total_tokens", label: "Total" },
  { key: "input_tokens", label: "Input" },
  { key: "cached_input_tokens", label: "Cached" },
  { key: "output_tokens", label: "Output" },
  { key: "reasoning_output_tokens", label: "Reasoning" },
] as const satisfies ReadonlyArray<{ key: TokenBucketKey; label: string }>;
const VIEW_OPTIONS = [
  { value: "overview", label: "Overview" },
  { value: "cost", label: "Cost" },
  { value: "tokens", label: "Tokens" },
  { value: "time", label: "Time" },
] as const;

type UsageView = (typeof VIEW_OPTIONS)[number]["value"];

export function ModelUsageRoute() {
  const search = useSearch({ from: "/model-usage" });
  const navigate = useNavigate({ from: "/model-usage" });
  const sinceDays = search.sinceDays ?? 7;
  const projectName = search.projectName ?? null;
  const modelKey = search.modelKey ?? null;
  const view = search.view ?? "overview";
  const query = useQuery({
    queryKey: ["model-usage", sinceDays, projectName, modelKey],
    queryFn: () => fetchModelUsage({ sinceDays, projectName, modelKey }),
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
  const setModelKey = (value: string) => {
    void navigate({
      search: (current) => ({
        ...current,
        modelKey: value === ALL_MODELS ? undefined : value,
      }),
    });
  };
  const setView = (nextView: UsageView) => {
    void navigate({
      search: (current) => ({
        ...current,
        view: nextView === "overview" ? undefined : nextView,
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
        title="Model usage overview"
        action={<RefreshButton queries={["model-usage"]} />}
      />

      <Card className="min-w-0">
        <CardContent className="flex flex-wrap items-end gap-3 pt-6">
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
          <FilterLabel label="Model">
            <Select value={modelKey ?? ALL_MODELS} onValueChange={setModelKey}>
              <SelectTrigger className="min-w-[18rem] max-w-[30rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_MODELS}>All models</SelectItem>
                {data.model_options.map((model) => (
                  <SelectItem key={model.model_key} value={model.model_key}>
                    {model.model_key}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FilterLabel>
          <nav className="flex flex-wrap gap-2" aria-label="Model usage views">
            {VIEW_OPTIONS.map((option) => (
              <Button
                key={option.value}
                type="button"
                size="sm"
                variant={view === option.value ? "default" : "outline"}
                onClick={() => setView(option.value)}
              >
                {option.label}
              </Button>
            ))}
          </nav>
        </CardContent>
      </Card>

      {view === "overview" ? <OverviewView data={data} /> : null}
      {view === "cost" ? <CostView data={data} /> : null}
      {view === "tokens" ? <TokensView data={data} /> : null}
      {view === "time" ? <TimeView data={data} /> : null}
    </div>
  );
}

function OverviewView({ data }: { data: ModelUsagePayload }) {
  return (
    <>
      <SummaryCards data={data} view="overview" />
      <OverviewModelTable data={data} />
      <OverviewSessionTable data={data} />
    </>
  );
}

function CostView({ data }: { data: ModelUsagePayload }) {
  return (
    <>
      <SummaryCards data={data} view="cost" />
      <ModelTable data={data} view="cost" />
      <TimeBuckets data={data} view="cost" />
      <SessionTable data={data} view="cost" />
      <TurnTable data={data} view="cost" />
    </>
  );
}

function TokensView({ data }: { data: ModelUsagePayload }) {
  return (
    <>
      <SummaryCards data={data} view="tokens" />
      <TokenBucketCards data={data} />
      <ModelTable data={data} view="tokens" />
      <TimeBuckets data={data} view="tokens" />
      <SessionTable data={data} view="tokens" />
      <TurnTable data={data} view="tokens" />
    </>
  );
}

function TimeView({ data }: { data: ModelUsagePayload }) {
  return (
    <>
      <SummaryCards data={data} view="time" />
      <TimeOverviewTable data={data} />
      <SessionTable data={data} view="time" />
    </>
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

function SummaryCards({ data, view }: { data: ModelUsagePayload; view: UsageView }) {
  if (view === "tokens") {
    const sessionStats = data.summary.token_stats.session;
    const turnStats = data.summary.token_stats.turn;
    return (
      <section className="grid min-w-0 grid-cols-4 gap-4 max-xl:grid-cols-2 max-md:grid-cols-1">
        <MetricCard label="Total Tokens" value={compactNumber(data.summary.total_tokens)} detail={`${data.summary.sessions.toLocaleString()} sessions`} />
        <MetricCard label="Session Tokens" value={compactNumber(sessionStats.avg)} detail={distributionDetail(sessionStats)} />
        <MetricCard label="Turn Tokens" value={compactNumber(turnStats.avg)} detail={distributionDetail(turnStats)} />
        <MetricCard label="Turns" value={compactNumber(data.summary.turns)} detail={`${data.summary.models.toLocaleString()} models in scope`} />
      </section>
    );
  }
  if (view === "time") {
    const sessionStats = data.summary.elapsed_stats.session;
    return (
      <section className="grid min-w-0 grid-cols-4 gap-4 max-xl:grid-cols-2 max-md:grid-cols-1">
        <MetricCard label="Elapsed Time" value={formatDuration(data.summary.total_elapsed_seconds)} detail={`${data.summary.sessions.toLocaleString()} completed sessions`} />
        <MetricCard label="Session Time" value={formatDuration(sessionStats.avg)} detail={distributionDetail(sessionStats, formatDuration)} />
        <MetricCard label="Turns" value={compactNumber(data.summary.turns)} detail={`${compactNumber(data.summary.total_tokens)} filtered tokens`} />
        <MetricCard label="Throughput" value={compactNumber(tokensPerMinute(data.summary.total_tokens, data.summary.total_elapsed_seconds))} detail="tokens/min across elapsed time" />
      </section>
    );
  }
  if (view === "cost") {
    const sessionStats = data.summary.cost_stats.session;
    const turnStats = data.summary.cost_stats.turn;
    return (
      <section className="grid min-w-0 grid-cols-4 gap-4 max-xl:grid-cols-2 max-md:grid-cols-1">
        <MetricCard label="Estimated Cost" value={formatCost(data.summary.estimated_cost_usd)} detail={`${data.summary.sessions.toLocaleString()} sessions in ${data.filters.since_days} days`} />
        <MetricCard label="Session Cost" value={formatCost(sessionStats.avg)} detail={distributionDetail(sessionStats, formatCost)} />
        <MetricCard label="Turn Cost" value={formatCost(turnStats.avg)} detail={distributionDetail(turnStats, formatCost)} />
        <MetricCard label="Pricing Gaps" value={data.summary.missing_price_count} detail={data.summary.top_model_by_sessions ? `Most sessions: ${data.summary.top_model_by_sessions}` : "No sessions"} />
      </section>
    );
  }
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

function TokenBucketCards({ data }: { data: ModelUsagePayload }) {
  return (
    <section className="grid min-w-0 grid-cols-5 gap-4 max-[110rem]:grid-cols-3 max-lg:grid-cols-2 max-md:grid-cols-1">
      {TOKEN_BUCKET_DEFS.map(({ key, label }) => {
        const sessionValues = data.sessions.map((session) => usageValue(session.usage, key));
        const bucketStats = data.summary.token_stats.buckets[key];
        return (
          <MetricCard
            key={key}
            label={`${label} Tokens`}
            value={compactNumber(sum(sessionValues))}
            detail={`session ${distributionDetail(bucketStats.session)} · turn ${distributionDetail(bucketStats.turn)}`}
          />
        );
      })}
    </section>
  );
}

const overviewModelColumns: ColumnDef<ModelUsageModel>[] = [
  {
    accessorKey: "model_key",
    header: () => <HeaderLabel>Model</HeaderLabel>,
    cell: ({ getValue }) => <span className="font-medium">{getValue<string>()}</span>,
  },
  {
    accessorKey: "sessions",
    header: () => <HeaderLabel>Sessions</HeaderLabel>,
    cell: ({ getValue }) => getValue<number>().toLocaleString(),
  },
  {
    accessorKey: "turns",
    header: () => <HeaderLabel>Turns</HeaderLabel>,
    cell: ({ getValue }) => getValue<number>().toLocaleString(),
  },
  {
    id: "avg_turn_tokens",
    accessorFn: (row) => average(totalTokens(row.usage), row.turns),
    header: () => <HeaderLabel align="right">Avg Turn Tokens</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "avg_turn_elapsed_seconds",
    header: () => <HeaderLabel align="right">Avg Turn Time</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    id: "tokens",
    accessorFn: (row) => totalTokens(row.usage),
    header: () => <HeaderLabel align="right">Total Tokens</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  },
  {
    id: "token_confidence",
    accessorFn: (row) => row.usage.total_confidence,
    header: () => <HeaderLabel>Token Total</HeaderLabel>,
    cell: ({ getValue }) => <TokenConfidenceBadge confidence={getValue<string>()} />,
  },
  {
    accessorKey: "estimated_cost_usd",
    header: () => <HeaderLabel align="right">Total Cost</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
  },
  {
    id: "pricing",
    accessorFn: (row) => row.pricing.confidence,
    header: () => <HeaderLabel>Pricing</HeaderLabel>,
    cell: ({ getValue }) => <PricingBadge confidence={getValue<string>()} />,
  },
];

function OverviewModelTable({ data }: { data: ModelUsagePayload }) {
  const rows = React.useMemo(
    () => [...data.models].sort((left, right) => totalTokens(right.usage) - totalTokens(left.usage)),
    [data.models],
  );
  const table = useReactTable({
    data: rows,
    columns: overviewModelColumns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="font-display text-xl tracking-tight">Model Mix Overview</CardTitle>
        <CardDescription>General model comparison across volume, allocated time, and estimated cost.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={overviewModelColumns.length}
          emptyMessage="No model usage found for this scope."
        />
      </CardContent>
    </Card>
  );
}

function modelColumns(view: "cost" | "tokens"): ColumnDef<ModelUsageModel>[] {
  const tokenBucketColumns: ColumnDef<ModelUsageModel>[] = [
    tokenColumn("input_tokens", "Input"),
    tokenColumn("cached_input_tokens", "Cached"),
    tokenColumn("output_tokens", "Output"),
    tokenColumn("reasoning_output_tokens", "Reasoning"),
  ];
  return [
    {
      accessorKey: "model_key",
      header: () => <HeaderLabel>Model</HeaderLabel>,
      cell: ({ getValue }) => <span className="font-medium">{getValue<string>()}</span>,
    },
    {
      accessorKey: "sessions",
      header: () => <HeaderLabel>Sessions</HeaderLabel>,
      cell: ({ getValue }) => getValue<number>().toLocaleString(),
    },
    {
      accessorKey: "turns",
      header: () => <HeaderLabel>Turns</HeaderLabel>,
      cell: ({ getValue }) => getValue<number>().toLocaleString(),
    },
    ...(view === "tokens" ? tokenBucketColumns : []),
    {
      id: "tokens",
      accessorFn: (row) => totalTokens(row.usage),
      header: () => <HeaderLabel align="right">Tokens</HeaderLabel>,
      cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
    },
    ...(view === "tokens"
      ? [
          {
            id: "avg_session_tokens",
            accessorFn: (row) => row.token_stats.session.avg,
            header: () => <HeaderLabel align="right">Avg Session Tokens</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageModel>,
          {
            id: "median_session_tokens",
            accessorFn: (row) => row.token_stats.session.median,
            header: () => <HeaderLabel align="right">Median Session Tokens</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageModel>,
          modelStatColumn("p90_session_tokens", "P90 Session Tokens", (row) => row.token_stats.session.p90),
          modelStatColumn("p95_session_tokens", "P95 Session Tokens", (row) => row.token_stats.session.p95),
          modelStatColumn("avg_turn_tokens", "Avg Turn Tokens", (row) => row.token_stats.turn.avg),
          modelStatColumn("median_turn_tokens", "Median Turn Tokens", (row) => row.token_stats.turn.median),
          modelStatColumn("p90_turn_tokens", "P90 Turn Tokens", (row) => row.token_stats.turn.p90),
          modelStatColumn("p95_turn_tokens", "P95 Turn Tokens", (row) => row.token_stats.turn.p95),
        ]
      : []),
    ...(view === "tokens"
      ? []
      : [
          {
            accessorKey: "estimated_cost_usd",
            header: () => <HeaderLabel align="right">Total Cost</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageModel>,
        ]),
    ...(view === "cost"
      ? [
          {
            accessorKey: "avg_session_cost_usd",
            header: () => <HeaderLabel align="right">Avg Session Cost</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageModel>,
          costStatColumn("median_session_cost_usd", "Median Session Cost", (row) => row.cost_stats.session.median),
          costStatColumn("p90_session_cost_usd", "P90 Session Cost", (row) => row.cost_stats.session.p90),
          costStatColumn("p95_session_cost_usd", "P95 Session Cost", (row) => row.cost_stats.session.p95),
          {
            accessorKey: "avg_turn_cost_usd",
            header: () => <HeaderLabel align="right">Avg Turn Cost</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageModel>,
          costStatColumn("median_turn_cost_usd", "Median Turn Cost", (row) => row.cost_stats.turn.median),
          costStatColumn("p90_turn_cost_usd", "P90 Turn Cost", (row) => row.cost_stats.turn.p90),
          costStatColumn("p95_turn_cost_usd", "P95 Turn Cost", (row) => row.cost_stats.turn.p95),
          {
            id: "pricing",
            accessorFn: (row) => row.pricing.confidence,
            header: () => <HeaderLabel>Pricing</HeaderLabel>,
            cell: ({ getValue }) => <PricingBadge confidence={getValue<string>()} />,
          } satisfies ColumnDef<ModelUsageModel>,
        ]
      : []),
  ];
}

function modelStatColumn(
  id: string,
  label: string,
  accessorFn: (row: ModelUsageModel) => number,
): ColumnDef<ModelUsageModel> {
  return {
    id,
    accessorFn,
    header: () => <HeaderLabel align="right">{label}</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  };
}

function costStatColumn(
  id: string,
  label: string,
  accessorFn: (row: ModelUsageModel) => number,
): ColumnDef<ModelUsageModel> {
  return {
    id,
    accessorFn,
    header: () => <HeaderLabel align="right">{label}</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
  };
}

function tokenColumn(key: keyof UsageBuckets, label: string): ColumnDef<ModelUsageModel> {
  return {
    id: key,
    accessorFn: (row) => row.usage[key] ?? 0,
    header: () => <HeaderLabel align="right">{label}</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  };
}

function ModelTable({ data, view }: { data: ModelUsagePayload; view: "cost" | "tokens" }) {
  const rows = React.useMemo(
    () => [...data.models].sort((left, right) => sortByLens(left, right, view)),
    [data.models, view],
  );
  const columns = React.useMemo(() => modelColumns(view), [view]);
  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="font-display text-xl tracking-tight">Model Mix by {viewLabel(view)}</CardTitle>
        <CardDescription>{modelTableDescription(view)}</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={columns.length}
          emptyMessage="No model usage found for this scope."
        />
      </CardContent>
    </Card>
  );
}

function TimeBuckets({ data, view }: { data: ModelUsagePayload; view: "cost" | "tokens" }) {
  const [grain, setGrain] = React.useState("daily");
  const rows = data.time_buckets[grain] ?? [];
  const topRows = React.useMemo(
    () => [...rows].sort((left, right) => bucketValue(right, view) - bucketValue(left, view)).slice(0, 18),
    [rows, view],
  );
  const maxValue = Math.max(...topRows.map((row) => bucketValue(row, view)), 0);

  return (
    <Card className="min-w-0">
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle className="font-display text-xl tracking-tight">{view === "tokens" ? "Tokens Over Time" : "Cost Over Time"}</CardTitle>
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
          topRows.map((row) => {
            const value = bucketValue(row, view);
            return (
              <div key={`${row.bucket}-${row.model_key}`} className="grid gap-1">
                <div className="flex flex-wrap items-center justify-between gap-2 text-body-sm">
                  <span className="font-medium">{row.bucket}</span>
                  <span className="text-muted-foreground">
                    {row.model_key} · {row.turns} turns · {view === "tokens" ? `${compactNumber(value)} tokens` : formatCost(value)}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded bg-muted">
                  <div
                    className="h-full rounded bg-primary"
                    style={{ width: `${maxValue ? Math.max(4, (value / maxValue) * 100) : 0}%` }}
                  />
                </div>
              </div>
            );
          })
        ) : (
          <StateBlock title="No time buckets" detail="No turn timestamps were available in this scope." />
        )}
      </CardContent>
    </Card>
  );
}

const overviewSessionColumns: ColumnDef<ModelUsageSession>[] = [
  sessionLinkColumn(),
  projectColumn(),
  dominantModelColumn(),
  {
    accessorKey: "elapsed_seconds",
    header: () => <HeaderLabel align="right">Elapsed</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    id: "tokens",
    accessorFn: (row) => totalTokens(row.usage),
    header: () => <HeaderLabel align="right">Tokens</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "estimated_cost_usd",
    header: () => <HeaderLabel align="right">Cost</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
  },
  {
    id: "context",
    accessorFn: (row) => formatPercent(row.context?.max_used_percent),
    header: () => <HeaderLabel align="right">Context</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{getValue<string>()}</RightCell>,
  },
];

function OverviewSessionTable({ data }: { data: ModelUsagePayload }) {
  const rows = React.useMemo(
    () => [...data.sessions].sort((left, right) => totalTokens(right.usage) - totalTokens(left.usage)).slice(0, 30),
    [data.sessions],
  );
  const table = useReactTable({
    data: rows,
    columns: overviewSessionColumns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="font-display text-xl tracking-tight">Session Overview</CardTitle>
        <CardDescription>Broad session comparison across time, tokens, cost, and context.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={overviewSessionColumns.length}
          emptyMessage="No sessions found for this scope."
        />
      </CardContent>
    </Card>
  );
}

const timeOverviewModelColumns: ColumnDef<ModelUsageModel>[] = [
  {
    accessorKey: "model_key",
    header: () => <HeaderLabel>Model</HeaderLabel>,
    cell: ({ getValue }) => <span className="font-medium">{getValue<string>()}</span>,
  },
  {
    accessorKey: "sessions",
    header: () => <HeaderLabel>Sessions</HeaderLabel>,
    cell: ({ getValue }) => getValue<number>().toLocaleString(),
  },
  {
    accessorKey: "turns",
    header: () => <HeaderLabel>Turns</HeaderLabel>,
    cell: ({ getValue }) => getValue<number>().toLocaleString(),
  },
  {
    accessorKey: "elapsed_seconds",
    header: () => <HeaderLabel align="right">Elapsed</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "avg_session_elapsed_seconds",
    header: () => <HeaderLabel align="right">Avg Session Time</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "avg_turn_elapsed_seconds",
    header: () => <HeaderLabel align="right">Avg Turn Time</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    id: "tokens_per_min",
    accessorFn: (row) => tokensPerMinute(totalTokens(row.usage), row.elapsed_seconds),
    header: () => <HeaderLabel align="right">Tokens/Min</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  },
];

function TimeOverviewTable({ data }: { data: ModelUsagePayload }) {
  const rows = React.useMemo(
    () => [...data.models].sort((left, right) => right.elapsed_seconds - left.elapsed_seconds),
    [data.models],
  );
  const table = useReactTable({
    data: rows,
    columns: timeOverviewModelColumns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="font-display text-xl tracking-tight">Model Time Overview</CardTitle>
        <CardDescription>Elapsed-time comparison across models, sessions, and turns.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={timeOverviewModelColumns.length}
          emptyMessage="No model timing found for this scope."
        />
      </CardContent>
    </Card>
  );
}

function sessionColumns(view: UsageView): ColumnDef<ModelUsageSession>[] {
  return [
    sessionLinkColumn(),
    projectColumn(),
    dominantModelColumn(),
    ...(view === "time"
      ? [
          {
            accessorKey: "elapsed_seconds",
            header: () => <HeaderLabel align="right">Elapsed</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageSession>,
          {
            id: "tokens_per_min",
            accessorFn: (row) => tokensPerMinute(totalTokens(row.usage), row.elapsed_seconds),
            header: () => <HeaderLabel align="right">Tokens/Min</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageSession>,
        ]
      : view === "tokens"
        ? [
            sessionTokenColumn("input_tokens", "Input"),
            sessionTokenColumn("cached_input_tokens", "Cached"),
            sessionTokenColumn("output_tokens", "Output"),
            sessionTokenColumn("reasoning_output_tokens", "Reasoning"),
          ]
      : [
          {
            id: "context",
            accessorFn: (row) => formatPercent(row.context?.max_used_percent),
            header: () => <HeaderLabel align="right">Context</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{getValue<string>()}</RightCell>,
          } satisfies ColumnDef<ModelUsageSession>,
        ]),
    {
      id: "tokens",
      accessorFn: (row) => totalTokens(row.usage),
      header: () => <HeaderLabel align="right">Tokens</HeaderLabel>,
      cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
    },
    ...(view === "tokens"
      ? []
      : [
          {
            accessorKey: "estimated_cost_usd",
            header: () => <HeaderLabel align="right">Cost</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageSession>,
        ]),
  ];
}

function sessionLinkColumn(): ColumnDef<ModelUsageSession> {
  return {
    id: "session",
    header: () => <HeaderLabel>Session</HeaderLabel>,
    cell: ({ row }) => (
      <div className="max-w-[24rem]">
        <SessionLink sessionId={row.original.id}>
          {row.original.title || shortSessionId(row.original.id)}
        </SessionLink>
      </div>
    ),
  };
}

function projectColumn(): ColumnDef<ModelUsageSession> {
  return {
    id: "project",
    accessorFn: (row) => row.project ?? "unknown",
    header: () => <HeaderLabel>Project</HeaderLabel>,
  };
}

function dominantModelColumn(): ColumnDef<ModelUsageSession> {
  return {
    id: "dominant_model",
    accessorFn: (row) => modelLabel(row.dominant_model),
    header: () => <HeaderLabel>Dominant Model</HeaderLabel>,
  };
}

function sessionTokenColumn(key: keyof UsageBuckets, label: string): ColumnDef<ModelUsageSession> {
  return {
    id: key,
    accessorFn: (row) => row.usage[key] ?? 0,
    header: () => <HeaderLabel align="right">{label}</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  };
}

function SessionTable({ data, view }: { data: ModelUsagePayload; view: UsageView }) {
  const rows = React.useMemo(
    () => [...data.sessions].sort((left, right) => sortByLens(left, right, view)).slice(0, 50),
    [data.sessions, view],
  );
  const columns = React.useMemo(() => sessionColumns(view), [view]);
  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="font-display text-xl tracking-tight">{view === "time" ? "Slowest Sessions" : "Sessions"}</CardTitle>
        <CardDescription>{sessionTableDescription(view)}</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={columns.length}
          emptyMessage="No sessions found for this scope."
        />
      </CardContent>
    </Card>
  );
}

function turnColumns(view: "cost" | "tokens"): ColumnDef<ModelUsageTurn>[] {
  return [
    {
      accessorKey: "sequence",
      header: () => <HeaderLabel>Turn</HeaderLabel>,
      cell: ({ getValue }) => <span className="font-mono text-body-sm">#{getValue<number>()}</span>,
    },
    {
      id: "session",
      header: () => <HeaderLabel>Session</HeaderLabel>,
      cell: ({ row }) => (
        <SessionLink sessionId={row.original.session_id}>
          {row.original.session_title || shortSessionId(row.original.session_id)}
        </SessionLink>
      ),
    },
    {
      accessorKey: "model_key",
      header: () => <HeaderLabel>Model</HeaderLabel>,
    },
    ...(view === "tokens"
      ? [
          turnTokenColumn("input_tokens", "Input"),
          turnTokenColumn("cached_input_tokens", "Cached"),
          turnTokenColumn("output_tokens", "Output"),
          turnTokenColumn("reasoning_output_tokens", "Reasoning"),
        ]
      : [
          {
            id: "context",
            accessorFn: (row) => formatPercent(row.context?.final_used_percent),
            header: () => <HeaderLabel align="right">Context</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{getValue<string>()}</RightCell>,
          } satisfies ColumnDef<ModelUsageTurn>,
        ]),
    {
      id: "tokens",
      accessorFn: (row) => totalTokens(row.usage),
      header: () => <HeaderLabel align="right">Tokens</HeaderLabel>,
      cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
    },
    ...(view === "tokens"
      ? []
      : [
          {
            accessorKey: "estimated_cost_usd",
            header: () => <HeaderLabel align="right">Cost</HeaderLabel>,
            cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageTurn>,
        ]),
  ];
}

function turnTokenColumn(key: keyof UsageBuckets, label: string): ColumnDef<ModelUsageTurn> {
  return {
    id: key,
    accessorFn: (row) => row.usage[key] ?? 0,
    header: () => <HeaderLabel align="right">{label}</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  };
}

function TurnTable({ data, view }: { data: ModelUsagePayload; view: "cost" | "tokens" }) {
  const rows = React.useMemo(
    () => [...data.turns].sort((left, right) => sortByLens(left, right, view)).slice(0, 30),
    [data.turns, view],
  );
  const columns = React.useMemo(() => turnColumns(view), [view]);
  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 font-display text-xl tracking-tight">
          <BarChart3 size={18} /> {view === "tokens" ? "Largest Token Turns" : "Expensive Turns"}
        </CardTitle>
        <CardDescription>{view === "tokens" ? "Top turns by observed token volume." : "Top turns by estimated cost, capped to the first 200 rows from the backend."}</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={columns.length}
          emptyMessage="No turns found for this scope."
        />
      </CardContent>
    </Card>
  );
}

function HeaderLabel({ children, align = "left" }: { children: React.ReactNode; align?: "left" | "right" }) {
  return (
    <span
      className={cn(
        "font-extrabold uppercase tracking-wide",
        align === "right" && "text-right",
      )}
    >
      {children}
    </span>
  );
}

function RightCell({ children }: { children: React.ReactNode }) {
  return <div className="text-right">{children}</div>;
}

function PricingBadge({ confidence }: { confidence: string }) {
  return (
    <Badge variant={confidence === "estimated" ? "default" : "secondary"}>
      {confidence === "estimated" ? "estimated" : "missing price"}
    </Badge>
  );
}

function TokenConfidenceBadge({ confidence }: { confidence?: string }) {
  if (confidence === "reported_inconsistent") {
    return <Badge variant="secondary">derived</Badge>;
  }
  if (confidence === "reported_consistent") {
    return <Badge variant="default">reported</Badge>;
  }
  return <Badge variant="secondary">derived</Badge>;
}

function modelLabel(value: ModelUsageSession["dominant_model"]) {
  if (!value) return "unknown";
  if (value.provider && value.model) return `${value.provider}/${value.model}`;
  return value.model ?? value.provider ?? "unknown";
}

function totalTokens(usage: UsageBuckets) {
  return usage.total_tokens ?? 0;
}

function usageValue(usage: UsageBuckets, key: TokenBucketKey) {
  return usage[key] ?? 0;
}

function average(numerator: number, denominator: number) {
  if (denominator <= 0) return 0;
  return numerator / denominator;
}

function sum(values: number[]) {
  return values.reduce((total, value) => total + value, 0);
}

function distributionDetail(
  stats: DistributionStats,
  formatValue: (value: number) => string = compactNumber,
) {
  return `avg ${formatValue(stats.avg)} / med ${formatValue(stats.median)} / p90 ${formatValue(stats.p90)} / p95 ${formatValue(stats.p95)}`;
}

function bucketValue(row: ModelUsagePayload["time_buckets"][string][number], view: "cost" | "tokens") {
  return view === "tokens" ? totalTokens(row.usage) : row.estimated_cost_usd;
}

function sortByLens<T extends { usage: UsageBuckets; estimated_cost_usd: number; elapsed_seconds?: number }>(
  left: T,
  right: T,
  view: UsageView | "cost" | "tokens",
) {
  if (view === "tokens") return totalTokens(right.usage) - totalTokens(left.usage);
  if (view === "time") return (right.elapsed_seconds ?? 0) - (left.elapsed_seconds ?? 0);
  return right.estimated_cost_usd - left.estimated_cost_usd;
}

function tokensPerMinute(tokens: number, seconds: number) {
  if (seconds <= 0) return 0;
  return Math.round((tokens / seconds) * 60);
}

function viewLabel(view: UsageView) {
  if (view === "tokens") return "Tokens";
  if (view === "time") return "Time";
  return "Cost";
}

function modelTableDescription(view: "cost" | "tokens") {
  if (view === "tokens") return "Model-price-neutral token volume with bucket breakdown.";
  if (view === "cost") return "Cost is estimated in the dashboard from observed core usage buckets.";
  return "Cost, volume, and pricing status across models in the selected scope.";
}

function sessionTableDescription(view: UsageView) {
  if (view === "time") return "Elapsed time is session-level; token throughput reflects the selected model filter.";
  if (view === "tokens") return "Sessions ranked by observed token volume for the selected scope.";
  return "Progressive drilldown from session cost to dominant model and context usage.";
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

function formatDuration(value: number) {
  const seconds = Math.max(0, Math.round(value));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${remainingSeconds}s`;
  return `${remainingSeconds}s`;
}

function formatPercent(value?: number | null) {
  if (value == null) return "-";
  return `${value.toFixed(1)}%`;
}
