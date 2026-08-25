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
import { ApexChart, escapeHtml, tooltipRow, useApexTheme } from "@/components/ui/apex-chart";
import type { ApexOptions } from "apexcharts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
import {
  ToggleGroup,
  ToggleGroupItem,
} from "@/components/ui/toggle-group";
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
  const search = useSearch({ from: "/compare" });
  const navigate = useNavigate({ from: "/compare" });
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
      <ModelMixChart data={data} />
      <SessionScatterChart data={data} />
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
              <ModelTable data={data} />
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
          <TokenMixChart data={data} />
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
              <ModelCompositionChart data={data} />
              <ModelDistributionChart data={data} />
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
        <MetricCard label="Active Execution" value={formatDuration(data.summary.total_execution_seconds)} detail={`${data.summary.runtime_eligible}/${data.summary.sessions} sessions with runtime telemetry`} />
        <MetricCard label="Waiting" value={formatDuration(data.summary.total_wait_seconds)} detail="Observed non-execution time" />
        <MetricCard label="Elapsed Time" value={formatDuration(data.summary.total_elapsed_seconds)} detail={distributionDetail(sessionStats, formatDuration)} />
        <MetricCard label="Throughput" value={formatCompactNumber(tokensPerMinute(data.summary.processed_tokens, data.summary.total_execution_seconds))} detail="tokens/min across active execution" />
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

// Composition buckets only: processed_tokens is the sum shown as the donut
// center total, not a slice.
const TOKEN_MIX_BUCKETS = TOKEN_BUCKET_DEFS.filter(({ key }) => key !== "processed_tokens");

/**
 * Token bucket composition donut replacing the five-card bucket bento. The
 * center shows total processed tokens; slice tooltips carry the session/turn
 * distribution stats the cards used to show.
 */
function TokenMixChart({ data }: { data: ModelUsagePayload }) {
  const theme = useApexTheme();
  const buckets = React.useMemo(
    () =>
      TOKEN_MIX_BUCKETS.map(({ key, label }) => ({
        key,
        label,
        value: sum(data.sessions.map((session) => usageValue(session.usage, key))),
        stats: data.summary.token_stats.buckets[key],
      })),
    [data.sessions, data.summary.token_stats.buckets],
  );

  const options = React.useMemo<ApexOptions>(
    () => ({
      labels: buckets.map((bucket) => bucket.label),
      stroke: { width: 2, colors: [theme.card] },
      dataLabels: { enabled: false },
      legend: { show: true, position: "right", fontSize: "12px" },
      plotOptions: {
        pie: {
          donut: {
            size: "66%",
            labels: {
              show: true,
              name: { show: true, color: theme.axis, fontSize: "0.7rem" },
              value: {
                show: true,
                fontSize: "1.5rem",
                fontFamily: theme.bodyFont,
                fontWeight: 800,
                color: theme.foreground,
                formatter: () => formatCompactNumber(data.summary.processed_tokens),
              },
              total: {
                show: true,
                showAlways: true,
                label: "Processed tokens",
                fontSize: "0.7rem",
                color: theme.axis,
                formatter: () => formatCompactNumber(data.summary.processed_tokens),
              },
            },
          },
        },
      },
      tooltip: {
        custom: ({ seriesIndex }) => {
          const bucket = buckets[seriesIndex];
          if (!bucket) return "";
          const rows = [
            tooltipRow("Total", formatCompactNumber(bucket.value), theme.axis),
            tooltipRow("Session", distributionDetail(bucket.stats.session), theme.axis),
            tooltipRow("Turn", distributionDetail(bucket.stats.turn), theme.axis),
          ].join("");
          return `<div style="padding:10px 12px;min-width:220px"><div style="font-weight:700;margin-bottom:6px">${bucket.label} tokens</div>${rows}</div>`;
        },
      },
    }),
    [buckets, theme, data.summary.processed_tokens],
  );

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Token Bucket Mix</CardTitle>
        <CardDescription>Composition of observed token buckets across the selected scope.</CardDescription>
      </CardHeader>
      <CardContent>
        <ApexChart
          type="donut"
          series={buckets.map((bucket) => bucket.value)}
          options={options}
          height={260}
          ariaLabel="Token bucket composition"
        />
      </CardContent>
    </Card>
  );
}

/**
 * Treemap of model mix by processed tokens. Tile size encodes token volume;
 * clicking a tile applies the model filter. Tooltips carry the volume, time,
 * and cost context the former table showed.
 */
