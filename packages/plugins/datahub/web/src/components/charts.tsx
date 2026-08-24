import * as React from "react"
import type { ApexOptions } from "apexcharts"
import { formatCompactNumber, formatCostUsd } from "@/lib/format"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  ApexChart,
  resolveCssColor,
  useApexTheme,
} from "@/components/ui/apex-chart"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  ToggleGroup,
  ToggleGroupItem,
} from "@/components/ui/toggle-group"

type MiniBarDatum = { label: string; value: number }

type MiniBarChartProps = {
  data: MiniBarDatum[]
  className?: string
  layout?: "horizontal" | "vertical"
  ariaLabel?: string
}

/**
 * Compact bar chart used inside metric cards and small summary surfaces.
 * Supports a vertical layout (columns, categories on the x axis) for in-card
 * sparklines and a horizontal layout (categories on the y axis) for ranked
 * single-series breakdowns.
 */
export function MiniBarChart({
  data,
  className,
  layout = "vertical",
  ariaLabel,
}: MiniBarChartProps) {
  const theme = useApexTheme()
  if (!data.length) return null

  const horizontal = layout === "horizontal"
  const options: ApexOptions = {
    plotOptions: {
      bar: {
        horizontal,
        borderRadius: 4,
        columnWidth: "62%",
        barHeight: "72%",
      },
    },
    dataLabels: {
      enabled: true,
      formatter: (value) => (Number(value) > 0 ? String(value) : ""),
      style: { fontSize: "10px", fontFamily: theme.monoFont, colors: horizontal ? [theme.card] : [theme.axis] },
      offsetY: horizontal ? 0 : 18,
    },
    xaxis: {
      categories: data.map((entry) => entry.label),
      labels: {
        show: !horizontal,
        style: { fontSize: "10px", colors: theme.axis, fontFamily: theme.monoFont },
      },
      axisBorder: { show: false },
      axisTicks: { show: false },
    },
    yaxis: {
      labels: {
        show: horizontal,
        style: { fontSize: "11px", colors: theme.axis },
      },
    },
    grid: { show: false, padding: horizontal ? { left: 8, right: 8 } : { top: -14, bottom: 0 } },
    tooltip: { y: { formatter: (value) => Number(value).toLocaleString() } },
  }

  return (
    <ApexChart
      type="bar"
      series={[{ name: "Value", data: data.map((entry) => entry.value) }]}
      options={options}
      height={horizontal ? 160 : 52}
      className={className}
      ariaLabel={ariaLabel}
    />
  )
}

type UsageTimelineChartProps = {
  buckets: UsageTimelineChartBuckets
  view: "cost" | "tokens"
}

export type UsageTimelineChartBuckets = Record<
  string,
  Array<{
    bucket: string
    model_key: string
    turns: number
    estimated_cost_usd: number
    usage: { processed_tokens?: number }
  }>
>

const GRAIN_OPTIONS = [
  { value: "five_hour", label: "5h" },
  { value: "daily", label: "Day" },
  { value: "weekly", label: "Week" },
  { value: "monthly", label: "Month" },
] as const

type GrainValue = (typeof GRAIN_OPTIONS)[number]["value"]

const TOP_SERIES_LIMIT = 6

/**
 * Interactive stacked bar chart for model usage over time: a ToggleGroup
 * grain selector (with a Select fallback on narrow cards) drives the time
 * bucketing, and the chart stacks the top models per bucket.
 */
