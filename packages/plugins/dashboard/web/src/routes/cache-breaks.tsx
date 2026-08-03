import * as React from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { fetchCacheBreaks, type AggregateCacheBreak, type CacheBreaksPayload, type CacheBreakSessionRow } from "@/api";
import { MetricCard } from "@/components/metric-card";
import { RouteHeader } from "@/components/route-header";
import { SectionTabs } from "@/components/section-tabs";
import { shortSessionId } from "@/components/session-link";
import { StateBlock } from "@/components/state-block";
import { LoadingShell } from "@/components/loading-shell";
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
import { useDateRange } from "@/hooks/use-date-range";
import { HeaderLabel, FilterLabel } from "@/components/table-cells";
import { cn } from "@/lib/utils";
import {
  cacheBreakTone,
  formatCostUsd,
  formatIdleSeconds,
  formatTokens,
  type CacheBreakType,
} from "@/lib/cache-breaks";

const ALL_PROJECTS = "__all_projects__";

const TYPE_BAR_COLOR: Record<CacheBreakType, string> = {
  effort_switch: "var(--warning)",
  ttl_confirmed: "color-mix(in srgb, var(--foreground) 45%, transparent)",
  ttl_likely: "color-mix(in srgb, var(--foreground) 22%, transparent)",
};

export function CacheBreaksRoute() {
  const search = useSearch({ from: "/cache-breaks" });
  const navigate = useNavigate({ from: "/cache-breaks" });
  const { days: sinceDays } = useDateRange();
  const projectName = search.projectName ?? null;
  const query = useQuery({
    queryKey: ["cache-breaks", sinceDays, projectName],
    queryFn: () => fetchCacheBreaks({ sinceDays, projectName }),
    placeholderData: (previous) => previous,
  });

  const [activeTab, setActiveTab] = React.useState("chart");

  const setProjectName = (value: string) => {
    void navigate({
      search: (current) => ({
        ...current,
        projectName: value === ALL_PROJECTS ? undefined : value,
      }),
    });
  };

  if (query.isPending) {
    return <LoadingShell eyebrow="Cache economics" title="Loading cache breaks" variant="metrics" />;
  }

  if (query.isError) {
    return <StateBlock title="Cache breaks unavailable" detail={query.error.message} onRetry={() => query.refetch()} />;
  }

  const data = query.data;
  const hasBreaks = data.summary.total_breaks > 0;

  return (
    <div className="route-container w-full min-w-0 overflow-hidden">
      <RouteHeader
        eyebrow="Cache economics"
        title="Cache breaks"
      />

      <Card className="min-w-0">
        <CardContent className="flex flex-wrap items-center gap-3 pt-6">
          <p className="eyebrow-soft text-muted-foreground">
            Showing last {sinceDays} day{sinceDays === 1 ? "" : "s"} · adjust in the header
          </p>
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

      {!hasBreaks ? (
        <StateBlock
          title="No cache breaks in this range"
          detail="No turn re-read a collapsed or evicted prompt cache during the selected window."
        />
      ) : (
        <SectionTabs
          activeTab={activeTab}
          onTabChange={setActiveTab}
          ariaLabel="Cache breaks sections"
          summary={
            <>
              <SummaryCards data={data} />
              {data.warnings.length > 0 ? (
                <div className="alert alert-warning rounded-xl text-body-sm text-foreground">
                  {data.warnings.join(" · ")}
                </div>
              ) : null}
            </>
          }
          tabs={[
            {
              id: "chart",
              label: "Trend",
              content: (
                <>
                  <ByTypeStrip data={data} />
                  <DailyBreaksChart data={data} />
                </>
              ),
            },
            {
              id: "breakdown",
              label: "Breakdown",
              content: <BreakdownCards data={data} />,
            },
            {
              id: "sessions",
              label: "Top Sessions",
              badge={data.top_sessions.length},
              content: <TopSessionsTable rows={data.top_sessions} />,
            },
            {
              id: "all",
              label: "All Breaks",
              badge={data.breaks.length},
              content: <BreakTable rows={data.breaks} />,
            },
          ]}
        />
      )}
    </div>
  );
}

