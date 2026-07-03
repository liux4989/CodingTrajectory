import * as React from "react";
import { useParams, useRouter } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import * as Tooltip from "@radix-ui/react-tooltip";
import { AlertTriangle, ArrowLeft, Eye, Lightbulb, Pin, PinOff, Play, Pause, Maximize, Minimize, Info, Search, X, Sparkles, Square } from "lucide-react";
import {
  analyzeSession,
  fetchContextWindow,
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
type AnalysisFinding = SessionAnalysis["findings"][number];
type AnalysisEvidenceRef = AnalysisFinding["evidence"][number];
type TimelineEvidenceAnnotation = AnalysisEvidenceRef & {
  finding: Pick<AnalysisFinding, "kind" | "title">;
};

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

function readStoredJobId(storageKey: string) {
  if (typeof window === "undefined") return null;
  const value = window.sessionStorage.getItem(storageKey);
  return value?.trim() || null;
}

function writeStoredJobId(storageKey: string, jobId: string | null) {
  if (typeof window === "undefined") return;
  if (jobId) window.sessionStorage.setItem(storageKey, jobId);
  else window.sessionStorage.removeItem(storageKey);
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

function findingLabel(kind: SessionAnalysis["findings"][number]["kind"]) {
  if (kind === "justified_expensive_work") return "Justified";
  if (kind === "avoidable_pattern") return "Avoidable";
  if (kind === "optimal_pattern") return "Optimal";
  return "Next workflow";
}

function evidenceKindLabel(kind: AnalysisEvidenceRef["kind"]) {
  if (kind === "context_category") return "Context";
  if (kind === "tool_item") return "Tool item";
  if (kind === "tool_bucket") return "Tool bucket";
  return "Turn";
}

function evidenceRefMatchesEvent(ref: AnalysisEvidenceRef, event: ContextEvent) {
  if (ref.kind === "context_category") {
    return event.detail_ref.stats_category === ref.ref;
  }
  if (ref.kind === "tool_item") {
    return event.detail_ref.item_id === ref.ref;
  }
  if (ref.kind === "tool_bucket") {
    return event.detail_ref.tool_bucket === ref.ref;
  }
  return event.turn_id === ref.ref;
}

function evidenceBadgeTone(severity: AnalysisEvidenceRef["severity"]) {
  return severity === "warning"
    ? "border-warning/45 bg-warning/10 text-foreground"
    : "border-moss/40 bg-moss/10 text-foreground";
}

export function ContextWindowRoute() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId/context-window" });
  const router = useRouter();
  const analysisRefreshRef = React.useRef(false);
  const analysisStorageKey = `ct-dashboard-session-analysis:${sessionId}`;
  const [initialAnalysisJobId] = React.useState(() => readStoredJobId(analysisStorageKey));
  const query = useQuery({
    queryKey: ["context-window", sessionId],
    queryFn: () => fetchContextWindow(sessionId),
  });
  const analysisJob = useJob<SessionAnalysis>({
    initialJobId: initialAnalysisJobId,
    onJobId: (jobId) => writeStoredJobId(analysisStorageKey, jobId),
    start: () => analyzeSession(sessionId, analysisRefreshRef.current),
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
  const analysis = analysisJob.data;
  const totalUsedTokens = query.data?.used_tokens?.value ?? 0;

  const evidenceByEventId = React.useMemo(() => {
    const next = new Map<string, TimelineEvidenceAnnotation[]>();
    if (!analysis) return next;
    for (const event of events) {
      const matches: TimelineEvidenceAnnotation[] = [];
      for (const finding of analysis.findings) {
        for (const evidence of finding.evidence) {
          if (evidenceRefMatchesEvent(evidence, event)) {
            matches.push({
              ...evidence,
              finding: { kind: finding.kind, title: finding.title },
            });
          }
        }
      }
      if (matches.length > 0) next.set(event.id, matches);
    }
    return next;
  }, [analysis, events]);

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
  const activeEvidence = activeEvent ? evidenceByEventId.get(activeEvent.id) ?? [] : [];

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
    <div className="route-container w-full min-w-0 overflow-hidden pb-8">
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
              <Button
                size="sm"
                variant={analysis ? "secondary" : "default"}
                onClick={() => {
                  analysisRefreshRef.current = Boolean(analysis);
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
        <div className="alert alert-destructive rounded-xl text-body-sm text-foreground">
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
                placeholder="Filter events"
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

      <div className="context-window-shell">
        <div className="context-window-layout">
          <section className="min-w-0" aria-labelledby="event-stream-title">
            <h2 id="event-stream-title" className="sr-only">Event stream</h2>
            {filteredEvents.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border-soft p-8 text-center text-caption text-muted-foreground">
                No events match the current filters.
              </div>
            ) : (
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
                  const eventEvidence = evidenceByEventId.get(event.id) ?? [];
                  const hasWarning = eventEvidence.some((item) => item.severity === "warning");
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
                          className="event-row"
                          data-active={isActive || undefined}
                          data-selected={isSelected || undefined}
                          data-highlight={isCategoryHighlight || undefined}
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
                          <span className="event-row-meta">
                            <span className="event-row-token mono text-body-sm font-medium">
                              {event.tokens ? `+${formatTokens(event.tokens.value)}` : "-"}
                            </span>
                            {tokenPercent > 0 ? (
                              <span className="event-row-meter">
                                <span
                                  className="block h-full rounded-full"
                                  style={{ width: `${Math.min(tokenPercent, 100)}%`, background: color }}
                                />
                              </span>
                            ) : null}
                            {event.terminal_visible ? (
                              <Eye size={14} className="text-muted-foreground" />
                            ) : null}
                            {eventEvidence.length > 0 ? (
                              <span
                                className={cn(
                                  "inline-flex h-5 min-w-5 items-center justify-center rounded-md border px-1",
                                  hasWarning ? "border-warning/45 bg-warning/10 text-warning" : "border-moss/40 bg-moss/10 text-moss",
                                )}
                                title={`${eventEvidence.length} analysis evidence ${eventEvidence.length === 1 ? "match" : "matches"}`}
                              >
                                {hasWarning ? <AlertTriangle size={13} /> : <Lightbulb size={13} />}
                              </span>
                            ) : null}
                          </span>
                        </button>
                      </li>
                    </React.Fragment>
                  );
                })}
              </ol>
            )}
          </section>

          <aside className="context-detail-rail min-w-0 self-start">
            <div className="context-detail-pane rounded-[var(--radius-2xl)] p-6">
            <div className="context-detail-scroll">
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
                  <p className="mt-4 whitespace-pre-wrap leading-relaxed">{activeEvent.summary ?? "No text preview is available."}</p>
                  {activeEvent.terminal_visible ? (
                    <div className="panel-subtle mt-4 overflow-hidden rounded-xl">
                      <div className="flex items-center gap-2 px-4 py-3 font-display text-body-sm font-bold">
                        <Info size={16} className="text-primary" />
                        Visible in the terminal
                      </div>
                      <div className="border-t border-border-subtle px-4 py-3">
                        <p className="m-0 text-body-sm text-muted-foreground">
                          This event appeared in the terminal output at the time, but the dashboard does not expand the full terminal transcript here.
                        </p>
                      </div>
                    </div>
                  ) : null}
                  {activeEvidence.length > 0 ? (
                    <div className="mt-4 overflow-hidden rounded-xl border border-border-soft">
                      <div className="bg-surface-subtle px-4 py-2 eyebrow text-muted-foreground">
                        Analysis Evidence
                      </div>
                      <div className="grid gap-2 px-4 py-3">
                        {activeEvidence.map((item, index) => (
                          <div
                            key={`${item.kind}-${item.ref}-${index}`}
                            className={cn("rounded-lg border px-3 py-2", evidenceBadgeTone(item.severity))}
                          >
                            <div className="flex flex-wrap items-center gap-2">
                              <Badge variant="outline" className="text-caption">
                                {evidenceKindLabel(item.kind)}
                              </Badge>
                              <span className="text-caption font-medium">{item.label}</span>
                              <span className="text-caption text-muted-foreground">{item.finding.title}</span>
                            </div>
                            <p className="m-0 mt-1 text-caption leading-relaxed text-muted-foreground">
                              {item.detail}
                            </p>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </>
              ) : (
                <>
                  <h3 className="m-0 font-display text-heading">Click any event</h3>
                  <p className="mt-1 text-muted-foreground">Click to preview details. Pin to keep it selected while you scroll.</p>
                </>
              )}
            </div>
            </div>
          </aside>
        </div>
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
  return (
    <section className="panel-subtle rounded-xl px-4 py-3" aria-labelledby="session-analysis-title">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="session-analysis-title" className="m-0 font-display text-heading">
            Session overview
          </h2>
        </div>
        <Badge variant="outline" className="mono text-caption">
          Codex
        </Badge>
      </div>

      <div className="mt-3 grid gap-2">
        {analysis.findings.map((finding, index) => (
          <p key={`${finding.kind}-${index}`} className="m-0 text-body-sm leading-relaxed text-muted-foreground">
            <span className="font-medium text-foreground">{findingLabel(finding.kind)}: {finding.title}.</span>{" "}
            {finding.body}
            {finding.impact ? <span className="mono text-caption"> {finding.impact}</span> : null}
          </p>
        ))}
      </div>
    </section>
  );
}
