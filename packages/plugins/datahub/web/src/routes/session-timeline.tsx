import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { AlertCircle, Bot, Box, MessageSquare, User, Wrench } from "lucide-react";

import {
  fetchSessionEventDetails,
  fetchSessionEvidenceTimeline,
  fetchSessionItemDetails,
  type SessionTimelineEntry,
  type TimelineKind,
} from "@/api";
import { LoadingState } from "@/components/loading-state";
import { MetricCard } from "@/components/metric-card";
import { SessionLink, shortSessionId } from "@/components/session-link";
import { SessionViewTabs } from "@/components/session-view-tabs";
import { StateBlock } from "@/components/state-block";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { FilterLabel } from "@/components/table-cells";
import { useDatahubDelivery } from "@/hooks/use-datahub-delivery";
import { relativeTime } from "@/lib/relative-time";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 300;

type OutcomeFilter = "all" | "failed" | "succeeded";

export function SessionTimelineRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId" });
  const search = useSearch({ from: "/sessions/$sessionId" });
  const navigate = useNavigate({ from: "/sessions/$sessionId" });
  const delivery = useDatahubDelivery();
  const kind = search.kind ?? "all";
  const agent = search.agent ?? "all";
  const outcome = search.outcome ?? "all";
  const [visibleCount, setVisibleCount] = React.useState(PAGE_SIZE);
  const query = useQuery({
    queryKey: ["session-timeline", sessionId],
    queryFn: () => fetchSessionEvidenceTimeline(sessionId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
  });

  React.useEffect(() => {
    setVisibleCount(PAGE_SIZE);
  }, [kind, agent, outcome, sessionId]);

  const updateSearch = React.useCallback(
    (updates: { kind?: TimelineKind; agent?: string; outcome?: Exclude<OutcomeFilter, "all">; entry?: string }) => {
      void navigate({
        search: (current) => ({ ...current, ...updates, view: "timeline" }),
        replace: true,
      });
    },
    [navigate],
  );

  if (query.isPending) {
    return (
      <div className="route-container w-full min-w-0 pb-8">
        <LoadingState title="Loading evidence timeline" detail="Reading retained canonical session activity." />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="route-container w-full min-w-0 pb-8">
        <StateBlock title="Evidence timeline failed" detail={query.error.message} onRetry={() => query.refetch()} />
      </div>
    );
  }

  const payload = query.data;
  const agents = Array.from(
    new Map(
      payload.entries.map((entry) => [entry.session_id, agentLabel(entry)]),
    ).entries(),
  );
  const filtered = payload.entries.filter((entry) => {
    if (kind !== "all" && entry.kind !== kind) return false;
    if (agent !== "all" && entry.session_id !== agent) return false;
    if (outcome === "failed" && !entry.failed) return false;
    if (outcome === "succeeded" && (entry.failed || !isTerminalSuccess(entry.status))) return false;
    return true;
  });
  const selected = search.entry
    ? payload.entries.find((entry) => entry.id === search.entry) ?? null
    : null;
  const selectedIndex = selected ? filtered.findIndex((entry) => entry.id === selected.id) : -1;
  const effectiveVisibleCount = selectedIndex >= visibleCount ? selectedIndex + 1 : visibleCount;
  const visible = filtered.slice(0, effectiveVisibleCount);
  const failed = payload.entries.filter((entry) => entry.failed).length;
  const subagents = new Set(
    payload.entries.map((entry) => entry.target_session_id).filter(Boolean),
  ).size;
  const linkedEntries = payload.entries.filter(
    (entry) => entry.item_ids.length > 0 || entry.event_ids.length > 0,
  ).length;
  const sourceFailures = delivery.sourceStatus?.failed ?? 0;
  const incompleteSources = delivery.sourceStatus?.incomplete ?? 0;

  return (
    <div className="route-container w-full min-w-0 overflow-hidden pb-8">
      <div className="grid gap-4">
        <div className="min-w-0">
          <h1 className="m-0 font-display text-h1 leading-tight">Evidence timeline</h1>
          <p className="m-0 mt-1 text-body-sm text-muted-foreground">
            Source-linked requests, responses, tools, failures, and child-agent activity in recorded order.
          </p>
        </div>
        <SessionViewTabs sessionId={sessionId} active="timeline" />

        <section className="stat-grid min-w-0">
          <MetricCard label="Evidence entries" value={payload.entries.length} detail={`Revision ${payload.revision}`} />
          <MetricCard label="Turns" value={new Set(payload.entries.map((entry) => entry.turn_id)).size} detail={`${agents.length} session branch(es)`} />
          <MetricCard label="Failures" value={failed} detail="Observed failed or error outcomes" />
          <MetricCard label="Child agents" value={subagents} detail="Linked through canonical graph edges" />
          <MetricCard
            label="Source linked"
            value={`${linkedEntries}/${payload.entries.length}`}
            detail={`${sourceFailures} failed · ${incompleteSources} incomplete source(s)`}
          />
        </section>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle className="title-card">Filters</CardTitle>
            <CardDescription>Filters change presentation only; the retained revision remains fixed.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-end gap-3">
            <FilterLabel label="Evidence type">
              <Select value={kind} onValueChange={(value) => updateSearch({ kind: value === "all" ? undefined : value as TimelineKind, entry: undefined })}>
                <SelectTrigger className="min-w-44"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All evidence</SelectItem>
                  <SelectItem value="user">User requests</SelectItem>
                  <SelectItem value="assistant">Assistant responses</SelectItem>
                  <SelectItem value="tool">Tools and artifacts</SelectItem>
                  <SelectItem value="subagent">Child agents</SelectItem>
                  <SelectItem value="compaction">Compactions</SelectItem>
                </SelectContent>
              </Select>
            </FilterLabel>
            <FilterLabel label="Agent / branch">
              <Select value={agent} onValueChange={(value) => updateSearch({ agent: value === "all" ? undefined : value, entry: undefined })}>
                <SelectTrigger className="min-w-52"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All agents</SelectItem>
                  {agents.map(([id, label]) => <SelectItem key={id} value={id}>{label}</SelectItem>)}
                </SelectContent>
              </Select>
            </FilterLabel>
            <FilterLabel label="Outcome">
              <Select value={outcome} onValueChange={(value) => updateSearch({ outcome: value === "all" ? undefined : value as Exclude<OutcomeFilter, "all">, entry: undefined })}>
                <SelectTrigger className="min-w-40"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All outcomes</SelectItem>
                  <SelectItem value="failed">Failures</SelectItem>
                  <SelectItem value="succeeded">Succeeded</SelectItem>
                </SelectContent>
              </Select>
            </FilterLabel>
            {kind !== "all" || agent !== "all" || outcome !== "all" ? (
              <Button variant="ghost" onClick={() => updateSearch({ kind: undefined, agent: undefined, outcome: undefined, entry: undefined })}>
                Clear filters
              </Button>
            ) : null}
          </CardContent>
        </Card>

        <div className="panel flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-muted-foreground">
          <span>Timeline revision {payload.revision}</span>
          <span>{delivery.freshness?.lag_seconds == null ? "Refresh lag unavailable" : `${Math.round(delivery.freshness.lag_seconds)}s refresh lag`}</span>
          <span>{linkedEntries} entries retain verified item/event references</span>
        </div>

        {payload.warnings.map((warning) => (
          <div key={warning} className="panel flex items-start gap-2 border-warning/40 text-body-sm">
            <AlertCircle aria-hidden="true" className="mt-0.5 text-warning" />
            <span>{warning}</span>
          </div>
        ))}
        {search.entry && !selected ? (
          <div className="panel flex items-start gap-2 border-warning/40 text-body-sm">
            <AlertCircle aria-hidden="true" className="mt-0.5 text-warning" />
            <span>The selected evidence entry is not present in revision {payload.revision}.</span>
          </div>
        ) : null}

        <section aria-label="Session evidence" className="relative grid gap-3 pl-5 before:absolute before:bottom-4 before:left-[0.45rem] before:top-4 before:w-px before:bg-border-soft">
          {visible.map((entry) => (
            <TimelineRow
              key={entry.id}
              entry={entry}
              selected={selected?.id === entry.id}
              onSelect={() => updateSearch({ entry: selected?.id === entry.id ? undefined : entry.id })}
            />
          ))}
          {!filtered.length ? (
            <Card><CardContent className="py-8 text-center text-muted-foreground">No retained evidence matches these filters.</CardContent></Card>
          ) : null}
        </section>

        {visibleCount < filtered.length ? (
          <div className="flex justify-center">
            <Button variant="outline" onClick={() => setVisibleCount((value) => value + PAGE_SIZE)}>
              Show {Math.min(PAGE_SIZE, filtered.length - visibleCount)} more
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function TimelineRow({ entry, selected, onSelect }: { entry: SessionTimelineEntry; selected: boolean; onSelect: () => void }) {
  const hasDetail = entry.item_ids.length > 0 || entry.event_ids.length > 0;
  return (
    <article className="relative">
      <span className={cn("absolute -left-5 top-5 z-10 grid size-4 place-items-center rounded-full border bg-background", entry.failed ? "border-destructive text-destructive" : "border-primary text-primary")}>
        {kindIcon(entry.kind)}
      </span>
      <Card className={cn("min-w-0", entry.failed && "border-destructive/40")}>
        <CardHeader className="gap-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <CardTitle className="title-card text-base">{entry.label}</CardTitle>
            <Badge variant="secondary">{entry.kind}</Badge>
            {entry.status ? <Badge variant={entry.failed ? "destructive" : "outline"}>{entry.status}</Badge> : null}
            <span className="ml-auto text-caption text-muted-foreground" title={entry.timestamp ?? undefined}>{formatWhen(entry.timestamp)}</span>
          </div>
          <CardDescription className="flex flex-wrap items-center gap-2">
            <SessionLink sessionId={entry.session_id}>{agentLabel(entry)}</SessionLink>
            <span>Turn {entry.turn_sequence + 1}</span>
            {entry.target_session_id ? (
              <Link to="/sessions/$sessionId" params={{ sessionId: entry.target_session_id }} search={{ view: "timeline" }} className="font-medium text-primary hover:underline">
                Open child {shortSessionId(entry.target_session_id)}
              </Link>
            ) : null}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3">
          {entry.summary ? <p className="m-0 whitespace-pre-wrap break-words text-body-sm">{entry.summary}</p> : null}
          {hasDetail ? <Button variant="outline" size="sm" className="w-fit" onClick={onSelect}>{selected ? "Hide source detail" : "Load source detail"}</Button> : null}
          {selected ? <EvidenceDetail entry={entry} /> : null}
        </CardContent>
      </Card>
    </article>
  );
}

function EvidenceDetail({ entry }: { entry: SessionTimelineEntry }) {
  const query = useQuery({
    queryKey: ["session-timeline-detail", entry.item_ids, entry.event_ids],
    queryFn: async () => {
      const [items, events] = await Promise.all([
        entry.item_ids.length ? fetchSessionItemDetails(entry.item_ids) : Promise.resolve([]),
        entry.event_ids.length ? fetchSessionEventDetails(entry.event_ids) : Promise.resolve({ root_session_id: null, matches: [] }),
      ]);
      return { items, events: events.matches };
    },
    staleTime: 5 * 60_000,
  });
  if (query.isPending) return <p className="m-0 text-caption text-muted-foreground">Verifying source ranges…</p>;
  if (query.isError) return <p className="m-0 text-caption text-destructive">{query.error.message}</p>;
  if (!query.data.items.length && !query.data.events.length) return <p className="m-0 text-caption text-muted-foreground">Source detail is unavailable for this retained entry.</p>;
  return (
    <div className="grid gap-2 border-t border-border-soft pt-3">
      {[...query.data.items, ...query.data.events].map((detail, index) => (
        <pre key={("item_id" in detail ? detail.item_id : detail.event_id) ?? index} className="m-0 max-h-80 overflow-auto rounded-lg bg-surface-emphasis p-3 text-caption whitespace-pre-wrap break-words">
          {JSON.stringify(detail, null, 2)}
        </pre>
      ))}
    </div>
  );
}

function agentLabel(entry: SessionTimelineEntry) {
  return entry.agent_name || entry.vendor || shortSessionId(entry.session_id);
}

function isTerminalSuccess(status: string | null) {
  return Boolean(status && ["success", "succeeded", "done", "completed"].some((value) => status.toLowerCase().includes(value)));
}

function formatWhen(value: string | null) {
  if (!value) return "Recorded order";
  return relativeTime(value);
}

function kindIcon(kind: TimelineKind) {
  const Icon = kind === "user" ? User : kind === "assistant" ? MessageSquare : kind === "subagent" ? Bot : kind === "compaction" ? Box : Wrench;
  return <Icon aria-hidden="true" className="size-2.5" />;
}
