import * as React from "react";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
import {
  fetchTokenEfficiencyIndex,
  fetchTokenEfficiencyProject,
  type TokenEfficiencyContributor,
  type TokenEfficiencyDistribution,
  type TokenEfficiencyGrain,
  type TokenEfficiencyIndexPayload,
  type TokenEfficiencyPatternRow,
  type TokenEfficiencyPeriodComparison,
  type TokenEfficiencyPeriodSummary,
  type TokenEfficiencyProjectIndexRow,
  type TokenEfficiencyProjectPayload,
  type TokenEfficiencyUnit,
} from "@/api";
import { MetricCard } from "@/components/metric-card";
import { RouteHeader } from "@/components/route-header";
import { SessionLink } from "@/components/session-link";
import { StateBlock } from "@/components/state-block";
import { useDateRange } from "@/hooks/use-date-range";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { MetricSkeleton, TableSkeleton } from "@/components/ui/skeleton";
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

type EfficiencySearch = {
  grain: TokenEfficiencyGrain | undefined;
  unit: TokenEfficiencyUnit | undefined;
};

type SearchChange = (patch: Partial<EfficiencySearch>) => void;
type EfficiencySection = "overview" | "patterns" | "hotspots" | "outliers";

const numberFormatter = new Intl.NumberFormat();
const compactFormatter = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});
const percentFormatter = new Intl.NumberFormat(undefined, {
  style: "percent",
  maximumFractionDigits: 1,
});

function formatCount(value: number | null | undefined) {
  return numberFormatter.format(value ?? 0);
}

function formatTokens(value: number | null | undefined) {
  return compactFormatter.format(value ?? 0);
}

function formatExactTokens(value: number | null | undefined) {
  return `${numberFormatter.format(Math.round(value ?? 0))} tokens`;
}

function formatPercent(value: number | null | undefined) {
  return percentFormatter.format(value ?? 0);
}

