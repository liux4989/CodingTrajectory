import * as React from "react";
import { MetricSkeleton, TableSkeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/route-header";

type LoadingShellProps = {
  eyebrow: string;
  title: string;
  /** Which skeleton layout to show. Defaults to "mixed" (metrics + table). */
  variant?: "metrics" | "table" | "mixed";
  /** Number of metric skeleton cards for "metrics"/"mixed" variants. */
  metricCount?: number;
  /** Number of table rows for "table"/"mixed" variants. */
  tableRows?: number;
  /** Number of table columns for "table"/"mixed" variants. */
  tableCols?: number;
  detail?: string;
};

/**
 * Unified loading surface for any route awaiting data. Renders a route header
 * plus the appropriate skeleton combination so every route shares the same
 * loading rhythm.
 */
export function LoadingShell({
  eyebrow,
  title,
  variant = "mixed",
  metricCount = 4,
  tableRows = 6,
  tableCols = 4,
  detail,
}: LoadingShellProps) {
  return (
    <div className="route-container">
      <PageHeader eyebrow={eyebrow} title={title} />
      {detail ? <p className="m-0 text-body-sm text-muted-foreground">{detail}</p> : null}
      {variant === "metrics" || variant === "mixed" ? (
        <section className="stat-grid" aria-label="Loading metrics">
          {Array.from({ length: metricCount }, (_, i) => (
            <MetricSkeleton key={i} />
          ))}
        </section>
      ) : null}
      {variant === "table" || variant === "mixed" ? (
        <TableSkeleton rows={tableRows} cols={tableCols} />
      ) : null}
    </div>
  );
}