function SummaryCards({ data }: { data: CacheBreaksPayload }) {
  const s = data.summary;
  const confirmedRatio = s.total_breaks ? s.confirmed_effort_switches / s.total_breaks : 0;
  return (
    <section className="stat-grid min-w-0">
      <MetricCard
        label="Cache breaks"
        value={s.total_breaks}
        detail={`${s.sessions_with_breaks.toLocaleString()} session${s.sessions_with_breaks === 1 ? "" : "s"} · ${s.affected_projects.toLocaleString()} project${s.affected_projects === 1 ? "" : "s"}`}
      />
      <MetricCard
        label="Re-read tokens"
        value={formatTokens(s.total_re_read_tokens)}
        detail="Context reprocessed after a cache miss"
      />
      <MetricCard
        label="Estimated waste"
        value={formatCostUsd(s.estimated_waste_usd)}
        detail={s.avg_break_cost_usd != null ? `${formatCostUsd(s.avg_break_cost_usd)} avg / break` : "uncached re-read premium"}
      />
      <MetricCard
        label="Confirmed effort switches"
        value={s.confirmed_effort_switches}
        detail={`${Math.round(confirmedRatio * 100)}% of breaks · real effort_from->effort_to`}
        ratio={confirmedRatio}
      />
    </section>
  );
}

function ByTypeStrip({ data }: { data: CacheBreaksPayload }) {
  const types: CacheBreakType[] = ["effort_switch", "ttl_confirmed", "ttl_likely"];
  const total = data.summary.total_breaks || 1;
  return (
    <section className="grid min-w-0 grid-cols-3 gap-4 max-lg:grid-cols-1">
      {types.map((type) => {
        const count = data.summary.by_type[type] ?? 0;
        const tone = cacheBreakTone(type, null, null);
        return (
          <Card key={type} className="min-w-0">
            <CardContent className="flex items-center gap-3 pt-6">
              <span
                className={cn(
                  "inline-flex size-8 shrink-0 items-center justify-center rounded-md border",
                  tone.className,
                )}
              >
                {tone.icon}
              </span>
              <div className="min-w-0">
                <p className="m-0 text-muted-foreground">{tone.label}</p>
                <p className="m-0 metric-hero">{count.toLocaleString()}</p>
                <p className="m-0 mono text-caption text-muted-foreground">
                  {Math.round((count / total) * 100)}% of breaks
                </p>
              </div>
            </CardContent>
          </Card>
        );
      })}
    </section>
  );
}

function DailyBreaksChart({ data }: { data: CacheBreaksPayload }) {
  const buckets = data.time_buckets;
  if (buckets.length < 2) return null;
  const maxValue = Math.max(...buckets.map((b) => Math.max(b.breaks, b.re_read_tokens > 0 ? 1 : 0)), 1);
  // Normalize bar height by break count; waste shown in the tooltip.
  const maxBreaks = Math.max(...buckets.map((b) => b.breaks), 1);
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Breaks over time</CardTitle>
        <CardDescription>
          Daily cache breaks stacked by cause. Effort-switch (amber) is the avoidable re-read; TTL is age eviction.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div
          className="flex h-40 w-full items-end gap-1.5"
          role="img"
          aria-label="Daily cache breaks by cause"
        >
          {buckets.map((bucket) => {
            const heightPct = (bucket.breaks / maxBreaks) * 100;
            const effort = bucket.by_type.effort_switch ?? 0;
            const ttlConfirmed = bucket.by_type.ttl_confirmed ?? 0;
            const ttlLikely = bucket.by_type.ttl_likely ?? 0;
            const segments = [
              { type: "effort_switch" as const, count: effort },
              { type: "ttl_confirmed" as const, count: ttlConfirmed },
              { type: "ttl_likely" as const, count: ttlLikely },
            ].filter((seg) => seg.count > 0);
            const day = bucket.bucket.slice(5);
            return (
              <div
                key={bucket.bucket}
                className="group relative flex h-full flex-1 flex-col justify-end"
                title={`${bucket.bucket} · ${bucket.breaks} breaks (${effort} effort, ${ttlConfirmed + ttlLikely} TTL) · ${formatTokens(bucket.re_read_tokens)} re-read · ${formatCostUsd(bucket.waste_usd)}`}
              >
                <div
                  className="flex w-full flex-col-reverse overflow-hidden rounded-t-sm transition-opacity group-hover:opacity-100"
                  style={{ height: `${Math.max(heightPct, bucket.breaks > 0 ? 4 : 0)}%` }}
                >
                  {segments.map((seg) => (
                    <div
                      key={seg.type}
                      style={{
                        background: TYPE_BAR_COLOR[seg.type],
                        height: `${(seg.count / bucket.breaks) * 100}%`,
                      }}
                    />
                  ))}
                </div>
                <span className="mt-1 block text-center mono text-[0.6rem] text-muted-foreground">{day}</span>
              </div>
            );
          })}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-muted-foreground">
          {(["effort_switch", "ttl_confirmed", "ttl_likely"] as CacheBreakType[]).map((type) => (
            <span key={type} className="inline-flex items-center gap-1.5">
              <span className="inline-block h-2 w-2 rounded-[2px]" style={{ background: TYPE_BAR_COLOR[type] }} />
              {cacheBreakTone(type, null, null).label}
            </span>
          ))}
          <span className="mono">peak {maxValue.toLocaleString()} breaks/day</span>
        </div>
      </CardContent>
    </Card>
  );
}

