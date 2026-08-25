import * as React from "react";
import { Hourglass, Zap } from "lucide-react";

// Shared cache-break formatting + tone helpers for the per-session
// context-window view.

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

export type CacheBreakType = "ttl_confirmed" | "ttl_likely" | "effort_switch" | "unattributed";

// Effort changes are actionable (amber); TTL breaks are age evictions
// (neutral). ``ttl_likely`` remains explicitly tentative.
export type CacheBreakTone = {
  icon: React.ReactNode;
  label: string;
  className: string;
};

export function cacheBreakTone(type: CacheBreakType, effortFrom: string | null, effortTo: string | null): CacheBreakTone {
  if (type === "effort_switch") {
    return {
      icon: <Zap size={12} />,
      label: `effort change${effortFrom ? ` ${effortFrom}->${effortTo}` : effortTo ? `->${effortTo}` : ""}`,
      className: "border-warning/45 bg-warning/10 text-warning",
    };
  }
  if (type === "unattributed") {
    return {
      icon: <Hourglass size={12} />,
      label: "Unattributed",
      className: "border-border-soft bg-surface-emphasis text-muted-foreground",
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
  "unattributed",
];