function formatDelta(value: number | null | undefined) {
  if (value == null) return "No baseline";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

function formatRatePointDelta(value: number) {
  const points = value * 100;
  const sign = points > 0 ? "+" : "";
  return `${sign}${points.toFixed(1)} pp`;
}

function formatLabel(value: string | null | undefined) {
  if (!value) return "Unclassified";
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function shortId(value: string | null | undefined) {
  if (!value) return "—";
  return value.length > 12 ? value.slice(0, 12) : value;
}

function distributionFor(
  summary: TokenEfficiencyPeriodSummary,
  unit: TokenEfficiencyUnit,
) {
  return unit === "turn" ? summary.turn_prompt : summary.session_prompt;
}

function patternDistribution(
  row: TokenEfficiencyPatternRow,
  unit: TokenEfficiencyUnit,
  mode: "zero_inclusive" | "conditional" = "zero_inclusive",
) {
  return row.current[mode][unit];
}

function deltaForUnit(
  comparison: TokenEfficiencyPeriodComparison,
  unit: TokenEfficiencyUnit,
  metric: "median" | "p90",
) {
  const key = `${unit}_${metric}_pct` as const;
  return comparison.deltas[key];
}

function patternDeltaForUnit(
  row: TokenEfficiencyPatternRow,
  unit: TokenEfficiencyUnit,
  metric: "median" | "p90",
) {
  const key = `${unit}_${metric}_pct` as const;
  return row.deltas[key] ?? null;
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

function useEfficiencyProject(
  projectName: string,
) {
  const { days: sinceDays } = useDateRange();
  return useQuery({
    queryKey: ["token-efficiency-project", projectName, sinceDays],
    queryFn: ({ signal }) =>
      fetchTokenEfficiencyProject({
        projectName,
        sinceDays,
        signal,
      }),
  });
}

function EfficiencyScope({
  projectName,
  section,
  grain,
  unit,
  comparisonDays,
  discoveryDays,
  onSearchChange,
}: {
  projectName: string;
  section: EfficiencySection;
  grain: TokenEfficiencyGrain;
  unit: TokenEfficiencyUnit;
  comparisonDays: number;
  discoveryDays: number;
  onSearchChange: SearchChange;
}) {
  const { days: requestedDays } = useDateRange();
  const search = { grain, unit };
  const links = [
    {
      key: "overview" as const,
      label: "Overview",
      to: "/token-efficiency/$projectName" as const,
    },
    {
      key: "patterns" as const,
      label: "Patterns",
      to: "/token-efficiency/$projectName/patterns" as const,
    },
    {
      key: "hotspots" as const,
      label: "Hotspots",
      to: "/token-efficiency/$projectName/hotspots" as const,
    },
    {
      key: "outliers" as const,
      label: "Outliers",
      to: "/token-efficiency/$projectName/outliers" as const,
    },
  ];

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h3 className="m-0 title-card">Comparison scope</h3>
        </CardTitle>
        <CardDescription>
          Completed {grain === "daily" ? "days" : "weeks"} · {comparisonDays}-day
          trend
          {requestedDays > comparisonDays
            ? ` (capped from the ${requestedDays}-day global range)`
            : ""}{" "}
          · {discoveryDays}-day discovery window · values shown per {unit}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-4">
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
        </div>
        <nav className="flex flex-wrap gap-2" aria-label="Token efficiency views">
          {links.map((link) => (
            <Button
              key={link.key}
              asChild
              size="sm"
              variant={section === link.key ? "default" : "outline"}
            >
              <Link to={link.to} params={{ projectName }} search={search}>
                {link.label}
              </Link>
            </Button>
          ))}
        </nav>
      </CardContent>
    </Card>
  );
}

function CoverageCard({
  data,
}: {
  data: Pick<
    TokenEfficiencyProjectPayload | TokenEfficiencyIndexPayload,
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
  const hasTurnDetail = typeof data.coverage.turns === "number";

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h3 className="m-0 title-card">Coverage and attribution</h3>
        </CardTitle>
        <CardDescription>
          {hasTurnDetail
            ? "Period totals use exact completed-turn usage. Pattern and resource figures allocate tool-associated prompt cost and are diagnostic attribution."
            : "Index totals are cumulative graph usage for the recently modified discovery cohort."}
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

function ProjectPage({
  projectName,
  section,
  grain,
  unit,
  onSearchChange,
  data,
  children,
}: {
  projectName: string;
  section: EfficiencySection;
  grain: TokenEfficiencyGrain;
  unit: TokenEfficiencyUnit;
  onSearchChange: SearchChange;
  data: TokenEfficiencyProjectPayload;
  children: React.ReactNode;
}) {
  return (
    <div className="route-container w-full min-w-0 overflow-hidden">
      <RouteHeader eyebrow="Token efficiency" title={data.project.display_name || projectName} />
      <EfficiencyScope
        projectName={projectName}
        section={section}
        grain={grain}
        unit={unit}
        comparisonDays={data.filters.since_days}
        discoveryDays={data.filters.discovery_days}
        onSearchChange={onSearchChange}
      />
      {children}
      <CoverageCard data={data} />
    </div>
  );
}

function ProjectLoading({ projectName }: { projectName: string }) {
  return (
    <div className="route-container">
      <RouteHeader eyebrow="Token efficiency" title={projectName} />
      <StateBlock
        title="Collecting the project snapshot"
        detail="The first historical load can take several minutes. Daily, weekly, pattern, hotspot, and outlier pages will reuse this same cached snapshot."
      />
      <section className="stat-grid" aria-label="Loading token efficiency metrics">
        {Array.from({ length: 4 }, (_, index) => (
          <MetricSkeleton key={index} />
        ))}
      </section>
      <TableSkeleton rows={6} cols={6} />
    </div>
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
  const chartConfig = {
    median: { label: "Median", color: "var(--chart-1)" },
    p90: { label: "P90", color: "var(--chart-2)" },
  } satisfies ChartConfig;
  const rows = summaries.map((summary) => {
    const distribution = distributionFor(summary, unit);
    return {
      bucket: summary.bucket,
      label: summary.label,
      median: distribution.median,
      p90: distribution.p90,
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
          Median and P90 prompt-token cost across completed {unit}s.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {rows.length ? (
          <>
            <ChartContainer
              config={chartConfig}
              className="h-[18rem] w-full"
              aria-label={`${grain} ${unit} median and P90 prompt-token trend`}
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
                            {name === "p90" ? "P90" : "Median"}
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
                  dataKey="median"
                  stroke="var(--color-median)"
                  strokeWidth={2}
                  dot={false}
                />
                <Line
                  type="monotone"
                  dataKey="p90"
                  stroke="var(--color-p90)"
                  strokeWidth={2}
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

function ContributorsTable({
  contributors,
  caption,
}: {
  contributors: TokenEfficiencyContributor[];
  caption: string;
}) {
  if (!contributors.length) {
    return <StateBlock title="No contributors" detail="No attributed session-turn contributors were found." />;
  }
  return (
    <Table>
      <TableCaption>{caption}</TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Session</TableHead>
          <TableHead scope="col">Turn</TableHead>
          <TableHead scope="col">Title</TableHead>
          <TableHead scope="col" className="text-right">Calls</TableHead>
          <TableHead scope="col" className="text-right">Repeated</TableHead>
          <TableHead scope="col" className="text-right">Prompt tokens</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {contributors.map((contributor, index) => (
          <TableRow key={`${contributor.session_id}-${contributor.turn_id ?? "none"}-${index}`}>
            <TableCell className="font-mono">
              <SessionLink sessionId={contributor.session_id} />
            </TableCell>
            <TableCell className="font-mono" title={contributor.turn_id ?? undefined}>
              {shortId(contributor.turn_id)}
            </TableCell>
            <TableCell className="max-w-[24rem] truncate">
              {contributor.title ?? "—"}
            </TableCell>
            <TableCell className="text-right font-mono">
              {formatCount(contributor.calls)}
            </TableCell>
            <TableCell className="text-right font-mono">
              {formatCount(contributor.repeated_calls)}
            </TableCell>
            <TableCell
              className="text-right font-mono"
              title={formatExactTokens(contributor.prompt_tokens)}
            >
              {formatTokens(contributor.prompt_tokens)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function TokenEfficiencyIndexRoute() {
  const { days: sinceDays } = useDateRange();
  const query = useQuery({
    queryKey: ["token-efficiency-index", sinceDays],
    queryFn: ({ signal }) => fetchTokenEfficiencyIndex({ sinceDays, signal }),
  });

  if (query.isPending) {
    return (
      <div className="route-container">
        <RouteHeader eyebrow="Token efficiency" title="Project baselines" />
        <TableSkeleton rows={7} cols={6} />
      </div>
    );
  }
  if (query.isError) {
    return <StateBlock title="Token efficiency unavailable" detail={query.error.message} />;
  }

  const data = query.data;
  const rows: TokenEfficiencyProjectIndexRow[] = data.projects;
  const rootGraphs = rows.reduce((sum, row) => sum + (row.root_graphs ?? 0), 0);
  const promptTokens = rows.reduce((sum, row) => sum + (row.prompt_tokens ?? 0), 0);

  return (
    <div className="route-container w-full min-w-0 overflow-hidden">
      <RouteHeader eyebrow="Token efficiency" title="Project baselines" />
      <section className="stat-grid" aria-label="Token efficiency project summary">
        <MetricCard
          label="Projects"
          value={rows.length}
          detail={`Graphs modified in the last ${data.filters.since_days} day${data.filters.since_days === 1 ? "" : "s"}${sinceDays > data.filters.since_days ? ` (capped from ${sinceDays})` : ""}`}
        />
        <MetricCard
          label="Root graphs"
          value={rootGraphs}
          detail="Independent graph-level billing units"
        />
        <MetricCard
          label="Prompt tokens"
          value={formatTokens(promptTokens)}
          detail="Cumulative usage of discovered graphs"
        />
        <MetricCard
          label="Mean project median"
          value={formatTokens(
            rows.length
              ? rows.reduce((sum, row) => sum + (row.graph_prompt?.median ?? 0), 0) /
                  rows.length
              : 0,
          )}
          detail="Unweighted mean of project medians"
        />
      </section>
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 title-card">Projects</h3>
          </CardTitle>
          <CardDescription>
            Choose a project to compare completed periods and inspect generic
            tool patterns, project-specific hotspots, and outlier turns.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {rows.length ? (
            <Table>
              <TableCaption>
                Cumulative graph usage for roots discovered by recent file modification.
              </TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Project</TableHead>
                  <TableHead scope="col" className="text-right">Root graphs</TableHead>
                  <TableHead scope="col" className="text-right">Prompt tokens</TableHead>
                  <TableHead scope="col" className="text-right">Average / graph</TableHead>
                  <TableHead scope="col" className="text-right">Median / graph</TableHead>
                  <TableHead scope="col" className="text-right">P90 / graph</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.project_name}>
                    <TableCell>
                      <Link
                        to="/token-efficiency/$projectName"
                        params={{ projectName: row.project_name }}
                        search={{ grain: "weekly", unit: "session" }}
                        className="link font-display font-extrabold"
                      >
                        {row.display_name}
                      </Link>
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatCount(row.root_graphs)}
                    </TableCell>
                    <TableCell
                      className="text-right font-mono"
                      title={formatExactTokens(row.prompt_tokens)}
                    >
                      {formatTokens(row.prompt_tokens)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatTokens(row.graph_prompt?.avg)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatTokens(row.graph_prompt?.median)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatTokens(row.graph_prompt?.p90)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <StateBlock
              title="No project telemetry"
              detail="No project graphs were observed in the selected date range."
            />
          )}
        </CardContent>
      </Card>
      <CoverageCard data={data} />
    </div>
  );
}

export function TokenEfficiencyProjectRoute() {
  const { projectName } = useParams({ from: "/token-efficiency/$projectName" });
  const search = useSearch({ from: "/token-efficiency/$projectName" });
  const grain = search.grain ?? "weekly";
  const unit = search.unit ?? "session";
  const navigate = useNavigate({ from: "/token-efficiency/$projectName" });
  const query = useEfficiencyProject(projectName);
  const onSearchChange: SearchChange = (patch) => {
    void navigate({ search: (current) => ({ ...current, ...patch }) });
  };

  if (query.isPending) return <ProjectLoading projectName={projectName} />;
  if (query.isError) {
    return <StateBlock title="Project efficiency unavailable" detail={query.error.message} />;
  }

  const data = query.data;
  const comparison = data.comparisons[grain];
  if (!comparison) {
    return (
      <ProjectPage
        projectName={projectName}
        section="overview"
        grain={grain}
        unit={unit}
        onSearchChange={onSearchChange}
        data={data}
      >
        <StateBlock
          title="No completed comparison period"
          detail="Select another grain or expand the global date range."
        />
      </ProjectPage>
    );
  }
  const current = comparison.current;
  const distribution = distributionFor(current, unit);
  const unitCount = unit === "session" ? current.session_count : current.turn_count;
  const topPatterns = data.patterns[grain]
    .filter((pattern) => pattern.kind === "exclusive")
    .slice(0, 3);
  const topHotspots = data.hotspots[grain].slice(0, 3);

  return (
    <ProjectPage
      projectName={projectName}
      section="overview"
      grain={grain}
      unit={unit}
      onSearchChange={onSearchChange}
      data={data}
    >
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 title-card">
              {current.label} compared with {comparison.previous?.label ?? "no baseline"}
            </h3>
          </CardTitle>
          <CardDescription>
            Full sessions are bucketed by session completion; turn distributions
            are bucketed independently by turn completion in the dashboard host timezone.
          </CardDescription>
        </CardHeader>
      </Card>
      <section className="stat-grid" aria-label={`${unit} prompt-token distribution`}>
        <MetricCard
          label="Total prompt tokens"
          value={formatTokens(current.total_prompt_tokens)}
          detail={`${formatCount(current.session_count)} sessions · ${formatCount(current.turn_count)} turns`}
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
      <div className="grid min-w-0 gap-6 xl:grid-cols-2">
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>
              <h3 className="m-0 title-card">Generic tool-pattern composition</h3>
            </CardTitle>
            <CardDescription>
              Exclusive structural classifications; overlapping diagnostic
              indicators remain available in the full pattern view.
            </CardDescription>
            <CardAction>
              <Button asChild variant="outline" size="sm">
                <Link
                  to="/token-efficiency/$projectName/patterns"
                  params={{ projectName }}
                  search={{ grain, unit }}
                >
                  View all
                </Link>
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            {topPatterns.length ? (
              <Table>
                <TableCaption>Highest attributed patterns in {current.label}.</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead scope="col">Pattern</TableHead>
                    <TableHead scope="col" className="text-right">Share</TableHead>
                    <TableHead scope="col" className="text-right">P90 / {unit}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {topPatterns.map((pattern) => (
                    <TableRow key={pattern.key}>
                      <TableCell>
                        <Link
                          to="/token-efficiency/$projectName/patterns/$patternKey"
                          params={{ projectName, patternKey: pattern.key }}
                          search={{ grain, unit }}
                          className="link font-medium"
                        >
                          {pattern.label}
                        </Link>
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        {formatPercent(pattern.current.token_share)}
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        {formatTokens(patternDistribution(pattern, unit).p90)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <StateBlock title="No attributed patterns" />
            )}
          </CardContent>
        </Card>
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>
              <h3 className="m-0 title-card">Project hotspots</h3>
            </CardTitle>
            <CardDescription>
              Business-specific resources are kept separate from the generic classifier.
            </CardDescription>
            <CardAction>
              <Button asChild variant="outline" size="sm">
                <Link
                  to="/token-efficiency/$projectName/hotspots"
                  params={{ projectName }}
                  search={{ grain, unit }}
                >
                  View all
                </Link>
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            {topHotspots.length ? (
              <Table>
                <TableCaption>Highest enclosing prompt-token resource hotspots.</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead scope="col">Resource</TableHead>
                    <TableHead scope="col">Status</TableHead>
                    <TableHead scope="col" className="text-right">Enclosing tokens</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {topHotspots.map((hotspot) => (
                    <TableRow key={hotspot.key}>
                      <TableCell className="max-w-[22rem] truncate">
                        <Link
                          to="/token-efficiency/$projectName/hotspots/$hotspotKey"
                          params={{ projectName, hotspotKey: hotspot.key }}
                          search={{ grain, unit }}
                          className="link font-mono"
                          title={hotspot.resource}
                        >
                          {hotspot.resource}
                        </Link>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">{formatLabel(hotspot.status)}</Badge>
                      </TableCell>
                      <TableCell
                        className="text-right font-mono"
                        title={formatExactTokens(hotspot.enclosing_prompt_tokens)}
                      >
                        {formatTokens(hotspot.enclosing_prompt_tokens)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <StateBlock title="No repeated resource hotspots" />
            )}
          </CardContent>
        </Card>
      </div>
    </ProjectPage>
  );
}

export function TokenEfficiencyPatternsRoute() {
  const { projectName } = useParams({
    from: "/token-efficiency/$projectName/patterns",
  });
  const search = useSearch({ from: "/token-efficiency/$projectName/patterns" });
  const grain = search.grain ?? "weekly";
  const unit = search.unit ?? "session";
  const navigate = useNavigate({
    from: "/token-efficiency/$projectName/patterns",
  });
  const query = useEfficiencyProject(projectName);
  const onSearchChange: SearchChange = (patch) => {
    void navigate({ search: (current) => ({ ...current, ...patch }) });
  };

  if (query.isPending) return <ProjectLoading projectName={projectName} />;
  if (query.isError) {
    return <StateBlock title="Pattern metrics unavailable" detail={query.error.message} />;
  }
  const data = query.data;

  return (
    <ProjectPage
      projectName={projectName}
      section="patterns"
      grain={grain}
      unit={unit}
      onSearchChange={onSearchChange}
      data={data}
    >
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 title-card">Generic tool patterns</h3>
          </CardTitle>
          <CardDescription>
            Average, median, and P90 use zero-inclusive {unit} distributions,
            so unaffected units remain in the baseline. Open a pattern for its
            conditional distribution and contributors. Calls, incidence, token
            share, and contributors use turn-period activity.
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
                      <TableCell>
                        <Link
                          to="/token-efficiency/$projectName/patterns/$patternKey"
                          params={{ projectName, patternKey: pattern.key }}
                          search={{ grain, unit }}
                          className="link font-medium"
                        >
                          {pattern.label}
                        </Link>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline">
                          {pattern.kind === "indicator" ? "Overlapping indicator" : "Exclusive"}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        {formatCount(pattern.current.calls)}
                      </TableCell>
                      <TableCell className="text-right font-mono">
                        {formatPercent(pattern.current.incidence_rate)}
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
                        {formatPercent(pattern.current.token_share)}
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
    </ProjectPage>
  );
}

export function TokenEfficiencyPatternDetailRoute() {
  const { projectName, patternKey } = useParams({
    from: "/token-efficiency/$projectName/patterns/$patternKey",
  });
  const search = useSearch({
    from: "/token-efficiency/$projectName/patterns/$patternKey",
  });
  const grain = search.grain ?? "weekly";
  const unit = search.unit ?? "session";
  const navigate = useNavigate({
    from: "/token-efficiency/$projectName/patterns/$patternKey",
  });
  const query = useEfficiencyProject(projectName);
  const onSearchChange: SearchChange = (patch) => {
    void navigate({ search: (current) => ({ ...current, ...patch }) });
  };

  if (query.isPending) return <ProjectLoading projectName={projectName} />;
  if (query.isError) {
    return <StateBlock title="Pattern detail unavailable" detail={query.error.message} />;
  }
  const data = query.data;
  const pattern = data.patterns[grain].find((row) => row.key === patternKey);
  if (!pattern) {
    return (
      <ProjectPage
        projectName={projectName}
        section="patterns"
        grain={grain}
        unit={unit}
        onSearchChange={onSearchChange}
        data={data}
      >
        <StateBlock
          title="Pattern not present"
          detail="This pattern has no calls in either selected comparison period."
        />
      </ProjectPage>
    );
  }

  const zeroInclusive = patternDistribution(pattern, unit);
  const conditional = patternDistribution(pattern, unit, "conditional");
  const contributors = pattern.contributors;

  return (
    <ProjectPage
      projectName={projectName}
      section="patterns"
      grain={grain}
      unit={unit}
      onSearchChange={onSearchChange}
      data={data}
    >
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 title-card">{pattern.label}</h3>
          </CardTitle>
          <CardDescription>
            {pattern.kind === "indicator"
              ? "Overlapping diagnostic indicator; its tokens also belong to an exclusive tool class."
              : "Exclusive generic tool classification."}
          </CardDescription>
          <CardAction>
            <Badge variant="outline">
              {pattern.kind === "indicator" ? "Indicator" : "Exclusive"}
            </Badge>
          </CardAction>
        </CardHeader>
      </Card>
      <section className="stat-grid" aria-label={`${pattern.label} metrics`}>
        <MetricCard
          label="Attributed prompt tokens"
          value={formatTokens(pattern.current.total_prompt_tokens)}
          detail={`${formatPercent(pattern.current.token_share)} of turn-period prompt activity`}
          trend={trendBadge(pattern.deltas.prompt_tokens_pct)}
        />
        <MetricCard
          label="Incidence"
          value={formatPercent(pattern.current.incidence_rate)}
          detail={`${formatCount(pattern.current.incidence_count)} affected active sessions · ${formatRatePointDelta(pattern.deltas.incidence_rate_points)}`}
        />
        <MetricCard
          label={`Median per ${unit}`}
          value={formatTokens(zeroInclusive.median)}
          detail="Zero-inclusive project baseline"
          trend={trendBadge(patternDeltaForUnit(pattern, unit, "median"))}
        />
        <MetricCard
          label={`P90 per ${unit}`}
          value={formatTokens(zeroInclusive.p90)}
          detail="Zero-inclusive project baseline"
          trend={trendBadge(patternDeltaForUnit(pattern, unit, "p90"))}
        />
      </section>
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 title-card">Distribution semantics</h3>
          </CardTitle>
          <CardDescription>
            Zero-inclusive answers “what does a typical project {unit} cost from
            this pattern?” Conditional answers “what does it cost when the
            pattern occurs?”
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableCaption>
              Current-period {unit} prompt-token distribution for {pattern.label}.
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Population</TableHead>
                <TableHead scope="col" className="text-right">Count</TableHead>
                <TableHead scope="col" className="text-right">Average</TableHead>
                <TableHead scope="col" className="text-right">Median</TableHead>
                <TableHead scope="col" className="text-right">P90</TableHead>
                <TableHead scope="col" className="text-right">P95</TableHead>
                <TableHead scope="col" className="text-right">Max</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {[
                ["All project units", zeroInclusive],
                ["Units with pattern", conditional],
              ].map(([label, distribution]) => {
                const item = distribution as TokenEfficiencyDistribution;
                return (
                  <TableRow key={label as string}>
                    <TableCell>{label as string}</TableCell>
                    <TableCell className="text-right font-mono">{formatCount(item.count)}</TableCell>
                    <TableCell className="text-right font-mono">{formatTokens(item.avg)}</TableCell>
                    <TableCell className="text-right font-mono">{formatTokens(item.median)}</TableCell>
                    <TableCell className="text-right font-mono">{formatTokens(item.p90)}</TableCell>
                    <TableCell className="text-right font-mono">{formatTokens(item.p95)}</TableCell>
                    <TableCell className="text-right font-mono">{formatTokens(item.max)}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 title-card">Structural indicators</h3>
          </CardTitle>
          <CardDescription>
            These turn-period call counts can overlap.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          <Badge variant="secondary">
            Repeated resource access: {formatCount(pattern.current.indicators.repeated_read)}
          </Badge>
          <Badge variant="secondary">
            Parallel fan-out: {formatCount(pattern.current.indicators.parallel_fanout)}
          </Badge>
          <Badge variant="secondary">
            Truncated output: {formatCount(pattern.current.indicators.truncated_output)}
          </Badge>
        </CardContent>
      </Card>
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 title-card">Top session-turn contributors</h3>
          </CardTitle>
          <CardDescription>
            Ranked turn-period attributed prompt cost; open a session for its
            context-window evidence.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ContributorsTable
            contributors={contributors}
            caption={`Top contributors to ${pattern.label}.`}
          />
        </CardContent>
      </Card>
    </ProjectPage>
  );
}

export function TokenEfficiencyHotspotsRoute() {
  const { projectName } = useParams({
    from: "/token-efficiency/$projectName/hotspots",
  });
  const search = useSearch({ from: "/token-efficiency/$projectName/hotspots" });
  const grain = search.grain ?? "weekly";
  const unit = search.unit ?? "session";
  const navigate = useNavigate({
    from: "/token-efficiency/$projectName/hotspots",
  });
  const query = useEfficiencyProject(projectName);
  const onSearchChange: SearchChange = (patch) => {
    void navigate({ search: (current) => ({ ...current, ...patch }) });
  };

  if (query.isPending) return <ProjectLoading projectName={projectName} />;
  if (query.isError) {
    return <StateBlock title="Hotspot metrics unavailable" detail={query.error.message} />;
  }
  const data = query.data;

  return (
    <ProjectPage
      projectName={projectName}
      section="hotspots"
      grain={grain}
      unit={unit}
      onSearchChange={onSearchChange}
      data={data}
    >
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
                    <TableCell className="max-w-[26rem] truncate">
                      <Link
                        to="/token-efficiency/$projectName/hotspots/$hotspotKey"
                        params={{ projectName, hotspotKey: hotspot.key }}
                        search={{ grain, unit }}
                        className="link font-mono"
                        title={hotspot.resource}
                      >
                        {hotspot.resource}
                      </Link>
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
                      {formatPercent(hotspot.largest_call_share)}
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
    </ProjectPage>
  );
}

export function TokenEfficiencyHotspotDetailRoute() {
  const { projectName, hotspotKey } = useParams({
    from: "/token-efficiency/$projectName/hotspots/$hotspotKey",
  });
  const search = useSearch({
    from: "/token-efficiency/$projectName/hotspots/$hotspotKey",
  });
  const grain = search.grain ?? "weekly";
  const unit = search.unit ?? "session";
  const navigate = useNavigate({
    from: "/token-efficiency/$projectName/hotspots/$hotspotKey",
  });
  const query = useEfficiencyProject(projectName);
  const onSearchChange: SearchChange = (patch) => {
    void navigate({ search: (current) => ({ ...current, ...patch }) });
  };

  if (query.isPending) return <ProjectLoading projectName={projectName} />;
  if (query.isError) {
    return <StateBlock title="Hotspot detail unavailable" detail={query.error.message} />;
  }
  const data = query.data;
  const hotspot = data.hotspots[grain].find((row) => row.key === hotspotKey);
  if (!hotspot) {
    return (
      <ProjectPage
        projectName={projectName}
        section="hotspots"
        grain={grain}
        unit={unit}
        onSearchChange={onSearchChange}
        data={data}
      >
        <StateBlock
          title="Hotspot not present"
          detail="This resource does not meet the hotspot threshold in the selected periods."
        />
      </ProjectPage>
    );
  }
  const hotspotDistribution = unit === "session" ? hotspot.session : hotspot.turn;

  return (
    <ProjectPage
      projectName={projectName}
      section="hotspots"
      grain={grain}
      unit={unit}
      onSearchChange={onSearchChange}
      data={data}
    >
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 break-all font-mono title-card">{hotspot.resource}</h3>
          </CardTitle>
          <CardDescription>
            Project-specific phase signal · enclosing cost is non-additive across resources.
          </CardDescription>
          <CardAction>
            <Badge variant="outline">{formatLabel(hotspot.status)}</Badge>
          </CardAction>
        </CardHeader>
      </Card>
      <section className="stat-grid" aria-label={`${hotspot.resource} hotspot metrics`}>
        <MetricCard
          label="Enclosing prompt tokens"
          value={formatTokens(hotspot.enclosing_prompt_tokens)}
          detail={formatExactTokens(hotspot.enclosing_prompt_tokens)}
          trend={trendBadge(hotspot.delta_pct)}
        />
        <MetricCard
          label="Calls"
          value={hotspot.calls}
          detail={`${formatCount(hotspot.repeat_count)} repeated · ${formatCount(hotspot.sessions)} sessions`}
        />
        <MetricCard
          label={`Median per ${unit}`}
          value={formatTokens(hotspotDistribution.median)}
          detail={`${formatCount(hotspotDistribution.count)} affected ${unit}${hotspotDistribution.count === 1 ? "" : "s"}`}
        />
        <MetricCard
          label={`P90 per ${unit}`}
          value={formatTokens(hotspotDistribution.p90)}
          detail={`Largest call ${formatTokens(hotspot.largest_call_tokens)} · ${formatPercent(hotspot.largest_call_share)} of total`}
        />
      </section>
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 title-card">Tool-pattern breakdown</h3>
          </CardTitle>
          <CardDescription>
            Broad and targeted search/read are exclusive classifications; other
            tool classes account for the remaining resource calls.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableCaption>Generic classification of calls mentioning this resource.</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Class</TableHead>
                <TableHead scope="col" className="text-right">Calls</TableHead>
                <TableHead scope="col" className="text-right">Share</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {[
                ["Broad / batched search and read", hotspot.broad_calls],
                ["Targeted search and read", hotspot.targeted_calls],
                [
                  "Other tool classes",
                  Math.max(0, hotspot.calls - hotspot.broad_calls - hotspot.targeted_calls),
                ],
              ].map(([label, calls]) => (
                <TableRow key={label as string}>
                  <TableCell>{label as string}</TableCell>
                  <TableCell className="text-right font-mono">{formatCount(calls as number)}</TableCell>
                  <TableCell className="text-right font-mono">
                    {formatPercent(hotspot.calls ? (calls as number) / hotspot.calls : 0)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>
            <h3 className="m-0 title-card">Top session-turn contributors</h3>
          </CardTitle>
          <CardDescription>
            Ranked enclosing prompt cost for calls that mention this resource.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ContributorsTable
            contributors={hotspot.contributors}
            caption={`Top enclosing-cost contributors for ${hotspot.resource}.`}
          />
        </CardContent>
      </Card>
    </ProjectPage>
  );
}

export function TokenEfficiencyOutliersRoute() {
  const { projectName } = useParams({
    from: "/token-efficiency/$projectName/outliers",
  });
  const search = useSearch({ from: "/token-efficiency/$projectName/outliers" });
  const grain = search.grain ?? "weekly";
  const unit = search.unit ?? "turn";
  const navigate = useNavigate({
    from: "/token-efficiency/$projectName/outliers",
  });
  const query = useEfficiencyProject(projectName);
  const onSearchChange: SearchChange = (patch) => {
    void navigate({ search: (current) => ({ ...current, ...patch }) });
  };

  if (query.isPending) return <ProjectLoading projectName={projectName} />;
  if (query.isError) {
    return <StateBlock title="Outlier metrics unavailable" detail={query.error.message} />;
  }
  const data = query.data;

  return (
    <ProjectPage
      projectName={projectName}
      section="outliers"
      grain={grain}
      unit={unit}
      onSearchChange={onSearchChange}
      data={data}
    >
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
                      {formatPercent(outlier.session_share)}
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
    </ProjectPage>
  );
}

// Keep concise aliases available for callers that use the feature names
// without the route suffix.
export const TokenEfficiencyIndex = TokenEfficiencyIndexRoute;
export const TokenEfficiencyProject = TokenEfficiencyProjectRoute;
export const TokenEfficiencyPatterns = TokenEfficiencyPatternsRoute;
export const TokenEfficiencyPatternDetail = TokenEfficiencyPatternDetailRoute;
export const TokenEfficiencyHotspots = TokenEfficiencyHotspotsRoute;
export const TokenEfficiencyHotspotDetail = TokenEfficiencyHotspotDetailRoute;
export const TokenEfficiencyOutliers = TokenEfficiencyOutliersRoute;
