import * as React from "react";
import { useParams, useRouter } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import * as ScrollArea from "@radix-ui/react-scroll-area";
import * as Tooltip from "@radix-ui/react-tooltip";
import { ArrowLeft, Eye, Pin, PinOff, Play, Pause, Maximize, Minimize, Info, Search, X, Sparkles, Square } from "lucide-react";
import {
  analyzeSession,
  fetchContextWindow,
  type AnalysisProvider,
  type ContextCategory,
  type ContextEvent,
  type JobRecord,
  type SessionAnalysis,
  type TokenEvidence,
} from "@/api";
import { useJob } from "@/hooks/use-job";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/loading-state";
import { Card, CardHeader, CardTitle, CardDescription, CardAction } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
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

function categoryDotStyle(category: string): React.CSSProperties {
  return { background: categoryColors[category] ?? categoryColors.unattributed };
}

function eventColor(event: ContextEvent) {
  return categoryColors[event.category] ?? categoryColors.unattributed;
}

function categoryTint(color: string, alpha: number) {
  return `color-mix(in srgb, ${color} ${alpha * 100}%, transparent)`;
}

function formatTokens(value: number | null | undefined) {
  if (value == null) return "-";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

function formatCount(value: number | null | undefined) {
  if (value == null) return "-";
  return new Intl.NumberFormat().format(value);
}

function formatPercent(value: number | null | undefined) {
  if (value == null) return "-";
  return `${value.toFixed(1)}%`;
}

function evidenceLabel(evidence: TokenEvidence | null) {
  if (!evidence) return "No event-level token evidence";
  return `${formatTokens(evidence.value)} tokens`;
}

function groupHeader(event: ContextEvent, previous?: ContextEvent): string | null {
  if (event.source === "subagent" && previous?.source !== "subagent") {
    return "SUBAGENT'S SEPARATE CONTEXT WINDOW";
  }
  if (event.group === "before_first_prompt" && (!previous || previous.group !== "before_first_prompt")) {
    return "BEFORE YOU TYPE ANYTHING";
  }
  if (event.group === "post_turn" && (!previous || previous.group !== "post_turn")) {
    return "CLAUDE WORKS";
  }
  if (event.group === "turn" && (!previous || previous.turn_id !== event.turn_id)) {
    if (event.source === "you") return "you";
    return "CLAUDE WORKS";
  }
  return null;
}

function categoryLabel(category: string) {
  if (category === "starting_context") return "Starting context";
  if (category === "user_input") return "User input";
  if (category === "files") return "Files";
  if (category === "output") return "Output";
  if (category === "agent") return "Agent";
  return category.replaceAll("_", " ");
}

function keyTakeaway(event: ContextEvent) {
  if (event.source === "hook") return "Hooks fire automatically on tool events. Output reaches Claude via additionalContext JSON.";
  if (event.source === "subagent") return "Subagents run in their own context window. Only summaries flow back to the parent session.";
  if (event.category === "starting_context") return "A lot loads before you type anything. System prompts, memory, skills, and MCP tools are all in context.";
  if (event.category === "user_input") return "Your messages are tokenized and fed into context as user turns.";
  if (event.category === "output") return "Model output is re-fed into context so Claude can build on prior responses.";
  if (event.category === "files") return "File contents are expanded and placed in context when referenced.";
  return "Every item in context costs tokens. Pin an event to inspect it while scrolling.";
}

function findingLabel(kind: SessionAnalysis["findings"][number]["kind"]) {
  if (kind === "justified_expensive_work") return "Justified";
  if (kind === "avoidable_pattern") return "Avoidable";
  if (kind === "optimal_pattern") return "Optimal";
  return "Next workflow";
}

function findingTone(kind: SessionAnalysis["findings"][number]["kind"]) {
  if (kind === "avoidable_pattern") return "border-warning/35 bg-warning/8";
  if (kind === "optimal_pattern") return "border-moss/35 bg-moss/8";
  return "border-border-soft bg-surface-subtle";
}

export function ContextWindowRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId/context-window" });
  const router = useRouter();
  const [analysisProvider, setAnalysisProvider] = React.useState<AnalysisProvider>("codex");
  const [analysisRefresh, setAnalysisRefresh] = React.useState(false);
  const query = useQuery({
    queryKey: ["context-window", sessionId],
    queryFn: () => fetchContextWindow(sessionId),
  });
  const analysisJob = useJob<SessionAnalysis>({
    start: () => analyzeSession(sessionId, analysisRefresh, analysisProvider),
    resolve: (record: JobRecord) => record.result as unknown as SessionAnalysis,
  });
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [pinnedId, setPinnedId] = React.useState<string | null>(null);
  const [hoveredCategory, setHoveredCategory] = React.useState<string | null>(null);
  const [isPlaying, setIsPlaying] = React.useState(false);
  const [isFullscreen, setIsFullscreen] = React.useState(false);
  const [searchQuery, setSearchQuery] = React.useState("");
  const [activeCategories, setActiveCategories] = React.useState<Set<string>>(new Set());
  const eventRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
  const events = query.data?.events ?? [];

  const filteredEvents = React.useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return events.filter((event) => {
      if (activeCategories.size > 0 && !activeCategories.has(event.category)) return false;
      if (q && !event.label.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [events, activeCategories, searchQuery]);

  React.useEffect(() => {
    if (filteredEvents.length === 0) return;
    if (!selectedId || !filteredEvents.some((e) => e.id === selectedId)) {
      setSelectedId(filteredEvents[0].id);
    }
  }, [filteredEvents, selectedId]);

  React.useEffect(() => {
    if (!isPlaying) return;
    const interval = setInterval(() => {
      setSelectedId((current) => {
        const idx = Math.max(filteredEvents.findIndex((e) => e.id === current), 0);
        const nextIndex = Math.min(idx + 1, filteredEvents.length - 1);
        if (nextIndex === idx) {
          setIsPlaying(false);
          return current;
        }
        return filteredEvents[nextIndex].id;
      });
    }, 800);
    return () => clearInterval(interval);
  }, [isPlaying, filteredEvents]);

  React.useEffect(() => {
    const handler = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", handler);
    return () => document.removeEventListener("fullscreenchange", handler);
  }, []);

  const activeId = pinnedId ?? selectedId ?? filteredEvents[0]?.id ?? null;
  const activeEvent = events.find((event) => event.id === activeId) ?? null;
  const analysis =
    analysisJob.data?.provider === analysisProvider ? analysisJob.data : null;
  const totalUsedTokens = query.data?.used_tokens?.value ?? 0;

  const combinedSegments = React.useMemo(() => {
    const capacity = query.data?.context_window_tokens?.value ?? 0;
    return aggregateCategories(query.data?.categories ?? []).map(({ category, tokens }) => ({
      category,
      tokens,
      widthPct: capacity > 0 ? Math.min((tokens / capacity) * 100, 100) : 0,
    }));
  }, [query.data?.categories, query.data?.context_window_tokens?.value]);
  const remainingPct = Math.max(100 - (query.data?.used_percent ?? 0), 0);

  const playbackIndex = filteredEvents.findIndex((e) => e.id === activeId);
  const playbackPct = filteredEvents.length > 0
    ? ((Math.max(playbackIndex, 0) + 1) / filteredEvents.length) * 100
    : 0;

  React.useEffect(() => {
    if (!isPlaying || !activeId) return;
    const index = filteredEvents.findIndex((e) => e.id === activeId);
    eventRefs.current[index]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [activeId, isPlaying, filteredEvents]);

  function moveFocus(index: number, direction: -1 | 1) {
    const next = Math.min(Math.max(index + direction, 0), filteredEvents.length - 1);
    eventRefs.current[next]?.focus();
    setSelectedId(filteredEvents[next]?.id ?? null);
  }

  function toggleCategory(category: string) {
    setActiveCategories((current) => {
      const next = new Set(current);
      if (next.has(category)) next.delete(category);
      else next.add(category);
      return next;
    });
  }

  function clearFilters() {
    setActiveCategories(new Set());
    setSearchQuery("");
  }

  function toggleFullscreen() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen().catch(() => {});
    }
  }

  if (query.isPending) {
    return <LoadingState title="Loading context window" detail="Reading normalized session projections." />;
  }
  if (query.isError) {
    return <StateBlock title="Context window failed" detail={query.error.message} />;
  }

  const payload = query.data;
  const hasFilters = activeCategories.size > 0 || searchQuery.trim().length > 0;
  const analysisRunning = analysisJob.status === "pending" || analysisJob.status === "running";
  const analysisButtonLabel = analysisRunning
    ? "Analyzing..."
    : analysis
      ? "Refresh analysis"
      : "Analyze session";

  return (
    <div className="route-container pb-8">
      <Card className="gap-4 p-6">
        <CardHeader className="px-0">
          <button
            type="button"
            onClick={() => router.history.back()}
            className="mb-2 inline-flex cursor-pointer items-center gap-1.5 font-display text-caption font-extrabold text-primary"
          >
            <ArrowLeft size={14} /> Back
          </button>
          <CardTitle className="font-display text-display leading-tight tracking-tight">
            Explore the context window
          </CardTitle>
          <CardDescription>
            A session showing what enters context and what it costs
          </CardDescription>
          <CardAction>
            <div className="flex flex-wrap items-center justify-end gap-3">
              <div className="text-right">
                <p className="m-0 mono text-heading font-bold leading-none text-moss">
                  ~{formatTokens(payload.used_tokens?.value)} tokens
                </p>
                <p className="m-0 mt-1 mono text-caption text-muted-foreground">
                  / {formatTokens(payload.context_window_tokens?.value)} · illustrative
                </p>
              </div>
              <Select
                value={analysisProvider}
                onValueChange={(value) => setAnalysisProvider(value as AnalysisProvider)}
                disabled={analysisRunning}
              >
                <SelectTrigger size="sm" className="min-w-28">
                  <SelectValue aria-label="Analysis provider" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="codex">Codex</SelectItem>
                  <SelectItem value="pi">Pi</SelectItem>
                </SelectContent>
              </Select>
              <Button
                size="sm"
                variant={analysis ? "secondary" : "default"}
                onClick={() => {
                  setAnalysisRefresh(Boolean(analysis));
                  analysisJob.reset();
                  analysisJob.start();
                }}
                disabled={analysisRunning}
                className="gap-1.5"
              >
                {analysisRunning ? <Square size={13} className="fill-current" /> : <Sparkles size={15} />}
                {analysisButtonLabel}
              </Button>
            </div>
          </CardAction>
        </CardHeader>
      </Card>

      {analysisRunning ? (
        <LoadingState
          title="Analyzing session"
          detail="The coding agent is reviewing session usage and tool events."
          elapsedMs={analysisJob.elapsedMs}
          progress={analysisJob.progress}
          onCancel={analysisJob.cancel}
        />
      ) : null}
      {analysisJob.status === "error" ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/8 px-4 py-3 text-body-sm text-foreground">
          {analysisJob.error}
        </div>
      ) : null}

      {analysis ? <SessionAnalysisPanel analysis={analysis} /> : null}

      <figure className="m-0">
        <Tooltip.Provider delayDuration={160} skipDelayDuration={120}>
          <div
            className="flex h-3 w-full overflow-hidden rounded-full bg-surface-emphasis"
            role="img"
            aria-label={`Context window usage: ${formatTokens(totalUsedTokens)} of ${formatTokens(payload.context_window_tokens?.value ?? 0)} tokens, grouped by category`}
          >
            {combinedSegments.map((seg) => {
              const isActive = activeCategories.has(seg.category);
              const label = `${categoryLabel(seg.category)}: ${formatTokens(seg.tokens)} tokens (${seg.widthPct.toFixed(1)}% of window)`;
              return (
                <div
                  key={seg.category}
                  className="flex h-full min-w-0"
                  style={{ width: `${seg.widthPct}%` }}
                >
                  <Tooltip.Root>
                    <Tooltip.Trigger asChild>
                      <button
                        type="button"
                        className={cn(
                          "h-full w-full cursor-pointer border-0 border-r border-r-white/24 p-0 transition-opacity",
                          "hover:opacity-100 hover:outline-none hover:ring-2 hover:ring-white/80",
                          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white",
                          isActive ? "opacity-100 ring-2 ring-white" : "opacity-80",
                        )}
                        style={{ background: categoryColors[seg.category] ?? categoryColors.unattributed }}
                        aria-pressed={isActive}
                        aria-label={label}
                        onClick={() => toggleCategory(seg.category)}
                        onMouseEnter={() => setHoveredCategory(seg.category)}
                        onMouseLeave={() => setHoveredCategory(null)}
                      >
                        <span className="sr-only">{label}</span>
                      </button>
                    </Tooltip.Trigger>
                    <Tooltip.Portal>
                      <Tooltip.Content
                        className="z-[120] max-w-[min(28rem,calc(100vw-2rem))] rounded-md border border-border-soft bg-card px-3 py-2 text-caption leading-[1.35] text-foreground shadow-popover"
                        side="top"
                        sideOffset={8}
                      >
                        {label}
                        <Tooltip.Arrow className="fill-card" />
                      </Tooltip.Content>
                    </Tooltip.Portal>
                  </Tooltip.Root>
                </div>
              );
            })}
            {remainingPct > 0 ? (
              <div
                className="h-full"
                style={{ width: `${remainingPct}%` }}
                aria-label="Unused capacity"
              />
            ) : null}
          </div>
        </Tooltip.Provider>

        <div className="m-0 mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
          <ul className="m-0 flex flex-wrap gap-x-3 gap-y-2 list-none" role="list">
            {aggregateCategories(payload.categories).map(({ category, tokens }) => {
              const isActive = activeCategories.has(category);
              return (
                <li key={category}>
                  <button
                    type="button"
                    onClick={() => toggleCategory(category)}
                    onMouseEnter={() => setHoveredCategory(category)}
                    onMouseLeave={() => setHoveredCategory(null)}
                    onFocus={() => setHoveredCategory(category)}
                    onBlur={() => setHoveredCategory(null)}
                    aria-pressed={isActive}
                    className={cn(
                      "inline-flex cursor-pointer items-center gap-1.5 rounded-md px-1 py-0.5 text-caption transition-colors",
                      isActive
                        ? "bg-surface-emphasis text-foreground font-medium"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    <span className="inline-block h-2 w-2 rounded-[2px]" style={categoryDotStyle(category)} />
                    <span>{categoryLabel(category)}</span>
                    <span className="font-mono">{formatTokens(tokens)}</span>
                  </button>
                </li>
              );
            })}
            <li className="inline-flex items-center gap-1.5 text-caption text-muted-foreground">
              <Eye size={12} />
              <span>= appears in your terminal</span>
            </li>
          </ul>

          <div className="ml-auto flex items-center gap-2">
            <div className="relative">
              <Search size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search by name..."
                className="h-8 w-56 rounded-md border border-border-soft bg-card pl-7 pr-2 text-caption text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              />
            </div>
            {hasFilters ? (
              <Button size="sm" variant="ghost" onClick={clearFilters} className="h-8 gap-1 px-2 text-caption">
                <X size={14} /> Clear
              </Button>
            ) : null}
          </div>
        </div>
      </figure>

      <div className="grid grid-cols-[minmax(22rem,1.15fr)_minmax(20rem,0.85fr)] items-start gap-4 max-lg:grid-cols-1">
        <section className="min-w-0" aria-labelledby="event-stream-title">
          <h2 id="event-stream-title" className="sr-only">Event stream</h2>
          {filteredEvents.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border-soft p-8 text-center text-caption text-muted-foreground">
              No events match the current filters.
            </div>
          ) : (
            <ScrollArea.Root className="relative min-h-[18rem] max-h-[min(48rem,calc(100vh-14rem))] overflow-hidden">
              <ScrollArea.Viewport className="max-h-[min(48rem,calc(100vh-14rem))] min-h-[18rem] pe-3 scroll-py-3">
                <ol className="m-0 grid list-none gap-2 p-0">
                  {filteredEvents.map((event, index) => {
                    const previous = filteredEvents[index - 1];
                    const header = groupHeader(event, previous);
                    const isSelected = event.id === selectedId;
                    const isActive = event.id === activeId;
                    const isCategoryHighlight = hoveredCategory != null && event.category === hoveredCategory;
                    const color = eventColor(event);
                    const tokenPercent = totalUsedTokens > 0 && event.tokens
                      ? Math.max((event.tokens.value / totalUsedTokens) * 100, 2)
                      : 0;
                    const isSubagent = event.source === "subagent";
                    return (
                      <React.Fragment key={event.id}>
                        {header ? (
                          <li className={cn("list-none", isSubagent && "ml-4 border-l-2 border-border-subtle pl-4")}>
                            <h4 className={cn(
                              "eyebrow-soft text-muted-foreground",
                              index > 0 && "mt-4",
                            )}>
                              {header}
                            </h4>
                          </li>
                        ) : null}
                        <li className={cn("list-none", isSubagent && "ml-4 border-l-2 border-border-subtle pl-4")}>
                          <button
                            ref={(node) => { eventRefs.current[index] = node; }}
                            type="button"
                            className={cn(
                              "relative flex w-full items-center gap-3 overflow-hidden rounded-xl border border-border-soft bg-surface-subtle px-3 py-2.5 text-start text-foreground cursor-pointer",
                              "hover:border-primary/60 hover:bg-surface-emphasis",
                              isActive && "border-primary/60 bg-surface-emphasis",
                              isSelected && "ring-2 ring-primary ring-offset-1 ring-offset-card",
                              isCategoryHighlight && "bg-surface-emphasis",
                            )}
                            style={isCategoryHighlight ? { boxShadow: `inset 3px 0 0 0 ${color}` } : undefined}
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
                            <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
                            <Badge
                              variant="outline"
                              className="shrink-0 px-1.5 py-0 text-caption text-foreground"
                              style={{ backgroundColor: categoryTint(color, 0.15), borderColor: categoryTint(color, 0.25) }}
                            >
                              {categoryLabel(event.category)}
                            </Badge>
                            <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-body-sm">
                              {event.label}
                            </span>
                            <span className="flex shrink-0 items-center gap-2">
                              <span className="mono text-body-sm font-medium">
                                {event.tokens ? `+${formatTokens(event.tokens.value)}` : "-"}
                              </span>
                              {tokenPercent > 0 ? (
                                <span className="block h-1 w-12 overflow-hidden rounded-full bg-surface-emphasis">
                                  <span
                                    className="block h-full rounded-full"
                                    style={{ width: `${Math.min(tokenPercent, 100)}%`, background: color }}
                                  />
                                </span>
                              ) : null}
                              {event.terminal_visible ? (
                                <Eye size={14} className="text-muted-foreground" />
                              ) : null}
                            </span>
                          </button>
                        </li>
                      </React.Fragment>
                    );
                  })}
                </ol>
              </ScrollArea.Viewport>
              <ScrollArea.Scrollbar className="flex w-2.5 touch-none select-none bg-surface-subtle p-px" orientation="vertical">
                <ScrollArea.Thumb className="relative flex-1 rounded-full bg-foreground/28" />
              </ScrollArea.Scrollbar>
            </ScrollArea.Root>
          )}
        </section>

        <aside className="sticky top-4 rounded-xl border border-border-soft bg-card p-5 max-lg:static">
          {activeEvent ? (
            <>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-[2px]" style={{ background: eventColor(activeEvent) }} />
                    <h3 className="m-0 break-words font-display text-heading" title={activeEvent.label}>{activeEvent.label}</h3>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <Badge
                      variant="outline"
                      className="px-2 text-caption text-foreground"
                      style={{ backgroundColor: categoryTint(eventColor(activeEvent), 0.15), borderColor: categoryTint(eventColor(activeEvent), 0.25) }}
                    >
                      {categoryLabel(activeEvent.category)}
                    </Badge>
                    <span className="mono text-body-sm text-moss">{evidenceLabel(activeEvent.tokens)}</span>
                  </div>
                </div>
                <Button
                  size="icon"
                  variant={pinnedId === activeEvent.id ? "default" : "secondary"}
                  onClick={() => setPinnedId((current) => current === activeEvent.id ? null : activeEvent.id)}
                  aria-pressed={pinnedId === activeEvent.id}
                  aria-label={pinnedId === activeEvent.id ? "Unpin details" : "Pin details"}
                >
                  {pinnedId === activeEvent.id ? <PinOff size={15} /> : <Pin size={15} />}
                </Button>
              </div>
              <p className="mt-4 max-h-[18rem] overflow-auto whitespace-pre-wrap leading-relaxed">{activeEvent.summary ?? "No text preview is available."}</p>
              {activeEvent.terminal_visible ? (
                <div className="mt-4 overflow-hidden rounded-xl border border-border-soft bg-surface-subtle">
                  <div className="flex items-center gap-2 px-4 py-3 font-display text-body-sm font-bold">
                    <Info size={16} className="text-primary" />
                    One-liner in your terminal
                  </div>
                  <div className="border-t border-border-subtle px-4 py-3">
                    <p className="m-0 text-body-sm text-muted-foreground">
                      You see a brief mention, not the full content.
                    </p>
                  </div>
                </div>
              ) : null}
              <div className="mt-4 overflow-hidden rounded-xl border border-warning/30">
                <div className="bg-warning px-4 py-2 eyebrow text-white">
                  Key Takeaway
                </div>
                <div className="bg-surface-subtle px-4 py-4">
                  <p className="m-0 font-display text-body font-bold leading-snug">
                    {keyTakeaway(activeEvent)}
                  </p>
                </div>
              </div>
            </>
          ) : (
            <>
              <h3 className="m-0 font-display text-heading">Click any event</h3>
              <p className="mt-1 text-muted-foreground">Click to preview details. Pin to keep it selected while you scroll.</p>
            </>
          )}
        </aside>
      </div>

      <div className="sticky bottom-0 z-50 mt-2 rounded-xl border border-border-soft bg-card p-3 shadow-lg">
        <div className="flex items-center gap-3">
          <Button
            size="icon"
            variant="secondary"
            onClick={() => setIsPlaying((p) => !p)}
            aria-label={isPlaying ? "Pause playback" : "Play through events"}
          >
            {isPlaying ? <Pause size={16} /> : <Play size={16} />}
          </Button>
          <div className="flex-1">
            <div
              className="flex h-2 w-full overflow-hidden rounded-full bg-surface-emphasis"
              role="img"
              aria-label={`Replay progress: ${Math.max(playbackIndex, 0) + 1} of ${filteredEvents.length} events`}
            >
              <span
                className="block bg-moss transition-all duration-300"
                style={{ width: `${Math.min(playbackPct, 100)}%` }}
              />
            </div>
          </div>
          <span className="w-16 text-right mono text-body-sm">
            {filteredEvents.length > 0 ? `${Math.max(playbackIndex, 0) + 1}/${filteredEvents.length}` : "-"}
          </span>
          <Button
            size="icon"
            variant="secondary"
            onClick={toggleFullscreen}
            aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
          >
            {isFullscreen ? <Minimize size={16} /> : <Maximize size={16} />}
          </Button>
        </div>
      </div>
    </div>
  );
}

function SessionAnalysisPanel({ analysis }: { analysis: SessionAnalysis }) {
  const riskyBuckets = analysis.tool_evidence.buckets.filter((bucket) => bucket.judgment === "risky");
  const topBuckets = analysis.tool_evidence.buckets.slice(0, 6);
  return (
    <section className="grid gap-4" aria-labelledby="session-analysis-title">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="session-analysis-title" className="m-0 font-display text-heading">
            Session analysis
          </h2>
          <p className="m-0 mt-1 text-body-sm text-muted-foreground">
            Generated from ct session usage, stats, overview, and tool events.
          </p>
        </div>
        <Badge variant="outline" className="mono text-caption">
          {analysis.source}
        </Badge>
      </div>

      <div className="grid grid-cols-4 gap-3 max-lg:grid-cols-2 max-sm:grid-cols-1">
        <MetricTile label="Cumulative usage" value={formatTokens(analysis.usage_evidence.cumulative_tokens)} detail="all turns" />
        <MetricTile label="Final context" value={formatTokens(analysis.usage_evidence.final_context_tokens)} detail={formatPercent(analysis.usage_evidence.final_context_percent)} />
        <MetricTile label="Tool results" value={formatCount(analysis.tool_evidence.total_result_calls)} detail={`${analysis.tool_evidence.failed_result_calls} failed`} />
        <MetricTile label="Risky output" value={formatPercent(riskyBuckets.reduce((sum, bucket) => sum + bucket.output_share, 0))} detail={`${riskyBuckets.length} buckets`} />
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_minmax(18rem,0.7fr)] gap-4 max-lg:grid-cols-1">
        <div className="grid gap-3">
          {analysis.findings.map((finding, index) => (
            <article
              key={`${finding.kind}-${index}`}
              className={cn("rounded-xl border px-4 py-3", findingTone(finding.kind))}
            >
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="text-caption">{findingLabel(finding.kind)}</Badge>
                {finding.impact ? <span className="mono text-caption text-muted-foreground">{finding.impact}</span> : null}
              </div>
              <h3 className="m-0 mt-2 font-display text-body font-bold">{finding.title}</h3>
              <p className="m-0 mt-1 text-body-sm leading-relaxed text-muted-foreground">{finding.body}</p>
              {finding.evidence.length > 0 ? (
                <ul className="m-0 mt-2 flex list-none flex-wrap gap-1.5 p-0">
                  {finding.evidence.slice(0, 4).map((item) => (
                    <li key={item}>
                      <Badge variant="secondary" className="max-w-[18rem] overflow-hidden text-ellipsis whitespace-nowrap text-caption">
                        {item}
                      </Badge>
                    </li>
                  ))}
                </ul>
              ) : null}
            </article>
          ))}
        </div>

        <div className="rounded-xl border border-border-soft bg-surface-subtle p-4">
          <h3 className="m-0 font-display text-body font-bold">Output buckets</h3>
          <div className="mt-3 grid gap-3">
            {topBuckets.map((bucket) => (
              <div key={bucket.key}>
                <div className="mb-1 flex items-center justify-between gap-3 text-caption">
                  <span className="font-medium">{bucket.label}</span>
                  <span className="mono text-muted-foreground">{formatPercent(bucket.output_share)}</span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-surface-emphasis">
                  <span
                    className={cn(
                      "block h-full rounded-full",
                      bucket.judgment === "risky" ? "bg-warning" : bucket.judgment === "good" ? "bg-moss" : "bg-primary",
                    )}
                    style={{ width: `${Math.min(bucket.output_share, 100)}%` }}
                  />
                </div>
                <div className="mt-1 mono text-caption text-muted-foreground">
                  {bucket.calls} calls · {formatCount(bucket.output_chars)} chars
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function MetricTile({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="rounded-xl border border-border-soft bg-surface-subtle px-4 py-3">
      <p className="m-0 text-caption text-muted-foreground">{label}</p>
      <p className="m-0 mt-1 mono text-heading font-bold text-foreground">{value}</p>
      <p className="m-0 mt-1 text-caption text-muted-foreground">{detail}</p>
    </div>
  );
}
