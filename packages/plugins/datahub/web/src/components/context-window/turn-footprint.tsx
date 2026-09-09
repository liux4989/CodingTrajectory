import * as React from "react";
import { useNavigate } from "@tanstack/react-router";
import type { ContextWindowPayload } from "@/api";
import { formatTokens } from "@/lib/cache-breaks";
import { cn } from "@/lib/utils";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  buildFootprintRows,
  cacheBreakLabel,
  categoryDotStyle,
  categoryLabel,
  formatDuration,
  type FootprintCompactionRow,
  type FootprintIdleRow,
  type FootprintTurnRow,
} from "./shared";

const GRID_COLS =
  "grid-cols-[5.5rem_1fr_4.5rem] sm:grid-cols-[8rem_1fr_6rem]";

/**
 * Per-turn context footprint as a waterfall: one row per turn on a shared
 * 0→window-limit scale, each row carrying the fill left by the previous turn.
 * Resting state stays quiet (carried base + single-color delta + running
 * total); hover splits the delta into categories with a tooltip breakdown;
 * click pins an inline drawer with the full stack and the timeline handoff.
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
  const rows = React.useMemo(() => buildFootprintRows(payload), [payload]);
  const [openKey, setOpenKey] = React.useState<string | null>(null);
  if (!rows.some((row) => row.kind === "turn")) return null;

  const windowTokens = payload.context_window_tokens?.value ?? null;
  const peak = Math.max(
    ...rows.map((row) =>
      row.kind === "turn"
        ? row.fillAfter
        : row.kind === "compaction"
          ? (row.preTokens ?? row.postTokens ?? 0)
          : 0,
    ),
    1,
  );
  const scale = windowTokens ?? peak;
  const pct = (tokens: number) =>
    `${Math.min((tokens / Math.max(scale, 1)) * 100, 100)}%`;
  const pctLabel = (tokens: number) =>
    windowTokens ? `${Math.round((tokens / scale) * 100)}%` : null;

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
          Running window fill per turn on a shared 0→limit scale. Hover a row for the
          breakdown, click to pin it open.
        </p>
      </div>

      <TooltipProvider>
        <div className="group/foot grid">
          {windowTokens ? <ScaleRow scale={scale} /> : null}
          <ol className="m-0 grid list-none gap-0.5">
            {rows.map((row) => {
              if (row.kind === "idle") return <IdleRow key={row.key} row={row} />;
              if (row.kind === "compaction")
                return <CompactionRow key={row.key} row={row} pct={pct} />;
              return (
                <TurnRow
                  key={row.key}
                  row={row}
                  pct={pct}
                  pctLabel={pctLabel}
                  scale={scale}
                  open={openKey === row.key}
                  onToggle={() => setOpenKey(openKey === row.key ? null : row.key)}
                  onOpenTimeline={row.turnId ? () => openTurn(row.turnId!) : null}
                />
              );
            })}
          </ol>
        </div>
      </TooltipProvider>
    </section>
  );
}

function ScaleRow({ scale }: { scale: number }) {
  return (
    <div className={cn("grid", GRID_COLS, "gap-2 px-2 sm:gap-3")}>
      <span />
      <span className="relative block h-4 opacity-40 transition-opacity duration-300 group-hover/foot:opacity-100">
        {[0, 0.25, 0.5, 0.75].map((at) => (
          <span
            key={at}
            className="mono absolute -translate-x-1/2 text-[10px] text-muted-foreground"
            style={{ left: `${at * 100}%` }}
          >
            {formatTokens(scale * at)}
          </span>
        ))}
        <span className="mono absolute right-0 text-[10px] font-medium text-destructive">
          limit {formatTokens(scale)}
        </span>
      </span>
      <span />
    </div>
  );
}

function TurnRow({
  row,
  pct,
  pctLabel,
  scale,
  open,
  onToggle,
  onOpenTimeline,
}: {
  row: FootprintTurnRow;
  pct: (tokens: number) => string;
  pctLabel: (tokens: number) => string | null;
  scale: number;
  open: boolean;
  onToggle: () => void;
  onOpenTimeline: (() => void) | null;
}) {
  const hot = row.fillAfter / Math.max(scale, 1) >= 0.85;
  let offset = row.carriedBefore;
  const carriedStyle: React.CSSProperties = row.postCompaction
    ? {
        background:
          "color-mix(in srgb, var(--color-category-compacted-history) 25%, transparent)",
      }
    : { background: "var(--color-surface-emphasis)" };

  return (
    <li className="list-none transition-opacity duration-200 group-hover/foot:opacity-50 hover:opacity-100">
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={open}
            aria-label={`${row.label}: window fill ${formatTokens(row.fillAfter)}`}
            className={cn(
              "group/row grid w-full min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-start sm:gap-3",
              GRID_COLS,
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            )}
          >
            <span className="truncate text-caption font-medium">
              {row.label}
              {row.durationSeconds != null ? (
                <span className="ml-1.5 font-normal text-muted-foreground">
                  {formatDuration(row.durationSeconds)}
                </span>
              ) : null}
            </span>
            <span className="relative block h-4 border-r border-dashed border-destructive/40">
              <span
                className="absolute top-1/2 h-2 -translate-y-1/2 rounded-l-sm transition-[height] duration-200 group-hover/row:h-3.5"
                style={{ left: 0, width: pct(row.carriedBefore), ...carriedStyle }}
                aria-hidden
              />
              <span
                className="absolute top-1/2 h-2 -translate-y-1/2 rounded-sm transition-[height,opacity] duration-200 group-hover/row:h-3.5 group-hover/row:opacity-0"
                style={{
                  left: pct(row.carriedBefore),
                  width: pct(row.deltaTokens),
                  background: hot ? "var(--color-destructive)" : "var(--color-moss)",
                }}
                aria-hidden
              />
              {row.deltas.map((delta) => {
                const left = pct(offset);
                offset += delta.tokens;
                return (
                  <span
                    key={delta.category}
                    className="absolute top-1/2 h-2 w-0 -translate-y-1/2 opacity-0 transition-[width,height,opacity] duration-300 group-hover/row:h-3.5 group-hover/row:w-(--w) group-hover/row:opacity-100"
                    style={
                      {
                        left,
                        "--w": pct(delta.tokens),
                        ...categoryDotStyle(delta.category),
                      } as React.CSSProperties
                    }
                    aria-hidden
                  />
                );
              })}
            </span>
            <span className="mono shrink-0 text-right text-caption text-muted-foreground">
              <span className="opacity-0 transition-opacity duration-200 group-hover/row:opacity-100">
                +{formatTokens(row.deltaTokens)} ·{" "}
              </span>
              <b className="font-semibold text-foreground">
                {formatTokens(row.fillAfter)}
              </b>
              {pctLabel(row.fillAfter) ? ` ${pctLabel(row.fillAfter)}` : ""}
            </span>
          </button>
        </TooltipTrigger>
        <TooltipContent side="left" align="center" className="min-w-48">
          <p className="m-0 font-semibold">
            {row.label}
            {row.durationSeconds != null ? ` · ${formatDuration(row.durationSeconds)}` : ""}
          </p>
          <ul className="m-0 mt-1 grid list-none gap-0.5 p-0">
            {row.deltas.map((delta) => (
              <li key={delta.category} className="flex items-center justify-between gap-6">
                <span className="flex items-center gap-1.5">
                  <span
                    className="inline-block size-2 rounded-[2px]"
                    style={categoryDotStyle(delta.category)}
                  />
                  {categoryLabel(delta.category)}
                </span>
                <span className="mono">+{formatTokens(delta.tokens)}</span>
              </li>
            ))}
          </ul>
          <p className="m-0 mt-1 opacity-70">
            window after {formatTokens(row.fillAfter)}
            {pctLabel(row.fillAfter) ? ` · ${pctLabel(row.fillAfter)}` : ""}
            {hot ? " — near limit" : ""}
          </p>
        </TooltipContent>
      </Tooltip>

      <div
        className={cn(
          "grid transition-[grid-template-rows] duration-300",
          open ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
        )}
      >
        <div className="overflow-hidden">
          <div className="grid gap-2 px-2 pb-2.5 pt-1">
            <div className="flex h-4 overflow-hidden rounded-sm">
              <span
                className="h-full"
                style={{ width: pct(row.carriedBefore), ...carriedStyle }}
              />
              {row.deltas.map((delta) => (
                <span
                  key={delta.category}
                  className="h-full"
                  style={{ width: pct(delta.tokens), ...categoryDotStyle(delta.category) }}
                  title={`${categoryLabel(delta.category)}: +${formatTokens(delta.tokens)}`}
                />
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-muted-foreground">
              {row.deltas.map((delta) => (
                <span key={delta.category} className="flex items-center gap-1.5">
                  <span
                    className="inline-block size-2 rounded-[2px]"
                    style={categoryDotStyle(delta.category)}
                  />
                  {categoryLabel(delta.category)} +{formatTokens(delta.tokens)}
                </span>
              ))}
              <span className={cn(hot && "text-destructive")}>
                carried {formatTokens(row.carriedBefore)} → {formatTokens(row.fillAfter)}
                {pctLabel(row.fillAfter) ? ` · ${pctLabel(row.fillAfter)}` : ""}
                {hot ? " — near limit" : ""}
              </span>
              {onOpenTimeline ? (
                <button
                  type="button"
                  onClick={onOpenTimeline}
                  className="ml-auto font-medium text-moss hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  Open in timeline →
                </button>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </li>
  );
}

function IdleRow({ row }: { row: FootprintIdleRow }) {
  const idle = formatDuration(row.idleSeconds);
  if (!idle && !row.cacheBreak) return null;
  return (
    <li className="group/idle list-none px-2 text-caption leading-5 text-muted-foreground/60 transition-colors duration-200 hover:text-muted-foreground">
      ···{idle ? ` idle ${idle}` : ""}
      {row.cacheBreak ? (
        <span className="inline-block max-w-0 overflow-hidden whitespace-nowrap align-bottom opacity-0 transition-all duration-300 group-hover/idle:max-w-96 group-hover/idle:opacity-100">
          {" "}
          · <span className="text-destructive">{cacheBreakLabel(row.cacheBreak)}</span> ·
          re-read {formatTokens(row.cacheBreak.re_read_tokens)}
        </span>
      ) : null}
    </li>
  );
}

function CompactionRow({
  row,
  pct,
}: {
  row: FootprintCompactionRow;
  pct: (tokens: number) => string;
}) {
  const post = row.postTokens ?? Math.max((row.preTokens ?? 0) - (row.droppedTokens ?? 0), 0);
  const pre = row.preTokens ?? post + (row.droppedTokens ?? 0);
  return (
    <li className="list-none transition-opacity duration-200 group-hover/foot:opacity-50 hover:opacity-100">
      <Tooltip>
        <TooltipTrigger asChild>
          <div
            className={cn(
              "group/row grid w-full min-w-0 items-center gap-2 rounded-md px-2 py-1.5 sm:gap-3",
              GRID_COLS,
            )}
          >
            <span className="truncate text-caption font-medium text-destructive">
              Compaction
            </span>
            <span className="relative block h-4 border-r border-dashed border-destructive/40">
              <span
                className="absolute top-1/2 h-2 -translate-y-1/2 rounded-l-sm transition-[height] duration-200 group-hover/row:h-3.5"
                style={{
                  left: 0,
                  width: pct(post),
                  background:
                    "color-mix(in srgb, var(--color-category-compacted-history) 25%, transparent)",
                }}
                aria-hidden
              />
              <span
                className="absolute top-1/2 h-2 -translate-y-1/2 rounded-r-sm border border-destructive/30 transition-[height] duration-200 group-hover/row:h-3.5"
                style={{
                  left: pct(post),
                  width: pct(Math.max(pre - post, 0)),
                  backgroundImage:
                    "repeating-linear-gradient(-45deg, color-mix(in srgb, var(--color-destructive) 18%, transparent) 0 5px, color-mix(in srgb, var(--color-destructive) 6%, transparent) 5px 10px)",
                }}
                aria-hidden
              />
              <span className="mono absolute top-1/2 -translate-y-1/2 text-[10px] text-destructive opacity-0 transition-opacity duration-200 group-hover/row:opacity-100"
                style={{ left: `calc(${pct(post)} + 8px)` }}
              >
                −{formatTokens(Math.max(pre - post, 0))} · {formatTokens(pre)} →{" "}
                {formatTokens(post)}
              </span>
            </span>
            <span className="mono shrink-0 text-right text-caption text-destructive">
              <b className="font-semibold">{formatTokens(post)}</b>
            </span>
          </div>
        </TooltipTrigger>
        <TooltipContent side="left" align="center" className="min-w-48">
          <p className="m-0 font-semibold">
            Compaction{row.trigger ? ` · ${row.trigger.replaceAll("_", " ")}` : ""}
          </p>
          <p className="m-0 mt-1 opacity-70">
            {formatTokens(pre)} → {formatTokens(post)} · dropped{" "}
            {formatTokens(Math.max(pre - post, 0))}; history was replaced by a summary.
          </p>
        </TooltipContent>
      </Tooltip>
    </li>
  );
}