export function UsageTimelineChart({ buckets, view }: UsageTimelineChartProps) {
  const theme = useApexTheme()
  const [grain, setGrain] = React.useState<GrainValue>("daily")

  const { categories, series } = React.useMemo(() => {
    const rows = buckets[grain] ?? []
    const totals = new Map<string, number>()
    for (const row of rows) {
      const value = bucketValue(row, view)
      totals.set(row.model_key, (totals.get(row.model_key) ?? 0) + value)
    }
    const ranked = [...totals.entries()]
      .sort((left, right) => right[1] - left[1])
      .map(([key]) => key)
    const top = ranked.slice(0, TOP_SERIES_LIMIT)
    const hasOther = ranked.length > TOP_SERIES_LIMIT

    const byBucket = new Map<string, Map<string, number>>()
    for (const row of rows) {
      const value = bucketValue(row, view)
      const model = top.includes(row.model_key) ? row.model_key : "Other"
      const entry = byBucket.get(row.bucket) ?? new Map<string, number>()
      entry.set(model, (entry.get(model) ?? 0) + value)
      byBucket.set(row.bucket, entry)
    }

    const orderedBuckets = [...byBucket.keys()].sort()
    const modelOrder = [...top, ...(hasOther ? ["Other"] : [])]
    return {
      categories: orderedBuckets,
      series: modelOrder.map((model) => ({
        name: model,
        data: orderedBuckets.map((bucket) => roundValue(byBucket.get(bucket)?.get(model) ?? 0, view)),
      })),
    }
  }, [buckets, grain, view])

  const options = React.useMemo<ApexOptions>(
    () => ({
      chart: { stacked: true, stackType: "normal" },
      plotOptions: { bar: { columnWidth: "58%", borderRadius: 3, borderRadiusApplication: "end" } },
      xaxis: {
        categories,
        tickPlacement: "on",
        labels: { style: { fontSize: "11px" }, rotate: -30, hideOverlappingLabels: true },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: {
        labels: {
          formatter: (value) => (view === "tokens" ? formatCompactNumber(value) : formatCostUsd(value)),
        },
      },
      legend: { show: true, position: "bottom", horizontalAlign: "left" },
      dataLabels: { enabled: false },
      tooltip: {
        shared: true,
        intersect: false,
        y: {
          formatter: (value) =>
            value == null ? "0" : view === "tokens" ? formatCompactNumber(Number(value)) : formatCostUsd(Number(value)),
        },
      },
    }),
    [categories, view],
  )

  const hasData = categories.length > 0

  return (
    <Card className="@container/card min-w-0">
      <CardHeader>
        <CardTitle className="title-card">
          {view === "tokens" ? "Tokens Over Time" : "Cost Over Time"}
        </CardTitle>
        <CardDescription>
          Stacked by model for the selected time grain.
        </CardDescription>
        <CardAction>
          <ToggleGroup
            type="single"
            value={grain}
            onValueChange={(value) => {
              if (value) setGrain(value as GrainValue)
            }}
            variant="outline"
            className="hidden *:data-[slot=toggle-group-item]:px-3! @[480px]/card:flex"
          >
            {GRAIN_OPTIONS.map((option) => (
              <ToggleGroupItem key={option.value} value={option.value}>
                {option.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <Select value={grain} onValueChange={(value) => setGrain(value as GrainValue)}>
            <SelectTrigger
              className="flex w-28 **:data-[slot=select-value]:block **:data-[slot=select-value]:truncate @[480px]/card:hidden"
              size="sm"
              aria-label="Select a time grain"
            >
              <SelectValue placeholder="Per day" />
            </SelectTrigger>
            <SelectContent className="rounded-xl">
              {GRAIN_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value} className="rounded-lg">
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </CardAction>
      </CardHeader>
      <CardContent className="pt-2">
        {hasData ? (
          <ApexChart
            type="bar"
            series={series}
            options={options}
            height={280}
            ariaLabel={`${view === "tokens" ? "Tokens" : "Cost"} over time, stacked by model`}
          />
        ) : (
          <p className="py-12 text-center text-muted-foreground">
            No turn timestamps were available in this scope.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function bucketValue(
  row: UsageTimelineChartBuckets[string][number],
  view: "cost" | "tokens",
) {
  return view === "tokens" ? row.usage.processed_tokens ?? 0 : row.estimated_cost_usd
}

function roundValue(value: number, view: "cost" | "tokens") {
  return view === "cost" ? Math.round(value * 10000) / 10000 : Math.round(value)
}

// ---- DonutChart ----

type DonutDatum = { label: string; value: number; color?: string }

type DonutChartProps = {
  data: DonutDatum[]
  className?: string
  ariaLabel?: string
  /** Center value (e.g. total count). */
  centerLabel?: string
  /** Center sub-label (e.g. "total"). */
  centerSubLabel?: string
  /** Tooltip value formatter; defaults to compact count + percentage. */
  formatValue?: (value: number) => string
  height?: number
}

/**
 * Donut chart for proportional breakdowns with a native center total label.
 * Slices take the chart palette unless a per-datum color is provided.
 */
export function DonutChart({ data, className, ariaLabel, centerLabel, centerSubLabel, formatValue, height = 220 }: DonutChartProps) {
  const theme = useApexTheme()
  const total = data.reduce((sum, item) => sum + item.value, 0) || 1

  const options = React.useMemo<ApexOptions>(
    () => ({
      labels: data.map((item) => item.label),
      colors: data.map((item, index) =>
        item.color ? resolveCssColor(item.color, theme.palette[index % theme.palette.length]) : theme.palette[index % theme.palette.length],
      ),
      stroke: { width: 2, colors: [theme.card] },
      dataLabels: { enabled: false },
      legend: { show: true, position: "bottom", fontSize: "12px" },
      plotOptions: {
        pie: {
          donut: {
            size: "66%",
            labels: {
              show: Boolean(centerLabel),
              value: {
                show: true,
                fontSize: "1.5rem",
                fontFamily: theme.bodyFont,
                fontWeight: 800,
                color: theme.foreground,
                formatter: () => centerLabel ?? "",
              },
              name: { show: true, color: theme.axis, fontSize: "0.7rem" },
              total: {
                show: true,
                showAlways: true,
                label: centerSubLabel ?? "Total",
                fontSize: "0.7rem",
                color: theme.axis,
                formatter: () => centerLabel ?? "",
              },
            },
          },
        },
      },
      tooltip: {
        y: {
          formatter: (value) => {
            const formatted = formatValue ? formatValue(value) : Number(value).toLocaleString()
            return `${formatted} (${Math.round((Number(value) / total) * 100)}%)`
          },
        },
      },
    }),
    [data, theme, centerLabel, centerSubLabel, formatValue, total],
  )

  return (
    <ApexChart
      type="donut"
      series={data.map((item) => item.value)}
      options={options}
      height={height}
      className={className}
      ariaLabel={ariaLabel}
    />
  )
}

// ---- Sparkline ----

type SparklineProps = {
  data: Array<{ label: string; value: number }>
  className?: string
  ariaLabel?: string
  color?: string
  variant?: "line" | "area"
  height?: number
}

/**
 * Ultra-compact sparkline for embedding in metric card footers. No axes, no
 * grid, no legend - just the shape. Supports line and area variants.
 */
export function Sparkline({ data, className, ariaLabel, color = "var(--chart-1)", variant = "area", height = 32 }: SparklineProps) {
  const theme = useApexTheme()
  if (!data.length) return null

  const resolved = resolveCssColor(color, theme.palette[0])
  const options: ApexOptions = {
    chart: { sparkline: { enabled: true } },
    colors: [resolved],
    stroke: { curve: "smooth", width: 1.5 },
    fill:
      variant === "area"
        ? { type: "gradient", gradient: { shadeIntensity: 0, opacityFrom: 0.3, opacityTo: 0, stops: [0, 100] } }
        : { type: "solid", opacity: 0 },
    tooltip: { enabled: false },
  }

  return (
    <ApexChart
      type={variant}
      series={[{ name: "Value", data: data.map((entry) => entry.value) }]}
      options={options}
      height={height}
      className={className}
      ariaLabel={ariaLabel}
    />
  )
}
