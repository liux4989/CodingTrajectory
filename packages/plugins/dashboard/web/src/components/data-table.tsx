import * as React from "react";
import {
  flexRender,
  getExpandedRowModel,
  type RowData,
  type Table as ReactTable,
} from "@tanstack/react-table";
import { ChevronRight, Columns3, Download, Rows3 } from "lucide-react";
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

type Density = "compact" | "normal" | "comfortable";

const DENSITY_STYLES: Record<Density, { row: string; cell: string }> = {
  compact: { row: "h-8", cell: "py-1 px-2 text-caption" },
  normal: { row: "h-10", cell: "p-2 text-body-sm" },
  comfortable: { row: "h-14", cell: "p-3 text-body" },
};

const DENSITY_LABELS: Record<Density, string> = {
  compact: "Compact",
  normal: "Normal",
  comfortable: "Comfortable",
};

const DENSITY_STORAGE_KEY = "ct-table-density";

function readStoredDensity(): Density {
  if (typeof window === "undefined") return "normal";
  const raw = window.localStorage.getItem(DENSITY_STORAGE_KEY);
  return raw === "compact" || raw === "comfortable" ? raw : "normal";
}

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
  /** When provided, rows become expandable to reveal this detail panel. */
  renderRowDetail?: (row: TData) => React.ReactNode;
  /** When true, shows a density toggle button. */
  showDensityToggle?: boolean;
  /** When true, shows a CSV export button. */
  showExport?: boolean;
  /** File name for CSV export (without extension). */
  exportFilename?: string;
  /** When true, freezes the first column during horizontal scroll. */
  stickyFirstColumn?: boolean;
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
  renderRowDetail,
  showDensityToggle,
  showExport,
  exportFilename = "export",
  stickyFirstColumn,
}: DataTableProps<TData>) {
  const allColumns = table.getAllLeafColumns();
  const toggleableColumns = allColumns.filter((col) => col.getCanHide());
  const shouldShowToggle = showColumnToggle ?? toggleableColumns.length > 6;
  const hasToolbar = shouldShowToggle || showDensityToggle || showExport;

  const [density, setDensity] = React.useState<Density>(readStoredDensity);

  function setAndStoreDensity(next: Density) {
    setDensity(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(DENSITY_STORAGE_KEY, next);
    }
  }

  // Wire up expanded row model when row detail is provided
  React.useEffect(() => {
    if (renderRowDetail && !table.options.getExpandedRowModel) {
      table.setOptions((prev) => ({
        ...prev,
        getExpandedRowModel: getExpandedRowModel(),
        enableExpanding: true,
      }));
    }
  }, [renderRowDetail, table]);

  const densityStyle = DENSITY_STYLES[density];

  function exportCsv() {
    const visibleColumns = table.getVisibleFlatColumns();
    const headers = visibleColumns.map((col) => {
      const header = col.columnDef.header;
      return typeof header === "string" ? header : col.id;
    });
    const rows = table.getRowModel().rows.map((row) =>
      visibleColumns.map((col) => {
        const value = row.getValue(col.id);
        if (value == null) return "";
        if (typeof value === "object") return JSON.stringify(value);
        return String(value);
      })
    );
    const csv = [headers, ...rows]
      .map((line) => line.map((cell) => `"${cell.replace(/"/g, '""')}"`).join(","))
      .join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${exportFilename}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  const visibleColumnIds = table.getVisibleFlatColumns().map((c) => c.id);

  return (
    <div className="grid gap-0">
      {hasToolbar ? (
        <div className="flex flex-wrap items-center justify-between gap-2 pb-2">
          <div className="flex items-center gap-2">
            {showDensityToggle ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="outline" size="sm" className="gap-1.5">
                    <Rows3 size={14} />
                    {DENSITY_LABELS[density]}
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start">
                  <DropdownMenuLabel>Row density</DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  {(Object.keys(DENSITY_STYLES) as Density[]).map((d) => (
                    <DropdownMenuItem
                      key={d}
                      className={cn(density === d && "font-bold")}
                      onClick={() => setAndStoreDensity(d)}
                    >
                      {DENSITY_LABELS[d]}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
            {showExport ? (
              <Button variant="outline" size="sm" className="gap-1.5" onClick={exportCsv}>
                <Download size={14} />
                CSV
              </Button>
            ) : null}
          </div>
          {shouldShowToggle ? (
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
          ) : null}
        </div>
      ) : null}
      <div className={cn("overflow-auto rounded-2xl border border-border-soft bg-card/78", className)}>
        <Table>
          <TableHeader className={cn("sticky top-0 z-1 bg-table-head font-display text-caption uppercase", tableHeadClassName)}>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {renderRowDetail ? (
                  <TableHead key="expander" className="w-8" />
                ) : null}
                {headerGroup.headers.map((header, colIndex) => {
                  const isFirst = colIndex === 0 && stickyFirstColumn;
                  return (
                    <TableHead
                      key={header.id}
                      className={cn(
                        header.id === "select" ? "w-10" : undefined,
                        isFirst && "sticky left-0 z-2 bg-table-head",
                      )}
                    >
                      {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                    </TableHead>
                  );
                })}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.map((row) => {
              const isExpanded = renderRowDetail ? row.getIsExpanded() : false;
              return (
                <React.Fragment key={row.id}>
                  <TableRow
                    data-state={row.getIsSelected() && "selected"}
                    onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                    className={cn(
                      onRowClick ? "cursor-pointer" : undefined,
                      densityStyle.row,
                    )}
                  >
                    {renderRowDetail ? (
                      <TableCell key="expander" className="w-8 p-0">
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            row.toggleExpanded();
                          }}
                          className="flex size-6 items-center justify-center rounded text-muted-foreground transition-transform hover:text-foreground"
                          style={{ transform: isExpanded ? "rotate(90deg)" : "none" }}
                          aria-label={isExpanded ? "Collapse row" : "Expand row"}
                        >
                          <ChevronRight size={14} />
                        </button>
                      </TableCell>
                    ) : null}
                    {row.getVisibleCells().map((cell, colIndex) => {
                      const isFirst = colIndex === 0 && stickyFirstColumn;
                      return (
                        <TableCell
                          key={cell.id}
                          className={cn(densityStyle.cell, isFirst && "sticky left-0 z-2 bg-card")}
                        >
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </TableCell>
                      );
                    })}
                  </TableRow>
                  {isExpanded && renderRowDetail ? (
                    <TableRow key={`${row.id}-detail`}>
                      <TableCell
                        colSpan={visibleColumnIds.length + (renderRowDetail ? 1 : 0)}
                        className="bg-surface-subtle p-3"
                      >
                        {renderRowDetail(row.original)}
                      </TableCell>
                    </TableRow>
                  ) : null}
                </React.Fragment>
              );
            })}
            {!table.getRowModel().rows.length ? (
              <TableRow>
                <TableCell colSpan={columnCount + (renderRowDetail ? 1 : 0)}>
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
