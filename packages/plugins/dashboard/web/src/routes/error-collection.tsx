import * as React from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, CircleSlash, ShieldAlert } from "lucide-react";
import {
  fetchErrorCollection,
  type ErrorCollectionItem,
  type ErrorCollectionKind,
  type ErrorCollectionPayload,
} from "@/api";
import { MetricCard } from "@/components/metric-card";
import { LoadingShell } from "@/components/loading-shell";
import { RouteHeader } from "@/components/route-header";
import { shortSessionId } from "@/components/session-link";
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
import { useDateRange } from "@/hooks/use-date-range";
import { HeaderLabel, FilterLabel } from "@/components/table-cells";

const ALL_PROJECTS = "__all_projects__";

const KIND_LABELS: Record<ErrorCollectionKind, string> = {
  abort_coding_session: "Abort coding session",
  abrupt_coding_mid_session: "Abrupt mid-session",
  fail_tool_coverage: "Fail tool coverage",
};

export function ErrorCollectionRoute() {
  const search = useSearch({ from: "/error-collection" });
  const navigate = useNavigate({ from: "/error-collection" });
  const { days: sinceDays } = useDateRange();
  const projectName = search.projectName ?? null;
  const query = useQuery({
    queryKey: ["error-collection", sinceDays, projectName],
    queryFn: () => fetchErrorCollection({ sinceDays, projectName }),
    placeholderData: (previous) => previous,
  });

  const setProjectName = (value: string) => {
    void navigate({
      search: (current) => ({
        ...current,
        projectName: value === ALL_PROJECTS ? undefined : value,
      }),
    });
  };

  if (query.isPending) {
    return <LoadingShell eyebrow="Session quality" title="Loading error collection" variant="metrics" />;
  }

  if (query.isError) {
    return <StateBlock title="Error collection unavailable" detail={query.error.message} onRetry={() => query.refetch()} />;
  }

  const data = query.data;

  return (
    <div className="route-container w-full min-w-0 overflow-hidden">
      <RouteHeader
        eyebrow="Session quality"
        title="Error collection"
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

      <SummaryCards data={data} />
      <KindCards data={data} />
      <ProjectCards data={data} />
      <ErrorTable rows={data.errors} />
    </div>
  );
}

function SummaryCards({ data }: { data: ErrorCollectionPayload }) {
  const affectedRatio = data.summary.sessions
    ? data.summary.affected_sessions / data.summary.sessions
    : 0;
  return (
    <section className="stat-grid min-w-0">
      <MetricCard
        label="Errors"
        value={data.summary.total_errors}
        detail={`${data.summary.sessions.toLocaleString()} sessions in ${data.filters.since_days} days`}
      />
      <MetricCard
        label="Affected Sessions"
        value={data.summary.affected_sessions}
        detail={`${Math.round(affectedRatio * 100)}% of filtered sessions`}
        ratio={affectedRatio}
      />
      <MetricCard
        label="Critical"
        value={data.summary.by_severity.critical}
        detail="Incomplete sessions or missing tool-result coverage"
      />
      <MetricCard
        label="Warnings"
        value={data.summary.by_severity.warning}
        detail="Abort events or failed tool results"
      />
    </section>
  );
}

function KindCards({ data }: { data: ErrorCollectionPayload }) {
  return (
    <section className="grid min-w-0 grid-cols-3 gap-4 max-lg:grid-cols-1">
      {Object.entries(KIND_LABELS).map(([kind, label]) => (
        <Card key={kind} className="min-w-0">
          <CardContent className="flex items-center gap-3 pt-6">
            <KindIcon kind={kind as ErrorCollectionKind} />
            <div className="min-w-0">
              <p className="m-0 text-muted-foreground">{label}</p>
              <p className="m-0 metric-hero">
                {data.summary.by_kind[kind as ErrorCollectionKind].toLocaleString()}
              </p>
            </div>
          </CardContent>
        </Card>
      ))}
    </section>
  );
}

