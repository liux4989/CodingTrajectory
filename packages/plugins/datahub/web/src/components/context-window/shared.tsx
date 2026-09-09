import type * as React from "react";
import type {
  CacheBreakRecord,
  CompactionEventRecord,
  ContextCategory,
  ContextEvent,
  ContextWindowPayload,
} from "@/api";

export const categoryColors: Record<string, string> = {
  starting_context: "var(--color-category-starting-context)",
  user_input: "var(--color-category-user-input)",
  files: "var(--color-category-files)",
  output: "var(--color-category-output)",
  agent: "var(--color-category-agent)",
  compacted_history: "var(--color-category-compacted-history)",
  unattributed: "var(--color-category-unattributed)",
};

const CATEGORY_ORDER = ["starting_context", "user_input", "files", "output", "agent", "compacted_history", "unattributed"];

export function aggregateCategories(categories: ContextCategory[]) {
  const totals = new Map<string, number>();
  for (const category of categories) {
    totals.set(category.category, (totals.get(category.category) ?? 0) + category.tokens.value);
  }
  return CATEGORY_ORDER
    .filter((key) => totals.has(key))
    .map((key) => ({ category: key, tokens: totals.get(key) ?? 0 }));
}

export function categoryDotStyle(category: string): React.CSSProperties {
  return { background: categoryColors[category] ?? categoryColors.unattributed };
}

export function categoryLabel(category: string) {
  if (category === "starting_context") return "Starting context";
  if (category === "user_input") return "User input";
  if (category === "files") return "Files";
  if (category === "output") return "Output";
  if (category === "agent") return "Agent";
  if (category === "compacted_history") return "Compacted history";
  return category.replaceAll("_", " ");
}

export function isEstimatedConfidence(confidence: string | null | undefined) {
  return confidence === "estimated_tokens" || confidence === "structural" || confidence === "unknown";
}

export type TurnGroup = {
  key: string;
  label: string;
  /** Canonical turn id for turn groups; null for before/after-turn buckets. */
  turnId: string | null;
  totalTokens: number;
  events: ContextEvent[];
};

function groupKey(event: ContextEvent): string {
  if (event.group === "before_first_prompt") return "before_first_prompt";
  if (event.group === "post_turn") return "post_turn";
  return `turn:${event.turn_id ?? "none"}`;
}

/** Group events into turn buckets with natural-language labels. */
export function buildTurnGroups(events: ContextEvent[]): TurnGroup[] {
  const groups: TurnGroup[] = [];
  let turnNumber = 0;
  for (const event of events) {
    const key = groupKey(event);
    let current = groups[groups.length - 1];
    if (!current || current.key !== key) {
      let label: string;
      let turnId: string | null = null;
      if (key === "before_first_prompt") label = "Before first prompt";
      else if (key === "post_turn") label = "After last turn";
      else {
        label = `Turn ${++turnNumber}`;
        turnId = event.turn_id ?? null;
      }
      current = { key, label, turnId, totalTokens: 0, events: [] };
      groups.push(current);
    }
    current.events.push(event);
    if (event.tokens) current.totalTokens += event.tokens.value;
  }
  return groups;
}

/** Token delta per category within one turn group, in stable category order. */
export function orderedCategoryDeltas(events: ContextEvent[]) {
  const totals = new Map<string, number>();
  for (const event of events) {
    if (!event.tokens) continue;
    totals.set(event.category, (totals.get(event.category) ?? 0) + event.tokens.value);
  }
  return CATEGORY_ORDER.filter((key) => totals.has(key)).map((key) => ({
    category: key,
    tokens: totals.get(key) ?? 0,
  }));
}

export type FootprintTurnRow = {
  kind: "turn";
  key: string;
  label: string;
  turnId: string | null;
  durationSeconds: number | null;
  deltas: { category: string; tokens: number }[];
  deltaTokens: number;
  /** Window fill carried into this turn (after any preceding compaction). */
  carriedBefore: number;
  /** Running window fill after this turn's delta. */
  fillAfter: number;
  /** The carried base contains compacted history (a compaction happened earlier). */
  postCompaction: boolean;
};

