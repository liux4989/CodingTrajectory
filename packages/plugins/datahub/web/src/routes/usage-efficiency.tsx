import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
import {
  fetchTokenEfficiencyProject,
  type TokenEfficiencyGrain,
  type TokenEfficiencyPatternRow,
  type TokenEfficiencyPeriodComparison,
  type TokenEfficiencyPeriodSummary,
  type TokenEfficiencyProjectPayload,
  type TokenEfficiencyUnit,
} from "@/api";
import { MetricCard } from "@/components/metric-card";
import { LoadingShell } from "@/components/loading-shell";
import { SessionLink } from "@/components/session-link";
import { StateBlock } from "@/components/state-block";
import { useDateRange } from "@/hooks/use-date-range";
import {
  formatCount,
  formatDelta,
  formatExactTokens,
  formatLabel,
  formatShare,
  formatTokens,
  shortId,
} from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export type EfficiencySearchChange = (patch: Partial<{
  grain: TokenEfficiencyGrain;
  unit: TokenEfficiencyUnit;
}>) => void;

function distributionFor(
  summary: TokenEfficiencyPeriodSummary,
  unit: TokenEfficiencyUnit,
) {
  return unit === "turn" ? summary.turn_prompt : summary.session_prompt;
}

function patternDistribution(
  row: TokenEfficiencyPatternRow,
  unit: TokenEfficiencyUnit,
) {
  return row.current.zero_inclusive[unit];
}

function deltaForUnit(
  comparison: TokenEfficiencyPeriodComparison,
  unit: TokenEfficiencyUnit,
  metric: "median" | "p90",
) {
  const key = `${unit}_${metric}_pct` as const;
  return comparison.deltas[key];
}

function trendBadge(value: number | null | undefined) {
  if (value == null) return undefined;
  return {
    value: `${Math.abs(value).toFixed(1)}%`,
    direction: value <= 0 ? ("down" as const) : ("up" as const),
  };
}

function DeltaBadge({ value }: { value: number | null | undefined }) {
  return (
    <Badge variant="outline">
      {value == null ? "—" : value > 0 ? "▲" : value < 0 ? "▼" : "•"}{" "}
      {formatDelta(value)}
    </Badge>
  );
}

function useEfficiencyProject(projectName: string) {
  const { days: sinceDays } = useDateRange();
  return useQuery({
    queryKey: ["token-efficiency", "project", projectName, sinceDays],
    queryFn: ({ signal }) =>
      fetchTokenEfficiencyProject({ projectName, sinceDays, limit: 100, signal }),
    gcTime: 5 * 60_000,
  });
}

export function EfficiencyLens({
  projectName,
  grain,
  unit,
  onSearchChange,
}: {
  projectName: string | null;
  grain: TokenEfficiencyGrain;
  unit: TokenEfficiencyUnit;
  onSearchChange: EfficiencySearchChange;
}) {
  if (!projectName) {
    return (
      <StateBlock
        title="Select a project"
        detail="Efficiency trends, tool patterns, hotspots, and outlier turns are computed per project. Choose a project in the filter above."
      />
    );
  }
  return (
    <EfficiencyProject
      projectName={projectName}
      grain={grain}
      unit={unit}
      onSearchChange={onSearchChange}
    />
  );
}

function EfficiencyProject({
  projectName,
  grain,
  unit,
  onSearchChange,
}: {
  projectName: string;
  grain: TokenEfficiencyGrain;
  unit: TokenEfficiencyUnit;
  onSearchChange: EfficiencySearchChange;
}) {
  const query = useEfficiencyProject(projectName);

  if (query.isPending) {
    return (
      <LoadingShell
        eyebrow="Efficiency"
        title={`Collecting telemetry for ${projectName}`}
        variant="mixed"
      />
    );
  }
  if (query.isError || !query.data) {
    return (
      <StateBlock
        title="Project efficiency unavailable"
        detail={query.error?.message ?? "The collection did not return data."}
        onRetry={() => void query.refetch()}
      />
    );
  }

  const data = query.data;
  return (
    <div className="grid gap-4">
      <ScopeCard
        grain={grain}
        unit={unit}
        comparisonDays={data.filters.since_days}
        discoveryDays={data.filters.discovery_days}
        onSearchChange={onSearchChange}
      />
      <ComparisonSection data={data} grain={grain} unit={unit} />
      <PatternsCard data={data} grain={grain} unit={unit} />
      <HotspotsCard data={data} grain={grain} />
      <OutliersCard data={data} grain={grain} />
      <CoverageCard data={data} />
    </div>
  );
}

