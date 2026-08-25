import * as React from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useInfiniteQuery } from "@tanstack/react-query";
import { getCoreRowModel, getSortedRowModel, getPaginationRowModel, useReactTable, type ColumnDef, type SortingState } from "@tanstack/react-table";
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
import { DataTableColumnHeader } from "@/components/ui/data-table-column-header";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { MetricCard } from "@/components/metric-card";
import { RouteHeader } from "@/components/route-header";
import { SectionTabs } from "@/components/section-tabs";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { StateBlock } from "@/components/state-block";
import { LoadingShell } from "@/components/loading-shell";
import { UsageTimelineChart } from "@/components/charts";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { FilterLabel, RightCell } from "@/components/table-cells";
import { useDateRange } from "@/hooks/use-date-range";
import {
  formatCompactNumber,
  formatCostUsd,
  formatDuration,
  formatPercent,
} from "@/lib/format";
import { EfficiencyLens } from "@/routes/usage-efficiency";

const ALL_PROJECTS = "__all_projects__";
const ALL_MODELS = "__all_models__";
type TokenBucketKey =
  | "processed_tokens"
  | "prompt_tokens"
  | "cached_prompt_tokens"
  | "completion_tokens"
  | "reasoning_tokens";
const TOKEN_BUCKET_DEFS = [
  { key: "processed_tokens", label: "Processed" },
  { key: "prompt_tokens", label: "Prompt" },
  { key: "cached_prompt_tokens", label: "Cached" },
  { key: "completion_tokens", label: "Completion" },
  { key: "reasoning_tokens", label: "Reasoning" },
] as const satisfies ReadonlyArray<{ key: TokenBucketKey; label: string }>;
const VIEW_OPTIONS = [
  { value: "overview", label: "Overview" },
  { value: "cost", label: "Cost" },
  { value: "tokens", label: "Tokens" },
  { value: "time", label: "Time" },
  { value: "efficiency", label: "Efficiency" },
] as const;

type UsageView = (typeof VIEW_OPTIONS)[number]["value"];

