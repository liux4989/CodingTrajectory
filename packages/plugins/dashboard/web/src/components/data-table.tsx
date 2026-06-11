import * as React from "react";
import {
  flexRender,
  type RowData,
  type Table as ReactTable,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

type SortableHeaderProps = {
  column: {
    getIsSorted: () => false | "asc" | "desc";
    toggleSorting: () => void;
  };
  label: string;
};

export function SortableHeader({ column, label }: SortableHeaderProps) {
  const sorted = column.getIsSorted();
  return (
    <button
      className="inline-flex cursor-pointer items-center gap-1.5 border-none bg-transparent p-0 font-extrabold uppercase text-foreground hover:text-primary"
      onClick={() => column.toggleSorting()}
    >
      {label}
      {sorted === "asc" ? <ArrowUp size={14} /> : sorted === "desc" ? <ArrowDown size={14} /> : <ArrowUpDown size={14} />}
    </button>
  );
}

type DataTableProps<TData extends RowData> = {
  table: ReactTable<TData>;
  columnCount: number;
  emptyMessage: string;
  className?: string;
  tableHeadClassName?: string;
};

export function DataTable<TData extends RowData>({
  table,
  columnCount,
  emptyMessage,
  className,
  tableHeadClassName,
}: DataTableProps<TData>) {
  return (
    <div className={cn("overflow-auto rounded-2xl border border-foreground/13 bg-card/78 dark:border-border-subtle", className)}>
      <Table>
        <TableHead className={cn("sticky top-0 z-1 bg-table-head font-display text-caption uppercase", tableHeadClassName)}>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <TableHeader key={header.id} className={header.id === "select" ? "w-10" : undefined}>
                  {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                </TableHeader>
              ))}
            </TableRow>
          ))}
        </TableHead>
        <TableBody>
          {table.getRowModel().rows.map((row) => (
            <TableRow key={row.id} data-state={row.getIsSelected() && "selected"}>
              {row.getVisibleCells().map((cell) => (
                <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
              ))}
            </TableRow>
          ))}
          {!table.getRowModel().rows.length ? (
            <TableRow>
              <TableCell colSpan={columnCount}>{emptyMessage}</TableCell>
            </TableRow>
          ) : null}
        </TableBody>
      </Table>
    </div>
  );
}

export function TablePagination<TData extends RowData>({ table }: { table: ReactTable<TData> }) {
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
