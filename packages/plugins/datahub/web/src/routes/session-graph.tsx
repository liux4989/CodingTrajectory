import * as React from "react";
import { useNavigate, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type { ApexOptions } from "apexcharts";
import {
  fetchSessionGraph,
  type GraphStatsSession,
  type GraphUsageSession,
  type SessionGraphPayload,
} from "@/api";
import { GraphTree } from "@/components/graph-tree";
import { MetricCard } from "@/components/metric-card";
import { StaggerGroup } from "@/components/stagger-group";
import { StateBlock } from "@/components/state-block";
import { LoadingState } from "@/components/loading-state";
import { SessionViewTabs } from "@/components/session-view-tabs";
import { shortSessionId } from "@/components/session-link";
import { DonutChart } from "@/components/charts";
import { ApexChart, escapeHtml, tooltipRow, useApexTheme } from "@/components/ui/apex-chart";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  formatCompactNumber,
  formatCostUsd,
  formatDuration,
  formatLabel,
  formatPercent,
  formatTokens,
} from "@/lib/format";
import { relativeTime } from "@/lib/relative-time";

function compositionRows(payload: SessionGraphPayload) {
  const statsBySession = new Map(
    (payload.stats.sessions ?? []).map((section) => [section.session_id, section]),
  );
  const usageBySession = new Map(
    (payload.usage.sessions ?? []).map((section) => [section.session_id, section]),
  );
  const ids: string[] = [];
  for (const node of payload.overview.sessions) {
    if (!ids.includes(node.session_id)) ids.push(node.session_id);
  }
  for (const id of [...statsBySession.keys(), ...usageBySession.keys()]) {
    if (!ids.includes(id)) ids.push(id);
  }
  // Single-session graphs carry no per-session sections; the top-level
  // stats/usage payloads already describe the root session.
  const isSingleSession = !payload.stats.sessions && !payload.usage.sessions;
  return ids.map((id) => {
    const fallbackStats: GraphStatsSession | undefined =
      isSingleSession && id === payload.root_session_id
        ? {
            session_id: id,
            role: "main",
            context_window: payload.stats.context_window,
            runtime: payload.stats.runtime,
            usage: payload.stats.usage,
          }
        : undefined;
    const fallbackUsage: GraphUsageSession | undefined =
      isSingleSession && id === payload.root_session_id
        ? {
            session_id: id,
            role: "main",
            total_usage: payload.usage.total_usage,
            estimated_cost: payload.usage.estimated_cost,
            runtime: payload.usage.runtime,
          }
        : undefined;
    return {
      id,
      node: payload.overview.sessions.find((node) => node.session_id === id),
      stats: statsBySession.get(id) ?? fallbackStats,
      usage: usageBySession.get(id) ?? fallbackUsage,
    };
  });
}

function cachedShare(section?: GraphStatsSession) {
  const prompt = section?.usage?.processed_tokens;
  const cached = section?.usage?.cached_prompt_tokens;
  if (!prompt || cached == null) return null;
  return cached / prompt;
}