export function ModelUsageRoute() {
  const search = useSearch({ from: "/model-usage" });
  const navigate = useNavigate({ from: "/model-usage" });
  const { days: sinceDays } = useDateRange();
  const projectName = search.projectName ?? null;
  const modelKey = search.modelKey ?? null;
  const view = search.view ?? "overview";
  const grain = search.grain ?? "weekly";
  const unit = search.unit ?? "session";
  const [sectionTab, setSectionTab] = React.useState("models");
  React.useEffect(() => {
    setSectionTab("models");
  }, [view]);
  const needsTurns =
    (view === "cost" || view === "tokens") && sectionTab === "turns";
  const sessionsQuery = useInfiniteQuery({
    queryKey: ["model-usage", sinceDays, projectName, modelKey, "sessions"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      fetchModelUsage({
        sinceDays,
        projectName,
        modelKey,
        detail: "sessions",
        cursor: pageParam ?? undefined,
        limit: 50,
        signal,
      }),
    getNextPageParam: (lastPage) =>
      lastPage.pages?.sessions?.next_cursor ?? undefined,
    gcTime: 5 * 60_000,
  });
  const sessionRevision =
    sessionsQuery.data?.pages[0]?.pages?.sessions?.revision ?? null;
  const turnsQuery = useInfiniteQuery({
    queryKey: [
      "model-usage",
      sinceDays,
      projectName,
      modelKey,
      "turns",
      sessionRevision,
    ],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      fetchModelUsage({
        sinceDays,
        projectName,
        modelKey,
        detail: "turns",
        cursor: pageParam ?? undefined,
        revision: sessionRevision ?? undefined,
        limit: 50,
        signal,
      }),
    getNextPageParam: (lastPage) =>
      lastPage.pages?.turns?.next_cursor ?? undefined,
    enabled: sessionRevision != null && needsTurns,
    gcTime: 5 * 60_000,
  });

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

  if (sessionsQuery.isPending) {
    return <LoadingShell eyebrow="Model economics" title="Loading model usage" variant="metrics" />;
  }

  if (sessionsQuery.isError) {
    return <StateBlock title="Model usage unavailable" detail={sessionsQuery.error?.message ?? "The model usage query failed."} onRetry={() => { void sessionsQuery.refetch(); }} />;
  }

  const first = sessionsQuery.data.pages[0];
  const data: ModelUsagePayload = {
    ...first,
    sessions: Array.from(
      new Map(
        sessionsQuery.data.pages
          .flatMap((page) => page.sessions)
          .map((row) => [row.id, row]),
      ).values(),
    ),
    turns: Array.from(
      new Map(
        (turnsQuery.data?.pages ?? [])
          .flatMap((page) => page.turns)
          .map((row) => [`${row.session_id}:${row.turn_id}`, row]),
      ).values(),
    ),
  };

  return (
    <div className="route-container w-full min-w-0 overflow-hidden">
      <RouteHeader
        eyebrow="Usage"
        title="Where did tokens, time, and money go?"
      />

      <Card className="min-w-0">
        <CardContent className="grid gap-6 pt-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="eyebrow-soft text-muted-foreground">
              Showing the last {sinceDays} days
            </p>
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
          </div>
          <div className="flex flex-wrap items-end gap-3">
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
          </div>
        </CardContent>
      </Card>

      {view === "overview" ? <OverviewView data={data} /> : null}
      {view === "cost" ? <CostView data={data} activeTab={sectionTab} onTabChange={setSectionTab} turnsPending={turnsQuery.isPending} turnsError={turnsQuery.error} onRetryTurns={() => { void turnsQuery.refetch(); }} /> : null}
      {view === "tokens" ? <TokensView data={data} activeTab={sectionTab} onTabChange={setSectionTab} turnsPending={turnsQuery.isPending} turnsError={turnsQuery.error} onRetryTurns={() => { void turnsQuery.refetch(); }} /> : null}
      {view === "time" ? <TimeView data={data} /> : null}
      {view === "efficiency" ? (
        <EfficiencyLens
          projectName={projectName}
          grain={grain}
          unit={unit}
          onSearchChange={(patch) => {
            void navigate({ search: (current) => ({ ...current, ...patch }) });
          }}
        />
      ) : null}
      {sessionsQuery.hasNextPage || turnsQuery.hasNextPage ? (
        <div className="flex flex-wrap justify-center gap-2">
          {sessionsQuery.hasNextPage ? (
            <Button
              type="button"
              variant="outline"
              onClick={() => void sessionsQuery.fetchNextPage()}
              disabled={sessionsQuery.isFetchingNextPage}
            >
              {sessionsQuery.isFetchingNextPage ? "Loading sessions…" : "Load more sessions"}
            </Button>
          ) : null}
          {needsTurns && turnsQuery.hasNextPage ? (
            <Button
              type="button"
              variant="outline"
              onClick={() => void turnsQuery.fetchNextPage()}
              disabled={turnsQuery.isFetchingNextPage}
            >
              {turnsQuery.isFetchingNextPage ? "Loading turns…" : "Load more turns"}
            </Button>
          ) : null}
        </div>
      ) : null}
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

function CostView({
  data,
  activeTab,
  onTabChange,
  turnsPending,
  turnsError,
  onRetryTurns,
}: {
  data: ModelUsagePayload;
  activeTab: string;
  onTabChange: (tab: string) => void;
  turnsPending: boolean;
  turnsError: Error | null;
  onRetryTurns: () => void;
}) {
  return (
    <SectionTabs
      summary={<SummaryCards data={data} view="cost" />}
      activeTab={activeTab}
      onTabChange={onTabChange}
      tabs={[
        {
          id: "models",
          label: "Models",
          content: (
            <>
              <ModelTable data={data} view="cost" />
              <TimeBuckets data={data} view="cost" />
            </>
          ),
        },
        {
          id: "sessions",
          label: "Sessions",
          content: <SessionTable data={data} view="cost" />,
        },
        {
          id: "turns",
          label: "Turns",
          content: turnsPending ? (
            <LoadingShell eyebrow="Usage detail" title="Loading turn economics" variant="table" />
          ) : turnsError ? (
            <StateBlock title="Turn economics unavailable" detail={turnsError.message} onRetry={onRetryTurns} />
          ) : (
            <TurnTable data={data} view="cost" />
          ),
        },
      ]}
    />
  );
}

function TokensView({
  data,
  activeTab,
  onTabChange,
  turnsPending,
  turnsError,
  onRetryTurns,
}: {
  data: ModelUsagePayload;
  activeTab: string;
  onTabChange: (tab: string) => void;
  turnsPending: boolean;
  turnsError: Error | null;
  onRetryTurns: () => void;
}) {
  return (
    <SectionTabs
      summary={
        <>
          <SummaryCards data={data} view="tokens" />
          <TokenBucketCards data={data} />
        </>
      }
      activeTab={activeTab}
      onTabChange={onTabChange}
      tabs={[
        {
          id: "models",
          label: "Models",
          content: (
            <>
              <ModelTable data={data} view="tokens" />
              <TimeBuckets data={data} view="tokens" />
            </>
          ),
        },
        {
          id: "sessions",
          label: "Sessions",
          content: <SessionTable data={data} view="tokens" />,
        },
        {
          id: "turns",
          label: "Turns",
          content: turnsPending ? (
            <LoadingShell eyebrow="Usage detail" title="Loading turn economics" variant="table" />
          ) : turnsError ? (
            <StateBlock title="Turn economics unavailable" detail={turnsError.message} onRetry={onRetryTurns} />
          ) : (
            <TurnTable data={data} view="tokens" />
          ),
        },
      ]}
    />
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

function SummaryCards({ data, view }: { data: ModelUsagePayload; view: UsageView }) {
  if (view === "tokens") {
    const sessionStats = data.summary.token_stats.session;
    const turnStats = data.summary.token_stats.turn;
    return (
      <section className="stat-grid min-w-0">
        <MetricCard label="Processed Tokens" value={formatCompactNumber(data.summary.processed_tokens)} detail={`${data.summary.sessions.toLocaleString()} sessions`} />
        <MetricCard label="Session Tokens" value={formatCompactNumber(sessionStats.avg)} detail={distributionDetail(sessionStats)} />
        <MetricCard label="Turn Tokens" value={formatCompactNumber(turnStats.avg)} detail={distributionDetail(turnStats)} />
        <MetricCard label="Turns" value={formatCompactNumber(data.summary.turns)} detail={`${data.summary.models.toLocaleString()} models in scope`} />
      </section>
    );
  }
  if (view === "time") {
    const sessionStats = data.summary.elapsed_stats.session;
    return (
      <section className="stat-grid min-w-0">
        <MetricCard label="Elapsed Time" value={formatDuration(data.summary.total_elapsed_seconds)} detail={`${data.summary.sessions.toLocaleString()} completed sessions`} />
        <MetricCard label="Session Time" value={formatDuration(sessionStats.avg)} detail={distributionDetail(sessionStats, formatDuration)} />
        <MetricCard label="Turns" value={formatCompactNumber(data.summary.turns)} detail={`${formatCompactNumber(data.summary.processed_tokens)} filtered tokens`} />
        <MetricCard label="Throughput" value={formatCompactNumber(tokensPerMinute(data.summary.processed_tokens, data.summary.total_elapsed_seconds))} detail="tokens/min across elapsed time" />
      </section>
    );
  }
  if (view === "cost") {
    const sessionStats = data.summary.cost_stats.session;
    const turnStats = data.summary.cost_stats.turn;
    return (
      <section className="stat-grid min-w-0">
        <MetricCard label="Estimated Cost" value={formatCostUsd(data.summary.estimated_cost_usd)} detail={`${data.summary.sessions.toLocaleString()} sessions in ${data.filters.since_days} days`} />
        <MetricCard label="Session Cost" value={formatCostUsd(sessionStats.avg)} detail={distributionDetail(sessionStats, formatCostUsd)} />
        <MetricCard label="Turn Cost" value={formatCostUsd(turnStats.avg)} detail={distributionDetail(turnStats, formatCostUsd)} />
        <MetricCard label="Pricing Gaps" value={data.summary.missing_price_count} detail={data.summary.top_model_by_sessions ? `Most sessions: ${data.summary.top_model_by_sessions}` : "No sessions"} />
      </section>
    );
  }
  return (
    <section className="stat-grid min-w-0">
      <MetricCard
        label="Estimated Cost"
        value={formatCostUsd(data.summary.estimated_cost_usd)}
        detail={`${data.summary.sessions.toLocaleString()} sessions in ${data.filters.since_days} days`}
      />
      <MetricCard
        label="Turns"
        value={formatCompactNumber(data.summary.turns)}
        detail={`${formatCompactNumber(data.summary.processed_tokens)} observed tokens`}
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
    <section className="stat-grid-5 min-w-0">
      {TOKEN_BUCKET_DEFS.map(({ key, label }) => {
        const sessionValues = data.sessions.map((session) => usageValue(session.usage, key));
        const bucketStats = data.summary.token_stats.buckets[key];
        return (
          <MetricCard
            key={key}
            label={`${label} Tokens`}
            value={formatCompactNumber(sum(sessionValues))}
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
    header: ({ column }) => <DataTableColumnHeader column={column} label="Model" />,
    cell: ({ getValue }) => <span className="font-medium">{getValue<string>()}</span>,
  },
  {
    accessorKey: "sessions",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Sessions" />,
    cell: ({ getValue }) => getValue<number>().toLocaleString(),
  },
  {
    accessorKey: "turns",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Turns" />,
    cell: ({ getValue }) => getValue<number>().toLocaleString(),
  },
  {
    id: "avg_turn_tokens",
    accessorFn: (row) => average(totalTokens(row.usage), row.turns),
    header: ({ column }) => <DataTableColumnHeader column={column} label="Avg Turn Tokens" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "avg_turn_elapsed_seconds",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Avg Turn Time" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    id: "tokens",
    accessorFn: (row) => totalTokens(row.usage),
    header: ({ column }) => <DataTableColumnHeader column={column} label="Processed Tokens" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
  },
  {
    id: "token_confidence",
    accessorFn: (row) => row.usage.total_confidence,
    header: ({ column }) => <DataTableColumnHeader column={column} label="Token Total" />,
    cell: ({ getValue }) => <TokenConfidenceBadge confidence={getValue<string>()} />,
  },
  {
    accessorKey: "estimated_cost_usd",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Total Cost" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
  },
  {
    id: "pricing",
    accessorFn: (row) => row.pricing.confidence,
    header: ({ column }) => <DataTableColumnHeader column={column} label="Pricing" />,
    cell: ({ getValue }) => <PricingBadge confidence={getValue<string>()} />,
  },
];

function OverviewModelTable({ data }: { data: ModelUsagePayload }) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const rows = React.useMemo(
    () => [...data.models].sort((left, right) => totalTokens(right.usage) - totalTokens(left.usage)),
    [data.models],
  );
  const table = useReactTable({
    data: rows,
    columns: overviewModelColumns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Model Mix Overview</CardTitle>
        <CardDescription>General model comparison across volume, allocated time, and estimated cost.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={overviewModelColumns.length}
          emptyMessage="No model usage found for this scope."
          emptyHint="Try selecting a different project or model filter."
          showColumnToggle
          showDensityToggle
          showExport
          exportFilename="model-usage-overview-models"
        />
        <DataTablePagination table={table} />
      </CardContent>
    </Card>
  );
}

function modelColumns(view: "cost" | "tokens"): ColumnDef<ModelUsageModel>[] {
  const tokenBucketColumns: ColumnDef<ModelUsageModel>[] = [
    tokenColumn("prompt_tokens", "Prompt"),
    tokenColumn("cached_prompt_tokens", "Cached"),
    tokenColumn("completion_tokens", "Completion"),
    tokenColumn("reasoning_tokens", "Reasoning"),
  ];
  return [
    {
      accessorKey: "model_key",
      header: ({ column }) => <DataTableColumnHeader column={column} label="Model" />,
      cell: ({ getValue }) => <span className="font-medium">{getValue<string>()}</span>,
    },
    {
      accessorKey: "sessions",
      header: ({ column }) => <DataTableColumnHeader column={column} label="Sessions" />,
      cell: ({ getValue }) => getValue<number>().toLocaleString(),
    },
    {
      accessorKey: "turns",
      header: ({ column }) => <DataTableColumnHeader column={column} label="Turns" />,
      cell: ({ getValue }) => getValue<number>().toLocaleString(),
    },
    ...(view === "tokens" ? tokenBucketColumns : []),
    {
      id: "tokens",
      accessorFn: (row) => totalTokens(row.usage),
      header: ({ column }) => <DataTableColumnHeader column={column} label="Tokens" className="text-right" />,
      cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
    },
    ...(view === "tokens"
      ? [
          {
            id: "avg_session_tokens",
            accessorFn: (row) => row.token_stats.session.avg,
            header: ({ column }) => <DataTableColumnHeader column={column} label="Avg Session Tokens" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageModel>,
          {
            id: "median_session_tokens",
            accessorFn: (row) => row.token_stats.session.median,
            header: ({ column }) => <DataTableColumnHeader column={column} label="Median Session Tokens" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
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
            header: ({ column }) => <DataTableColumnHeader column={column} label="Total Cost" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageModel>,
        ]),
    ...(view === "cost"
      ? [
          {
            accessorKey: "avg_session_cost_usd",
            header: ({ column }) => <DataTableColumnHeader column={column} label="Avg Session Cost" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageModel>,
          costStatColumn("median_session_cost_usd", "Median Session Cost", (row) => row.cost_stats.session.median),
          costStatColumn("p90_session_cost_usd", "P90 Session Cost", (row) => row.cost_stats.session.p90),
          costStatColumn("p95_session_cost_usd", "P95 Session Cost", (row) => row.cost_stats.session.p95),
          {
            accessorKey: "avg_turn_cost_usd",
            header: ({ column }) => <DataTableColumnHeader column={column} label="Avg Turn Cost" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageModel>,
          costStatColumn("median_turn_cost_usd", "Median Turn Cost", (row) => row.cost_stats.turn.median),
          costStatColumn("p90_turn_cost_usd", "P90 Turn Cost", (row) => row.cost_stats.turn.p90),
          costStatColumn("p95_turn_cost_usd", "P95 Turn Cost", (row) => row.cost_stats.turn.p95),
          {
            id: "pricing",
            accessorFn: (row) => row.pricing.confidence,
            header: ({ column }) => <DataTableColumnHeader column={column} label="Pricing" />,
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
    header: ({ column }) => <DataTableColumnHeader column={column} label={label} className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
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
    header: ({ column }) => <DataTableColumnHeader column={column} label={label} className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
  };
}

function tokenColumn(key: keyof UsageBuckets, label: string): ColumnDef<ModelUsageModel> {
  return {
    id: key,
    accessorFn: (row) => row.usage[key] ?? 0,
    header: ({ column }) => <DataTableColumnHeader column={column} label={label} className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
  };
}

function ModelTable({ data, view }: { data: ModelUsagePayload; view: "cost" | "tokens" }) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const rows = React.useMemo(
    () => [...data.models].sort((left, right) => sortByLens(left, right, view)),
    [data.models, view],
  );
  const columns = React.useMemo(() => modelColumns(view), [view]);
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Model Mix by {viewLabel(view)}</CardTitle>
        <CardDescription>{modelTableDescription(view)}</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={columns.length}
          emptyMessage="No model usage found for this scope."
          emptyHint="Try selecting a different project or model filter."
          showColumnToggle
          showDensityToggle
          showExport
          exportFilename="model-usage-models"
        />
        <DataTablePagination table={table} />
      </CardContent>
    </Card>
  );
}

function TimeBuckets({ data, view }: { data: ModelUsagePayload; view: "cost" | "tokens" }) {
  return <UsageTimelineChart buckets={data.time_buckets} view={view} />;
}

const overviewSessionColumns: ColumnDef<ModelUsageSession>[] = [
  sessionLinkColumn(),
  projectColumn(),
  dominantModelColumn(),
  {
    accessorKey: "elapsed_seconds",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Elapsed" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    id: "tokens",
    accessorFn: (row) => totalTokens(row.usage),
    header: ({ column }) => <DataTableColumnHeader column={column} label="Tokens" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "estimated_cost_usd",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Cost" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
  },
  {
    id: "context",
    accessorFn: (row) => formatPercent(row.context?.max_used_percent),
    header: ({ column }) => <DataTableColumnHeader column={column} label="Context" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{getValue<string>()}</RightCell>,
  },
];

function SessionModelsDetail({ session }: { session: ModelUsageSession }) {
  return (
    <div className="grid gap-1.5">
      <p className="text-caption font-medium text-muted-foreground">Models in this session</p>
      {session.models.length === 0 ? (
        <p className="text-caption text-muted-foreground">No model breakdown available.</p>
      ) : (
        <Table className="text-caption">
          <TableHeader>
            <TableRow>
              <TableHead>Model</TableHead>
              <TableHead className="text-right">Turns</TableHead>
              <TableHead className="text-right">Tokens</TableHead>
              <TableHead className="text-right">Cost</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {session.models.map((model) => (
              <TableRow key={model.model_key}>
                <TableCell className="py-1 px-2 font-medium">{model.model_key}</TableCell>
                <TableCell className="py-1 px-2 text-right">{model.turns.toLocaleString()}</TableCell>
                <TableCell className="py-1 px-2 text-right">{formatCompactNumber(totalTokens(model.usage))}</TableCell>
                <TableCell className="py-1 px-2 text-right">{formatCostUsd(model.estimated_cost_usd)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

function OverviewSessionTable({ data }: { data: ModelUsagePayload }) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const rows = React.useMemo(
    () => [...data.sessions].sort((left, right) => totalTokens(right.usage) - totalTokens(left.usage)),
    [data.sessions],
  );
  const table = useReactTable({
    data: rows,
    columns: overviewSessionColumns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Session Overview</CardTitle>
        <CardDescription>Broad session comparison across time, tokens, cost, and context.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={overviewSessionColumns.length}
          emptyMessage="No sessions found for this scope."
          emptyHint="Try selecting a different project or model filter."
          showDensityToggle
          showExport
          exportFilename="model-usage-sessions"
          renderRowDetail={(session) => <SessionModelsDetail session={session} />}
        />
        <DataTablePagination table={table} />
      </CardContent>
    </Card>
  );
}

const timeOverviewModelColumns: ColumnDef<ModelUsageModel>[] = [
  {
    accessorKey: "model_key",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Model" />,
    cell: ({ getValue }) => <span className="font-medium">{getValue<string>()}</span>,
  },
  {
    accessorKey: "sessions",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Sessions" />,
    cell: ({ getValue }) => getValue<number>().toLocaleString(),
  },
  {
    accessorKey: "turns",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Turns" />,
    cell: ({ getValue }) => getValue<number>().toLocaleString(),
  },
  {
    accessorKey: "elapsed_seconds",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Elapsed" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "avg_session_elapsed_seconds",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Avg Session Time" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "avg_turn_elapsed_seconds",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Avg Turn Time" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
  },
  {
    id: "tokens_per_min",
    accessorFn: (row) => tokensPerMinute(totalTokens(row.usage), row.elapsed_seconds),
    header: ({ column }) => <DataTableColumnHeader column={column} label="Tokens/Min" className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
  },
];

function TimeOverviewTable({ data }: { data: ModelUsagePayload }) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const rows = React.useMemo(
    () => [...data.models].sort((left, right) => right.elapsed_seconds - left.elapsed_seconds),
    [data.models],
  );
  const table = useReactTable({
    data: rows,
    columns: timeOverviewModelColumns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Model Time Overview</CardTitle>
        <CardDescription>Elapsed-time comparison across models, sessions, and turns.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={timeOverviewModelColumns.length}
          emptyMessage="No model timing found for this scope."
          emptyHint="Try selecting a different project or model filter."
          showDensityToggle
          showExport
          exportFilename="model-usage-time-overview"
        />
        <DataTablePagination table={table} />
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
            header: ({ column }) => <DataTableColumnHeader column={column} label="Elapsed" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{formatDuration(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageSession>,
          {
            id: "tokens_per_min",
            accessorFn: (row) => tokensPerMinute(totalTokens(row.usage), row.elapsed_seconds),
            header: ({ column }) => <DataTableColumnHeader column={column} label="Tokens/Min" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageSession>,
        ]
      : view === "tokens"
        ? [
            sessionTokenColumn("prompt_tokens", "Prompt"),
            sessionTokenColumn("cached_prompt_tokens", "Cached"),
            sessionTokenColumn("completion_tokens", "Completion"),
            sessionTokenColumn("reasoning_tokens", "Reasoning"),
          ]
      : [
          {
            id: "context",
            accessorFn: (row) => formatPercent(row.context?.max_used_percent),
            header: ({ column }) => <DataTableColumnHeader column={column} label="Context" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{getValue<string>()}</RightCell>,
          } satisfies ColumnDef<ModelUsageSession>,
        ]),
    {
      id: "tokens",
      accessorFn: (row) => totalTokens(row.usage),
      header: ({ column }) => <DataTableColumnHeader column={column} label="Tokens" className="text-right" />,
      cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
    },
    ...(view === "tokens"
      ? []
      : [
          {
            accessorKey: "estimated_cost_usd",
            header: ({ column }) => <DataTableColumnHeader column={column} label="Cost" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageSession>,
        ]),
  ];
}

function sessionLinkColumn(): ColumnDef<ModelUsageSession> {
  return {
    id: "session",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Session" />,
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
    header: ({ column }) => <DataTableColumnHeader column={column} label="Project" />,
  };
}

function dominantModelColumn(): ColumnDef<ModelUsageSession> {
  return {
    id: "dominant_model",
    accessorFn: (row) => modelLabel(row.dominant_model),
    header: ({ column }) => <DataTableColumnHeader column={column} label="Dominant Model" />,
  };
}

function sessionTokenColumn(key: keyof UsageBuckets, label: string): ColumnDef<ModelUsageSession> {
  return {
    id: key,
    accessorFn: (row) => row.usage[key] ?? 0,
    header: ({ column }) => <DataTableColumnHeader column={column} label={label} className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
  };
}

function SessionTable({ data, view }: { data: ModelUsagePayload; view: UsageView }) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const rows = React.useMemo(
    () => [...data.sessions].sort((left, right) => sortByLens(left, right, view)),
    [data.sessions, view],
  );
  const columns = React.useMemo(() => sessionColumns(view), [view]);
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">{view === "time" ? "Slowest Sessions" : "Sessions"}</CardTitle>
        <CardDescription>{sessionTableDescription(view)}</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={columns.length}
          emptyMessage="No sessions found for this scope."
          emptyHint="Try selecting a different project or model filter."
          showDensityToggle
          showExport
          exportFilename="model-usage-sessions"
          renderRowDetail={(session) => <SessionModelsDetail session={session} />}
        />
        <DataTablePagination table={table} />
      </CardContent>
    </Card>
  );
}

function turnColumns(view: "cost" | "tokens"): ColumnDef<ModelUsageTurn>[] {
  return [
    {
      accessorKey: "sequence",
      header: ({ column }) => <DataTableColumnHeader column={column} label="Turn" />,
      cell: ({ getValue }) => <span className="mono text-body-sm">#{getValue<number>()}</span>,
    },
    {
      id: "session",
      header: ({ column }) => <DataTableColumnHeader column={column} label="Session" />,
      cell: ({ row }) => (
        <SessionLink sessionId={row.original.session_id}>
          {row.original.session_title || shortSessionId(row.original.session_id)}
        </SessionLink>
      ),
    },
    {
      accessorKey: "model_key",
      header: ({ column }) => <DataTableColumnHeader column={column} label="Model" />,
    },
    ...(view === "tokens"
      ? [
          turnTokenColumn("prompt_tokens", "Prompt"),
          turnTokenColumn("cached_prompt_tokens", "Cached"),
          turnTokenColumn("completion_tokens", "Completion"),
          turnTokenColumn("reasoning_tokens", "Reasoning"),
        ]
      : [
          {
            id: "context",
            accessorFn: (row) => formatPercent(row.context?.final_used_percent),
            header: ({ column }) => <DataTableColumnHeader column={column} label="Context" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{getValue<string>()}</RightCell>,
          } satisfies ColumnDef<ModelUsageTurn>,
        ]),
    {
      id: "tokens",
      accessorFn: (row) => totalTokens(row.usage),
      header: ({ column }) => <DataTableColumnHeader column={column} label="Tokens" className="text-right" />,
      cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
    },
    ...(view === "tokens"
      ? []
      : [
          {
            accessorKey: "estimated_cost_usd",
            header: ({ column }) => <DataTableColumnHeader column={column} label="Cost" className="text-right" />,
            cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
          } satisfies ColumnDef<ModelUsageTurn>,
        ]),
  ];
}

function turnTokenColumn(key: keyof UsageBuckets, label: string): ColumnDef<ModelUsageTurn> {
  return {
    id: key,
    accessorFn: (row) => row.usage[key] ?? 0,
    header: ({ column }) => <DataTableColumnHeader column={column} label={label} className="text-right" />,
    cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
  };
}

function TurnTable({ data, view }: { data: ModelUsagePayload; view: "cost" | "tokens" }) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const rows = React.useMemo(
    () => [...data.turns].sort((left, right) => sortByLens(left, right, view)),
    [data.turns, view],
  );
  const columns = React.useMemo(() => turnColumns(view), [view]);
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 title-card">
          <BarChart3 size={18} /> {view === "tokens" ? "Largest Token Turns" : "Expensive Turns"}
        </CardTitle>
        <CardDescription>{view === "tokens" ? "Top turns by observed token volume." : "Top turns by estimated cost, capped to the first 200 rows from the backend."}</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={columns.length}
          emptyMessage="No turns found for this scope."
          emptyHint="Try selecting a different project or model filter."
          showColumnToggle
          showDensityToggle
          showExport
          exportFilename="model-usage-turns"
        />
        <DataTablePagination table={table} />
      </CardContent>
    </Card>
  );
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
  return usage.processed_tokens ?? 0;
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
  formatValue: (value: number) => string = formatCompactNumber,
) {
  return `avg ${formatValue(stats.avg)} / med ${formatValue(stats.median)} / p90 ${formatValue(stats.p90)} / p95 ${formatValue(stats.p95)}`;
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
  if (view === "cost") return "Cost is estimated in the datahub from observed core usage buckets.";
  return "Cost, volume, and pricing status across models in the selected scope.";
}

function sessionTableDescription(view: UsageView) {
  if (view === "time") return "Elapsed time is session-level; token throughput reflects the selected model filter.";
  if (view === "tokens") return "Sessions ranked by observed token volume for the selected scope.";
  return "Progressive drilldown from session cost to dominant model and context usage.";
}

