import * as React from "react";
import { Link, useParams, useSearch } from "@tanstack/react-router";
import { useInfiniteQuery } from "@tanstack/react-query";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { FolderGit2, Gauge } from "lucide-react";
import { fetchProjectDetail, type SessionItem } from "@/api";
import { TableSkeleton } from "@/components/ui/skeleton";
import { RouteHeader } from "@/components/route-header";
import { Toolbar } from "@/components/toolbar";
import { StateBlock } from "@/components/state-block";
import { VendorBadges } from "@/components/badges";
import { DataTable } from "@/components/data-table";
import { DataTableColumnHeader } from "@/components/ui/data-table-column-header";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { SessionLink } from "@/components/session-link";
import { useDateRange } from "@/hooks/use-date-range";
import { Button } from "@/components/ui/button";

const CURSOR_PAGE_SIZE = 50;

function sessionId(item: SessionItem) {
  return item.root_session_id;
}

function sessionVendors(item: SessionItem) {
  return item.vendors;
}

const columns: ColumnDef<SessionItem>[] = [
  {
    id: "session",
    header: () => <span className="label-uppercase">Session</span>,
    cell: ({ row }) => (
      <span className="mono text-body-sm">
        <SessionLink sessionId={sessionId(row.original)} />
      </span>
    ),
    enableSorting: false,
  },
  {
    id: "vendors",
    accessorFn: (row) => sessionVendors(row).join(", "),
    header: ({ column }) => <DataTableColumnHeader column={column} label="Vendors" />,
    cell: ({ row }) => <VendorBadges vendors={sessionVendors(row.original)} />,
  },
  {
    accessorKey: "title",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Title" />,
    cell: ({ getValue }) => getValue<string | null>() ?? "-",
  },
];

export function ProjectDetailRoute() {
  const { projectName } = useParams({ from: "/projects/$projectName" });
  const { sinceDays: urlSinceDays } = useSearch({ from: "/projects/$projectName" });
  const { days: rangeDays } = useDateRange();
  const sinceDays = urlSinceDays ?? rangeDays;
  const [filter, setFilter] = React.useState("");
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const detail = useInfiniteQuery({
    queryKey: ["project", projectName, sinceDays, "cursor"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      fetchProjectDetail(projectName, sinceDays, {
        cursor: pageParam ?? undefined,
        limit: CURSOR_PAGE_SIZE,
        signal,
      }),
    getNextPageParam: (lastPage) => lastPage.page?.next_cursor ?? undefined,
    placeholderData: (previous) => previous,
  });
  const project = detail.data?.pages[0];
  const data = React.useMemo(() => {
    const byId = new Map<string, SessionItem>();
    for (const page of detail.data?.pages ?? []) {
      for (const item of page.sessions) byId.set(sessionId(item), item);
    }
    return [...byId.values()];
  }, [detail.data]);

  const table = useReactTable({
    data,
    columns,
    state: { globalFilter: filter, sorting },
    onGlobalFilterChange: setFilter,
    onSortingChange: setSorting,
    globalFilterFn: (row, _columnId, filterValue: string) => {
      const term = filterValue.toLowerCase();
      const item = row.original;
      return `${sessionId(item)} ${item.title ?? ""} ${sessionVendors(item).join(" ")}`.toLowerCase().includes(term);
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 20 } },
  });

  return (
    <div className="route-container">
      <RouteHeader
        eyebrow="Project drill-down"
        title={projectName}
        action={
          <Button asChild variant="outline">
            <Link
              to="/token-efficiency/$projectName"
              params={{ projectName }}
              search={{ grain: "weekly", unit: "session" }}
            >
              <Gauge data-icon="inline-start" />
              Token efficiency
            </Link>
          </Button>
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <FolderGit2 size={16} className="text-muted-foreground" />
        <VendorBadges vendors={project?.vendors ?? []} />
        {project?.path ? (
          <span className="mono text-body-sm text-muted-foreground">{project.path}</span>
        ) : null}
        <span className="text-body-sm text-muted-foreground">
          {project?.session_count ?? 0} session(s) from the last {sinceDays} day{sinceDays === 1 ? "" : "s"}
        </span>
      </div>
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter sessions by title, vendor, or id" />
      {detail.isPending ? <TableSkeleton rows={6} cols={3} /> : null}
      {detail.isError ? <StateBlock title="Project detail failed" detail={detail.error.message} onRetry={() => detail.refetch()} /> : null}
      {detail.data ? (
        <>
          <DataTable table={table} columnCount={columns.length} emptyMessage="No sessions found for this project." />
          <DataTablePagination table={table} />
          <div className="flex flex-wrap items-center justify-between gap-2 px-2 pb-2 text-body-sm text-muted-foreground">
            <span>
              {data.length.toLocaleString()} of {(project?.session_count ?? data.length).toLocaleString()} sessions loaded
            </span>
            {detail.hasNextPage ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={detail.isFetchingNextPage}
                onClick={() => void detail.fetchNextPage()}
              >
                {detail.isFetchingNextPage ? "Loading…" : `Load ${CURSOR_PAGE_SIZE} more`}
              </Button>
            ) : null}
          </div>
          {detail.isFetchNextPageError ? (
            <StateBlock
              title="More project sessions could not be loaded"
              detail={detail.error.message}
              onRetry={() => void detail.fetchNextPage()}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
