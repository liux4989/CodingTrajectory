import type { Column } from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { cn } from "@/lib/utils";

type DataTableColumnHeaderProps<TData, TValue> = {
  column: Column<TData, TValue>;
  label: string;
  className?: string;
};

export function DataTableColumnHeader<TData, TValue>({
  column,
  label,
  className,
}: DataTableColumnHeaderProps<TData, TValue>) {
  if (!column.getCanSort()) {
    return <span className={cn("font-extrabold uppercase text-foreground", className)}>{label}</span>;
  }

  const sorted = column.getIsSorted();

  return (
    <button
      type="button"
      className={cn(
        "inline-flex cursor-pointer items-center gap-1.5 border-none bg-transparent p-0 font-extrabold uppercase text-foreground hover:text-primary",
        className,
      )}
      onClick={() => column.toggleSorting(sorted === "asc" ? false : undefined)}
    >
      {label}
      {sorted === "asc" ? (
        <ArrowUp size={14} />
      ) : sorted === "desc" ? (
        <ArrowDown size={14} />
      ) : (
        <ArrowUpDown size={14} />
      )}
    </button>
  );
}
