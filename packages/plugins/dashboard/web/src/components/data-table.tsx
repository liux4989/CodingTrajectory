import * as React from "react";
import {
  flexRender,
  type RowData,
  type Table as ReactTable,
} from "@tanstack/react-table";
import { Columns3 } from "lucide-react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

type DataTableProps<TData extends RowData> = {
  table: ReactTable<TData>;
  columnCount: number;
  emptyMessage: string;
  /** Optional hint text shown in the empty state. */
  emptyHint?: string;
  className?: string;
  tableHeadClassName?: string;
  onRowClick?: (row: TData) => void;
  /** When true, shows a column-visibility dropdown (default: auto, shows when > 6 columns). */
  showColumnToggle?: boolean;
};

export function DataTable<TData extends RowData>({
  table,
  columnCount,
  emptyMessage,
  emptyHint,
  className,
  tableHeadClassName,
  onRowClick,
  showColumnToggle,
}: DataTableProps<TData>) {
  const allColumns = table.getAllLeafColumns();
  const toggleableColumns = allColumns.filter((col) => col.getCanHide());
  const shouldShowToggle = showColumnToggle ?? toggleableColumns.length > 6;

  return (
    <div className="grid gap-0">
      {shouldShowToggle ? (
        <div className="flex justify-end pb-2">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" className="gap-1.5">
                <Columns3 size={14} />
                Columns
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-48">
              <DropdownMenuLabel>Toggle columns</DropdownMenuLabel>
              <DropdownMenuSeparator />
              {toggleableColumns.map((column) => (
                <DropdownMenuItem
                  key={column.id}
                  className="capitalize"
                  onSelect={(e) => e.preventDefault()}
                  onClick={() => column.toggleVisibility()}
                >
                  <input
                    type="checkbox"
                    checked={column.getIsVisible()}
                    readOnly
                    className="mr-2 size-3.5"
                  />
                  {typeof column.columnDef.header === "string"
                    ? column.columnDef.header
                    : column.id.replace(/_/g, " ")}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      ) : null}
      <div className={cn("overflow-auto rounded-2xl border border-border-soft bg-card/78", className)}>
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
              <TableRow
                key={row.id}
                data-state={row.getIsSelected() && "selected"}
                onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                className={onRowClick ? "cursor-pointer" : undefined}
              >
                {row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
                ))}
              </TableRow>
            ))}
            {!table.getRowModel().rows.length ? (
              <TableRow>
                <TableCell colSpan={columnCount}>
                  <div className="grid gap-1 py-2 text-center">
                    <span>{emptyMessage}</span>
                    {emptyHint ? (
                      <span className="text-caption text-muted-foreground">{emptyHint}</span>
                    ) : null}
                  </div>
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
