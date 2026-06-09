import * as React from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight } from "lucide-react";
import { fetchSessions, type SessionItem } from "@/api";
import { TableSkeleton } from "@/components/ui/skeleton";
import { RouteHeader } from "@/components/route-header";
import { Toolbar } from "@/components/toolbar";
import { StateBlock } from "@/components/state-block";
import { VendorBadges } from "@/components/badges";
import { RefreshButton } from "@/components/refresh-button";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { relativeTime } from "@/lib/relative-time";

function sessionId(item: SessionItem) {
  return item.root_session_id ?? item.id ?? null;
}

function sessionVendors(item: SessionItem) {
  return item.vendors ?? item.v ?? [];
}

function shortId(value: string | null | undefined) {
  if (!value) return "-";
  return value.length > 12 ? value.slice(0, 12) : value;
}

function SortableButton({ header, label }: { header: { column: { getIsSorted: () => false | "asc" | "desc"; toggleSorting: () => void } }; label: string }) {
  const sorted = header.column.getIsSorted();
  return (
    <button
      className="inline-flex cursor-pointer items-center gap-1.5 border-none bg-transparent p-0 font-extrabold uppercase tracking-[0.08em] text-foreground hover:text-primary"
      onClick={() => header.column.toggleSorting()}
    >
      {label}
      {sorted === "asc" ? <ArrowUp size={14} /> : sorted === "desc" ? <ArrowDown size={14} /> : <ArrowUpDown size={14} />}
    </button>
  );
}

const columns: ColumnDef<SessionItem>[] = [
  {
    id: "session",
    header: () => <span className="font-extrabold uppercase tracking-[0.08em]">Session</span>,
    cell: ({ row }) => {
      const id = sessionId(row.original);
      return (
        <span className="font-mono text-[0.88rem]">
          {id ? (
            <Link
              to="/sessions/$sessionId/context-window"
              params={{ sessionId: id }}
              className="font-display font-extrabold text-primary decoration-[0.08em] underline-offset-[0.2em]"
            >
              {shortId(id)}
            </Link>
          ) : "-"}
        </span>
      );
    },
    enableSorting: false,
  },
  {
    id: "vendors",
    accessorFn: (row) => sessionVendors(row).join(", "),
    header: ({ column }) => <SortableButton header={{ column }} label="Vendors" />,
    cell: ({ row }) => <VendorBadges vendors={sessionVendors(row.original)} />,
  },
  {
    accessorKey: "title",
    header: ({ column }) => <SortableButton header={{ column }} label="Title" />,
    cell: ({ getValue }) => getValue<string | null>() ?? "-",
  },
  {
    id: "updated",
    accessorFn: (row) => row.updated_at ?? row.started_at ?? "",
    header: ({ column }) => <SortableButton header={{ column }} label="Updated" />,
    cell: ({ row }) => (
      <span className="font-mono text-[0.88rem]" title={row.original.updated_at ?? row.original.started_at ?? ""}>
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
          <div className="overflow-auto rounded-[1.2rem] border border-foreground/13 bg-card/78 dark:border-[rgb(255_255_255/8%)]">
            <Table>
              <TableHead className="sticky top-0 z-1 bg-[#eee0bd] font-display text-[0.8rem] uppercase tracking-[0.08em] dark:bg-[#2a2620]">
                {table.getHeaderGroups().map((headerGroup) => (
                  <TableRow key={headerGroup.id}>
                    {headerGroup.headers.map((header) => (
                      <TableHeader key={header.id}>
                        {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                      </TableHeader>
                    ))}
                  </TableRow>
                ))}
              </TableHead>
              <TableBody>
                {table.getRowModel().rows.map((row) => (
                  <TableRow key={row.id}>
                    {row.getVisibleCells().map((cell) => (
                      <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
                    ))}
                  </TableRow>
                ))}
                {!table.getRowModel().rows.length ? (
                  <TableRow><TableCell colSpan={columns.length}>No sessions match the current filter.</TableCell></TableRow>
                ) : null}
              </TableBody>
            </Table>
          </div>
          <TablePagination table={table} />
        </>
      ) : null}
    </div>
  );
}

function TablePagination({ table }: { table: ReturnType<typeof useReactTable<SessionItem>> }) {
  const pageCount = table.getPageCount();
  if (pageCount <= 1) return null;
  const page = table.getState().pagination.pageIndex;

  return (
    <div className="flex items-center justify-center gap-4 py-2">
      <Button variant="ghost" size="sm" disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()}>
        <ChevronLeft size={16} /> Prev
      </Button>
      <span className="font-display text-[0.88rem] font-bold text-muted-foreground">
        Page {page + 1} of {pageCount}
      </span>
      <Button variant="ghost" size="sm" disabled={!table.getCanNextPage()} onClick={() => table.nextPage()}>
        Next <ChevronRight size={16} />
      </Button>
    </div>
  );
}
