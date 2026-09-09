import * as React from "react";
import type { SessionTimelineEntry } from "@/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
 * Observed turn intervals, one lane per session section. Rendered only when at
 * least two turns retain complete timing — a single interval is a summary-line
 * fact, not a chart. Selecting a bar in the current session inspects its first
 * evidence entry; selecting another session's bar opens that session's scope.
 */
export function TurnWaterfall({
  entries,
  sessionId,
  onSelect,
}: {
  entries: SessionTimelineEntry[];
  sessionId: string;
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
  const lanes = Array.from(laneMap.entries()).sort(
    ([leftId], [rightId]) => Number(rightId === sessionId) - Number(leftId === sessionId),
  );

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="title-card">Turn waterfall</CardTitle>
        <CardDescription>
          Observed turn intervals, one lane per session section. Select a bar in this session to inspect its
          first evidence entry; selecting another session&apos;s bar opens that session.
          {omittedTurns ? ` ${omittedTurns} turn(s) without complete timing are omitted.` : ""}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 overflow-x-auto">
        <TooltipProvider>
        {lanes.map(([laneSessionId, lane]) => (
          <div key={laneSessionId} className="grid min-w-[34rem] grid-cols-[6.5rem_1fr] items-center gap-3 sm:grid-cols-[9rem_1fr]">
            <span className="truncate text-caption font-medium" title={laneSessionId}>
              {lane.agent}
              {laneSessionId !== sessionId ? " · child session" : ""}
            </span>
            <div className="relative h-8 rounded-md bg-surface-emphasis">
              {lane.turns.map((turn) => {
                const left = ((turn.startedAt - startedAt) / duration) * 100;
                const width = Math.max(((turn.endedAt - turn.startedAt) / duration) * 100, 1.2);
                return (
                  <Tooltip key={turn.id}>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        className={cn(
                          "absolute top-1 h-6 rounded-sm border border-primary/50 bg-primary/70 hover:bg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                          turn.failed && "border-destructive bg-destructive/70 hover:bg-destructive",
                        )}
                        style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
                        aria-label={`Inspect ${turn.agent} turn lasting ${formatDuration((turn.endedAt - turn.startedAt) / 1000)}`}
                        onClick={() => onSelect(turn)}
                      />
                    </TooltipTrigger>
                    <TooltipContent>
                      {turn.agent} · {formatDuration((turn.endedAt - turn.startedAt) / 1000)}
                      {turn.failed ? " · failed" : ""}
                    </TooltipContent>
                  </Tooltip>
                );
              })}
            </div>
          </div>
        ))}
        </TooltipProvider>
        <div className="flex min-w-[34rem] justify-between pl-[7.25rem] text-caption text-muted-foreground sm:pl-[9.75rem]">
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
