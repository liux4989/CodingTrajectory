import * as React from "react";
import { useQuery } from "@tanstack/react-query";
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
import { RefreshButton } from "@/components/refresh-button";
import { DataTable } from "@/components/data-table";
import { DataTableColumnHeader } from "@/components/ui/data-table-column-header";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { SessionLink } from "@/components/session-link";
import { relativeTime } from "@/lib/relative-time";

function sessionId(item: SessionItem) {
  return item.root_session_id ?? item.id ?? null;
}

function sessionVendors(item: SessionItem) {
  return item.vendors ?? item.v ?? [];
}

const columns: ColumnDef<SessionItem>[] = [
  {
    id: "session",
    header: () => <span className="font-extrabold uppercase tracking-wide">Session</span>,
    cell: ({ row }) => (
      <span className="font-mono text-body-sm">
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
  {
    id: "updated",
    accessorFn: (row) => row.updated_at ?? row.started_at ?? "",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Updated" />,
    cell: ({ row }) => (
      <span className="font-mono text-body-sm" title={row.original.updated_at ?? row.original.started_at ?? ""}>
        {relativeTime(row.original.updated_at ?? row.original.started_at)}
      </span>
    ),
  },
];

export function SessionsRoute() {
  const [filter, setFilter] = React.useState("");
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: fetchSessions });
  const data = sessions.data?.items ?? [];

  const table = useReactTable({
    data,
    columns,
    state: { globalFilter: filter, sorting },
    onGlobalFilterChange: setFilter,
    onSortingChange: setSorting,
    globalFilterFn: (row, _columnId, filterValue: string) => {
      const term = filterValue.toLowerCase();
      const item = row.original;
      return `${sessionId(item)} ${item.title ?? ""} ${sessionVendors(item).join(" ")} ${item.project_name ?? ""}`.toLowerCase().includes(term);
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 20 } },
  });

  return (
    <div className="mx-auto grid max-w-[96rem] gap-5">
      <RouteHeader eyebrow="Session stream" title="Recent session entry points, kept compact for triage." action={<RefreshButton queries={["sessions"]} />} />
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter sessions by title, vendor, project, or id" />
      {sessions.isPending ? <TableSkeleton rows={6} cols={4} /> : null}
      {sessions.isError ? <StateBlock title="Session scan failed" detail={sessions.error.message} /> : null}
      {sessions.data ? (
        <>
          <DataTable table={table} columnCount={columns.length} emptyMessage="No sessions match the current filter." />
          <DataTablePagination table={table} />
        </>
      ) : null}
    </div>
  );
}
