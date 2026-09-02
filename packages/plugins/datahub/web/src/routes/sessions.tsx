import * as React from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useNavigate, useRouter, useSearch } from "@tanstack/react-router";
import { X } from "lucide-react";
import { fetchDatahubSnapshot, fetchSessions, type SessionItem } from "@/api";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/route-header";
import { Toolbar } from "@/components/toolbar";
import { StateBlock } from "@/components/state-block";
import { VendorBadges } from "@/components/badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { formatCostUsd, formatDuration, formatTokens, shortId } from "@/lib/format";
import { relativeTime } from "@/lib/relative-time";
import { cn } from "@/lib/utils";

const CURSOR_PAGE_SIZE = 50;

const WINDOW_OPTIONS = [
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
] as const;

function sessionId(item: SessionItem) {
  return item.root_session_id;
}

function matchesFilter(item: SessionItem, term: string): boolean {
  const haystack = [
    sessionId(item),
    item.title ?? "",
    item.preview ?? "",
    item.vendors.join(" "),
    item.project ?? "",
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(term);
}

function dayKey(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function itemDayKey(item: SessionItem): string {
  const value = item.started_at ? new Date(item.started_at) : null;
  return value && !Number.isNaN(value.getTime()) ? dayKey(value) : "undated";
}

function groupLabel(key: string): string {
  if (key === "undated") return "Undated";
  if (key === dayKey(new Date())) return "Today";
  if (key === dayKey(new Date(Date.now() - 86_400_000))) return "Yesterday";
  const [year, month, day] = key.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

type SessionGroup = { key: string; label: string; items: SessionItem[] };

function groupByDay(items: SessionItem[]): SessionGroup[] {
  const groups = new Map<string, SessionItem[]>();
  for (const item of items) {
    const key = itemDayKey(item);
    const bucket = groups.get(key);
    if (bucket) bucket.push(item);
    else groups.set(key, [item]);
  }
  return [...groups.entries()].map(([key, groupItems]) => ({
    key,
    label: groupLabel(key),
    items: groupItems,
  }));
}

function StatusDot({ item }: { item: SessionItem }) {
  const failed = (item.failed_tool_calls ?? 0) > 0;
  const living = item.status === "living";
  const label = living
    ? "Live now"
    : failed
      ? `${item.failed_tool_calls} failed tool call${item.failed_tool_calls === 1 ? "" : "s"}`
      : "Completed";
  return (
    <span
      title={label}
      aria-label={label}
      className={cn(
        "mt-1.5 size-2 shrink-0 self-start rounded-full",
        living && "animate-pulse bg-success",
        !living && failed && "bg-warning",
        !living && !failed && "bg-surface-emphasis",
      )}
    />
  );
}

function SessionRow({ item, onOpen }: { item: SessionItem; onOpen: () => void }) {
  const title = item.title ?? item.preview ?? "Untitled";
  return (
    <button
      type="button"
      onClick={onOpen}
      className="grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-3 border-b border-border-subtle px-2 py-2.5 text-left transition-colors last:border-b-0 hover:bg-surface-subtle"
    >
      <StatusDot item={item} />
      <span className="grid min-w-0 gap-1">
        <span
          className={cn(
            "truncate text-body-sm font-medium",
            !item.title && item.preview && "italic text-muted-foreground",
          )}
          title={title}
        >
          {title}
        </span>
        <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-caption text-muted-foreground">
          <span className="mono">{shortId(sessionId(item))}</span>
          {item.project ? <span className="truncate">{item.project}</span> : null}
          <VendorBadges vendors={item.vendors} />
        </span>
      </span>
      <span className="flex items-center gap-3 pt-0.5 text-caption tabular-nums text-muted-foreground">
        {item.cost_usd != null ? (
          <span className="hidden lg:inline" title={item.pricing_confidence === "estimated" ? "Estimated cost" : "Reported cost"}>
            {item.pricing_confidence === "estimated" ? "~" : ""}
            {formatCostUsd(item.cost_usd)}
          </span>
        ) : null}
        {item.processed_tokens != null ? (
          <span className="hidden sm:inline">{formatTokens(item.processed_tokens)} tok</span>
        ) : null}
        {item.execution_seconds != null ? (
          <span className="hidden md:inline">{formatDuration(item.execution_seconds)}</span>
        ) : null}
        <span className="shrink-0">{relativeTime(item.started_at)}</span>
      </span>
    </button>
  );
}

function SessionListSkeleton() {
  return (
    <div className="grid gap-2" aria-hidden="true">
      {Array.from({ length: 6 }, (_, index) => (
        <Skeleton key={index} className="h-14 w-full" />
      ))}
    </div>
  );
}

export function SessionsRoute() {
  const [filter, setFilter] = React.useState("");
  const [windowDays, setWindowDays] = React.useState<string>("7");
  const router = useRouter();
  const navigate = useNavigate({ from: "/sessions" });
  const { projectName } = useSearch({ from: "/sessions" });

  // The runtime materializes a fixed recent horizon; only offer windows the
  // server can actually serve (snapshot is cached by the delivery provider).
  const snapshot = useQuery({
    queryKey: ["datahub", "snapshot"],
    queryFn: ({ signal }) => fetchDatahubSnapshot(signal),
    staleTime: Infinity,
  });
  const horizon = snapshot.data?.horizon_days ?? 7;
  const windowOptions = WINDOW_OPTIONS.filter((option) => Number(option.value) <= horizon);
  React.useEffect(() => {
    if (Number(windowDays) > horizon) setWindowDays(String(horizon));
  }, [horizon, windowDays]);

  const sessions = useInfiniteQuery({
    queryKey: ["sessions", "cursor", windowDays, projectName ?? null],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      fetchSessions({
        sinceDays: Number(windowDays),
        projectName: projectName ?? undefined,
        cursor: pageParam ?? undefined,
        limit: CURSOR_PAGE_SIZE,
        signal,
      }),
    getNextPageParam: (lastPage) => lastPage.page?.next_cursor ?? undefined,
    placeholderData: (previous) => previous,
  });

  const data = React.useMemo(() => {
    const byId = new Map<string, SessionItem>();
    for (const page of sessions.data?.pages ?? []) {
      for (const item of page.items) byId.set(sessionId(item), item);
    }
    return [...byId.values()];
  }, [sessions.data]);

  const groups = React.useMemo(() => {
    const term = filter.trim().toLowerCase();
    const visible = term ? data.filter((item) => matchesFilter(item, term)) : data;
    return groupByDay(visible);
  }, [data, filter]);

  const openSession = (id: string) =>
    void router.navigate({
      to: "/sessions/$sessionId",
      params: { sessionId: id },
      search: { view: "context" },
    });

  return (
    <div className="route-container-wide">
      <PageHeader
        eyebrow="Observe"
        title="Sessions"
        description="Conversation branches and their agent runs."
        actions={
          windowOptions.length > 1 ? (
            <ToggleGroup
              type="single"
              size="sm"
              variant="outline"
              value={windowDays}
              onValueChange={(value) => {
                if (value) setWindowDays(value);
              }}
              aria-label="Session window"
            >
              {windowOptions.map((option) => (
                <ToggleGroupItem key={option.value} value={option.value}>
                  {option.label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          ) : null
        }
      />
      {projectName ? (
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary" className="gap-1.5">
            Project: {projectName}
            <button
              type="button"
              aria-label={`Clear project filter ${projectName}`}
              className="rounded-full hover:text-foreground"
              onClick={() =>
                void navigate({ search: { projectName: undefined }, replace: true })
              }
            >
              <X size={12} />
            </button>
          </Badge>
        </div>
      ) : null}
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter sessions by title, preview, vendor, project, or id" />
      {sessions.isPending ? <SessionListSkeleton /> : null}
      {sessions.isError ? <StateBlock title="Session scan failed" detail={sessions.error.message} onRetry={() => sessions.refetch()} /> : null}
      {sessions.data ? (
        <>
          {groups.length ? (
            <div className="grid gap-4">
              {groups.map((group) => (
                <section key={group.key} className="grid gap-1">
                  <div className="flex items-baseline justify-between gap-2 px-2">
                    <h2 className="eyebrow-soft m-0 text-muted-foreground">{group.label}</h2>
                    <span className="text-caption tabular-nums text-muted-foreground">{group.items.length}</span>
                  </div>
                  <div className="grid border-t border-border-subtle">
                    {group.items.map((item) => (
                      <SessionRow key={sessionId(item)} item={item} onOpen={() => openSession(sessionId(item))} />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          ) : (
            <div className="panel py-8 text-center text-body-sm text-muted-foreground">
              <p className="m-0">No sessions match the current filter.</p>
              <p className="m-0 mt-1 text-caption">Try adjusting the filter.</p>
            </div>
          )}
          <div className="flex flex-wrap items-center justify-between gap-2 px-2 pb-2 text-body-sm text-muted-foreground">
            <span>
              {data.length.toLocaleString()} session{data.length === 1 ? "" : "s"} loaded from the last {windowDays} days
            </span>
            {sessions.hasNextPage ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={sessions.isFetchingNextPage}
                onClick={() => void sessions.fetchNextPage()}
              >
                {sessions.isFetchingNextPage ? "Loading…" : `Load ${CURSOR_PAGE_SIZE} more`}
              </Button>
            ) : null}
          </div>
          {sessions.isFetchNextPageError ? (
            <StateBlock
              title="More sessions could not be loaded"
              detail={sessions.error.message}
              onRetry={() => void sessions.fetchNextPage()}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