function ProjectCards({ data }: { data: ErrorCollectionPayload }) {
  if (!data.summary.top_projects.length) {
    return null;
  }
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Project Concentration</CardTitle>
        <CardDescription>Projects with the highest number of collected coding-session errors.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        {data.summary.top_projects.map((project) => (
          <div key={project.project} className="grid gap-1">
            <div className="flex items-center justify-between gap-3 text-body-sm">
              <span className="min-w-0 truncate font-medium">{project.project}</span>
              <span className="text-muted-foreground">{project.errors} errors</span>
            </div>
            <div className="h-2 overflow-hidden rounded bg-muted">
              <div
                className="h-full rounded bg-primary"
                style={{
                  width: `${Math.max(4, (project.errors / data.summary.top_projects[0].errors) * 100)}%`,
                }}
              />
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

const errorColumns: ColumnDef<ErrorCollectionItem>[] = [
  {
    id: "type",
    header: () => <HeaderLabel>Type</HeaderLabel>,
    cell: ({ row }) => {
      const item = row.original;
      return (
        <div className="min-w-[13rem]">
          <div className="flex items-center gap-2 font-medium">
            <KindIcon kind={item.kind} compact />
            {KIND_LABELS[item.kind]}
          </div>
          <p className="m-0 mt-1 text-body-sm text-muted-foreground">{item.detail}</p>
        </div>
      );
    },
  },
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
            {item.session_title || item.started_at || "Untitled session"}
          </p>
        </div>
      );
    },
  },
  {
    id: "project",
    accessorFn: (row) => row.project || "unknown",
    header: () => <HeaderLabel>Project</HeaderLabel>,
  },
  {
    id: "severity",
    accessorFn: (row) => row.severity,
    header: () => <HeaderLabel>Severity</HeaderLabel>,
    cell: ({ getValue }) => {
      const severity = getValue<"info" | "warning" | "critical">();
      return (
        <Badge variant={severity === "critical" ? "destructive" : "secondary"}>{severity}</Badge>
      );
    },
  },
  {
    id: "confidence",
    accessorFn: (row) => row.confidence,
    header: () => <HeaderLabel>Confidence</HeaderLabel>,
    cell: ({ getValue }) => {
      const confidence = getValue<"direct" | "inferred">();
      return (
        <Badge variant={confidence === "direct" ? "default" : "outline"}>{confidence}</Badge>
      );
    },
  },
  {
    id: "evidence",
    accessorFn: (row) => row.evidence,
    header: () => <HeaderLabel>Evidence</HeaderLabel>,
    cell: ({ getValue }) => {
      const items = getValue<string[]>();
      return (
        <ul className="m-0 grid min-w-[18rem] gap-1 p-0 text-body-sm text-muted-foreground">
          {items.map((item) => (
            <li key={item} className="list-none">
              {item}
            </li>
          ))}
        </ul>
      );
    },
    enableSorting: false,
  },
];

function ErrorTable({ rows }: { rows: ErrorCollectionItem[] }) {
  const table = useReactTable({
    data: rows,
    columns: errorColumns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Collected Errors</CardTitle>
        <CardDescription>Each row keeps the classifier evidence visible with direct or inferred confidence.</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          table={table}
          columnCount={errorColumns.length}
          emptyMessage="No collected errors for this scope."
          emptyHint="No errors collected in this period. Try expanding the date range."
        />
      </CardContent>
    </Card>
  );
}

function KindIcon({ kind, compact = false }: { kind: ErrorCollectionKind; compact?: boolean }) {
  const className = compact ? "size-4 text-primary" : "size-8 text-primary";
  if (kind === "abort_coding_session") {
    return <CircleSlash className={className} aria-hidden="true" />;
  }
  if (kind === "abrupt_coding_mid_session") {
    return <AlertTriangle className={className} aria-hidden="true" />;
  }
  return <ShieldAlert className={className} aria-hidden="true" />;
}
