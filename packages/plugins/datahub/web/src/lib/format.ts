// Shared display formatters. Keep one implementation per unit so routes do
// not drift on how numbers, tokens, costs, and percentages render.

const compactFormatter = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});
const numberFormatter = new Intl.NumberFormat();
const shareFormatter = new Intl.NumberFormat(undefined, {
  style: "percent",
  maximumFractionDigits: 1,
});

export function formatCompactNumber(value: number) {
  return compactFormatter.format(value);
}

export function formatCount(value: number | null | undefined) {
  return numberFormatter.format(value ?? 0);
}

export function formatTokens(value: number | null | undefined) {
  return compactFormatter.format(value ?? 0);
}

export function formatExactTokens(value: number | null | undefined) {
  return `${numberFormatter.format(Math.round(value ?? 0))} tokens`;
}

export function formatCostUsd(value: number | null | undefined) {
  if (value == null) return "-";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: value < 0.01 && value > 0 ? 4 : 2,
  }).format(value);
}

/** Percent for values already on a 0–100 scale. */
export function formatPercent(value: number | null | undefined) {
  if (value == null) return "-";
  return `${value.toFixed(1)}%`;
}

/** Percent for ratios on a 0–1 scale. */
export function formatShare(value: number | null | undefined) {
  return shareFormatter.format(value ?? 0);
}

export function formatDelta(value: number | null | undefined) {
  if (value == null) return "No baseline";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

export function formatDuration(value: number) {
  const seconds = Math.max(0, Math.round(value));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${remainingSeconds}s`;
  return `${remainingSeconds}s`;
}

export function formatLabel(value: string | null | undefined) {
  if (!value) return "Unclassified";
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function shortId(value: string | null | undefined) {
  if (!value) return "—";
  return value.length > 12 ? value.slice(0, 12) : value;
}
