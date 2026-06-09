import * as React from "react";
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
import { fetchProjects, type ProjectItem } from "@/api";
import { TableSkeleton } from "@/components/ui/skeleton";
import { RouteHeader } from "@/components/route-header";
import { Toolbar } from "@/components/toolbar";
import { StateBlock } from "@/components/state-block";
import { VendorBadges } from "@/components/badges";
import { RefreshButton } from "@/components/refresh-button";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

function SortableButton({ header, label }: { header: { column: { getIsSorted: () => false | "asc" | "desc"; toggleSorting: () => void } }; label: string }) {
  const sorted = header.column.getIsSorted();
  return (
    <button
      className="inline-flex cursor-pointer items-center gap-1.5 border-none bg-transparent p-0 font-extrabold uppercase tracking-wide text-foreground hover:text-primary"
      onClick={() => header.column.toggleSorting()}
    >
      {label}
      {sorted === "asc" ? <ArrowUp size={14} /> : sorted === "desc" ? <ArrowDown size={14} /> : <ArrowUpDown size={14} />}
    </button>
  );
}

const columns: ColumnDef<ProjectItem>[] = [
  {
    accessorKey: "name",
    header: ({ column }) => <SortableButton header={{ column }} label="Project" />,
    cell: ({ getValue }) => <span className="font-bold">{getValue<string>()}</span>,
  },
  {
    accessorKey: "vendors",
    header: ({ column }) => <SortableButton header={{ column }} label="Vendors" />,
    cell: ({ getValue }) => <VendorBadges vendors={getValue<string[]>()} />,
    sortingFn: (a, b) => a.original.vendors.join(", ").localeCompare(b.original.vendors.join(", ")),
  },
  {
    accessorKey: "path",
    header: ({ column }) => <SortableButton header={{ column }} label="Path" />,
    cell: ({ getValue }) => <span className="font-mono text-body-sm">{getValue<string | null>() ?? "-"}</span>,
  },
];

export function ProjectsRoute() {
  const [filter, setFilter] = React.useState("");
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const projects = useQuery({ queryKey: ["projects"], queryFn: fetchProjects });
  const data = projects.data?.items ?? [];

  const table = useReactTable({
    data,
    columns,
    state: { globalFilter: filter, sorting },
    onGlobalFilterChange: setFilter,
    onSortingChange: setSorting,
    globalFilterFn: (row, _columnId, filterValue: string) => {
      const term = filterValue.toLowerCase();
      const item = row.original;
      return `${item.name} ${item.path ?? ""} ${item.vendors.join(" ")}`.toLowerCase().includes(term);
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 20 } },
  });

  return (
    <div className="mx-auto grid max-w-[96rem] gap-5">
      <RouteHeader eyebrow="Project inventory" title="Browse discovered project metadata without exposing raw log paths." action={<RefreshButton queries={["projects"]} />} />
      <Toolbar value={filter} onChange={setFilter} placeholder="Filter projects by name, vendor, or path" />
      {projects.isPending ? <TableSkeleton rows={6} cols={3} /> : null}
      {projects.isError ? <StateBlock title="Project scan failed" detail={projects.error.message} /> : null}
      {projects.data ? (
        <>
          <div className="overflow-auto rounded-2xl border border-foreground/13 bg-card/78 dark:border-border-subtle">
            <Table>
              <TableHead className="sticky top-0 z-1 bg-table-head font-display text-caption uppercase tracking-wide">
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
                  <TableRow><TableCell colSpan={columns.length}>No projects match the current filter.</TableCell></TableRow>
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

function TablePagination({ table }: { table: ReturnType<typeof useReactTable<ProjectItem>> }) {
  const pageCount = table.getPageCount();
  if (pageCount <= 1) return null;
  const page = table.getState().pagination.pageIndex;

  return (
    <div className="flex items-center justify-center gap-4 py-2">
      <Button variant="ghost" size="sm" disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()}>
        <ChevronLeft size={16} /> Prev
      </Button>
      <span className="font-display text-body-sm font-bold text-muted-foreground">
        Page {page + 1} of {pageCount}
      </span>
      <Button variant="ghost" size="sm" disabled={!table.getCanNextPage()} onClick={() => table.nextPage()}>
        Next <ChevronRight size={16} />
      </Button>
    </div>
  );
}
