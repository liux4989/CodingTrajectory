import * as React from "react";
import type { SessionTimelineEntry } from "@/api";
import { formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";
import { AlertCircle } from "lucide-react";

/**
 * One-line session evidence summary plus the warnings block. Replaces the
 * previous grid of equal-weight metric cards.
 */
export function TimelineSummary({
  entries,
  warnings,
}: {
  entries: SessionTimelineEntry[];
  warnings: string[];
}) {
  const stats = React.useMemo(() => summarize(entries), [entries]);
  return (
    <>
      <p className="m-0 text-body-sm text-muted-foreground">
        <span className="mono text-foreground">{stats.entries}</span> entries ·{" "}
        <span className="mono text-foreground">{stats.turns}</span> turns ·{" "}
        <span className={cn("mono", stats.failed > 0 ? "font-medium text-destructive" : "text-foreground")}>
          {stats.failed}
        </span>{" "}
        failure{stats.failed === 1 ? "" : "s"} ·{" "}
        <span className="mono text-foreground">{stats.subagents}</span> child agent
        {stats.subagents === 1 ? "" : "s"} ·{" "}
        <span className="mono text-foreground">
          {stats.linked}/{stats.entries}
        </span>{" "}
        source-linked
        {stats.spanSeconds != null ? (
          <>
            {" "}· <span className="mono text-foreground">{formatDuration(stats.spanSeconds)}</span> observed span
          </>
        ) : null}
        {stats.peakConcurrency > 1 ? (
          <>
            {" "}· peak <span className="mono text-foreground">{stats.peakConcurrency}</span> concurrent
          </>
        ) : null}
      </p>
      {warnings.length > 0 ? (
        <div className="alert alert-warning flex items-start gap-2 text-body-sm">
          <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
          <ul className="m-0 grid list-disc gap-1 pl-4">
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}

export function summarize(entries: SessionTimelineEntry[]) {
  const failed = entries.filter((entry) => entry.failed).length;
  const turns = new Set(entries.map((entry) => `${entry.session_id}:${entry.turn_id}`)).size;
  const subagents = new Set(entries.map((entry) => entry.target_session_id).filter(Boolean)).size;
  const linked = entries.filter((entry) => entry.item_ids.length > 0 || entry.event_ids.length > 0).length;

  const starts: number[] = [];
  const ends: number[] = [];
  // Concurrency is a per-turn property; entries within one turn share its
  // interval, so dedupe by turn before sweeping.
  const intervalByTurn = new Map<string, [number, number]>();
  for (const entry of entries) {
    const start = entry.timestamp ? Date.parse(entry.timestamp) : Number.NaN;
    const end = entry.ended_at ? Date.parse(entry.ended_at) : Number.NaN;
    if (Number.isFinite(start)) starts.push(start);
    if (Number.isFinite(end)) ends.push(end);
    if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
      intervalByTurn.set(`${entry.session_id}:${entry.turn_id}`, [start, end]);
    }
  }
  const intervals = [...intervalByTurn.values()];
  const spanSeconds =
    starts.length && ends.length ? Math.max((Math.max(...ends) - Math.min(...starts)) / 1000, 0) : null;

  const boundaries = intervals
    .flatMap(([start, end]) => [
      { at: start, change: 1 },
      { at: end, change: -1 },
    ])
    .sort((left, right) => left.at - right.at || left.change - right.change);
  let active = 0;
  let peakConcurrency = 0;
  for (const boundary of boundaries) {
    active += boundary.change;
    peakConcurrency = Math.max(peakConcurrency, active);
  }

  return { entries: entries.length, turns, failed, subagents, linked, spanSeconds, peakConcurrency };
}
