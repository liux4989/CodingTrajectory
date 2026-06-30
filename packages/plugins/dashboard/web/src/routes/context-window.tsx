import * as React from "react";
import { useParams, useRouter } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import * as ScrollArea from "@radix-ui/react-scroll-area";
import * as Tooltip from "@radix-ui/react-tooltip";
import { ArrowLeft, Eye, Pin, PinOff } from "lucide-react";
import {
  fetchContextWindow,
  type ContextCategory,
  type ContextEvent,
  type TokenEvidence,
} from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardDescription, CardAction } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StateBlock } from "@/components/state-block";
import { cn } from "@/lib/utils";

const categoryColors: Record<string, string> = {
  starting_context: "var(--color-category-starting-context)",
  user_input: "var(--color-category-user-input)",
  files: "var(--color-category-files)",
  output: "var(--color-category-output)",
  agent: "var(--color-category-agent)",
  unattributed: "var(--color-category-unattributed)",
};

const CATEGORY_ORDER = ["starting_context", "user_input", "files", "output", "agent", "unattributed"];

function aggregateCategories(categories: ContextCategory[]) {
  const totals = new Map<string, number>();
  for (const category of categories) {
    totals.set(category.category, (totals.get(category.category) ?? 0) + category.tokens.value);
  }
  return CATEGORY_ORDER
    .filter((key) => totals.has(key))
    .map((key) => ({ category: key, tokens: totals.get(key) ?? 0 }));
}