function ModelMixChart({ data }: { data: ModelUsagePayload }) {
  const theme = useApexTheme();
  const navigate = useNavigate({ from: "/compare" });
  const models = React.useMemo(
    () =>
      [...data.models]
        .filter((model) => totalTokens(model.usage) > 0)
        .sort((left, right) => totalTokens(right.usage) - totalTokens(left.usage))
        .slice(0, 12),
    [data.models],
  );

  const options = React.useMemo<ApexOptions>(
    () => ({
      chart: {
        events: {
          dataPointSelection: (_event, _chartContext, config) => {
            const model = config ? models[config.dataPointIndex] : undefined;
            if (model) {
              void navigate({
                search: (current) => ({ ...current, modelKey: model.model_key }),
              });
            }
          },
        },
      },
      legend: { show: false },
      plotOptions: { treemap: { distributed: true, enableShades: false, borderRadius: 4 } },
      dataLabels: {
        enabled: true,
        style: { fontSize: "12px", fontFamily: theme.bodyFont },
        formatter: (text, op) => [String(text), formatCompactNumber(Number(op?.value ?? 0))],
      },
      tooltip: {
        custom: ({ dataPointIndex }) => {
          const model = models[dataPointIndex];
          if (!model) return "";
          const rows = [
            tooltipRow("Sessions", model.sessions.toLocaleString(), theme.axis),
            tooltipRow("Turns", model.turns.toLocaleString(), theme.axis),
            tooltipRow("Avg turn tokens", formatCompactNumber(average(totalTokens(model.usage), model.turns)), theme.axis),
            tooltipRow("Avg turn time", formatDuration(model.avg_turn_elapsed_seconds), theme.axis),
            tooltipRow("Tokens", formatCompactNumber(totalTokens(model.usage)), theme.axis),
            tooltipRow("Cost", formatCostUsd(model.estimated_cost_usd), theme.axis),
            tooltipRow("Pricing", model.pricing.confidence === "estimated" ? "estimated" : "missing price", theme.axis),
          ].join("");
          return `<div style="padding:10px 12px;min-width:230px"><div style="font-weight:700;margin-bottom:6px">${escapeHtml(model.model_key)}</div>${rows}</div>`;
        },
      },
    }),
    [models, theme, navigate],
  );

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Model Mix Overview</CardTitle>
        <CardDescription>Model share of processed tokens. Select a tile to filter by that model.</CardDescription>
      </CardHeader>
      <CardContent>
        {models.length ? (
          <>
            <ApexChart
              type="treemap"
              series={[{ name: "Tokens", data: models.map((model) => ({ x: model.model_key, y: totalTokens(model.usage) })) }]}
              options={options}
              height={320}
              ariaLabel="Model mix treemap by processed tokens"
            />
            <ul className="sr-only">
              {models.map((model) => (
                <li key={model.model_key}>
                  {model.model_key}: {formatCompactNumber(totalTokens(model.usage))} tokens, {model.sessions.toLocaleString()} sessions, {formatCostUsd(model.estimated_cost_usd)}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="py-8 text-center text-muted-foreground">No model usage found for this scope. Try selecting a different project or model filter.</p>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Sessions scattered by elapsed time vs processed tokens, colored by dominant
 * model. Clicking a point opens the session detail.
 */
function SessionScatterChart({ data }: { data: ModelUsagePayload }) {
  const theme = useApexTheme();
  const navigate = useNavigate();
  const groups = React.useMemo(() => {
    const byModel = new Map<string, ModelUsageSession[]>();
    for (const session of data.sessions) {
      const label = modelLabel(session.dominant_model);
      const entry = byModel.get(label) ?? [];
      entry.push(session);
      byModel.set(label, entry);
    }
    const ranked = [...byModel.entries()].sort(
      (left, right) =>
        sum(right[1].map((session) => totalTokens(session.usage))) -
        sum(left[1].map((session) => totalTokens(session.usage))),
    );
    const top = ranked.slice(0, TOP_SCATTER_GROUPS);
    const rest = ranked.slice(TOP_SCATTER_GROUPS).flatMap(([, sessions]) => sessions);
    return rest.length ? [...top, ["Other", rest] as [string, ModelUsageSession[]]] : top;
  }, [data.sessions]);

  const totalSessions = data.sessions.length;

  const options = React.useMemo<ApexOptions>(
    () => ({
      chart: {
        events: {
          dataPointSelection: (_event, _chartContext, config) => {
            const session = config ? groups[config.seriesIndex ?? 0]?.[1][config.dataPointIndex ?? 0] : undefined;
            if (session) {
              void navigate({
                to: "/sessions/$sessionId",
                params: { sessionId: session.id },
                search: { view: "context" },
              });
            }
          },
        },
      },
      markers: { size: 5 },
      xaxis: {
        type: "numeric",
        tickAmount: 8,
        title: { text: "Elapsed time", style: { fontSize: "11px" } },
        labels: { formatter: (value) => formatDuration(Number(value)) },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: {
        title: { text: "Tokens", style: { fontSize: "11px" } },
        labels: { formatter: (value) => formatCompactNumber(Number(value)) },
      },
      legend: { show: true, position: "bottom", horizontalAlign: "left" },
      tooltip: {
        custom: ({ seriesIndex, dataPointIndex }) => {
          const session = groups[seriesIndex]?.[1][dataPointIndex];
          if (!session) return "";
          const rows = [
            tooltipRow("Project", escapeHtml(session.project ?? "Unknown"), theme.axis),
            tooltipRow("Elapsed", formatDuration(session.elapsed_seconds), theme.axis),
            tooltipRow("Tokens", formatCompactNumber(totalTokens(session.usage)), theme.axis),
            tooltipRow("Cost", formatCostUsd(session.estimated_cost_usd), theme.axis),
            tooltipRow("Context", formatPercent(session.context?.max_used_percent), theme.axis),
          ].join("");
          return `<div style="padding:10px 12px;min-width:220px"><div style="font-weight:700;margin-bottom:6px">${escapeHtml(session.title || shortSessionId(session.id))}</div>${rows}</div>`;
        },
      },
    }),
    [groups, theme, navigate],
  );

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Session Overview</CardTitle>
        <CardDescription>Elapsed time versus token volume per session, colored by dominant model. Select a point to open the session.</CardDescription>
      </CardHeader>
      <CardContent>
        {totalSessions ? (
          <>
            <ApexChart
              type="scatter"
              series={groups.map(([label, sessions]) => ({
                name: label,
                data: sessions.map((session) => ({ x: session.elapsed_seconds, y: totalTokens(session.usage) })),
              }))}
              options={options}
              height={340}
              ariaLabel="Session elapsed time versus tokens by dominant model"
            />
            <ul className="sr-only">
              {data.sessions.map((session) => (
                <li key={session.id}>
                  {session.title || shortSessionId(session.id)}: {formatDuration(session.elapsed_seconds)}, {formatCompactNumber(totalTokens(session.usage))} tokens, {formatCostUsd(session.estimated_cost_usd)}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="py-8 text-center text-muted-foreground">No sessions found for this scope. Try selecting a different project or model filter.</p>
        )}
      </CardContent>
    </Card>
  );
}

const TOP_SCATTER_GROUPS = 6;

/**
 * Per-model token bucket composition (horizontal stacked bar). Replaces the
 * Prompt/Cached/Completion/Reasoning columns of the former Model Mix by
 * Tokens table; the tooltip carries absolute values and shares.
 */
function ModelCompositionChart({ data }: { data: ModelUsagePayload }) {
  const theme = useApexTheme();
  const models = React.useMemo(
    () => [...data.models].sort((left, right) => totalTokens(right.usage) - totalTokens(left.usage)),
    [data.models],
  );

  const options = React.useMemo<ApexOptions>(
    () => ({
      chart: { stacked: true },
      plotOptions: { bar: { horizontal: true, barHeight: "58%", borderRadius: 3 } },
      dataLabels: { enabled: false },
      xaxis: {
        categories: models.map((model) => model.model_key),
        labels: { formatter: (value) => formatCompactNumber(Number(value)) },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: { labels: { style: { fontSize: "11px" }, maxWidth: 220 } },
      legend: { show: true, position: "bottom", horizontalAlign: "left" },
      tooltip: {
        custom: ({ dataPointIndex }) => {
          const model = models[dataPointIndex];
          if (!model) return "";
          const total = totalTokens(model.usage) || 1;
          const bucketRows = TOKEN_MIX_BUCKETS.map(({ key, label }) => {
            const value = usageValue(model.usage, key);
            return tooltipRow(label, `${formatCompactNumber(value)} (${Math.round((value / total) * 100)}%)`, theme.axis);
          }).join("");
          const rows = bucketRows + tooltipRow("Processed", formatCompactNumber(totalTokens(model.usage)), theme.axis);
          return `<div style="padding:10px 12px;min-width:230px"><div style="font-weight:700;margin-bottom:6px">${escapeHtml(model.model_key)}</div>${rows}</div>`;
        },
      },
    }),
    [models, theme],
  );

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Model Mix by Tokens</CardTitle>
        <CardDescription>Token bucket composition per model across the selected scope.</CardDescription>
      </CardHeader>
      <CardContent>
        {models.length ? (
          <>
            <ApexChart
              type="bar"
              series={TOKEN_MIX_BUCKETS.map(({ key, label }) => ({
                name: label,
                data: models.map((model) => usageValue(model.usage, key)),
              }))}
              options={options}
              height={Math.max(220, models.length * 64)}
              ariaLabel="Token bucket composition by model"
            />
            <ul className="sr-only">
              {models.map((model) => (
                <li key={model.model_key}>
                  {model.model_key}: {TOKEN_MIX_BUCKETS.map(({ key, label }) => `${label} ${formatCompactNumber(usageValue(model.usage, key))}`).join(", ")}, processed {formatCompactNumber(totalTokens(model.usage))}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="py-8 text-center text-muted-foreground">No model usage found for this scope. Try selecting a different project or model filter.</p>
        )}
      </CardContent>
    </Card>
  );
}

const DISTRIBUTION_SERIES = [
  { key: "avg", label: "Average" },
  { key: "median", label: "Median" },
  { key: "p90", label: "P90" },
  { key: "p95", label: "P95" },
] as const;

/**
 * Per-model token distribution (Avg / Median / P90 / P95 as grouped bars)
 * with a session/turn unit toggle. Replaces the sixteen statistic columns of
 * the former Model Mix by Tokens table.
 */
function ModelDistributionChart({ data }: { data: ModelUsagePayload }) {
  const [unit, setUnit] = React.useState<"session" | "turn">("session");
  const models = React.useMemo(
    () => [...data.models].sort((left, right) => totalTokens(right.usage) - totalTokens(left.usage)),
    [data.models],
  );

  const options = React.useMemo<ApexOptions>(
    () => ({
      plotOptions: { bar: { columnWidth: "62%", borderRadius: 3 } },
      dataLabels: { enabled: false },
      xaxis: {
        categories: models.map((model) => model.model_key),
        labels: { style: { fontSize: "11px" }, rotate: -20, hideOverlappingLabels: false, trim: true, maxHeight: 80 },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: { labels: { formatter: (value) => formatCompactNumber(Number(value)) } },
      legend: { show: true, position: "bottom", horizontalAlign: "left" },
      tooltip: {
        shared: true,
        intersect: false,
        y: { formatter: (value) => (value == null ? "—" : formatCompactNumber(Number(value))) },
      },
    }),
    [models],
  );

  return (
    <Card className="@container/card min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Token Distribution by Model</CardTitle>
        <CardDescription>Average, median, P90, and P95 tokens per {unit}, grouped by model.</CardDescription>
        <CardAction>
          <ToggleGroup
            type="single"
            value={unit}
            onValueChange={(value) => {
              if (value === "session" || value === "turn") setUnit(value);
            }}
            variant="outline"
            className="hidden *:data-[slot=toggle-group-item]:px-3! @[480px]/card:flex"
          >
            <ToggleGroupItem value="session">Session</ToggleGroupItem>
            <ToggleGroupItem value="turn">Turn</ToggleGroupItem>
          </ToggleGroup>
          <Select
            value={unit}
            onValueChange={(value) => setUnit(value as "session" | "turn")}
          >
            <SelectTrigger className="flex w-28 @[480px]/card:hidden" size="sm" aria-label="Select a distribution unit">
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="rounded-xl">
              <SelectItem value="session" className="rounded-lg">Session</SelectItem>
              <SelectItem value="turn" className="rounded-lg">Turn</SelectItem>
            </SelectContent>
          </Select>
        </CardAction>
      </CardHeader>
      <CardContent>
        {models.length ? (
          <>
            <ApexChart
              type="bar"
              series={DISTRIBUTION_SERIES.map(({ key, label }) => ({
                name: label,
                data: models.map((model) => model.token_stats[unit][key]),
              }))}
              options={options}
              height={300}
              ariaLabel={`Token distribution by model per ${unit}`}
            />
            <ul className="sr-only">
              {models.map((model) => {
                const stats = model.token_stats[unit];
                return (
                  <li key={model.model_key}>
                    {model.model_key} per {unit}: avg {formatCompactNumber(stats.avg)}, median {formatCompactNumber(stats.median)}, p90 {formatCompactNumber(stats.p90)}, p95 {formatCompactNumber(stats.p95)}
                  </li>
                );
              })}
            </ul>
          </>
        ) : (
          <p className="py-8 text-center text-muted-foreground">No model usage found for this scope. Try selecting a different project or model filter.</p>
        )}
      </CardContent>
    </Card>
  );
}

function modelColumns(): ColumnDef<ModelUsageModel>[] {
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
    {
      id: "tokens",
      accessorFn: (row) => totalTokens(row.usage),
      header: ({ column }) => <DataTableColumnHeader column={column} label="Tokens" className="text-right" />,
      cell: ({ getValue }) => <RightCell>{formatCompactNumber(getValue<number>())}</RightCell>,
    },
    {
      accessorKey: "estimated_cost_usd",
      header: ({ column }) => <DataTableColumnHeader column={column} label="Total Cost" className="text-right" />,
      cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
    },
    {
      accessorKey: "avg_session_cost_usd",
      header: ({ column }) => <DataTableColumnHeader column={column} label="Avg Session Cost" className="text-right" />,
      cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
    },
    costStatColumn("median_session_cost_usd", "Median Session Cost", (row) => row.cost_stats.session.median),
    costStatColumn("p90_session_cost_usd", "P90 Session Cost", (row) => row.cost_stats.session.p90),
    costStatColumn("p95_session_cost_usd", "P95 Session Cost", (row) => row.cost_stats.session.p95),
    {
      accessorKey: "avg_turn_cost_usd",
      header: ({ column }) => <DataTableColumnHeader column={column} label="Avg Turn Cost" className="text-right" />,
      cell: ({ getValue }) => <RightCell>{formatCostUsd(getValue<number>())}</RightCell>,
    },
    costStatColumn("median_turn_cost_usd", "Median Turn Cost", (row) => row.cost_stats.turn.median),
    costStatColumn("p90_turn_cost_usd", "P90 Turn Cost", (row) => row.cost_stats.turn.p90),
    costStatColumn("p95_turn_cost_usd", "P95 Turn Cost", (row) => row.cost_stats.turn.p95),
    {
      id: "pricing",
      accessorFn: (row) => row.pricing.confidence,
      header: ({ column }) => <DataTableColumnHeader column={column} label="Pricing" />,
      cell: ({ getValue }) => <PricingBadge confidence={getValue<string>()} />,
    },
  ];
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

function ModelTable({ data }: { data: ModelUsagePayload }) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const rows = React.useMemo(
    () => [...data.models].sort((left, right) => sortByLens(left, right, "cost")),
    [data.models],
  );
  const columns = React.useMemo(() => modelColumns(), []);
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
        <CardTitle className="title-card">Model Mix by Cost</CardTitle>
        <CardDescription>Cost is estimated in Datahub from observed core usage buckets.</CardDescription>
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
        <CardDescription>Model elapsed time is descriptive only; mixed-model active runtime remains attributed to its session below.</CardDescription>
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
            accessorKey: "execution_seconds",
            header: ({ column }) => <DataTableColumnHeader column={column} label="Active" className="text-right" />,
            cell: ({ row, getValue }) => <RightCell>{row.original.runtime_available ? formatDuration(getValue<number>()) : "Unavailable"}</RightCell>,
          } satisfies ColumnDef<ModelUsageSession>,
          {
            accessorKey: "wait_seconds",
            header: ({ column }) => <DataTableColumnHeader column={column} label="Wait" className="text-right" />,
            cell: ({ row, getValue }) => <RightCell>{row.original.runtime_available ? formatDuration(getValue<number>()) : "Unavailable"}</RightCell>,
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
    cell: ({ row, getValue }) => (
      <span className="flex flex-wrap items-center gap-1">
        <span>{getValue<string>()}</span>
        {row.original.mixed_models ? <Badge variant="outline">Mixed runtime</Badge> : null}
      </span>
    ),
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


function sessionTableDescription(view: UsageView) {
  if (view === "time") return "Elapsed time is session-level; token throughput reflects the selected model filter.";
  if (view === "tokens") return "Sessions ranked by observed token volume for the selected scope.";
  return "Progressive drilldown from session cost to dominant model and context usage.";
}
