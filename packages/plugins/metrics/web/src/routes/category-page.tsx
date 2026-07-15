import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { AlertCircle, ChartNoAxesCombined } from "lucide-react";

import { fetchCategory } from "@/api";
import { ComparisonChart } from "@/components/comparison-chart";
import { MetricCard } from "@/components/metric-card";
import { ComparisonTable, SessionTable } from "@/components/metrics-tables";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export type CategoryConfig = {
  category: "tokens" | "cost" | "execution";
  title: string;
  description: string;
  modes: ReadonlyArray<{ value: string; label: string }>;
  explanation: string;
  caveat: string;
  endpoint: string;
};

type CategoryPageProps = {
  config: CategoryConfig;
  chart: string;
  sinceDays: number;
  onChartChange: (chart: string) => void;
  onSinceDaysChange: (sinceDays: number) => void;
};

export function CategoryPage({ config, chart, sinceDays, onChartChange, onSinceDaysChange }: CategoryPageProps) {
  const selectedMode = config.modes.find((mode) => mode.value === chart) ?? config.modes[0];
  const query = useQuery({
    queryKey: ["metrics", config.category, sinceDays, selectedMode.value],
    queryFn: () => fetchCategory(config.endpoint, sinceDays, selectedMode.value),
    placeholderData: keepPreviousData,
  });
  const data = query.data;
  const displayedMode = config.modes.find((mode) => mode.value === data?.chart) ?? selectedMode;
  return (
    <div className="grid min-w-0 gap-6">
      <section className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-3xl">
          <p className="m-0 font-display text-xs font-bold uppercase tracking-[0.14em] text-primary">Metrics / {config.title}</p>
          <h1 className="m-0 mt-2 text-balance font-display text-[clamp(2rem,5vw,4.5rem)] font-bold leading-[0.95] tracking-[-0.04em]">{config.title}</h1>
          <p className="m-0 mt-4 max-w-2xl text-pretty text-base leading-relaxed text-muted-foreground">{config.description}</p>
        </div>
        <ToggleGroup
          type="single"
          value={String(sinceDays)}
          onValueChange={(value) => value && onSinceDaysChange(Number(value))}
          variant="outline"
          aria-label="Cohort date range"
        >
          <ToggleGroupItem value="7" aria-label="Last 7 days">7d</ToggleGroupItem>
          <ToggleGroupItem value="30" aria-label="Last 30 days">30d</ToggleGroupItem>
          <ToggleGroupItem value="90" aria-label="Last 90 days">90d</ToggleGroupItem>
        </ToggleGroup>
      </section>

      <Card className="gap-4 bg-card/80">
        <CardHeader>
          <CardTitle>Cohort</CardTitle>
          <CardDescription>One explicit scope will apply to every metric on this page.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          <Badge variant="secondary">Last {data?.cohort.since_days ?? sinceDays} days</Badge>
          {data ? <><Badge variant="outline">{data.cohort.session_graph_count.toLocaleString()} graphs</Badge><Badge variant="outline">{data.cohort.turn_count.toLocaleString()} turns</Badge><Badge variant="outline">Usage {data.cohort.usage_eligible}/{data.cohort.session_graph_count}</Badge><Badge variant="outline">Pricing {data.cohort.pricing_eligible}/{data.cohort.session_graph_count}</Badge></> : <Skeleton className="h-6 w-72" />}
          {query.isPlaceholderData ? <Badge variant="outline">Loading {sinceDays}d cohort</Badge> : query.isFetching && data ? <Badge variant="outline">Refreshing</Badge> : null}
        </CardContent>
      </Card>

      <section className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,14rem),1fr))] gap-4" aria-label={`${config.title} highlights`}>
        {data ? data.highlights.map((highlight) => <MetricCard key={highlight.key} highlight={highlight} />) : Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="min-h-36 rounded-xl border border-border" />)}
      </section>

      {query.error ? (
        <Alert variant="destructive">
          <AlertCircle aria-hidden="true" />
          <AlertTitle>Unable to load the cohort</AlertTitle>
          <AlertDescription><p>{query.error instanceof Error ? query.error.message : String(query.error)}</p><Button variant="outline" size="sm" onClick={() => void query.refetch()}>Retry</Button></AlertDescription>
        </Alert>
      ) : null}

      {data?.warnings.length ? (
        <Alert>
          <AlertCircle aria-hidden="true" />
          <AlertTitle>Partial source warnings</AlertTitle>
          <AlertDescription>{data.warnings.slice(0, 3).map((warning) => <p key={warning}>{warning}</p>)}</AlertDescription>
        </Alert>
      ) : null}

      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="m-0 font-display text-xl font-semibold">Comparison view</h2>
            <p className="m-0 mt-1 text-sm text-muted-foreground">Changing the mode preserves the cohort.</p>
          </div>
          <ToggleGroup
            type="single"
            value={selectedMode.value}
            onValueChange={(value) => value && onChartChange(value)}
            variant="outline"
            className="max-w-full flex-wrap"
            aria-label={`${config.title} chart mode`}
          >
            {config.modes.map((mode) => <ToggleGroupItem key={mode.value} value={mode.value}>{mode.label}</ToggleGroupItem>)}
          </ToggleGroup>
        </div>

        <Card className="min-h-[24rem] overflow-clip">
          <CardHeader>
            <CardTitle>{displayedMode.label}</CardTitle>
            <CardDescription>Primary chart and accessible comparison table share this view.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-1">
            {data?.chart_points.length ? <ComparisonChart category={data.category} chart={data.chart} points={data.chart_points} /> : data ? <Empty className="min-h-64 w-full border border-dashed border-border"><EmptyHeader><EmptyTitle>No eligible comparison rows</EmptyTitle><EmptyDescription>The selected cohort contains no supported values for this chart mode.</EmptyDescription></EmptyHeader></Empty> : <Skeleton className="min-h-64 w-full" />}
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><ChartNoAxesCombined aria-hidden="true" />Metric definition</CardTitle>
          </CardHeader>
          <CardContent className="text-sm leading-relaxed text-muted-foreground">{config.explanation}</CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Interpretation boundary</CardTitle>
          </CardHeader>
          <CardContent className="text-sm leading-relaxed text-muted-foreground">{config.caveat}</CardContent>
        </Card>
      </section>

      {data ? <ComparisonTable rows={data.comparison_rows} /> : null}
      {data ? <SessionTable rows={data.sessions} /> : null}
    </div>
  );
}