function formatTokens(value: number | null | undefined) {
  if (value == null) return "-";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

function groupLabel(event: ContextEvent) {
  if (event.group === "before_first_prompt") return "BEFORE YOU TYPE ANYTHING";
  if (event.group === "post_turn") return "AFTER FINAL TURN";
  return `TURN ${event.turn_id ?? "-"}`;
}

function evidenceLabel(evidence: TokenEvidence | null) {
  if (!evidence) return "No event-level token evidence";
  return `${formatTokens(evidence.value)} tokens, ${evidence.confidence.replaceAll("_", " ")}`;
}

function categoryLabel(category: string) {
  if (category === "starting_context") return "Starting context";
  if (category === "user_input") return "User input";
  if (category === "files") return "Files";
  if (category === "output") return "Output";
  if (category === "agent") return "Agent";
  return category.replaceAll("_", " ");
}

function categoryDotStyle(category: string): React.CSSProperties {
  return { background: categoryColors[category] ?? categoryColors.unattributed };
}

type TimelineSegment = {
  id: string;
  category: string;
  startIndex: number;
  endIndex: number;
  eventCount: number;
  tokens: number;
  firstEventId: string;
};

function compactTimelineSegments(events: ContextEvent[]) {
  const segments: TimelineSegment[] = [];
  events.forEach((event, index) => {
    const previous = segments[segments.length - 1];
    if (previous && previous.category === event.category) {
      previous.endIndex = index;
      previous.eventCount += 1;
      previous.tokens += event.tokens?.value ?? 0;
      return;
    }
    segments.push({
      id: `${event.category}:${index}:${event.id}`,
      category: event.category,
      startIndex: index,
      endIndex: index,
      eventCount: 1,
      tokens: event.tokens?.value ?? 0,
      firstEventId: event.id,
    });
  });
  return segments;
}

function timelineSegmentLabel(segment: TimelineSegment) {
  const rowLabel = segment.startIndex === segment.endIndex
    ? `Segment ${segment.startIndex + 1}`
    : `Segments ${segment.startIndex + 1}-${segment.endIndex + 1}`;
  const tokens = segment.tokens ? `, ${formatTokens(segment.tokens)} tokens` : "";
  return `${rowLabel}: ${categoryLabel(segment.category)}, ${segment.eventCount} row${segment.eventCount === 1 ? "" : "s"}${tokens}`;
}

function CapacityBar({
  contextWindowTokens,
  usedTokens,
}: {
  contextWindowTokens: number;
  usedTokens: number;
}) {
  if (contextWindowTokens <= 0) return null;
  const widthPct = Math.min((usedTokens / contextWindowTokens) * 100, 100);
  return (
    <div
      className="flex h-2.5 w-full overflow-hidden rounded-full border border-foreground/14 bg-foreground/7"
      role="img"
      aria-label={`Context window usage: ${formatTokens(usedTokens)} of ${formatTokens(contextWindowTokens)} tokens`}
    >
      <span className="block bg-primary" style={{ width: `${widthPct}%` }} />
    </div>
  );
}

export function ContextWindowRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId/context-window" });
  const router = useRouter();
  const query = useQuery({
    queryKey: ["context-window", sessionId],
    queryFn: () => fetchContextWindow(sessionId),
  });
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [pinnedId, setPinnedId] = React.useState<string | null>(null);
  const [hoveredCategory, setHoveredCategory] = React.useState<string | null>(null);
  const eventRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
  const events = query.data?.events ?? [];

  React.useEffect(() => {
    if (!selectedId && events[0]) setSelectedId(events[0].id);
  }, [events, selectedId]);

  const activeId = pinnedId ?? selectedId ?? events[0]?.id ?? null;
  const activeEvent = events.find((event) => event.id === activeId) ?? null;
  const timelineSegments = React.useMemo(() => compactTimelineSegments(events), [events]);
  const totalUsedTokens = query.data?.used_tokens?.value ?? 0;

  function moveFocus(index: number, direction: -1 | 1) {
    const next = Math.min(Math.max(index + direction, 0), events.length - 1);
    eventRefs.current[next]?.focus();
    setSelectedId(events[next]?.id ?? null);
  }

  if (query.isPending) {
    return <StateBlock title="Loading context window" detail="Reading normalized session projections." />;
  }
  if (query.isError) {
    return <StateBlock title="Context window failed" detail={query.error.message} />;
  }

  const payload = query.data;

  return (
    <div className="mx-auto grid max-w-[96rem] gap-5">
      <Card className="gap-0 p-8">
        <CardHeader className="px-0">
          <button
            type="button"
            onClick={() => router.history.back()}
            className="mb-4 inline-flex cursor-pointer items-center gap-1.5 font-display font-extrabold text-primary decoration-[0.08em] underline-offset-[0.2em]"
          >
            <ArrowLeft size={16} /> Back
          </button>
          <CardTitle className="font-display text-display leading-tight tracking-tight">
            Explore the context window
          </CardTitle>
          <CardDescription>
            A session showing what enters context and what it costs
          </CardDescription>
          <CardAction>
            <p className="m-0 font-mono text-[0.9rem] text-moss">
              ~{formatTokens(payload.used_tokens?.value)} tokens
              {payload.context_window_tokens?.value
                ? ` / ${formatTokens(payload.context_window_tokens.value)}`
                : ""}
              {payload.used_percent != null ? ` · ${payload.used_percent.toFixed(1)}%` : ""}
            </p>
          </CardAction>
        </CardHeader>
      </Card>

      <CapacityBar
        contextWindowTokens={payload.context_window_tokens?.value ?? 0}
        usedTokens={totalUsedTokens}
      />

      {payload.provider_usage_buckets.length > 0 ? (
        <Card className="gap-3 p-5">
          <CardHeader className="px-0">
            <CardTitle className="text-base">Provider usage buckets</CardTitle>
            <CardDescription>Exact accounting reported by the provider, kept separate from semantic composition.</CardDescription>
          </CardHeader>
          <ul className="m-0 grid gap-2 p-0" role="list">
            {payload.provider_usage_buckets.map((bucket) => (
              <li key={bucket.id} className="flex items-center justify-between gap-4 text-caption">
                <span>{bucket.label}</span>
                <strong className="font-mono">{formatTokens(bucket.tokens.value)}</strong>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      <figure className="m-0 rounded-xl border border-foreground/13 bg-card p-4 dark:border-border-subtle">
        <figcaption className="flex items-center justify-between gap-4 font-display text-[0.9rem]">
          <span>Context timeline</span>
          <strong className="font-mono">
            {formatTokens(payload.used_tokens?.value)}
            {payload.used_percent != null ? ` (${payload.used_percent.toFixed(1)}%)` : ""} used
          </strong>
        </figcaption>
        <Tooltip.Provider delayDuration={160} skipDelayDuration={120}>
          <ol
            className="m-[0.75rem_0_0.8rem] flex h-[0.5rem] list-none gap-0 overflow-hidden rounded-full border border-foreground/14 bg-foreground/7 p-0"
            aria-label="Ordered context event timeline"
          >
            {timelineSegments.map((segment) => {
              const isActive = hoveredCategory === segment.category;
              const label = timelineSegmentLabel(segment);
              return (
                <li key={segment.id} className="flex min-w-[2px] flex-1" style={{ flexGrow: segment.eventCount }}>
                  <Tooltip.Root>
                    <Tooltip.Trigger asChild>
                      <button
                        type="button"
                        className={cn(
                          "relative h-[0.5rem] min-w-[2px] w-full cursor-pointer border-0 border-r border-r-white/28 p-0 opacity-72 last:border-r-0",
                          "hover:z-1 hover:opacity-100 hover:outline-none hover:shadow-[inset_0_0_0_2px_rgb(255_255_255/86%),0_0_0_2px_var(--accent-teal)]",
                          "focus-visible:z-1 focus-visible:opacity-100 focus-visible:outline-none focus-visible:shadow-[inset_0_0_0_2px_rgb(255_255_255/92%),0_0_0_3px_var(--accent-teal)]",
                          isActive && "z-1 opacity-100 outline-none shadow-[inset_0_0_0_2px_rgb(255_255_255/86%),0_0_0_2px_var(--accent-teal)]",
                        )}
                        style={{ background: categoryColors[segment.category] ?? categoryColors.unattributed }}
                        aria-label={label}
                        aria-current={isActive ? "step" : undefined}
                        onClick={() => setSelectedId(segment.firstEventId)}
                      >
                        <span className="sr-only">{label}</span>
                      </button>
                    </Tooltip.Trigger>
                    <Tooltip.Portal>
                      <Tooltip.Content
                        className="z-[120] max-w-[min(28rem,calc(100vw-2rem))] rounded-md border border-foreground/12 bg-card px-3 py-2 text-caption leading-[1.35] text-foreground shadow-popover"
                        side="top"
                        sideOffset={8}
                      >
                        {label}
                        <Tooltip.Arrow className="fill-card" />
                      </Tooltip.Content>
                    </Tooltip.Portal>
                  </Tooltip.Root>
                </li>
              );
            })}
          </ol>
        </Tooltip.Provider>
        <ul className="m-0 mt-1 flex flex-wrap gap-x-4 gap-y-1.5 list-none" role="list">
          {aggregateCategories(payload.categories).map(({ category, tokens }) => (
            <li
              key={category}
              tabIndex={0}
              onMouseEnter={() => setHoveredCategory(category)}
              onMouseLeave={() => setHoveredCategory(null)}
              onFocus={() => setHoveredCategory(category)}
              onBlur={() => setHoveredCategory(null)}
              className={cn(
                "inline-flex min-w-0 cursor-default items-center gap-1.5 text-caption text-muted-foreground transition-colors",
                hoveredCategory === category ? "text-foreground" : "hover:text-foreground",
              )}
            >
              <span className="inline-block h-[0.55rem] w-[0.55rem] rounded-[2px]" style={categoryDotStyle(category)} />
              <span>{categoryLabel(category)}</span>
              <span className="font-mono">{formatTokens(tokens)}</span>
            </li>
          ))}
          <li className="inline-flex items-center gap-1.5 text-caption text-muted-foreground">
            <Eye size={12} />
            <span>= appears in your terminal</span>
          </li>
        </ul>
      </figure>

      <div className="grid grid-cols-[minmax(22rem,1.15fr)_minmax(20rem,0.85fr)] items-start gap-4 max-lg:grid-cols-1">
        <section className="min-w-0" aria-labelledby="event-stream-title">
          <ScrollArea.Root className="relative min-h-[18rem] max-h-[min(48rem,calc(100vh-14rem))] overflow-hidden">
            <ScrollArea.Viewport
              className="max-h-[min(48rem,calc(100vh-14rem))] min-h-[18rem] pe-3 scroll-py-3"
            >
              <ol className="m-0 grid list-none gap-2 p-0">
                {events.map((event, index) => {
                  const previous = events[index - 1];
                  const startsGroup = !previous || previous.group !== event.group || previous.turn_id !== event.turn_id;
                  const isSelected = event.id === selectedId;
                  const isActive = event.id === activeId;
                  const isCategoryHighlight = hoveredCategory != null && event.category === hoveredCategory;
                  const categoryColor = categoryColors[event.category] ?? categoryColors.unattributed;
                  const tokenPercent = totalUsedTokens > 0 && event.tokens
                    ? Math.max((event.tokens.value / totalUsedTokens) * 100, 2)
                    : 0;
                  return (
                    <React.Fragment key={event.id}>
                      {startsGroup ? (
                        <li className="list-none">
                          <h4 className={cn(
                            "font-display text-eyebrow font-extrabold uppercase tracking-wide",
                            event.group === "before_first_prompt" ? "text-primary" : "text-muted-foreground",
                            index > 0 && "mt-4",
                          )}>
                            {groupLabel(event)}
                          </h4>
                        </li>
                      ) : null}
                      <li>
                        <button
                          ref={(node) => { eventRefs.current[index] = node; }}
                          type="button"
                          className={cn(
                            "relative grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-3 overflow-hidden rounded-xl border border-foreground/11 bg-foreground/5 px-4 py-3 text-start text-foreground cursor-pointer",
                            "dark:border-border-subtle dark:bg-[rgb(255_255_255/4%)]",
                            "hover:border-primary/60 hover:bg-foreground/8",
                            isActive && "border-primary/60 bg-foreground/8",
                            isSelected && "ring-2 ring-primary ring-offset-1 ring-offset-card",
                            isCategoryHighlight && "bg-foreground/10",
                          )}
                          style={isCategoryHighlight ? { boxShadow: `inset 3px 0 0 0 ${categoryColor}` } : undefined}
                          aria-pressed={isSelected}
                          onFocus={() => setSelectedId(event.id)}
                          onClick={() => setSelectedId(event.id)}
                          onKeyDown={(eventKey) => {
                            if (eventKey.key === "ArrowDown") {
                              eventKey.preventDefault();
                              moveFocus(index, 1);
                            } else if (eventKey.key === "ArrowUp") {
                              eventKey.preventDefault();
                              moveFocus(index, -1);
                            }
                          }}
                        >
                          <span className="grid min-w-0 gap-1">
                            <span className="flex min-w-0 items-center gap-2">
                              <span
                                className="inline-block h-[0.55rem] w-[0.55rem] shrink-0 rounded-[2px]"
                                style={categoryDotStyle(event.category)}
                              />
                              <span className="inline-flex min-w-0 items-center gap-1.5">
                                <Badge variant="secondary" className="shrink-0 font-mono text-[0.7rem]">
                                  {event.confidence === "exact_usage" || event.confidence === "exact_text" ? "auto" : event.confidence.replaceAll("_", " ")}
                                </Badge>
                                <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-body-sm text-muted-foreground">
                                  {event.source}
                                </span>
                              </span>
                            </span>
                            <strong className="min-w-0 break-words font-display text-body">{event.label}</strong>
                          </span>
                          <span className="flex items-center gap-2">
                            {event.terminal_visible ? (
                              <Eye size={14} className="shrink-0 text-muted-foreground" />
                            ) : null}
                            <span className="font-mono text-[0.9rem] font-bold text-moss">
                              {event.tokens ? `+${formatTokens(event.tokens.value)}` : "-"}
                            </span>
                          </span>
                          {tokenPercent > 0 ? (
                            <span className="col-span-2 mt-2 block h-[3px] w-full overflow-hidden rounded-full bg-foreground/8">
                              <span
                                className="block h-full rounded-full"
                                style={{
                                  width: `${tokenPercent}%`,
                                  background: categoryColors[event.category] ?? categoryColors.unattributed,
                                }}
                              />
                            </span>
                          ) : null}
                        </button>
                      </li>
                    </React.Fragment>
                  );
                })}
              </ol>
            </ScrollArea.Viewport>
            <ScrollArea.Scrollbar className="flex w-[0.6rem] touch-none select-none bg-foreground/5 p-px" orientation="vertical">
              <ScrollArea.Thumb className="relative flex-1 rounded-full bg-foreground/28" />
            </ScrollArea.Scrollbar>
          </ScrollArea.Root>
        </section>

        <aside className="sticky top-4 rounded-xl border border-foreground/13 bg-card p-5 max-lg:static dark:border-border-subtle dark:bg-[rgb(255_255_255/4%)]">
          {activeEvent ? (
            <>
              <div className="flex items-center justify-between gap-4 font-display">
                <div>
                  <p className="mb-1 font-display text-eyebrow font-extrabold uppercase tracking-wider text-primary">{categoryLabel(activeEvent.category)}</p>
                  <h3 className="m-0 font-display text-heading">{activeEvent.label}</h3>
                </div>
                <Button
                  size="sm"
                  variant={pinnedId === activeEvent.id ? "default" : "secondary"}
                  onClick={() => setPinnedId((current) => current === activeEvent.id ? null : activeEvent.id)}
                  aria-pressed={pinnedId === activeEvent.id}
                >
                  {pinnedId === activeEvent.id ? <PinOff size={15} /> : <Pin size={15} />}
                  {pinnedId === activeEvent.id ? "Unpin" : "Pin"}
                </Button>
              </div>
              <p className="mt-3 max-h-[18rem] overflow-auto whitespace-pre-wrap leading-relaxed">{activeEvent.summary ?? "No text preview is available."}</p>
              <dl className="mt-4 grid gap-0">
                <div className="grid grid-cols-[minmax(8rem,0.45fr)_minmax(0,1fr)] gap-3 border-t border-foreground/9 py-3">
                  <dt className="font-display font-extrabold capitalize text-muted-foreground">Token impact</dt>
                  <dd className="m-0 break-words">{evidenceLabel(activeEvent.tokens)}</dd>
                </div>
                <div className="grid grid-cols-[minmax(8rem,0.45fr)_minmax(0,1fr)] gap-3 border-t border-foreground/9 py-3">
                  <dt className="font-display font-extrabold capitalize text-muted-foreground">Evidence source</dt>
                  <dd className="m-0 break-words">{activeEvent.tokens?.source ?? activeEvent.source}</dd>
                </div>
                <div className="grid grid-cols-[minmax(8rem,0.45fr)_minmax(0,1fr)] gap-3 border-t border-foreground/9 py-3">
                  <dt className="font-display font-extrabold capitalize text-muted-foreground">Event confidence</dt>
                  <dd className="m-0 break-words">{activeEvent.confidence.replaceAll("_", " ")}</dd>
                </div>
                <div className="grid grid-cols-[minmax(8rem,0.45fr)_minmax(0,1fr)] gap-3 border-t border-foreground/9 py-3">
                  <dt className="font-display font-extrabold capitalize text-muted-foreground">Terminal visibility</dt>
                  <dd className="m-0 break-words">{activeEvent.terminal_visible ? "Visible" : "Hidden"}</dd>
                </div>
                {Object.entries(activeEvent.detail_ref).map(([key, value]) => (
                  <div key={key} className="grid grid-cols-[minmax(8rem,0.45fr)_minmax(0,1fr)] gap-3 border-t border-foreground/9 py-3">
                    <dt className="font-display font-extrabold capitalize text-muted-foreground">{key.replaceAll("_", " ")}</dt>
                    <dd className="m-0 break-words"><code>{value}</code></dd>
                  </div>
                ))}
              </dl>
            </>
          ) : (
            <>
              <h3 className="m-0 font-display text-heading">Click any event</h3>
              <p className="mt-1 text-muted-foreground">Click to preview details. Pin to keep it selected while you scroll.</p>
              <div className="mt-6 overflow-hidden rounded-xl border border-warning/30">
                <div className="bg-warning px-4 py-2 font-display text-eyebrow font-extrabold uppercase tracking-wide text-white">
                  Key Takeaway
                </div>
                <div className="bg-foreground/5 px-4 py-4">
                  <p className="m-0 font-display text-body font-bold leading-snug">
                    A lot loads before you type anything.
                  </p>
                  <p className="m-0 mt-2 text-body-sm leading-relaxed text-muted-foreground">
                    CLAUDE.md, memory, skills, and MCP tools are all in context before your first prompt.
                  </p>
                </div>
              </div>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
