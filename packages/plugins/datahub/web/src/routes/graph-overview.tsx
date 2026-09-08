import * as React from "react";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type { ApexOptions } from "apexcharts";
import { GitBranch } from "lucide-react";
import {
  fetchSessionGraph,
  fetchSessionTree,
  type ConversationBranch,
  type GraphSessionNode,
  type GraphStatsSession,
  type GraphUsageSession,
  type SessionGraphPayload,
} from "@/api";
import { GraphTree } from "@/components/graph-tree";
import { MetricCard } from "@/components/metric-card";
import { PageHeader } from "@/components/route-header";
import { StaggerGroup } from "@/components/stagger-group";
import { StateBlock } from "@/components/state-block";
import { LoadingState } from "@/components/loading-state";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { DonutChart } from "@/components/charts";
import { ApexChart, escapeHtml, tooltipRow, useApexTheme } from "@/components/ui/apex-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { cn } from "@/lib/utils";

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

/** The selected conversation branch plus every agent session spawned under it. */
function branchScopeIds(
  nodes: GraphSessionNode[],
  branchId: string | undefined,
): ReadonlySet<string> | null {
  if (!branchId) return null;
  const childrenByParent = new Map<string, string[]>();
  for (const node of nodes) {
    if (!node.parent_session_id) continue;
    const list = childrenByParent.get(node.parent_session_id);
    if (list) list.push(node.session_id);
    else childrenByParent.set(node.parent_session_id, [node.session_id]);
  }
  const scope = new Set<string>([branchId]);
  const queue = [branchId];
  while (queue.length) {
    const current = queue.pop()!;
    for (const child of childrenByParent.get(current) ?? []) {
      if (scope.has(child)) continue;
      scope.add(child);
      queue.push(child);
    }
  }
  return scope;
}

