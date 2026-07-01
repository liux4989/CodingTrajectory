import type { Table } from "@tanstack/react-table";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100] as const;

type DataTablePaginationProps<TData> = {
  table: Table<TData>;
  pageSizeOptions?: readonly number[];
};

export function DataTablePagination<TData>({
  table,
  pageSizeOptions = PAGE_SIZE_OPTIONS,
}: DataTablePaginationProps<TData>) {
  const { pageIndex, pageSize } = table.getState().pagination;
  const pageCount = table.getPageCount();
  const totalRows = table.getFilteredRowModel().rows.length;
  const firstRow = totalRows === 0 ? 0 : pageIndex * pageSize + 1;
  const lastRow = Math.min(totalRows, (pageIndex + 1) * pageSize);
  const canPrevious = pageIndex > 0;
  const canNext = pageIndex + 1 < pageCount;

  if (pageCount <= 1 && totalRows <= pageSizeOptions[0]) {
    return (
      <div className="flex items-center justify-between px-2 py-2 font-display text-body-sm text-muted-foreground">
        <span>
          {totalRows} {totalRows === 1 ? "row" : "rows"}
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center justify-between gap-4 px-2 py-2 font-display text-body-sm">
      <span className="text-muted-foreground">
        {firstRow}–{lastRow} of {totalRows}
      </span>

      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground">Rows per page</span>
          <Select
            value={String(pageSize)}
            onValueChange={(value) => {
              table.setPageSize(Number(value));
            }}
          >
            <SelectTrigger size="sm" className="h-8 w-16">
              <SelectValue />
            </SelectTrigger>
            <SelectContent side="top">
              {pageSizeOptions.map((size) => (
                <SelectItem key={size} value={String(size)}>
                  {size}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={!canPrevious}
            onClick={() => table.previousPage()}
            aria-label="Previous page"
          >
            <ChevronLeft size={16} />
          </Button>
          <span className="px-2 font-bold text-muted-foreground">
            Page {pageIndex + 1} of {pageCount}
          </span>
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={!canNext}
            onClick={() => table.nextPage()}
            aria-label="Next page"
          >
            <ChevronRight size={16} />
          </Button>
        </div>
      </div>
    </div>
  );
}