export function SessionGraphRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId/graph" });
  const query = useQuery({
    queryKey: ["session-graph", sessionId],
    queryFn: () => fetchSessionGraph(sessionId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
  });

  if (query.isPending) {
    return (
      <div className="route-container w-full min-w-0 pb-8">
        <LoadingState
          title="Loading session graph"
          detail="Reading the retained graph projections for this session."
        />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="route-container w-full min-w-0 pb-8">
        <StateBlock
          title="Session graph failed"
          detail={query.error.message}
          onRetry={() => query.refetch()}
        />
      </div>
    );
  }

  const payload = query.data;
  const { overview, stats, usage } = payload;
  const orchestration = overview.graph?.orchestration ?? {};
  const summary = overview.summary ?? {};
  const rows = compositionRows(payload);
  const tokensBySession = new Map<string, number>();
  for (const section of usage.sessions ?? []) {
    const value = section.total_usage?.processed_tokens;
    if (value != null) tokensBySession.set(section.session_id, value);
  }
  if (!usage.sessions && usage.total_usage?.processed_tokens != null) {
    tokensBySession.set(payload.root_session_id, usage.total_usage.processed_tokens);
  }
  const totalUsage = usage.total_usage ?? {};
  const runtime = stats.runtime ?? {};
  const turnCount = summary.turn_count ?? runtime.turns ?? 0;
  const versions = orchestration.multi_agent_versions ?? [];
  const modes = orchestration.multi_agent_modes ?? [];

  return (
    <div className="route-container w-full min-w-0 overflow-hidden pb-8">
      <div className="grid gap-4">
        <div className="min-w-0">
          <h1 className="m-0 font-display text-h1 leading-tight">Agent graph</h1>
          <p className="m-0 mt-1 text-body-sm text-muted-foreground">
            {overview.project ?? "Unknown project"} · branch-local orchestration across{" "}
            {orchestration.session_count ?? overview.sessions.length} session(s)
          </p>
        </div>
        <SessionViewTabs sessionId={sessionId} active="graph" />

        <section className="stat-grid min-w-0">
          <StaggerGroup className="contents">
            <MetricCard
              label="Orchestration"
              value={formatLabel(orchestration.kind)}
              detail={(orchestration.vendors ?? summary.vendors ?? []).join(", ") || "No vendor evidence"}
            />
            <MetricCard
              label="Sessions"
              value={orchestration.session_count ?? overview.sessions.length}
              detail={`${turnCount.toLocaleString()} turn${turnCount === 1 ? "" : "s"} · started ${relativeTime(summary.started_at)}`}
            />
            <MetricCard
              label="Spawned agents"
              value={orchestration.spawned_agent_count ?? 0}
              detail={
                [...versions, ...modes].join(" · ") ||
                `${overview.edges.length} structural edge(s)`
              }
            />
            <MetricCard
              label="Tokens"
              value={formatCompactNumber(totalUsage.processed_tokens ?? stats.usage?.processed_tokens ?? 0)}
              detail={
                usage.estimated_cost
                  ? `${formatCostUsd(usage.estimated_cost.value_usd)} ${usage.estimated_cost.confidence} cost`
                  : "Cost unavailable"
              }
            />
          </StaggerGroup>
        </section>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Agent hierarchy</CardTitle>
            <CardDescription>
              Only the selected conversation branch and agents spawned from it.
              Ordinary human forks are available in Conversation tree.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <GraphTree
              nodes={overview.sessions}
              edges={overview.edges}
              tokensBySession={tokensBySession}
              activeSessionId={sessionId}
            />
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Session Composition</CardTitle>
            <CardDescription>
              Per-session processed tokens, split into cached and fresh portions. Select a bar to open the session.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <SessionCompositionChart rows={rows} rootId={payload.root_session_id} />
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Graph Usage</CardTitle>
            <CardDescription>
              Aggregate turn-level token usage, as reported by `ct session graph usage`
              {runtime.execution_seconds != null ? ` · ${formatDuration(runtime.execution_seconds)} execution` : ""}.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-6 lg:grid-cols-2">
              <div className="min-w-0">
                <p className="m-0 mb-2 heading-section">Bucket mix</p>
                {bucketMix(totalUsage).some((bucket) => bucket.value > 0) ? (
                  <DonutChart
                    data={bucketMix(totalUsage)}
                    ariaLabel="Graph token bucket mix"
                    centerLabel={formatTokens(totalUsage.processed_tokens)}
                    centerSubLabel="Processed"
                    formatValue={formatTokens}
                    height={240}
                  />
                ) : (
                  <p className="text-muted-foreground">No token usage reported for this graph.</p>
                )}
              </div>
              {(usage.models ?? []).length > 0 ? (
                <div className="min-w-0">
                  <p className="m-0 mb-2 heading-section">By model</p>
                  <GraphModelChart models={usage.models ?? []} />
                </div>
              ) : null}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

type CompositionRow = ReturnType<typeof compositionRows>[number];

type GraphModel = NonNullable<SessionGraphPayload["usage"]["models"]>[number];

function bucketMix(totalUsage: SessionGraphPayload["usage"]["total_usage"]) {
  const buckets = totalUsage ?? {};
  return [
    { label: "Prompt", value: Math.max((buckets.prompt_tokens ?? 0) - (buckets.cached_prompt_tokens ?? 0), 0) },
    { label: "Cached", value: buckets.cached_prompt_tokens ?? 0 },
    { label: "Completion", value: buckets.completion_tokens ?? 0 },
    { label: "Reasoning", value: buckets.reasoning_tokens ?? 0 },
  ];
}

/**
 * Per-session processed tokens, stacked into cached and fresh portions.
 * Context usage, cached share, turns, and cost from the former table move
 * into the tooltip; clicking a bar opens the session detail.
 */
function SessionCompositionChart({ rows, rootId }: { rows: CompositionRow[]; rootId: string }) {
  const theme = useApexTheme();
  const navigate = useNavigate();

  const rowLabel = React.useCallback(
    (row: CompositionRow) => row.node?.title || row.node?.agent_name || row.usage?.title || shortSessionId(row.id),
    [],
  );

  const options = React.useMemo<ApexOptions>(
    () => ({
      chart: {
        stacked: true,
        events: {
          dataPointSelection: (_event, _chartContext, config) => {
            const row = config ? rows[config.dataPointIndex] : undefined;
            if (row) {
              void navigate({ to: "/sessions/$sessionId", params: { sessionId: row.id } });
            }
          },
        },
      },
      plotOptions: { bar: { horizontal: true, barHeight: "58%", borderRadius: 3 } },
      dataLabels: { enabled: false },
      xaxis: {
        categories: rows.map((row) => {
          const label = rowLabel(row);
          return label.length > 24 ? `${label.slice(0, 23)}…` : label;
        }),
        labels: { formatter: (value) => formatCompactNumber(Number(value)) },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: { labels: { style: { fontSize: "11px" }, maxWidth: 220 } },
      legend: { show: true, position: "bottom", horizontalAlign: "left" },
      tooltip: {
        custom: ({ dataPointIndex }) => {
          const row = rows[dataPointIndex];
          if (!row) return "";
          const role = formatLabel(row.stats?.role ?? row.usage?.role ?? (row.id === rootId ? "main" : undefined));
          const context = row.stats?.context_window?.used_tokens != null
            ? `${formatTokens(row.stats.context_window.used_tokens)}${row.stats.context_window.used_percent != null ? ` (${formatPercent(row.stats.context_window.used_percent)})` : ""}`
            : "-";
          const processed = row.usage?.total_usage?.processed_tokens ?? row.stats?.usage?.processed_tokens;
          const share = cachedShare(row.stats);
          const tooltipRows = [
            tooltipRow("Role", escapeHtml(role), theme.axis),
            tooltipRow("Context used", context, theme.axis),
            tooltipRow("Processed", formatTokens(processed), theme.axis),
            tooltipRow("Cached share", share != null ? formatPercent(share * 100) : "-", theme.axis),
            tooltipRow("Turns", String(row.stats?.runtime?.turns ?? row.usage?.runtime?.turns ?? "-"), theme.axis),
            tooltipRow("Est. cost", formatCostUsd(row.usage?.estimated_cost?.value_usd), theme.axis),
          ].join("");
          return `<div style="padding:10px 12px;min-width:220px"><div style="font-weight:700;margin-bottom:6px">${escapeHtml(rowLabel(row))}</div>${tooltipRows}</div>`;
        },
      },
    }),
    [rows, rootId, theme, navigate, rowLabel],
  );

  if (!rows.length) {
    return <p className="text-muted-foreground">No session composition data for this graph.</p>;
  }

  return (
    <>
      <ApexChart
        type="bar"
        series={[
          { name: "Cached", data: rows.map((row) => row.stats?.usage?.cached_prompt_tokens ?? 0) },
          {
            name: "Fresh prompt + output",
            data: rows.map((row) =>
              Math.max(
                (row.usage?.total_usage?.processed_tokens ?? row.stats?.usage?.processed_tokens ?? 0) -
                  (row.stats?.usage?.cached_prompt_tokens ?? 0),
                0,
              ),
            ),
          },
        ]}
        options={options}
        height={Math.max(180, rows.length * 48)}
        ariaLabel="Session composition: processed tokens per session split by cached share"
      />
      <ul className="sr-only">
        {rows.map((row) => (
          <li key={row.id}>
            {rowLabel(row)}: {formatTokens(row.usage?.total_usage?.processed_tokens ?? row.stats?.usage?.processed_tokens)} processed,{" "}
            {cachedShare(row.stats) != null ? formatPercent((cachedShare(row.stats) ?? 0) * 100) : "unknown"} cached
          </li>
        ))}
      </ul>
    </>
  );
}

/** Processed tokens per model in this graph, replacing the model table. */
function GraphModelChart({ models }: { models: GraphModel[] }) {
  const theme = useApexTheme();

  const options = React.useMemo<ApexOptions>(
    () => ({
      colors: [theme.palette[0]],
      plotOptions: { bar: { horizontal: true, barHeight: "58%", borderRadius: 4 } },
      dataLabels: { enabled: false },
      xaxis: {
        categories: models.map((model) => `${model.model ?? "Unknown model"}${model.provider ? ` · ${model.provider}` : ""}`),
        labels: { formatter: (value) => formatCompactNumber(Number(value)) },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: { labels: { style: { fontSize: "11px" }, maxWidth: 220 } },
      legend: { show: false },
      tooltip: {
        custom: ({ dataPointIndex }) => {
          const model = models[dataPointIndex];
          if (!model) return "";
          const rows = [
            tooltipRow("Turns", String(model.turns ?? "-"), theme.axis),
            tooltipRow("Processed", formatTokens(model.usage?.processed_tokens), theme.axis),
          ].join("");
          return `<div style="padding:10px 12px;min-width:200px"><div style="font-weight:700;margin-bottom:6px">${escapeHtml(model.model ?? "Unknown model")}</div>${rows}</div>`;
        },
      },
    }),
    [models, theme],
  );

  return (
    <ApexChart
      type="bar"
      series={[{ name: "Processed", data: models.map((model) => model.usage?.processed_tokens ?? 0) }]}
      options={options}
      height={Math.max(160, models.length * 44)}
      ariaLabel="Processed tokens per model in this graph"
    />
  );
}