export function GraphOverviewRoute() {
  const { rootId } = useParams({ from: "/graphs/$rootId" });
  const { branch } = useSearch({ from: "/graphs/$rootId" });
  const query = useQuery({
    queryKey: ["session-graph", rootId],
    queryFn: () => fetchSessionGraph(rootId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
  });
  const treeQuery = useQuery({
    queryKey: ["session-tree", rootId],
    queryFn: () => fetchSessionTree(rootId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
  });

  if (query.isPending) {
    return (
      <div className="route-container-wide w-full min-w-0 pb-8">
        <LoadingState
          title="Loading session graph"
          detail="Reading the retained graph projections for this session family."
        />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="route-container-wide w-full min-w-0 pb-8">
        <StateBlock
          title="Session graph failed"
          detail={query.error.message}
          onRetry={() => query.refetch()}
        />
      </div>
    );
  }

  const payload = query.data;
  // Canonicalize on the graph identity: the root session id.
  if (payload.root_session_id && payload.root_session_id !== rootId) {
    return <GraphRedirect rootId={payload.root_session_id} branch={branch} />;
  }

  const { overview, stats, usage } = payload;
  const orchestration = overview.graph?.orchestration ?? {};
  const summary = overview.summary ?? {};
  const scope = branchScopeIds(overview.sessions, branch);
  const scopedNodes = scope
    ? overview.sessions.filter((node) => scope.has(node.session_id))
    : overview.sessions;
  const scopedEdges = scope
    ? overview.edges.filter(
        (edge) =>
          edge.source_session_id != null &&
          edge.target_session_id != null &&
          scope.has(edge.source_session_id) &&
          scope.has(edge.target_session_id),
      )
    : overview.edges;
  const allRows = compositionRows(payload);
  const rows = scope ? allRows.filter((row) => scope.has(row.id)) : allRows;
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
  const branches = treeQuery.data?.branches ?? [];
  const currentBranchId = treeQuery.data?.selected_branch_id ?? undefined;

  return (
    <div className="route-container-wide w-full min-w-0 overflow-hidden pb-8">
      <div className="grid gap-4">
        <PageHeader
          title="Session graph"
          description={`${overview.project ?? "Unknown project"} · branch-local orchestration across ${orchestration.session_count ?? overview.sessions.length} session(s)`}
        />

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

        {branches.length > 0 || treeQuery.isPending ? (
          <Card className="min-w-0">
            <CardHeader>
              <CardTitle className="title-card">Conversation branches</CardTitle>
              <CardDescription>
                Ordinary human forks stay separate; each branch owns its agent graph. Scope the
                hierarchy and composition below to one branch, or open a branch as a session.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {treeQuery.isPending ? (
                <p className="m-0 text-body-sm text-muted-foreground">Loading branches…</p>
              ) : treeQuery.isError ? (
                <p className="m-0 text-body-sm text-destructive">{treeQuery.error.message}</p>
              ) : (
                <ConversationBranches
                  branches={branches}
                  rootId={payload.root_session_id}
                  scopedBranchId={branch}
                  currentBranchId={currentBranchId}
                />
              )}
            </CardContent>
          </Card>
        ) : null}

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Agent hierarchy</CardTitle>
            <CardDescription>
              {scope
                ? "Only the selected conversation branch and agents spawned from it."
                : "All conversation branches and the agents spawned from them."}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <GraphTree
              nodes={scopedNodes}
              edges={scopedEdges}
              tokensBySession={tokensBySession}
              activeSessionId={branch}
            />
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Session Composition</CardTitle>
            <CardDescription>
              Per-session processed tokens, split into cached and uncached portions. Select a bar to
              open that session.
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

function GraphRedirect({ rootId, branch }: { rootId: string; branch?: string }) {
  const navigate = useNavigate();
  React.useEffect(() => {
    void navigate({
      to: "/graphs/$rootId",
      params: { rootId },
      search: { branch },
      replace: true,
    });
  }, [navigate, rootId, branch]);
  return (
    <div className="route-container-wide w-full min-w-0 pb-8">
      <LoadingState title="Opening session graph" detail="Canonicalizing on the graph root session." />
    </div>
  );
}

type BranchNode = ConversationBranch & { children: BranchNode[] };

function branchTree(branches: ConversationBranch[]): BranchNode[] {
  const nodes = new Map<string, BranchNode>(
    branches.map((branch) => [
      branch.session_id,
      { ...branch, children: [] } as BranchNode,
    ]),
  );
  const roots: BranchNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.parent_session_id
      ? nodes.get(node.parent_session_id)
      : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const byTime = (left: BranchNode, right: BranchNode) =>
    (left.started_at ?? "").localeCompare(right.started_at ?? "");
  roots.sort(byTime);
  for (const node of nodes.values()) node.children.sort(byTime);
  return roots;
}

function branchLabel(branch: ConversationBranch) {
  return branch.title || branch.agent_name || shortSessionId(branch.session_id);
}

function ConversationBranches({
  branches,
  rootId,
  scopedBranchId,
  currentBranchId,
}: {
  branches: ConversationBranch[];
  rootId: string;
  scopedBranchId?: string;
  currentBranchId?: string;
}) {
  const roots = React.useMemo(() => branchTree(branches), [branches]);

  const renderBranch = (branch: BranchNode): React.ReactNode => {
    const scoped = branch.session_id === scopedBranchId;
    const current = branch.session_id === currentBranchId;
    const agentCount = branch.spawned_agent_count ?? 0;
    return (
      <div key={branch.session_id}>
        <div
          role="treeitem"
          aria-current={scoped ? "true" : undefined}
          aria-expanded={branch.children.length ? true : undefined}
          className={cn(
            "flex min-w-0 flex-wrap items-center gap-2 rounded-lg px-3 py-2",
            scoped && "bg-surface-emphasis",
          )}
        >
          <GitBranch aria-hidden="true" className="text-muted-foreground" />
          <SessionLink sessionId={branch.session_id} className="min-w-0 truncate">
            {branchLabel(branch)}
          </SessionLink>
          {scoped ? <Badge>Branch scope</Badge> : null}
          {!scoped && current ? <Badge variant="secondary">Current branch</Badge> : null}
          {branch.vendor ? <Badge variant="secondary">{branch.vendor}</Badge> : null}
          <Badge variant="outline">
            {branch.turn_count ?? 0} turn{branch.turn_count === 1 ? "" : "s"}
          </Badge>
          <Badge variant="outline">
            {agentCount} agent{agentCount === 1 ? "" : "s"}
          </Badge>
          <span className="text-caption text-muted-foreground">
            {branch.status ?? "unknown"} · {relativeTime(branch.started_at)}
          </span>
          <div className="ml-auto flex flex-wrap gap-2">
            <Button asChild variant="outline" size="sm">
              <Link
                to="/graphs/$rootId/sessions/$sessionId"
                params={{ rootId, sessionId: branch.session_id }}
                search={{ tab: "context" }}
              >
                Open session
              </Link>
            </Button>
            {scoped ? (
              <Button asChild size="sm" variant="ghost">
                <Link to="/graphs/$rootId" params={{ rootId }} search={{ branch: undefined }}>
                  Graph-wide
                </Link>
              </Button>
            ) : (
              <Button asChild size="sm">
                <Link to="/graphs/$rootId" params={{ rootId }} search={{ branch: branch.session_id }}>
                  Scope agents
                </Link>
              </Button>
            )}
          </div>
        </div>
        {branch.children.length ? (
          <div role="group" className="ml-5 border-l border-border-soft pl-3">
            {branch.children.map(renderBranch)}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <div role="tree" aria-label="Conversation branches" className="grid gap-1">
      {roots.map(renderBranch)}
    </div>
  );
}

type CompositionRow = ReturnType<typeof compositionRows>[number];

type GraphModel = NonNullable<SessionGraphPayload["usage"]["models"]>[number];

function bucketMix(totalUsage: SessionGraphPayload["usage"]["total_usage"]) {
  const buckets = totalUsage ?? {};
  // Uncached input falls back to gross prompt when the source does not split
  // it out, matching the glossary display rule.
  const input = buckets.uncached_prompt_tokens ?? buckets.prompt_tokens ?? 0;
  return [
    { label: "Input", value: input },
    { label: "Cached", value: buckets.cached_prompt_tokens ?? 0 },
    { label: "Cache write", value: buckets.cache_write_tokens ?? 0 },
    { label: "Output", value: buckets.completion_tokens ?? 0 },
    { label: "Reasoning", value: buckets.reasoning_tokens ?? 0 },
  ];
}

/**
 * Per-session processed tokens, stacked into cached and fresh portions.
 * Cached share and cost live in the tooltip; context pressure and turn detail
 * belong to the session scope, which selecting a bar opens.
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
              void navigate({
                to: "/graphs/$rootId/sessions/$sessionId",
                params: { rootId, sessionId: row.id },
                search: { tab: "context" },
              });
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
          const processed = row.usage?.total_usage?.processed_tokens ?? row.stats?.usage?.processed_tokens;
          const share = cachedShare(row.stats);
          const tooltipRows = [
            tooltipRow("Role", escapeHtml(role), theme.axis),
            tooltipRow("Processed", formatTokens(processed), theme.axis),
            tooltipRow("Cached share", share != null ? formatPercent(share * 100) : "-", theme.axis),
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
            name: "Uncached",
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
