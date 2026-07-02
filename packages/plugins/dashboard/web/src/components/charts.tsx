import * as React from "react"
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  XAxis,
  YAxis,
} from "recharts"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
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
import { cn } from "@/lib/utils"

const CHART_PALETTE = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
]

type MiniBarDatum = { label: string; value: number }

type MiniBarChartProps = {
  data: MiniBarDatum[]
  className?: string
  layout?: "horizontal" | "vertical"
  ariaLabel?: string
}

/**
 * Compact bar chart used inside metric cards and small summary surfaces.
 * Replaces the former hand-rolled div-bar sparkline with the shadcn chart
 * primitive (recharts). Supports a vertical layout (categories on the x axis)
 * for in-card sparklines and a horizontal layout (categories on the y axis)
 * for ranked single-series breakdowns.
 */
export function MiniBarChart({
  data,
  className,
  layout = "vertical",
  ariaLabel,
}: MiniBarChartProps) {
  if (!data.length) return null

  const config = {
    value: { label: "Value", color: "var(--chart-1)" },
  } satisfies ChartConfig

  const chartData = data.map((entry) => ({ label: entry.label, value: entry.value }))

  return (
    <ChartContainer
      config={config}
      className={cn("aspect-auto h-[3.25rem] w-full", className)}
      aria-label={ariaLabel}
    >
      {layout === "vertical" ? (
        <BarChart data={chartData} margin={{ top: 2, right: 0, bottom: 12, left: 0 }}>
          <Bar dataKey="value" fill="var(--color-value)" radius={4} maxBarSize={28}>
            <LabelList
              position="bottom"
              offset={2}
              className="fill-muted-foreground font-mono text-[0.6rem]"
              formatter={(value: unknown) => (Number(value) > 0 ? String(value) : "")}
            />
          </Bar>
        </BarChart>
      ) : (
        <BarChart
          data={chartData}
          layout="vertical"
          margin={{ top: 0, right: 8, bottom: 0, left: 8 }}
        >
          <XAxis type="number" hide />
          <YAxis
            type="category"
            dataKey="label"
            tickLine={false}
            axisLine={false}
            width={64}
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
          />
          <Bar dataKey="value" fill="var(--color-value)" radius={4} maxBarSize={18} />
        </BarChart>
      )}
    </ChartContainer>
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
 * Interactive stacked bar chart for model usage over time, following the
 * dashboard-01 chart-area-interactive pattern: a ToggleGroup grain selector
 * (with a Select fallback on narrow cards) drives the time bucketing, and the
 * chart stacks the top models per bucket. Replaces the former hand-rolled
 * progress-bar timeline.
 */
export function UsageTimelineChart({ buckets, view }: UsageTimelineChartProps) {
  const [grain, setGrain] = React.useState<GrainValue>("daily")

  const { chartData, series, config } = React.useMemo(() => {
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

    // Model keys can contain `/`, `.`, spaces — invalid as CSS custom property
    // names. Map each displayed model to a safe `series-<i>` data key while
    // keeping the real model name as the chart legend label.
    const keyForModel = new Map<string, string>()
    const registerModel = (model: string, index: number) => {
      const safeKey = `series-${index}`
      keyForModel.set(model, safeKey)
      return safeKey
    }
    top.forEach((model, index) => registerModel(model, index))
    const otherKey = hasOther ? registerModel("Other", top.length) : null

    const byBucket = new Map<string, { bucket: string; values: Record<string, number> }>()
    for (const row of rows) {
      const value = bucketValue(row, view)
      const entry = byBucket.get(row.bucket) ?? { bucket: row.bucket, values: {} }
      const safeKey = keyForModel.get(row.model_key) ?? otherKey
      if (safeKey) {
        entry.values[safeKey] = (entry.values[safeKey] ?? 0) + value
      }
      byBucket.set(row.bucket, entry)
    }

    const orderedBuckets = [...byBucket.keys()].sort()
    const data = orderedBuckets.map((bucket) => ({
      bucket,
      ...byBucket.get(bucket)!.values,
    }))

    const modelOrder: Array<{ key: string; label: string }> = [
      ...top.map((model) => ({ key: keyForModel.get(model)!, label: model })),
      ...(otherKey ? [{ key: otherKey, label: "Other" }] : []),
    ]
    const builtConfig: ChartConfig = {}
    modelOrder.forEach((entry, index) => {
      builtConfig[entry.key] = {
        label: entry.label,
        color: CHART_PALETTE[index % CHART_PALETTE.length],
      }
    })
    return { chartData: data, series: modelOrder, config: builtConfig }
  }, [buckets, grain, view])

  const hasData = chartData.length > 0

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
          <ChartContainer config={config} className="aspect-auto h-[260px] w-full">
            <BarChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
              <CartesianGrid vertical={false} />
              <XAxis
                dataKey="bucket"
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                minTickGap={24}
                tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              />
              <ChartTooltip
                cursor={false}
                content={
                  <ChartTooltipContent
                    indicator="dot"
                    formatter={(value) =>
                      view === "tokens"
                        ? compactNumber(Number(value))
                        : formatCost(Number(value))
                    }
                  />
                }
              />
              {series.map((entry) => (
                <Bar
                  key={entry.key}
                  dataKey={entry.key}
                  stackId="a"
                  fill={`var(--color-${entry.key})`}
                />
              ))}
            </BarChart>
          </ChartContainer>
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

function compactNumber(value: number) {
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value)
}

function formatCost(value: number) {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: value < 0.01 && value > 0 ? 4 : 2,
  }).format(value)
}
