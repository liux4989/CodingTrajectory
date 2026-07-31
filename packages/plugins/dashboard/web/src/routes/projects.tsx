import * as React from "react";
import { Link, useParams, useSearch } from "@tanstack/react-router";
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
import { relativeTime } from "@/lib/relative-time";
import { Button } from "@/components/ui/button";

function sessionId(item: SessionItem) {
  return item.root_session_id ?? item.id ?? null;
}

function sessionVendors(item: SessionItem) {
  return item.vendors ?? item.v ?? [];
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
  {
    id: "updated",
    accessorFn: (row) => row.updated_at ?? row.started_at ?? "",
    header: ({ column }) => <DataTableColumnHeader column={column} label="Updated" />,
    cell: ({ row }) => (
      <span className="mono text-body-sm" title={row.original.updated_at ?? row.original.started_at ?? ""}>
        {relativeTime(row.original.updated_at ?? row.original.started_at)}
      </span>
    ),
  },
];

export function ProjectDetailRoute() {
  const { projectName } = useParams({ from: "/projects/$projectName" });
  const { sinceDays: urlSinceDays } = useSearch({ from: "/projects/$projectName" });
  const { days: rangeDays } = useDateRange();
  const sinceDays = urlSinceDays ?? rangeDays;
  const [filter, setFilter] = React.useState("");
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const detail = useQuery({
    queryKey: ["project", projectName, sinceDays],
    queryFn: () => fetchProjectDetail(projectName, sinceDays),
    placeholderData: (previous) => previous,
  });
  const data = detail.data?.sessions ?? [];

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
        <VendorBadges vendors={detail.data?.vendors ?? []} />
        {detail.data?.path ? (
          <span className="mono text-body-sm text-muted-foreground">{detail.data.path}</span>
        ) : null}
        <span className="text-body-sm text-muted-foreground">
          {detail.data?.session_count ?? 0} session(s) from the last {sinceDays} day{sinceDays === 1 ? "" : "s"}
        </span>
      </div>
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter sessions by title, vendor, or id" />
      {detail.isPending ? <TableSkeleton rows={6} cols={4} /> : null}
      {detail.isError ? <StateBlock title="Project detail failed" detail={detail.error.message} /> : null}
      {detail.data ? (
        <>
          <DataTable table={table} columnCount={columns.length} emptyMessage="No sessions found for this project." />
          <DataTablePagination table={table} />
        </>
      ) : null}
    </div>
  );
}
