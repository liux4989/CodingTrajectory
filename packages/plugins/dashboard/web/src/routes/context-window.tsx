import * as React from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import * as ScrollArea from "@radix-ui/react-scroll-area";
import * as Tooltip from "@radix-ui/react-tooltip";
import { ArrowLeft, Pin, PinOff } from "lucide-react";
import {
  fetchContextWindow,
  type ContextCategory,
  type ContextEvent,
  type TokenEvidence,
} from "@/api";
import { Button } from "@/components/ui/button";
import { StateBlock } from "@/components/state-block";
import { cn } from "@/lib/utils";

const categoryColors: Record<string, string> = {
  system: "var(--color-category-system)",
  project_instructions: "var(--color-category-project-instructions)",
  memory: "var(--color-category-memory)",
  skills: "var(--color-category-skills)",
  mcp: "var(--color-category-mcp)",
  rules: "var(--color-category-rules)",
  you: "var(--color-category-you)",
  files: "var(--color-category-files)",
  output: "var(--color-category-output)",
  agent: "var(--color-category-agent)",
  assistant: "var(--color-category-agent)",
  hooks: "var(--color-category-hooks)",
  unattributed: "var(--color-category-unattributed)",
};

function formatTokens(value: number | null | undefined) {
  if (value == null) return "-";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

function groupLabel(event: ContextEvent) {
  if (event.group === "before_first_prompt") return "Before first prompt";
  if (event.group === "post_turn") return "After final turn";
  return `Turn ${event.turn_id ?? "-"}`;
}

function evidenceLabel(evidence: TokenEvidence | null) {
  if (!evidence) return "No event-level token evidence";
  return `${formatTokens(evidence.value)} tokens, ${evidence.confidence.replaceAll("_", " ")}`;
}

function categoryLabel(category: string) {
  if (category === "agent") return "Agent";
  return category.replaceAll("_", " ");
}

function topCategories(categories: ContextCategory[]) {
  return [...categories]
    .sort((left, right) => right.tokens.value - left.tokens.value)
    .slice(0, 6);
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
    ? `Step ${segment.startIndex + 1}`
    : `Steps ${segment.startIndex + 1}-${segment.endIndex + 1}`;
  const tokens = segment.tokens ? `, ${formatTokens(segment.tokens)} tokens` : "";
  return `${rowLabel}: ${categoryLabel(segment.category)}, ${segment.eventCount} row${segment.eventCount === 1 ? "" : "s"}${tokens}`;
}

function categoryDotStyle(category: string): React.CSSProperties {
  return { background: categoryColors[category] ?? categoryColors.unattributed };
}

export function ContextWindowRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId/context-window" });
  const query = useQuery({
    queryKey: ["context-window", sessionId],
    queryFn: () => fetchContextWindow(sessionId),
  });
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [hoveredId, setHoveredId] = React.useState<string | null>(null);
  const [pinnedId, setPinnedId] = React.useState<string | null>(null);
  const eventRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
  const scrollViewportRef = React.useRef<HTMLDivElement | null>(null);
  const events = query.data?.events ?? [];

  React.useEffect(() => {
    if (!selectedId && events[0]) setSelectedId(events[0].id);
  }, [events, selectedId]);

  const activeId = pinnedId ?? hoveredId ?? selectedId ?? events[0]?.id ?? null;
  const activeEvent = events.find((event) => event.id === activeId) ?? null;
  const activeIndex = events.findIndex((event) => event.id === activeId);
  const timelineSegments = React.useMemo(() => compactTimelineSegments(events), [events]);

  function activateEvent(id: string) {
    setHoveredId(id);
    setSelectedId(id);
    const index = events.findIndex((event) => event.id === id);
    const eventNode = eventRefs.current[index];
    const scrollNode = scrollViewportRef.current;
    if (eventNode && scrollNode) {
      const eventRect = eventNode.getBoundingClientRect();
      const scrollRect = scrollNode.getBoundingClientRect();
      scrollNode.scrollTo({
        top:
          scrollNode.scrollTop +
          eventRect.top -
          scrollRect.top -
          scrollNode.clientHeight / 2 +
          eventRect.height / 2,
        behavior: "smooth",
      });
    }
  }

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
  const visibleCategories = topCategories(payload.categories);

  return (
    <div className="mx-auto grid max-w-[96rem] gap-5">
      <header className="rounded-[2rem] border border-foreground/13 bg-[linear-gradient(135deg,rgb(255_249_234/95%),rgb(13_92_99/10%)),var(--paper-strong)] p-[clamp(1.2rem,4vw,3rem)] shadow-[0_24px_70px_rgb(49_42_25/18%)] dark:border-[rgb(255_255_255/8%)] dark:bg-[linear-gradient(135deg,rgb(34_32_25/95%),rgb(77_184_176/8%)),var(--paper-strong)] dark:shadow-[0_24px_70px_rgb(0_0_0/40%)]">
        <div>
          <Link to="/sessions" className="mb-5 inline-flex items-center gap-1.5 font-display font-extrabold text-primary decoration-[0.08em] underline-offset-[0.2em]">
            <ArrowLeft size={16} /> Sessions
          </Link>
          <p className="mb-1 font-display text-[0.74rem] font-extrabold uppercase tracking-[0.14em] text-primary">
            Context inspector
          </p>
          <h2 className="m-0 font-display text-[clamp(2.2rem,6vw,5.6rem)] leading-[0.9] tracking-[-0.05em]">
            {payload.model ?? "Unknown model"}
          </h2>
          <p className="mt-4 text-muted-foreground break-all">
            <code>{payload.session_id}</code> · {payload.vendor} · {formatTokens(payload.used_tokens?.value)}
            {payload.used_percent != null ? ` (${payload.used_percent.toFixed(1)}%)` : ""} used
          </p>
        </div>
      </header>

      <figure className="m-0 rounded-[1.4rem] border border-foreground/13 bg-card p-4 dark:border-[rgb(255_255_255/8%)]">
        <figcaption className="flex items-center justify-between gap-4 font-display">
          <span>Compact context timeline</span>
          <strong>
            {formatTokens(payload.used_tokens?.value)}
            {payload.used_percent != null ? ` (${payload.used_percent.toFixed(1)}%)` : ""} used
          </strong>
        </figcaption>
        <Tooltip.Provider delayDuration={160} skipDelayDuration={120}>
          <ol
            className="m-[0.75rem_0_0.8rem] flex h-[0.65rem] list-none gap-0 overflow-hidden rounded-full border border-foreground/14 bg-foreground/7 p-0"
            aria-label="Ordered context event timeline"
            onMouseLeave={() => setHoveredId(null)}
          >
            {timelineSegments.map((segment) => {
              const isActive = activeEvent?.category === segment.category
                && activeIndex >= segment.startIndex
                && activeIndex <= segment.endIndex;
              const label = timelineSegmentLabel(segment);
              return (
                <li key={segment.id} className="flex min-w-[2px] flex-1" style={{ flexGrow: segment.eventCount }}>
                  <Tooltip.Root>
                    <Tooltip.Trigger asChild>
                      <button
                        type="button"
                        className={cn(
                          "relative h-[0.65rem] min-w-[2px] w-full cursor-pointer border-0 border-r border-r-white/28 p-0 opacity-72 last:border-r-0",
                          "hover:z-1 hover:opacity-100 hover:outline-none hover:shadow-[inset_0_0_0_2px_rgb(255_255_255/86%),0_0_0_2px_var(--accent-teal)]",
                          "focus-visible:z-1 focus-visible:opacity-100 focus-visible:outline-none focus-visible:shadow-[inset_0_0_0_2px_rgb(255_255_255/92%),0_0_0_3px_var(--accent-teal)]",
                          isActive && "z-1 opacity-100 outline-none shadow-[inset_0_0_0_2px_rgb(255_255_255/86%),0_0_0_2px_var(--accent-teal)]",
                        )}
                        style={{ background: categoryColors[segment.category] ?? categoryColors.unattributed }}
                        aria-label={label}
                        aria-current={isActive ? "step" : undefined}
                        onPointerEnter={() => activateEvent(segment.firstEventId)}
                        onMouseEnter={() => activateEvent(segment.firstEventId)}
                        onFocus={() => activateEvent(segment.firstEventId)}
                        onBlur={() => setHoveredId(null)}
                        onClick={() => activateEvent(segment.firstEventId)}
                      >
                        <span className="sr-only">{label}</span>
                      </button>
                    </Tooltip.Trigger>
                    <Tooltip.Portal>
                      <Tooltip.Content
                        className="z-[120] max-w-[min(28rem,calc(100vw-2rem))] rounded-md border border-foreground/12 bg-card px-3 py-2 text-[0.82rem] leading-[1.35] text-foreground shadow-[0_24px_70px_rgb(49_42_25/18%)]"
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
        <ul className="m-0 flex flex-wrap gap-1.5 px-0 py-0 list-none" role="list">
          {visibleCategories.map((category) => (
            <li key={category.id} className="inline-flex min-w-0 items-center gap-1.5 text-[0.9rem] text-muted-foreground">
              <span className="inline-block h-[0.7rem] w-[0.7rem] rounded-full" style={categoryDotStyle(category.category)} />
              <span>{category.label}</span>
              <strong className="font-mono text-[0.86rem] text-foreground">{formatTokens(category.tokens.value)}</strong>
            </li>
          ))}
          {payload.categories.length > visibleCategories.length ? (
            <li className="ml-auto inline-flex items-center gap-1.5 text-[0.9rem] text-muted-foreground">
              <span />
              <span>{payload.categories.length - visibleCategories.length} more</span>
              <strong className="font-mono text-[0.86rem] text-foreground">{formatTokens(payload.context_window_tokens?.value)} window</strong>
            </li>
          ) : null}
        </ul>
      </figure>

      <div className="grid grid-cols-[minmax(22rem,1.15fr)_minmax(20rem,0.85fr)] items-start gap-4 max-lg:grid-cols-1">
        <section className="min-w-0" aria-labelledby="event-stream-title">
          <div className="mb-3 flex items-center justify-between gap-4 font-display">
            <div>
              <p className="mb-1 font-display text-[0.74rem] font-extrabold uppercase tracking-[0.14em] text-primary">Trajectory</p>
              <h3 id="event-stream-title" className="m-0 font-display text-[1.45rem]">Context events</h3>
            </div>
            <span className="text-muted-foreground">{events.length} rows</span>
          </div>
          <ScrollArea.Root className="relative min-h-[18rem] max-h-[min(42rem,calc(100vh-17rem))] overflow-hidden">
            <ScrollArea.Viewport
              className="max-h-[min(42rem,calc(100vh-17rem))] min-h-[18rem] pe-3 scroll-py-3"
              ref={scrollViewportRef}
            >
              <ol className="m-0 grid list-none gap-0 p-0" onMouseLeave={() => setHoveredId(null)}>
                {events.map((event, index) => {
                  const previous = events[index - 1];
                  const startsGroup = !previous || previous.group !== event.group || previous.turn_id !== event.turn_id;
                  const isActive = event.id === activeId;
                  return (
                    <li
                      key={event.id}
                      className={cn(
                        "relative grid min-w-0 grid-cols-[1.35rem_minmax(0,1fr)]",
                        "before:col-[1] before:row-[1/span_2] before:mx-auto before:w-px before:bg-foreground/16",
                        "first:before:mt-[1.2rem] last:before:h-[1.6rem]",
                        "dark:before:bg-[rgb(255_255_255/12%)]",
                        startsGroup && index > 0 && "mt-2",
                      )}
                      data-group={event.group}
                    >
                      {startsGroup ? (
                        <h4 className="col-[2] mb-1.5 mt-4 font-display text-[0.78rem] font-bold uppercase tracking-[0.08em] text-muted-foreground">
                          {groupLabel(event)}
                        </h4>
                      ) : null}
                      <button
                        ref={(node) => { eventRefs.current[index] = node; }}
                        type="button"
                        className={cn(
                          "relative col-[2] grid w-full grid-cols-[minmax(0,1fr)_auto] items-start gap-3 rounded-md border border-foreground/11 bg-card/42 px-3 py-2.5 text-start text-foreground cursor-pointer",
                          "before:absolute before:top-4 before:-left-[0.68rem] before:h-px before:w-[0.68rem] before:bg-foreground/16",
                          "dark:border-[rgb(255_255_255/8%)] dark:bg-card/62",
                          "hover:border-primary hover:bg-primary/9",
                          isActive && "border-primary bg-primary/9",
                        )}
                        aria-pressed={event.id === selectedId}
                        onMouseEnter={() => setHoveredId(event.id)}
                        onFocus={() => setHoveredId(event.id)}
                        onBlur={() => setHoveredId(null)}
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
                        <span
                          className="absolute -left-[1.68rem] top-[0.7rem] z-1 h-[0.7rem] w-[0.7rem] rounded-full shadow-[0_0_0_3px_var(--paper)]"
                          style={categoryDotStyle(event.category)}
                        />
                        <span className="grid min-w-0 gap-0.5">
                          <strong className="font-display text-[0.96rem]">{event.label}</strong>
                          <span className="overflow-hidden text-ellipsis whitespace-nowrap text-[0.9rem] text-muted-foreground">
                            {event.summary ?? event.source}
                          </span>
                          <small className="w-fit rounded-full border border-foreground/10 px-1.5 py-px font-mono text-[0.72rem] capitalize text-muted-foreground dark:border-[rgb(255_255_255/10%)]">
                            {categoryLabel(event.category)}
                          </small>
                        </span>
                        <span className="font-mono text-[0.9rem] font-extrabold text-primary">
                          {event.tokens ? `+${formatTokens(event.tokens.value)}` : "-"}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            </ScrollArea.Viewport>
            <ScrollArea.Scrollbar className="flex w-[0.6rem] touch-none select-none bg-foreground/5 p-px" orientation="vertical">
              <ScrollArea.Thumb className="relative flex-1 rounded-full bg-foreground/28" />
            </ScrollArea.Scrollbar>
          </ScrollArea.Root>
        </section>

        <aside className="sticky top-4 rounded-[1.4rem] border border-foreground/13 bg-card p-5 max-lg:static dark:border-[rgb(255_255_255/8%)]">
          {activeEvent ? (
            <>
              <div className="flex items-center justify-between gap-4 font-display">
                <div>
                  <p className="mb-1 font-display text-[0.74rem] font-extrabold uppercase tracking-[0.14em] text-primary">{categoryLabel(activeEvent.category)}</p>
                  <h3 className="m-0 font-display text-[1.45rem]">{activeEvent.label}</h3>
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
          ) : <p>No event selected.</p>}
        </aside>
      </div>
    </div>
  );
}
