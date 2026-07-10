import * as React from "react";
import { Hourglass, Zap } from "lucide-react";

// Shared cache-break formatting + tone helpers. Used by both the per-session
// context-window view and the aggregate cache-breaks page so the two stay
// visually consistent.

export function formatTokens(value: number | null | undefined) {
  if (value == null) return "-";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

export function formatCostUsd(value: number | null | undefined) {
  if (value == null) return "-";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

export function formatIdleSeconds(seconds: number | null | undefined) {
  if (seconds == null) return "-";
  if (seconds >= 60) return `${(seconds / 60).toFixed(1)}m`;
  return `${Math.round(seconds)}s`;
}

export type CacheBreakType = "ttl_confirmed" | "ttl_likely" | "effort_switch";

// effort_switch is the avoidable, actionable cause (amber); TTL breaks are an
// unavoidable age eviction (neutral). ttl_likely softens with a "?".
export type CacheBreakTone = {
  icon: React.ReactNode;
  label: string;
  className: string;
};

export function cacheBreakTone(type: CacheBreakType, effortFrom: string | null, effortTo: string | null): CacheBreakTone {
  if (type === "effort_switch") {
    const confirmed = Boolean(effortTo);
    return {
      icon: <Zap size={12} />,
      label: confirmed
        ? `effort switch${effortFrom ? ` ${effortFrom}->${effortTo}` : `->${effortTo}`}`
        : "effort switch?",
      className: "border-warning/45 bg-warning/10 text-warning",
    };
  }
  const confirmed = type === "ttl_confirmed";
  return {
    icon: <Hourglass size={12} />,
    label: confirmed ? "TTL break" : "TTL break?",
    className: "border-border-soft bg-surface-emphasis text-muted-foreground",
  };
}

export const CACHE_BREAK_TYPE_ORDER: CacheBreakType[] = [
  "effort_switch",
  "ttl_confirmed",
  "ttl_likely",
];
