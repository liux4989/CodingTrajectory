import * as React from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";

type SortDirection = "asc" | "desc";

type Props = {
  label: string;
  sortKey: string;
  currentSort: { key: string; direction: SortDirection } | null;
  onSort: (key: string) => void;
  className?: string;
};

export function SortableHeader({ label, sortKey, currentSort, onSort, className }: Props) {
  const isActive = currentSort?.key === sortKey;
  const direction = isActive ? currentSort!.direction : null;

  return (
    <button className={`sortable-header-btn ${isActive ? "is-active" : ""} ${className ?? ""}`} onClick={() => onSort(sortKey)}>
      {label}
      {direction === "asc" ? <ArrowUp size={14} /> : direction === "desc" ? <ArrowDown size={14} /> : <ArrowUpDown size={14} />}
    </button>
  );
}

export function useSort<T>(items: T[], getKey: (item: T, key: string) => string | number | null) {
  const [sort, setSort] = React.useState<{ key: string; direction: SortDirection } | null>(null);

  const handleSort = React.useCallback((key: string) => {
    setSort((current) => {
      if (current?.key === key) {
        return current.direction === "asc" ? { key, direction: "desc" } : null;
      }
      return { key, direction: "asc" };
    });
  }, []);

  const sorted = React.useMemo(() => {
    if (!sort) return items;
    return [...items].sort((a, b) => {
      const aVal = getKey(a, sort.key);
      const bVal = getKey(b, sort.key);
      if (aVal == null && bVal == null) return 0;
      if (aVal == null) return 1;
      if (bVal == null) return -1;
      const cmp = aVal < bVal ? -1 : aVal > bVal ? 1 : 0;
      return sort.direction === "asc" ? cmp : -cmp;
    });
  }, [items, sort, getKey]);

  return { sort, handleSort, sorted };
}
