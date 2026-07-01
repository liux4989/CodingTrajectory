import * as React from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { BarChart3 } from "lucide-react";
import {
  fetchModelUsage,
  type ModelUsageModel,
  type ModelUsagePayload,
  type ModelUsageSession,
  type ModelUsageTurn,
  type UsageBuckets,
} from "@/api";
import { MetricCard } from "@/components/metric-card";
import { RefreshButton } from "@/components/refresh-button";
import { RouteHeader } from "@/components/route-header";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { StateBlock } from "@/components/state-block";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTable } from "@/components/data-table";
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

const modelColumns: ColumnDef<ModelUsageModel>[] = [
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
    id: "tokens",
    accessorFn: (row) => totalTokens(row.usage),
    header: () => <HeaderLabel align="right">Tokens</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "estimated_cost_usd",
    header: () => <HeaderLabel align="right">Total Cost</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "avg_session_cost_usd",
    header: () => <HeaderLabel align="right">Avg Session</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
  },
  {
    accessorKey: "avg_turn_cost_usd",
    header: () => <HeaderLabel align="right">Avg Turn</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
  },
  {
    id: "pricing",
    accessorFn: (row) => row.pricing.confidence,
    header: () => <HeaderLabel>Pricing</HeaderLabel>,
    cell: ({ getValue }) => {
      const confidence = getValue<string>();
      return (
        <Badge variant={confidence === "estimated" ? "default" : "secondary"}>
          {confidence === "estimated" ? "estimated" : "missing price"}
        </Badge>
      );
    },
  },
];

function ModelTable({ data }: { data: ModelUsagePayload }) {
  const table = useReactTable({
    data: data.models,
    columns: modelColumns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="font-display text-xl tracking-tight">Model Mix</CardTitle>
        <CardDescription>Cost is estimated in the dashboard from observed core usage buckets.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={modelColumns.length}
          emptyMessage="No model usage found for this scope."
        />
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

const sessionColumns: ColumnDef<ModelUsageSession>[] = [
  {
    id: "session",
    header: () => <HeaderLabel>Session</HeaderLabel>,
    cell: ({ row }) => (
      <div className="max-w-[24rem]">
        <SessionLink sessionId={row.original.id}>
          {row.original.title || shortSessionId(row.original.id)}
        </SessionLink>
      </div>
    ),
  },
  {
    id: "project",
    accessorFn: (row) => row.project ?? "unknown",
    header: () => <HeaderLabel>Project</HeaderLabel>,
  },
  {
    id: "dominant_model",
    accessorFn: (row) => modelLabel(row.dominant_model),
    header: () => <HeaderLabel>Dominant Model</HeaderLabel>,
  },
  {
    id: "context",
    accessorFn: (row) => formatPercent(row.context?.max_used_percent),
    header: () => <HeaderLabel align="right">Context</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{getValue<string>()}</RightCell>,
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
];

function SessionTable({ data }: { data: ModelUsagePayload }) {
  const rows = React.useMemo(() => data.sessions.slice(0, 50), [data.sessions]);
  const table = useReactTable({
    data: rows,
    columns: sessionColumns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="font-display text-xl tracking-tight">Sessions</CardTitle>
        <CardDescription>Progressive drilldown from session cost to dominant model and context usage.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={sessionColumns.length}
          emptyMessage="No sessions found for this scope."
        />
      </CardContent>
    </Card>
  );
}

const turnColumns: ColumnDef<ModelUsageTurn>[] = [
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
  {
    id: "tokens",
    accessorFn: (row) => totalTokens(row.usage),
    header: () => <HeaderLabel align="right">Tokens</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{compactNumber(getValue<number>())}</RightCell>,
  },
  {
    id: "context",
    accessorFn: (row) => formatPercent(row.context?.final_used_percent),
    header: () => <HeaderLabel align="right">Context</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{getValue<string>()}</RightCell>,
  },
  {
    accessorKey: "estimated_cost_usd",
    header: () => <HeaderLabel align="right">Cost</HeaderLabel>,
    cell: ({ getValue }) => <RightCell>{formatCost(getValue<number>())}</RightCell>,
  },
];

function TurnTable({ data }: { data: ModelUsagePayload }) {
  const rows = React.useMemo(() => data.turns.slice(0, 30), [data.turns]);
  const table = useReactTable({
    data: rows,
    columns: turnColumns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 font-display text-xl tracking-tight">
          <BarChart3 size={18} /> Expensive Turns
        </CardTitle>
        <CardDescription>Top turns by estimated cost, capped to the first 200 rows from the backend.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={turnColumns.length}
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

function modelLabel(value: ModelUsageSession["dominant_model"]) {
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
