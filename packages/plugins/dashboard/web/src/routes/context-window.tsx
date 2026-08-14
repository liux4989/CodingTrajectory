import * as React from "react";
import { useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import * as Tooltip from "@radix-ui/react-tooltip";
import { AlertTriangle, Eye, Lightbulb, Pin, PinOff, Search, X, Sparkles, Square } from "lucide-react";
import {
  analyzeSession,
  fetchContextWindow,
  type CacheBreakRecord,
  type CacheBreakSummary,
  type CompactionSummary,
  type ContextCategory,
  type ContextEvent,
  type JobRecord,
  type SessionAnalysis,
  type TokenEvidence,
} from "@/api";
import {
  cacheBreakTone,
  formatCostUsd,
  formatIdleSeconds,
  formatTokens,
} from "@/lib/cache-breaks";
import { useJob } from "@/hooks/use-job";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/loading-state";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
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

function groupStartsHere(event: ContextEvent, previous?: ContextEvent): boolean {
  if (event.source === "subagent" && previous?.source !== "subagent") return true;
  if (event.group === "before_first_prompt" && (!previous || previous.group !== "before_first_prompt")) return true;
  if (event.group === "post_turn" && (!previous || previous.group !== "post_turn")) return true;
  if (event.group === "turn" && (!previous || previous.turn_id !== event.turn_id)) return true;
  return false;
}

function turnGroupKey(event: ContextEvent): string {
  if (event.source === "subagent") return "subagent";
  if (event.group === "before_first_prompt") return "before_first_prompt";
  if (event.group === "post_turn") return "post_turn";
  return `turn:${event.turn_id ?? "none"}`;
}

function turnGroupLabel(event: ContextEvent): string {
  if (event.source === "subagent") return "SUBAGENT'S SEPARATE CONTEXT WINDOW";
  if (event.group === "before_first_prompt") return "BEFORE YOU TYPE ANYTHING";
  if (event.source === "you") return "You";
  return "Claude works";
}

type TurnGroup = {
  key: string;
  label: string;
  isSubagent: boolean;
  totalTokens: number;
  events: Array<{ event: ContextEvent; index: number }>;
};

function buildTurnGroups(events: ContextEvent[]): TurnGroup[] {
  const groups: TurnGroup[] = [];
  for (let i = 0; i < events.length; i++) {
    const event = events[i];
    const previous = events[i - 1];
    if (groups.length === 0 || groupStartsHere(event, previous)) {
      groups.push({
        key: turnGroupKey(event),
        label: turnGroupLabel(event),
        isSubagent: event.source === "subagent",
        totalTokens: 0,
        events: [],
      });
    }
    const current = groups[groups.length - 1];
    current.events.push({ event, index: i });
    if (event.tokens) current.totalTokens += event.tokens.value;
  }
  return groups;
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
  const { sessionId } = useParams({ from: "/sessions/$sessionId" });
  const analysisRefreshRef = React.useRef(false);
  const analysisStorageKey = `ct-dashboard-session-analysis:${sessionId}`;
  const [initialAnalysisJobId] = React.useState(() => readStoredJobId(analysisStorageKey));
  const query = useQuery({
    queryKey: ["context-window", sessionId],
    queryFn: () => fetchContextWindow(sessionId),
    placeholderData: (previous) => previous,
    gcTime: 60_000,
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
  const [searchQuery, setSearchQuery] = React.useState("");
  const [activeCategories, setActiveCategories] = React.useState<Set<string>>(new Set());
  const [expandedGroups, setExpandedGroups] = React.useState<string[] | null>(null);
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

  const turnGroups = React.useMemo(() => buildTurnGroups(filteredEvents), [filteredEvents]);

  // Default to every turn expanded; once the user folds one, the explicit
  // array takes over. Accordion `value` is always controlled so this works
  // as a fall-untouched default.
  const expandedValue = expandedGroups ?? turnGroups.map((group) => group.key);

  const activeId = pinnedId ?? selectedId ?? filteredEvents[0]?.id ?? null;
  const activeEvent = events.find((event) => event.id === activeId) ?? null;

  const activeGroupId = React.useMemo(
    () => (activeId ? turnGroups.find((g) => g.events.some((e) => e.event.id === activeId))?.key ?? null : null),
    [turnGroups, activeId],
  );

  // Auto-expand the turn whose event becomes inspected/playing so the list
  // scrubber can never land on a hidden row.
  React.useEffect(() => {
    if (!activeGroupId) return;
    setExpandedGroups((current) => {
      if (!current || current.includes(activeGroupId)) return current;
      return [...current, activeGroupId];
    });
  }, [activeGroupId]);
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

  // turn_id -> break record, so the turn accordion header can flag the break
  // that landed on it (mirrors the markdown per-turn cache-break flag).
  const breaksByTurnId = React.useMemo(() => {
    const next = new Map<string, CacheBreakRecord>();
    for (const record of query.data?.cache_breaks?.events ?? []) {
      next.set(record.turn_id, record);
    }
    return next;
  }, [query.data?.cache_breaks]);

  const activeEvidence = activeEvent ? evidenceByEventId.get(activeEvent.id) ?? [] : [];

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

  if (query.isPending) {
    return <LoadingState title="Loading context window" detail="Reading normalized session projections." />;
  }
  if (query.isError) {
    return <StateBlock title="Context window failed" detail={query.error.message} onRetry={() => query.refetch()} />;
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
      <div className="context-window-shell">
        <div className="context-window-topbar">
          <div className="context-window-title-row">
            <div className="min-w-0">
              <h1 className="m-0 font-display text-h1 leading-tight">
                Explore the context window
              </h1>
              <p className="m-0 mt-1 text-body-sm text-muted-foreground">
                A session showing what enters context and what it costs
              </p>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-3">
              <div className="text-right">
                <p className="m-0 mono text-heading font-bold leading-none text-moss">
                  ~{formatTokens(payload.used_tokens?.value)}
                </p>
                <p className="m-0 mt-1 mono text-caption text-muted-foreground">
                  / {formatTokens(payload.context_window_tokens?.value)} tokens
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
          </div>
        </div>

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

        {payload.session_sections.length > 1 ? (
          <section className="rounded-lg border border-border-soft bg-card p-4" aria-label="Session graph context scopes">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <h2 className="m-0 font-display text-heading">Session graph scopes</h2>
                <p className="m-0 mt-1 text-caption text-muted-foreground">
                  Context composition below is scoped to the active session window.
                </p>
              </div>
              <Badge variant="outline" className="px-2 text-caption">
                Active {payload.active_session_id.slice(0, 8)}
              </Badge>
            </div>
            <div className="mt-3 grid gap-2 md:grid-cols-3">
              {payload.session_sections.map((section) => {
                const isActiveScope = section.session_id === payload.active_session_id;
                return (
                  <div
                    key={section.session_id}
                    className={cn(
                      "rounded-md border border-border-soft bg-surface-subtle p-3",
                      isActiveScope && "border-primary/60 bg-surface-emphasis",
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <Badge variant={isActiveScope ? "default" : "secondary"} className="px-1.5 py-0 text-caption">
                        {section.role}
                      </Badge>
                      <span className="mono text-caption text-muted-foreground">{section.session_id.slice(0, 8)}</span>
                    </div>
                    <p className="m-0 mt-2 truncate text-body-sm font-medium text-foreground" title={section.label}>
                      {section.label}
                    </p>
                    <p className="m-0 mt-1 mono text-caption text-muted-foreground">
                      {formatTokens(section.used_tokens?.value)} tokens
                      {section.used_percent != null ? ` · ${section.used_percent.toFixed(1)}%` : ""}
                    </p>
                  </div>
                );
              })}
            </div>
          </section>
        ) : null}

        <figure className="m-0">
          <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
            <figcaption className="m-0 text-body-sm text-muted-foreground">
              Context composition
            </figcaption>
            <span className="mono text-caption text-muted-foreground">
              {payload.used_percent == null ? "unknown" : `${payload.used_percent.toFixed(1)}%`} used
            </span>
          </div>
          <Tooltip.Provider delayDuration={160} skipDelayDuration={120}>
            <div
              className="flex h-8 w-full overflow-hidden rounded-md border border-border-soft bg-surface-emphasis"
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
                  className="h-8 w-56 max-w-[calc(100vw-4rem)] rounded-md border border-border-soft bg-card pl-7 pr-2 text-caption text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
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

        {payload.cache_breaks && payload.cache_breaks.count > 0 ? (
          <CacheBreaksPanel cacheBreaks={payload.cache_breaks} />
        ) : null}

        {payload.compaction && payload.compaction.events.length > 0 ? (
          <CompactionTimeline compaction={payload.compaction} />
        ) : null}

        <div className="context-window-layout">
          <section className="min-w-0" aria-labelledby="event-stream-title">
            <h2 id="event-stream-title" className="sr-only">Event stream</h2>
            {filteredEvents.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border-soft p-8 text-center text-caption text-muted-foreground">
                No events match the current filters.
              </div>
            ) : (
              <Accordion
                type="multiple"
                value={expandedValue}
                onValueChange={setExpandedGroups}
                className="flex flex-col gap-2"
              >
                {turnGroups.map((group) => {
                  const containsActive = activeGroupId === group.key;
                  const hasWarning = group.events.some(({ event }) =>
                    (evidenceByEventId.get(event.id) ?? []).some((item) => item.severity === "warning"),
                  );
                  const breakRecord = group.events[0]?.event.turn_id
                    ? breaksByTurnId.get(group.events[0].event.turn_id) ?? null
                    : null;
                  const breakTone = breakRecord ? cacheBreakTone(breakRecord.type, breakRecord.effort_from, breakRecord.effort_to) : null;
                  return (
                    <AccordionItem
                      key={group.key}
                      value={group.key}
                      className={cn(
                        "border-b-0",
                        group.isSubagent && "ml-4 border-l-2 border-border-subtle pl-3",
                      )}
                    >
                      <AccordionTrigger
                        className={cn(
                          "items-center rounded-md px-2.5 py-2 text-caption font-normal hover:no-underline",
                          containsActive && "border border-primary/50 bg-surface-emphasis",
                        )}
                      >
                        <span className="eyebrow-soft min-w-0 flex-1 truncate text-left text-muted-foreground">
                          {group.label}
                        </span>
                        {hasWarning ? <AlertTriangle size={12} className="shrink-0 text-warning" /> : null}
                        {breakRecord && breakTone ? (
                          <span
                            className={cn(
                              "inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-0 text-caption",
                              breakTone.className,
                            )}
                            title={`${breakTone.label}: ${formatIdleSeconds(breakRecord.idle_seconds)} idle -> ${formatTokens(breakRecord.re_read_tokens)} cache-hit loss${breakRecord.est_cost_usd != null ? ` (${formatCostUsd(breakRecord.est_cost_usd)} estimated premium)` : ""}`}
                          >
                            {breakTone.icon}
                            <span className="hidden sm:inline">{breakTone.label}</span>
                            <span className="mono">{formatTokens(breakRecord.re_read_tokens)}</span>
                          </span>
                        ) : null}
                        <span className="hidden shrink-0 sm:inline mono text-caption text-muted-foreground">
                          {formatTokens(group.totalTokens)}
                        </span>
                        <span className="hidden shrink-0 sm:inline text-caption text-muted-foreground/70">·</span>
                        <span className="shrink-0 mono text-caption text-muted-foreground">{group.events.length}</span>
                      </AccordionTrigger>
                      <AccordionContent className="pt-0 pb-0">
                        <ol className="m-0 mt-1.5 grid list-none gap-2 p-0">
                          {group.events.map(({ event, index }) => {
                            const isSelected = event.id === selectedId;
                            const isActive = event.id === activeId;
                            const isCategoryHighlight = hoveredCategory != null && event.category === hoveredCategory;
                            const color = eventColor(event);
                            const tokenPercent = totalUsedTokens > 0 && event.tokens
                              ? Math.max((event.tokens.value / totalUsedTokens) * 100, 2)
                              : 0;
                            const eventEvidence = evidenceByEventId.get(event.id) ?? [];
                            const eventHasWarning = eventEvidence.some((item) => item.severity === "warning");
                            return (
                              <li key={event.id} className="list-none">
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
                                          eventHasWarning ? "border-warning/45 bg-warning/10 text-warning" : "border-moss/40 bg-moss/10 text-moss",
                                        )}
                                        title={`${eventEvidence.length} analysis evidence ${eventEvidence.length === 1 ? "match" : "matches"}`}
                                      >
                                        {eventHasWarning ? <AlertTriangle size={13} /> : <Lightbulb size={13} />}
                                      </span>
                                    ) : null}
                                  </span>
                                </button>
                              </li>
                            );
                          })}
                        </ol>
                      </AccordionContent>
                    </AccordionItem>
                  );
                })}
              </Accordion>
            )}
          </section>

          <aside className="context-detail-rail min-w-0">
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

function CompactionTimeline({ compaction }: { compaction: CompactionSummary }) {
  const totalDropped = compaction.cumulative_dropped_tokens ?? 0;
  return (
    <section
      className="rounded-xl border border-border-soft bg-card px-4 py-3"
      aria-labelledby="compaction-timeline-title"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="min-w-0">
          <h2 id="compaction-timeline-title" className="m-0 font-display text-heading">
            Compaction timeline
          </h2>
          <p className="m-0 mt-1 text-body-sm text-muted-foreground">
            {compaction.count} compaction{compaction.count === 1 ? "" : "s"} evicted context
            {totalDropped > 0 ? (
              <span className="mono text-caption"> · {formatTokens(totalDropped)} tokens dropped</span>
            ) : null}
          </p>
        </div>
      </div>

      <ol className="m-0 mt-3 grid list-none gap-2 p-0">
        {compaction.events.map((event, index) => {
          const pre = event.pre_tokens;
          const post = event.post_tokens;
          const dropped = event.dropped_tokens;
          const hasDelta = pre != null && post != null;
          const isCodexCompaction = event.mechanism === "context_compacted";
          const timestampLabel = formatCompactionTimestamp(event.timestamp);
          // ``context_compacted`` (Codex) exposes no pre/post/dropped in the
          // pre/post/dropped, so render the mechanism label instead of a bare
          // ``-`` (which would read as missing data rather than "not exposed").
          const deltaLabel = hasDelta
            ? `${formatTokens(pre)} → ${formatTokens(post)}`
            : dropped != null
              ? `${formatTokens(dropped)} dropped`
              : isCodexCompaction
                ? "context compacted"
                : "-";
          return (
            <li
              key={`${event.timestamp}-${index}`}
              className="event-row"
              style={{ cursor: "default" }}
            >
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ background: "var(--color-category-unattributed)" }}
              />
              <Badge variant="outline" className="shrink-0 px-1.5 py-0 text-caption text-foreground">
                {event.trigger ?? "auto"}
              </Badge>
              {event.mechanism === "sliding_window" ? (
                <Badge variant="outline" className="shrink-0 px-1.5 py-0 text-caption text-muted-foreground">
                  sliding
                </Badge>
              ) : null}
              <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-body-sm">
                <span className="mono text-caption text-muted-foreground">{timestampLabel}</span>
              </span>
              <span className="event-row-meta">
                <span className="event-row-token mono text-body-sm font-medium">
                  {deltaLabel}
                </span>
                {dropped != null && dropped > 0 ? (
                  <span className="event-row-meter" aria-hidden="true">
                    <span
                      className="block h-full rounded-full"
                      style={{
                        width: "100%",
                        background: "var(--color-category-unattributed)",
                        opacity: 0.6,
                      }}
                    />
                  </span>
                ) : null}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function formatCompactionTimestamp(value: string) {
  if (!value) return "-";
  // Truncate to ``YYYY-MM-DD HH:MM`` (UTC) for compactness.
  return value.slice(0, 16).replace("T", " ");
}

const CACHE_BREAK_TYPE_ORDER: CacheBreakRecord["type"][] = [
  "effort_switch",
  "ttl_confirmed",
  "ttl_likely",
];

function CacheBreaksPanel({ cacheBreaks }: { cacheBreaks: CacheBreakSummary }) {
  const confirmedCount = cacheBreaks.events.filter((record) => record.effort_to).length;
  const typeSummary = CACHE_BREAK_TYPE_ORDER
    .filter((key) => cacheBreaks.by_type[key])
    .map((key) => `${cacheBreaks.by_type[key]} ${key}`)
    .join(", ");
  return (
    <section
      className="rounded-xl border border-border-soft bg-card px-4 py-3"
      aria-labelledby="cache-breaks-title"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="min-w-0">
          <h2 id="cache-breaks-title" className="m-0 font-display text-heading">
            Cache breaks
          </h2>
          <p className="m-0 mt-1 text-body-sm text-muted-foreground">
            {cacheBreaks.count} turn{cacheBreaks.count === 1 ? "" : "s"} with measured cache-hit loss
            <span className="mono text-caption"> · {formatTokens(cacheBreaks.total_re_read_tokens)} affected tokens</span>
            {cacheBreaks.estimated_waste_usd != null ? (
              <span className="mono text-caption"> · {formatCostUsd(cacheBreaks.estimated_waste_usd)} estimated premium</span>
            ) : null}
            {typeSummary ? <span className="mono text-caption"> · {typeSummary}</span> : null}
            {confirmedCount > 0 ? (
              <span className="mono text-caption"> · {confirmedCount} confirmed</span>
            ) : null}
          </p>
        </div>
      </div>

      <ol className="m-0 mt-3 grid list-none gap-2 p-0">
        {cacheBreaks.events.map((record, index) => {
          const tone = cacheBreakTone(record.type, record.effort_from, record.effort_to);
          const effort = record.effort_to
            ? record.effort_from
              ? `${record.effort_from}->${record.effort_to}`
              : `->${record.effort_to}`
            : null;
          return (
            <li
              key={`${record.turn_id}-${index}`}
              className="event-row"
              style={{ cursor: "default" }}
            >
              <span
                className={cn(
                  "inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-md border px-1",
                  tone.className,
                )}
                title={tone.label}
              >
                {tone.icon}
              </span>
              <Badge variant="outline" className="shrink-0 px-1.5 py-0 text-caption text-foreground">
                {tone.label}
              </Badge>
              <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-body-sm text-muted-foreground">
                <span className="mono text-caption">turn {record.turn_id.slice(0, 10)}</span>
                {effort ? <span className="mono text-caption text-warning"> · {effort}</span> : null}
              </span>
              <span className="event-row-meta">
                <span className="event-row-token mono text-body-sm font-medium">
                  {formatTokens(record.re_read_tokens)}
                </span>
                <span className="mono text-caption text-muted-foreground">
                  {formatIdleSeconds(record.idle_seconds)} idle
                </span>
                {record.est_cost_usd != null ? (
                  <span className="mono text-caption text-muted-foreground">
                    {formatCostUsd(record.est_cost_usd)}
                  </span>
                ) : null}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
