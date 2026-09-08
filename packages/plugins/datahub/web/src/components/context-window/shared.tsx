import type * as React from "react";
import type { ContextCategory, ContextEvent } from "@/api";

export const categoryColors: Record<string, string> = {
  starting_context: "var(--color-category-starting-context)",
  user_input: "var(--color-category-user-input)",
  files: "var(--color-category-files)",
  output: "var(--color-category-output)",
  agent: "var(--color-category-agent)",
  unattributed: "var(--color-category-unattributed)",
};

const CATEGORY_ORDER = ["starting_context", "user_input", "files", "output", "agent", "unattributed"];

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

