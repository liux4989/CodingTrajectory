import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { X } from "lucide-react";

import {
  fetchSessionEventDetails,
  fetchSessionItemDetails,
  type SessionTimelineEntry,
  type TimelineArtifactKind,
  type TimelineBranch,
  type TimelineKind,
} from "@/api";
import { SessionLink, shortSessionId } from "@/components/session-link";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { formatCostUsd, formatDuration, formatTokens } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  agentLabel,
  artifactLabel,
  formatWhen,
  isTerminalSuccess,
  kindIcon,
  kindLabel,
  type OutcomeFilter,
} from "./shared";

const TURN_PREVIEW_COUNT = 20;

export type EvidenceFilterState = {
  kind: TimelineKind | "all";
  artifact: TimelineArtifactKind | "all";
  agent: string;
  outcome: OutcomeFilter;
  entry: string | undefined;
};

export type EvidenceFilterUpdate = {
  kind?: TimelineKind;
  artifact?: TimelineArtifactKind;
  agent?: string;
  outcome?: Exclude<OutcomeFilter, "all">;
  entry?: string;
};

type TurnNode = {
  key: string;
  sequence: number;
  entries: SessionTimelineEntry[];
  failures: number;
};

type BranchNode = {
  sessionId: string;
  label: string;
  vendor: string | null;
  entries: SessionTimelineEntry[];
  turns: TurnNode[];
  children: BranchNode[];
  failures: number;
  firstPosition: number;
};

/**
 * Filterable evidence tree: agent branch -> turn -> entry, so the session /
 * subagent hierarchy is explicit instead of a flat interleave. Chronology is
 * preserved inside each branch (position order) and via row timestamps.
 * Filters change presentation only; selection deep-links through the `entry`
 * search param. One row expands at a time and auto-loads source detail.
 */
