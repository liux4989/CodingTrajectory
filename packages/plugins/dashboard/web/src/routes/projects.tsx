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
import { fetchProjects, type ProjectItem } from "@/api";
import { TableSkeleton } from "@/components/ui/skeleton";
import { RouteHeader } from "@/components/route-header";
import { Toolbar } from "@/components/toolbar";
import { StateBlock } from "@/components/state-block";
import { VendorBadges } from "@/components/badges";
import { RefreshButton } from "@/components/refresh-button";
import { DataTable, SortableHeader, TablePagination } from "@/components/data-table";

const columns: ColumnDef<ProjectItem>[] = [
  {
    accessorKey: "name",
    header: ({ column }) => <SortableHeader column={column} label="Project" />,
    cell: ({ getValue }) => <span className="font-bold">{getValue<string>()}</span>,
  },
  {
    accessorKey: "vendors",
    header: ({ column }) => <SortableHeader column={column} label="Vendors" />,
    cell: ({ getValue }) => <VendorBadges vendors={getValue<string[]>()} />,
    sortingFn: (a, b) => a.original.vendors.join(", ").localeCompare(b.original.vendors.join(", ")),
  },
  {
    accessorKey: "path",
    header: ({ column }) => <SortableHeader column={column} label="Path" />,
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
          <DataTable table={table} columnCount={columns.length} emptyMessage="No projects match the current filter." />
          <TablePagination table={table} />
        </>
      ) : null}
    </div>
  );
}
