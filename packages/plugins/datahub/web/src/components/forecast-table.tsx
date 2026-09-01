import type { ForecastRecord } from "@/api";
import { ForecastKindBadge } from "@/components/forecast-kind-badge";
import { ResponsiveDataList } from "@/components/responsive-data-list";

function formatMinutes(minutes: number | null | undefined): string {
  if (minutes === undefined || minutes === null) return "-";
  if (minutes < 60) return `${Math.round(minutes)}m`;
  return `${(minutes / 60).toFixed(1)}h`;
}

function formatRatio(record: ForecastRecord): string {
  const actual = record.comparison?.actual_execution_seconds;
  if (!record.p50_minutes || !actual || actual <= 0) return "-";
  return `${(record.p50_minutes / (actual / 60)).toFixed(2)}x`;
}

function formatIssuedAt(iso: string | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Individual forecast records. These are raw artifacts behind the aggregate
 * cohorts — one row is never a performance conclusion.
 */
export function ForecastTable({ forecasts }: { forecasts: ForecastRecord[] }) {
  if (!forecasts.length) {
    return (
      <p className="py-8 text-center text-caption text-muted-foreground">
        No forecasts found for the current filters.
      </p>
    );
  }

  return (
    <ResponsiveDataList table={<div className="overflow-x-auto">
      <table className="w-full text-body-sm">
        <thead>
          <tr className="border-b border-border bg-table-head text-left text-eyebrow font-display uppercase tracking-wider text-muted-foreground">
            <th className="px-4 py-2">ID</th>
            <th className="px-4 py-2">Kind</th>
            <th className="px-4 py-2">Project</th>
            <th className="px-4 py-2">Target</th>
            <th className="px-4 py-2 text-right">p50</th>
            <th className="px-4 py-2 text-right">p80</th>
            <th className="px-4 py-2 text-right">Actual</th>
            <th className="px-4 py-2 text-right">Ratio</th>
            <th className="px-4 py-2">Status</th>
            <th className="px-4 py-2">Issued</th>
          </tr>
        </thead>
        <tbody>
          {forecasts.map((record) => (
            <tr key={record.prediction_id} className="border-b border-border-subtle hover:bg-accent/30">
              <td className="px-4 py-2">
                <code className="text-xs">{record.prediction_id.slice(0, 8)}</code>
                {record.role === "diagnostic" && (
                  <span className="ml-1 text-caption text-muted-foreground">(diag)</span>
                )}
              </td>
              <td className="px-4 py-2">
                <ForecastKindBadge kind={record.forecast_kind} />
              </td>
              <td className="px-4 py-2">{record.project_name ?? "-"}</td>
              <td className="px-4 py-2 text-muted-foreground">
                {[record.target?.harness_name, record.target?.model].filter(Boolean).join(" / ") || "-"}
              </td>
              <td className="px-4 py-2 text-right tabular-nums">{formatMinutes(record.p50_minutes)}</td>
              <td className="px-4 py-2 text-right tabular-nums">{formatMinutes(record.p80_minutes)}</td>
              <td className="px-4 py-2 text-right tabular-nums">
                {formatMinutes(
                  record.comparison?.actual_execution_seconds != null
                    ? record.comparison.actual_execution_seconds / 60
                    : undefined,
                )}
              </td>
              <td className="px-4 py-2 text-right tabular-nums">{formatRatio(record)}</td>
              <td className="px-4 py-2 text-caption text-muted-foreground">
                {record.status}
                {record.comparison?.exclusion ? ` (${record.comparison.exclusion})` : ""}
              </td>
              <td className="px-4 py-2 text-caption text-muted-foreground">
                {formatIssuedAt(record.issued_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>} cards={forecasts.map((record) => (
      <article key={record.prediction_id} className="panel grid gap-2 bg-card">
        <div className="flex items-center justify-between gap-2"><code className="text-xs">{record.prediction_id.slice(0, 8)}</code><ForecastKindBadge kind={record.forecast_kind} /></div>
        <p className="m-0 text-body-sm">{record.project_name ?? "Unknown project"}</p>
        <div className="flex justify-between text-caption text-muted-foreground"><span>p50 {formatMinutes(record.p50_minutes)}</span><span>{record.status}</span></div>
      </article>
    ))} />
  );
}
