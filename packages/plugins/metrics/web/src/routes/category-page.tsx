import { ChartNoAxesCombined, DatabaseZap } from "lucide-react";

import { MetricCard } from "@/components/metric-card";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export type CategoryConfig = {
  title: string;
  description: string;
  modes: ReadonlyArray<{ value: string; label: string }>;
  highlights: ReadonlyArray<{ label: string; detail: string }>;
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
          <Badge variant="secondary">Last {sinceDays} days</Badge>
          <Badge variant="outline">All projects</Badge>
          <Badge variant="outline">All vendors</Badge>
          <Badge variant="outline">All models</Badge>
          <Badge variant="outline">All session graphs</Badge>
        </CardContent>
      </Card>

      <section className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,14rem),1fr))] gap-4" aria-label={`${config.title} highlights`}>
        {config.highlights.map((highlight) => <MetricCard key={highlight.label} {...highlight} />)}
      </section>

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
            <CardTitle>{selectedMode.label}</CardTitle>
            <CardDescription>Primary chart and accessible comparison table share this view.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-1">
            <Empty className="min-h-64 w-full border border-dashed border-border">
              <EmptyHeader>
                <EmptyMedia variant="icon"><DatabaseZap aria-hidden="true" /></EmptyMedia>
                <EmptyTitle>Cohort read model not connected</EmptyTitle>
                <EmptyDescription>The web shell is ready. A later service slice will connect <code>{config.endpoint}</code> without inventing placeholder metrics.</EmptyDescription>
              </EmptyHeader>
            </Empty>
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
    </div>
  );
}
