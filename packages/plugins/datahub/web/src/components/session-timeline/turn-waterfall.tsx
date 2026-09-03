import * as React from "react";
import type { SessionTimelineEntry } from "@/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";
import { agentLabel } from "./shared";

export type WaterfallTurn = {
  id: string;
  sessionId: string;
  agent: string;
  entryId: string;
  startedAt: number;
  endedAt: number;
  failed: boolean;
};

function buildWaterfallTurns(entries: SessionTimelineEntry[]) {
  const seen = new Set<string>();
  const turns: WaterfallTurn[] = [];
  let omittedTurns = 0;
  for (const entry of entries) {
    const id = `${entry.session_id}:${entry.turn_id}`;
    if (seen.has(id)) continue;
    seen.add(id);
    const startedAt = entry.timestamp ? Date.parse(entry.timestamp) : Number.NaN;
    const endedAt = entry.ended_at ? Date.parse(entry.ended_at) : Number.NaN;
    if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt) || endedAt < startedAt) {
      omittedTurns += 1;
      continue;
    }
    turns.push({
      id,
      sessionId: entry.session_id,
      agent: agentLabel(entry),
      entryId: entry.id,
      startedAt,
      endedAt,
      failed: entry.failed,
    });
  }
  return { turns, omittedTurns };
}

/**
 * Observed turn intervals across agent branches. Rendered only when at least
 * two turns retain complete timing — a single interval is a summary-line
 * fact, not a chart.
 */
export function TurnWaterfall({
  entries,
  onSelect,
}: {
  entries: SessionTimelineEntry[];
  onSelect: (turn: WaterfallTurn) => void;
}) {
  const { turns, omittedTurns } = React.useMemo(() => buildWaterfallTurns(entries), [entries]);
  if (turns.length < 2) return null;

  const startedAt = Math.min(...turns.map((turn) => turn.startedAt));
  const endedAt = Math.max(...turns.map((turn) => turn.endedAt));
  const duration = Math.max(endedAt - startedAt, 1);
  const laneMap = new Map<string, { agent: string; turns: WaterfallTurn[] }>();
  for (const turn of turns) {
    const lane = laneMap.get(turn.sessionId);
    if (lane) lane.turns.push(turn);
    else laneMap.set(turn.sessionId, { agent: turn.agent, turns: [turn] });
  }
  const lanes = Array.from(laneMap.entries());

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Turn waterfall</CardTitle>
        <CardDescription>
          Observed turn intervals across agent branches. Select a bar to filter and inspect its first evidence
          entry.{omittedTurns ? ` ${omittedTurns} turn(s) without complete timing are omitted.` : ""}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 overflow-x-auto">
        {lanes.map(([sessionId, lane]) => (
          <div key={sessionId} className="grid min-w-[42rem] grid-cols-[9rem_1fr] items-center gap-3">
            <span className="truncate text-caption font-medium" title={sessionId}>
              {lane.agent}
            </span>
            <div className="relative h-8 rounded-md bg-surface-emphasis">
              {lane.turns.map((turn) => {
                const left = ((turn.startedAt - startedAt) / duration) * 100;
                const width = Math.max(((turn.endedAt - turn.startedAt) / duration) * 100, 1.2);
                return (
                  <button
                    key={turn.id}
                    type="button"
                    className={cn(
                      "absolute top-1 h-6 rounded-sm border border-primary/50 bg-primary/70 hover:bg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      turn.failed && "border-destructive bg-destructive/70 hover:bg-destructive",
                    )}
                    style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
                    title={`${turn.agent} · ${formatDuration((turn.endedAt - turn.startedAt) / 1000)}`}
                    aria-label={`Inspect ${turn.agent} turn lasting ${formatDuration((turn.endedAt - turn.startedAt) / 1000)}`}
                    onClick={() => onSelect(turn)}
                  />
                );
              })}
            </div>
          </div>
        ))}
        <div className="flex min-w-[42rem] justify-between pl-[9.75rem] text-caption text-muted-foreground">
          <span>{new Date(startedAt).toLocaleTimeString()}</span>
          <span>{formatDuration(duration / 1000)} observed span</span>
          <span>{new Date(endedAt).toLocaleTimeString()}</span>
        </div>
        <ul className="sr-only">
          {turns.map((turn) => (
            <li key={turn.id}>
              {turn.agent}: {formatDuration((turn.endedAt - turn.startedAt) / 1000)}
              {turn.failed ? ", failed" : ""}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