export function EvidenceExplorer({
  entries,
  branches,
  state,
  onChange,
}: {
  entries: SessionTimelineEntry[];
  branches: TimelineBranch[];
  state: EvidenceFilterState;
  onChange: (updates: EvidenceFilterUpdate) => void;
}) {
  const { kind, artifact, agent, outcome } = state;
  const [expandedBranches, setExpandedBranches] = React.useState<string[] | null>(null);
  const [expandedTurns, setExpandedTurns] = React.useState<string[] | null>(null);
  const [fullTurns, setFullTurns] = React.useState<ReadonlySet<string>>(new Set());

  const agents = React.useMemo(
    () => Array.from(new Map(entries.map((entry) => [entry.session_id, agentLabel(entry)])).entries()),
    [entries],
  );
  const presentKinds = React.useMemo(
    () => Array.from(new Set(entries.map((entry) => entry.kind))),
    [entries],
  );
  const presentArtifacts = React.useMemo(
    () =>
      Array.from(new Set(entries.map((entry) => entry.artifact_kind).filter(Boolean))) as TimelineArtifactKind[],
    [entries],
  );
  const hasFailures = React.useMemo(() => entries.some((entry) => entry.failed), [entries]);

  const filtered = React.useMemo(
    () =>
      entries.filter((entry) => {
        if (kind !== "all" && entry.kind !== kind) return false;
        if (artifact !== "all" && entry.artifact_kind !== artifact) return false;
        if (agent !== "all" && entry.session_id !== agent) return false;
        if (outcome === "failed" && !entry.failed) return false;
        if (outcome === "succeeded" && (entry.failed || !isTerminalSuccess(entry.status))) return false;
        return true;
      }),
    [entries, kind, artifact, agent, outcome],
  );

  // Branch parentage is authoritative from the projection (`branches`), so
  // filtering cannot orphan a child branch: parents resolve over the full
  // entry set regardless of which entries match.
  const roots = React.useMemo(
    () => buildBranchTree(entries, filtered, branches),
    [entries, filtered, branches],
  );
  const hasFilters = kind !== "all" || artifact !== "all" || agent !== "all" || outcome !== "all";

  const allBranchKeys = React.useMemo(() => roots.flatMap(collectBranchKeys), [roots]);
  const allTurnKeys = React.useMemo(
    () => roots.flatMap((root) => collectTurnKeys(root)),
    [roots],
  );
  const latestTurnKey = React.useMemo(() => {
    let latest: TurnNode | null = null;
    for (const root of roots) {
      for (const turn of flattenTurns(root)) {
        const at = turn.entries[0]?.position ?? -1;
        if (latest == null || at > (latest.entries[0]?.position ?? -1)) latest = turn;
      }
    }
    return latest?.key ?? null;
  }, [roots]);

  // Default: every branch open (the outline reads as a tree), latest turn
  // open, other turns folded. While filtering, everything with matches opens.
  const branchValue = hasFilters ? allBranchKeys : (expandedBranches ?? allBranchKeys);
  const turnValue = hasFilters
    ? allTurnKeys
    : (expandedTurns ?? (latestTurnKey ? [latestTurnKey] : []));

  const selected = state.entry
    ? (entries.find((entry) => entry.id === state.entry) ?? null)
    : null;

  // Keep the selected entry's branch/turn open and fully paged so a deep
  // link never lands on a hidden row.
  const selectedTurnKey = selected ? `${selected.session_id}:${selected.turn_id}` : null;
  const selectedBranchKeys = React.useMemo(
    () => (selected ? ancestorBranchKeys(roots, selected.session_id) : []),
    [roots, selected],
  );
  React.useEffect(() => {
    if (!selected || hasFilters) return;
    setExpandedBranches((current) => {
      const base = current ?? allBranchKeys;
      const missing = selectedBranchKeys.filter((key) => !base.includes(key));
      return missing.length ? [...base, ...missing] : current;
    });
    setExpandedTurns((current) => {
      const base = current ?? (latestTurnKey ? [latestTurnKey] : []);
      return selectedTurnKey && !base.includes(selectedTurnKey) ? [...base, selectedTurnKey] : current;
    });
    if (selectedTurnKey) {
      setFullTurns((current) =>
        current.has(selectedTurnKey) ? current : new Set(current).add(selectedTurnKey),
      );
    }
  }, [selected, hasFilters, selectedBranchKeys, selectedTurnKey, allBranchKeys, latestTurnKey]);

  function toggleEntry(entry: SessionTimelineEntry) {
    onChange({ entry: state.entry === entry.id ? undefined : entry.id });
  }

  return (
    <section
      aria-labelledby="evidence-title"
      className="grid gap-3"
      onKeyDown={(event) => {
        if (event.key === "Escape" && state.entry) {
          event.stopPropagation();
          onChange({ entry: undefined });
        }
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="evidence-title" className="m-0 font-display text-heading">
          Evidence
        </h2>
        {hasFilters ? (
          <span className="text-caption text-muted-foreground" role="status">
            {filtered.length} of {entries.length} entries
          </span>
        ) : null}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {presentKinds.length > 1 ? (
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={kind === "all" ? "" : kind}
              onValueChange={(value) =>
                onChange({ kind: (value || undefined) as TimelineKind | undefined, entry: undefined })
              }
              aria-label="Filter by evidence type"
              className="flex-wrap"
            >
              {presentKinds.map((entryKind) => (
                <ToggleGroupItem
                  key={entryKind}
                  value={entryKind}
                  aria-label={`Filter ${kindLabel(entryKind)}`}
                  className="gap-1.5 px-2 text-caption"
                >
                  {kindIcon(entryKind, "size-3")}
                  {kindLabel(entryKind)}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          ) : null}
          {hasFailures ? (
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={outcome === "all" ? "" : outcome}
              onValueChange={(value) =>
                onChange({
                  outcome: (value || undefined) as Exclude<OutcomeFilter, "all"> | undefined,
                  entry: undefined,
                })
              }
              aria-label="Filter by outcome"
            >
              <ToggleGroupItem value="failed" aria-label="Show failures" className="px-2 text-caption">
                Failures
              </ToggleGroupItem>
              <ToggleGroupItem value="succeeded" aria-label="Show succeeded" className="px-2 text-caption">
                Succeeded
              </ToggleGroupItem>
            </ToggleGroup>
          ) : null}
          {agents.length > 1 ? (
            <Select
              value={agent}
              onValueChange={(value) => onChange({ agent: value === "all" ? undefined : value, entry: undefined })}
            >
              <SelectTrigger className="h-8 min-w-44 text-caption" aria-label="Filter by agent or branch">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All agents</SelectItem>
                {agents.map(([id, label]) => (
                  <SelectItem key={id} value={id}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
          {presentArtifacts.length > 0 ? (
            <Select
              value={artifact}
              onValueChange={(value) =>
                onChange({
                  artifact: value === "all" ? undefined : (value as TimelineArtifactKind),
                  entry: undefined,
                })
              }
            >
              <SelectTrigger className="h-8 min-w-36 text-caption" aria-label="Filter by artifact">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All artifacts</SelectItem>
                {presentArtifacts.map((artifactKind) => (
                  <SelectItem key={artifactKind} value={artifactKind}>
                    {artifactLabel(artifactKind)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
          {hasFilters ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                onChange({ kind: undefined, artifact: undefined, agent: undefined, outcome: undefined })
              }
              className="h-8 gap-1 px-2 text-caption"
            >
              <X size={14} /> Reset
            </Button>
          ) : null}
        </div>
      </div>

      {state.entry && !selected ? (
        <p className="m-0 text-caption text-warning" role="alert">
          The selected evidence entry is not present in this revision.
        </p>
      ) : null}

      {filtered.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border-soft p-8 text-center text-caption text-muted-foreground">
          No retained evidence matches these filters.
        </div>
      ) : roots.length === 1 && roots[0].children.length === 0 ? (
        // Single-branch session: the branch wrapper adds no hierarchy, so
        // render its turn groups directly.
        <Accordion
          type="multiple"
          value={turnValue}
          onValueChange={setExpandedTurns}
          className="grid gap-1.5"
        >
          {roots[0].turns.map((turn) => (
            <TurnSection
              key={turn.key}
              turn={turn}
              expandedEntryId={state.entry ?? null}
              onToggleEntry={toggleEntry}
              showAll={fullTurns.has(turn.key)}
              onShowAll={() => setFullTurns((current) => new Set(current).add(turn.key))}
            />
          ))}
        </Accordion>
      ) : (
        <Accordion
          type="multiple"
          value={branchValue}
          onValueChange={setExpandedBranches}
          className="grid gap-2"
        >
          {roots.map((branch) => (
            <BranchSection
              key={branch.sessionId}
              branch={branch}
              depth={0}
              turnValue={turnValue}
              onTurnValueChange={setExpandedTurns}
              expandedEntryId={state.entry ?? null}
              onToggleEntry={toggleEntry}
              fullTurns={fullTurns}
              onShowFullTurn={(key) => setFullTurns((current) => new Set(current).add(key))}
            />
          ))}
        </Accordion>
      )}
    </section>
  );
}

function BranchSection({
  branch,
  depth,
  turnValue,
  onTurnValueChange,
  expandedEntryId,
  onToggleEntry,
  fullTurns,
  onShowFullTurn,
}: {
  branch: BranchNode;
  depth: number;
  turnValue: string[];
  onTurnValueChange: (value: string[]) => void;
  expandedEntryId: string | null;
  onToggleEntry: (entry: SessionTimelineEntry) => void;
  fullTurns: ReadonlySet<string>;
  onShowFullTurn: (key: string) => void;
}) {
  return (
    <AccordionItem
      value={branch.sessionId}
      className={cn("border-b-0", depth > 0 && "tree-child mt-2")}
    >
      <AccordionTrigger className="items-center rounded-md px-2.5 py-2 text-caption font-normal hover:no-underline">
        <span className="flex min-w-0 flex-1 items-center gap-2 text-left">
          <span className="truncate font-medium text-body-sm text-foreground">{branch.label}</span>
          {branch.vendor ? (
            <Badge variant="secondary" className="shrink-0 px-1.5 py-0 text-caption">
              {branch.vendor}
            </Badge>
          ) : null}
          {depth > 0 ? (
            <span className="shrink-0 text-caption text-muted-foreground">child branch</span>
          ) : null}
        </span>
        {branch.failures > 0 ? (
          <span className="shrink-0 text-caption font-medium text-destructive">
            {branch.failures} failed
          </span>
        ) : null}
        <span className="shrink-0 text-caption text-muted-foreground">
          {branch.entries.length} entries · {branch.turns.length} turn{branch.turns.length === 1 ? "" : "s"}
        </span>
      </AccordionTrigger>
      <AccordionContent className="pb-0 pt-0">
        <div className="tree-children">
          <Accordion
            type="multiple"
            value={turnValue}
            onValueChange={onTurnValueChange}
            className="grid gap-1.5"
          >
            {branch.turns.map((turn) => (
              <TurnSection
                key={turn.key}
                turn={turn}
                expandedEntryId={expandedEntryId}
                onToggleEntry={onToggleEntry}
                showAll={fullTurns.has(turn.key)}
                onShowAll={() => onShowFullTurn(turn.key)}
              />
            ))}
          </Accordion>
          {branch.children.map((child) => (
            <BranchSection
              key={child.sessionId}
              branch={child}
              depth={depth + 1}
              turnValue={turnValue}
              onTurnValueChange={onTurnValueChange}
              expandedEntryId={expandedEntryId}
              onToggleEntry={onToggleEntry}
              fullTurns={fullTurns}
              onShowFullTurn={onShowFullTurn}
            />
          ))}
        </div>
      </AccordionContent>
    </AccordionItem>
  );
}

function TurnSection({
  turn,
  expandedEntryId,
  onToggleEntry,
  showAll,
  onShowAll,
}: {
  turn: TurnNode;
  expandedEntryId: string | null;
  onToggleEntry: (entry: SessionTimelineEntry) => void;
  showAll: boolean;
  onShowAll: () => void;
}) {
  const visible = showAll ? turn.entries : turn.entries.slice(0, TURN_PREVIEW_COUNT);
  const hiddenCount = turn.entries.length - visible.length;
  return (
    <AccordionItem value={turn.key} className="border-b-0">
      <AccordionTrigger className="items-center rounded-md px-2.5 py-1.5 text-caption font-normal hover:no-underline">
        <span className="min-w-0 flex-1 truncate text-left font-medium text-foreground">
          Turn {turn.sequence + 1}
        </span>
        {turn.failures > 0 ? (
          <span className="shrink-0 text-caption font-medium text-destructive">
            {turn.failures} failed
          </span>
        ) : null}
        <span className="shrink-0 text-caption text-muted-foreground">
          {turn.entries.length} entr{turn.entries.length === 1 ? "y" : "ies"}
        </span>
      </AccordionTrigger>
      <AccordionContent className="pb-0 pt-0">
        <ol className="tree-children relative m-0 grid list-none gap-1.5">
          {visible.map((entry) => (
            <TimelineRow
              key={entry.id}
              entry={entry}
              expanded={expandedEntryId === entry.id}
              onToggle={() => onToggleEntry(entry)}
            />
          ))}
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

function TimelineRow({
  entry,
  expanded,
  onToggle,
}: {
  entry: SessionTimelineEntry;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <li className="relative list-none">
      <span
        className={cn(
          "absolute top-2.5 left-[-1.28rem] z-10 grid size-3 place-items-center rounded-full border bg-background",
          entry.failed ? "border-destructive text-destructive" : "border-primary text-primary",
        )}
      >
        {kindIcon(entry.kind, "size-2")}
      </span>
      <div
        className={cn(
          "overflow-hidden rounded-lg border border-border-soft bg-surface-subtle transition-colors",
          entry.failed && "border-destructive/40",
          expanded && "border-primary/50 bg-card",
        )}
      >
        <button
          type="button"
          className="grid w-full min-w-0 gap-0.5 px-3 py-2 text-start focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-expanded={expanded}
          onClick={onToggle}
        >
          <span className="flex min-w-0 items-baseline gap-3">
            <span className="min-w-0 flex-1 truncate text-body-sm font-medium">{entry.label}</span>
            {entry.failed ? (
              <span className="shrink-0 text-caption font-medium text-destructive">failed</span>
            ) : null}
            <span
              className="mono shrink-0 text-caption text-muted-foreground"
              title={entry.timestamp ?? undefined}
            >
              {formatWhen(entry.timestamp)}
            </span>
          </span>
          <span className="flex min-w-0 items-center gap-1.5 text-caption text-muted-foreground">
            <span className="shrink-0">{kindLabel(entry.kind)}</span>
            {entry.artifact_kind ? (
              <>
                <span aria-hidden="true">·</span>
                <span className="shrink-0">{artifactLabel(entry.artifact_kind)}</span>
              </>
            ) : null}
            {entry.status && entry.status !== "completed" ? (
              <>
                <span aria-hidden="true">·</span>
                <span className="shrink-0">{entry.status}</span>
              </>
            ) : null}
          </span>
        </button>
        {expanded ? <RowDetail entry={entry} /> : null}
      </div>
    </li>
  );
}

function RowDetail({ entry }: { entry: SessionTimelineEntry }) {
  const accounting = entry.turn_accounting;
  const hasDetail = entry.item_ids.length > 0 || entry.event_ids.length > 0;
  return (
    <div className="grid gap-3 border-t border-border-soft px-3 py-3">
      <div className="flex flex-wrap items-center gap-2 text-caption text-muted-foreground">
        <SessionLink sessionId={entry.session_id}>{agentLabel(entry)}</SessionLink>
        <span>Turn {entry.turn_sequence + 1}</span>
        {entry.status ? <span>status {entry.status}</span> : null}
        {entry.target_session_id ? (
          <Link
            to="/sessions/$sessionId"
            params={{ sessionId: entry.target_session_id }}
            search={{ view: "timeline" }}
            className="font-medium text-primary hover:underline"
          >
            Open child {shortSessionId(entry.target_session_id)}
          </Link>
        ) : null}
      </div>
      {accounting ? (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-caption text-muted-foreground" aria-label="Turn accounting">
          <span>{formatTokens(accounting.processed_tokens)} tokens</span>
          <span>
            {accounting.cost_usd == null
              ? "Cost unavailable"
              : `${formatCostUsd(accounting.cost_usd)} ${accounting.cost_confidence ?? "unknown"}`}
          </span>
          {accounting.execution_seconds == null ? null : (
            <span>{formatDuration(accounting.execution_seconds)} elapsed</span>
          )}
          {accounting.model_active_seconds == null ? null : (
            <span>{formatDuration(accounting.model_active_seconds)} active</span>
          )}
          {accounting.wait_before_seconds == null ? null : (
            <span>{formatDuration(accounting.wait_before_seconds)} waiting</span>
          )}
          {accounting.model ? <span>{accounting.model}</span> : null}
        </div>
      ) : null}
      {entry.summary ? (
        <p className="m-0 whitespace-pre-wrap break-words text-body-sm">{entry.summary}</p>
      ) : null}
      {hasDetail ? (
        <EvidenceDetail entry={entry} />
      ) : (
        <p className="m-0 text-caption text-muted-foreground">
          No verified item or event references retained for this entry.
        </p>
      )}
    </div>
  );
}

function EvidenceDetail({ entry }: { entry: SessionTimelineEntry }) {
  const query = useQuery({
    queryKey: ["session-timeline-detail", entry.item_ids, entry.event_ids],
    queryFn: async () => {
      const [items, events] = await Promise.all([
        entry.item_ids.length ? fetchSessionItemDetails(entry.item_ids) : Promise.resolve([]),
        entry.event_ids.length
          ? fetchSessionEventDetails(entry.event_ids)
          : Promise.resolve({ root_session_id: null, matches: [] }),
      ]);
      return { items, events: events.matches };
    },
    staleTime: 5 * 60_000,
  });
  if (query.isPending) return <p className="m-0 text-caption text-muted-foreground">Verifying source ranges…</p>;
  if (query.isError) return <p className="m-0 text-caption text-destructive">{query.error.message}</p>;
  if (!query.data.items.length && !query.data.events.length) {
    return (
      <p className="m-0 text-caption text-muted-foreground">
        Source detail is unavailable for this retained entry.
      </p>
    );
  }
  return (
    <div className="grid gap-2 border-t border-border-soft pt-3">
      {[...query.data.items, ...query.data.events].map((detail, index) => (
        <pre
          key={String(("item_id" in detail ? detail.item_id : detail.event_id) ?? index)}
          className="m-0 max-h-80 overflow-auto rounded-lg bg-surface-emphasis p-3 text-caption whitespace-pre-wrap break-words"
        >
          {JSON.stringify(detail, null, 2)}
        </pre>
      ))}
    </div>
  );
}

/** Group filtered entries into branch -> turn nodes using projection parentage. */
function buildBranchTree(
  all: SessionTimelineEntry[],
  visible: SessionTimelineEntry[],
  branches: TimelineBranch[],
): BranchNode[] {
  const parentBySession = new Map<string, string>();
  for (const branch of branches) {
    if (branch.parent_session_id && branch.parent_session_id !== branch.session_id) {
      parentBySession.set(branch.session_id, branch.parent_session_id);
    }
  }
  // Fallback for payloads without branch metadata: infer parentage from
  // spawn edges (entry.target_session_id) on the full entry set.
  if (parentBySession.size === 0) {
    for (const entry of all) {
      if (entry.target_session_id && entry.target_session_id !== entry.session_id) {
        parentBySession.set(entry.target_session_id, entry.session_id);
      }
    }
  }

  const byBranch = new Map<string, SessionTimelineEntry[]>();
  for (const entry of visible) {
    const list = byBranch.get(entry.session_id);
    if (list) list.push(entry);
    else byBranch.set(entry.session_id, [entry]);
  }

  const nodes = new Map<string, BranchNode>();
  for (const [sessionId, branchEntries] of byBranch) {
    const sorted = [...branchEntries].sort((a, b) => a.position - b.position);
    const turnMap = new Map<string, TurnNode>();
    for (const entry of sorted) {
      const key = `${entry.session_id}:${entry.turn_id}`;
      let turn = turnMap.get(key);
      if (!turn) {
        turn = { key, sequence: entry.turn_sequence, entries: [], failures: 0 };
        turnMap.set(key, turn);
      }
      turn.entries.push(entry);
      if (entry.failed) turn.failures += 1;
    }
    const turns = [...turnMap.values()].sort((a, b) => a.sequence - b.sequence);
    const first = sorted[0];
    nodes.set(sessionId, {
      sessionId,
      label: first ? agentLabel(first) : sessionId,
      vendor: first?.vendor ?? null,
      entries: sorted,
      turns,
      children: [],
      failures: sorted.filter((entry) => entry.failed).length,
      firstPosition: first?.position ?? 0,
    });
  }

  const roots: BranchNode[] = [];
  for (const node of nodes.values()) {
    const parent = parentBySession.get(node.sessionId);
    const parentNode = parent ? nodes.get(parent) : undefined;
    if (parentNode) parentNode.children.push(node);
    else roots.push(node);
  }
  const byPosition = (a: BranchNode, b: BranchNode) => a.firstPosition - b.firstPosition;
  roots.sort(byPosition);
  for (const node of nodes.values()) node.children.sort(byPosition);
  return roots;
}

function collectBranchKeys(branch: BranchNode): string[] {
  return [branch.sessionId, ...branch.children.flatMap(collectBranchKeys)];
}

function collectTurnKeys(branch: BranchNode): string[] {
  return [...branch.turns.map((turn) => turn.key), ...branch.children.flatMap(collectTurnKeys)];
}

function flattenTurns(branch: BranchNode): TurnNode[] {
  return [...branch.turns, ...branch.children.flatMap(flattenTurns)];
}

/** Branch key path from a root to the branch that owns `sessionId`. */
function ancestorBranchKeys(roots: BranchNode[], sessionId: string): string[] {
  const path: string[] = [];
  const walk = (branch: BranchNode): boolean => {
    path.push(branch.sessionId);
    if (branch.sessionId === sessionId) return true;
    for (const child of branch.children) {
      if (walk(child)) return true;
    }
    path.pop();
    return false;
  };
  for (const root of roots) {
    if (walk(root)) return path;
  }
  return [];
}