function BreakdownCards({ data }: { data: CacheBreaksPayload }) {
  return (
    <div className="grid min-w-0 gap-4 md:grid-cols-2">
      <BreakdownCard
        title="By vendor"
        description="Cache-break cost concentration across vendors."
        rows={data.by_vendor.map((row) => ({ label: row.vendor, ...row }))}
      />
      <BreakdownCard
        title="By project"
        description="Where the re-read waste accumulates."
        rows={data.by_project.map((row) => ({ label: row.project, ...row }))}
      />
    </div>
  );
}

function BreakdownCard({
  title,
  description,
  rows,
}: {
  title: string;
  description: string;
  rows: Array<{ label: string; breaks: number; re_read_tokens: number; waste_usd: number }>;
}) {
  if (!rows.length) return null;
  const maxBreaks = Math.max(...rows.map((r) => r.breaks), 1);
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        {rows.map((row) => (
          <div key={row.label} className="grid gap-1">
            <div className="flex items-center justify-between gap-3 text-body-sm">
              <span className="min-w-0 truncate font-medium">{row.label}</span>
              <span className="mono text-muted-foreground">
                {row.breaks.toLocaleString()} breaks · {formatCostUsd(row.waste_usd)}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded bg-surface-emphasis">
              <div
                className="h-full rounded bg-primary"
                style={{ width: `${Math.max(4, (row.breaks / maxBreaks) * 100)}%` }}
              />
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

const sessionColumns: ColumnDef<CacheBreakSessionRow>[] = [
  {
    id: "session",
    header: () => <HeaderLabel>Session</HeaderLabel>,
    cell: ({ row }) => {
      const item = row.original;
      return (
        <div className="min-w-[14rem]">
          <Link
            to="/sessions/$sessionId/context-window"
            params={{ sessionId: item.session_id }}
            className="font-medium text-primary hover:underline"
          >
            {shortSessionId(item.session_id)}
          </Link>
          <p className="m-0 mt-1 max-w-[28rem] truncate text-body-sm text-muted-foreground">
            {item.title || item.started_at || "Untitled session"}
          </p>
        </div>
      );
    },
  },
  {
    id: "project",
    accessorFn: (row) => row.project,
    header: () => <HeaderLabel>Project</HeaderLabel>,
  },
  {
    id: "vendor",
    accessorFn: (row) => row.vendor,
    header: () => <HeaderLabel>Vendor</HeaderLabel>,
  },
  {
    id: "breaks",
    accessorFn: (row) => row.breaks,
    header: () => <HeaderLabel>Breaks</HeaderLabel>,
    cell: ({ getValue }) => <span className="mono">{getValue<number>().toLocaleString()}</span>,
  },
  {
    id: "confirmed",
    accessorFn: (row) => row.confirmed,
    header: () => <HeaderLabel>Confirmed</HeaderLabel>,
    cell: ({ getValue }) => {
      const n = getValue<number>();
      return n > 0 ? <Badge variant="secondary" className="mono">{n}</Badge> : <span className="mono text-muted-foreground">0</span>;
    },
  },
  {
    id: "re_read",
    accessorFn: (row) => row.re_read_tokens,
    header: () => <HeaderLabel>Re-read</HeaderLabel>,
    cell: ({ getValue }) => <span className="mono">{formatTokens(getValue<number>())}</span>,
  },
  {
    id: "waste",
    accessorFn: (row) => row.waste_usd,
    header: () => <HeaderLabel>Waste</HeaderLabel>,
    cell: ({ getValue }) => <span className="mono">{formatCostUsd(getValue<number>())}</span>,
  },
];

function TopSessionsTable({ rows }: { rows: CacheBreakSessionRow[] }) {
  const table = useReactTable({
    data: rows,
    columns: sessionColumns,
    getCoreRowModel: getCoreRowModel(),
  });
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Top sessions by waste</CardTitle>
        <CardDescription>
          Drill into a session's context window for the per-turn break timeline and effort levels.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable table={table} columnCount={sessionColumns.length} emptyMessage="No sessions with cache breaks." emptyHint="No cache breaks recorded in this period." />
      </CardContent>
    </Card>
  );
}

const breakColumns: ColumnDef<AggregateCacheBreak>[] = [
  {
    id: "type",
    accessorFn: (row) => row.type,
    header: () => <HeaderLabel>Type</HeaderLabel>,
    cell: ({ row }) => {
      const item = row.original;
      const tone = cacheBreakTone(item.type, item.effort_from, item.effort_to);
      return (
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-md border px-1.5 py-0 text-caption",
            tone.className,
          )}
        >
          {tone.icon}
          {tone.label}
        </span>
      );
    },
  },
  {
    id: "session",
    header: () => <HeaderLabel>Session</HeaderLabel>,
    cell: ({ row }) => {
      const item = row.original;
      return (
        <Link
          to="/sessions/$sessionId/context-window"
          params={{ sessionId: item.session_id }}
          className="font-medium text-primary hover:underline"
        >
          {shortSessionId(item.session_id)}
        </Link>
      );
    },
  },
  {
    id: "vendor",
    accessorFn: (row) => row.vendor,
    header: () => <HeaderLabel>Vendor</HeaderLabel>,
  },
  {
    id: "idle",
    accessorFn: (row) => row.idle_seconds,
    header: () => <HeaderLabel>Idle</HeaderLabel>,
    cell: ({ getValue }) => <span className="mono text-muted-foreground">{formatIdleSeconds(getValue<number>())}</span>,
  },
  {
    id: "re_read",
    accessorFn: (row) => row.re_read_tokens,
    header: () => <HeaderLabel>Re-read</HeaderLabel>,
    cell: ({ getValue }) => <span className="mono">{formatTokens(getValue<number>())}</span>,
  },
  {
    id: "cost",
    accessorFn: (row) => row.est_cost_usd ?? -1,
    header: () => <HeaderLabel>Est. cost</HeaderLabel>,
    cell: ({ getValue }) => {
      const v = getValue<number>();
      return <span className="mono">{v < 0 ? "-" : formatCostUsd(v)}</span>;
    },
  },
  {
    id: "day",
    accessorFn: (row) => row.timestamp ?? "",
    header: () => <HeaderLabel>When</HeaderLabel>,
    cell: ({ getValue }) => {
      const v = getValue<string>();
      return <span className="mono text-caption text-muted-foreground">{v ? v.slice(0, 10) : "-"}</span>;
    },
  },
];

function BreakTable({ rows }: { rows: AggregateCacheBreak[] }) {
  const table = useReactTable({
    data: rows,
    columns: breakColumns,
    getCoreRowModel: getCoreRowModel(),
  });
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">All breaks</CardTitle>
        <CardDescription>
          Every per-turn cache re-read in the window, with its cause and cost. Sort to find the costliest re-reads.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable table={table} columnCount={breakColumns.length} emptyMessage="No cache breaks recorded." emptyHint="No cache breaks recorded in this period." />
      </CardContent>
    </Card>
  );
}