export type FootprintIdleRow = {
  kind: "idle";
  key: string;
  idleSeconds: number | null;
  cacheBreak: CacheBreakRecord | null;
};

export type FootprintCompactionRow = {
  kind: "compaction";
  key: string;
  preTokens: number | null;
  postTokens: number | null;
  droppedTokens: number | null;
  trigger: string | null;
};

export type FootprintRow = FootprintTurnRow | FootprintIdleRow | FootprintCompactionRow;

const IDLE_ROW_MIN_SECONDS = 60;

/**
 * Waterfall row model for the turn footprint: turn rows interleaved with idle
 * hairlines and compaction drops. Each turn carries the running fill left by
 * the previous one; a compaction resets the running total to its measured
 * ``post_tokens`` anchor (or subtracts ``dropped_tokens`` as a fallback).
 */
export function buildFootprintRows(payload: ContextWindowPayload): FootprintRow[] {
  const groups = buildTurnGroups(payload.events);
  const spans = new Map((payload.turn_spans ?? []).map((span) => [span.turn_id, span]));
  const breaks = new Map(
    (payload.cache_breaks?.events ?? []).map((event) => [event.turn_id, event]),
  );
  const compactionByTurn = new Map<string, CompactionEventRecord[]>();
  const trailingCompactions: CompactionEventRecord[] = [];
  for (const event of payload.compaction?.events ?? []) {
    if (event.before_turn_id) {
      const list = compactionByTurn.get(event.before_turn_id) ?? [];
      list.push(event);
      compactionByTurn.set(event.before_turn_id, list);
    } else {
      trailingCompactions.push(event);
    }
  }

  const rows: FootprintRow[] = [];
  let running = 0;
  let postCompaction = false;
  const pushCompactions = (events: CompactionEventRecord[], keyPrefix: string) => {
    for (const event of events) {
      rows.push({
        kind: "compaction",
        key: `${keyPrefix}:${event.timestamp}`,
        preTokens: event.pre_tokens,
        postTokens: event.post_tokens,
        droppedTokens: event.dropped_tokens,
        trigger: event.trigger ?? null,
      });
      running =
        event.post_tokens ?? Math.max(running - (event.dropped_tokens ?? 0), 0);
      postCompaction = true;
    }
  };

  for (const group of groups) {
    const turnId = group.turnId;
    const span = turnId ? spans.get(turnId) : undefined;
    if (turnId) {
      const cacheBreak = breaks.get(turnId) ?? null;
      const idle = span?.idle_before_seconds ?? cacheBreak?.idle_seconds ?? null;
      if (cacheBreak ?? (idle != null && idle >= IDLE_ROW_MIN_SECONDS)) {
        rows.push({ kind: "idle", key: `idle:${turnId}`, idleSeconds: idle, cacheBreak });
      }
      pushCompactions(compactionByTurn.get(turnId) ?? [], `compaction:${turnId}`);
    }
    rows.push({
      kind: "turn",
      key: group.key,
      label: group.label,
      turnId,
      durationSeconds: span?.duration_seconds ?? null,
      deltas: orderedCategoryDeltas(group.events),
      deltaTokens: group.totalTokens,
      carriedBefore: running,
      fillAfter: running + group.totalTokens,
      postCompaction,
    });
    running += group.totalTokens;
  }
  pushCompactions(trailingCompactions, "compaction:end");
  return rows;
}

export function formatDuration(seconds: number | null) {
  if (seconds == null) return null;
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export function cacheBreakLabel(record: CacheBreakRecord) {
  switch (record.type) {
    case "ttl_confirmed":
      return "cache TTL expired";
    case "ttl_likely":
      return "cache TTL likely expired";
    case "effort_switch":
      return `effort changed${record.effort_to ? ` → ${record.effort_to}` : ""}`;
    case "model_switch":
      return "model changed";
    case "intra_turn_drop":
      return "mid-turn cache drop";
    default:
      return "cache re-read";
  }
}

