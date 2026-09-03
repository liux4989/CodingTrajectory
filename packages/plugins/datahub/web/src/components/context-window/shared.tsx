import * as React from "react";
import type { ContextCategory, ContextEvent, TokenEvidence } from "@/api";

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

export function eventColor(event: ContextEvent) {
  return categoryColors[event.category] ?? categoryColors.unattributed;
}

export function categoryTint(color: string, alpha: number) {
  return `color-mix(in srgb, ${color} ${alpha * 100}%, transparent)`;
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

export function evidenceLabel(evidence: TokenEvidence | null) {
  if (!evidence) return "No event-level token evidence";
  return `${evidence.value.toLocaleString()} tokens${isEstimatedConfidence(evidence.confidence) ? " (estimated)" : ""}`;
}

export type TurnGroup = {
  key: string;
  label: string;
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
      if (key === "before_first_prompt") label = "Before first prompt";
      else if (key === "post_turn") label = "After last turn";
      else label = `Turn ${++turnNumber}`;
      current = { key, label, totalTokens: 0, events: [] };
      groups.push(current);
    }
    current.events.push(event);
    if (event.tokens) current.totalTokens += event.tokens.value;
  }
  return groups;
}

/** Identity line under a row: target excerpt, category, terminal visibility. */
export function eventTarget(event: ContextEvent): string | null {
  const summary = event.summary?.split(",")[0]?.trim();
  if (!summary || summary === event.label) return null;
  return summary;
}
