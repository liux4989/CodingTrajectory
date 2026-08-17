import * as React from "react";
import { useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
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
import { SessionLink, shortSessionId } from "@/components/session-link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
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
          <h1 className="m-0 font-display text-h1 leading-tight">Session graph</h1>
          <p className="m-0 mt-1 text-body-sm text-muted-foreground">
            {overview.project ?? "Unknown project"} · orchestration across{" "}
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
            <CardTitle className="title-card">Session Graph</CardTitle>
            <CardDescription>
              Interactive session hierarchy. The chip on each child is its observed
              relationship to the parent session; hover it for provenance.
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
              Per-session context and usage share, from `ct graph stats --session-composition`.
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Session</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead className="text-right">Context used</TableHead>
                  <TableHead className="text-right">Processed</TableHead>
                  <TableHead className="text-right">Cached share</TableHead>
                  <TableHead className="text-right">Turns</TableHead>
                  <TableHead className="text-right">Est. cost</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map(({ id, node, stats: section, usage: usageSection }) => (
                  <TableRow key={id}>
                    <TableCell className="max-w-[16rem]">
                      <SessionLink sessionId={id} className="truncate">
                        {node?.title || node?.agent_name || usageSection?.title || shortSessionId(id)}
                      </SessionLink>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">
                        {formatLabel(section?.role ?? usageSection?.role ?? (id === payload.root_session_id ? "main" : undefined))}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {section?.context_window?.used_tokens != null
                        ? `${formatTokens(section.context_window.used_tokens)}${
                            section.context_window.used_percent != null
                              ? ` (${formatPercent(section.context_window.used_percent)})`
                              : ""
                          }`
                        : "-"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatTokens(
                        usageSection?.total_usage?.processed_tokens ??
                          section?.usage?.processed_tokens,
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {cachedShare(section) != null
                        ? formatPercent((cachedShare(section) ?? 0) * 100)
                        : "-"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {section?.runtime?.turns ?? usageSection?.runtime?.turns ?? "-"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatCostUsd(usageSection?.estimated_cost?.value_usd)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Graph Usage</CardTitle>
            <CardDescription>
              Aggregate turn-level token usage, as reported by `ct graph usage`.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-4">
              <div className="flex flex-wrap gap-x-6 gap-y-2 text-body-sm">
                <span>
                  <strong className="tabular-nums">{formatTokens(totalUsage.prompt_tokens)}</strong>{" "}
                  <span className="text-muted-foreground">prompt</span>
                </span>
                <span>
                  <strong className="tabular-nums">{formatTokens(totalUsage.cached_prompt_tokens)}</strong>{" "}
                  <span className="text-muted-foreground">cached</span>
                </span>
                <span>
                  <strong className="tabular-nums">{formatTokens(totalUsage.completion_tokens)}</strong>{" "}
                  <span className="text-muted-foreground">completion</span>
                </span>
                <span>
                  <strong className="tabular-nums">{formatTokens(totalUsage.reasoning_tokens)}</strong>{" "}
                  <span className="text-muted-foreground">reasoning</span>
                </span>
                {runtime.execution_seconds != null ? (
                  <span>
                    <strong>{formatDuration(runtime.execution_seconds)}</strong>{" "}
                    <span className="text-muted-foreground">execution</span>
                  </span>
                ) : null}
              </div>
              {(usage.models ?? []).length > 0 ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Model</TableHead>
                      <TableHead className="text-right">Turns</TableHead>
                      <TableHead className="text-right">Processed</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {(usage.models ?? []).map((model, index) => (
                      <TableRow key={`${model.provider}-${model.model}-${index}`}>
                        <TableCell className="font-medium">
                          {model.model ?? "Unknown model"}
                          {model.provider ? (
                            <span className="text-muted-foreground"> · {model.provider}</span>
                          ) : null}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {model.turns ?? "-"}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {formatTokens(model.usage?.processed_tokens)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : null}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