function ScopeCard({
  grain,
  unit,
  comparisonDays,
  discoveryDays,
  onSearchChange,
}: {
  grain: TokenEfficiencyGrain;
  unit: TokenEfficiencyUnit;
  comparisonDays: number;
  discoveryDays: number;
  onSearchChange: EfficiencySearchChange;
}) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h3 className="m-0 title-card">Comparison scope</h3>
        </CardTitle>
        <CardDescription>
          Completed {grain === "daily" ? "days" : "weeks"} · {comparisonDays}-day
          trend · {discoveryDays}-day discovery window · values shown per {unit}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center gap-4">
        <div className="flex flex-col gap-1">
          <span className="label-uppercase">Period</span>
          <ToggleGroup
            type="single"
            value={grain}
            variant="outline"
            size="sm"
            aria-label="Comparison period"
            onValueChange={(value) => {
              if (value === "daily" || value === "weekly") {
                onSearchChange({ grain: value });
              }
            }}
          >
            <ToggleGroupItem value="daily">Daily</ToggleGroupItem>
            <ToggleGroupItem value="weekly">Weekly</ToggleGroupItem>
          </ToggleGroup>
        </div>
        <div className="flex flex-col gap-1">
          <span className="label-uppercase">Unit</span>
          <ToggleGroup
            type="single"
            value={unit}
            variant="outline"
            size="sm"
            aria-label="Distribution unit"
            onValueChange={(value) => {
              if (value === "session" || value === "turn") {
                onSearchChange({ unit: value });
              }
            }}
          >
            <ToggleGroupItem value="session">Session</ToggleGroupItem>
            <ToggleGroupItem value="turn">Turn</ToggleGroupItem>
          </ToggleGroup>
        </div>
      </CardContent>
    </Card>
  );
}

function ComparisonSection({
  data,
  grain,
  unit,
}: {
  data: TokenEfficiencyProjectPayload;
  grain: TokenEfficiencyGrain;
  unit: TokenEfficiencyUnit;
}) {
  const comparison = data.comparisons[grain];
  if (!comparison) {
    return (
      <StateBlock
        title="No completed comparison period"
        detail="Select another grain or expand the global date range."
      />
    );
  }
  const current = comparison.current;
  const distribution = distributionFor(current, unit);
  const unitCount = unit === "session" ? current.session_count : current.turn_count;
  return (
    <>
      <section className="stat-grid" aria-label={`${unit} prompt-token distribution`}>
        <MetricCard
          label="Total prompt tokens"
          value={formatTokens(current.total_prompt_tokens)}
          detail={`${formatCount(current.session_count)} sessions · ${formatCount(current.turn_count)} turns · ${current.label}`}
          trend={trendBadge(comparison.deltas.total_prompt_tokens_pct)}
        />
        <MetricCard
          label={`Average per ${unit}`}
          value={formatTokens(distribution.avg)}
          detail={`${formatCount(unitCount)} completed ${unit}${unitCount === 1 ? "" : "s"}`}
        />
        <MetricCard
          label={`Median per ${unit}`}
          value={formatTokens(distribution.median)}
          detail={formatExactTokens(distribution.median)}
          trend={trendBadge(deltaForUnit(comparison, unit, "median"))}
        />
        <MetricCard
          label={`P90 per ${unit}`}
          value={formatTokens(distribution.p90)}
          detail={formatExactTokens(distribution.p90)}
          trend={trendBadge(deltaForUnit(comparison, unit, "p90"))}
        />
      </section>
      <TrendChart summaries={data.trends[grain]} unit={unit} grain={grain} />
    </>
  );
}

