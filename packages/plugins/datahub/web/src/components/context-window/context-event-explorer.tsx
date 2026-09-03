import * as React from "react";
import type { ContextEvent, ContextWindowPayload } from "@/api";
import { formatTokens } from "@/lib/cache-breaks";
import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Eye, Search, X } from "lucide-react";
import {
  aggregateCategories,
  buildTurnGroups,
  categoryDotStyle,
  categoryLabel,
  categoryTint,
  eventColor,
  eventTarget,
  evidenceLabel,
  type TurnGroup,
} from "./shared";

const GROUP_PREVIEW_COUNT = 12;

/**
 * Searchable, grouped context history with on-demand event detail. Desktop
 * shows a sticky detail column; mobile opens detail in a Sheet. One
 * page-level scroll — no nested scroll regions.
 */
export function ContextEventExplorer({ payload }: { payload: ContextWindowPayload }) {
  const events = payload.events;
  const isMobile = useIsMobile();
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [searchQuery, setSearchQuery] = React.useState("");
  const [activeCategories, setActiveCategories] = React.useState<string[]>([]);
  const [expandedGroups, setExpandedGroups] = React.useState<string[] | null>(null);
  const [expandedRows, setExpandedRows] = React.useState<ReadonlySet<string>>(new Set());
  const eventRefs = React.useRef<Array<HTMLButtonElement | null>>([]);

  const presentCategories = React.useMemo(
    () => aggregateCategories(payload.categories).map((item) => item.category),
    [payload.categories],
  );

  const hasFilters = activeCategories.length > 0 || searchQuery.trim().length > 0;
  const filteredEvents = React.useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    const categories = new Set(activeCategories);
    return events.filter((event) => {
      if (categories.size > 0 && !categories.has(event.category)) return false;
      if (query && !event.label.toLowerCase().includes(query)) return false;
      return true;
    });
  }, [events, activeCategories, searchQuery]);

  const turnGroups = React.useMemo(() => buildTurnGroups(filteredEvents), [filteredEvents]);
  const indexById = React.useMemo(
    () => new Map(filteredEvents.map((event, index) => [event.id, index])),
    [filteredEvents],
  );

  // Latest turn expanded; everything else (incl. before-first-prompt)
  // collapsed until the user folds groups explicitly. While filtering, all
  // matching groups stay open so results are never hidden.
  const defaultExpanded = React.useMemo(() => {
    const lastTurn = [...turnGroups].reverse().find((group) => group.key.startsWith("turn:"));
    if (lastTurn) return [lastTurn.key];
    return turnGroups.length === 1 ? [turnGroups[0].key] : [];
  }, [turnGroups]);
  const expandedValue = hasFilters
    ? turnGroups.map((group) => group.key)
    : (expandedGroups ?? defaultExpanded);

  const selectedEvent = events.find((event) => event.id === selectedId) ?? null;
  const selectedGroupKey = React.useMemo(
    () =>
      selectedId
        ? (turnGroups.find((group) => group.events.some((event) => event.id === selectedId))?.key ??
          null)
        : null,
    [turnGroups, selectedId],
  );

  // Keep the selected event's group open so selection never lands on a
  // hidden row.
  React.useEffect(() => {
    if (!selectedGroupKey || hasFilters) return;
    setExpandedGroups((current) => {
      const base = current ?? defaultExpanded;
      return base.includes(selectedGroupKey) ? current : [...base, selectedGroupKey];
    });
  }, [selectedGroupKey, hasFilters, defaultExpanded]);

  function clearSelection() {
    setSelectedId(null);
  }

  function toggleSelected(event: ContextEvent) {
    setSelectedId((current) => (current === event.id ? null : event.id));
  }

  function clearFilters() {
    setActiveCategories([]);
    setSearchQuery("");
  }

  function moveFocus(index: number, direction: -1 | 1) {
    const next = Math.min(Math.max(index + direction, 0), filteredEvents.length - 1);
    eventRefs.current[next]?.focus();
    setSelectedId(filteredEvents[next]?.id ?? null);
  }

  return (
    <section
      aria-labelledby="context-history-title"
      className="grid gap-3"
      onKeyDown={(event) => {
        if (event.key === "Escape" && selectedId) {
          event.stopPropagation();
          clearSelection();
        }
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="context-history-title" className="m-0 font-display text-heading">
          Context history
        </h2>
        {hasFilters ? (
          <span className="text-caption text-muted-foreground" role="status">
            {filteredEvents.length} of {events.length} events
          </span>
        ) : null}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search
              size={14}
              className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground"
            />
            <Input
              type="search"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Search history"
              aria-label="Search context history"
              className="h-8 w-56 max-w-[calc(100vw-4rem)] pl-7 text-caption"
            />
          </div>
          {presentCategories.length > 0 ? (
            <ToggleGroup
              type="multiple"
              variant="outline"
              size="sm"
              value={activeCategories}
              onValueChange={setActiveCategories}
              aria-label="Filter by category"
              className="flex-wrap"
            >
              {presentCategories.map((category) => (
                <ToggleGroupItem
                  key={category}
                  value={category}
                  aria-label={`Filter ${categoryLabel(category)}`}
                  className="gap-1.5 px-2 text-caption"
                >
                  <span className="inline-block h-2 w-2 rounded-[2px]" style={categoryDotStyle(category)} />
                  {categoryLabel(category)}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          ) : null}
          {hasFilters ? (
            <Button size="sm" variant="ghost" onClick={clearFilters} className="h-8 gap-1 px-2 text-caption">
              <X size={14} /> Reset
            </Button>
          ) : null}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,22rem)]">
        <div className="min-w-0">
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
              {turnGroups.map((group) => (
                <TurnGroupSection
                  key={group.key}
                  group={group}
                  selectedId={selectedId}
                  showAll={expandedRows.has(group.key)}
                  onShowAll={() =>
                    setExpandedRows((current) => new Set(current).add(group.key))
                  }
                  onToggleSelected={toggleSelected}
                  onRowFocus={setSelectedId}
                  onArrowKey={moveFocus}
                  indexById={indexById}
                  registerRef={(event, node) => {
                    const index = indexById.get(event.id);
                    if (index != null) eventRefs.current[index] = node;
                  }}
                />
              ))}
            </Accordion>
          )}
        </div>

        {!isMobile ? (
          <aside className="min-w-0 self-start lg:sticky lg:top-4" aria-label="Event detail">
            <div className="rounded-xl border border-border-soft bg-card p-5">
              {selectedEvent ? (
                <EventDetail event={selectedEvent} onClose={clearSelection} />
              ) : (
                <SessionExplainer payload={payload} />
              )}
            </div>
          </aside>
        ) : null}
      </div>

      {isMobile ? (
        <Sheet open={selectedEvent != null} onOpenChange={(open) => (!open ? clearSelection() : null)}>
          <SheetContent side="bottom" className="max-h-[85dvh] overflow-y-auto">
            {selectedEvent ? (
              <>
                <SheetHeader>
                  <SheetTitle>{selectedEvent.label}</SheetTitle>
                  <SheetDescription>{categoryLabel(selectedEvent.category)}</SheetDescription>
                </SheetHeader>
                <div className="px-4 pb-6">
                  <EventDetail event={selectedEvent} heading={false} />
                </div>
              </>
            ) : null}
          </SheetContent>
        </Sheet>
      ) : null}
    </section>
  );
}

