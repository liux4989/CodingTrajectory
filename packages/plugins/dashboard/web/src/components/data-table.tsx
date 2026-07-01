import * as React from "react";
import {
  flexRender,
  type RowData,
  type Table as ReactTable,
} from "@tanstack/react-table";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

import { cn } from "@/lib/utils";

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
        <TableHeader className={cn("sticky top-0 z-1 bg-table-head font-display text-caption uppercase", tableHeadClassName)}>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <TableHead key={header.id} className={header.id === "select" ? "w-10" : undefined}>
                  {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
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