function TrendChart({
  summaries,
  unit,
  grain,
}: {
  summaries: TokenEfficiencyPeriodSummary[];
  unit: TokenEfficiencyUnit;
  grain: TokenEfficiencyGrain;
}) {
  const chartConfig: ChartConfig = {
    median: { label: "Median", color: "var(--chart-1)" },
    p90: { label: "P90", color: "var(--chart-2)" },
    p95: { label: "P95", color: "var(--chart-3)" },
    max: { label: "Max", color: "var(--chart-3)" },
  };
  const rows = summaries.map((summary) => {
    const distribution = distributionFor(summary, unit);
    return {
      bucket: summary.bucket,
      label: summary.label,
      median: distribution.median,
      p90: distribution.p90,
      p95: distribution.p95,
      max: distribution.max,
    };
  });

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h3 className="m-0 title-card">
            {grain === "daily" ? "Daily" : "Weekly"} {unit} trend
          </h3>
        </CardTitle>
        <CardDescription>
          Median, P90, P95, and max prompt-token cost across completed {unit}s.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {rows.length ? (
          <>
            <ChartContainer
              config={chartConfig}
              className="h-[18rem] w-full"
              aria-label={`${grain} ${unit} prompt-token distribution trend`}
            >
              <LineChart accessibilityLayer data={rows} margin={{ left: 8, right: 16 }}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="label" tickLine={false} axisLine={false} minTickGap={24} />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  width={56}
                  tickFormatter={(value) => formatTokens(Number(value))}
                />
                <ChartTooltip
                  cursor={false}
                  content={
                    <ChartTooltipContent
                      formatter={(value, name) => (
                        <div className="flex min-w-[9rem] items-center justify-between gap-3">
                          <span className="text-muted-foreground">
                            {chartConfig[String(name)]?.label ?? String(name)}
                          </span>
                          <span className="font-mono font-medium">
                            {formatExactTokens(Number(value))}
                          </span>
                        </div>
                      )}
                    />
                  }
                />
                <Line
                  type="monotone"
                  dataKey="max"
                  stroke="var(--color-max)"
                  strokeWidth={1}
                  strokeDasharray="4 4"
                  strokeOpacity={0.3}
                  dot={false}
                />
                <Line
                  type="monotone"
                  dataKey="p95"
                  stroke="var(--color-p95)"
                  strokeWidth={1.5}
                  strokeDasharray="4 4"
                  strokeOpacity={0.5}
                  dot={false}
                />
                <Line
                  type="monotone"
                  dataKey="p90"
                  stroke="var(--color-p90)"
                  strokeWidth={2}
                  dot={false}
                />
                <Line
                  type="monotone"
                  dataKey="median"
                  stroke="var(--color-median)"
                  strokeWidth={2.5}
                  dot={false}
                />
              </LineChart>
            </ChartContainer>
            <Table>
              <TableCaption>
                Exact values for the {grain} {unit} trend chart.
              </TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Period</TableHead>
                  <TableHead scope="col" className="text-right">Median</TableHead>
                  <TableHead scope="col" className="text-right">P90</TableHead>
                  <TableHead scope="col" className="text-right">P95</TableHead>
                  <TableHead scope="col" className="text-right">Max</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.bucket}>
                    <TableCell>{row.label}</TableCell>
                    <TableCell className="text-right font-mono">
                      {formatExactTokens(row.median)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatExactTokens(row.p90)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatExactTokens(row.p95)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatExactTokens(row.max)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </>
        ) : (
          <StateBlock title="No trend data" detail="No completed periods are available in this range." />
        )}
      </CardContent>
    </Card>
  );
}

