import * as React from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

type PaginationProps = {
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
};

export function Pagination({ page, totalPages, onPageChange }: PaginationProps) {
  if (totalPages <= 1) return null;

  return (
    <div className="pagination">
      <button className="button button-ghost button-size-sm" disabled={page === 0} onClick={() => onPageChange(page - 1)}>
        <ChevronLeft size={16} /> Prev
      </button>
      <span className="pagination-info">
        Page {page + 1} of {totalPages}
      </span>
      <button className="button button-ghost button-size-sm" disabled={page >= totalPages - 1} onClick={() => onPageChange(page + 1)}>
        Next <ChevronRight size={16} />
      </button>
    </div>
  );
}