function TurnGroupSection({
  group,
  selectedId,
  showAll,
  onShowAll,
  onToggleSelected,
  onRowFocus,
  onArrowKey,
  indexById,
  registerRef,
}: {
  group: TurnGroup;
  selectedId: string | null;
  showAll: boolean;
  onShowAll: () => void;
  onToggleSelected: (event: ContextEvent) => void;
  onRowFocus: (id: string) => void;
  onArrowKey: (index: number, direction: -1 | 1) => void;
  indexById: Map<string, number>;
  registerRef: (event: ContextEvent, node: HTMLButtonElement | null) => void;
}) {
  const visible = showAll ? group.events : group.events.slice(0, GROUP_PREVIEW_COUNT);
  const hiddenCount = group.events.length - visible.length;
  return (
    <AccordionItem value={group.key} className="border-b-0">
      <AccordionTrigger className="items-center rounded-md px-2.5 py-2 text-caption font-normal hover:no-underline">
        <span className="min-w-0 flex-1 truncate text-left font-medium text-foreground">
          {group.label}
        </span>
        <span className="shrink-0 text-caption text-muted-foreground">
          {group.events.length} event{group.events.length === 1 ? "" : "s"}
        </span>
        <span className="shrink-0 text-caption text-muted-foreground/70">·</span>
        <span className="mono shrink-0 text-caption text-muted-foreground">
          {formatTokens(group.totalTokens)}
        </span>
      </AccordionTrigger>
      <AccordionContent className="pb-0 pt-0">
        <ol className="tree-children m-0 mt-1.5 grid list-none gap-1.5">
          {visible.map((event) => {
            const index = indexById.get(event.id) ?? 0;
            return (
              <li key={event.id} className="list-none">
                <EventRow
                  event={event}
                  selected={event.id === selectedId}
                  ref={(node) => registerRef(event, node)}
                  onClick={() => onToggleSelected(event)}
                  onFocus={() => onRowFocus(event.id)}
                  onKeyDown={(keyEvent) => {
                    if (keyEvent.key === "ArrowDown") {
                      keyEvent.preventDefault();
                      onArrowKey(index, 1);
                    } else if (keyEvent.key === "ArrowUp") {
                      keyEvent.preventDefault();
                      onArrowKey(index, -1);
                    }
                  }}
                />
              </li>
            );
          })}
        </ol>
        {hiddenCount > 0 ? (
          <Button
            size="sm"
            variant="ghost"
            className="ml-[1.35rem] mt-1.5 h-7 px-2 text-caption"
            onClick={onShowAll}
          >
            Show {hiddenCount} more
          </Button>
        ) : null}
      </AccordionContent>
    </AccordionItem>
  );
}

