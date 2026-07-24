import type { MetricFormat } from "@/api";

export function formatMetricValue(value: number | null, format: MetricFormat, compact = false): string {
  if (value == null) return "Unavailable";
  if (format === "usd") return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: value < 0.01 ? 4 : 2 }).format(value);
  if (format === "percent") return `${value.toFixed(1)}%`;
  if (format === "ratio") return value.toFixed(3);
  if (format === "rate") return `${value.toFixed(1)} tok/s`;
  if (format === "duration") return formatDuration(value);
  if (format === "tokens" || compact) return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
  return Math.round(value).toLocaleString();
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

export function metricFormatForChart(category: "tokens" | "cost" | "execution", chart: string): MetricFormat {
  if (category === "cost") return "usd";
  if (category === "execution") return chart === "turns" ? "integer" : "duration";
  return chart === "cache-hit-rate" ? "percent" : "tokens";
}

export function seriesLabels(category: "tokens" | "cost" | "execution", chart: string): [string, string?, string?] {
  if (category === "tokens") {
    if (chart === "distribution") return ["Median", "P75", "P90"];
    if (chart === "cache-hit-rate") return ["Cache hit rate"];
    if (chart === "input-output") return ["Prompt", "Completion + reasoning"];
    return ["Processed", "Cached prompt", "Completion + reasoning"];
  }
  if (category === "cost") {
    if (chart === "distribution") return ["Median", "P75", "P90"];
    if (chart === "total") return ["Supported cost"];
    return ["Average", "Median"];
  }
  if (chart === "distribution") return ["Median", "P75", "P90"];
  if (chart === "active-wait") return ["Active", "Wait"];
  if (chart === "turns") return ["Turns", "Tool calls"];
  return ["Average", "Median"];
}