function PatternsCard({
  data,
  grain,
  unit,
}: {
  data: TokenEfficiencyProjectPayload;
  grain: TokenEfficiencyGrain;
  unit: TokenEfficiencyUnit;
}) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h3 className="m-0 title-card">Generic tool patterns</h3>
        </CardTitle>
        <CardDescription>
          Average, median, and P90 use zero-inclusive {unit} distributions, so
          unaffected units remain in the baseline. Calls, incidence, and token
          share use turn-period activity.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {data.patterns[grain].length ? (
          <Table>
            <TableCaption>
              Generic structural tool patterns for the latest completed {grain === "daily" ? "day" : "week"}.
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Pattern</TableHead>
                <TableHead scope="col">Kind</TableHead>
                <TableHead scope="col" className="text-right">Calls</TableHead>
                <TableHead scope="col" className="text-right">Incidence</TableHead>
                <TableHead scope="col" className="text-right">Average / {unit}</TableHead>
                <TableHead scope="col" className="text-right">Median / {unit}</TableHead>
                <TableHead scope="col" className="text-right">P90 / {unit}</TableHead>
                <TableHead scope="col" className="text-right">Token share</TableHead>
                <TableHead scope="col" className="text-right">Period delta</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.patterns[grain].map((pattern) => {
                const distribution = patternDistribution(pattern, unit);
                return (
                  <TableRow key={pattern.key}>
                    <TableCell className="font-medium">{pattern.label}</TableCell>
                    <TableCell>
                      <Badge variant="outline">
                        {pattern.kind === "indicator" ? "Overlapping indicator" : "Exclusive"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatCount(pattern.current.calls)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatShare(pattern.current.incidence_rate)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatTokens(distribution.avg)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatTokens(distribution.median)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatTokens(distribution.p90)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatShare(pattern.current.token_share)}
                    </TableCell>
                    <TableCell className="text-right">
                      <DeltaBadge value={pattern.deltas.prompt_tokens_pct} />
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        ) : (
          <StateBlock
            title="No classified tool patterns"
            detail="No pattern calls were found in either comparison period."
          />
        )}
      </CardContent>
    </Card>
  );
}

function HotspotsCard({
  data,
  grain,
}: {
  data: TokenEfficiencyProjectPayload;
  grain: TokenEfficiencyGrain;
}) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h3 className="m-0 title-card">Project resource hotspots</h3>
        </CardTitle>
        <CardDescription>
          Resource names are project-specific phase signals from the
          completed-session cohort. Enclosing prompt totals overlap when one
          call references multiple resources and must not be summed.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {data.hotspots[grain].length ? (
          <Table>
            <TableCaption>
              Repeated or high-cost resources among sessions completed in the latest {grain === "daily" ? "day" : "week"}.
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Resource</TableHead>
                <TableHead scope="col">Status</TableHead>
                <TableHead scope="col" className="text-right">Sessions</TableHead>
                <TableHead scope="col" className="text-right">Turns</TableHead>
                <TableHead scope="col" className="text-right">Calls</TableHead>
                <TableHead scope="col" className="text-right">Repeated</TableHead>
                <TableHead scope="col" className="text-right">Broad / targeted</TableHead>
                <TableHead scope="col" className="text-right">Enclosing tokens</TableHead>
                <TableHead scope="col" className="text-right">Largest share</TableHead>
                <TableHead scope="col" className="text-right">Period delta</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.hotspots[grain].map((hotspot) => (
                <TableRow key={hotspot.key}>
                  <TableCell
                    className="max-w-[26rem] truncate font-mono"
                    title={hotspot.resource}
                  >
                    {hotspot.resource}
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline">{formatLabel(hotspot.status)}</Badge>
                  </TableCell>
                  <TableCell className="text-right font-mono">{formatCount(hotspot.sessions)}</TableCell>
                  <TableCell className="text-right font-mono">{formatCount(hotspot.turns)}</TableCell>
                  <TableCell className="text-right font-mono">{formatCount(hotspot.calls)}</TableCell>
                  <TableCell className="text-right font-mono">{formatCount(hotspot.repeat_count)}</TableCell>
                  <TableCell className="text-right font-mono">
                    {formatCount(hotspot.broad_calls)} / {formatCount(hotspot.targeted_calls)}
                  </TableCell>
                  <TableCell
                    className="text-right font-mono"
                    title={formatExactTokens(hotspot.enclosing_prompt_tokens)}
                  >
                    {formatTokens(hotspot.enclosing_prompt_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatShare(hotspot.largest_call_share)}
                  </TableCell>
                  <TableCell className="text-right">
                    <DeltaBadge value={hotspot.delta_pct} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <StateBlock
            title="No resource hotspots"
            detail="No resource met the repeated-call or high-cost threshold."
          />
        )}
      </CardContent>
    </Card>
  );
}

function OutliersCard({
  data,
  grain,
}: {
  data: TokenEfficiencyProjectPayload;
  grain: TokenEfficiencyGrain;
}) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h3 className="m-0 title-card">High-cost turns</h3>
        </CardTitle>
        <CardDescription>
          Turns flagged by current-period P90, concentration within their
          session, or high resident context.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {data.outliers[grain].length ? (
          <Table>
            <TableCaption>
              Flagged turns in the latest completed {grain === "daily" ? "day" : "week"}.
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Session</TableHead>
                <TableHead scope="col">Turn</TableHead>
                <TableHead scope="col">Title</TableHead>
                <TableHead scope="col" className="text-right">Prompt tokens</TableHead>
                <TableHead scope="col" className="text-right">Session share</TableHead>
                <TableHead scope="col" className="text-right">Max context</TableHead>
                <TableHead scope="col">Primary pattern</TableHead>
                <TableHead scope="col">Reasons</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.outliers[grain].map((outlier) => (
                <TableRow key={`${outlier.session_id}-${outlier.turn_id}`}>
                  <TableCell className="font-mono">
                    <SessionLink sessionId={outlier.session_id} />
                  </TableCell>
                  <TableCell className="font-mono" title={outlier.turn_id}>
                    {shortId(outlier.turn_id)}
                  </TableCell>
                  <TableCell className="max-w-[22rem] truncate">
                    {outlier.title ?? "—"}
                  </TableCell>
                  <TableCell
                    className="text-right font-mono"
                    title={formatExactTokens(outlier.prompt_tokens)}
                  >
                    {formatTokens(outlier.prompt_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatShare(outlier.session_share)}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {outlier.max_context_tokens == null
                      ? "—"
                      : formatTokens(outlier.max_context_tokens)}
                  </TableCell>
                  <TableCell>
                    {outlier.primary_pattern ? formatLabel(outlier.primary_pattern) : "—"}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {outlier.reason_codes.map((reason) => (
                        <Badge key={reason} variant="outline">
                          {formatLabel(reason)}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <StateBlock
            title="No high-cost turns"
            detail="No completed turn met the current-period outlier threshold."
          />
        )}
      </CardContent>
    </Card>
  );
}

function CoverageCard({
  data,
}: {
  data: Pick<
    TokenEfficiencyProjectPayload,
    "coverage" | "warnings" | "attribution"
  >;
}) {
  const coverageEntries = [
    ["Root graphs", data.coverage.root_graphs],
    ["Sessions", data.coverage.sessions],
    ["Turns", data.coverage.turns],
    ["Tool items", data.coverage.tool_items],
    ["Attributed tools", data.coverage.attributed_tool_items],
    ["Undated tools", data.coverage.undated_tool_items],
    ["Truncated inputs", data.coverage.truncated_input_summaries],
  ].filter((entry): entry is [string, number] => typeof entry[1] === "number");
  const billingAuthority = data.attribution.billing_authority;
  const hotspotAuthority = data.attribution.hotspot_costs;
  const timezone = data.attribution.period_timezone;
  const discoveryScope = data.attribution.discovery_scope;
  const periodAssignment = data.attribution.period_assignment;

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h3 className="m-0 title-card">Coverage and attribution</h3>
        </CardTitle>
        <CardDescription>
          Period totals use exact completed-turn usage. Pattern and resource
          figures allocate tool-associated prompt cost and are diagnostic
          attribution.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {coverageEntries.length ? (
          <div className="flex flex-wrap gap-2" aria-label="Telemetry coverage">
            {coverageEntries.map(([label, value]) => (
              <Badge key={label} variant="secondary">
                {label}: {formatCount(value)}
              </Badge>
            ))}
          </div>
        ) : null}
        {typeof billingAuthority === "string" ||
        typeof hotspotAuthority === "string" ||
        typeof timezone === "string" ||
        typeof periodAssignment === "string" ||
        typeof discoveryScope === "string" ? (
          <ul className="grid gap-1 text-body-sm text-muted-foreground" role="list">
            {typeof billingAuthority === "string" ? (
              <li>Billing: {billingAuthority}</li>
            ) : null}
            {typeof hotspotAuthority === "string" ? (
              <li>Hotspots: {hotspotAuthority}</li>
            ) : null}
            {typeof timezone === "string" ? <li>Periods: {timezone}</li> : null}
            {typeof periodAssignment === "string" ? (
              <li>Assignment: {periodAssignment}</li>
            ) : null}
            {typeof discoveryScope === "string" ? (
              <li>Discovery: {discoveryScope}</li>
            ) : null}
          </ul>
        ) : null}
        {data.warnings.length ? (
          <div className="flex flex-col gap-2">
            <p className="m-0 font-medium">Partial coverage warnings</p>
            <ul className="grid gap-1 text-body-sm text-muted-foreground" role="list">
              {data.warnings.map((warning, index) => (
                <li key={`${warning}-${index}`}>{warning}</li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="m-0 text-body-sm text-muted-foreground">
            No partial-coverage warnings were reported.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
