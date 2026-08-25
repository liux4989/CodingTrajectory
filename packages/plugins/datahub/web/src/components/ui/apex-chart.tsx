import * as React from "react";
import type { ApexOptions, ApexAxisChartSeries, ApexNonAxisChartSeries } from "apexcharts";
import { cn } from "@/lib/utils";

// apexcharts is heavy (~960 KB min); lazy-load it so the vendor-charts chunk
// stays off the first paint of routes that render charts.
const ReactApexChart = React.lazy(() => import("react-apexcharts"));

/**
 * Resolved design tokens for ApexCharts. ApexCharts applies colors as SVG
 * presentation attributes, where `var(--token)` references do not resolve,
 * so every color is read from computed styles at render time and re-read
 * when the theme attribute on <html> changes.
 */
export type ApexTheme = {
  mode: "light" | "dark";
  palette: string[];
  grid: string;
  axis: string;
  foreground: string;
  card: string;
  monoFont: string;
  bodyFont: string;
};

const FALLBACK_PALETTE = ["#0d5c63", "#7a8c45", "#b9472b", "#6d28d9", "#c2410c", "#334155"];

const CHART_VARS = ["--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5", "--chart-6"];

function pickVar(style: CSSStyleDeclaration, name: string, fallback: string) {
  const value = style.getPropertyValue(name).trim();
  return value || fallback;
}

function readApexTheme(): ApexTheme {
  if (typeof window === "undefined") {
    return {
      mode: "light",
      palette: FALLBACK_PALETTE,
      grid: "#e0d4b8",
      axis: "#687267",
      foreground: "#18211c",
      card: "#fff9ea",
      monoFont: "monospace",
      bodyFont: "sans-serif",
    };
  }
  const style = getComputedStyle(document.documentElement);
  const palette = CHART_VARS.map((name, index) => pickVar(style, name, FALLBACK_PALETTE[index]));
  const scheme = style.colorScheme;
  return {
    mode: scheme === "dark" ? "dark" : "light",
    palette,
    grid: pickVar(style, "--border", "#e0d4b8"),
    axis: pickVar(style, "--muted-foreground", "#687267"),
    foreground: pickVar(style, "--foreground", "#18211c"),
    card: pickVar(style, "--card", "#fff9ea"),
    monoFont: pickVar(style, "--font-mono", "monospace"),
    bodyFont: pickVar(style, "--font-body", "sans-serif"),
  };
}

/** Resolve a `var(--token)` color string to its computed value. */
export function resolveCssColor(value: string, fallback: string) {
  if (typeof window === "undefined") return fallback;
  const match = /^var\((--[^)]+)\)$/.exec(value.trim());
  if (!match) return value;
  const resolved = getComputedStyle(document.documentElement).getPropertyValue(match[1]).trim();
  return resolved || fallback;
}

export function useApexTheme(): ApexTheme {
  const [theme, setTheme] = React.useState<ApexTheme>(readApexTheme);
  React.useEffect(() => {
    const update = () => setTheme(readApexTheme());
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "class", "style"],
    });
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", update);
    return () => {
      observer.disconnect();
      media.removeEventListener("change", update);
    };
  }, []);
  return theme;
}

export function baseApexOptions(theme: ApexTheme): ApexOptions {
  return {
    chart: {
      background: "transparent",
      toolbar: { show: false },
      fontFamily: theme.bodyFont,
      foreColor: theme.axis,
      animations: { easing: "easeout", speed: 400 },
      parentHeightOffset: 0,
    },
    colors: theme.palette,
    theme: { mode: theme.mode },
    grid: { borderColor: theme.grid, strokeDashArray: 3 },
    tooltip: {
      theme: theme.mode,
      style: { fontFamily: theme.monoFont },
    },
    legend: {
      labels: { colors: theme.foreground },
      fontFamily: theme.bodyFont,
      markers: { size: 5, shape: "square" },
    },
  };
}

type OptionRecord = Record<string, unknown>;

function isPlainObject(value: unknown): value is OptionRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function deepMerge(base: OptionRecord, override: OptionRecord): OptionRecord {
  const result: OptionRecord = { ...base };
  for (const [key, value] of Object.entries(override)) {
    const current = result[key];
    result[key] = isPlainObject(current) && isPlainObject(value) ? deepMerge(current, value) : value;
  }
  return result;
}

export function mergeApexOptions(base: ApexOptions, override: ApexOptions): ApexOptions {
  return deepMerge(base as OptionRecord, override as OptionRecord) as ApexOptions;
}

type ApexChartProps = {
  type:
    | "line"
    | "area"
    | "bar"
    | "donut"
    | "pie"
    | "scatter"
    | "bubble"
    | "heatmap"
    | "treemap"
    | "radialBar";
  series: ApexAxisChartSeries | ApexNonAxisChartSeries;
  options?: ApexOptions;
  height?: number | string;
  className?: string;
  ariaLabel?: string;
};

/**
 * Themed ApexCharts wrapper: merges per-chart options over the dashboard
 * base theme (palette, grid, tooltip, legend) and re-renders on theme
 * switches.
 */
export function ApexChart({ type, series, options, height = 280, className, ariaLabel }: ApexChartProps) {
  const theme = useApexTheme();
  const merged = React.useMemo(
    () => mergeApexOptions(baseApexOptions(theme), options ?? {}),
    [theme, options],
  );
  return (
    <div className={cn("w-full min-w-0", className)} role="img" aria-label={ariaLabel}>
      <React.Suspense
        fallback={
          <div
            className="w-full animate-pulse rounded-lg bg-surface-emphasis"
            style={{ height: typeof height === "number" ? height : undefined }}
          />
        }
      >
        <ReactApexChart type={type} series={series} options={merged} height={height} width="100%" />
      </React.Suspense>
    </div>
  );
}

/** Escape user-provided strings interpolated into custom tooltip HTML. */
export function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Inline-styled tooltip row builder for `tooltip.custom` callbacks. */
export function tooltipRow(label: string, value: string, axisColor: string) {
  return `<div style="display:flex;justify-content:space-between;gap:16px;line-height:1.5"><span style="color:${axisColor}">${label}</span><span style="font-weight:600">${value}</span></div>`;
}