const EventRow = React.forwardRef<
  HTMLButtonElement,
  {
    event: ContextEvent;
    selected: boolean;
    onClick: () => void;
    onFocus: () => void;
    onKeyDown: (event: React.KeyboardEvent<HTMLButtonElement>) => void;
  }
>(function EventRow({ event, selected, onClick, onFocus, onKeyDown }, ref) {
  const color = eventColor(event);
  const target = eventTarget(event);
  return (
    <button
      ref={ref}
      type="button"
      className="event-row"
      data-selected={selected || undefined}
      aria-pressed={selected}
      onClick={onClick}
      onFocus={onFocus}
      onKeyDown={onKeyDown}
    >
      <span
        className="mt-1 h-2 w-2 shrink-0 self-start rounded-full"
        style={{ background: color }}
        aria-hidden="true"
      />
      <span className="grid min-w-0 gap-0.5">
        <span className="flex items-baseline gap-3">
          <span className="min-w-0 flex-1 truncate text-start text-body-sm">{event.label}</span>
          <span className="mono shrink-0 text-body-sm font-medium">
            {event.tokens ? `+${formatTokens(event.tokens.value)}` : "-"}
          </span>
        </span>
        <span className="flex min-w-0 items-center gap-1.5 text-caption text-muted-foreground">
          {target ? (
            <>
              <span className="min-w-0 truncate" title={target}>
                {target}
              </span>
              <span className="shrink-0" aria-hidden="true">
                ·
              </span>
            </>
          ) : null}
          <span className="shrink-0">{categoryLabel(event.category)}</span>
          {event.terminal_visible ? (
            <span className="inline-flex shrink-0 items-center gap-1">
              · <Eye size={12} aria-hidden="true" /> terminal-visible
            </span>
          ) : null}
        </span>
      </span>
    </button>
  );
});

/** Whole-session placeholder while no event is selected. */
function SessionExplainer({ payload }: { payload: ContextWindowPayload }) {
  const top = [...aggregateCategories(payload.categories)].sort((a, b) => b.tokens - a.tokens)[0];
  return (
    <div className="grid gap-2">
      <h3 className="m-0 font-display text-heading">Session summary</h3>
      <p className="m-0 text-body-sm text-muted-foreground">
        {payload.events.length} events across this session
        {top ? (
          <>
            ; the largest consumer is{" "}
            <span className="text-foreground">{categoryLabel(top.category)}</span> at{" "}
            <span className="mono">{formatTokens(top.tokens)}</span>
          </>
        ) : null}
        .
      </p>
      <p className="m-0 text-caption text-muted-foreground">
        Select an event to inspect its evidence. Click again or press Escape to return here.
      </p>
    </div>
  );
}

function EventDetail({
  event,
  onClose,
  heading = true,
}: {
  event: ContextEvent;
  onClose?: () => void;
  heading?: boolean;
}) {
  const color = eventColor(event);
  return (
    <div className="grid gap-3">
      {heading ? (
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <span
              className="inline-block h-2.5 w-2.5 shrink-0 rounded-[2px]"
              style={{ background: color }}
              aria-hidden="true"
            />
            <h3 className="m-0 break-words font-display text-heading" title={event.label}>
              {event.label}
            </h3>
          </div>
          {onClose ? (
            <Button
              size="icon"
              variant="ghost"
              onClick={onClose}
              aria-label="Close event detail"
              className="h-7 w-7 shrink-0"
            >
              <X size={14} />
            </Button>
          ) : null}
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          variant="outline"
          className="px-2 text-caption text-foreground"
          style={{
            backgroundColor: categoryTint(color, 0.15),
            borderColor: categoryTint(color, 0.25),
          }}
        >
          {categoryLabel(event.category)}
        </Badge>
        <span className="mono text-body-sm text-moss">{evidenceLabel(event.tokens)}</span>
        {event.terminal_visible ? (
          <span className="inline-flex items-center gap-1 text-caption text-muted-foreground">
            <Eye size={12} aria-hidden="true" /> terminal-visible
          </span>
        ) : null}
      </div>
      <p className="m-0 whitespace-pre-wrap text-body-sm leading-relaxed">
        {event.summary ?? "No text preview is available."}
      </p>
    </div>
  );
}
