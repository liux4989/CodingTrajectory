import * as React from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useNavigate, useRouter, useSearch } from "@tanstack/react-router";
import { X } from "lucide-react";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { fetchSessions, type SessionItem } from "@/api";
import { TableSkeleton } from "@/components/ui/skeleton";
import { RouteHeader } from "@/components/route-header";
import { Toolbar } from "@/components/toolbar";
import { StateBlock } from "@/components/state-block";
import { VendorBadges } from "@/components/badges";
import { DataTable } from "@/components/data-table";
import { DataTableColumnHeader } from "@/components/ui/data-table-column-header";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { SessionLink } from "@/components/session-link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

const SESSION_WINDOW_DAYS = 7;
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
    id: "branch",
    accessorFn: (row) =>
      (row.lineage_root_session_id ?? row.root_session_id) === row.root_session_id
        ? "Root"
        : "Fork",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Branch" />,
    cell: ({ getValue }) => (
      <Badge variant="outline">{getValue<string>()}</Badge>
    ),
  },
  {
    id: "agents",
    accessorFn: (row) => Math.max(row.session_ids.length - 1, 0),
    header: ({ column }) => <DataTableColumnHeader column={column} label="Agents" />,
    cell: ({ getValue }) => (
      <span className="tabular-nums">{getValue<number>().toLocaleString()}</span>
    ),
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
    cell: ({ getValue }) => {
      const value = getValue<string | null>();
      if (!value) return "-";
      return (
        <span className="max-w-[28ch] truncate inline-block" title={value}>
          {value}
        </span>
      );
    },
  },
];

export function SessionsRoute() {
  const [filter, setFilter] = React.useState("");
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const router = useRouter();
  const navigate = useNavigate({ from: "/sessions" });
  const { projectName } = useSearch({ from: "/sessions" });
  const sessions = useInfiniteQuery({
    queryKey: ["sessions", "cursor", SESSION_WINDOW_DAYS, projectName ?? null],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      fetchSessions({
        sinceDays: SESSION_WINDOW_DAYS,
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

  const table = useReactTable({
    data,
    columns,
    state: { globalFilter: filter, sorting },
    onGlobalFilterChange: setFilter,
    onSortingChange: setSorting,
    globalFilterFn: (row, _columnId, filterValue: string) => {
      const term = filterValue.toLowerCase();
      const item = row.original;
      return `${sessionId(item)} ${item.title ?? ""} ${sessionVendors(item).join(" ")} ${item.project ?? ""}`.toLowerCase().includes(term);
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 20 } },
  });

  return (
    <div className="route-container">
      <RouteHeader eyebrow="Orchestration runs" title="Conversation branches and their owned agent runs." />
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
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter sessions by title, vendor, project, or id" />
      {sessions.isPending ? <TableSkeleton rows={6} cols={3} /> : null}
      {sessions.isError ? <StateBlock title="Session scan failed" detail={sessions.error.message} onRetry={() => sessions.refetch()} /> : null}
      {sessions.data ? (
        <>
          <DataTable
            table={table}
            columnCount={columns.length}
            emptyMessage="No sessions match the current filter."
            emptyHint="Try adjusting the filter."
            onRowClick={(item) => {
              const id = sessionId(item);
              if (id) router.navigate({ to: "/sessions/$sessionId", params: { sessionId: id } });
            }}
          />
          <DataTablePagination table={table} />
          <div className="flex flex-wrap items-center justify-between gap-2 px-2 pb-2 text-body-sm text-muted-foreground">
            <span>
              {data.length.toLocaleString()} session{data.length === 1 ? "" : "s"} loaded from the last {SESSION_WINDOW_DAYS} days
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
