import * as React from "react";
import { useNavigate } from "@tanstack/react-router";
import type { ContextEvent, ContextWindowPayload } from "@/api";
import { formatTokens } from "@/lib/cache-breaks";
import { cn } from "@/lib/utils";
import {
  aggregateCategories,
  buildTurnGroups,
  categoryDotStyle,
  categoryLabel,
} from "./shared";

/** Token delta per category within one turn group, in stable category order. */
function groupCategoryTotals(events: ContextEvent[]) {
  const totals = new Map<string, number>();
  for (const event of events) {
    if (!event.tokens) continue;
    totals.set(event.category, (totals.get(event.category) ?? 0) + event.tokens.value);
  }
  return totals;
}

/**
 * Per-turn context footprint: one row per turn, token delta stacked by
 * category, scaled against the largest turn. This is the aggregate view of
 * where the window went — event-level text and evidence stay in the session
 * timeline, which selecting a turn opens at that turn's anchor.
 */
export function ContextTurnFootprint({
  payload,
  rootId,
  sessionId,
}: {
  payload: ContextWindowPayload;
  rootId: string;
  sessionId: string;
}) {
  const navigate = useNavigate();
  const groups = React.useMemo(() => buildTurnGroups(payload.events), [payload.events]);
  const legend = React.useMemo(() => aggregateCategories(payload.categories), [payload.categories]);
  if (groups.length === 0) return null;

  const maxTokens = Math.max(...groups.map((group) => group.totalTokens), 1);

  function openTurn(turnId: string) {
    void navigate({
      to: "/graphs/$rootId/sessions/$sessionId",
      params: { rootId, sessionId },
      search: { tab: "timeline", turn: turnId },
    });
  }

  return (
    <section aria-labelledby="context-footprint-title" className="grid gap-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 id="context-footprint-title" className="m-0 font-display text-heading">
          Turn footprint
        </h2>
        <p className="m-0 text-caption text-muted-foreground">
          Token delta per turn, stacked by category. Select a turn to inspect its evidence in the
          timeline.
        </p>
      </div>

      <ol className="m-0 grid list-none gap-1.5">
        {groups.map((group) => {
          const categories = groupCategoryTotals(group.events);
          const widthPct = Math.max((group.totalTokens / maxTokens) * 100, group.totalTokens > 0 ? 1.5 : 0);
          const bar = (
            <div
              className="flex h-5 overflow-hidden rounded-sm bg-surface-emphasis"
              style={{ width: `${widthPct}%` }}
              role="img"
              aria-label={`${group.label}: ${formatTokens(group.totalTokens)} added`}
            >
              {[...categories.entries()].map(([category, tokens]) => (
                <span
                  key={category}
                  className="h-full min-w-0"
                  style={{
                    ...categoryDotStyle(category),
                    width: `${(tokens / Math.max(group.totalTokens, 1)) * 100}%`,
                  }}
                  title={`${categoryLabel(category)}: +${formatTokens(tokens)}`}
                />
              ))}
            </div>
          );
          const label = (
            <span className="truncate text-caption font-medium">{group.label}</span>
          );
          const total = (
            <span className="mono shrink-0 text-right text-caption text-muted-foreground">
              +{formatTokens(group.totalTokens)}
            </span>
          );
          return (
            <li key={group.key} className="list-none">
              {group.turnId ? (
                <button
                  type="button"
                  onClick={() => openTurn(group.turnId!)}
                  className="grid w-full min-w-0 grid-cols-[5.5rem_1fr_3.5rem] items-center gap-2 rounded-md px-2 py-1.5 text-start transition-colors hover:bg-surface-emphasis focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:grid-cols-[9rem_1fr_4.5rem] sm:gap-3"
                  aria-label={`Inspect ${group.label} in the evidence timeline`}
                >
                  {label}
                  {bar}
                  {total}
                </button>
              ) : (
                <div className="grid w-full min-w-0 grid-cols-[5.5rem_1fr_3.5rem] items-center gap-2 px-2 py-1.5 sm:grid-cols-[9rem_1fr_4.5rem] sm:gap-3">
                  {label}
                  {bar}
                  {total}
                </div>
              )}
            </li>
          );
        })}
      </ol>

      {legend.length > 0 ? (
        <ul className="m-0 flex list-none flex-wrap gap-3">
          {legend.map(({ category }) => (
            <li
              key={category}
              className={cn("flex items-center gap-1.5 text-caption text-muted-foreground")}
            >
              <span className="inline-block h-2 w-2 rounded-[2px]" style={categoryDotStyle(category)} />
              {categoryLabel(category)}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
